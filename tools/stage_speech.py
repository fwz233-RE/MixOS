#!/usr/bin/env python3
"""Download the deck's speech models here, where the network works.

The language model already travels this way (``tools/stage_models.py``). Speech
needs the same treatment for the same reason: the deck cannot fetch its own
models, and ``moonshine_voice`` otherwise downloads them on first use, inside
the request that is trying to transcribe or speak. On this device that request
does not fail slowly - it fails, because ``mixos-aiserver.service`` runs with
``IPAddressDeny=any``.

Both directions are staged. Recognition components come from a table inside the
Python package and are mirrored below. Synthesis assets come from moonshine's
native library instead, so that list had to be read off the device; the comment
on ``TTS_ASSETS`` says when and how.

Two things about ``download.moonshine.ai`` decide whether this works.

**It answers HEAD with 403.** Sizing files with a HEAD request reports every
model as forbidden when all of them download perfectly well. A one-byte ranged
GET is the question the CDN is willing to answer, and its ``Content-Range``
carries the total.

**It rejects the default urllib user agent with 403.** Measured 2026-09-13:
the identical ranged GET returns 403 as ``Python-urllib/3.12`` and 206 as a
browser. This is Cloudflare policy on the origin, not an authentication
requirement, so the tool sends a browser user agent and says so here rather
than leaving a future reader to rediscover it.

The files land under ``build/speech/`` in exactly the layout
``moonshine_voice`` expects inside its cache, so deploying them is a copy and
not a translation:

    <cache>/download.moonshine.ai/model/base-zh/quantized/base-zh/tokenizer.bin
    <cache>/download.moonshine.ai/tts/kokoro/model.onnx

Those paths are not invented here. ``download_model_from_info`` builds the first
as ``get_cache_dir() / download_url.replace("https://", "") / component``, and
``download_tts_assets`` builds the second as
``get_cache_dir() / "download.moonshine.ai" / "tts" / key``. A mismatch means
the deck tries to download the file again at runtime, which is the failure this
tool exists to prevent.

    py -3.12 tools/stage_speech.py                    # zh + en, both directions
    py -3.12 tools/stage_speech.py --language zh
    py -3.12 tools/stage_speech.py --no-synthesis     # recognition only
    py -3.12 tools/stage_speech.py --verify           # re-hash what is here
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
DEST = ROOT / 'build/speech'
MANIFEST = DEST / 'manifest.json'

# Cloudflare in front of download.moonshine.ai refuses Python's default agent.
USER_AGENT = ('Mozilla/5.0 (X11; Linux aarch64) AppleWebKit/537.36 '
              '(KHTML, like Gecko) Chrome/140.0 Safari/537.36')

# Mirrors moonshine_voice.download.get_components_for_model_info.
STREAMING_COMPONENTS = ('adapter.ort', 'cross_kv.ort', 'decoder_kv.ort', 'encoder.ort',
                        'frontend.ort', 'streaming_config.json', 'tokenizer.bin')
PLAIN_COMPONENTS = ('encoder_model.ort', 'decoder_model_merged.ort', 'tokenizer.bin')

# What the translator app asks for, and nothing else. moonshine_voice publishes
# six languages and up to five English sizes; every one of them is another
# download and another few hundred MB across a slow link, so the set is chosen
# here rather than fetched wholesale.
#
# English is small-streaming rather than the package default. Left alone,
# `get_model_for_language("en")` picks medium-streaming-en: 449 MB on disk and
# the same again resident, on a machine with 4 GiB shared with a 2.6 GB
# language model. linux/apps/translator/vendor/server.py asks for
# ModelArch.SMALL_STREAMING to match this.
MODELS = {
    'en': {
        'model_name': 'small-streaming-en',
        'model_arch': 4,                      # ModelArch.SMALL_STREAMING
        'url': 'https://download.moonshine.ai/model/small-streaming-en/quantized',
        'streaming': True,
    },
    'zh': {
        'model_name': 'base-zh',
        'model_arch': 1,                      # ModelArch.BASE
        'url': 'https://download.moonshine.ai/model/base-zh/quantized/base-zh',
        'streaming': False,
    },
}

# get_model_for_language prefetches this alongside the English transcriber, so
# staging it is what keeps the first English request from reaching the network.
SPELLING = {
    'language': 'en',
    'url': 'https://download.moonshine.ai/model/spelling-en',
    'components': ('spelling_cnn.ort', 'spelling_cnn_meta.json'),
}

# Speech synthesis, which is the other half of a translator and fails the same
# way without its files: HTTP 500 from a name lookup the unit forbids.
#
# This list cannot be computed here. Recognition components come from a table in
# Python, but TTS dependencies come from moonshine's native library through
# `moonshine_get_tts_dependencies`, which is an aarch64 shared object. These keys
# were read off typixdeck on 2026-09-13 with
# `list_tts_dependency_keys(language, voice=voice)` for exactly the languages and
# voices linux/apps/translator/vendor/server.py asks for - TTS_LANG_MAP maps
# en -> en-us and zh -> zh-hans, and TTS_VOICE_MAP pins Chinese to
# kokoro_zf_xiaoxiao. Change either of those and this list has to be read again;
# .tools/probe_tts_state.py prints it.
#
# kokoro/model.onnx and kokoro/config.json are shared, which is why the two
# languages come to 222 MB together rather than 314 MB.
TTS_BASE = 'https://download.moonshine.ai/tts/'
TTS_ASSETS = {
    'en': ('en_us/dict_filtered_heteronyms.tsv', 'en_us/g2p-config.json',
           'en_us/oov/model.onnx', 'en_us/oov/onnx-config.json',
           'kokoro/model.onnx', 'kokoro/config.json',
           'kokoro/voices/af_heart.kokorovoice'),
    'zh': ('zh_hans/dict.tsv',
           'zh_hans/roberta_chinese_base_upos_onnx/meta.json',
           'zh_hans/roberta_chinese_base_upos_onnx/vocab.txt',
           'zh_hans/roberta_chinese_base_upos_onnx/tokenizer_config.json',
           'zh_hans/roberta_chinese_base_upos_onnx/model.onnx',
           'kokoro/model.onnx', 'kokoro/config.json',
           'kokoro/voices/zf_xiaoxiao.kokorovoice'),
}

CHUNK = 1 << 20


def components_for(language: str, model: dict) -> tuple[str, ...]:
    parts = STREAMING_COMPONENTS if model['streaming'] else PLAIN_COMPONENTS
    if language != 'en':
        return parts
    extra = 'decoder_kv_with_attention.ort' if model['streaming'] else 'decoder_with_attention.ort'
    return parts + (extra,)


def planned(languages: list[str], *, synthesis: bool = True) -> list[dict]:
    """Every file to stage, as (cache-relative path, url) pairs."""
    files, seen = [], set()

    def add(url: str, language: str, model_name: str, model_arch: int | None,
            purpose: str) -> None:
        path = url.replace('https://', '')
        if path in seen:          # kokoro is shared between the two languages
            return
        seen.add(path)
        files.append({'url': url, 'path': path, 'language': language,
                      'model_name': model_name, 'model_arch': model_arch,
                      'purpose': purpose})

    for language in languages:
        model = MODELS[language]
        for component in components_for(language, model):
            add(f"{model['url']}/{component}", language, model['model_name'],
                model['model_arch'], 'recognition')
    if 'en' in languages:
        for component in SPELLING['components']:
            add(f"{SPELLING['url']}/{component}", 'en', 'spelling-en', None,
                'recognition')
    if synthesis:
        for language in languages:
            for key in TTS_ASSETS.get(language, ()):
                add(TTS_BASE + key, language, 'kokoro-g2p', None, 'synthesis')
    return files


def request_for(url: str, headers: dict | None = None) -> urllib.request.Request:
    return urllib.request.Request(url, headers={'User-Agent': USER_AGENT, **(headers or {})})


def remote_size(url: str, timeout: int) -> int | None:
    """Total length, asked for as one byte because HEAD is refused."""
    try:
        with urllib.request.urlopen(request_for(url, {'Range': 'bytes=0-0'}),
                                    timeout=timeout) as response:
            content_range = response.headers.get('Content-Range', '')
            if '/' in content_range:
                return int(content_range.rsplit('/', 1)[1])
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
            print('    local file is larger than the source; starting over')
            target.unlink()
            have = 0
        headers = {'Range': f'bytes={have}-'} if have else {}
        if have:
            print(f'    attempt {attempt}/{attempts}, resuming at {have / 1e6:,.0f} MB',
                  flush=True)
        try:
            with urllib.request.urlopen(request_for(url, headers), timeout=timeout) as response:
                if have and response.status != 206:
                    # Appending to a partial file after the server ignored the
                    # range produces a corrupt model that still loads.
                    print('    server ignored the resume request; starting over')
                    target.unlink(missing_ok=True)
                    have = 0
                mode = 'ab' if have else 'wb'
                with target.open(mode) as handle:
                    while True:
                        block = response.read(CHUNK)
                        if not block:
                            break
                        handle.write(block)
                        have += len(block)
            if total is None or have == total:
                return have
            print(f'    short read: {have:,} of {total:,} bytes')
        except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
            print(f'    interrupted: {exc}')
        time.sleep(min(3 * attempt, 20))
    raise RuntimeError(f'Could not finish downloading {url}')


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--language', action='append', choices=sorted(MODELS),
                        help='stage one language (repeatable); default: every '
                             'language in MODELS')
    parser.add_argument('--no-synthesis', action='store_true',
                        help='recognition only; leave the speech synthesis assets '
                             'alone, which leaves the translator unable to speak')
    parser.add_argument('--timeout', type=int, default=60)
    parser.add_argument('--attempts', type=int, default=8)
    parser.add_argument('--verify', action='store_true',
                        help='hash what is already staged; download nothing')
    args = parser.parse_args()

    languages = args.language or sorted(MODELS)
    files = planned(languages, synthesis=not args.no_synthesis)
    DEST.mkdir(parents=True, exist_ok=True)

    entries, started = [], time.monotonic()
    for index, item in enumerate(files, 1):
        target = DEST / item['path']
        label = f"[{index}/{len(files)}] {item['path'].split('/', 2)[-1]}"
        if args.verify:
            if not target.is_file():
                print(f'{label}: not staged')
                continue
            print(f'{label}: {target.stat().st_size:,} bytes', flush=True)
        else:
            print(label, flush=True)
            fetch(item['url'], target, args.timeout, args.attempts)
        entries.append({'path': item['path'], 'bytes': target.stat().st_size,
                        'sha256': digest(target), 'source': item['url'],
                        'language': item['language'],
                        'model_name': item['model_name'],
                        'model_arch': item['model_arch'],
                        'purpose': item['purpose']})

    MANIFEST.write_text(json.dumps({'staged': time.strftime('%Y-%m-%d %H:%M:%S'),
                                    'languages': languages,
                                    'synthesis': not args.no_synthesis,
                                    'cache_layout': 'moonshine_voice user cache root',
                                    'files': entries}, indent=2) + '\n',
                        encoding='utf-8')
    total = sum(e['bytes'] for e in entries)
    print(f'\nManifest: {MANIFEST.relative_to(ROOT)}')
    for purpose in ('recognition', 'synthesis'):
        part = [e for e in entries if e['purpose'] == purpose]
        if part:
            print(f'  {purpose:<12} {len(part):>3} file(s)  '
                  f'{sum(e["bytes"] for e in part) / 1e6:>7,.1f} MB')
    print(f'{len(entries)} file(s), {total / 1e6:,.1f} MB, '
          f'{time.monotonic() - started:,.0f}s')
    print('Deploy with: py -3.12 tools/deploy_speech.py --host 10.12.194.1 --block-mb 64')
    return 0


if __name__ == '__main__':
    os.environ.setdefault('PYTHONIOENCODING', 'utf-8')
    sys.exit(main())
