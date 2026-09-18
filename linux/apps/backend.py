"""Clients for the two local services the interfaces depend on.

There are two, on two ports, and they are reached differently on purpose.

**Speech, on 127.0.0.1:3000.** ``linux/apps/translator/service.py`` runs the
vendored backend, which turns recorded audio into text (``POST /api/stt``) and
text into audio (``GET /api/tts``). Those are request-and-answer: nothing is
gained by streaming them, and the WAV has to be complete before ``aplay`` can
be handed it anyway.

**The language model, on 127.0.0.1:9379, spoken to directly.** The vendored
backend also offers ``/proxy?url=...`` into the model, and the upstream web
frontend uses it because a browser page has no other way through the
same-origin policy. This is not a browser. Going through the proxy here would
cost the translation its streaming: upstream's proxy does
``res_body = response.read()``, so it holds the entire answer until the model
has finished generating it. On a 4 GiB Compute Module that is the difference
between words appearing as they are translated and a blank screen for several
seconds. The proxy also restricts targets to ``localhost:9379``, which is
exactly where this connects, so nothing is reached that the proxy would not
have reached.

The model's own API is the OpenAI-compatible one that ``litert-lm serve``
exposes. The path and the model name are read from the environment rather than
frozen here, because the only copy of that server that matters is the one
installed on the device, and a changed path should be a line in a unit file
rather than a patch.
"""
from __future__ import annotations

import base64
import http.client
import json
import os
import socket
import threading
from urllib.parse import urlencode

SPEECH_HOST = os.environ.get('MIXOS_SPEECH_HOST', '127.0.0.1')
SPEECH_PORT = int(os.environ.get('MIXOS_SPEECH_PORT', '3000'))
MODEL_HOST = os.environ.get('MIXOS_MODEL_HOST', '127.0.0.1')
MODEL_PORT = int(os.environ.get('MIXOS_MODEL_PORT', '9379'))
MODEL_PATH = os.environ.get('MIXOS_MODEL_PATH', '/v1/chat/completions')
MODEL_LIST_PATH = os.environ.get('MIXOS_MODEL_LIST_PATH', '/v1/models')
MODEL_NAME = os.environ.get('MIXOS_MODEL_NAME', '')

# Recognising thirty seconds of speech on four Cortex-A76 cores is not fast,
# and the first request additionally loads the model from eMMC.
STT_TIMEOUT = float(os.environ.get('MIXOS_STT_TIMEOUT', '180'))
TTS_TIMEOUT = float(os.environ.get('MIXOS_TTS_TIMEOUT', '180'))
MODEL_TIMEOUT = float(os.environ.get('MIXOS_MODEL_TIMEOUT', '300'))
PROBE_TIMEOUT = 2.0


class ServiceError(RuntimeError):
    """A service answered with a failure, or did not answer.

    Carries a sentence fit to put on a 64-column screen. The traceback stays in
    the journal where it belongs; the person holding the device needs to know
    which of the two services is not running, not which line raised.
    """


class Cancelled(Exception):
    """The person stopped waiting. Not an error, and not shown as one."""


def _connect(host: str, port: int, timeout: float) -> http.client.HTTPConnection:
    try:
        return http.client.HTTPConnection(host, port, timeout=timeout)
    except OSError as exc:                      # pragma: no cover - construction rarely fails
        raise ServiceError(f'{host}:{port} unreachable: {exc}') from exc


