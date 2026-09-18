"""Where notes are kept.

Plain UTF-8 files in a directory, one per note, with the first line as the
title. Not a database: the point of writing something down on this device is
that it is still readable when the device is not involved, over SSH, in any
editor, after every part of this project has been replaced.

Every write goes to a temporary file in the same directory and is then renamed
over the target. The rename is atomic within a filesystem, so a note is either
the old text or the new text and never a truncated mixture. This matters more
here than on a desktop: the storage is the same eMMC the system boots from, and
the usual way this device stops is the USB cable coming out.
"""
from __future__ import annotations

import os
import re
import time
from pathlib import Path

DEFAULT_ROOT = Path(os.environ.get(
    'MIXOS_NOTES_DIR',
    os.path.join(os.environ.get('XDG_DATA_HOME',
                                os.path.expanduser('~/.local/share')),
                 'mixos', 'notes')))
SUFFIX = '.md'
# Long enough for a sentence, short enough to read in a 64-column list.
TITLE_LIMIT = 48


class Note:
    __slots__ = ('path', 'modified')

    def __init__(self, path: Path, modified: float):
        self.path, self.modified = path, modified

    @property
    def name(self) -> str:
        return self.path.stem

    def title(self) -> str:
        """The first non-empty line, or a note that there is nothing yet."""
        try:
            with self.path.open('r', encoding='utf-8', errors='replace') as handle:
                for line in handle:
                    stripped = line.strip()
                    if stripped:
                        return stripped[:TITLE_LIMIT]
        except OSError:
            return '(unreadable)'
        return '(empty)'

    def when(self) -> str:
        return time.strftime('%m-%d %H:%M', time.localtime(self.modified))


class Store:
    def __init__(self, root: Path | str = DEFAULT_ROOT):
        self.root = Path(root)

    def ensure(self) -> None:
        # 0o700: notes are the one thing on this device that is nobody else's.
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)

    def list(self) -> list[Note]:
        """Newest first, which is the order they are wanted in."""
        self.ensure()
        notes = []
        for path in self.root.glob('*' + SUFFIX):
            try:
                notes.append(Note(path, path.stat().st_mtime))
            except OSError:
                continue
        notes.sort(key=lambda note: note.modified, reverse=True)
        return notes

    def new_name(self) -> str:
        """A name from the clock, made unique without asking anybody."""
        stamp = time.strftime('%Y%m%d-%H%M%S')
        candidate, index = stamp, 1
        while (self.root / (candidate + SUFFIX)).exists():
            index += 1
            candidate = f'{stamp}-{index}'
        return candidate

    def path_for(self, name: str) -> Path:
        """Resolve a note name to a file, refusing anything that escapes.

        The names this interface generates are safe, but a note file can be
        created by hand over SSH, and a name is the only thing here that ever
        reaches the filesystem.
        """
        if not re.fullmatch(r'[A-Za-z0-9._-]{1,64}', name) or name.startswith('.'):
            raise ValueError('unusable note name')
        return self.root / (name + SUFFIX)

    def read(self, name: str) -> str:
        try:
            return self.path_for(name).read_text(encoding='utf-8', errors='replace')
        except FileNotFoundError:
            return ''

    def write(self, name: str, text: str) -> None:
        self.ensure()
        target = self.path_for(name)
        temporary = target.with_name(target.name + '.new')
        with temporary.open('w', encoding='utf-8', newline='\n') as handle:
            handle.write(text)
            handle.flush()
            # The rename is atomic, but only relative to bytes that have
            # reached the disk. Without this an unplanned power cut can leave
            # a correctly named, empty file where the note used to be.
            os.fsync(handle.fileno())
        os.replace(temporary, target)
        try:
            os.chmod(target, 0o600)
        except OSError:
            pass

    def delete(self, name: str) -> None:
        try:
            self.path_for(name).unlink()
        except FileNotFoundError:
            pass
