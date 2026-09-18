#!/usr/bin/env python3
"""Collect the speech stack's aarch64 wheels here, where the network works.

The device cannot install its own dependencies. Measured on 2026-09-13,
`pypi.org` from the CM5 ran at about 125 kB/s and timed out mid-transfer; the
speech package alone is 92 MB. So the wheels are downloaded on this machine and
travel with the model over the USB maintenance link.

Two details decide whether this works at all.

**The platform tag.** `moonshine-voice` publishes
`manylinux_2_34_aarch64`, not `manylinux2014_aarch64`. Asking pip for the older
tag reports "no matching distribution" for a package that plainly exists, which
reads like the package is unavailable rather than like the question was wrong.
Debian 13 carries glibc 2.41, so the newer tags are the correct ones to ask for
and the older ones are accepted as a fallback.

**The interpreter version.** The device runs Python 3.13. Wheels are resolved
for that, not for whatever is installed here.

    py -3.12 tools/stage_wheels.py
    py -3.12 tools/stage_wheels.py --index https://pypi.tuna.tsinghua.edu.cn/simple

Anything that has no aarch64 wheel is named at the end rather than passed over,
because finding out on the device costs a round trip and a rebuild.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REQUIREMENTS = ROOT / 'linux/apps/translator/vendor/requirements.txt'
DEST = ROOT / 'build/wheels'
MANIFEST = DEST / 'manifest.json'

# What the device is. Not what this machine is.
PYTHON_VERSION = '3.13'
# Newest first: pip takes every --platform as acceptable, and a package that
# publishes several gets the one that matches the device's glibc 2.41.
#
# The list is long because publishers pick whatever their build container
# happened to have: moonshine-voice tags manylinux_2_34, litert-lm-api tags
# manylinux_2_27, and neither is manylinux2014. A tag missing from this list
# reads as "no aarch64 build exists", which for both of those would be wrong.
PLATFORMS = (
    'manylinux_2_39_aarch64',
    'manylinux_2_36_aarch64',
    'manylinux_2_35_aarch64',
    'manylinux_2_34_aarch64',
    'manylinux_2_31_aarch64',
    'manylinux_2_28_aarch64',
    'manylinux_2_27_aarch64',
    'manylinux_2_24_aarch64',
    'manylinux_2_17_aarch64',
    'manylinux2014_aarch64',
)
CHUNK = 1 << 20


def digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open('rb') as handle:
        for block in iter(lambda: handle.read(CHUNK), b''):
            h.update(block)
    return h.hexdigest()


def download(index: str | None, timeout: int) -> subprocess.CompletedProcess:
    command = [sys.executable, '-m', 'pip', 'download',
               '-r', str(REQUIREMENTS), '-d', str(DEST),
               '--only-binary=:all:', '--python-version', PYTHON_VERSION,
               '--timeout', str(timeout), '--retries', '5']
    for platform in PLATFORMS:
        command += ['--platform', platform]
    if index:
        command += ['--index-url', index]
    print('  ' + ' '.join(command[1:]), flush=True)
    return subprocess.run(command, text=True, capture_output=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--index', default=None,
                        help='package index to use (default: pip\'s own). A mirror '
                             'is faster but may not carry a very recent release.')
    parser.add_argument('--timeout', type=int, default=60)
    parser.add_argument('--verify', action='store_true',
                        help='hash what is already here; download nothing')
    args = parser.parse_args()

    if not REQUIREMENTS.is_file():
        raise SystemExit(f'Missing requirements file: {REQUIREMENTS}')
    DEST.mkdir(parents=True, exist_ok=True)

    if not args.verify:
        print(f'Resolving {REQUIREMENTS.relative_to(ROOT)} for aarch64, '
              f'Python {PYTHON_VERSION}')
        result = download(args.index, args.timeout)
        if result.returncode:
            tail = (result.stderr or result.stdout or '').strip().splitlines()
            print('\n'.join(tail[-25:]))
            raise SystemExit(
                '\npip could not resolve every requirement for aarch64. The line '
                'above names the package; check its published wheel tags before '
                'assuming it has no aarch64 build.')
        print(result.stdout.strip().splitlines()[-1] if result.stdout.strip() else '')

    wheels = sorted(DEST.glob('*.whl'))
    if not wheels:
        raise SystemExit('Nothing was downloaded.')

    entries, foreign = [], []
    for wheel in wheels:
        # A wheel tagged for this machine would install here and fail there.
        if 'x86_64' in wheel.name or 'win_amd64' in wheel.name or 'macosx' in wheel.name:
            foreign.append(wheel.name)
        entries.append({'name': wheel.name, 'bytes': wheel.stat().st_size,
                        'sha256': digest(wheel)})

    MANIFEST.write_text(json.dumps({
        'staged': time.strftime('%Y-%m-%d %H:%M:%S'),
        'python_version': PYTHON_VERSION,
        'platforms': list(PLATFORMS),
        'wheels': entries,
    }, indent=2) + '\n', encoding='utf-8')

    total = sum(entry['bytes'] for entry in entries)
    print(f'\n{len(entries)} wheels, {total / 1e6:,.0f} MB')
    print(f'Manifest: {MANIFEST.relative_to(ROOT)}')
    if foreign:
        print('\nThese are not for the device and must not be shipped:')
        for name in foreign:
            print('  ' + name)
        return 1
    print('Deploy with: py -3.12 tools/deploy_venv.py --host 10.12.194.1')
    return 0


if __name__ == '__main__':
    os.environ.setdefault('PYTHONIOENCODING', 'utf-8')
    sys.exit(main())
