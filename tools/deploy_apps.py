#!/usr/bin/env python3
"""Install the four-button interfaces, their launchers, the daemon and the units.

The device holds three separate things and they are installed to three separate
places, because they have three different jobs:

``/opt/mixos/linux``
    ``mixosd.py`` and ``protocol.py``: the daemon that owns the CDC link to the
    ESP32 and the frame codec it speaks. This is the half that turns a button
    press on the screen into a running program, through a fixed table of four
    names. Until 2026-09-14 nothing in this repository installed it, so the
    device kept running whatever was copied there by hand on 2026-09-10 - a
    version with no application table at all, which answered every launcher
    button with silence.

``/opt/mixos/linux/apps``
    The Python that draws the interfaces, beside ``mixosd.py`` in the tree it
    came from. Read by the launchers, never run by ``mixosd`` directly.

``/usr/local/lib/mixos/apps``
    The launchers ``translate``, ``notes`` and ``agent``: one executable per
    button, taking no arguments. This is ``mixosd``'s ``--app-dir``, and the
    name of a file in it is the entire vocabulary the device can use to ask for
    a program. Nothing typed by a person is ever part of that name.

Those three have to agree or the button does nothing, and each can be wrong on
its own: firmware that never draws the card, a daemon that does not know the
name, or a launcher that is not installed. ``--check-only`` reports all three.

Everything travels as one gzipped tar over the SSH connection and is unpacked
into a staging directory the ordinary user owns. The only privileged step is a
single script, run once with ``sudo``, that copies out of that staging
directory. The password reaches ``sudo`` on standard input and never appears in
an argument vector or in a file.

    $env:MIXOS_SSH_PASSWORD='...'
    py -3.12 tools/deploy_apps.py --host 192.168.1.22
    py -3.12 tools/deploy_apps.py --host 192.168.1.22 --check-only
    py -3.12 tools/deploy_apps.py --host 192.168.1.22 --enable

``--enable`` starts the two AI services, and refuses to when their virtual
environment is not there: a unit that cannot start is worse than a unit that is
not installed, because systemd will restart it forever and fill the journal.

Updating the daemon restarts ``mixosd``, which drops whatever is on the screen
and reconnects. ``--no-daemon`` skips it when that matters.
"""
from __future__ import annotations

import argparse
import getpass
import io
import os
import shlex
import sys
import tarfile
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from deploy_models import Remote                              # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
APPS = ROOT / 'linux/apps'
LAUNCHERS = ROOT / 'linux/launchers'
UNITS = ('mixos-litertlm.service', 'mixos-aiserver.service')
POLKIT = 'linux/50-mixos-network.rules'
# The daemon and the two modules it imports. mixosd.service runs mixosd.py from
# DAEMON_DIR with WorkingDirectory there, so both have to land beside it.
# netctl.py is easy to forget because nothing references it by name in the unit
# or the docs; leaving it out makes the daemon exit on import and restart for
# ever, which is what happened on 2026-09-14.
DAEMON_FILES = ('mixosd.py', 'protocol.py', 'netctl.py')
DAEMON_SERVICE = 'mixosd.service'

DAEMON_DIR = '/opt/mixos/linux'
APP_LIB = '/opt/mixos/linux/apps'
APP_DIR = '/usr/local/lib/mixos/apps'
VENV = '/home/pi/mixos-ai/venv'

# What goes in the tar. Compiled bytecode and the editor's scratch files are
# not source and must not be shipped: a stale .pyc that shadows a changed .py
# is a bug that only appears on the device.
SKIP_DIRECTORIES = {'__pycache__', '.pytest_cache'}
SKIP_SUFFIXES = {'.pyc', '.pyo', '.orig', '.rej'}
# Files that a shell or a kernel has to read line by line. A carriage return
# in a shebang makes Linux report that the interpreter does not exist, which is
# a confusing way to find out that a file was written on Windows.
TEXT_SUFFIXES = {'.py', '.sh', '.service', '.rules', '.txt', '.json', '.md'}


def interesting(path: Path) -> bool:
    if any(part in SKIP_DIRECTORIES for part in path.parts):
        return False
    return path.suffix not in SKIP_SUFFIXES


def payload(name: str, data: bytes) -> bytes:
    """Normalise line endings for anything that is read as lines."""
    if Path(name).suffix in TEXT_SUFFIXES or not Path(name).suffix:
        return data.replace(b'\r\n', b'\n')
    return data