def reachable(host: str, port: int, timeout: float = PROBE_TIMEOUT) -> bool:
    """Is anything listening. Used to explain a failure before it happens."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


class Speech:
    """Recognition and synthesis, on the loopback backend."""

    def __init__(self, host: str = SPEECH_HOST, port: int = SPEECH_PORT):
        self.host, self.port = host, port

    def available(self) -> bool:
        return reachable(self.host, self.port)

    def transcribe(self, float32_audio: bytes, language: str = 'en') -> str:
        """Audio to text. ``float32_audio`` comes from ``audio.pcm16_to_float32``."""
        if not float32_audio:
            return ''
        body = json.dumps({
            'audio_base64': base64.b64encode(float32_audio).decode('ascii'),
            'language': language,
        }).encode('utf-8')
        payload = self._request('POST', '/api/stt', body,
                                {'Content-Type': 'application/json'}, STT_TIMEOUT)
        try:
            return json.loads(payload.decode('utf-8')).get('text', '').strip()
        except (ValueError, UnicodeDecodeError) as exc:
            raise ServiceError(f'speech service returned something that is not '
                               f'JSON: {exc}') from exc

    def synthesize(self, text: str, language: str = 'en') -> bytes:
        """Text to a complete WAV buffer, ready for ``audio.Player``."""
        if not text.strip():
            return b''
        query = urlencode({'text': text, 'lang': language})
        return self._request('GET', f'/api/tts?{query}', None, {}, TTS_TIMEOUT)

    def _request(self, method: str, path: str, body, headers: dict, timeout: float) -> bytes:
        connection = _connect(self.host, self.port, timeout)
        try:
            connection.request(method, path, body=body, headers=headers)
            response = connection.getresponse()
            payload = response.read()
            if response.status != 200:
                detail = payload.decode('utf-8', 'replace').strip().split('\n')[0]
                # A model that was never staged surfaces here as a failed name
                # lookup from inside the backend, whose first line is a
                # 200-character urllib3 message. Truncated to fit a 64-column
                # screen that is worse than no message at all, so the one thing
                # it means is said instead.
                if 'download.moonshine.ai' in detail or 'NameResolution' in detail:
                    raise ServiceError('that language is not installed on this '
                                       'device')
                raise ServiceError(f'speech service: HTTP {response.status} '
                                   f'{detail[:100]}')
            return payload
        except (OSError, http.client.HTTPException) as exc:
            if not reachable(self.host, self.port):
                raise ServiceError('the speech service is not running '
                                   '(mixos-aiserver.service)') from exc
            raise ServiceError(f'speech service failed: {exc}') from exc
        finally:
            connection.close()


class LanguageModel:
    """The OpenAI-compatible endpoint that ``litert-lm serve`` publishes.

    One request at a time. The model is the largest thing in a 4 GiB machine
    and a second concurrent generation does not make the first one finish
    sooner; it makes both of them swap.
    """

    def __init__(self, host: str = MODEL_HOST, port: int = MODEL_PORT,
                 path: str = MODEL_PATH, name: str = MODEL_NAME):
        self.host, self.port, self.path = host, port, path
        self._name = name
        self._lock = threading.Lock()

    def available(self) -> bool:
        return reachable(self.host, self.port)

    def name(self) -> str:
        """The model identifier to send, asked of the server once.

        Hard-coding it would mean that deploying a different model silently
        produces "model not found" on the first translation.
        """
        if self._name:
            return self._name
        connection = _connect(self.host, self.port, PROBE_TIMEOUT + 3)
        try:
            connection.request('GET', MODEL_LIST_PATH)
            response = connection.getresponse()
            payload = response.read()
            if response.status == 200:
                listed = json.loads(payload.decode('utf-8')).get('data') or []
                if listed and isinstance(listed[0], dict):
                    self._name = str(listed[0].get('id') or '')
        except (OSError, ValueError, http.client.HTTPException):
            pass                     # the server may not implement the listing
        finally:
            connection.close()
        return self._name

    def complete(self, messages: list[dict], on_token=None,
                 cancel: threading.Event | None = None,
                 temperature: float = 0.2, max_tokens: int = 512) -> str:
        """Generate an answer, calling ``on_token`` as each piece arrives.

        Returns the whole answer. Raises ``Cancelled`` if ``cancel`` is set
        while the model is still generating, which is what pressing Escape
        during a long translation does.
        """
        if not self._lock.acquire(blocking=False):
            raise ServiceError('the model is already answering something')
        try:
            return self._stream(messages, on_token, cancel, temperature, max_tokens)
        finally:
            self._lock.release()

    def _stream(self, messages, on_token, cancel, temperature, max_tokens) -> str:
        body = json.dumps({
            'model': self.name() or 'default',
            'messages': messages,
            'temperature': temperature,
            'max_tokens': max_tokens,
            'stream': True,
        }).encode('utf-8')
        connection = _connect(self.host, self.port, MODEL_TIMEOUT)
        pieces: list[str] = []
        try:
            connection.request('POST', self.path, body=body,
                               headers={'Content-Type': 'application/json',
                                        'Accept': 'text/event-stream'})
            response = connection.getresponse()
            if response.status != 200:
                detail = response.read().decode('utf-8', 'replace').strip().split('\n')[0]
                raise ServiceError(f'language model: HTTP {response.status} {detail[:100]}')
            streaming = 'event-stream' in (response.getheader('Content-Type') or '')
            if not streaming:
                # A server that ignored `stream` still answered correctly; take
                # the whole thing rather than failing over a content type.
                text = self._content(json.loads(response.read().decode('utf-8')))
                if text and on_token:
                    on_token(text)
                return text
            for line in response:
                if cancel is not None and cancel.is_set():
                    raise Cancelled()
                piece = self._event(line)
                if piece is None:
                    continue
                if piece == '':
                    break                    # [DONE]
                pieces.append(piece)
                if on_token:
                    on_token(piece)
        except Cancelled:
            raise
        except (OSError, http.client.HTTPException) as exc:
            if not reachable(self.host, self.port):
                raise ServiceError('the language model is not running '
                                   '(mixos-litertlm.service)') from exc
            raise ServiceError(f'language model failed: {exc}') from exc
        except ValueError as exc:
            raise ServiceError(f'language model sent malformed JSON: {exc}') from exc
        finally:
            # Closing an unfinished response is how a cancelled generation is
            # actually stopped; the server sees the socket go away.
            connection.close()
        return ''.join(pieces)

    @staticmethod
    def _event(line: bytes) -> str | None:
        """One server-sent-event line to a piece of text, '' for the end."""
        text = line.decode('utf-8', 'replace').strip()
        if not text or not text.startswith('data:'):
            return None
        payload = text[5:].strip()
        if payload == '[DONE]':
            return ''
        try:
            return LanguageModel._content(json.loads(payload)) or None
        except ValueError:
            return None

    @staticmethod
    def _content(message: dict) -> str:
        """Pull the text out of a chunk or a whole completion.

        Streaming chunks carry ``delta``; a non-streaming answer carries
        ``message``. Both shapes appear depending on whether the server
        honoured ``stream``, so both are read here rather than at two call
        sites that could drift apart.
        """
        choices = message.get('choices') or []
        if not choices:
            return ''
        choice = choices[0]
        for key in ('delta', 'message'):
            part = choice.get(key)
            if isinstance(part, dict) and part.get('content'):
                return str(part['content'])
        if choice.get('text'):
            return str(choice['text'])
        return ''
