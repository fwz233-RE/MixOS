#!/usr/bin/env python3
"""Move staged models onto the deck and import them, resumably.

The link to this device is slow. Measured on 2026-09-13 over SSH to
`typixdeck`: 0.20 MB/s up and 0.12 MB/s down, on a Wi-Fi association reporting
130 Mbit/s. At that rate the 2.6 GB language model needs roughly three and a
half hours, and any transfer that cannot resume will never finish.

So this tool:

* refuses to start until the local manifest from ``tools/stage_models.py``
  matches the bytes on disk, because a three-hour transfer of the wrong file is
  worse than no transfer;
* sends in blocks and continues from whatever already arrived, checking the
  remote length before each block;
* verifies the SHA-256 **on the device** before importing, so a truncated or
  corrupted file is caught there rather than surfacing later as a model that
  loads and talks nonsense;
* reports the measured rate and the honest remaining time while it runs.

    $env:MIXOS_SSH_PASSWORD='...'
    py -3.12 tools/deploy_models.py --host 192.168.1.22
    py -3.12 tools/deploy_models.py --host 192.168.1.22 --verify-only

Over the USB maintenance link the same command works with a different address
and a larger block, because there the SSH handshake, not the data, is what
costs time:

    py -3.12 tools/usb_gadget.py --enable --host 192.168.1.22
    py -3.12 tools/deploy_models.py --host 10.12.194.1 --block-mb 64

If the estimate is unacceptable, the alternative is not a faster protocol - it
is a faster path. See docs/AI_DECK.md for what was measured and what the
options are.
"""
from __future__ import annotations

import argparse
import base64
import getpass
import hashlib
import json
import os
import shlex
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / 'build/models/manifest.json'
# What one append carries. Every block costs one SSH connection, which is
# nothing next to 20 seconds of Wi-Fi transfer and is most of the time on the
# USB link, where a block arrives in a fifth of a second. Hence --block-mb.
BLOCK = 4 << 20
CHUNK = 1 << 20


def digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open('rb') as handle:
        for block in iter(lambda: handle.read(CHUNK), b''):
            h.update(block)
    return h.hexdigest()


def human_time(seconds: float) -> str:
    if seconds < 90:
        return f'{seconds:.0f}s'
    if seconds < 5400:
        return f'{seconds / 60:.0f} min'
    return f'{seconds / 3600:.1f} h'


class Remote:
    """A password-authenticated SSH channel that never puts a secret in argv."""

    def __init__(self, host: str, user: str, password: str, temporary: Path):
        helper = temporary / ('askpass.cmd' if os.name == 'nt' else 'askpass.sh')
        helper.write_text('@echo off\necho %MIXOS_SSH_PASSWORD%\n' if os.name == 'nt' else
                          '#!/bin/sh\nprintf "%s\\n" "$MIXOS_SSH_PASSWORD"\n')
        helper.chmod(0o700)
        self.env = dict(os.environ, MIXOS_SSH_PASSWORD=password, SSH_ASKPASS=str(helper),
                        SSH_ASKPASS_REQUIRE='force',
                        DISPLAY=os.environ.get('DISPLAY') or 'unused:0')
        self.target = f'{user}@{host}'
        self.options = ['-o', 'StrictHostKeyChecking=accept-new',
                        '-o', 'PreferredAuthentications=password',
                        '-o', 'PubkeyAuthentication=no',
                        '-o', 'NumberOfPasswordPrompts=1',
                        '-o', 'ServerAliveInterval=15', '-o', 'ServerAliveCountMax=8']

    def run(self, script: str, *, data: bytes | None = None, timeout: int = 120,
            check: bool = True) -> subprocess.CompletedProcess:
        """Run a script on the device, with standard input left for the caller.

        The script is base64-encoded into the command line and decoded inside a
        command substitution, so ``bash -c`` receives it as an argument. The
        obvious alternative — piping the script into ``bash`` — hands the
        script to the remote shell *on standard input*, and then the first
        command in it that reads standard input eats the rest of the script
        instead of the bytes this method was given. That is silent: ``tar``
        reports a truncated archive and ``cat`` writes the tail of a shell
        script into the file it was meant to fill.

        Nothing secret travels in a script. Passwords go through ``data``, on
        standard input, where ``sudo -S`` reads them.
        """
        encoded = base64.b64encode(script.encode()).decode()
        command = 'bash -c "$(printf %s ' + encoded + ' | base64 -d)"'
        result = subprocess.run(['ssh', '-T'] + self.options + [self.target, command],
                                env=self.env, input=data, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, timeout=timeout)
        if check and result.returncode:
            raise RuntimeError(f'Remote command failed ({result.returncode}): '
                               f'{result.stderr.decode("utf-8", "replace")[-400:]}')
        return result

    def text(self, script: str, **kwargs) -> str:
        return self.run(script, **kwargs).stdout.decode('utf-8', 'replace').strip()


