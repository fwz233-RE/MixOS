#!/usr/bin/env python3
"""The notes interface: a list of notes, and an editor for one of them.

Typing Chinese is not this program's job. ``term-ime`` sits between the
keyboard and this process and hands over finished characters, so as far as the
editor is concerned somebody typed 汉字 directly. That is why every command in
the editor is a control key or a function key: a printable character has to
remain a printable character, or the input method has nothing to convert.

The list obeys the same rule, and for a reason that is easy to miss. The list is
where a person arrives back from the editor, and the input method is still in
whatever mode they left it in — so in Chinese mode ``N``, ``D`` and ``Q`` are
pinyin, not commands, and the list becomes unusable without knowing to switch
back first. Ctrl-N and Ctrl-D always arrive, and Ctrl-Q returns from the editor to the
list, staying there at the root. The device Home control ends the session.
The letters still work for a device with no input method installed;
the footer advertises the control keys, because those are correct either way.
Deleting is confirmed with Enter for the same reason ``Y`` alone is not enough.

The other way to get text in is to say it. Ctrl-R starts recording, Ctrl-R
stops it, and the recognised words are inserted at the cursor. The recognition
runs on the same loopback service the translator uses and takes seconds, so it
happens on a worker thread and the editor stays usable while it works — what
you type meanwhile stays where you typed it, and the recognised text lands at
the cursor's position at the moment it arrives.

Which language is being spoken has to be said, not guessed. The recogniser is
one model per language and it is chosen before the audio is sent, so a Chinese
model asked to transcribe English returns nothing useful rather than English.
Ctrl-T moves between the languages that are installed and the choice is shown
in the footer, because the wrong setting looks exactly like broken recognition.

Wrapping is done here rather than left to the terminal. The screen is 64
columns of a font where a 汉字 is two cells wide; letting lines run off the
edge would hide the end of every sentence, and letting the terminal wrap them
would put the cursor somewhere this program cannot predict.

The session ends when the person leaves this application on the screen, and
that arrives as a SIGHUP from mixosd. It is turned into an ordinary exit so the
note is written on the way out; only a program that ignores it gets killed.
"""
from __future__ import annotations

import os
import queue
import signal
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

import audio                                                  # noqa: E402
import backend                                                # noqa: E402
import tui                                                    # noqa: E402

from store import Store                                       # noqa: E402

# Notes is a document surface rather than a terminal status page. Render it
# on an explicit light paper background so normal text is black instead of
# inheriting the terminal's low-contrast white/dim roles.
NOTES_BG = 15
NOTES_INK = 0
NOTES_META = 0
NOTES_SELECTED_BG = 14
INK = NOTES_INK
DIM = NOTES_META
ACCENT = 12
GOOD = 2
WARN = 3
BAD = 1
BODY_ATTR = tui.BOLD
EDITOR_BODY_Y = 3  # fixed title, full modification time, then a blank separator

AUTOSAVE_SECONDS = 20.0

# The recognition languages this device has models for, in the order Ctrl-T
# walks through them. tools/stage_speech.py stages one recogniser per entry;
# offering a language whose model was never staged would be offering a request
# the backend cannot answer, because mixos-aiserver.service runs with
# IPAddressDeny=any and cannot fetch what it was not given.
SPEECH_LANGUAGES = [('zh', '中文'), ('en', 'English')]
DEFAULT_SPEECH_LANGUAGE = os.environ.get('MIXOS_NOTES_LANG', 'zh')


def language_label(code: str) -> str:
    for value, label in SPEECH_LANGUAGES:
        if value == code:
            return label
    return code


class Interrupted(Exception):
    """mixosd asked this session to end. Not an error; the note still saves."""


