#!/usr/bin/env python3
"""Send term-ime's source to the device, build it there, and install it.

This is what actually gives the notes editor Chinese input. The editor has
always been written for it — every command in it is a control key so that
printable keys stay printable and the input method has something to convert —
but ``term-ime`` itself was never on the device, so the launcher kept taking its
documented fallback and running the editor directly.

It cannot be installed from a package or a release. term-ime publishes prebuilt
binaries for ``linux-x86_64`` and this is an aarch64 Compute Module, so the
binary is compiled on the device from the tree ``tools/stage_ime.py`` collects.

What this tool does, in order:

1. checks the staged archive against ``build/ime/manifest.json`` before using
   the link at all, because a slow transfer of the wrong bytes is worse than no
   transfer;
2. reports the device's side of it — architecture, free memory and disk, whether
   ``cmake`` is there, whether term-ime already is;
3. installs ``cmake`` if it is missing. This is the one thing that needs the
   Debian archive, which the device can reach (``deb.debian.org`` answered in
   0.5 s on 2026-09-14) even though ``github.com`` it cannot;
4. uploads the archive in resumable blocks and verifies its digest on the device;
5. starts ``tools/build_ime_on_pi.py`` as a transient systemd unit, so the build
   survives the SSH connection dropping, and prints the job name to ask about.

Only ``--execute`` changes anything. Without it this reports and exits.

    $env:MIXOS_SSH_PASSWORD='...'
    py -3.12 tools/build_ime_remote.py --host 192.168.1.22
    py -3.12 tools/build_ime_remote.py --host 192.168.1.22 --execute
    py -3.12 tools/build_ime_remote.py --host 192.168.1.22 --status mixos-ime-20260914-131500
    py -3.12 tools/build_ime_remote.py --host 192.168.1.22 --verify-only

``JOB SUBMITTED`` means the unit started, not that the build succeeded. The build
takes tens of minutes; ask with ``--status`` and look for
``build_and_verify_complete`` in the audit. ``--verify-only`` re-runs just the
end-to-end check against whatever is installed now, which is the useful thing to
run after the device has been rebooted or the apps redeployed.
"""
from __future__ import annotations

import argparse
import getpass
import json
import os
import re
import shlex
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from deploy_models import Remote, digest, send                # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / 'build/ime/manifest.json'
WORKER = Path(__file__).resolve().parent / 'build_ime_on_pi.py'
REMOTE_ROOT = '/home/pi/mixos-ime'
BINARY = '/usr/local/bin/term-ime'
JOB_PATTERN = re.compile(r'mixos-ime-\d{8}-\d{6}')


def report(remote: Remote) -> dict:
    """What the device looks like now, in the order the build would trip over it.

    Every probe puts its own fallback inside a command substitution rather than
    after a ``|``. ``cmd | head -1 || echo missing`` looks like it reports
    "missing" and does not: the exit status of a pipeline is the status of
    ``head``, which succeeds on empty input, so a missing tool comes back as an
    empty string. That matters here beyond tidiness — ``--execute`` decides
    whether to install cmake from what this returns.
    """
    checks = {
        'architecture': 'uname -m',
        'os': '. /etc/os-release && printf "%s" "$PRETTY_NAME"',
        'memory available': 'awk "/MemAvailable/ {printf \\"%.1f GiB\\", \\$2/1048576}" '
                            '/proc/meminfo',
        'free disk': f'd={shlex.quote(REMOTE_ROOT)}; [ -d "$d" ] || d=/home/pi; '
                     'printf "%s at %s" "$(df -h --output=avail "$d" | tail -1 | tr -d " ")" "$d"',
        'cores': 'nproc',
        'cmake': 'v=$(cmake --version 2>/dev/null | head -1); '
                 'printf "%s" "${v:-not installed}"',
        'compiler': 'g++ --version | head -1',
        'debian archive': 'curl -sS -o /dev/null -m 20 -w "%{http_code} in %{time_total}s" '
                          'https://deb.debian.org/debian/ 2>/dev/null || printf unreachable',
        'term-ime binary': f'if [ -x {BINARY} ]; then stat -c "%s bytes, %y" {BINARY}; '
                           f'else printf "not installed"; fi',
        'term-ime linkage': f'if [ -x {BINARY} ]; then ldd {BINARY} 2>&1 | head -1; '
                            f'else printf -; fi',
        'rime shared data': 'v=$(ls -1 /usr/local/share/term-ime/rime-data/*.schema.yaml '
                            '2>/dev/null | xargs -r -n1 basename | tr "\\n" " "); '
                            'printf "%s" "${v:-not installed}"',
        'rime compiled': 'v=$(ls -1 /home/pi/.local/share/term-ime/build/*.bin 2>/dev/null '
                         '| xargs -r -n1 basename | tr "\\n" " "); printf "%s" "${v:-none yet}"',
        'notes launcher': 'f=/usr/local/lib/mixos/apps/notes; '
                          'if [ -f "$f" ]; then printf "%s term-ime mentions" '
                          '"$(grep -c term-ime "$f")"; else printf missing; fi',
        'AI services': 'systemctl is-active mixos-litertlm.service mixos-aiserver.service '
                       '2>&1 | tr "\\n" " "',
        'uploaded archive': f'v=$(stat -c "%s bytes" {REMOTE_ROOT}/term-ime-src.tar.gz '
                            f'2>/dev/null); printf "%s" "${{v:-not uploaded}}"',
        'last audit event': f'v=$(tail -1 {REMOTE_ROOT}/build-audit.jsonl 2>/dev/null); '
                            f'printf "%s" "${{v:-no audit yet}}"',
    }
    answers = {}
    print('\nOn the device now:')
    for label, command in checks.items():
        answer = remote.text(command, check=False).strip() or '(probe returned nothing)'
        answers[label] = answer
        print(f'  {label:20} {answer[:110]}')
    return answers


