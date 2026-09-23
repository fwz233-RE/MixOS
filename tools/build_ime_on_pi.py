#!/usr/bin/env python3
"""Build term-ime on the device, install it, and prove that pinyin becomes 汉字.

This runs on the Compute Module, started by ``tools/build_ime_remote.py``. It is
the device half for the same reason the flashing worker is: the build takes tens
of minutes over a Wi-Fi link that drops, so it runs as a transient systemd unit
that outlives the SSH session, and everything it concludes is appended to
``build-audit.jsonl`` next to its log.

Why the build happens here at all. term-ime publishes prebuilt binaries for
``linux-x86_64``; this is aarch64. Nothing cross-compiles it, because it links
librime, opencc, leveldb, marisa, yaml-cpp, libuv, FTXUI and spdlog from source
into one static executable.

Three things about this machine shape the whole procedure.

**4 GiB of RAM, shared with the AI stack.** ``mixos-litertlm`` and
``mixos-aiserver`` hold about 3.2 GB resident between them, leaving 1.3 GB. A
C++ build of this size does not fit in that, so both units are stopped for the
duration and started again afterwards —including when the build fails, which
is why the restore runs from a ``finally``.

**term-ime's CMakeLists builds its vendored deps with a bare ``-j``.** For GNU
make that means unlimited parallelism: one compiler per source file, about 150
of them at once, which on this machine is not slow but fatal. A ``make`` shim
earlier on ``PATH`` rewrites a bare ``-j`` into a bounded one and passes
everything else through. The alternative —reimplementing those four nested
configure commands here —would duplicate upstream's flags and silently rot.

**The compile must not run as root.** The unit is root so that it can stop and
start units and install into ``/usr/local``; every compiler process is run
through ``setpriv`` as the ordinary user, and the source and build trees are
owned by that user.

Proof, not assumption. Installing a binary is not evidence that Chinese input
works, so ``verify`` opens a pseudo-terminal, runs the installed term-ime with
``/bin/cat`` as its child, switches to Chinese with Ctrl-A Space, types
``nihao`` and requires 你好 to appear in the rendered frames and then in the
committed text. A build that cannot do that is reported as a failure even
though it compiled.

    python3 build_ime_on_pi.py build --root ~/mixos-ime --sha256 <digest>
    python3 build_ime_on_pi.py verify --root ~/mixos-ime
"""
from __future__ import annotations

import argparse
import errno
import hashlib
import json
import os
import re
import select
import shutil
import signal
import subprocess
import sys
import tarfile
import time
from pathlib import Path

# pty, termios and fcntl are imported where they are used rather than here.
# This module is also read by the portable test suite, which runs on Windows,
# and those three do not exist there; nothing outside the pseudo-terminal check
# needs them.

# The two units that own the memory this build needs. Order matters on restore:
# the model server is what the API server talks to.
AI_UNITS = ('mixos-litertlm.service', 'mixos-aiserver.service')

PREFIX = '/usr/local'
BINARY = PREFIX + '/bin/term-ime'
SHARED_DATA = PREFIX + '/share/term-ime/rime-data'
# mixosd's --app-dir entry for the notes button: the launcher, not the editor.
LAUNCHER = '/usr/local/lib/mixos/apps/notes'

# What the verification types and what it must see. 'nihao' is the canonical
# two-syllable test: luna_pinyin_simp with essay.txt ranks 你好 first, and a
# missing essay.txt shows up here as rare single characters instead.
PINYIN = b'nihao'
EXPECTED = '你好'
TOGGLE = b'\x01 '                       # Upstream shortcut retained for compatibility
DEVICE_TOGGLE = b'\x1b[32;2u'            # Physical Shift+Space forwarded by MixOS

AUDIT: Path | None = None


def running_as_root() -> bool:
    """True when this process can change user and write to /usr/local.

    ``os.geteuid`` does not exist on Windows, and this module is read by the
    portable test suite, which runs there.
    """
    return hasattr(os, 'geteuid') and os.geteuid() == 0


def audit(event: str, **fields) -> None:
    record = {'at': time.strftime('%Y-%m-%dT%H:%M:%S%z'), 'event': event, **fields}
    line = json.dumps(record, ensure_ascii=False)
    if AUDIT is not None:
        with AUDIT.open('a', encoding='utf-8') as handle:
            handle.write(line + '\n')
            handle.flush()
            os.fsync(handle.fileno())
    print('AUDIT ' + line, flush=True)


