"""Shared helpers for the host test suite.

Why this module exists
----------------------
Several tests need a POSIX C toolchain. On Linux that is the local compiler;
on Windows it is reached through WSL. Before this module, each test hard-coded
one developer's WSL distribution name and Linux user name, so the suite only
ran on a single machine. Everything machine-specific now lives here and is
discovered at run time, with environment variables as the override:

    MIXOS_WSL_DISTRO   WSL distribution to use (default: auto-detected)
    MIXOS_WSL_USER     Linux user inside that distribution (default: its own)
    MIXOS_HOST_CC      Compiler name or path (default: cc)
    MIXOS_PROJECT_ROOT Repository root (default: derived from this file)

Nothing here talks to a board, a serial port or a network.
"""
from __future__ import annotations

import functools
import os
import shutil
import subprocess
import sys
from pathlib import Path

__all__ = [
    "ROOT",
    "IS_WINDOWS",
    "wsl_path",
    "posix_path",
    "host_command",
    "host_run",
    "host_cc_available",
    "require_host_cc",
    "scratch_dir",
]

ROOT = Path(os.environ.get("MIXOS_PROJECT_ROOT", Path(__file__).resolve().parents[1]))
IS_WINDOWS = os.name == "nt"

_CC = os.environ.get("MIXOS_HOST_CC", "cc")


def wsl_path(path) -> str:
    """Translate ``D:\\dir\\file`` to ``/mnt/d/dir/file``.

    Paths that are already POSIX are returned unchanged, so callers may pass
    either flavour without checking the platform first.
    """
    text = str(path)
    if len(text) > 1 and text[1] == ":":
        return "/mnt/" + text[0].lower() + text[2:].replace("\\", "/")
    return text.replace("\\", "/")


def posix_path(path) -> str:
    """Path as the C toolchain will see it, on whichever platform we are."""
    return wsl_path(path) if IS_WINDOWS else str(path)


@functools.lru_cache(maxsize=1)
def _wsl_distro() -> str | None:
    """Pick the WSL distribution to build in.

    An explicit MIXOS_WSL_DISTRO always wins. Otherwise we ask ``wsl.exe`` for
    the installed distributions and take the default one, which is what a
    plain ``wsl.exe -- cc`` would have used anyway. Returning the name
    explicitly keeps the compiler invocation reproducible in failure messages.
    """
    override = os.environ.get("MIXOS_WSL_DISTRO")
    if override:
        return override
    if not IS_WINDOWS or not shutil.which("wsl.exe"):
        return None
    try:
        # wsl.exe writes UTF-16LE to a pipe.
        raw = subprocess.run(["wsl.exe", "--list", "--quiet"],
                             capture_output=True, timeout=30).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    names = [line.strip() for line in raw.decode("utf-16-le", "ignore").splitlines()]
    names = [name for name in names if name]
    return names[0] if names else None


def host_command(args: list[str]) -> list[str]:
    """Wrap a POSIX command so it runs on this machine.

    On Linux the command is returned unchanged. On Windows it is prefixed with
    the WSL launcher, including ``-u`` only when MIXOS_WSL_USER asks for a
    specific account.
    """
    if not IS_WINDOWS:
        return args
    distro = _wsl_distro()
    if distro is None:
        raise RuntimeError("no WSL distribution available; set MIXOS_WSL_DISTRO")
    prefix = ["wsl.exe", "-d", distro]
    user = os.environ.get("MIXOS_WSL_USER")
    if user:
        prefix += ["-u", user]
    return [*prefix, "--", *args]


def host_run(args: list[str], *, check: bool = True) -> str:
    """Run a POSIX command and return its combined output.

    GCC quotes identifiers with U+2018/U+2019, which the Windows ANSI code page
    cannot decode. Reading as UTF-8 with replacement keeps the diagnostic
    readable instead of losing it to a UnicodeDecodeError.
    """
    full = host_command(args)
    result = subprocess.run(full, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, encoding="utf-8", errors="replace")
    if check and result.returncode:
        raise AssertionError(
            f"Command failed ({result.returncode}): {' '.join(full)}\n{result.stdout}")
    return result.stdout


@functools.lru_cache(maxsize=1)
def host_cc_available() -> bool:
    """True when a POSIX C compiler can actually be invoked from here."""
    if not IS_WINDOWS:
        return shutil.which(_CC) is not None
    if _wsl_distro() is None:
        return False
    try:
        probe = subprocess.run(host_command([_CC, "--version"]),
                               capture_output=True, timeout=120)
    except (OSError, subprocess.SubprocessError, RuntimeError):
        return False
    return probe.returncode == 0


def require_host_cc() -> str:
    """Return the compiler name, or raise SkipTest with an actionable message."""
    import unittest

    if not host_cc_available():
        if IS_WINDOWS:
            raise unittest.SkipTest(
                "no POSIX C compiler reachable; install WSL with build-essential, "
                "or set MIXOS_WSL_DISTRO / MIXOS_WSL_USER / MIXOS_HOST_CC")
        raise unittest.SkipTest(f"{_CC} not found; set MIXOS_HOST_CC")
    return _CC


def scratch_dir(name: str):
    """A temporary directory that is removed when the context exits.

    Tests used to write fixtures into the tracked ``build/scratch`` folder,
    which left artefacts behind and let one test observe another's leftovers.
    """
    import tempfile

    return tempfile.TemporaryDirectory(prefix=f"mixos-{name}-")


if str(ROOT / "tools") not in sys.path:
    sys.path.insert(0, str(ROOT / "tools"))
if str(ROOT / "linux") not in sys.path:
    sys.path.insert(0, str(ROOT / "linux"))
