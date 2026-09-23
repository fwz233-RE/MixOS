"""A small terminal user interface for the MixOS screen.

Why not curses. The screen is not a generic terminal; it is
``firmware/esp32s3/main/mix_terminal.c``, a parser this project owns, reached
through ``TERM=mixos``. curses would drive it from a terminfo description that
has to be compiled and installed on the device before anything can draw, and
would then emit whatever that description claims. Writing the sequences
directly means every escape this module sends is one the parser demonstrably
implements: CUP, EL, ED, SGR with the 256-colour extension, the alternate
screen, and cursor visibility. Nothing else is used.

Why a cell buffer. The link to the device is a bounded credit-limited channel.
Redrawing a 64x22 screen costs about 1.4 kB of text before escapes; doing that
sixty times a second would starve it. Drawing happens into a buffer and only
the cells that changed are sent, which for a blinking status line is a few
dozen bytes.

Two rules this module keeps:

* **Double-width characters occupy two cells.** Chinese, Japanese and Korean
  text is the normal case here, not an edge case; a renderer that counts
  characters instead of columns tears the layout apart on the first 汉字.
* **The screen is restored on every exit path.** A TUI that dies leaving the
  alternate screen active and the cursor hidden makes the device look broken.
"""
from __future__ import annotations

import os
import re
import select
import signal
import sys
import unicodedata

ESC = '\x1b'

# --- colours -----------------------------------------------------------------
# xterm palette indices. The firmware renderer maps these to the active theme's
# palette for 0-15 and to the standard cube above that.
BLACK, RED, GREEN, YELLOW, BLUE, MAGENTA, CYAN, WHITE = range(8)
BRIGHT = 8
DEFAULT = -1

BOLD = 1
UNDERLINE = 4
INVERSE = 8


def char_width(character: str) -> int:
    """Columns one character occupies. Zero for combining marks."""
    if unicodedata.combining(character):
        return 0
    return 2 if unicodedata.east_asian_width(character) in ('W', 'F') else 1


def text_width(text: str) -> int:
    return sum(char_width(c) for c in text)


def truncate(text: str, columns: int, ellipsis: str = '…') -> str:
    """Cut to fit, never in the middle of a double-width cell."""
    if text_width(text) <= columns:
        return text
    if columns <= 0:
        return ''
    budget = columns - char_width(ellipsis)
    out, used = [], 0
    for character in text:
        width = char_width(character)
        if used + width > budget:
            break
        out.append(character)
        used += width
    return ''.join(out) + ellipsis


def wrap(text: str, columns: int) -> list[str]:
    """Wrap to a column budget, breaking between words where one can."""
    if columns <= 0:
        return []
    lines: list[str] = []
    for paragraph in text.split('\n'):
        if not paragraph:
            lines.append('')
            continue
        current, used = '', 0
        # CJK has no spaces, so a word-only wrap would produce one endless line.
        # Words are kept together when they fit and broken by column when not.
        for token in re.findall(r'\s+|\S+', paragraph):
            for character in token:
                width = char_width(character)
                if used + width > columns:
                    lines.append(current)
                    if character.isspace():
                        current, used = '', 0
                        continue
                    current, used = character, width
                else:
                    current += character
                    used += width
        lines.append(current)
    return lines


def measure(columns: int | None = None, rows: int | None = None) -> tuple[int, int]:
    """How big the screen actually is, asked of the terminal itself.

    The kernel knows, because whoever opened the pseudo-terminal told it: for
    a program started by ``mixosd`` that is the grid the device is displaying,
    and for one started inside ``term-ime`` it is one row less, because the
    input method keeps the bottom row for its candidate bar. Reading
    ``MIXOS_COLS`` and ``MIXOS_ROWS`` instead would be right in the first case
    and one row too tall in the second, which scrolls the whole interface up
    by a line on every redraw.

    The environment variables remain the fallback, for the case where there is
    no terminal to ask at all.
    """
    if columns and rows:
        return int(columns), int(rows)
    measured = None
    try:
        measured = os.get_terminal_size(sys.stdout.fileno())
    except (OSError, ValueError, AttributeError):
        try:
            measured = os.get_terminal_size()
        except (OSError, ValueError):
            measured = None
    if measured and measured.columns > 1 and measured.lines > 1:
        return (int(columns or measured.columns), int(rows or measured.lines))
    return (int(columns or os.environ.get('MIXOS_COLS') or 64),
            int(rows or os.environ.get('MIXOS_ROWS') or 22))