def run(command: list[str], *, cwd: Path | None = None, env: dict | None = None,
        timeout: int = 3600, check: bool = True) -> subprocess.CompletedProcess:
    """Run a command, streaming nothing and returning everything."""
    print(f'+ {" ".join(command)}', flush=True)
    result = subprocess.run(command, cwd=str(cwd) if cwd else None, env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            timeout=timeout)
    text = result.stdout.decode('utf-8', 'replace')
    print(text, end='', flush=True)
    if check and result.returncode:
        raise RuntimeError(f'{command[0]} exited {result.returncode}')
    return subprocess.CompletedProcess(command, result.returncode, text, '')


def as_user(user: str, command: list[str]) -> list[str]:
    """The same command, run as the ordinary user with that user's groups.

    Only root can change user, and only root needs to: the build runs as a root
    systemd unit and drops every compiler and every check to ``user``, while the
    same checks re-run by hand are already that user. Calling setpriv anyway
    fails with "initgroups failed: Operation not permitted", which arrives as an
    interface that never drew rather than as a permission error.
    """
    if not running_as_root():
        return command
    return ['setpriv', '--reuid', user, '--regid', user, '--init-groups',
            '--inh-caps=-all', '--'] + command


# --------------------------------------------------------------------------
# preflight
# --------------------------------------------------------------------------
def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open('rb') as handle:
        for block in iter(lambda: handle.read(1 << 20), b''):
            h.update(block)
    return h.hexdigest()


def free_bytes(path: str) -> int:
    stat = os.statvfs(path)
    return stat.f_bavail * stat.f_frsize


def preflight(root: Path, archive: Path, expected: str) -> dict:
    machine = os.uname().machine
    if machine != 'aarch64':
        raise RuntimeError(f'This worker expects aarch64; found {machine}')
    missing = [tool for tool in ('gcc', 'g++', 'make', 'cmake', 'setpriv')
               if shutil.which(tool) is None]
    if missing:
        raise RuntimeError('Missing build tools: ' + ', '.join(missing) +
                           '. cmake is installed by the remote driver; the rest '
                           'come with build-essential.')
    if not archive.is_file():
        raise RuntimeError(f'Source archive not found: {archive}')
    actual = sha256_of(archive)
    if actual != expected:
        raise RuntimeError(f'{archive.name}: sha256 {actual} on the device, '
                           f'{expected} expected. The upload is incomplete or '
                           f'corrupt; delete it and send it again.')
    free = free_bytes(str(root))
    # Measured: the unpacked tree is about 110 MB and the build tree, with four
    # vendored dependency stages and unstripped objects, about 2.2 GB.
    if free < 4 << 30:
        raise RuntimeError(f'Only {free / 1e9:,.1f} GB free at {root}; the build '
                           f'tree needs about 2.5 GB.')
    facts = {'machine': machine, 'cmake': version_of('cmake'),
             'gcc': version_of('gcc'), 'free_bytes': free,
             'archive_sha256': actual, 'archive_bytes': archive.stat().st_size}
    audit('preflight_ok', **facts)
    return facts


def version_of(tool: str) -> str:
    try:
        out = subprocess.run([tool, '--version'], stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT, timeout=30)
        return out.stdout.decode('utf-8', 'replace').splitlines()[0].strip()
    except (OSError, subprocess.SubprocessError, IndexError):
        return 'unknown'


# --------------------------------------------------------------------------
# the units that hold the memory
# --------------------------------------------------------------------------
def unit_state(unit: str) -> str:
    out = subprocess.run(['systemctl', 'is-active', unit], stdout=subprocess.PIPE,
                         stderr=subprocess.STDOUT)
    return out.stdout.decode().strip() or 'unknown'


def release_memory() -> list[str]:
    """Stop the AI units and report which ones were running."""
    stopped = []
    for unit in AI_UNITS:
        if unit_state(unit) == 'active':
            run(['systemctl', 'stop', unit], timeout=120, check=False)
            stopped.append(unit)
    if stopped:
        # Stopping is asynchronous enough that the pages are not free yet.
        time.sleep(3)
    audit('services_stopped', units=stopped, available_kb=available_memory_kb())
    return stopped


def restore_memory(stopped: list[str]) -> None:
    for unit in stopped:
        run(['systemctl', 'start', unit], timeout=180, check=False)
    audit('services_restored', units=stopped,
          states={unit: unit_state(unit) for unit in stopped})


def available_memory_kb() -> int:
    for line in Path('/proc/meminfo').read_text().splitlines():
        if line.startswith('MemAvailable:'):
            return int(line.split()[1])
    return 0


