"""The one description of the vendored ESP-IDF toolchain layout.

Before this module the same set of IDF_PATH / IDF_TOOLS_PATH / PATH exports was
written out three times — in tools/build_esp_local.sh, in the ESP build driver
and again inside a documentation code block — each with the same absolute path
``/mnt/d/TheEndDEvice/MixOS`` and the same toolchain date stamp baked in.
Upgrading the toolchain meant editing three places, two of them executable and
one of them prose, with nothing keeping them in step.

Everything is now derived from the repository location, and the pinned
versions appear exactly once, below.

The toolchain runs under Linux. On Windows the callers reach it through WSL,
so every path this module produces is a POSIX path.
"""
from __future__ import annotations

import os
from pathlib import Path

# --- Pinned versions. Changing the toolchain means changing these four lines.
IDF_VERSION = "5.4.2"
XTENSA_TOOLCHAIN = "esp-14.2.0_20241119"
CMAKE_VERSION = "3.30.2"
NINJA_VERSION = "1.12.1"
ESP_ROM_ELFS = "20241011"
PYTHON_ENV = "idf5.4_py3.10_env"

ROOT = Path(os.environ.get("MIXOS_PROJECT_ROOT", Path(__file__).resolve().parents[1]))
PROJECT_SUBDIR = "firmware/esp32s3"


def to_posix(path) -> str:
    """Render a path the way the Linux side of the build will see it."""
    text = str(path)
    if len(text) > 1 and text[1] == ":":
        return "/mnt/" + text[0].lower() + text[2:].replace("\\", "/")
    return text.replace("\\", "/")


def idf_paths(root: Path | None = None) -> dict[str, str]:
    """POSIX paths of every component of the vendored toolchain."""
    base = to_posix(root if root is not None else ROOT)
    tools = f"{base}/.tools/idf-tools"
    return {
        "base": base,
        "idf_path": f"{base}/.tools/esp-idf-clean",
        "tools_path": tools,
        "python_env": f"{tools}/python_env/{PYTHON_ENV}",
        "python": f"{tools}/python_env/{PYTHON_ENV}/bin/python",
        "compiler_bin": f"{tools}/tools/xtensa-esp-elf/{XTENSA_TOOLCHAIN}/xtensa-esp-elf/bin",
        "cmake_bin": f"{tools}/tools/cmake/{CMAKE_VERSION}/bin",
        "ninja_bin": f"{tools}/tools/ninja/{NINJA_VERSION}",
        "rom_elfs": f"{tools}/tools/esp-rom-elfs/{ESP_ROM_ELFS}",
        "project": f"{base}/{PROJECT_SUBDIR}",
    }


def environment(root: Path | None = None) -> dict[str, str]:
    """Environment variables the IDF build needs, without inheriting anything."""
    p = idf_paths(root)
    return {
        "IDF_PATH": p["idf_path"],
        "IDF_TOOLS_PATH": p["tools_path"],
        "IDF_PYTHON_ENV_PATH": p["python_env"],
        "ESP_ROM_ELF_DIR": p["rom_elfs"],
        # The vendored checkout has no populated submodules and does not need
        # them for this project; the check otherwise fails the build outright.
        "IDF_SKIP_CHECK_SUBMODULES": "1",
    }


def path_prefix(root: Path | None = None) -> str:
    """Directories that must precede the system PATH for the IDF build."""
    p = idf_paths(root)
    return ":".join([p["python_env"] + "/bin", p["compiler_bin"],
                     p["cmake_bin"], p["ninja_bin"]])


def bash_command(argv: list[str], root: Path | None = None, cwd: str | None = None) -> str:
    """A single ``bash -lc`` string that runs argv inside the IDF environment."""
    import shlex

    p = idf_paths(root)
    exports = " ".join(f"{k}={shlex.quote(v)}" for k, v in environment(root).items())
    directory = cwd if cwd is not None else p["project"]
    return (f"export {exports} && "
            f"export PATH={shlex.quote(path_prefix(root))}:$PATH && "
            f"cd {shlex.quote(directory)} && "
            + " ".join(shlex.quote(a) for a in argv))


def build_command(root: Path | None = None) -> str:
    """The bash command that builds the ESP32-S3 application."""
    p = idf_paths(root)
    return bash_command([p["python"], f"{p['idf_path']}/tools/idf.py", "build"], root)


def describe() -> str:
    p = idf_paths()
    lines = [f"ESP-IDF {IDF_VERSION} (vendored)", f"  project    {p['project']}"]
    lines += [f"  {k:<10} {v}" for k, v in p.items() if k not in ("base", "project")]
    return "\n".join(lines)


def shell_exports(root: Path | None = None) -> str:
    """Lines a POSIX shell can ``eval`` to enter the IDF environment.

    tools/build_esp_local.sh consumes this so that the shell script and the
    Python callers share one definition instead of two copies.
    """
    import shlex

    lines = [f"export {k}={shlex.quote(v)}" for k, v in environment(root).items()]
    lines.append(f'export PATH={shlex.quote(path_prefix(root))}:"$PATH"')
    return "\n".join(lines)


if __name__ == "__main__":
    import sys

    if "--shell" in sys.argv[1:]:
        print(shell_exports())
    else:
        print(describe())