def layout(lines: list[str], width: int) -> list[tuple[int, int, str]]:
    """Break logical lines into visual rows that fit the column budget.

    Returns ``(line index, first character index, text)`` per visual row, which
    is what both the drawing and the cursor arithmetic need. A double-width
    character is never split across a row boundary.
    """
    rows: list[tuple[int, int, str]] = []
    if width < 2:
        return [(index, 0, '') for index in range(len(lines))]
    for index, line in enumerate(lines):
        if not line:
            rows.append((index, 0, ''))
            continue
        start, used, chunk = 0, 0, ''
        for position, character in enumerate(line):
            cell = tui.char_width(character)
            if used + cell > width:
                rows.append((index, start, chunk))
                start, chunk, used = position, character, cell
            else:
                chunk += character
                used += cell
        rows.append((index, start, chunk))
    return rows


class Buffer:
    """The text of one note, with a cursor in it."""

    def __init__(self, text: str = ''):
        self.lines = text.split('\n') or ['']
        self.row, self.column = 0, 0
        self.modified = False

    def text(self) -> str:
        return '\n'.join(self.lines)

    def clamp(self) -> None:
        self.row = max(0, min(self.row, len(self.lines) - 1))
        self.column = max(0, min(self.column, len(self.lines[self.row])))

    # -- editing -------------------------------------------------------------
    def insert_text(self, text: str) -> None:
        """Insert possibly multi-line text at the cursor."""
        parts = text.replace('\r\n', '\n').replace('\r', '\n').split('\n')
        line = self.lines[self.row]
        head, tail = line[:self.column], line[self.column:]
        if len(parts) == 1:
            self.lines[self.row] = head + parts[0] + tail
            self.column += len(parts[0])
        else:
            body = [head + parts[0]] + parts[1:-1] + [parts[-1] + tail]
            self.lines[self.row:self.row + 1] = body
            self.row += len(parts) - 1
            self.column = len(parts[-1])
        self.modified = True

    def newline(self) -> None:
        line = self.lines[self.row]
        self.lines[self.row:self.row + 1] = [line[:self.column], line[self.column:]]
        self.row += 1
        self.column = 0
        self.modified = True

    def backspace(self) -> None:
        if self.column > 0:
            line = self.lines[self.row]
            self.lines[self.row] = line[:self.column - 1] + line[self.column:]
            self.column -= 1
        elif self.row > 0:
            previous = self.lines[self.row - 1]
            self.column = len(previous)
            self.lines[self.row - 1] = previous + self.lines[self.row]
            del self.lines[self.row]
            self.row -= 1
        else:
            return
        self.modified = True

    def delete(self) -> None:
        line = self.lines[self.row]
        if self.column < len(line):
            self.lines[self.row] = line[:self.column] + line[self.column + 1:]
        elif self.row + 1 < len(self.lines):
            self.lines[self.row] = line + self.lines[self.row + 1]
            del self.lines[self.row + 1]
        else:
            return
        self.modified = True

    def kill_line(self) -> None:
        line = self.lines[self.row]
        if self.column < len(line):
            self.lines[self.row] = line[:self.column]
        elif self.row + 1 < len(self.lines):
            del self.lines[self.row + 1]
        else:
            return
        self.modified = True

    # -- moving --------------------------------------------------------------
    def left(self) -> None:
        if self.column > 0:
            self.column -= 1
        elif self.row > 0:
            self.row -= 1
            self.column = len(self.lines[self.row])

    def right(self) -> None:
        if self.column < len(self.lines[self.row]):
            self.column += 1
        elif self.row + 1 < len(self.lines):
            self.row, self.column = self.row + 1, 0

    def visual(self, rows: list[tuple[int, int, str]]) -> tuple[int, int]:
        """Where the cursor is on screen: (visual row, column in cells)."""
        best = 0
        for index, (line_index, start, chunk) in enumerate(rows):
            if line_index != self.row:
                continue
            best = index
            if start <= self.column < start + len(chunk):
                return index, tui.text_width(chunk[:self.column - start])
        _, start, chunk = rows[best]
        return best, tui.text_width(chunk[:max(0, self.column - start)])

    def move_visual(self, rows: list[tuple[int, int, str]], delta: int) -> None:
        """Up or down one screen row, keeping roughly the same column."""
        current, cells = self.visual(rows)
        target = current + delta
        if not 0 <= target < len(rows):
            return
        line_index, start, chunk = rows[target]
        used, column = 0, start
        for character in chunk:
            if used >= cells:
                break
            used += tui.char_width(character)
            column += 1
        self.row, self.column = line_index, column
        self.clamp()


