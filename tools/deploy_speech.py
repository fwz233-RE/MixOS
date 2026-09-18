#!/usr/bin/env python3
"""Put the staged speech-recognition models into the deck's model cache.

``tools/deploy_models.py`` moves one 2.6 GB language model and imports it.
This moves a tree of small ones into the directory ``moonshine_voice`` looks in
before it decides to download anything, which is the only way the translator
backend ever gets speech recognition: ``mixos-aiserver.service`` runs with
``IPAddressDeny=any`` and ``IPAddressAllow=localhost``, so the download it would
otherwise attempt inside a request handler cannot succeed. Staged models are
not an optimisation here; they are the mechanism.

The destination is not guessed. The device's own ``moonshine_voice`` is asked
where its cache is (``download_file.get_cache_dir()``, which honours
``MOONSHINE_VOICE_CACHE`` and otherwise resolves to
``/home/pi/.cache/moonshine_voice``), and the answer is checked against the
paths the service is allowed to write - ``ReadWritePaths=/home/pi/mixos-ai
/home/pi/.cache``. A cache the service cannot read is a transfer that appears
to succeed and changes nothing.

Transfers resume, and each file's SHA-256 is checked on the device before the
next one starts, for the reason the model deployer gives: a corrupt model
loads and produces nonsense rather than failing.

    $env:MIXOS_SSH_PASSWORD='...'
    py -3.12 tools/deploy_speech.py --host 10.12.194.1 --block-mb 64
    py -3.12 tools/deploy_speech.py --host 10.12.194.1 --verify-only
"""
from __future__ import annotations

import argparse
import getpass
import json
import os
import shlex
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from deploy_models import Remote, digest, human_time, send  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
STAGE = ROOT / 'build/speech'
MANIFEST = STAGE / 'manifest.json'

# Where the service is permitted to read, per linux/mixos-aiserver.service.
ALLOWED_CACHE_PREFIXES = ('/home/pi/.cache', '/home/pi/mixos-ai')

CACHE_QUERY = '''
set -eu
V={venv}
if [ ! -x "$V/bin/python" ]; then
  echo "NOVENV"
  exit 0
fi
"$V/bin/python" - <<'PY'
try:
    from moonshine_voice.download_file import get_cache_dir
    print("CACHE", get_cache_dir())
except Exception as exc:
    print("NOPKG", type(exc).__name__, exc)
PY
'''