def build_archive(daemon: bool = True) -> bytes:
    """One tar holding the daemon, the interfaces, the launchers and the units."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode='w:gz') as archive:
        def add(source: Path, name: str, mode: int) -> None:
            data = payload(name, source.read_bytes())
            info = tarfile.TarInfo(name)
            info.size, info.mode, info.mtime = len(data), mode, int(time.time())
            archive.addfile(info, io.BytesIO(data))

        if daemon:
            for name in DAEMON_FILES:
                add(ROOT / 'linux' / name, f'daemon/{name}', 0o644)

        for path in sorted(APPS.rglob('*')):
            if not path.is_file() or not interesting(path.relative_to(APPS)):
                continue
            relative = path.relative_to(APPS).as_posix()
            # The two interfaces are executed directly by term-ime, which uses
            # execl and therefore needs the bit set on the file itself.
            executable = relative in ('notes/app.py', 'translator/app.py')
            add(path, f'apps/{relative}', 0o755 if executable else 0o644)

        for path in sorted(LAUNCHERS.iterdir()):
            if path.is_file():
                add(path, f'launchers/{path.name}', 0o755)

        for unit in UNITS:
            add(ROOT / 'linux' / unit, f'units/{unit}', 0o644)
        add(ROOT / POLKIT, 'polkit/' + Path(POLKIT).name, 0o644)
    return buffer.getvalue()


def install_script(stage: str, enable: bool, daemon: bool = True) -> str:
    """The one privileged step, written out so it can be read before it runs."""
    stage = shlex.quote(stage)
    lines = [
        'set -eu',
        f'install -d -m 0755 {APP_LIB}',
        # --delete so a file removed here is removed there; a module left
        # behind keeps being imported and keeps being wrong.
        f'rsync -a --delete --chmod=D0755 {stage}/apps/ {APP_LIB}/',
        f'install -d -m 0755 {APP_DIR}',
        f'install -m 0755 {stage}/launchers/translate {APP_DIR}/translate',
        f'install -m 0755 {stage}/launchers/notes {APP_DIR}/notes',
        f'install -m 0755 {stage}/launchers/agent {APP_DIR}/agent',
        'install -d -m 0755 /etc/polkit-1/rules.d',
        f'install -m 0644 {stage}/polkit/50-mixos-network.rules '
        '/etc/polkit-1/rules.d/50-mixos-network.rules',
    ]
    if daemon:
        for name in DAEMON_FILES:
            lines.append(f'install -m 0644 {stage}/daemon/{name} {DAEMON_DIR}/{name}')
    for unit in UNITS:
        lines.append(f'install -m 0644 {stage}/units/{unit} /etc/systemd/system/{unit}')
    lines += [
        'systemctl daemon-reload',
        # polkit reads its rules directory on change, but only reliably after a
        # reload; without this the settings page still fails to scan Wi-Fi.
        'systemctl reload polkit || systemctl restart polkit || true',
        # The interfaces run under the system interpreter, so the system
        # interpreter is what must be able to parse them.
        f'python3 -m compileall -q {APP_LIB} >/dev/null',
    ]
    if daemon:
        lines += [
            # The daemon is what turns a button into a program. Replacing a
            # working one with a broken one leaves the screen with no host and
            # systemd restarting it for ever, so it is checked first - by
            # running it, not by compiling it. Compiling proves the syntax
            # parses and says nothing about a missing import, which is exactly
            # the way this broke on 2026-09-14.
            f'( cd {DAEMON_DIR} && python3 mixosd.py --help >/dev/null )',
            f'systemctl restart {DAEMON_SERVICE}',
        ]
    if enable:
        lines += [
            f'if [ -x {VENV}/bin/python ]; then',
            '  systemctl enable --now mixos-litertlm.service mixos-aiserver.service',
            'else',
            f'  echo "not enabling: {VENV} does not exist yet" >&2',
            'fi',
        ]
    return '\n'.join(lines) + '\n'


def report(remote: Remote) -> None:
    """Say what is on the device now, in the order it would fail."""
    checks = {
        'launchers': f'ls -1 {APP_DIR} 2>/dev/null | tr "\\n" " "',
        'interfaces': f'ls -1 {APP_LIB} 2>/dev/null | tr "\\n" " "',
        # The button does nothing when the daemon predates the application
        # table, which is exactly what was on this device until 2026-09-14.
        # Its size says nothing; the presence of the table says everything.
        'daemon apps': f'grep -o "APP_NAMES = (.*)" {DAEMON_DIR}/mixosd.py '
                       '2>/dev/null || echo "no application table: buttons do nothing"',
        'daemon': f'systemctl is-active {DAEMON_SERVICE}',
        'polkit rule': 'test -f /etc/polkit-1/rules.d/50-mixos-network.rules '
                       '&& echo installed || echo missing',
        'nmcli permission': 'nmcli general permissions 2>/dev/null | '
                            'awk "/wifi.scan/ {print \\$2}"',
        'units': 'systemctl is-enabled mixos-litertlm.service mixos-aiserver.service '
                 '2>&1 | tr "\\n" " "',
        'services': 'systemctl is-active mixos-litertlm.service mixos-aiserver.service '
                    '2>&1 | tr "\\n" " "',
        'python venv': f'test -x {VENV}/bin/python && echo present || echo absent',
        # term-ime has no --version: it treats its first argument as the path to
        # its configuration file, so asking for a version starts a terminal
        # emulator, fails for want of a TTY and exits non-zero. A check that ran
        # it reported "not installed" on a device where it was installed and
        # working. What matters is the file and the rime data beside it, because
        # the launcher requires both before it uses the input method at all.
        'term-ime': 'b=/usr/local/bin/term-ime; '
                    'd=/usr/local/share/term-ime/rime-data; '
                    'if [ -x "$b" ] && [ -f "$d/luna_pinyin_simp.schema.yaml" ]; then '
                    'printf "installed, %s bytes, schemas present" "$(stat -c %s "$b")"; '
                    'elif [ -x "$b" ]; then printf "binary present but %s has no schema" "$d"; '
                    'else printf "not installed (notes works, Chinese input does not)"; fi',
        'audio capture': 'arecord -l 2>/dev/null | grep -c UACCDC',
        'mixosd': 'systemctl is-active mixosd.service',
    }
    print('\nOn the device now:')
    for label, command in checks.items():
        answer = remote.text(command + ' || true', check=False).strip() or '-'
        print(f'  {label:18} {answer[:90]}')


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--host', default='192.168.1.22')
    parser.add_argument('--user', default='pi')
    parser.add_argument('--enable', action='store_true',
                        help='enable and start the two AI services afterwards')
    parser.add_argument('--check-only', action='store_true',
                        help='report what is installed; change nothing')
    parser.add_argument('--no-daemon', action='store_true',
                        help='leave mixosd.py and protocol.py alone; installing '
                             'them restarts mixosd, which drops what is on screen')
    parser.add_argument('--print-script', action='store_true',
                        help='print the privileged script and exit, without connecting')
    args = parser.parse_args()

    daemon = not args.no_daemon

    if args.print_script:
        print(install_script('/home/pi/mixos-install', args.enable, daemon))
        return 0

    for required in (APPS, LAUNCHERS):
        if not required.is_dir():
            raise SystemExit(f'Missing source directory: {required}')
    if daemon:
        for name in DAEMON_FILES:
            if not (ROOT / 'linux' / name).is_file():
                raise SystemExit(f'Missing daemon source: linux/{name}')

    password = os.environ.get('MIXOS_SSH_PASSWORD') or getpass.getpass(
        f'{args.user}@{args.host} password: ')
    if not password:
        raise SystemExit('No password supplied; set MIXOS_SSH_PASSWORD or type one.')

    with tempfile.TemporaryDirectory(prefix='mixos-apps-') as temporary:
        remote = Remote(args.host, args.user, password, Path(temporary))
        if args.check_only:
            report(remote)
            return 0

        archive = build_archive(daemon)
        carried = 'the daemon, interfaces and launchers' if daemon else \
                  'interfaces and launchers'
        print(f'Sending {len(archive) / 1024:,.0f} kB of {carried}')
        home = remote.text('printf "%s" "$HOME"')
        stage = f'{home}/mixos-install'
        remote.run(f'rm -rf {shlex.quote(stage)} && mkdir -p {shlex.quote(stage)} && '
                   f'tar -xzf - -C {shlex.quote(stage)}', data=archive, timeout=300)

        script = install_script(stage, args.enable, daemon)
        remote.run(f'cat > {shlex.quote(stage)}/install.sh',
                   data=script.encode('utf-8'))
        print('Running the privileged step')
        # sudo reads exactly one line from standard input as the password; bash
        # then reads the script from the file, so the two never collide.
        result = remote.run(
            f'sudo -S -p "" bash {shlex.quote(stage)}/install.sh',
            data=(password + '\n').encode('utf-8'), timeout=600, check=False)
        errors = result.stderr.decode('utf-8', 'replace').strip()
        if result.returncode:
            raise SystemExit('The privileged step failed:\n' + errors[-1500:])
        if errors:
            print(errors)
        print(result.stdout.decode('utf-8', 'replace').strip())
        report(remote)

    print('\nDone. Press a button on the device to start one of them.')
    return 0


if __name__ == '__main__':
    os.environ.setdefault('PYTHONIOENCODING', 'utf-8')
    sys.exit(main())