def install_cmake_script() -> str:
    """The one package this needs, installed only when it is missing."""
    return (
        'set -eu\n'
        'if command -v cmake >/dev/null; then\n'
        '  echo "cmake already installed: $(cmake --version | head -1)"\n'
        '  exit 0\n'
        'fi\n'
        'export DEBIAN_FRONTEND=noninteractive\n'
        'apt-get update\n'
        # No recommends: this machine has 12 GB free and no reason to pull in
        # cmake's documentation and Qt-based GUI to compile one binary.
        'apt-get install -y --no-install-recommends cmake\n'
        'cmake --version | head -1\n')


def detached_command(job: str, root: str, sha: str, jobs: int, with_tests: bool) -> str:
    """Start the worker as a transient unit that outlives this SSH session.

    Root, deliberately: the worker stops the two AI units to free the memory the
    compile needs, installs into ``/usr/local`` and starts them again. Every
    compiler process it runs is dropped to the ordinary user with ``setpriv``.
    """
    worker = ['python3', '-u', f'{root}/build_ime_on_pi.py', 'build',
              '--root', root, '--sha256', sha, '--jobs', str(jobs)]
    if with_tests:
        worker.append('--with-tests')
    return shlex.join([
        'sudo', '-S', '-p', '', 'systemd-run', '--unit', job,
        '--property=Type=exec', '--property=Restart=no',
        # Four hours: the measured build is far shorter, but a unit killed
        # halfway leaves a half-installed prefix, which is worse than waiting.
        '--property=RuntimeMaxSec=14400', '--property=UMask=0022',
        f'--property=WorkingDirectory={root}',
        f'--property=StandardOutput=append:{root}/{job}.log',
        '--property=StandardError=inherit', '--'] + worker)