class Cell:
    __slots__ = ('char', 'fg', 'bg', 'attr', 'width')

    def __init__(self, char=' ', fg=DEFAULT, bg=DEFAULT, attr=0, width=1):
        self.char, self.fg, self.bg, self.attr, self.width = char, fg, bg, attr, width

    def same(self, other) -> bool:
        return (self.char == other.char and self.fg == other.fg
                and self.bg == other.bg and self.attr == other.attr)


class Screen:
    """A double-buffered view of the device's terminal grid."""

    def __init__(self, out=None, columns: int | None = None, rows: int | None = None):
        self.out = out or sys.stdout
        self.columns, self.rows = measure(columns, rows)
        self.front = self._blank()
        self.back = self._blank()
        self._entered = False
        self._cursor = None
        self._resized = False

    def _blank(self):
        return [[Cell() for _ in range(self.columns)] for _ in range(self.rows)]

    # -- lifetime ------------------------------------------------------------
    def __enter__(self):
        self.enter()
        return self

    def __exit__(self, *_):
        self.leave()
        return False

    def enter(self):
        if self._entered:
            return
        self._entered = True
        # Alternate screen first, so whatever the shell left behind is kept and
        # comes back untouched when this exits.
        self.out.write(f'{ESC}[?1049h{ESC}[?25l{ESC}[2J{ESC}[H')
        self.out.flush()
        for name in ('SIGTERM', 'SIGHUP'):
            received = getattr(signal, name, None)
            if received is None:
                continue          # Windows has no SIGHUP; the tests run there
            try:
                signal.signal(received, self._on_signal)
            except (ValueError, OSError):
                pass          # not the main thread, or the platform disagrees
        try:
            signal.signal(signal.SIGWINCH, self._on_resize)
        except (AttributeError, ValueError, OSError):
            pass              # no SIGWINCH here; the size simply never changes

    def _on_resize(self, *_):
        # Only a flag: the handler runs between bytecodes and must not touch
        # the buffers the drawing code is in the middle of.
        self._resized = True

    def poll_resize(self) -> bool:
        """Adopt a new terminal size if one arrived. True when it changed.

        The device can switch between the two cell sizes while an interface is
        running, which changes the grid under it. Every stored coordinate is
        then wrong, so the buffers are rebuilt and the caller redraws.
        """
        if not self._resized:
            return False
        self._resized = False
        columns, rows = measure()
        if (columns, rows) == (self.columns, self.rows):
            return False
        self.columns, self.rows = columns, rows
        self.front, self.back = self._blank(), self._blank()
        self.out.write(f'{ESC}[2J{ESC}[H')
        self.out.flush()
        return True

    def _on_signal(self, *_):
        self.leave()
        raise SystemExit(0)

    def leave(self):
        if not self._entered:
            return
        self._entered = False
        self.out.write(f'{ESC}[0m{ESC}[?25h{ESC}[?1049l')
        try:
            self.out.flush()
        except (ValueError, OSError):
            pass

    # -- drawing -------------------------------------------------------------
    def clear(self, bg=DEFAULT):
        for row in self.back:
            for cell in row:
                cell.char, cell.fg, cell.bg, cell.attr, cell.width = ' ', DEFAULT, bg, 0, 1

    def put(self, x: int, y: int, text: str, fg=DEFAULT, bg=DEFAULT, attr=0) -> int:
        """Write text at a cell position. Returns the column after the text."""
        if not 0 <= y < self.rows:
            return x
        for character in text:
            width = char_width(character)
            if width == 0:
                continue
            if x >= self.columns:
                break
            if x + width > self.columns:
                # A double-width glyph that does not fit is not drawn half.
                break
            if x >= 0:
                cell = self.back[y][x]
                cell.char, cell.fg, cell.bg, cell.attr, cell.width = character, fg, bg, attr, width
                if width == 2:
                    trailing = self.back[y][x + 1]
                    trailing.char, trailing.fg = '', fg
                    trailing.bg, trailing.attr, trailing.width = bg, attr, 0
            x += width
        return x

    def fill(self, x: int, y: int, width: int, height: int, bg=DEFAULT, char=' '):
        for row in range(y, min(y + height, self.rows)):
            column = max(x, 0)
            while column < min(x + width, self.columns):
                cell = self.back[row][column]
                cell.char, cell.fg, cell.bg, cell.attr, cell.width = char, DEFAULT, bg, 0, 1
                column += 1

    def box(self, x: int, y: int, width: int, height: int, fg=DEFAULT, bg=DEFAULT,
            title: str = '', title_fg=None):
        """A single-line frame. Every glyph used is in the GB2312 font subset."""
        if width < 2 or height < 2:
            return
        horizontal = '─' * (width - 2)
        self.put(x, y, '┌' + horizontal + '┐', fg, bg)
        for row in range(y + 1, y + height - 1):
            self.put(x, row, '│', fg, bg)
            self.put(x + width - 1, row, '│', fg, bg)
        self.put(x, y + height - 1, '└' + horizontal + '┘', fg, bg)
        if title:
            label = truncate(' ' + title + ' ', width - 4)
            self.put(x + 2, y, label, title_fg if title_fg is not None else fg, bg)

    def set_cursor(self, x: int | None, y: int | None = None):
        """Show the terminal cursor at a cell, or hide it with None."""
        self._cursor = None if x is None or y is None else (x, y)

    # -- presenting ----------------------------------------------------------
    @staticmethod
    def _sgr(fg, bg, attr) -> str:
        parts = ['0']
        if attr & BOLD:
            parts.append('1')
        if attr & UNDERLINE:
            parts.append('4')
        if attr & INVERSE:
            parts.append('7')
        if fg != DEFAULT:
            parts.append(f'38;5;{fg}')
        if bg != DEFAULT:
            parts.append(f'48;5;{bg}')
        return f'{ESC}[' + ';'.join(parts) + 'm'

    def flush(self):
        """Send only what changed."""
        out: list[str] = []
        pen = None
        for y in range(self.rows):
            x = 0
            cursor_here = False
            while x < self.columns:
                back, front = self.back[y][x], self.front[y][x]
                if back.same(front):
                    x += max(back.width, 1)
                    continue
                if not cursor_here:
                    out.append(f'{ESC}[{y + 1};{x + 1}H')
                    cursor_here = True
                style = (back.fg, back.bg, back.attr)
                if style != pen:
                    out.append(self._sgr(*style))
                    pen = style
                out.append(back.char or ' ')
                front.char, front.fg, front.bg = back.char, back.fg, back.bg
                front.attr, front.width = back.attr, back.width
                if back.width == 2 and x + 1 < self.columns:
                    trailing_back, trailing_front = self.back[y][x + 1], self.front[y][x + 1]
                    trailing_front.char, trailing_front.fg = trailing_back.char, trailing_back.fg
                    trailing_front.bg, trailing_front.attr = trailing_back.bg, trailing_back.attr
                    trailing_front.width = trailing_back.width
                    x += 2
                else:
                    x += 1
                # The next changed cell may not be adjacent; re-home then.
                if x < self.columns and self.back[y][x].same(self.front[y][x]):
                    cursor_here = False
        if self._cursor:
            x, y = self._cursor
            out.append(f'{ESC}[{y + 1};{x + 1}H{ESC}[?25h')
        else:
            out.append(f'{ESC}[?25l')
        if out:
            self.out.write(''.join(out))
            self.out.flush()

    def invalidate(self):
        """Forget what is on screen so the next flush redraws everything."""
        self.front = self._blank()
        self.out.write(f'{ESC}[2J')
        self.out.flush()