def remote_size(remote: Remote, path: str) -> int:
    answer = remote.text(f'stat -c %s {shlex.quote(path)} 2>/dev/null || echo 0')
    try:
        return int(answer.split()[0])
    except (ValueError, IndexError):
        return 0


def send(remote: Remote, source: Path, destination: str, total: int,
         block: int = BLOCK) -> None:
    """Append block by block, continuing from whatever already arrived."""
    have = remote_size(remote, destination)
    if have > total:
        print('  the device has more bytes than the source; removing and starting over')
        remote.run(f'rm -f {shlex.quote(destination)}')
        have = 0
    if have:
        print(f'  resuming at {have / 1e6:,.0f} MB of {total / 1e6:,.0f} MB')

    started, sent_now = time.monotonic(), 0
    with source.open('rb') as handle:
        handle.seek(have)
        while have < total:
            chunk = handle.read(block)
            if not chunk:
                break
            # Re-check before every append: if a previous append half-arrived,
            # blindly appending again would corrupt the file in place.
            actual = remote_size(remote, destination)
            if actual != have:
                print(f'  device holds {actual:,} bytes, expected {have:,}; resynchronising')
                have = actual
                if have > total:
                    remote.run(f'rm -f {shlex.quote(destination)}')
                    have = 0
                handle.seek(have)
                continue
            remote.run(f'cat >> {shlex.quote(destination)}', data=chunk,
                       timeout=max(300, len(chunk) // 20000))
            have += len(chunk)
            sent_now += len(chunk)
            elapsed = time.monotonic() - started
            rate = sent_now / max(elapsed, 1e-6)
            remaining = (total - have) / max(rate, 1.0)
            print(f'  {have / 1e6:,.0f} / {total / 1e6:,.0f} MB  '
                  f'{rate / 1e6:,.2f} MB/s  about {human_time(remaining)} left', flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--host', default='192.168.1.22')
    parser.add_argument('--user', default='pi')
    parser.add_argument('--remote-dir', default=None,
                        help='staging directory on the device (default: $HOME/mixos-models)')
    parser.add_argument('--verify-only', action='store_true',
                        help='check what is already on the device; transfer nothing')
    parser.add_argument('--no-import', action='store_true',
                        help='transfer and verify, but do not run litert-lm import')
    parser.add_argument('--block-mb', type=int, default=BLOCK >> 20,
                        help='MiB per append (default: %(default)s). Every block is '
                             'one SSH connection, so use a larger block on the USB '
                             'link, where a handshake costs more than the data does')
    parser.add_argument('--venv', default='/home/pi/mixos-ai/venv',
                        help='where litert-lm is installed on the device '
                             '(default: %(default)s); it is not on PATH')
    args = parser.parse_args()

    if not 1 <= args.block_mb <= 256:
        raise SystemExit('--block-mb must be between 1 and 256.')

    if not MANIFEST.is_file():
        raise SystemExit('No staged models. Run tools/stage_models.py first.')
    manifest = json.loads(MANIFEST.read_text(encoding='utf-8'))
    artifacts = manifest.get('artifacts') or []
    if not artifacts:
        raise SystemExit('The staging manifest lists no artifacts.')

    print('Checking the staged files against the manifest')
    for entry in artifacts:
        local = MANIFEST.parent / entry['name']
        if not local.is_file():
            raise SystemExit(f'Staged file missing: {local}')
        if local.stat().st_size != entry['bytes']:
            raise SystemExit(f'{entry["name"]}: {local.stat().st_size:,} bytes on disk, '
                             f'{entry["bytes"]:,} in the manifest. Re-stage it.')
        if digest(local) != entry['sha256']:
            raise SystemExit(f'{entry["name"]}: content does not match the manifest. Re-stage it.')
        print(f'  {entry["name"]}  {entry["bytes"]:,} bytes  ok')

    password = os.environ.get('MIXOS_SSH_PASSWORD') or getpass.getpass(
        f'{args.user}@{args.host} password: ')
    if not password:
        raise SystemExit('No password supplied; set MIXOS_SSH_PASSWORD or type one.')

    with tempfile.TemporaryDirectory(prefix='mixos-models-') as temporary:
        remote = Remote(args.host, args.user, password, Path(temporary))
        home = remote.text('printf "%s" "$HOME"')
        directory = args.remote_dir or (home + '/mixos-models')
        remote.run(f'mkdir -p {shlex.quote(directory)}')

        free = int(remote.text(f'df -Pk {shlex.quote(directory)} | awk "NR==2{{print \\$4}}"') or 0) * 1024
        needed = sum(e['bytes'] for e in artifacts) * 2   # the copy, plus the import
        print(f'\nDevice free space: {free / 1e9:,.1f} GB, '
              f'needed including the import: {needed / 1e9:,.1f} GB')
        if free < needed:
            raise SystemExit('Not enough free space on the device for the transfer and the import.')

        for entry in artifacts:
            name = entry['name']
            local = MANIFEST.parent / name
            destination = f'{directory}/{name}'
            print(f'\n{name}')
            if not args.verify_only:
                send(remote, local, destination, entry['bytes'],
                     block=args.block_mb << 20)

            size = remote_size(remote, destination)
            if size != entry['bytes']:
                raise SystemExit(f'{name}: device holds {size:,} bytes, expected '
                                 f'{entry["bytes"]:,}. Run again to resume.')
            print('  verifying on the device (this reads the whole file there)', flush=True)
            actual = remote.text(f'sha256sum {shlex.quote(destination)} | cut -d" " -f1',
                                 timeout=1800)
            if actual != entry['sha256']:
                raise SystemExit(f'{name}: the device computed {actual}, the manifest says '
                                 f'{entry["sha256"]}. The copy is corrupt; delete '
                                 f'{destination} and run again.')
            print('  verified on the device')

            if args.no_import or args.verify_only:
                continue
            model_id = entry['model_id']
            print(f'  importing as {model_id}', flush=True)
            venv = shlex.quote(args.venv + '/bin/litert-lm')
            script = (
                'set -eu\n'
                # litert-lm lives in the service's virtual environment, which is
                # not on anybody's PATH. Looking only at PATH reports it as not
                # installed on a machine where it is installed and working.
                f'if [ -x {venv} ]; then LITERT={venv}\n'
                f'elif command -v litert-lm >/dev/null; then LITERT=litert-lm\n'
                f'else\n'
                f'  echo "litert-lm is in neither {args.venv}/bin nor PATH; '
                f'run tools/deploy_venv.py first" >&2; exit 1\n'
                f'fi\n'
                f'if "$LITERT" list 2>/dev/null | awk -v m={shlex.quote(model_id)} '
                f'"\\$1 == m {{ found = 1 }} END {{ exit !found }}"; then\n'
                f'  echo "already imported"; exit 0\n'
                f'fi\n'
                f'"$LITERT" import {shlex.quote(destination)} {shlex.quote(model_id)}\n'
                f'"$LITERT" list\n')
            print(remote.text(script, timeout=3600))

    print('\nDone.')
    return 0


if __name__ == '__main__':
    os.environ.setdefault('PYTHONIOENCODING', 'utf-8')
    sys.exit(main())
