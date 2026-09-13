"""Exclusive locks and operation timeouts, without Linux-only imports at startup.

Three tools each carried their own copy of the same flock-based device lock,
and the copies had already drifted: two opened the lock file with ``'a'`` and
one with ``'w'``, which truncates a lock file another process is holding.

They also each imported ``fcntl`` at module scope. On Windows that turns a
plain ``import flash_esp_on_pi`` into an ImportError, which is why large parts
of the test suite were wrapped in ``if sys.platform == 'linux':`` and silently
skipped on the development machine. Acquiring ``fcntl`` inside the lock
function keeps the rest of each module importable and testable everywhere.
"""
from __future__ import annotations

import os
import sys
from contextlib import contextmanager
from pathlib import Path

__all__ = ["default_lock_path", "device_lock", "operation_timeout", "LockBusy"]


class LockBusy(RuntimeError):
    """Another process already holds the lock."""


def default_lock_path(name: str = "flash.lock") -> Path:
    return Path.home() / ".cache/mixos" / name


@contextmanager
def device_lock(path: Path | None = None, *, name: str = "flash.lock"):
    """Hold an exclusive, non-blocking lock for the duration of the block.

    The lock file is opened for append so that taking the lock never truncates
    a file another process is currently holding open.

    On a platform without ``fcntl`` the lock cannot be honoured, so this raises
    rather than pretending the caller is protected: these tools write flash.
    """
    target = Path(path) if path is not None else default_lock_path(name)
    target.parent.mkdir(parents=True, exist_ok=True)

    try:
        import fcntl
    except ImportError as exc:  # pragma: no cover - not reachable on Linux
        raise RuntimeError(
            f"exclusive device locking needs fcntl, unavailable on {sys.platform}; "
            "run this tool on the Linux host that owns the device") from exc

    with target.open("a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise LockBusy(f"another MixOS job holds {target}") from exc
        try:
            yield handle
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


@contextmanager
def operation_timeout(label: str, seconds: float):
    """Abort the enclosed block if it outlasts ``seconds``.

    Implemented with SIGALRM, which exists only on POSIX. On other platforms
    the block still runs, because the alternative — refusing to run at all —
    would make the surrounding logic untestable off-device. The caller is told
    through the raised message which guarantee it did not get.
    """
    if seconds <= 0:
        yield
        return

    try:
        import signal

        setitimer = signal.setitimer
        alarm_signal = signal.SIGALRM
    except (ImportError, AttributeError):
        yield
        return

    def expire(signum, frame):  # noqa: ARG001
        raise TimeoutError(f"{label} exceeded {seconds:g}s")

    previous = signal.signal(alarm_signal, expire)
    setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        setitimer(signal.ITIMER_REAL, 0)
        signal.signal(alarm_signal, previous)


def timeouts_enforced() -> bool:
    """Whether operation_timeout can actually interrupt a blocked call here."""
    if os.name != "posix":
        return False
    try:
        import signal
    except ImportError:  # pragma: no cover
        return False
    return hasattr(signal, "SIGALRM") and hasattr(signal, "setitimer")
