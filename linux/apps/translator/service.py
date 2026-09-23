#!/usr/bin/env python3
"""Run the vendored translator backend the way this device needs it.

``vendor/server.py`` is a byte-exact copy of Google's `gemma-translator`
backend and is never edited; see ``vendor/PROVENANCE.json``. Every difference
between upstream and this device lives here, where it can be read in one place:

1. **Loopback only.** Upstream binds ``("", 3000)``, which publishes speech
   recognition, speech synthesis and a proxy into the language model on every
   interface. On a handheld that joins whatever Wi-Fi is nearby that is an open
   service on a stranger's network. Only the two local TUIs ever call this, so
   it binds ``127.0.0.1``.

2. **No eager model loading by default.** Upstream pre-warms the English speech
   models at startup. This machine has 4 GiB of RAM shared with a language
   model of comparable size, so the first request pays the loading cost instead
   of the device paying it forever. ``--prewarm en`` restores the old behaviour
   when the memory is known to be there.

3. **A readable failure when a dependency is missing.** Upstream imports
   ``moonshine_voice`` lazily inside the request handler, so a missing
   dependency surfaces as an HTTP 500 with a traceback in a log nobody is
   reading. ``--check`` answers the question directly.

4. **The English recogniser is the small one.** Upstream asks
   ``get_model_for_language`` for no particular size, which returns the first
   entry in moonshine's table: ``medium-streaming-en``, 449 MB on disk and
   resident again while it runs. ``mixos-aiserver.service`` caps this process
   at 1200 MiB on a 4 GiB machine that is also holding a 2.6 GB language model,
   and ``MAX_MODELS = 2`` promises room for a second recogniser beside it.
   ``small-streaming-en`` is 246 MB and is what ``tools/stage_speech.py`` puts
   on the device. The two have to agree: the unit runs with
   ``IPAddressDeny=any``, so a request for a model that was not staged is not a
   slow first request, it is a failed one.

5. **Chinese decoding has a sufficient output budget.** Moonshine's default
   per-second token budget truncates Chinese even with a complete recording.
   A same-waveform device comparison recovered the missing sentence ending at
   16 tokens/second, while adding silence did not. The Chinese recognizer alone
   gets this bounded override; model files, VAD and English settings stay intact.

Run it:

    python3 -m linux.apps.translator.service            # 127.0.0.1:3000
    python3 linux/apps/translator/service.py --check    # dependencies only
"""
from __future__ import annotations

import argparse
import importlib.util
import socketserver
import sys
import threading
from pathlib import Path

HERE = Path(__file__).resolve().parent
VENDOR = HERE / 'vendor' / 'server.py'
DEFAULT_HOST = '127.0.0.1'
DEFAULT_PORT = 3000

# Language -> the moonshine ModelArch to ask for, where the package's own choice
# is wrong for this device. ModelArch.SMALL_STREAMING == 4; the integer appears
# here rather than the name so that reading this file does not require importing
# a package that only exists on the device. tools/stage_speech.py declares the
# same value, and tests/test_apps.py fails if the two drift apart.
STT_MODEL_ARCH = {'en': 4}
# Tokens are model subword units, not characters. A Chinese character can use
# multiple tokens; the unconfigured decoder can reach its cap before sentence
# end. 16 is finite and measured against complete short/long Chinese fixtures.
STT_OPTIONS = {'zh': {'max_tokens_per_second': 16}}


def load_upstream():
    """Import the vendored module without running its ``__main__`` block."""
    if not VENDOR.is_file():
        raise SystemExit(f'Vendored backend missing: {VENDOR}')
    spec = importlib.util.spec_from_file_location('gemma_translator_backend', VENDOR)
    module = importlib.util.module_from_spec(spec)
    # The upstream file guards its server startup with __name__ == '__main__',
    # so importing it gives us the handler and the model caches and nothing else.
    spec.loader.exec_module(module)
    return module


def check_dependencies() -> int:
    """Name what is missing, once, instead of failing per request."""
    missing = []
    for name in ('numpy', 'moonshine_voice'):
        if importlib.util.find_spec(name) is None:
            missing.append(name)
    if missing:
        print('Missing Python packages: ' + ', '.join(missing))
        print('Install them into this service\'s virtual environment with '
              'linux/apps/translator/vendor/requirements.txt.')
        return 1
    print('numpy and moonshine_voice are importable.')
    return 0


