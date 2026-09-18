#!/usr/bin/env python3
"""The live translation interface.

Press the space bar, say something, press it again. What you said appears, and
underneath it the translation arrives a few words at a time while the model is
still working on the rest.

Three decisions in here are worth knowing about before reading the code.

**The work happens on a thread and the screen never waits for it.** Recognising
a sentence takes seconds on this machine and translating it takes longer; a
loop that called those in line would stop redrawing, stop reading the keyboard,
and look exactly like a crash. Everything slow runs on one worker thread and
posts events to a queue that the drawing loop drains.

**One job at a time, and it can be abandoned.** Escape sets a flag the worker
checks between tokens and the connection is dropped. Starting a new recording
while an old translation is still streaming would put two generations into a
4 GiB machine that has room for one.

**The transcript is kept, bounded.** Earlier exchanges scroll up rather than
being erased, because the usual reason to translate a sentence is to show it to
somebody, and it should still be there a moment later. Only the most recent
exchanges are held; this runs for hours on a device with no swap to spare.
"""
from __future__ import annotations

import os
import queue
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
        self.state = 'idle'
        self.status = ''
        self.status_kind = 'info'
        self.events: queue.Queue = queue.Queue()
        self.cancel = threading.Event()
        self.worker: threading.Thread | None = None
        self.dirty = True
        self.last_wav = b''

    # -- state ---------------------------------------------------------------
    def say(self, message: str, kind: str = 'info') -> None:
        self.status, self.status_kind, self.dirty = message, kind, True

    def busy(self) -> bool:
        return self.state in ('transcribing', 'translating')

    # -- the slow half -------------------------------------------------------
    def begin_recording(self) -> None:
        if self.busy():
            self.say('still working on the last one; Esc to abandon it', 'warn')
            return
        # Pressing space wins over anything the device is saying. Stopping the
        # player is not enough on its own: _speak runs on the worker thread and
        # synthesis takes seconds, so a reply whose audio had not been handed
        # to the player yet would start playing into the recording that is
        # about to begin. Setting cancel is what _speak checks between
        # synthesising and playing.
        #
        # This is safe here precisely because busy() is false: nothing is being
        # transcribed or translated, so the only worker this can cancel is one
        # that is speaking. end_recording clears it again before the next job.
        self.cancel.set()
        self.player.stop()
        try:
            self.recorder.start()
        except audio.AudioUnavailable as exc:
            self.say(str(exc), 'bad')
            return
        self.state = 'recording'
        self.current = Exchange()
        self.say('listening — space to stop', 'info')

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
        self.cancel.clear()
        self.state = 'transcribing'
        self.say(f'recognising {seconds:.1f}s …', 'info')
        self.worker = threading.Thread(target=self._work, args=(pcm,), daemon=True)
        self.worker.start()

    def _work(self, pcm: bytes) -> None:
        """Recognition, then translation, then speech. Off the drawing thread."""
        peak, _ = audio.levels(pcm)
        if audio.no_signal(pcm):
            # No microphone in the capture at all; see audio.no_signal. Said
            # here rather than after recognition, which would spend several
            # seconds to arrive at an empty transcript and then blame the
            # person for speaking too quietly.
            self.events.put(('failed', audio.NO_MICROPHONE))
            return
        try:
            text = self.speech.transcribe(audio.for_recognition(pcm),
                                          self.source_language)
            if self.cancel.is_set():
                raise backend.Cancelled()
            if not text:
                # An empty transcript and an empty room look the same on screen,
                # and only one of them is answered by standing closer.
                self.events.put(('failed', '没听到声音，离麦克风近一点再说'
                                 if audio.too_quiet(peak)
                                 else 'nothing was recognised'))
                return
            self.events.put(('heard', text))
            translation = self.model.complete(
                self._prompt(text),
                on_token=lambda piece: self.events.put(('token', piece)),
                cancel=self.cancel)
            if self.cancel.is_set():
                raise backend.Cancelled()
            self.events.put(('translated', translation.strip()))
            if self.speak_result and translation.strip():
                self._speak(translation.strip())
        except backend.Cancelled:
            self.events.put(('cancelled', ''))
        except backend.ServiceError as exc:
            self.events.put(('failed', str(exc)))
        except Exception as exc:                    # a bug here must not kill the screen
            self.events.put(('failed', f'{type(exc).__name__}: {exc}'))

    def _speak(self, text: str) -> None:
        try:
            self.events.put(('speaking', ''))
            wav = self.speech.synthesize(text, self.target_language)
            if self.cancel.is_set() or not wav:
                return
            self.last_wav = wav
            self.player.play(wav)
            self.events.put(('spoke', ''))
        except (backend.ServiceError, audio.AudioUnavailable) as exc:
            self.events.put(('note', f'speech output unavailable: {exc}'))

    def _prompt(self, text: str) -> list[dict]:
        """What the model is told. Deliberately narrow.

        A general assistant asked to translate will sometimes explain itself,
        add a romanisation, or answer the question in the sentence rather than
        translating it. On a screen this size that noise is the whole screen,
        so the instruction says what to return and the format it must be in.
        """
        source = LANGUAGE_NAMES.get(self.source_language, self.source_language)
        target = LANGUAGE_NAMES.get(self.target_language, self.target_language)
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
        if self.busy():
            self.cancel.set()
            self.say('stopping …', 'warn')
            return
        if self.player.playing:
            self.player.stop()
            self.say('speech stopped', 'info')

    # -- events from the worker ---------------------------------------------
    def drain(self) -> None:
        while True:
            try:
                kind, value = self.events.get_nowait()
            except queue.Empty:
                return
            self.dirty = True
            if kind == 'heard':
                self.current.source = value
                self.state = 'translating'
                self.say('translating …', 'info')
            elif kind == 'token':
                self.current.target += value
            elif kind == 'translated':
                self.current.target = value or self.current.target
                self.state = 'idle'
                self.say('', 'info')
                self._archive()
            elif kind == 'speaking':
                # Only if the person has not already taken the device back. A
                # reply that was cancelled mid-synthesis still posts this, and
                # overwriting "listening" with "speaking" would say the
                # opposite of what is happening.
                if self.state == 'idle':
                    self.say('speaking …', 'info')
            elif kind == 'spoke':
                if self.state == 'idle':
                    self.say('', 'info')
            elif kind == 'note':
                self.say(value, 'warn')
            elif kind == 'cancelled':
                self.state = 'idle'
                self.say('abandoned', 'warn')
                self._archive()
            elif kind == 'failed':
                self.state = 'idle'
                self.current.error = value
                self.say(value, 'bad')
                self._archive()

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

        # The live pane is at the bottom, where the eye already is, and takes
        # whatever the transcript above does not need.
        live_height = max(6, height // 2)
        transcript_height = height - live_height - 3
        if transcript_height >= 3:
            self._transcript(1, 1, width - 2, transcript_height)
            top = 1 + transcript_height
        else:
            transcript_height, top = 0, 1
        self._live(1, top, width - 2, height - top - 2)
        self._footer(width, height)
        screen.flush()
        self.dirty = False

    def _title(self, width: int) -> None:
        arrow = f'{label_of(self.source_language)} → {label_of(self.target_language)}'
        self.screen.fill(0, 0, width, 1, bg=4)
        self.screen.put(1, 0, tui.truncate('实时翻译', 12), INK, 4, tui.BOLD)
        self.screen.put(max(1, width - tui.text_width(arrow) - 1), 0, arrow, INK, 4)

    def _transcript(self, x: int, y: int, width: int, height: int) -> None:
        self.screen.box(x, y, width, height, DIM, title='历史')
        inner, lines = width - 4, []
        for exchange in self.history:
            for line in tui.wrap(exchange.source, inner):
                lines.append((line, DIM))
            body = exchange.error or exchange.target
            colour = BAD if exchange.error else INK
            for line in tui.wrap(body, inner):
                lines.append((line, colour))
            lines.append(('', DIM))
        # The most recent exchange is the one worth seeing; older ones scroll off.
        for offset, (line, colour) in enumerate(lines[-(height - 2):]):
            self.screen.put(x + 2, y + 1 + offset, line, colour)

    def _live(self, x: int, y: int, width: int, height: int) -> None:
        heading = {'recording': '录音中', 'transcribing': '识别中',
                   'translating': '翻译中'}.get(self.state, '当前')
        self.screen.box(x, y, width, height, ACCENT, title=heading, title_fg=ACCENT)
        inner = width - 4
        row = y + 1
        limit = y + height - 1

        if self.state == 'recording':
            self._meter(x + 2, row, inner)
            row += 2
        for line in tui.wrap(self.current.source, inner):
            if row >= limit:
                break
            self.screen.put(x + 2, row, line, INK, attr=tui.BOLD)
            row += 1
        if self.current.source and row < limit:
            row += 1
        body = self.current.error or self.current.target
        colour = BAD if self.current.error else GOOD
        rendered = tui.wrap(body, inner)
        # A translation longer than the pane shows its end: that is where the
        # tokens are still arriving.
        room = max(0, limit - row)
        visible = rendered[-room:] if room else []
        for line in visible:
            self.screen.put(x + 2, row, line, colour)
            row += 1
        if self.state == 'translating' and visible:
            # A block after the last token, so a slow model is visibly working
            # rather than visibly stuck.
            self.screen.put(x + 2 + tui.text_width(visible[-1]), row - 1, '▌', GOOD)

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
                 'Esc 中止', 'Q 退出',
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
            return False
        if name == ' ':
            self.end_recording() if self.state == 'recording' else self.begin_recording()
        elif name == 'escape':
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
            if self.last_wav:
                try:
                    self.player.play(self.last_wav)
                except audio.AudioUnavailable as exc:
                    self.say(str(exc), 'bad')
            else:
                self.say('nothing has been spoken yet', 'warn')
        elif name in ('c', 'C'):
            self.history.clear()
            self.current = Exchange()
            self.say('cleared')
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
            self.cancel.set()
            self.recorder.cancel()
            self.player.stop()

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