class Notes:
    def __init__(self, screen: tui.Screen, store: Store):
        self.screen, self.store = screen, store
        self.speech = backend.Speech()
        self.recorder = audio.Recorder()
        self.events: queue.Queue = queue.Queue()

        self.mode = 'list'
        self.notes = store.list()
        self.selected = 0
        self.name = ''
        self.note_modified = time.time()
        self.buffer = Buffer()
        self.top = 0
        self.status, self.status_kind = '', 'info'
        self.dirty = True
        self.recording = False
        self.recognising = False
        self.last_save = 0.0
        self.confirm_delete = False
        codes = [code for code, _ in SPEECH_LANGUAGES]
        self.speech_language = (DEFAULT_SPEECH_LANGUAGE
                                if DEFAULT_SPEECH_LANGUAGE in codes else codes[0])

    def say(self, message: str, kind: str = 'info') -> None:
        self.status, self.status_kind, self.dirty = message, kind, True

    # -- notes ---------------------------------------------------------------
    def open_selected(self) -> None:
        if not self.notes:
            return
        note = self.notes[self.selected]
        self.name = note.name
        self.note_modified = note.modified
        self.buffer = Buffer(self.store.read(self.name))
        self.mode, self.top = 'edit', 0
        self.last_save = _now()
        self.say('Ctrl+T 切换识别语言   Ctrl+K 删至行尾   Ctrl+Q 返回')

    def create(self) -> None:
        self.name = self.store.new_name()
        self.note_modified = time.time()
        self.buffer = Buffer('')
        self.mode, self.top = 'edit', 0
        self.last_save = _now()
        self.say('new note')

    def save(self, quiet: bool = False) -> None:
        if not self.name:
            return
        try:
            self.store.write(self.name, self.buffer.text())
        except (OSError, ValueError) as exc:
            self.say(f'could not save: {exc}', 'bad')
            return
        self.buffer.modified = False
        try:
            self.note_modified = os.path.getmtime(self.store.path_for(self.name))
        except OSError:
            self.note_modified = time.time()
        self.last_save = _now()
        if not quiet:
            self.say('saved', 'good')

    def close_editor(self) -> None:
        if self.buffer.modified:
            self.save(quiet=True)
        if self.recording:
            self.recorder.cancel()
            self.recording = False
        self.notes = self.store.list()
        self.selected = min(self.selected, max(0, len(self.notes) - 1))
        # The note just edited is the newest, so it is at the top of the list.
        for index, note in enumerate(self.notes):
            if note.name == self.name:
                self.selected = index
                break
        self.mode, self.name = 'list', ''
        self.say('')

    # -- voice ---------------------------------------------------------------
    def cycle_language(self) -> None:
        """Next installed recognition language. Safe while recognising."""
        codes = [code for code, _ in SPEECH_LANGUAGES]
        index = codes.index(self.speech_language)
        self.speech_language = codes[(index + 1) % len(codes)]
        self.say(f'语音识别语言：{language_label(self.speech_language)}')

    def toggle_recording(self) -> None:
        if self.recognising:
            self.say('still recognising the last one', 'warn')
            return
        if self.recording:
            pcm = self.recorder.stop()
            self.recording = False
            if self.recorder.error:
                self.say(self.recorder.error, 'bad')
                return
            if len(pcm) < audio.SAMPLE_RATE * audio.SAMPLE_BYTES // 3:
                self.say('too short to recognise', 'warn')
                return
            self.recognising = True
            self.say('recognising …')
            threading.Thread(target=self._recognise, args=(pcm,), daemon=True).start()
            return
        try:
            self.recorder.start()
        except audio.AudioUnavailable as exc:
            self.say(str(exc), 'bad')
            return
        self.recording = True
        self.say('listening — Ctrl+R to stop')

    def _recognise(self, pcm: bytes) -> None:
        # The language is read once, here, so that pressing Ctrl-T while the
        # worker is running cannot change which model this audio was sent to.
        language = self.speech_language
        peak, _ = audio.levels(pcm)
        if audio.no_signal(pcm):
            # Nothing was captured at all. Recognising it would take several
            # seconds and end in an empty transcript, which reads on screen as
            # "you were too quiet" - the one explanation that cannot be right.
            self.events.put(('dead', ''))
            return
        try:
            text = self.speech.transcribe(audio.for_recognition(pcm), language)
            self.events.put(('quiet' if not text and audio.too_quiet(peak)
                             else 'text', text))
        except backend.ServiceError as exc:
            self.events.put(('failed', str(exc)))
        except Exception as exc:                   # never take the screen down
            self.events.put(('failed', f'{type(exc).__name__}: {exc}'))

    def drain(self) -> None:
        while True:
            try:
                kind, value = self.events.get_nowait()
            except queue.Empty:
                return
            self.recognising = False
            self.dirty = True
            if kind == 'text' and value:
                self.buffer.insert_text(value)
                self.say('')
            elif kind == 'dead':
                # The capture carried no microphone at all; see audio.no_signal.
                self.say(audio.NO_MICROPHONE, 'bad')
            elif kind == 'quiet':
                # An empty transcript and an empty room look the same on screen,
                # and only one of them is answered by standing closer.
                self.say('没听到声音，离麦克风近一点再说', 'warn')
            elif kind == 'text':
                self.say('nothing was recognised', 'warn')
            else:
                self.say(value, 'bad')

    # -- drawing -------------------------------------------------------------
    def draw(self) -> None:
        # Do not inherit the terminal's dark default background. Every cell is
        # painted as black ink on a light paper surface below.
        self.screen.clear(bg=NOTES_BG)
        if self.mode == 'list':
            self._draw_list()
        else:
            self._draw_editor()
        self._footer()
        self.screen.flush()
        self.dirty = False

    def _draw_list(self) -> None:
        width, height = self.screen.columns, self.screen.rows
        self.screen.fill(0, 0, width, 1, bg=NOTES_BG)
        self.screen.put(1, 0, '笔记', INK, NOTES_BG, tui.BOLD)
        count = f'{len(self.notes)} 条'
        self.screen.put(max(1, width - tui.text_width(count) - 1), 0,
                        count, INK, NOTES_BG, BODY_ATTR)
        self.screen.set_cursor(None)

        body = height - 3
        if not self.notes:
            self.screen.put(2, 2, '还没有笔记。按 Ctrl+N 新建。', INK,
                            NOTES_BG, BODY_ATTR)
            return
        # Keep the selection on screen without ever scrolling past the ends.
        first = max(0, min(self.selected - body // 2, len(self.notes) - body))
        for offset in range(min(body, len(self.notes))):
            index = first + offset
            note = self.notes[index]
            chosen = index == self.selected
            y = 1 + offset
            row_bg = NOTES_SELECTED_BG if chosen else NOTES_BG
            self.screen.fill(0, y, width, 1, bg=row_bg)
            stamp = note.when()
            self.screen.put(1, y, stamp, INK, row_bg, BODY_ATTR)
            self.screen.put(1 + tui.text_width(stamp) + 1, y,
                            tui.truncate(note.title(),
                                         width - tui.text_width(stamp) - 3),
                            INK, row_bg, BODY_ATTR)

    def _draw_editor(self) -> None:
        width, height = self.screen.columns, self.screen.rows
        self.screen.fill(0, 0, width, 1, bg=NOTES_BG)
        # Keep document identity separate from its contents. The timestamp
        # gets its own row so it remains complete at the larger 48-column size.
        heading = '笔记'
        self.screen.put(1, 0, heading, INK, NOTES_BG, tui.BOLD)
        stamp = time.strftime('%Y-%m-%d %H:%M',
                              time.localtime(self.note_modified))
        metadata = f'{self.name}.md'
        meta_x = 1 + tui.text_width(heading) + 2
        meta_width = max(0, width - meta_x - 3)
        self.screen.put(meta_x, 0, tui.truncate(metadata, meta_width),
                        NOTES_META, NOTES_BG, BODY_ATTR)
        self.screen.put(1, 1, tui.truncate(f'修改 {stamp}', width - 2),
                        NOTES_META, NOTES_BG, BODY_ATTR)
        mark = '●' if self.buffer.modified else '○'
        self.screen.put(width - 2, 0, mark, INK if self.buffer.modified else NOTES_META,
                        NOTES_BG, BODY_ATTR)

        body = max(1, height - EDITOR_BODY_Y - 2)
        rows = layout(self.buffer.lines, width - 2)
        cursor_row, cursor_cells = self.buffer.visual(rows)
        if cursor_row < self.top:
            self.top = cursor_row
        elif cursor_row >= self.top + body:
            self.top = cursor_row - body + 1
        self.top = max(0, min(self.top, max(0, len(rows) - body)))

        for offset in range(body):
            index = self.top + offset
            if index >= len(rows):
                break
            self.screen.put(1, EDITOR_BODY_Y + offset, rows[index][2], INK,
                            NOTES_BG, BODY_ATTR)
        if self.recording:
            self._meter(height)
        self.screen.set_cursor(1 + cursor_cells, EDITOR_BODY_Y + cursor_row - self.top)

    def _meter(self, height: int) -> None:
        width = self.screen.columns
        caption = f' 录音 {self.recorder.seconds:4.1f}s '
        self.screen.fill(0, height - 2, width, 1, bg=NOTES_BG)
        self.screen.put(1, height - 2, caption, INK, NOTES_BG, tui.BOLD)
        bar = max(0, width - tui.text_width(caption) - 2)
        filled = int(bar * audio.meter(self.recorder.level))
        self.screen.put(1 + tui.text_width(caption), height - 2, '█' * filled,
                        INK, NOTES_BG, BODY_ATTR)

    def _footer(self) -> None:
        width, height = self.screen.columns, self.screen.rows
        if self.status and not (self.mode == 'edit' and self.recording):
            colour = {'info': INK, 'warn': WARN, 'bad': BAD, 'good': GOOD}[self.status_kind]
            self.screen.put(1, height - 2, tui.truncate(self.status, width - 2),
                            colour, NOTES_BG, BODY_ATTR)
        if self.mode == 'list':
            # Control keys, not letters. The list is reached from the editor, and
            # the input method is wherever the person left it: in Chinese mode an
            # 'n' is the start of a syllable and never a command. The letters
            # still work, for the device without term-ime installed, but what is
            # advertised is what is true in both cases.
            keys = '^N 新建  ^D 删除  Enter 打开  ^Q 桌面'
        else:
            # Compact hints remain complete with the device's larger font.
            keys = (f'^R 语音:{language_label(self.speech_language)} '
                    f'^T 换 ^S 保存 ^Q 返回')
        self.screen.fill(0, height - 1, width, 1, bg=NOTES_BG)
        self.screen.put(1, height - 1, tui.truncate(keys, width - 2),
                        INK, NOTES_BG, BODY_ATTR)

    # -- keys ----------------------------------------------------------------
    def key(self, name: str) -> bool:
        self.dirty = True
        if self.mode == 'list':
            return self._list_key(name)
        return self._edit_key(name)

    def _list_key(self, name: str) -> bool:
        if self.confirm_delete:
            self.confirm_delete = False
            # Enter as well as Y: with the input method in Chinese mode a 'y' is
            # the start of a syllable, not an answer.
            if name in ('y', 'Y', 'enter') and self.notes:
                self.store.delete(self.notes[self.selected].name)
                self.notes = self.store.list()
                self.selected = min(self.selected, max(0, len(self.notes) - 1))
                self.say('deleted')
            else:
                self.say('')
            return True
        if name in ('ctrl-q', 'escape', 'q', 'Q', 'ctrl-c'):
            # This is the outermost page: normal PTY exit tells the firmware
            # to return to the desktop. Editor Back still saves one level up.
            return False
        if name == 'up':
            self.selected = max(0, self.selected - 1)
        elif name == 'down':
            self.selected = min(max(0, len(self.notes) - 1), self.selected + 1)
        elif name in ('enter', 'right'):
            self.open_selected()
        elif name in ('n', 'N', 'ctrl-n'):
            self.create()
        elif name in ('d', 'D', 'ctrl-d') and self.notes:
            self.confirm_delete = True
            self.say(f'删除「{self.notes[self.selected].title()}」？ Enter 确认', 'warn')
        elif name == 'ctrl-l':
            self.screen.invalidate()
        else:
            self.dirty = False
        return True

    def _edit_key(self, name: str) -> bool:
        buffer = self.buffer
        rows = layout(buffer.lines, self.screen.columns - 2)
        if name == 'ctrl-q' or name == 'escape':
            self.close_editor()
        elif name == 'ctrl-c':
            self.close_editor()
            return False
        elif name == 'ctrl-s':
            self.save()
        elif name == 'ctrl-r':
            self.toggle_recording()
        elif name == 'ctrl-t':
            self.cycle_language()
        elif name == 'ctrl-k':
            buffer.kill_line()
        elif name == 'enter':
            buffer.newline()
        elif name == 'backspace':
            buffer.backspace()
        elif name == 'delete':
            buffer.delete()
        elif name == 'left':
            buffer.left()
        elif name == 'right':
            buffer.right()
        elif name == 'up':
            buffer.move_visual(rows, -1)
        elif name == 'down':
            buffer.move_visual(rows, 1)
        elif name == 'home':
            buffer.column = 0
        elif name == 'end':
            buffer.column = len(buffer.lines[buffer.row])
        elif name == 'pageup':
            buffer.move_visual(rows, -(max(1, self.screen.rows - EDITOR_BODY_Y - 2)))
        elif name == 'pagedown':
            buffer.move_visual(rows, max(1, self.screen.rows - EDITOR_BODY_Y - 2))
        elif name == 'tab':
            buffer.insert_text('  ')
        elif name == 'ctrl-l':
            self.screen.invalidate()
        elif len(name) == 1 and name >= ' ':
            # Everything printable is text, including whatever term-ime just
            # converted. No printable key is a command in this mode.
            buffer.insert_text(name)
        else:
            self.dirty = False
        return True

    # -- the loop ------------------------------------------------------------
    def run(self, keyboard: tui.Keyboard) -> None:
        running = True
        try:
            while running:
                self.drain()
                if self.screen.poll_resize():
                    self.top, self.dirty = 0, True
                if self.dirty or self.recording:
                    self.draw()
                timeout = 0.06 if self.recording else 0.2
                name = keyboard.read(timeout)
                if name is not None:
                    running = self.key(name)
                if (self.mode == 'edit' and self.buffer.modified
                        and _now() - self.last_save > AUTOSAVE_SECONDS):
                    self.save(quiet=True)
        finally:
            # Reached on the ordinary way out and on SIGHUP alike. Whatever was
            # typed is on disk before this returns.
            if self.mode == 'edit' and self.buffer.modified:
                self.save(quiet=True)
            self.recorder.cancel()


def _now() -> float:
    return time.monotonic()


def _end_session(_signum, _frame):
    """Turn "your session is over" into an exception the main loop unwinds."""
    raise Interrupted()


def main(argv: list[str] | None = None) -> int:
    if not sys.stdout.isatty():
        print('This interface draws on the device screen; it needs a terminal.',
              file=sys.stderr)
        return 2
    # mixosd sends SIGHUP when the person leaves this application on the screen
    # and SIGKILL only if that is ignored. Python's default for SIGHUP is to die
    # immediately, which would lose up to one autosave interval of typing.
    for number in (signal.SIGHUP, signal.SIGTERM):
        signal.signal(number, _end_session)
    store = Store()
    store.ensure()
    try:
        with tui.Screen() as screen, tui.Keyboard() as keyboard:
            Notes(screen, store).run(keyboard)
    except Interrupted:
        return 0
    return 0


if __name__ == '__main__':
    sys.exit(main())