# --- input --------------------------------------------------------------------
KEY_SEQUENCES = {
    '[A': 'up', '[B': 'down', '[C': 'right', '[D': 'left',
    '[H': 'home', '[F': 'end', '[1~': 'home', '[4~': 'end',
    '[5~': 'pageup', '[6~': 'pagedown', '[2~': 'insert', '[3~': 'delete',
    '[Z': 'shift-tab', '[32;2u': 'shift-space',
    'OA': 'up', 'OB': 'down', 'OC': 'right', 'OD': 'left',
}


class Keyboard:
    """Raw keystrokes from the device, decoded into names.

    There is no key-release event. The link carries the bytes a key produces
    and nothing else, so an interface here can react to a key going down and
    never to it coming up. Anything that wants "hold to do X" has to be
    "press to start, press again to stop" instead.
    """

    def __init__(self, fd: int | None = None):
        self.fd = fd if fd is not None else sys.stdin.fileno()
        self.buffer = b''
        self._saved = None

    def __enter__(self):
        self.raw()
        return self

    def __exit__(self, *_):
        self.restore()
        return False

    def raw(self):
        try:
            import termios
            import tty
        except ImportError:
            return                      # not a POSIX terminal; reads stay cooked
        try:
            self._saved = termios.tcgetattr(self.fd)
            tty.setraw(self.fd)
        except (termios.error, OSError):
            self._saved = None

    def restore(self):
        if self._saved is None:
            return
        try:
            import termios
            termios.tcsetattr(self.fd, termios.TCSADRAIN, self._saved)
        except Exception:
            pass
        self._saved = None

    def wait(self, timeout: float | None = None) -> bool:
        if self.buffer:
            return True
        try:
            ready, _, _ = select.select([self.fd], [], [], timeout)
        except (OSError, ValueError):
            return False
        return bool(ready)

    def read(self, timeout: float | None = 0.0) -> str | None:
        """One key, or None if nothing arrived before the timeout.

        Names: printable characters as themselves, 'enter', 'escape', 'tab',
        'backspace', 'up'/'down'/'left'/'right', 'ctrl-x', 'f1'..'f4'.
        """
        if not self.buffer:
            if not self.wait(timeout):
                return None
            try:
                chunk = os.read(self.fd, 1024)
            except (OSError, ValueError):
                return None
            if not chunk:
                return None
            self.buffer += chunk

        byte = self.buffer[0]
        if byte == 0x1b:
            return self._escape()
        if byte in (0x0d, 0x0a):
            self.buffer = self.buffer[1:]
            return 'enter'
        if byte == 0x09:
            self.buffer = self.buffer[1:]
            return 'tab'
        if byte in (0x7f, 0x08):
            self.buffer = self.buffer[1:]
            return 'backspace'
        if byte < 0x20:
            self.buffer = self.buffer[1:]
            return 'ctrl-' + chr(byte + 96)
        # UTF-8: wait for the whole sequence rather than decoding half a 汉字.
        length = 1 if byte < 0x80 else 2 if byte < 0xe0 else 3 if byte < 0xf0 else 4
        if len(self.buffer) < length:
            if not self.wait(0.05):
                self.buffer = self.buffer[1:]
                return None
            try:
                self.buffer += os.read(self.fd, 1024)
            except (OSError, ValueError):
                return None
            if len(self.buffer) < length:
                self.buffer = self.buffer[1:]
                return None
        raw, self.buffer = self.buffer[:length], self.buffer[length:]
        try:
            return raw.decode('utf-8')
        except UnicodeDecodeError:
            return None

    def _read_more(self, timeout: float) -> bool:
        # wait() intentionally reports buffered bytes as ready. When decoding
        # an incomplete escape, poll the fd itself or os.read could block.
        try:
            ready, _, _ = select.select([self.fd], [], [], timeout)
            if not ready:
                return False
            chunk = os.read(self.fd, 1024)
        except (OSError, ValueError):
            return False
        self.buffer += chunk
        return bool(chunk)

    def _escape(self) -> str | None:
        # A lone Escape needs a short timeout, but once a CSI/SS3 introducer
        # arrived retain its parameters across reads. Splitting Shift+Space's
        # CSI 32;2u must not leak "32;2u" into a note without term-ime.
        if len(self.buffer) == 1 and not self._read_more(0.05):
            self.buffer = b''
            return 'escape'
        while True:
            text = self.buffer[1:].decode('latin-1')
            for sequence, name in KEY_SEQUENCES.items():
                if text.startswith(sequence):
                    self.buffer = self.buffer[1 + len(sequence):]
                    return name
            if len(text) >= 2 and text[:2] in ('OP', 'OQ', 'OR', 'OS'):
                self.buffer = self.buffer[3:]
                return 'f' + str(ord(text[1]) - ord('P') + 1)
            if len(text) <= 32 and re.fullmatch(r'[\[O][0-9;?]*', text):
                if self._read_more(0.05):
                    continue
                return None
            # Unrecognised: discard the whole sequence, never printable tail
            # bytes. Bound partial input so a malformed report cannot grow it.
            match = re.match(r'[\[\]O][0-9;?]*[A-Za-z~]', text)
            consumed = match.end() if match else (len(text) if len(text) > 32 else 1)
            self.buffer = self.buffer[1 + consumed:]
            return None