def apply_model_choice() -> str:
    """Make "no size specified" mean the size that was actually staged.

    The vendored backend calls ``get_model_for_language(language)`` without an
    architecture, and moonshine answers with the first entry in its table. For
    English that is ``medium-streaming-en``, which is not the model on this
    device. Rather than edit the vendored file - the one rule this directory has
    - the package function is wrapped so that an unspecified size becomes the
    specified one. The vendored code imports it inside the request handler, so
    it picks up this wrapper on every call.

    Returns a line describing what changed, for the startup log.
    """
    import moonshine_voice
    from moonshine_voice.moonshine_api import ModelArch

    original = moonshine_voice.get_model_for_language
    if getattr(original, 'mixos_wrapped', False):
        return 'speech model sizes: already set'

    def choose(wanted_language='en', wanted_model_arch=None, **kwargs):
        if wanted_model_arch is None:
            override = STT_MODEL_ARCH.get(wanted_language)
            if override is not None:
                wanted_model_arch = ModelArch(override)
        return original(wanted_language, wanted_model_arch, **kwargs)

    choose.mixos_wrapped = True
    moonshine_voice.get_model_for_language = choose
    chosen = ', '.join(f'{language}={ModelArch(arch).name.lower()}'
                       for language, arch in sorted(STT_MODEL_ARCH.items()))
    return f'speech model sizes: {chosen}'


def apply_stt_options(upstream) -> None:
    """Configure Chinese decoding without editing the vendored backend.

    Keep upstream's lock, two-model LRU cache and other languages unchanged.
    This is installed once before the HTTP server or prewarm thread starts.
    """
    original = upstream.get_stt_recognizer
    if getattr(original, 'mixos_options_wrapped', False) is True:
        return

    def configured(language='en'):
        options = STT_OPTIONS.get(language)
        if not options:
            return original(language)
        with upstream._stt_lock:
            cache = upstream._stt_recognizers
            if language in cache:
                cache.move_to_end(language)
                return cache[language]
            from moonshine_voice import get_model_for_language, Transcriber
            if len(cache) >= upstream.MAX_MODELS:
                _, evicted = cache.popitem(last=False)
                del evicted
            model_path, model_arch = get_model_for_language(language)
            recognizer = Transcriber(model_path=model_path, model_arch=model_arch,
                                      options=dict(options))
            cache[language] = recognizer
            print(f'[STT] {language}: decoder options {options}', flush=True)
            return recognizer

    configured.mixos_options_wrapped = True
    upstream.get_stt_recognizer = configured


class LocalServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--host', default=DEFAULT_HOST,
                        help='interface to bind (default: %(default)s; anything else '
                             'publishes speech and the model proxy to the network)')
    parser.add_argument('--port', type=int, default=DEFAULT_PORT)
    parser.add_argument('--prewarm', metavar='LANG', default=None,
                        help='load the speech models for LANG at startup instead of '
                             'on first use; costs memory this device may not have')
    parser.add_argument('--check', action='store_true',
                        help='report whether the dependencies are importable, then exit')
    args = parser.parse_args(argv)

    if args.check:
        return check_dependencies()

    upstream = load_upstream()
    # A wrong model size here is not a slow first request but a failed one: the
    # unit runs with IPAddressDeny=any and cannot fetch what it was not given.
    try:
        print(apply_model_choice(), flush=True)
    except ImportError as exc:
        print(f'WARNING: could not set the speech model sizes ({exc}); the backend '
              f'will ask for moonshine\'s defaults, which are not what is staged '
              f'on this device.', flush=True)

    apply_stt_options(upstream)

    if args.host != DEFAULT_HOST:
        print(f'WARNING: binding {args.host} publishes speech recognition, speech '
              f'synthesis and a proxy into the language model beyond this machine.',
              flush=True)

    with LocalServer((args.host, args.port), upstream.ProxyHTTPRequestHandler) as httpd:
        print(f'Translator backend on http://{args.host}:{args.port}', flush=True)
        print('  POST /api/stt   {"audio_base64": <float32 LE, 16 kHz mono>, "language": "en"}', flush=True)
        print('  GET  /api/tts?text=...&lang=...  -> 16-bit mono WAV', flush=True)
        print('  ANY  /proxy?url=http://localhost:9379/...', flush=True)
        if args.prewarm:
            def warm(language: str) -> None:
                try:
                    print(f'[prewarm] loading speech models for {language}', flush=True)
                    upstream.get_stt_recognizer(language)
                    upstream.get_tts_engine(language)
                    print('[prewarm] done', flush=True)
                except Exception as exc:                      # a failed warm-up is not fatal
                    print(f'[prewarm] failed: {exc}', flush=True)
            threading.Thread(target=warm, args=(args.prewarm,), daemon=True).start()
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print('\nShutting down.', flush=True)
    return 0


if __name__ == '__main__':
    sys.exit(main())
