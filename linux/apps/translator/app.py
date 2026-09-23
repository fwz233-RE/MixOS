#!/usr/bin/env python3
"""The live translation interface.

Press the space bar, say something, press it again. What you said appears, and
underneath it the translation arrives a few words at a time while the model is
still working on the rest.

Three decisions in here are worth knowing about before reading the code.

**The work happens on a thread and the screen never waits for it.** Recognising
a sentence takes seconds on this machine and translating it takes longer; a
loop that called those in line would stop redrawing, stop reading the keyboard,
and look exactly like a crash. Recognition and generation run on a worker;
bounded synthesis and playback queues let the first phrase speak while later
text is still arriving. The drawing loop only drains UI events.

**One job at a time, and it can be abandoned.** Escape stops playback and marks
all stages cancelled. An in-flight STT/TTS request has no server-side cancel API,
so a new job waits for it to return (or time out); another Escape exits the UI.
This prevents cancelled requests accumulating on a 4 GiB machine.

**The transcript is kept on fixed pages.** Recognition is never replaced by a
translation-only tail view. The most recent exchange opens at its original text;
Left/Right (or PageUp/PageDown) show every remaining line and older exchanges.
Only the most recent exchanges are held; this runs for hours on a device with
no swap to spare.
"""
from __future__ import annotations

import os
import queue
import re
import signal
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import audio                                                  # noqa: E402
import backend                                                # noqa: E402
import tui                                                    # noqa: E402

# What this device can actually do. Recognition and synthesis are one model per
# language and the models are staged ahead of time, because
# mixos-aiserver.service runs with IPAddressDeny=any: a language whose model is
# not on the device does not answer slowly, it answers HTTP 500 from a failed
# name lookup. Offering such a language is offering a failure.
#
# Until 2026-09-14 this list held six languages and two of them were staged. One
# press of `l` reached 日本語 and everything after it failed:
#
#   stt ja: 500  HTTPSConnectionPool(host='download.moonshine.ai', port=443):
#           Max retries exceeded with url: /model/base-ja/...
#
# tools/stage_speech.py is what puts the models on the device, so it is the
# authority on what belongs here, and tests/test_apps.py fails if the two drift
# apart. MIXOS_TRANSLATE_LANGUAGES overrides it for a device staged with more,
# so adding a language is a deployment change rather than a code change.
LANGUAGE_LABELS = {
    'zh': '中文',
    'en': 'English',
    'ja': '日本語',
    'ko': '한국어',
    'es': 'Español',
    'ar': 'العربية',
}
INSTALLED_LANGUAGES = ('zh', 'en')
# The English names are what the model is told, and they cover every language
# the backend knows rather than only the installed ones: a device staged with
# more must not start prompting the model in language codes.
LANGUAGE_NAMES = {
    'zh': 'Chinese', 'en': 'English', 'ja': 'Japanese',
    'ko': 'Korean', 'es': 'Spanish', 'ar': 'Arabic',
}


def installed_languages() -> list[tuple[str, str]]:
    """The languages this device is staged for, in the order `l` walks them."""
    override = os.environ.get('MIXOS_TRANSLATE_LANGUAGES', '')
    codes = [code.strip() for code in override.split(',') if code.strip()]
    if not codes:
        codes = list(INSTALLED_LANGUAGES)
    seen, ordered = set(), []
    for code in codes:
        if code not in seen:
            seen.add(code)
            ordered.append(code)
    return [(code, LANGUAGE_LABELS.get(code, code)) for code in ordered]


LANGUAGES = installed_languages()
HISTORY_LIMIT = 20
# Backpressure limits both generated text and complete WAVs on this 4 GiB device.
EVENT_QUEUE_SIZE = 64
TEXT_QUEUE_SIZE = 3
WAV_QUEUE_SIZE = 1
MAX_REPLAY_BYTES = 16 << 20
QUEUE_POLL = 0.03
_END = object()


