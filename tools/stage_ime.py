#!/usr/bin/env python3
"""Collect the term-ime source tree here, where GitHub is partly reachable.

``term-ime`` is the virtual terminal that gives the notes editor Chinese input.
The project publishes prebuilt binaries for ``linux-x86_64`` only, and the deck
is an aarch64 Compute Module, so the binary has to be compiled on the device.
That build needs the complete source tree, and getting it there is the problem
this tool solves.

**A recursive git clone cannot be used, on either machine.** Measured
2026-09-14 from both the Windows host and the CM5: ``github.com:443`` does not
answer, while ``codeload.github.com``, ``raw.githubusercontent.com`` and
``api.github.com`` all answer normally. Git needs ``github.com``; tarballs come
from ``codeload``. So every repository is fetched as a tarball at a pinned
commit and unpacked into the place a clone would have put it.

The pins in ``PINS`` are the gitlinks of ``adam-ikari/term-ime`` at ``v1.0.9``
and of ``adam-ikari/librime`` at the commit that tag points to. They are written
out rather than discovered at run time, so a build is reproducible and a change
upstream is visible as a diff here. ``--verify-pins`` re-reads them from the
GitHub API and reports any that moved; it is a check, not an update.

``leveldb/third_party`` is deliberately not fetched. Those two submodules are
GoogleTest and Google Benchmark, and ``deps/librime/deps/leveldb`` is configured
with ``LEVELDB_BUILD_TESTS=OFF`` and ``LEVELDB_BUILD_BENCHMARKS=OFF``, which is
what guards the ``add_subdirectory`` calls that would need them. Fetching them
would add about 10 MB to a transfer that runs at 0.20 MB/s.

Six of librime's files are edited, and the manifest records the digest of each
before and after. They use ``std::any_of`` and five other names from
``<algorithm>`` without including it, which compiled on the distributions
upstream builds on and does not compile with GCC 14 on Debian 13. Adding the
include is the whole change; ``--scan-includes`` is how the six were found and
is what to re-run after moving a pin.

term-ime's own sources are edited for defects with visible consequences.
``Renderer::init`` enables raw mode by clearing three ``c_lflag`` bits and leaves
``IXON`` set, so the line discipline consumes Ctrl-S and Ctrl-Q as XOFF and XON
and no program running inside term-ime ever receives them. In the notes editor
those are Save and Back. ``App::on_input`` drops control bytes while a
syllable is pending, which makes those same command keys dead from the first
pinyin letter until the composition ends. And ``App::init`` spawns the child on
a pseudo-terminal whose size is the 24x80 literal in ``Pty::spawn`` and never
corrects it, so on this device - a 64x22 screen with the bottom row taken by the
candidate bar - every full-screen program inside term-ime draws for a terminal
16 columns too wide and 3 rows too tall. ``SOURCE_EDITS`` carries those fixes,
plus a CSI u Shift+Space decoder and a launcher-scoped hint. The keyboard emits
only a local action; the firmware owner forwards CSI 32;2u only to an allowed
notes session. Without term-ime the Python toolkit consumes that named key,
so it never turns into Ctrl+A or an inserted space. The notes launcher declares
``MIXOS_IME_SHORTCUT=Shift+Space`` only under ``TERM=mixos``; other terminals
retain the upstream Ctrl+A then Space hint and shortcut.

The result is one deterministic tarball plus a manifest:

    build/ime/term-ime-src.tar.gz     what tools/build_ime_on_pi.py sends
    build/ime/manifest.json           every pin, every digest, the archive digest

    py -3.12 tools/stage_ime.py
    py -3.12 tools/stage_ime.py --verify        # re-hash what is already here
    py -3.12 tools/stage_ime.py --verify-pins   # has upstream moved?
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import shutil
import sys
import tarfile
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEST = ROOT / 'build/ime'
CACHE = DEST / 'cache'
TREE = DEST / 'src'
ARCHIVE = DEST / 'term-ime-src.tar.gz'
MANIFEST = DEST / 'manifest.json'

RELEASE = 'v1.0.9'
CODELOAD = 'https://codeload.github.com/{repo}/tar.gz/{commit}'
API_CONTENTS = 'https://api.github.com/repos/{repo}/contents/{path}?ref={ref}'

# Destination in the unpacked tree -> (GitHub repository, pinned commit).
# '' is term-ime itself. Every other entry is a submodule gitlink; the four
# under deps/librime/deps are librime's own, which is why a non-recursive clone
# of term-ime does not build.
PINS: dict[str, tuple[str, str]] = {
    '': ('adam-ikari/term-ime', '1fcb7ae2eab77e1f40947bcb7ac675d49255b3f8'),
    'deps/ftxui': ('arthursonzogni/ftxui', '5cfed50702f52d51c1b189b5f97f8beaf5eaa2a6'),
    'deps/spdlog': ('gabime/spdlog', '8e5613379f5140fefb0b60412fbf1f5406e7c7f8'),
    'deps/json': ('nlohmann/json', '9cca280a4d0ccf0c08f47a99aa71d1b0e52f8d03'),
    'deps/googletest': ('google/googletest', 'f8d7d77c06936315286eb55f8de22cd23c188571'),
    'deps/sml': ('boost-ext/sml', '2e228bbe440cd8a654186a2caeb3f27e0b4ecee6'),
    'deps/utf8proc': ('JuliaStrings/utf8proc', '26dbf597ffa8167ef5edcc7f2da905500a5aa3c2'),
    'deps/libuv': ('libuv/libuv', '74c1dcb8455d735d3ffbb1515ba78661621d5cf5'),
    'deps/librime': ('adam-ikari/librime', '1d7c2618bbaaa28f1987d57a0d26f33921f391be'),
    'deps/librime/deps/glog': ('google/glog', '7b134a5c82c0c0b5698bb6bf7a835b230c5638e4'),
    'deps/librime/deps/googletest': ('google/googletest',
                                     'f8d7d77c06936315286eb55f8de22cd23c188571'),
    'deps/librime/deps/leveldb': ('google/leveldb',
                                  '99b3c03b3284f5886f9ef9a4ef703d57373e61be'),
    'deps/librime/deps/marisa-trie': ('s-yata/marisa-trie',
                                      '3e87d53b78e15f2f43783d5e376561a8c9722051'),
    'deps/librime/deps/opencc': ('BYVoid/OpenCC',
                                 '556ed22496d650bd0b13b6c163be9814637970ae'),
    'deps/librime/deps/yaml-cpp': ('jbeder/yaml-cpp',
                                   '2f86d13775d119edbb69af52e5f566fd65c6953b'),
}

# Where each pin is recorded upstream, for --verify-pins. The key is the pin
# path here; the value is (repository holding the gitlink, ref, directory).
PIN_SOURCES: dict[str, tuple[str, str, str]] = {
    path: (('adam-ikari/librime', PINS['deps/librime'][1], 'deps')
           if path.startswith('deps/librime/deps/')
           else ('adam-ikari/term-ime', RELEASE, 'deps'))
    for path in PINS if path
}

# Upstream files this tree cannot compile without a change, and the smallest
# change that fixes each: one missing standard include.
#
# GCC 14's libstdc++ stopped pulling <algorithm> in through other headers.
# librime uses std::any_of, std::upper_bound, std::all_of, std::partial_sort,
# std::find and std::stable_sort without including it, so on Debian 13 the build
# stops at the first of them with "'any_of' is not a member of 'std'". Upstream
# builds on distributions whose libstdc++ still leaked the include; nothing about
# the code is aarch64-specific.
#
# The list was not guessed. ``--scan-includes`` reads every translation unit in
# the staged tree and reports each name used from a header the file does not
# include, which is how these six were found in one pass instead of one rebuild
# each. Re-run it after moving any pin.
#
# The insertion point is the first ``#include`` in the file, which is inside the
# include guard where there is one. Each entry also names the symbol that makes
# the include necessary, and staging fails rather than guesses if that symbol is
# no longer there.
MISSING_INCLUDES: dict[str, tuple[str, str]] = {
    'deps/librime/src/rime/segmentation.h': ('algorithm', 'any_of'),
    'deps/librime/src/rime/config/config_compiler.cc': ('algorithm', 'upper_bound'),
    'deps/librime/src/rime/config/config_data.cc': ('algorithm', 'all_of'),
    'deps/librime/src/rime/dict/dictionary.cc': ('algorithm', 'partial_sort'),
    'deps/librime/src/rime/gear/chord_composer.cc': ('algorithm', 'find'),
    'deps/librime/src/rime/gear/schema_list_translator.cc': ('algorithm', 'stable_sort'),
}

# Names that live in these headers, for --scan-includes. Not exhaustive; it is
# the set that a C++ project realistically reaches for without noticing.
STANDARD_NAMES: dict[str, tuple[str, ...]] = {
    'algorithm': (
        'any_of', 'all_of', 'none_of', 'for_each', 'find', 'find_if',
        'find_if_not', 'count', 'count_if', 'copy', 'copy_if', 'copy_n',
        'transform', 'remove', 'remove_if', 'replace', 'reverse', 'rotate',
        'unique', 'sort', 'stable_sort', 'partial_sort', 'nth_element',
        'lower_bound', 'upper_bound', 'binary_search', 'merge', 'includes',
        'min_element', 'max_element', 'minmax_element', 'clamp', 'swap_ranges',
        'fill', 'fill_n', 'generate', 'shuffle', 'partition', 'stable_partition',
        'is_sorted', 'equal', 'mismatch', 'set_difference', 'set_union',
        'set_intersection'),
    'numeric': ('accumulate', 'inner_product', 'partial_sum', 'iota', 'reduce',
                'transform_reduce', 'gcd', 'lcm'),
}
SCAN_SUFFIXES = ('.h', '.hpp', '.hxx', '.cc', '.cpp', '.ipp')
# Directories whose contents are never compiled into term-ime.
SCAN_SKIP = ('/test/', '/tests/', '/bindings/', '/doc/', '/docs/', '/sample/',
             '/googletest/', '/benchmark/', '/third_party/', '/example/',
             '/examples/')

# Exact replacements in upstream source, each with the reason it is necessary
# and the text it must match. Staging fails if the text is not found, so a pin
# that moves cannot silently drop a fix.
#
# The original entries fix defects with visible consequences on this device.
# Keyboard/hint patches follow them and are equally reproducible on re-stage.
#
# ``Renderer::init`` puts term-ime's controlling terminal into "raw mode" by
# clearing three ``c_lflag`` bits and leaves ``c_iflag`` alone, so ``IXON``
# stays on. XON/XOFF flow control then consumes Ctrl-S and Ctrl-Q in the line
# discipline: they never reach term-ime's ``read``, so term-ime never forwards
# them, so the program inside it never sees them. In the notes editor those two
# keys are Save and Back — the editor works, and its two most-used commands do
# nothing. A terminal emulator must not leave software flow control enabled on
# the terminal it is emulating.
#
# ``App::on_input`` drops every key that is not part of the composition while
# a syllable is pending: escape sequences, digits above nine, punctuation and
# all control bytes. The notes editor's commands are control bytes — Save,
# Back, Record, the recognition-language switch — so from the first pinyin
# letter until the composition is committed or cancelled, every command key is
# dead. Cancelling the syllable and forwarding the byte is what ESC already
# does with the composition, extended to the keys a person actually reaches
# for.
SOURCE_EDITS: list[dict] = [
    {
        'path': 'src/ui/renderer.cpp',
        'find': '    raw.c_lflag &= ~(ICANON | ECHO | ISIG);\n',
        'replace': '    raw.c_lflag &= ~(ICANON | ECHO | ISIG);\n'
                   '    // Software flow control has to go too. Left on, the line\n'
                   '    // discipline eats Ctrl-S as XOFF and Ctrl-Q as XON, so no\n'
                   '    // program running inside term-ime can ever receive them.\n'
                   '    raw.c_iflag &= ~(IXON | IXOFF | IXANY);\n',
        'why': 'Ctrl-S and Ctrl-Q are Save and Back in the notes editor; with '
               'IXON left on they are consumed as flow control and never arrive',
    },
    {
        'path': 'src/core/app.cpp',
        'find': '            // Other keys are ignored while composing\n'
                '            spdlog::debug("IME composing: ignoring key 0x{:02x}", byte);\n'
                '            continue;\n',
        'replace': '            // A control key with a syllable pending is still a\n'
                   '            // command: in the child those bytes are Save, Back,\n'
                   '            // Record and Quit, and dropping them here made them\n'
                   '            // dead until the composition ended. Cancel the\n'
                   '            // syllable and forward the byte; what was being\n'
                   '            // typed is discarded, which is what ESC does too.\n'
                   '            if (byte < 0x20) {\n'
                   '                ime_->cancel();\n'
                   '                selected_candidate_ = 0;\n'
                   '                pty_.write(std::vector<uint8_t>{byte});\n'
                   '                render();\n'
                   '                continue;\n'
                   '            }\n'
                   '            // Other keys are ignored while composing\n'
                   '            spdlog::debug("IME composing: ignoring key 0x{:02x}", byte);\n'
                   '            continue;\n',
        'why': 'While a syllable is pending the composing branch swallows every '
               'control byte, so Ctrl-R, Ctrl-T, Ctrl-S and Ctrl-Q are dead '
               'mid-composition; cancel the syllable and forward the key',
    },
    {
        'path': 'src/core/app.cpp',
        'find': '        // Create screen and parser\n'
                '        spdlog::info("Creating screen {}x{}", ws.ws_row - 1, ws.ws_col);\n',
        'replace': '        // Tell the child how big its terminal actually is.\n'
                   '        // Pty::spawn passes forkpty a literal 24x80, and the only\n'
                   '        // call that corrects it is in App::on_resize - which runs\n'
                   '        // on SIGWINCH, and no SIGWINCH arrives on a screen that\n'
                   '        // never changes size. The child therefore believed it had\n'
                   '        // 24 rows and 80 columns on a 64x22 device: a full-screen\n'
                   '        // program wrapped every line 16 columns early and drew\n'
                   '        // three rows past the bottom, scrolling its own header off\n'
                   '        // and landing its footer on the candidate bar.\n'
                   '        // The row count matches Screen below: the last row belongs\n'
                   '        // to the candidate bar, not to the child.\n'
                   '        pty_.resize(ws.ws_row - 1, ws.ws_col);\n'
                   '\n'
                   '        // Create screen and parser\n'
                   '        spdlog::info("Creating screen {}x{}", ws.ws_row - 1, ws.ws_col);\n',
        'why': 'the child pseudo-terminal keeps the 24x80 literal from '
               'Pty::spawn unless a SIGWINCH arrives, so on this fixed-size '
               "screen every full-screen program inside term-ime draws for the "
               'wrong grid',
    },
    {
        'path': 'src/core/input_processor.cpp',
        'find': '    sm_.process_event(event);\n'
                '    return result;\n',
        'replace': '    sm_.process_event(event);\n'
                   '    // CSI u: Unicode Space (32), Shift (2). Consume the entire key before\n'
                   '    // composition or the child PTY sees it; Ctrl+A + Space remains supported.\n'
                   "    if (result.forward && result.data == std::vector<uint8_t>{0x1b, '[', '3', '2', ';', '2', 'u'}) {\n"
                   '        result.data.clear();\n'
                   '        result.forward = false;\n'
                   '        result.toggle_mode = true;\n'
                   '    }\n'
                   '    return result;\n',
        'why': 'Decode Shift+Space CSI 32;2u as one mode-toggle event, including '
               'split reads, without forwarding Ctrl+A or Space to the child; '
               'the upstream Ctrl+A then Space shortcut is unchanged',
    },
    {
        'path': 'src/ui/components.cpp',
        'find': '#include <ftxui/dom/elements.hpp>\n',
        'replace': '#include <ftxui/dom/elements.hpp>\n'
                   '#include <cstdlib>\n'
                   '#include <cstring>\n',
        'why': 'Declare std::getenv and std::strcmp for the MixOS-only mode hint',
    },
    {
        'path': 'src/ui/components.cpp',
        'find': 'Element HintsBar() {\n'
                '    return HBox({HintItem({.key = "^A Space", .action = I18n::t("hint.toggle_mode")}),\n',
        'replace': 'static const char* ImeToggleHint() {\n'
                   '    const char* term = std::getenv("TERM");\n'
                   '    const char* shortcut = std::getenv("MIXOS_IME_SHORTCUT");\n'
                   '    // Only the MixOS notes launcher declares the physical keyboard mapping.\n'
                   '    return term && std::strcmp(term, "mixos") == 0 && shortcut &&\n'
                   '                   std::strcmp(shortcut, "Shift+Space") == 0\n'
                   '               ? "Shift+Space" : "^A Space";\n'
                   '}\n'
                   '\n'
                   'Element HintsBar() {\n'
                   '    return HBox({HintItem({.key = ImeToggleHint(), .action = I18n::t("hint.toggle_mode")}),\n',
        'why': 'Advertise Shift+Space only when TERM=mixos and the notes launcher '
               'declares MIXOS_IME_SHORTCUT=Shift+Space; ordinary terminals '
               'retain the upstream ^A Space hint',
    },
]

# Fetched, then dropped before packing, and why. A codeload tarball is the whole
# repository, and four of these directories are 92 MB of documentation and web
# assets that no CMake target reads. The link to the device runs at 0.20 MB/s,
# so leaving them in costs about eight minutes per transfer.
#
# Each entry is justified against the build files, not guessed:
#   * deps/sml is header-only and reached only through SML_INCLUDE_DIR
#     (deps/sml/include). term-ime never calls add_subdirectory on it.
#   * nlohmann_json sets JSON_BuildTests from MAIN_PROJECT, which is false for a
#     subdirectory, so tests/ is never added and docs/ is never read.
#   * website/ is term-ime's marketing site; no CMakeLists mentions it.
PRUNED = {
    'deps/sml/doc': 'sml is used as headers only, via SML_INCLUDE_DIR',
    'deps/json/docs': 'nlohmann_json documentation; no target reads it',
    'deps/json/tests': 'JSON_BuildTests is off for a subdirectory build',
    'website': "term-ime's documentation site, not part of the build",
}

# Not fetched, and why. Printed by --explain so the omission is a decision on
# the record rather than something that looks like an oversight.
OMITTED = {
    'deps/librime/deps/leveldb/third_party/googletest':
        'leveldb is configured with LEVELDB_BUILD_TESTS=OFF, which is what '
        'guards the add_subdirectory that would need it',
    'deps/librime/deps/leveldb/third_party/benchmark':
        'leveldb is configured with LEVELDB_BUILD_BENCHMARKS=OFF, same reason',
    'deps/librime/deps/librime':
        'declared in librime\'s .gitmodules but absent from the tree at the '
        'pinned commit, so a recursive clone does not fetch it either',
}

# A tarball, not a model: github answers a plain urllib agent here, and these
# are tens of megabytes rather than gigabytes.
CHUNK = 1 << 20
# Every file in the archive gets this timestamp so that staging the same pins
# twice produces the same bytes, and the device can tell "already sent" from
# "sent something else".
EPOCH = 1600000000


def digest_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def digest_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open('rb') as handle:
        for block in iter(lambda: handle.read(CHUNK), b''):
            h.update(block)
    return h.hexdigest()


def cache_name(repo: str, commit: str) -> Path:
    return CACHE / f"{repo.replace('/', '__')}-{commit[:12]}.tar.gz"


def fetch(repo: str, commit: str, attempts: int, timeout: int) -> Path:
    """One repository at one commit, kept so a rerun costs nothing."""
    target = cache_name(repo, commit)
    if target.is_file() and target.stat().st_size:
        return target
    target.parent.mkdir(parents=True, exist_ok=True)
    url = CODELOAD.format(repo=repo, commit=commit)
    partial = target.with_suffix('.part')
    for attempt in range(1, attempts + 1):
        try:
            with urllib.request.urlopen(url, timeout=timeout) as response, \
                 partial.open('wb') as handle:
                while True:
                    block = response.read(CHUNK)
                    if not block:
                        break
                    handle.write(block)
            partial.replace(target)
            return target
        except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
            partial.unlink(missing_ok=True)
            print(f'    attempt {attempt}/{attempts} failed: {exc}')
            time.sleep(min(3 * attempt, 20))
    raise RuntimeError(f'Could not fetch {repo}@{commit[:12]} from codeload')


def unpack(tarball: Path, destination: Path) -> int:
    """Unpack a codeload tarball, dropping its ``<repo>-<commit>/`` wrapper."""
    destination.mkdir(parents=True, exist_ok=True)
    written = 0
    with tarfile.open(tarball, 'r:gz') as archive:
        for member in archive:
            parts = Path(member.name).parts
            if len(parts) < 2:
                continue
            relative = Path(*parts[1:])
            if '..' in relative.parts or relative.is_absolute():
                raise RuntimeError(f'{tarball.name} contains {member.name}')
            out = destination / relative
            if member.isdir():
                out.mkdir(parents=True, exist_ok=True)
                continue
            if member.issym() or member.islnk():
                # Only source trees are expected here; a link out of the tree
                # would be unpacked as a file by accident on Windows anyway.
                continue
            if not member.isfile():
                continue
            out.parent.mkdir(parents=True, exist_ok=True)
            source = archive.extractfile(member)
            if source is None:
                continue
            with out.open('wb') as handle:
                shutil.copyfileobj(source, handle, CHUNK)
            written += 1
    return written


def build_tree(attempts: int, timeout: int) -> list[dict]:
    if TREE.exists():
        shutil.rmtree(TREE)
    entries = []
    for index, (path, (repo, commit)) in enumerate(PINS.items(), 1):
        label = path or '(term-ime)'
        print(f'[{index}/{len(PINS)}] {label:<34} {repo}@{commit[:8]}', flush=True)
        tarball = fetch(repo, commit, attempts, timeout)
        files = unpack(tarball, TREE / path if path else TREE)
        entries.append({'path': path, 'repo': repo, 'commit': commit,
                        'tarball_bytes': tarball.stat().st_size,
                        'tarball_sha256': digest_file(tarball),
                        'files': files})
        print(f'      {files:,} files, {tarball.stat().st_size / 1e6:,.1f} MB tarball')
    return entries


def add_missing_includes() -> list[dict]:
    """Insert the standard includes GCC 14 no longer supplies by accident.

    Each edit is recorded with the digest before and after, so the manifest says
    exactly which upstream bytes were changed and the change can be checked
    without re-reading this code.
    """
    applied = []
    for relative, (header, symbol) in MISSING_INCLUDES.items():
        path = TREE / relative
        if not path.is_file():
            raise RuntimeError(f'{relative} is not in the staged tree; a pin moved '
                               f'and MISSING_INCLUDES needs revisiting.')
        original = path.read_text(encoding='utf-8')
        before = digest_bytes(original.encode())
        directive = f'#include <{header}>'
        if directive in original:
            applied.append({'path': relative, 'header': header, 'symbol': symbol,
                            'state': 'already present upstream', 'sha256': before})
            print(f'      {relative}: already includes <{header}>')
            continue
        if f'std::{symbol}' not in original:
            raise RuntimeError(f'{relative} no longer uses std::{symbol}, so the '
                               f'reason for adding <{header}> is gone. Re-run '
                               f'--scan-includes and update MISSING_INCLUDES.')
        lines = original.splitlines(keepends=True)
        index = next((n for n, line in enumerate(lines)
                      if line.lstrip().startswith('#include')), None)
        if index is None:
            raise RuntimeError(f'{relative} has no #include to insert before.')
        lines.insert(index, directive + '\n')
        patched = ''.join(lines)
        path.write_text(patched, encoding='utf-8', newline='')
        applied.append({'path': relative, 'header': header, 'symbol': symbol,
                        'state': 'added', 'line': index + 1,
                        'sha256_before': before,
                        'sha256_after': digest_bytes(patched.encode())})
        print(f'      {relative}: added <{header}> for std::{symbol}')
    return applied


def scan_includes(tree: Path) -> int:
    """Report names used from a standard header the file does not include."""
    findings = 0
    for path in sorted(tree.rglob('*')):
        if path.suffix not in SCAN_SUFFIXES:
            continue
        posix = path.as_posix()
        if any(part in posix for part in SCAN_SKIP):
            continue
        try:
            text = path.read_text(encoding='utf-8', errors='replace')
        except OSError:
            continue
        included = set(re.findall(r'#\s*include\s*<([A-Za-z_0-9/.]+)>', text))
        for header, names in STANDARD_NAMES.items():
            if header in included:
                continue
            used = sorted({name for name in names
                           if re.search(r'\bstd::' + name + r'\s*[(<]', text)})
            if used:
                findings += 1
                print(f'{path.relative_to(tree).as_posix()}\n'
                      f'    no <{header}> for std::' + ', std::'.join(used))
    print(f'\n{findings} file(s) use a name from a header they do not include.')
    return findings


def apply_source_edits() -> list[dict]:
    """Apply the exact replacements in SOURCE_EDITS, recording every digest."""
    applied = []
    for edit in SOURCE_EDITS:
        path = TREE / edit['path']
        if not path.is_file():
            raise RuntimeError(f"{edit['path']} is not in the staged tree; a pin "
                               f'moved and SOURCE_EDITS needs revisiting.')
        original = path.read_text(encoding='utf-8')
        before = digest_bytes(original.encode())
        if edit['replace'] in original:
            applied.append({'path': edit['path'], 'why': edit['why'],
                            'state': 'already applied', 'sha256': before})
            print(f"      {edit['path']}: already carries the change")
            continue
        count = original.count(edit['find'])
        if count != 1:
            raise RuntimeError(f"{edit['path']}: the text to replace appears "
                               f'{count} times, expected exactly once. Upstream '
                               f'changed; re-check the edit before staging.')
        patched = original.replace(edit['find'], edit['replace'])
        path.write_text(patched, encoding='utf-8', newline='')
        applied.append({'path': edit['path'], 'why': edit['why'], 'state': 'applied',
                        'sha256_before': before,
                        'sha256_after': digest_bytes(patched.encode())})
        print(f"      {edit['path']}: {edit['why']}")
    return applied


def prune() -> list[dict]:
    """Remove the documentation and web assets no CMake target reads."""
    removed = []
    for path, why in PRUNED.items():
        target = TREE / path
        if not target.is_dir():
            # A pruned path that vanished upstream is worth saying out loud:
            # the reason recorded next to it no longer describes anything.
            removed.append({'path': path, 'reason': why, 'bytes': 0,
                            'state': 'absent upstream'})
            continue
        size = sum(p.stat().st_size for p in target.rglob('*') if p.is_file())
        shutil.rmtree(target)
        removed.append({'path': path, 'reason': why, 'bytes': size,
                        'state': 'removed'})
        print(f'      dropped {path} ({size / 1e6:,.1f} MB): {why}')
    return removed


def pack() -> tuple[int, str]:
    """One deterministic gzip tar of the whole tree, rooted at ``term-ime/``."""
    files = sorted(p for p in TREE.rglob('*') if p.is_file())
    buffer = io.BytesIO()
    # mtime=0 in GzipFile terms: tarfile's gzip wrapper would otherwise stamp
    # the current time into the header and change the digest on every run.
    with tarfile.open(fileobj=buffer, mode='w:gz', compresslevel=6,
                      format=tarfile.GNU_FORMAT) as archive:
        archive.gzip = None  # type: ignore[attr-defined]
        for path in files:
            data = path.read_bytes()
            info = tarfile.TarInfo('term-ime/' + path.relative_to(TREE).as_posix())
            info.size, info.mtime = len(data), EPOCH
            info.mode = 0o755 if data.startswith(b'#!') else 0o644
            info.uid = info.gid = 0
            info.uname = info.gname = 'root'
            archive.addfile(info, io.BytesIO(data))
    raw = buffer.getvalue()
    # Rewrite the 4-byte mtime in the gzip header so the archive is byte-stable.
    raw = raw[:4] + (0).to_bytes(4, 'little') + raw[8:]
    ARCHIVE.parent.mkdir(parents=True, exist_ok=True)
    ARCHIVE.write_bytes(raw)
    return len(files), digest_bytes(raw)


def api_json(url: str, timeout: int) -> object:
    request = urllib.request.Request(url, headers={
        'Accept': 'application/vnd.github+json',
        'User-Agent': 'mixos-stage-ime'})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode('utf-8'))


def verify_pins(timeout: int) -> int:
    """Ask GitHub what the gitlinks are now. Reports drift; changes nothing."""
    wanted: dict[tuple[str, str, str], dict[str, str]] = {}
    for path, source in PIN_SOURCES.items():
        wanted.setdefault(source, {})[Path(path).name] = PINS[path][1]

    drift = 0
    for (repo, ref, directory), expected in sorted(wanted.items()):
        print(f'{repo}@{ref}:{directory}')
        try:
            listing = api_json(API_CONTENTS.format(repo=repo, path=directory, ref=ref),
                               timeout)
        except (urllib.error.URLError, TimeoutError, ValueError) as exc:
            print(f'  could not read: {exc}')
            drift += 1
            continue
        actual = {item['name']: item.get('sha', '')
                  for item in listing if isinstance(item, dict)}
        for name, commit in sorted(expected.items()):
            found = actual.get(name)
            if found == commit:
                print(f'  {name:<16} {commit[:12]} unchanged')
            else:
                print(f'  {name:<16} pinned {commit[:12]}, upstream '
                      f'{(found or "absent")[:12]}  <-- moved')
                drift += 1
    print('\nEvery pin matches upstream.' if not drift else
          f'\n{drift} pin(s) differ from upstream. Update PINS deliberately, '
          'then re-stage and rebuild; do not edit the manifest.')
    return 1 if drift else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--verify', action='store_true',
                        help='re-hash the staged archive against the manifest; '
                             'download nothing')
    parser.add_argument('--verify-pins', action='store_true',
                        help='ask GitHub whether any pinned gitlink moved')
    parser.add_argument('--scan-includes', action='store_true',
                        help='report every staged file that uses a standard name '
                             'from a header it does not include; this is how '
                             'MISSING_INCLUDES was found')
    parser.add_argument('--explain', action='store_true',
                        help='list what is fetched and what is deliberately not')
    parser.add_argument('--timeout', type=int, default=60)
    parser.add_argument('--attempts', type=int, default=6)
    args = parser.parse_args()

    if args.explain:
        print(f'term-ime {RELEASE}, fetched as tarballs because github.com:443 '
              f'does not answer from here.\n')
        for path, (repo, commit) in PINS.items():
            print(f'  {(path or "(root)"):<38} {repo}@{commit[:12]}')
        print('\nStandard includes added after fetching (GCC 14 no longer '
              'supplies them transitively):')
        for path, (header, symbol) in MISSING_INCLUDES.items():
            print(f'  {path}\n      <{header}> for std::{symbol}')
        print('\nSource fixed after fetching:')
        for edit in SOURCE_EDITS:
            print(f"  {edit['path']}\n      {edit['why']}")
        print('\nFetched, then dropped before packing:')
        for path, why in PRUNED.items():
            print(f'  {path}\n      {why}')
        print('\nDeliberately not fetched:')
        for path, why in OMITTED.items():
            print(f'  {path}\n      {why}')
        return 0

    if args.scan_includes:
        if not TREE.is_dir():
            raise SystemExit('Nothing unpacked yet. Run tools/stage_ime.py first.')
        return 1 if scan_includes(TREE) else 0

    if args.verify_pins:
        return verify_pins(args.timeout)

    if args.verify:
        if not MANIFEST.is_file() or not ARCHIVE.is_file():
            raise SystemExit('Nothing staged yet. Run tools/stage_ime.py first.')
        manifest = json.loads(MANIFEST.read_text(encoding='utf-8'))
        actual = digest_file(ARCHIVE)
        if actual != manifest['archive']['sha256']:
            raise SystemExit(f'{ARCHIVE.name}: on disk {actual}, manifest says '
                             f'{manifest["archive"]["sha256"]}. Re-stage it.')
        print(f'{ARCHIVE.relative_to(ROOT)}  {ARCHIVE.stat().st_size:,} bytes  ok')
        print(f'{len(manifest["repositories"])} pinned repositories, '
              f'{manifest["archive"]["files"]:,} files')
        return 0

    started = time.monotonic()
    DEST.mkdir(parents=True, exist_ok=True)
    entries = build_tree(args.attempts, args.timeout)
    print('\nAdding the standard includes GCC 14 no longer supplies')
    patched = add_missing_includes()
    print('\nApplying the source fixes this device needs')
    edited = apply_source_edits()
    print('\nPruning')
    removed = prune()
    print('\nPacking')
    count, sha = pack()
    MANIFEST.write_text(json.dumps({
        'staged': time.strftime('%Y-%m-%d %H:%M:%S'),
        'release': RELEASE,
        'source': 'codeload.github.com tarballs; github.com:443 unreachable '
                  'from this host and from the device',
        'omitted': OMITTED,
        'pruned': removed,
        'patched': patched,
        'edited': edited,
        'repositories': entries,
        'archive': {'name': ARCHIVE.name, 'files': count,
                    'bytes': ARCHIVE.stat().st_size, 'sha256': sha},
    }, indent=2) + '\n', encoding='utf-8')

    print(f'\n{ARCHIVE.relative_to(ROOT)}  {count:,} files  '
          f'{ARCHIVE.stat().st_size / 1e6:,.1f} MB  sha256 {sha[:16]}…')
    print(f'{MANIFEST.relative_to(ROOT)}  {len(entries)} pinned repositories')
    print(f'{time.monotonic() - started:,.0f}s')
    print('\nBuild it on the device with: '
          'py -3.12 tools/build_ime_remote.py --host 192.168.1.22 --execute')
    return 0


if __name__ == '__main__':
    os.environ.setdefault('PYTHONIOENCODING', 'utf-8')
    sys.exit(main())