# --------------------------------------------------------------------------
# source and build
# --------------------------------------------------------------------------
def unpack(archive: Path, source: Path, user: str, expected: str) -> int:
    """Extract the source, unless the tree already holds exactly these bytes.

    The digest is recorded in the tree and checked here because re-extracting
    identical files is not free: it gives every file a new timestamp, so ninja
    rebuilds all 226 targets instead of nothing. A rerun after a failure further
    along should not pay for a full rebuild.
    """
    stamp = source / '.mixos-source-sha256'
    if stamp.is_file() and stamp.read_text(encoding='utf-8').strip() == expected:
        files = sum(1 for _ in source.rglob('*') if _.is_file())
        audit('source_already_present', path=str(source), files=files,
              sha256=expected)
        return files

    staging = source.parent / 'term-ime'
    for stale in (source, staging):
        if stale.exists():
            shutil.rmtree(stale)
    source.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive, 'r:gz') as tar:
        members = [m for m in tar.getmembers()
                   if not (Path(m.name).is_absolute() or '..' in Path(m.name).parts)]
        tar.extractall(source.parent, members=members, filter='data')
    staging.rename(source)
    # The staged archive is deterministic, which means every file in it carries
    # the same fixed 2020 timestamp. ninja compares timestamps, so extracting a
    # *changed* source tree over an existing build directory would leave every
    # object looking newer than its source and nothing would be rebuilt. Stamping
    # the tree with the current time is what makes a new archive mean a rebuild.
    now = time.time()
    for item in [source] + list(source.rglob('*')):
        os.utime(item, (now, now))
    stamp.write_text(expected + '\n', encoding='utf-8')
    files = sum(1 for _ in source.rglob('*') if _.is_file())
    run(['chown', '-R', f'{user}:{user}', str(source)], timeout=600)
    audit('source_unpacked', path=str(source), files=files, sha256=expected)
    return files


def bounded_arguments(arguments: list[str], limit: int) -> list[str]:
    """Replace a bare ``-j`` with ``-j<limit>``; leave everything else alone.

    CMake's ``--build ... -j`` with no number passes a bare ``-j`` to the native
    tool, and GNU make reads that as unlimited parallelism. Everything else —
    ``-j4``, targets, ``--target``, variable assignments —has to arrive
    untouched, because this stands in for make itself.
    """
    return [f'-j{limit}' if argument == '-j' else argument for argument in arguments]


def write_make_shim(directory: Path, jobs: int, worker: Path) -> Path:
    """A ``make`` that refuses to be unlimited.

    term-ime's CMakeLists configures and builds yaml-cpp, leveldb, marisa-trie
    and opencc with ``cmake --build ... --target install -j``. On this machine
    that is one compiler per source file —about 150 at once —which is not slow
    but fatal. This shim is put earlier on PATH, so it is what those nested
    builds record as CMAKE_MAKE_PROGRAM; it calls ``bounded_arguments`` above
    rather than repeating the rule, so the behaviour the tests check is the
    behaviour the device gets.
    """
    directory.mkdir(parents=True, exist_ok=True)
    shim = directory / 'make'
    shim.write_text(
        '#!/usr/bin/env python3\n'
        '"""Bound a bare -j; forward everything else to /usr/bin/make."""\n'
        'import os, sys\n'
        # as_posix, not str: this file is generated on a Windows host by the
        # tests and always runs on the device.
        f'sys.path.insert(0, {worker.parent.as_posix()!r})\n'
        'from build_ime_on_pi import bounded_arguments\n'
        f'arguments = bounded_arguments(sys.argv[1:], {jobs})\n'
        'if arguments != sys.argv[1:]:\n'
        f"    print('make shim: bounded a bare -j to -j{jobs}', "
        'file=sys.stderr, flush=True)\n'
        "os.execv('/usr/bin/make', ['make'] + arguments)\n")
    shim.chmod(0o755)
    return shim


def build_environment(root: Path, user: str, jobs: int) -> dict:
    home = str(Path('~' + user).expanduser())
    shim_dir = root / 'bin'
    write_make_shim(shim_dir, jobs, Path(__file__).resolve())
    return {
        'PATH': f'{shim_dir}:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin',
        'HOME': home,
        'USER': user,
        'LC_ALL': 'C.UTF-8',
        'LANG': 'C.UTF-8',
        # Belt to the shim's braces: honoured by `cmake --build` when no
        # explicit -j is on its command line.
        'CMAKE_BUILD_PARALLEL_LEVEL': str(jobs),
        'MAKEFLAGS': f'-j{jobs}',
    }