class SpeechChunks:
    """Incremental sentence/phrase boundaries, not one TTS request per token.

    The first unpunctuated phrase is short; later phrases amortise HTTP/model
    overhead. Latin words and decimal numbers stay intact at normal boundaries.
    A hard cap also bounds pathological output with no spaces or punctuation.
    """

    MAX_CHARS = 160
    ABBREVIATIONS = {'mr', 'mrs', 'ms', 'dr', 'prof', 'sr', 'jr', 'st', 'vs',
                     'etc', 'e.g', 'i.e'}

    def __init__(self, language: str):
        self.cjk = language in ('zh', 'ja', 'ko')
        self.pending = ''
        self.first = True

    def feed(self, piece: str):
        # Process incrementally even if a non-streaming endpoint sends its
        # entire answer in one callback. The pending buffer stays bounded.
        for char in piece:
            self.pending += char
            cut = self._boundary()
            if cut:
                text = self.pending[:cut].strip()
                self.pending = self.pending[cut:]
                if text and any(c.isalnum() for c in text):
                    self.first = False
                    yield text

    def _boundary(self) -> int:
        text = self.pending
        for index, char in enumerate(text):
            if char in '。！？!?\n':
                return index + 1
            if char == '.' and (index + 1 == len(text) or text[index + 1].isspace()):
                word = re.search(r'([\w.]+)\.$', text[:index + 1])
                prefix = word.group(1).lower() if word else ''
                if (prefix in self.ABBREVIATIONS
                        or (len(prefix) == 1 and prefix.isalpha())
                        or re.fullmatch(r'(?:[a-z]\.)+[a-z]', prefix)
                        or (prefix and prefix[-1].isdigit() and index + 1 == len(text))):
                    continue
                return index + 1
            minimum = (6 if self.cjk else 12) if self.first else (16 if self.cjk else 32)
            if char in ',，;；:：、' and index + 1 >= minimum:
                # A comma in 1,000 or colon in 12:30 needs lookahead too.
                if index and text[index - 1].isdigit():
                    if index + 1 == len(text) or text[index + 1].isdigit():
                        continue
                return index + 1
        target = (12 if self.first else 32) if self.cjk else (32 if self.first else 96)
        if len(text) >= target:
            if self.cjk:
                # Avoid cutting Latin names/numbers embedded in Chinese.
                for index in range(target, len(text)):
                    previous = text[index - 1]
                    if (('\u3000' <= previous <= '\u9fff' or '\uac00' <= previous <= '\ud7af')
                            and not text[index].isascii()):
                        return index
            else:
                for index in range(target, len(text)):
                    if text[index].isspace():
                        return index + 1
        return self.MAX_CHARS if len(text) >= self.MAX_CHARS else 0

    def finish(self):
        text, self.pending = self.pending.strip(), ''
        if text and any(char.isalnum() for char in text):
            yield text


class TranslationJob:
    """Immutable settings and private queues for exactly one exchange.

    Cancellation is never cleared/reused. The UI retains a cancelled job until
    its outstanding request returns, so repeated Esc/space cannot accumulate
    requests behind the speech server's synthesis lock.
    """

    def __init__(self, source: str, target: str, speak: bool):
        self.source, self.target, self.speak = source, target, speak
        self.cancel = threading.Event()
        self.audio_stop = threading.Event()
        self.done = threading.Event()
        self.events = queue.Queue(maxsize=EVENT_QUEUE_SIZE)
        self.text = queue.Queue(maxsize=TEXT_QUEUE_SIZE)
        self.wav = queue.Queue(maxsize=WAV_QUEUE_SIZE)
        self.outcome = 'finished'
        self.error = ''
        self.speech_error = ''
        self.clips: list[bytes] = []
        self.clip_bytes = 0
        self.replayable = True

    def remember(self, wav: bytes) -> None:
        self.clip_bytes += len(wav)
        if self.clip_bytes > MAX_REPLAY_BYTES:
            self.clips.clear()
            self.replayable = False
        if self.replayable:
            self.clips.append(wav)


# Palette indices, resolved by the firmware against the active theme.
INK = 7
DIM = 8
ACCENT = 12
GOOD = 10
WARN = 11
BAD = 9


