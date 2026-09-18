#!/usr/bin/env python3
"""Put the staged wheels on the device and build the service's environment.

Nothing here reaches the network from the device. `pip` is run with
`--no-index`, so every package comes from the directory this tool filled and
a missing one is an error here rather than a twenty-minute stall there.

The wheels are sent one at a time with the same resumable append the model
uses, and each is hashed on the device before it is installed. 133 MB over
Wi-Fi is about ten minutes and over the USB maintenance link about a quarter of
that; either way, an interrupted run continues rather than restarting.

    $env:MIXOS_SSH_PASSWORD='...'
    py -3.12 tools/deploy_venv.py --host 10.12.194.1 --block-mb 64
    py -3.12 tools/deploy_venv.py --host 192.168.1.22 --check-only

The environment is built at /home/pi/mixos-ai/venv, which is where both unit
files expect it. Building it is the last thing this does, so a failed transfer
never leaves a half-populated environment that systemd would then try to start.
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

from deploy_models import Remote, human_time, remote_size, send  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
WHEELS = ROOT / 'build/wheels'
MANIFEST = WHEELS / 'manifest.json'
REQUIREMENTS = ROOT / 'linux/apps/translator/vendor/requirements.txt'
VENV = '/home/pi/mixos-ai/venv'
REMOTE_DIR = '/home/pi/mixos-ai'


def check(remote: Remote) -> None:
    """What the environment can actually do, asked of it rather than assumed."""
    script = f'''
set -u
if [ ! -x {VENV}/bin/python ]; then echo "venv: absent"; exit 0; fi
echo "venv: $({VENV}/bin/python --version 2>&1)"
echo "packages: $({VENV}/bin/python -m pip list --disable-pip-version-check \
    --no-index 2>/dev/null | tail -n +3 | wc -l)"
for module in numpy moonshine_voice; do
  if {VENV}/bin/python -c "import $module" 2>/tmp/mixos-import-error; then
    echo "$module: importable"
  else
    echo "$module: $(head -c 200 /tmp/mixos-import-error | tr "\\n" " ")"
  fi
done
echo "litert-lm: $(test -x {VENV}/bin/litert-lm && echo present || echo absent)"
echo "models: $(ls {REMOTE_DIR}/*.litertlm 2>/dev/null | wc -l)"
'''
    print(remote.text(script, timeout=180, check=False))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--host', default='192.168.1.22')
    parser.add_argument('--user', default='pi')
    parser.add_argument('--block-mb', type=int, default=4,
                        help='MiB per append (default: %(default)s); use 64 on the '
                             'USB link, where the SSH handshake costs more than '
                             'the data does')
    parser.add_argument('--check-only', action='store_true',
                        help='report what the environment can do; change nothing')
    parser.add_argument('--no-build', action='store_true',
                        help='send and verify the wheels, but do not build the venv')
    args = parser.parse_args()

    if not 1 <= args.block_mb <= 256:
        raise SystemExit('--block-mb must be between 1 and 256.')

    password = os.environ.get('MIXOS_SSH_PASSWORD') or getpass.getpass(
        f'{args.user}@{args.host} password: ')
    if not password:
        raise SystemExit('No password supplied; set MIXOS_SSH_PASSWORD or type one.')

    with tempfile.TemporaryDirectory(prefix='mixos-venv-') as temporary:
        remote = Remote(args.host, args.user, password, Path(temporary))
        if args.check_only:
            check(remote)
            return 0

        if not MANIFEST.is_file():
            raise SystemExit('No staged wheels. Run tools/stage_wheels.py first.')
        manifest = json.loads(MANIFEST.read_text(encoding='utf-8'))
        wheels = manifest.get('wheels') or []
        if not wheels:
            raise SystemExit('The wheel manifest lists nothing.')

        print('Checking the staged wheels against the manifest')
        for entry in wheels:
            local = WHEELS / entry['name']
            if not local.is_file() or local.stat().st_size != entry['bytes']:
                raise SystemExit(f'{entry["name"]} is missing or the wrong size; re-stage.')
        total = sum(entry['bytes'] for entry in wheels)
        print(f'  {len(wheels)} wheels, {total / 1e6:,.0f} MB')

        directory = f'{REMOTE_DIR}/wheels'
        remote.run(f'mkdir -p {shlex.quote(directory)}')
        remote.run(f'cat > {shlex.quote(REMOTE_DIR)}/requirements.txt',
                   data=REQUIREMENTS.read_bytes())

        started, sent = time.monotonic(), 0
        for index, entry in enumerate(wheels, 1):
            name, local = entry['name'], WHEELS / entry['name']
            destination = f'{directory}/{name}'
            if remote_size(remote, destination) == entry['bytes']:
                continue                       # already there and the right length
            print(f'  [{index}/{len(wheels)}] {name}  {entry["bytes"] / 1e6:,.1f} MB',
                  flush=True)
            send(remote, local, destination, entry['bytes'], block=args.block_mb << 20)
            sent += entry['bytes']
        if sent:
            rate = sent / max(time.monotonic() - started, 1e-6)
            print(f'  sent {sent / 1e6:,.0f} MB at {rate / 1e6:,.2f} MB/s')

        print('Verifying every wheel on the device')
        listing = remote.text(f'cd {shlex.quote(directory)} && sha256sum *.whl',
                              timeout=600)
        actual = {}
        for line in listing.splitlines():
            checksum, _, name = line.partition(' ')
            actual[name.strip().lstrip('*')] = checksum.strip()
        for entry in wheels:
            if actual.get(entry['name']) != entry['sha256']:
                raise SystemExit(
                    f'{entry["name"]}: the device computed '
                    f'{actual.get(entry["name"], "nothing")}, the manifest says '
                    f'{entry["sha256"]}. Delete it there and run again.')
        print(f'  {len(wheels)} verified')

        if args.no_build:
            return 0

        print('Building the environment from those wheels alone', flush=True)
        build = f'''
set -eu
python3 -m venv --upgrade-deps {VENV} 2>/dev/null || python3 -m venv {VENV}
{VENV}/bin/python -m pip install --no-index --disable-pip-version-check \
    --find-links {shlex.quote(directory)} \
    -r {shlex.quote(REMOTE_DIR)}/requirements.txt
'''
        result = remote.run(build, timeout=1800, check=False)
        output = result.stdout.decode('utf-8', 'replace').strip().splitlines()
        print('\n'.join(output[-12:]) if output else '')
        if result.returncode:
            raise SystemExit('pip failed:\n' +
                             result.stderr.decode('utf-8', 'replace')[-1500:])

        print(f'\nBuilt in {human_time(time.monotonic() - started)}.')
        check(remote)

    print('Enable the services with: '
          'py -3.12 tools/deploy_apps.py --host <host> --enable')
    return 0


if __name__ == '__main__':
    os.environ.setdefault('PYTHONIOENCODING', 'utf-8')
    sys.exit(main())