def configure_and_build(root: Path, user: str, jobs: int, with_tests: bool) -> dict:
    source, build = root / 'src', root / 'build'
    build.mkdir(parents=True, exist_ok=True)
    run(['chown', '-R', f'{user}:{user}', str(build)], timeout=300)
    environment = build_environment(root, user, jobs)
    run(['chown', '-R', f'{user}:{user}', str(root / 'bin')], timeout=60)

    started = time.monotonic()
    # Ninja for the top level: it accepts an explicit job count, and the four
    # nested dependency builds keep the default Makefiles generator, where the
    # shim is what keeps `-j` bounded.
    run(as_user(user, ['cmake', '-S', str(source), '-B', str(build), '-G', 'Ninja',
                       '-DCMAKE_BUILD_TYPE=Release']),
        env=environment, timeout=5400)
    audit('configure_ok', seconds=round(time.monotonic() - started, 1),
          available_kb=available_memory_kb())

    targets = ['term-ime'] + (['term-ime-tests'] if with_tests else [])
    started = time.monotonic()
    run(as_user(user, ['cmake', '--build', str(build), '-j', str(jobs),
                       '--target'] + targets),
        env=environment, timeout=10800)
    binary = build / 'term-ime'
    if not binary.is_file():
        raise RuntimeError(f'The build reported success but {binary} is missing')
    facts = {'seconds': round(time.monotonic() - started, 1),
             'bytes': binary.stat().st_size,
             'linkage': linkage(binary),
             'available_kb': available_memory_kb()}
    audit('build_ok', **facts)

    if with_tests:
        result = run(as_user(user, ['ctest', '--test-dir', str(build),
                                   '--output-on-failure', '-j', '1']),
                     env=environment, timeout=3600, check=False)
        passed = re.search(r'(\d+)% tests passed, (\d+) tests failed out of (\d+)',
                           result.stdout)
        audit('upstream_tests', exit=result.returncode,
              summary=passed.group(0) if passed else 'no ctest summary')
    return facts


def linkage(binary: Path) -> str:
    """'static' is the claim term-ime makes about itself; check it."""
    out = subprocess.run(['ldd', str(binary)], stdout=subprocess.PIPE,
                         stderr=subprocess.STDOUT)
    text = out.stdout.decode('utf-8', 'replace').strip()
    return 'static' if 'not a dynamic executable' in text else text[:200]


def install(root: Path) -> dict:
    """Put the binary and its data where a running system looks for them.

    Not ``cmake --install``. That runs every subproject's install rules, so it
    tries to copy ``libftxui-component.a`` —a target this build never asked for,
    because term-ime links only ``ftxui::dom`` and ``ftxui::screen`` —and stops
    with "file INSTALL cannot find". Building every target to satisfy an install
    rule would also drop FTXUI and GoogleTest archives and their headers into
    ``/usr/local`` on a device that will never compile against them.

    So the three things that are actually needed are copied by name:

    * the executable. term-ime's CMakeLists has no
      ``install(TARGETS term-ime RUNTIME ...)`` at all, so even a successful
      project-wide install leaves ``/usr/local/bin`` empty and the notes launcher
      quietly falling back to no Chinese input.
    * the rime data —the luna_pinyin schemas, the 4 MB ``essay.txt`` frequency
      table and the OpenCC simplification dictionaries. Without ``essay.txt``
      candidates are single characters in Unicode order, so 你好 never appears.
    * the interface strings. Nothing installs these; ``I18n`` looks in
      ``<cwd>/data/translations`` and then in the two system prefixes, so with
      them absent term-ime falls back to nine hard-coded strings.
    """
    build, source = root / 'build', root / 'src'
    binary_source = build / 'term-ime'
    run(['install', '-D', '-m', '0755', str(binary_source), BINARY], timeout=120)

    data = source / 'data'
    shared = Path(SHARED_DATA)
    run(['install', '-d', '-m', '0755', str(shared), str(shared / 'opencc'),
         str(Path(PREFIX) / 'share/term-ime/translations')], timeout=60)
    copied = []
    for pattern, destination in (
            ('rime-data/*.yaml', shared),
            ('rime-data/essay.txt', shared),
            ('rime-data/opencc/*', shared / 'opencc'),
            ('translations/*.json', Path(PREFIX) / 'share/term-ime/translations'),
            ('pinyin.dict', Path(PREFIX) / 'share/term-ime')):
        for item in sorted(data.glob(pattern)):
            if item.is_file():
                run(['install', '-m', '0644', str(item),
                     str(destination / item.name)], timeout=60)
                copied.append(str(destination / item.name))

    schemas = sorted(p.name for p in shared.glob('*.schema.yaml'))
    if 'luna_pinyin_simp.schema.yaml' not in schemas:
        raise RuntimeError(f'{SHARED_DATA} has no luna_pinyin_simp schema; rime '
                           f'would start with nothing to convert with.')
    essay = shared / 'essay.txt'
    if not essay.is_file() or essay.stat().st_size < 1_000_000:
        raise RuntimeError('essay.txt is missing or truncated; without the preset '
                           'vocabulary table rime ranks rare single characters '
                           'ahead of common words.')
    facts = {'binary': BINARY, 'bytes': Path(BINARY).stat().st_size,
             'shared_data': SHARED_DATA, 'schemas': schemas,
             'essay_bytes': essay.stat().st_size, 'files': len(copied)}
    audit('installed', **facts)
    return facts