def label_of(code: str) -> str:
    for value, label in LANGUAGES:
        if value == code:
            return label
    return code


class Exchange:
    __slots__ = ('source', 'target', 'error')

    def __init__(self, source: str = '', target: str = '', error: str = ''):
        self.source, self.target, self.error = source, target, error


class Translator:
    def __init__(self, screen: tui.Screen):
        self.screen = screen
        self.speech = backend.Speech()
        self.model = backend.LanguageModel()
        self.recorder = audio.Recorder()
        self.player = audio.Player()

        self.source_language = os.environ.get('MIXOS_TRANSLATE_FROM', 'zh')
        self.target_language = os.environ.get('MIXOS_TRANSLATE_TO', 'en')
        # A direction naming a language this device was not staged for would
        # fail on the first recording, so it is corrected here rather than
        # discovered there.
        codes = [code for code, _ in LANGUAGES]
        if self.source_language not in codes:
            self.source_language = codes[0]
        if self.target_language not in codes or self.target_language == self.source_language:
            self.target_language = next((code for code in codes
                                         if code != self.source_language),
                                        self.source_language)
        self.speak_result = True

        self.history: list[Exchange] = []
        self.current = Exchange()
        self._view_exchange: Exchange | None = None
        self._view_page = 0
        self._recording_ready = False
        self.state = 'idle'
        self.status = ''
        self.status_kind = 'info'
        self.events: queue.Queue = queue.Queue(maxsize=EVENT_QUEUE_SIZE)
        self.cancel = threading.Event()
        self.worker: threading.Thread | None = None
        self._job: TranslationJob | None = None
        # Makes cancellation + stop atomic with the final check + play. A WAV
        # returning just after Escape can never start behind the user's back.
        self._play_lock = threading.Lock()
        self.dirty = True
        self.last_wavs: tuple[bytes, ...] = ()

    # -- state ---------------------------------------------------------------
    def say(self, message: str, kind: str = 'info') -> None:
        self.status, self.status_kind, self.dirty = message, kind, True

    def busy(self) -> bool:
        return self._job is not None or self.state in (
            'transcribing', 'translating', 'speaking', 'stopping')

    # -- the slow half -------------------------------------------------------
    def begin_recording(self) -> None:
        if self.busy():
            self.say('still working on the last one; Esc to abandon it', 'warn')
            return
        self.player.stop()
        try:
            self.recorder.start()
        except audio.AudioUnavailable as exc:
            self.say(str(exc), 'bad')
            return
        self.state = 'recording'
        self.current = Exchange()
        self._view_exchange, self._view_page = None, 0
        self._recording_ready = False
        self.say('正在开启麦克风，请等到显示“录音中”再说话', 'info')

    def _new_job(self) -> TranslationJob:
        job = TranslationJob(self.source_language, self.target_language, self.speak_result)
        self._job = job
        self.cancel = job.cancel
        self.events = job.events
        return job

    def end_recording(self) -> None:
        pcm = self.recorder.stop()
        self.state = 'idle'
        if self.recorder.error:
            self.say(self.recorder.error, 'bad')
            return
        seconds = len(pcm) / (audio.SAMPLE_RATE * audio.SAMPLE_BYTES)
        if seconds < 0.3:
            self.say('too short to recognise', 'warn')
            return
        job = self._new_job()
        self.state = 'transcribing'
        self.say(f'recognising {seconds:.1f}s …', 'info')
        self.worker = threading.Thread(target=self._work, args=(pcm, job), daemon=True)
        self.worker.start()

    @staticmethod
    def _emit(job: TranslationJob, kind: str, value: str) -> None:
        while not job.cancel.is_set():
            try:
                job.events.put((kind, value), timeout=QUEUE_POLL)
                return
            except queue.Full:
                pass
        raise backend.Cancelled()

    @staticmethod
    def _put_audio(job: TranslationJob, destination: queue.Queue, item) -> bool:
        while not job.cancel.is_set() and not job.audio_stop.is_set():
            try:
                destination.put(item, timeout=QUEUE_POLL)
                return True
            except queue.Full:
                pass
        return False

    @staticmethod
    def _get_audio(job: TranslationJob, source: queue.Queue):
        while not job.cancel.is_set() and not job.audio_stop.is_set():
            try:
                return source.get(timeout=QUEUE_POLL)
            except queue.Empty:
                pass
        return _END

    def _stop_job_audio(self, job: TranslationJob) -> None:
        with self._play_lock:
            if self._job is job:
                self.player.stop()

    def _audio_failed(self, job: TranslationJob, exc: Exception) -> None:
        with self._play_lock:
            if not job.speech_error:
                job.speech_error = f'speech output unavailable: {exc}'
            job.audio_stop.set()
            if self._job is job:
                self.player.stop()

    def _synthesize(self, job: TranslationJob) -> None:
        try:
            while True:
                text = self._get_audio(job, job.text)
                if text is _END:
                    self._put_audio(job, job.wav, _END)
                    return
                # The service returns a complete WAV and serialises synthesis;
                # one request at a time, but independent of model generation.
                if job.cancel.is_set() or job.audio_stop.is_set():
                    return
                wav = self.speech.synthesize(text, job.target)
                if job.cancel.is_set() or job.audio_stop.is_set():
                    return
                if not wav:
                    raise audio.AudioUnavailable('the speech service returned no audio')
                job.remember(wav)
                if not self._put_audio(job, job.wav, wav):
                    return
        except Exception as exc:
            self._audio_failed(job, exc)

    def _play_clip(self, job: TranslationJob, wav: bytes) -> bool:
        with self._play_lock:
            if self._job is not job or job.cancel.is_set() or job.audio_stop.is_set():
                return False
            self.player.play(wav)
        # play() itself replaces a clip. Waiting for real process completion,
        # rather than guessing WAV duration, prevents adjacent phrases cutting
        # each other off and keeps busy true until the final sample is played.
        return self.player.wait(job.cancel)

    def _playback(self, job: TranslationJob) -> None:
        try:
            while True:
                wav = self._get_audio(job, job.wav)
                if wav is _END:
                    return
                if not self._play_clip(job, wav):
                    return
        except Exception as exc:
            self._audio_failed(job, exc)

    def _work(self, pcm: bytes, job: TranslationJob) -> None:
        """STT -> streaming model -> bounded synthesis -> ordered playback."""
        threads: list[threading.Thread] = []
        try:
            peak, _ = audio.levels(pcm)
            if audio.no_signal(pcm):
                raise backend.ServiceError(audio.NO_MICROPHONE)
            if job.cancel.is_set():
                raise backend.Cancelled()
            recognition_audio = audio.for_recognition(pcm)
            if job.cancel.is_set():
                raise backend.Cancelled()
            text = self.speech.transcribe(recognition_audio, job.source)
            if job.cancel.is_set():
                raise backend.Cancelled()
            if not text:
                raise backend.ServiceError('没听到声音，离麦克风近一点再说'
                                           if audio.too_quiet(peak)
                                           else 'nothing was recognised')
            self._emit(job, 'heard', text)
            chunks = SpeechChunks(job.target)
            if job.speak:
                for target in (self._synthesize, self._playback):
                    thread = threading.Thread(target=target, args=(job,), daemon=True)
                    thread.start()
                    threads.append(thread)

            received = False

            def on_token(piece: str) -> None:
                nonlocal received
                received = received or bool(piece)
                self._emit(job, 'token', piece)
                if job.speak and not job.audio_stop.is_set():
                    for phrase in chunks.feed(piece):
                        if not self._put_audio(job, job.text, phrase):
                            break

            translation = self.model.complete(
                self._prompt(text, job.source, job.target),
                on_token=on_token, cancel=job.cancel).strip()
            if job.cancel.is_set():
                raise backend.Cancelled()
            # The normal client calls back even for non-SSE responses; support
            # a completion-only client too, without ever speaking text twice.
            if translation and not received:
                on_token(translation)
            self._emit(job, 'translated', translation)
            if job.speak:
                for phrase in chunks.finish():
                    self._put_audio(job, job.text, phrase)
                self._put_audio(job, job.text, _END)
        except backend.Cancelled:
            job.outcome = 'cancelled'
            job.cancel.set()
        except Exception as exc:                    # keep the screen alive
            job.outcome = 'failed'
            job.error = (str(exc) if isinstance(exc, backend.ServiceError)
                         else f'{type(exc).__name__}: {exc}')
            job.audio_stop.set()
        finally:
            if job.cancel.is_set() or job.audio_stop.is_set():
                self._stop_job_audio(job)
            for thread in threads:
                thread.join()
            # Terminal state lives outside the bounded event queue. Shutdown
            # remains possible even when the screen no longer drains events.
            if job.cancel.is_set():
                job.outcome = 'cancelled'
            self._stop_job_audio(job)
            job.done.set()

    def _replay(self, job: TranslationJob, clips: tuple[bytes, ...]) -> None:
        try:
            for wav in clips:
                if not self._play_clip(job, wav):
                    break
        except Exception as exc:
            self._audio_failed(job, exc)
        finally:
            if job.cancel.is_set():
                job.outcome = 'cancelled'
            self._stop_job_audio(job)
            job.done.set()

    def _prompt(self, text: str, source_language: str | None = None,
                target_language: str | None = None) -> list[dict]:
        """What the model is told. Deliberately narrow.

        A general assistant asked to translate will sometimes explain itself,
        add a romanisation, or answer the question in the sentence rather than
        translating it. On a screen this size that noise is the whole screen,
        so the instruction says what to return and the format it must be in.
        """
        source_language = self.source_language if source_language is None else source_language
        target_language = self.target_language if target_language is None else target_language
        source = LANGUAGE_NAMES.get(source_language, source_language)
        target = LANGUAGE_NAMES.get(target_language, target_language)
        return [
            {'role': 'system',
             'content': (f'You are a translation engine. Translate the user message '
                         f'from {source} into {target}. Reply with the translation '
                         f'and nothing else: no explanation, no transliteration, no '
                         f'quotation marks, no notes. Preserve names, numbers and '
                         f'units exactly. If the message is already in {target}, '
                         f'repeat it unchanged.')},
            {'role': 'user', 'content': text},
        ]

    def cycle_language(self, side: str) -> None:
        """Next installed language on one side, pushing the other side away.

        Advancing one side onto the other would leave a direction that
        translates nothing, and with two languages installed that is what the
        first press used to do: it reported "both sides are the same language"
        and left the interface in that state. Moving the other side to where
        this one was keeps every press a usable direction.
        """
        codes = [code for code, _ in LANGUAGES]
        if len(codes) < 2:
            self.say('only one language is installed on this device', 'warn')
            return
        current = self.source_language if side == 'source' else self.target_language
        other = self.target_language if side == 'source' else self.source_language
        following = (codes[(codes.index(current) + 1) % len(codes)]
                     if current in codes else codes[0])
        if following == other:
            other = current
        if side == 'source':
            self.source_language, self.target_language = following, other
        else:
            self.target_language, self.source_language = following, other
        self.say(f'{label_of(self.source_language)} → '
                 f'{label_of(self.target_language)}')

    def abandon(self) -> None:
        if self.state == 'recording':
            self.recorder.cancel()
            self.state = 'idle'
            self.say('recording discarded', 'info')
            return
        with self._play_lock:
            self.cancel.set()
            self.player.stop()
        if self._job is not None:
            self.state = 'stopping'
            self.say('stopping … Esc to exit', 'warn')
        else:
            self.state = 'idle'
            self.say('speech stopped', 'info')

    # -- events from the worker ---------------------------------------------
    def drain(self) -> None:
        job = self._job
        # Bound one redraw's work too: a fast producer must not starve keys.
        for _ in range(EVENT_QUEUE_SIZE):
            try:
                kind, value = self.events.get_nowait()
            except queue.Empty:
                break
            if job is not None and job.cancel.is_set():
                continue
            self.dirty = True
            if kind == 'heard':
                self.current.source = value
                self.state = 'translating'
                self.say('translating …', 'info')
            elif kind == 'token':
                self.current.target += value
            elif kind == 'translated':
                self.current.target = value or self.current.target
                if job is None:
                    self.state = 'idle'
                    self.say('', 'info')
                    self._archive()
                else:
                    self.state = 'speaking' if job.speak else 'translating'
                    self.say('speaking …' if job.speak else '', 'info')
            elif kind == 'failed':
                self.state = 'idle'
                self.current.error = value
                self.say(value, 'bad')
                self._archive()
        if job is None or not job.done.is_set() or not self.events.empty():
            return
        self._job = None
        self.state = 'idle'
        if job.cancel.is_set() or job.outcome == 'cancelled':
            self.say('abandoned', 'warn')
        elif job.outcome == 'failed':
            self.current.error = job.error
            self.say(job.error, 'bad')
        elif job.speech_error:
            self.say(job.speech_error, 'warn')
        else:
            self.say('', 'info')
            if job.speak and job.clips:
                self.last_wavs = tuple(job.clips)
            elif job.speak and not job.replayable:
                self.last_wavs = ()
                self.say('speech finished; too long to keep for replay', 'warn')
        self._archive()
        # Completed jobs must not retain queues of WAVs after cancellation.
        for pending in (job.text, job.wav):
            while True:
                try:
                    pending.get_nowait()
                except queue.Empty:
                    break
        job.clips.clear()

    def _archive(self) -> None:
        if self.current.source or self.current.target or self.current.error:
            self.history.append(self.current)
            del self.history[:-HISTORY_LIMIT]
            self.current = Exchange()

    # -- drawing -------------------------------------------------------------
    def draw(self) -> None:
        screen = self.screen
        width, height = screen.columns, screen.rows
        screen.clear()
        self._title(width)

        self._pages(width, height)
        self._footer(width, height)
        screen.flush()
        self.dirty = False

    def _title(self, width: int) -> None:
        arrow = f'{label_of(self.source_language)} → {label_of(self.target_language)}'
        self.screen.fill(0, 0, width, 1, bg=4)
        self.screen.put(1, 0, tui.truncate('实时翻译', 12), INK, 4, tui.BOLD)
        self.screen.put(max(1, width - tui.text_width(arrow) - 1), 0, arrow, INK, 4)

    def _page_entries(self) -> list[Exchange]:
        entries = list(self.history)
        if (self.current.source or self.current.target or self.current.error
                or self.state in ('recording', 'transcribing')):
            entries.append(self.current)
        return entries or [self.current]

    def _exchange_lines(self, exchange: Exchange) -> list[tuple[str, int]]:
        inner = max(1, self.screen.columns - 6)
        lines = []
        if exchange.source:
            lines.append(('原文', ACCENT))
            lines.extend((line, INK) for line in tui.wrap(exchange.source, inner))
        if exchange.target:
            if lines:
                lines.append(('', DIM))
            lines.append(('译文', GOOD))
            lines.extend((line, GOOD) for line in tui.wrap(exchange.target, inner))
        if exchange.error:
            lines.append(('错误', BAD))
            lines.extend((line, BAD) for line in tui.wrap(exchange.error, inner))
        return lines

    def _page_count(self, exchange: Exchange) -> int:
        room = max(1, self.screen.rows - 5)
        return max(1, (len(self._exchange_lines(exchange)) + room - 1) // room)

    def _selected_exchange(self, entries: list[Exchange]) -> int:
        if self._view_exchange in entries:
            return entries.index(self._view_exchange)
        self._view_exchange, self._view_page = None, 0
        return len(entries) - 1

    def turn_page(self, step: int) -> None:
        if self.state == 'recording':
            return
        entries = self._page_entries()
        index = self._selected_exchange(entries)
        page = min(self._view_page, self._page_count(entries[index]) - 1) + step
        if page < 0:
            if index:
                index -= 1
                page = self._page_count(entries[index]) - 1
            else:
                page = 0
        elif page >= self._page_count(entries[index]):
            if index + 1 < len(entries):
                index += 1
                page = 0
            else:
                page = self._page_count(entries[index]) - 1
        self._view_exchange, self._view_page = entries[index], page

    def _pages(self, width: int, height: int) -> None:
        heading = {'recording': '录音中' if self._recording_ready else '开启麦克风',
                   'transcribing': '识别中',
                   'translating': '翻译中', 'speaking': '朗读中',
                   'stopping': '停止中'}.get(self.state, '识别与翻译')
        self.screen.box(1, 1, width - 2, height - 3, ACCENT,
                        title=heading, title_fg=ACCENT)
        if self.state == 'recording':
            self._meter(3, 2, max(1, width - 6))
            return
        entries = self._page_entries()
        index = self._selected_exchange(entries)
        lines = self._exchange_lines(entries[index])
        room = max(1, height - 5)
        count = max(1, (len(lines) + room - 1) // room)
        self._view_page = min(self._view_page, count - 1)
        first = self._view_page * room
        # Stable page boundaries: new translation tokens never push the
        # original off screen; completing a job retains the same Exchange.
        for offset, (line, colour) in enumerate(lines[first:first + room]):
            self.screen.put(3, 2 + offset, line, colour)
        caption = (f' {index + 1}/{len(entries)} 条  '
                   f'{self._view_page + 1}/{count} 页  ←/→ 翻页 ')
        self.screen.put(3, height - 3, tui.truncate(caption, max(1, width - 6)), DIM)

    def _meter(self, x: int, y: int, width: int) -> None:
        seconds = self.recorder.seconds
        caption = f'{seconds:5.1f}s '
        self.screen.put(x, y, caption, WARN)
        bar = max(0, width - tui.text_width(caption))
        # A full bar means "loud enough to be recognised"; see audio.meter.
        filled = int(bar * audio.meter(self.recorder.level))
        self.screen.put(x + tui.text_width(caption), y, '█' * filled, GOOD)
        self.screen.put(x + tui.text_width(caption) + filled, y,
                        '─' * (bar - filled), DIM)

    def _footer(self, width: int, height: int) -> None:
        colour = {'info': DIM, 'warn': WARN, 'bad': BAD}[self.status_kind]
        if self.status:
            self.screen.put(1, height - 2, tui.truncate(self.status, width - 2), colour)
        self.screen.fill(0, height - 1, width, 1, bg=0)
        self.screen.put(1, height - 1, self.hints(width - 2), DIM, 0)

    def hints(self, budget: int) -> str:
        """As many key hints as fit, in screen order, least useful dropped first.

        These used to be one string. At 70 cells on a 64-column screen
        truncate() cut it mid-word and 'Q 退出' was never drawn at all, so the
        interface gave no way to find out how to leave it. Dropping a whole hint
        is legible; cutting one in half is not, and silently losing the last one
        is worse than either.
        """
        hints = ['空格 录音', 'Tab 换向', 'l/L 语言',
                 f"A 朗读:{'开' if self.speak_result else '关'}",
                 'Esc 中止' if self.busy() or self.state == 'recording' or self.player.playing
                 else 'Esc 返回', 'Q 退出',
                 # Niche, and therefore the first to go on a narrow screen.
                 'R 重放', 'C 清空']
        while hints:
            line = '  '.join(hints)
            if tui.text_width(line) <= budget:
                return line
            hints.pop()
        return ''

    # -- keys ----------------------------------------------------------------
    def key(self, name: str) -> bool:
        """Handle one key. Returns False when the interface should exit."""
        self.dirty = True
        if name in ('q', 'Q', 'ctrl-c', 'ctrl-d'):
            self.abandon()
            return False
        if name == ' ':
            self.end_recording() if self.state == 'recording' else self.begin_recording()
        elif name == 'escape':
            if self.state == 'stopping':
                return False             # cancellation already requested: leave
            if self.state != 'recording' and not self.busy() and not self.player.playing:
                return False
            self.abandon()
        elif name == 'tab':
            self.source_language, self.target_language = (self.target_language,
                                                          self.source_language)
            self.say(f'{label_of(self.source_language)} → '
                     f'{label_of(self.target_language)}')
        elif name in ('l', 'L'):
            self.cycle_language('source' if name == 'l' else 'target')
        elif name in ('a', 'A'):
            self.speak_result = not self.speak_result
        elif name in ('r', 'R'):
            if self.busy() or self.state == 'recording' or self.player.playing:
                self.say('still working; Esc to stop before replay', 'warn')
            elif self.last_wavs:
                job = self._new_job()
                self.state = 'speaking'
                self.say('speaking …', 'info')
                self.worker = threading.Thread(target=self._replay,
                                               args=(job, self.last_wavs), daemon=True)
                self.worker.start()
            else:
                self.say('nothing has been spoken yet', 'warn')
        elif name in ('c', 'C'):
            if self.busy() or self.state == 'recording':
                self.say('still working; Esc to stop before clearing', 'warn')
                return True
            self.history.clear()
            self.current = Exchange()
            self._view_exchange, self._view_page = None, 0
            self.say('cleared')
        elif name in ('left', 'pageup', 'right', 'pagedown'):
            self.turn_page(-1 if name in ('left', 'pageup') else 1)
        elif name == 'ctrl-l':
            self.screen.invalidate()
        else:
            self.dirty = False
        return True

    # -- the loop ------------------------------------------------------------
    def run(self, keyboard: tui.Keyboard) -> None:
        self._greet()
        running = True
        try:
            while running:
                self.drain()
                if self.state == 'recording':
                    if not self.recorder.running:
                        # Natural EOF, a capture fault or the two-minute limit
                        # must not leave the UI claiming it is still listening.
                        self.end_recording()
                    elif not self._recording_ready and self.recorder.seconds > 0:
                        self._recording_ready = True
                        self.say('正在录音，按空格结束', 'info')
                if self.screen.poll_resize():
                    self.dirty = True
                if self.dirty or self.state == 'recording':
                    self.draw()
                # A tenth of a second is invisible to a person and leaves the CPU
                # to the model; while recording the meter wants to move, so the
                # wait is shorter.
                timeout = 0.06 if self.state == 'recording' else 0.15
                name = keyboard.read(timeout)
                if name is not None:
                    running = self.key(name)
        finally:
            # Reached on the ordinary way out and on the SIGHUP mixosd sends
            # when the person leaves this application. arecord and aplay are
            # children of this process and are stopped here rather than left to
            # the kill that follows.
            with self._play_lock:
                self.cancel.set()
                self.player.stop()
            self.recorder.cancel()

    def _greet(self) -> None:
        """Say what is missing before the first attempt fails."""
        missing = []
        if not self.speech.available():
            missing.append('speech service (mixos-aiserver)')
        if not self.model.available():
            missing.append('language model (mixos-litertlm)')
        if missing:
            self.say('not running: ' + ', '.join(missing), 'bad')
        else:
            self.say(audio.describe_device())


class Interrupted(Exception):
    """mixosd asked this session to end. Not an error."""


def _end_session(_signum, _frame):
    raise Interrupted()


def main(argv: list[str] | None = None) -> int:
    if not sys.stdout.isatty():
        print('This interface draws on the device screen; it needs a terminal.',
              file=sys.stderr)
        return 2
    # Leaving this application on the screen arrives as SIGHUP. Handling it runs
    # the cleanup that stops arecord and aplay instead of leaving them to be
    # killed a moment later.
    for number in (signal.SIGHUP, signal.SIGTERM):
        signal.signal(number, _end_session)
    try:
        with tui.Screen() as screen, tui.Keyboard() as keyboard:
            Translator(screen).run(keyboard)
    except Interrupted:
        return 0
    return 0


if __name__ == '__main__':
    sys.exit(main())
