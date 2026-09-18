#!/usr/bin/env python3
"""Download the AI deck's models on this machine, where the network works.

The deck's Linux side cannot fetch its own models. Measured on 2026-09-13 from
`typixdeck`: ``huggingface.co`` resolves to an unrelated address and every
connection times out, and even a reachable mirror delivered 116 kB/s. A 2.6 GB
model is not arriving that way. This machine reached the same mirror at
2.8 MB/s, so the download happens here and the bytes travel to the device
afterwards with ``tools/deploy_models.py``.

    py -3.12 tools/stage_models.py                 # download what is declared
    py -3.12 tools/stage_models.py --verify        # re-check what is already here
    py -3.12 tools/stage_models.py --endpoint https://huggingface.co

Downloads resume: an interrupted 2.6 GB file continues from where it stopped
rather than starting again, because on a link that drops, "start again" means
"never finish". Every finished file is hashed and recorded in
``build/models/manifest.json``; nothing is deployed that is not in there.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEST = ROOT / 'build/models'
MANIFEST = DEST / 'manifest.json'

# huggingface.co is unreachable from both machines on this network; the mirror
# is. Override with --endpoint or HF_ENDPOINT when that stops being true.
DEFAULT_ENDPOINT = os.environ.get('HF_ENDPOINT', 'https://hf-mirror.com')

# What the deck needs, and why. Adding an entry is the only supported way to
# stage something new: nothing here is assembled from user input at runtime.
ARTIFACTS = [
    {
        'name': 'gemma-4-E2B-it.litertlm',
        'repo': 'litert-community/gemma-4-E2B-it-litert-lm',
        'path': 'gemma-4-E2B-it.litertlm',
        'model_id': 'gemma4-e2b',
        'purpose': 'The language model that does the translating. The plain '
                   '.litertlm file is the generic CPU build, which is the '
                   'correct one for a Compute Module; the _Google_Tensor, '
                   '_intel and _qualcomm variants are accelerator-specific.',
    },
]

CHUNK = 1 << 20


def url_for(endpoint: str, artifact: dict) -> str:
    return f"{endpoint.rstrip('/')}/{artifact['repo']}/resolve/main/{artifact['path']}"


def remote_size(url: str, timeout: int) -> int | None:
    request = urllib.request.Request(url, method='HEAD')
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            length = response.headers.get('Content-Length')
            return int(length) if length else None
    except (urllib.error.URLError, ValueError, TimeoutError):
        return None


def digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open('rb') as handle:
        for block in iter(lambda: handle.read(CHUNK), b''):
            h.update(block)
    return h.hexdigest()


def fetch(url: str, target: Path, timeout: int, attempts: int) -> int:
    """Download, resuming from whatever is already on disk."""
    target.parent.mkdir(parents=True, exist_ok=True)
    total = remote_size(url, timeout)
    for attempt in range(1, attempts + 1):
        have = target.stat().st_size if target.exists() else 0
        if total is not None and have == total:
            return have
        if total is not None and have > total:
            print('  local file is larger than the source; starting over')
            target.unlink()
            have = 0
        headers = {'Range': f'bytes={have}-'} if have else {}
        label = f'  attempt {attempt}/{attempts}'
        if have:
            label += f', resuming at {have / 1e6:,.0f} MB'
        print(label, flush=True)
        try:
            request = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(request, timeout=timeout) as response:
                if have and response.status != 206:
                    # The server ignored the range; do not append to a partial
                    # file, which would silently produce a corrupt model.
                    print('  server ignored the resume request; starting over')
                    target.unlink(missing_ok=True)
                    have = 0
                mode = 'ab' if have else 'wb'
                start, last = time.monotonic(), time.monotonic()
                with target.open(mode) as handle:
                    while True:
                        block = response.read(CHUNK)
                        if not block:
                            break
                        handle.write(block)
                        have += len(block)
                        now = time.monotonic()
                        if now - last >= 2.0:
                            rate = have / max(now - start, 1e-6) / 1e6
                            share = f' / {total / 1e6:,.0f}' if total else ''
                            print(f'    {have / 1e6:,.0f}{share} MB  {rate:,.2f} MB/s',
                                  flush=True)
                            last = now
            if total is None or have == total:
                return have
            print(f'  short read: {have} of {total} bytes')
        except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
            print(f'  interrupted: {exc}')
        time.sleep(min(5 * attempt, 30))
    raise RuntimeError(f'Could not finish downloading {url}')


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--endpoint', default=DEFAULT_ENDPOINT,
                        help='Hugging Face host to download from (default: %(default)s)')
    parser.add_argument('--timeout', type=int, default=60)
    parser.add_argument('--attempts', type=int, default=8)
    parser.add_argument('--verify', action='store_true',
                        help='hash what is already staged; download nothing')
    args = parser.parse_args()

    DEST.mkdir(parents=True, exist_ok=True)
    entries = []
    for artifact in ARTIFACTS:
        target = DEST / artifact['name']
        url = url_for(args.endpoint, artifact)
        print(f"{artifact['name']}")
        if args.verify:
            if not target.is_file():
                print('  not staged')
                continue
        else:
            print(f'  from {url}')
            fetch(url, target, args.timeout, args.attempts)
        size = target.stat().st_size
        print('  hashing', flush=True)
        sha = digest(target)
        print(f'  {size:,} bytes  sha256 {sha}')
        entries.append({'name': artifact['name'], 'bytes': size, 'sha256': sha,
                        'repo': artifact['repo'], 'path': artifact['path'],
                        'model_id': artifact['model_id'], 'source': url})

    MANIFEST.write_text(json.dumps({'staged': time.strftime('%Y-%m-%d %H:%M:%S'),
                                    'endpoint': args.endpoint,
                                    'artifacts': entries}, indent=2) + '\n',
                        encoding='utf-8')
    print(f'\nManifest: {MANIFEST.relative_to(ROOT)}')
    if entries:
        total = sum(e['bytes'] for e in entries)
        print(f'{len(entries)} artifact(s), {total / 1e9:,.2f} GB staged.')
        print('Deploy with: py -3.12 tools/deploy_models.py --host 192.168.1.22')
    return 0


if __name__ == '__main__':
    os.environ.setdefault('PYTHONIOENCODING', 'utf-8')
    sys.exit(main())