# --------------------------------------------------------------------------
# verification: does typing pinyin produce 汉字?
# --------------------------------------------------------------------------
def hand_to_user(path: Path, user: str) -> None:
    """Give a file to the unprivileged user, when there is anything to give.

    Root creating a file the ordinary user then has to read or append to is the
    default here, so this is called on each of them. Outside that case — the
    checks re-run by hand, or the portable tests on a machine with no such user —
    the file is already owned by whoever made it and there is nothing to do.
    """
    if not running_as_root():
        return
    try:
        shutil.chown(path, user, user)
    except (OSError, KeyError, LookupError):
        pass


def user_config(root: Path, user: str, shell: str) -> Path:
    """A config for the checks, kept away from the one the notes launcher writes.

    ``rime_shared_data_dir`` is named rather than left to be discovered, and it
    names the installed prefix —the same value the notes launcher writes. Left
    empty, term-ime searches, and the first place it looks is
    ``RIME_BUNDLED_DATA_DIR``: the path of the build tree it was compiled in.
    That exists here, so a check that left the key out would pass by reading the
    build tree and say nothing about whether the installed data is usable.
    """
    path = root / 'verify-config.json'
    path.write_text(json.dumps({
        'shell': shell,
        'active_language': 'zh-Hans',
        'ui_language': 'en',
        'candidate_bar_position': 'bottom',
        'rime_shared_data_dir': SHARED_DATA,
        'log_level': 'info',
    }, indent=2) + '\n', encoding='utf-8')
    hand_to_user(path, user)
    return path


class Terminal:
    """term-ime running on a pseudo-terminal, with its frames collected."""

    def __init__(self, command: list[str], environment: dict, columns=80, rows=24):
        import fcntl
        import pty
        import struct
        import termios
        self.text = ''
        self.master, slave = pty.openpty()
        # The size has to be set before the child starts: term-ime asks the
        # terminal once and gives its child one row less.
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack('HHHH', rows, columns, 0, 0))
        # And flow control has to go, for the same reason mixosd disables it on
        # the session terminal: a fresh pseudo-terminal has IXON set, so Ctrl-S
        # is XOFF. Left on, sending Ctrl-S to save a note suspends the child's
        # output instead of reaching it, and the check sees a screen that simply
        # stopped changing. This process is the terminal here, so it is this
        # process's job.
        attributes = termios.tcgetattr(slave)
        attributes[0] &= ~(termios.IXON | termios.IXOFF |
                           getattr(termios, 'IXANY', 0))
        termios.tcsetattr(slave, termios.TCSANOW, attributes)
        self.pid = os.fork()
        if self.pid == 0:                               # child
            try:
                os.setsid()
                fcntl.ioctl(slave, termios.TIOCSCTTY, 0)
                for target in (0, 1, 2):
                    os.dup2(slave, target)
                if slave > 2:
                    os.close(slave)
                os.close(self.master)
                os.execvpe(command[0], command, environment)
            except BaseException:
                os._exit(127)
        os.close(slave)

    def drain(self, seconds: float) -> str:
        """Read whatever arrives within a window, without blocking on silence."""
        deadline, collected = time.monotonic() + seconds, ''
        while time.monotonic() < deadline:
            ready, _, _ = select.select([self.master], [], [], 0.2)
            if not ready:
                continue
            try:
                chunk = os.read(self.master, 65536)
            except OSError as exc:
                if exc.errno in (errno.EIO, errno.EBADF):
                    break
                raise
            if not chunk:
                break
            collected += chunk.decode('utf-8', 'replace')
        self.text += collected
        return collected

    def wait_for(self, needle: str, seconds: float) -> bool:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if needle in self.text:
                return True
            self.drain(0.5)
        return needle in self.text

    def send(self, data: bytes, settle: float = 0.6) -> None:
        os.write(self.master, data)
        self.drain(settle)

    def close(self) -> int:
        for attempt, sig in enumerate((signal.SIGTERM, signal.SIGKILL)):
            try:
                os.kill(self.pid, sig)
            except ProcessLookupError:
                break
            for _ in range(40 if attempt == 0 else 20):
                done, status = os.waitpid(self.pid, os.WNOHANG)
                if done:
                    os.close(self.master)
                    return status
                time.sleep(0.1)
        try:
            os.close(self.master)
        except OSError:
            pass
        return -1


