"""Durable writes: get bytes onto the medium before reporting success.

These helpers existed in three copies (tools/flash_font_on_pi.py,
tools/flash_keyboard_on_pi.py, and a fourth as a string inside the installer
script that tools/deploy_keyboard.py generates), and only one of them guarded
the directory fsync for non-POSIX platforms.

They matter because every caller is about to write flash or hand a file to a
flashing tool: a file that is in the page cache but not on disk is a file that
a power cut turns into a bricked device.
"""
from __future__ import annotations

import os
from pathlib import Path

__all__ = ["sync_directory", "fsync_file", "durable_write", "durable_new"]


def sync_directory(path) -> None:
    """Flush a directory entry so a newly created name survives a power cut.

    Windows has no directory file descriptor and no equivalent call; the
    function is a no-op there rather than an error, because the tools that use
    it are also exercised off-device by the test suite.
    """
    if os.name != "posix" or not hasattr(os, "O_DIRECTORY"):
        return
    fd = os.open(str(path), os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def fsync_file(path) -> None:
    """Flush an already-written file, including one written by another process.

    Opened read-write on purpose: Windows refuses to flush a handle that has no
    write access, so the previous ``open('rb')`` + ``os.fsync`` raised
    ``OSError: [Errno 9] Bad file descriptor`` there while working on Linux.
    """
    with open(str(path), "rb+") as handle:
        os.fsync(handle.fileno())


def durable_write(path, data: bytes) -> Path:
    """Replace a file's contents, flushing both the file and its directory."""
    target = Path(path)
    with target.open("wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    sync_directory(target.parent)
    return target


def durable_new(path, data: bytes) -> Path:
    """Create a file that must not already exist, then flush it.

    ``'xb'`` makes a second writer fail loudly instead of silently winning a
    race over a job claim or an audit artefact.
    """
    target = Path(path)
    with target.open("xb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    sync_directory(target.parent)
    return target