def resolve_cache_dir(remote: Remote, venv: str, override: str | None) -> str:
    """Ask the device where its model cache is, rather than assuming."""
    if override:
        return override.rstrip('/')
    answer = remote.text(CACHE_QUERY.format(venv=shlex.quote(venv)),
                         timeout=180, check=False)
    for line in answer.splitlines():
        if line.startswith('CACHE '):
            return line.split(' ', 1)[1].strip().rstrip('/')
    raise SystemExit(
        'Could not ask the device where moonshine_voice keeps its cache.\n'
        f'The device said: {answer or "(nothing)"}\n'
        'Install the virtual environment first with tools/deploy_venv.py, or '
        'pass --cache-dir to place the files without asking.')


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--host', default='10.12.194.1',
                        help='default is the USB maintenance address; the Wi-Fi '
                             'address is 192.168.1.22 and is roughly ten times slower')
    parser.add_argument('--user', default='pi')
    parser.add_argument('--venv', default='/home/pi/mixos-ai/venv')
    parser.add_argument('--cache-dir', default=None,
                        help='skip asking the device and write here instead')
    parser.add_argument('--block-mb', type=int, default=16,
                        help='MiB per append (default: %(default)s); every block is '
                             'one SSH connection, so use 64 on the USB link')
    parser.add_argument('--verify-only', action='store_true',
                        help='hash what is already on the device; transfer nothing')
    args = parser.parse_args()

    if not 1 <= args.block_mb <= 256:
        raise SystemExit('--block-mb must be between 1 and 256.')
    if not MANIFEST.is_file():
        raise SystemExit('No staged speech models. Run tools/stage_speech.py first.')

    manifest = json.loads(MANIFEST.read_text(encoding='utf-8'))
    files = manifest.get('files') or []
    if not files:
        raise SystemExit('The staging manifest lists no files.')

    print('Checking the staged files against the manifest')
    for entry in files:
        local = STAGE / entry['path']
        if not local.is_file():
            raise SystemExit(f'Staged file missing: {local}')
        if local.stat().st_size != entry['bytes']:
            raise SystemExit(f'{entry["path"]}: {local.stat().st_size:,} bytes on disk, '
                             f'{entry["bytes"]:,} in the manifest. Re-stage it.')
        if digest(local) != entry['sha256']:
            raise SystemExit(f'{entry["path"]}: content does not match the manifest. '
                             'Re-stage it.')
    total = sum(e['bytes'] for e in files)
    print(f'  {len(files)} file(s), {total / 1e6:,.1f} MB, all matching')

    password = os.environ.get('MIXOS_SSH_PASSWORD') or getpass.getpass(
        f'{args.user}@{args.host} password: ')
    if not password:
        raise SystemExit('No password supplied; set MIXOS_SSH_PASSWORD or type one.')

    with tempfile.TemporaryDirectory(prefix='mixos-speech-') as temporary:
        remote = Remote(args.host, args.user, password, Path(temporary))
        cache = resolve_cache_dir(remote, args.venv, args.cache_dir)
        print(f'\nDevice model cache: {cache}')
        if not cache.startswith(ALLOWED_CACHE_PREFIXES):
            raise SystemExit(
                f'{cache} is outside {ALLOWED_CACHE_PREFIXES}, which is all '
                'mixos-aiserver.service may read. Files placed there would be '
                'invisible to the service. Fix MOONSHINE_VOICE_CACHE on the '
                'device or pass --cache-dir deliberately.')

        # df needs a path that exists, and the cache root may not yet.
        remote.run(f'mkdir -p {shlex.quote(cache)}')
        free = int(remote.text(
            f'df -Pk {shlex.quote(cache)} | awk "NR==2{{print \\$4}}"') or 0) * 1024
        print(f'Device free space: {free / 1e9:,.1f} GB, needed: {total / 1e9:,.2f} GB')
        if free < total * 1.1:
            raise SystemExit('Not enough free space on the device.')

        started, moved = time.monotonic(), 0
        for index, entry in enumerate(files, 1):
            local = STAGE / entry['path']
            destination = f"{cache}/{entry['path']}"
            print(f"\n[{index}/{len(files)}] {entry['path'].split('/', 2)[-1]}  "
                  f"{entry['bytes'] / 1e6:,.1f} MB", flush=True)
            remote.run(f'mkdir -p {shlex.quote(str(Path(destination).parent.as_posix()))}')
            if not args.verify_only:
                send(remote, local, destination, entry['bytes'],
                     block=args.block_mb << 20)
                moved += entry['bytes']
            actual = remote.text(
                f'sha256sum {shlex.quote(destination)} 2>/dev/null | cut -d" " -f1',
                timeout=600)
            if actual != entry['sha256']:
                raise SystemExit(
                    f'{entry["path"]}: the device computed {actual or "nothing"}, the '
                    f'manifest says {entry["sha256"]}. Delete {destination} and run again.')
            print('  verified on the device')

        elapsed = time.monotonic() - started
        if moved:
            print(f'\nMoved {moved / 1e6:,.1f} MB in {human_time(elapsed)} '
                  f'({moved / elapsed / 1e6:,.2f} MB/s)')

        print('\nWhat the device now has, by language:')
        for language in sorted({e['language'] for e in files}):
            names = sorted({e['model_name'] for e in files if e['language'] == language})
            print(f'  {language}: {", ".join(names)}')
        print('\nRestart the backend so it picks these up:')
        print('  sudo systemctl restart mixos-aiserver.service')
    return 0


if __name__ == '__main__':
    os.environ.setdefault('PYTHONIOENCODING', 'utf-8')
    sys.exit(main())