def strip_escapes(text: str) -> str:
    """The characters a person would see, with the cursor moves removed."""
    return re.sub(r'\x1b[\[\]][0-9;?]*[A-Za-z~]|\x1b[()][B0]|\x1b[=>]', '', text)


def warm_rime(root: Path, user: str, environment: dict, seconds: int) -> dict:
    """Run term-ime once so rime compiles the schema before anybody waits for it.

    On a fresh user data directory ``RimeIme::initialize`` builds prism.bin and
    table.bin from the 939 kB dictionary and the 4 MB essay file, synchronously,
    inside the first launch. Doing that here means the first time the notes
    button is pressed the editor appears immediately rather than after a minute
    of apparent hang.
    """
    home = Path(environment['HOME'])
    staging = home / '.local/share/term-ime/build'
    config = user_config(root, user, '/bin/cat')
    started = time.monotonic()
    terminal = Terminal(as_user(user, [BINARY, str(config)]), environment)
    try:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            terminal.drain(2.0)
            prisms = sorted(p.name for p in staging.glob('*.prism.bin')) \
                if staging.is_dir() else []
            if 'luna_pinyin_simp.prism.bin' in prisms:
                break
        prisms = sorted(p.name for p in staging.glob('*.bin')) if staging.is_dir() else []
    finally:
        terminal.close()
    facts = {'seconds': round(time.monotonic() - started, 1),
             'staging': str(staging), 'compiled': prisms}
    if 'luna_pinyin_simp.prism.bin' not in prisms:
        audit('rime_warm_incomplete', **facts)
        raise RuntimeError(f'rime did not compile luna_pinyin_simp within '
                           f'{seconds}s; {staging} holds {prisms}')
    audit('rime_warmed', **facts)
    return facts


def verify_input(root: Path, user: str, environment: dict) -> dict:
    """Type pinyin into the installed binary and require 汉字 to come out."""
    config = user_config(root, user, '/bin/cat')
    terminal = Terminal(as_user(user, [BINARY, str(config)]), environment)
    evidence: dict = {'expected': EXPECTED, 'pinyin': PINYIN.decode()}
    try:
        terminal.drain(4.0)
        started_len = len(terminal.text)
        terminal.send(TOGGLE, settle=1.0)
        for byte in PINYIN:
            terminal.send(bytes([byte]), settle=0.35)
        candidates_shown = terminal.wait_for(EXPECTED, 20.0)
        evidence['candidate_bar'] = candidates_shown
        frame = strip_escapes(terminal.text[started_len:])
        evidence['frame_tail'] = frame[-600:]
        if not candidates_shown:
            audit('ime_no_candidates', **evidence)
            raise RuntimeError(f'Typing {PINYIN.decode()!r} in Chinese mode never '
                               f'produced {EXPECTED}. The binary runs but the '
                               f'input method is not converting.')
        # Space commits the first candidate; /bin/cat echoes what was committed,
        # so seeing it again after the commit separates "the bar can draw 你好"
        # from "the child received 你好".
        before = len(terminal.text)
        terminal.send(b' ', settle=1.5)
        terminal.send(b'\r', settle=1.5)
        committed = strip_escapes(terminal.text[before:])
        evidence['committed_tail'] = committed[-400:]
        evidence['committed'] = EXPECTED in committed
    finally:
        evidence['exit_status'] = terminal.close()
    if not evidence['committed']:
        audit('ime_commit_failed', **evidence)
        raise RuntimeError(f'{EXPECTED} appeared as a candidate but was never '
                           f'committed to the child process.')
    audit('ime_verified', **{k: v for k, v in evidence.items()
                             if k not in ('frame_tail',)})
    return evidence


def log_tail(home: str, lines: int = 25) -> str:
    """The end of term-ime's own log, which is where it says what it refused."""
    path = Path(home) / '.cache/term-ime/term-ime.log'
    try:
        return '\n'.join(path.read_text(encoding='utf-8', errors='replace')
                         .splitlines()[-lines:])
    except OSError as exc:
        return f'({path}: {exc})'


def verify_launcher() -> dict:
    """The notes launcher decides on its own whether Chinese input happens."""
    launcher = Path(LAUNCHER)
    app = Path('/opt/mixos/linux/apps/notes/app.py')
    facts = {'launcher': LAUNCHER, 'launcher_present': launcher.is_file(),
             'editor_executable': app.is_file() and os.access(app, os.X_OK),
             'binary_executable': os.access(BINARY, os.X_OK),
             'shared_data_present': Path(SHARED_DATA).is_dir()}
    if launcher.is_file():
        text = launcher.read_text(encoding='utf-8', errors='replace')
        facts['expects_binary_at'] = BINARY in text
        facts['names_shared_data'] = SHARED_DATA in text
    audit('launcher_checked', **facts)
    return facts