def status_script(job: str, root: str) -> str:
    if not JOB_PATTERN.fullmatch(job):
        raise ValueError('Use the exact mixos-ime-YYYYMMDD-HHMMSS job name')
    return (f'systemctl show {shlex.quote(job + ".service")} '
            '-p LoadState -p ActiveState -p SubState -p Result -p ExecMainStatus; '
            f'printf "\\n=== build-audit.jsonl ===\\n"; '
            f'tail -n 25 {root}/build-audit.jsonl 2>/dev/null; '
            f'printf "\\n=== {job}.log (tail) ===\\n"; '
            f'tail -n 40 {root}/{job}.log 2>/dev/null')


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--host', default='192.168.1.22')
    parser.add_argument('--user', default='pi')
    parser.add_argument('--execute', action='store_true',
                        help='upload, install cmake if missing, and start the build')
    parser.add_argument('--status', metavar='JOB',
                        help='ask about one already-submitted build and exit')
    parser.add_argument('--verify-only', action='store_true',
                        help='run the end-to-end input check against what is '
                             'installed now; build nothing')
    parser.add_argument('--verify-notes', action='store_true',
                        help='drive the notes button itself: type pinyin into the '
                             'editor and read the saved note back off the disk')
    parser.add_argument('--jobs', type=int, default=2,
                        help='compiler processes on the device (default: %(default)s, '
                             'chosen for 4 GiB of RAM)')
    parser.add_argument('--with-tests', action='store_true',
                        help="also build and run term-ime's own test binaries")
    parser.add_argument('--block-mb', type=int, default=4,
                        help='MiB per upload block (default: %(default)s)')
    parser.add_argument('--remote-root', default=REMOTE_ROOT)
    args = parser.parse_args()

    password = os.environ.get('MIXOS_SSH_PASSWORD') or getpass.getpass(
        f'{args.user}@{args.host} password: ')
    if not password:
        raise SystemExit('No password supplied; set MIXOS_SSH_PASSWORD or type one.')

    with tempfile.TemporaryDirectory(prefix='mixos-ime-') as temporary:
        remote = Remote(args.host, args.user, password, Path(temporary))
        root = args.remote_root

        if args.status:
            print(remote.text(status_script(args.status, root), timeout=120,
                              check=False))
            print('\nA unit that exited 0 is not the claim; '
                  '"build_and_verify_complete" in the audit is.')
            return 0

        if args.verify_only or args.verify_notes:
            command = 'verify-notes' if args.verify_notes else 'verify'
            remote.run(f'mkdir -p {shlex.quote(root)}')
            remote.run(f'cat > {shlex.quote(root)}/build_ime_on_pi.py',
                       data=WORKER.read_bytes().replace(b'\r\n', b'\n'))
            # Both streams: the worker reports what it concluded on stdout and
            # why it stopped on stderr, and showing only one of them is how a
            # check appears to have produced nothing at all.
            result = remote.run(
                f'cd {shlex.quote(root)} && python3 -u build_ime_on_pi.py {command} '
                f'--root {shlex.quote(root)} 2>&1', timeout=1800, check=False)
            print(result.stdout.decode('utf-8', 'replace').strip() or
                  '(the worker printed nothing)')
            errors = result.stderr.decode('utf-8', 'replace').strip()
            if errors:
                print('--- stderr ---\n' + errors)
            return 1 if result.returncode else 0

        if not MANIFEST.is_file():
            raise SystemExit('Nothing staged. Run tools/stage_ime.py first.')
        manifest = json.loads(MANIFEST.read_text(encoding='utf-8'))
        archive = MANIFEST.parent / manifest['archive']['name']
        if not archive.is_file():
            raise SystemExit(f'Staged archive missing: {archive}')
        print(f'Staged: term-ime {manifest["release"]}, '
              f'{manifest["archive"]["files"]:,} files, '
              f'{archive.stat().st_size / 1e6:,.1f} MB')
        actual = digest(archive)
        if actual != manifest['archive']['sha256']:
            raise SystemExit('The staged archive does not match its manifest. '
                             'Re-run tools/stage_ime.py.')
        print(f'  sha256 {actual} matches the manifest')

        answers = report(remote)
        if not args.execute:
            print('\nReport only. Add --execute to upload and build.')
            return 0

        if 'aarch64' not in answers.get('architecture', ''):
            raise SystemExit(f'The device reports {answers.get("architecture")!r}; '
                             f'this build is for aarch64.')

        remote.run(f'mkdir -p {shlex.quote(root)}')

        # Affirmative rather than negative: install unless the probe came back
        # with a version. A probe that fails for any other reason — an unexpected
        # message, an empty answer, a changed locale — then leads to running the
        # install script, which checks for cmake itself and exits without doing
        # anything when it is already there. Deciding from the absence of one
        # particular phrase would skip the install on any surprise.
        if 'cmake version' not in answers.get('cmake', ''):
            print('\nInstalling cmake (the only package this needs)')
            result = remote.run('sudo -S -p "" bash -s',
                                data=(password + '\n' + install_cmake_script()).encode(),
                                timeout=1800, check=False)
            output = (result.stdout + result.stderr).decode('utf-8', 'replace')
            if result.returncode:
                raise SystemExit('Installing cmake failed:\n' + output[-2000:])
            print(output.strip()[-600:])
        else:
            print(f'\ncmake is already installed: {answers["cmake"]}')

        print('\nUploading the source archive')
        destination = f'{root}/{archive.name}'
        # Re-staging changes the bytes without necessarily changing the length,
        # and `send` resumes from the length it finds. A stale archive of the
        # same size would be treated as already delivered, so a copy that is
        # there and does not match is removed before anything is sent.
        existing = remote.text(f'if [ -f {shlex.quote(destination)} ]; then '
                               f'sha256sum {shlex.quote(destination)} | cut -d" " -f1; '
                               f'else printf none; fi', timeout=1800,
                               check=False).strip()
        if existing == actual:
            print('  the device already holds this exact archive')
        elif existing not in ('none', ''):
            print(f'  a different archive is already there ({existing[:16]}…); '
                  f'removing it')
            remote.run(f'rm -f {shlex.quote(destination)}')
        send(remote, archive, destination, archive.stat().st_size,
             block=args.block_mb << 20)
        on_device = remote.text(f'sha256sum {shlex.quote(destination)} | cut -d" " -f1',
                                timeout=1800)
        if on_device != actual:
            raise SystemExit(f'The device computed {on_device}, expected {actual}. '
                             f'Delete {destination} and run again to resend.')
        print('  verified on the device')

        remote.run(f'cat > {shlex.quote(root)}/build_ime_on_pi.py',
                   data=WORKER.read_bytes().replace(b'\r\n', b'\n'))

        job = 'mixos-ime-' + time.strftime('%Y%m%d-%H%M%S')
        print(f'\nStarting the build as {job}')
        result = remote.run(detached_command(job, root, actual, args.jobs,
                                            args.with_tests),
                            data=(password + '\n').encode(), timeout=300, check=False)
        output = (result.stdout + result.stderr).decode('utf-8', 'replace').strip()
        if output:
            print(output)
        if result.returncode:
            raise SystemExit(f'Could not start the build unit (exit {result.returncode}).')
        print(f'\nJOB SUBMITTED: {job}')
        print('This says the unit started, not that the build worked. Ask with:')
        print(f'  py -3.12 tools/build_ime_remote.py --host {args.host} '
              f'--status {job}')
        print('Look for "build_and_verify_complete" in the audit before believing it.')
    return 0


if __name__ == '__main__':
    os.environ.setdefault('PYTHONIOENCODING', 'utf-8')
    sys.exit(main())