def verify_notes(root: Path, user: str, environment: dict) -> dict:
    """Drive the real notes button and require 汉字 to reach the saved file.

    Everything before this proves that term-ime converts pinyin. This proves the
    thing a person actually does: the launcher that ``mixosd`` runs, the editor
    it starts inside term-ime, a new note, Chinese typed into it, saved, and then
    read back off the disk with this process nowhere near it.

    Notes go to a scratch directory through ``MIXOS_NOTES_DIR`` so a check never
    leaves anything in somebody's real notes.
    """
    notes_dir = root / 'verify-notes'
    if notes_dir.exists():
        shutil.rmtree(notes_dir)
    notes_dir.mkdir(parents=True)
    hand_to_user(notes_dir, user)
    child_environment = dict(environment, MIXOS_NOTES_DIR=str(notes_dir),
                             TERM='mixos')

    evidence: dict = {'notes_dir': str(notes_dir), 'columns': 48, 'rows': 16,
                      'toggle': DEVICE_TOGGLE.hex()}
    terminal = Terminal(as_user(user, ['/bin/sh', LAUNCHER]), child_environment,
                        columns=48, rows=16)

    def step(name: str, marker: str, seconds: float) -> None:
        """Wait for the screen to show that a step happened, or say what it showed.

        Each marker is text the interface itself draws, so the check follows the
        interface rather than a stopwatch. Without this, a key sent one beat too
        early is swallowed and the failure appears much later as "nothing was
        saved", which is true and useless.
        """
        if terminal.wait_for(marker, seconds):
            evidence[name] = True
            return
        evidence[name] = False
        evidence['stopped_at'] = name
        evidence['looking_for'] = marker
        evidence['frame_tail'] = strip_escapes(terminal.text)[-1500:]
        evidence['ime_log_tail'] = log_tail(environment['HOME'])
        audit('notes_step_failed', **evidence)
        raise RuntimeError(f'{name}: the screen never showed {marker!r}. The frame '
                           f'and the tail of term-ime\'s log are in the audit.')

    try:
        # '笔记' is the list header: the first thing that says the editor is
        # running rather than term-ime alone.
        step('list_drawn', '笔记', 25.0)
        # Into Chinese first, and then drive the list with control keys. This is
        # the order a person ends up in, and it is the case that used to be
        # broken: with the input method in Chinese mode the list's letters are
        # pinyin, so 'n' composed a syllable instead of making a note.
        terminal.send(DEVICE_TOGGLE, settle=1.0)
        terminal.send(b'\x0e', settle=0.8)                  # Ctrl-N, a new note
        # The editor's footer. Waiting for it is what distinguishes "the editor
        # opened" from "the keystroke was swallowed", which otherwise shows up
        # much later as no note at all.
        step('editor_open', '保存', 15.0)
        for byte in PINYIN:
            terminal.send(bytes([byte]), settle=0.35)
        step('candidate_bar', EXPECTED, 20.0)
        terminal.send(b' ', settle=1.2)                    # commit it
        terminal.send(DEVICE_TOGGLE, settle=0.6)            # English
        terminal.send(b'MixOS', settle=0.6)
        terminal.send(b'\x13', settle=1.0)                 # Ctrl-S, save
        step('saved', 'saved', 15.0)
        terminal.send(DEVICE_TOGGLE, settle=0.6)            # Chinese again
        terminal.send(b'ni', settle=0.6)                    # unfinished syllable
        terminal.text = ''
        terminal.send(b'\x11', settle=1.2)                 # cancel composition, Back
        step('back_to_list', '新建', 15.0)
        terminal.text = ''
        terminal.send(b'\x11', settle=1.2)                 # root remains open
        step('back_stays_at_root', '已到笔记列表', 15.0)
        evidence['frame_tail'] = strip_escapes(terminal.text)[-1200:]
    finally:
        evidence['exit_status'] = terminal.close()

    saved = sorted(notes_dir.glob('*.md'))
    evidence['files'] = [path.name for path in saved]
    texts = {path.name: path.read_text(encoding='utf-8', errors='replace')
             for path in saved}
    evidence['contents'] = texts
    if not any(text.strip() == EXPECTED + 'MixOS' for text in texts.values()):
        audit('notes_chinese_failed', **evidence)
        raise RuntimeError(f'No saved note exactly contains {EXPECTED}MixOS. Files: '
                           f'{evidence["files"]}')
    audit('notes_chinese_verified', **evidence)
    return evidence


# --------------------------------------------------------------------------
# entry points
# --------------------------------------------------------------------------
def command_build(args) -> int:
    root = Path(args.root).expanduser().resolve()
    archive = root / args.archive
    facts = {'preflight': preflight(root, archive, args.sha256)}
    stopped: list[str] = []
    try:
        stopped = release_memory()
        unpack(archive, root / 'src', args.user, args.sha256)
        facts['build'] = configure_and_build(root, args.user, args.jobs, args.with_tests)
        facts['install'] = install(root)
        environment = build_environment(root, args.user, args.jobs)
        facts['warm'] = warm_rime(root, args.user, environment, args.warm_seconds)
        facts['input'] = verify_input(root, args.user, environment)
        facts['launcher'] = verify_launcher()
        if facts['launcher'].get('launcher_present'):
            facts['notes'] = verify_notes(root, args.user, environment)
    finally:
        restore_memory(stopped)
    audit('build_and_verify_complete', binary=BINARY,
          bytes=facts['install']['bytes'], linkage=facts['build']['linkage'],
          notes_checked='notes' in facts)
    print('\nterm-ime is installed and typing pinyin produces 汉字.')
    return 0


def command_verify(args) -> int:
    root = Path(args.root).expanduser().resolve()
    if not os.access(BINARY, os.X_OK):
        raise RuntimeError(f'{BINARY} is not installed; run the build first.')
    environment = build_environment(root, args.user, args.jobs)
    audit('verify_start', binary=BINARY, bytes=Path(BINARY).stat().st_size,
          linkage=linkage(Path(BINARY)))
    warm_rime(root, args.user, environment, args.warm_seconds)
    verify_input(root, args.user, environment)
    facts = verify_launcher()
    if facts.get('launcher_present'):
        verify_notes(root, args.user, environment)
    else:
        print(f'{LAUNCHER} is not installed, so the notes path was not checked. '
              f'Run tools/deploy_apps.py first.')
    print('\nterm-ime converts pinyin to 汉字 on this device.')
    return 0


def command_verify_notes(args) -> int:
    """Only the user-facing path: the notes button, typed into, read back."""
    root = Path(args.root).expanduser().resolve()
    if not Path(LAUNCHER).is_file():
        raise RuntimeError(f'{LAUNCHER} is not installed; run tools/deploy_apps.py.')
    environment = build_environment(root, args.user, args.jobs)
    verify_notes(root, args.user, environment)
    print('\nTyping pinyin in the notes editor saves 汉字 to the note file.')
    return 0


def main(argv: list[str] | None = None) -> int:
    global AUDIT
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('command', choices=('build', 'verify', 'verify-notes'))
    parser.add_argument('--root', default='/home/pi/mixos-ime',
                        help='where the source, build tree and log live')
    parser.add_argument('--archive', default='term-ime-src.tar.gz')
    parser.add_argument('--sha256', default='',
                        help='expected digest of the uploaded archive')
    parser.add_argument('--user', default='pi',
                        help='the unprivileged user that compiles and runs the checks')
    parser.add_argument('--jobs', type=int, default=2,
                        help='compiler processes; 2 on a 4 GiB machine (default)')
    parser.add_argument('--with-tests', action='store_true',
                        help="also build and run term-ime's own test binaries")
    parser.add_argument('--warm-seconds', type=int, default=900,
                        help='how long rime may take to compile the schema')
    args = parser.parse_args(argv)

    root = Path(args.root).expanduser()
    root.mkdir(parents=True, exist_ok=True)
    AUDIT = root / 'build-audit.jsonl'
    # The build runs as root and the checks can be re-run as the ordinary user.
    # A root-owned audit file makes the second of those fail while it is writing
    # down why the first one failed, so ownership is handed over immediately.
    if running_as_root():
        AUDIT.touch(exist_ok=True)
        hand_to_user(AUDIT, args.user)
        os.chmod(AUDIT, 0o644)

    if args.command == 'build' and not args.sha256:
        raise SystemExit('build needs --sha256: the digest the archive must have.')
    if args.command == 'build' and not running_as_root():
        raise SystemExit('build has to run as root: it stops units and installs '
                         'into /usr/local. The compiler itself is run as '
                         f'{args.user} through setpriv.')

    try:
        if args.command == 'build':
            return command_build(args)
        if args.command == 'verify':
            return command_verify(args)
        return command_verify_notes(args)
    except Exception as exc:                       # one line, then the audit
        audit('failed', kind=type(exc).__name__, message=str(exc)[:2000])
        print(f'\nFAILED: {type(exc).__name__}: {exc}', file=sys.stderr, flush=True)
        return 1


if __name__ == '__main__':
    os.environ.setdefault('PYTHONIOENCODING', 'utf-8')
    sys.exit(main())
