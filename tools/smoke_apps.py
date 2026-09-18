#!/usr/bin/env python3
"""Start each interface on the device in a pseudo-terminal and see what it does.

`mixosd` starts these behind a USB link with a screen on the other end, which
is a slow way to find out that a module does not import. This drives the same
programs through an ordinary pseudo-terminal over SSH: every one is started,
given a moment to draw, sent the key that quits it, and judged on whether it
drew anything and whether it left a traceback behind.

It is not a substitute for pressing the buttons. It is what makes pressing the
buttons worth doing.

    $env:MIXOS_SSH_PASSWORD='...'
    py -3.12 tools/smoke_apps.py --host 192.168.1.22
"""
from __future__ import annotations

import argparse
import getpass
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from deploy_models import Remote                              # noqa: E402

APP_DIR = '/usr/local/lib/mixos/apps'

# Runs on the device, under its own python3. A pseudo-terminal is the point:
# every one of these refuses to draw on something that is not a terminal, which
# is what keeps a stray `python3 app.py` over SSH from painting escape codes
# into somebody's session.
DRIVER = r'''
import os, pty, select, signal, sys, time

program, keys, seconds = sys.argv[1], sys.argv[2].encode(), float(sys.argv[3])
environment = dict(os.environ, TERM='mixos', MIXOS_COLS='64', MIXOS_ROWS='22',
                   LANG='C.UTF-8')
pid, fd = pty.fork()
if pid == 0:
    os.execve(program, [program], environment)

import fcntl, struct, termios
fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack('HHHH', 22, 64, 0, 0))

output, deadline, sent = b'', time.monotonic() + seconds, False
while time.monotonic() < deadline:
    ready, _, _ = select.select([fd], [], [], 0.2)
    if ready:
        try:
            chunk = os.read(fd, 65536)
        except OSError:
            break
        if not chunk:
            break
        output += chunk
    if not sent and time.monotonic() > deadline - seconds / 2:
        os.write(fd, keys)
        sent = True

try:
    os.kill(pid, signal.SIGTERM)
except ProcessLookupError:
    pass
_, status = os.waitpid(pid, os.WNOHANG)
os.close(fd)

text = output.decode('utf-8', 'replace')
print('BYTES', len(output))
print('ALTSCREEN', '\x1b[?1049h' in text)
print('RESTORED', '\x1b[?1049l' in text)
for marker in ('Traceback', 'ModuleNotFoundError', 'ImportError', 'SyntaxError',
               'AttributeError', 'is not installed'):
    if marker in text:
        print('PROBLEM', marker)
print('---- what it drew, escapes stripped ----')
import re
visible = re.sub(r'\x1b\[[0-9;?]*[A-Za-z]', ' ', text)
visible = re.sub(r'[\x00-\x08\x0b-\x1f\x7f]', '', visible)
print(' '.join(visible.split())[:900])
'''

CASES = [
    ('translate', 'q', 8.0, 'the translation interface'),
    ('notes', 'q', 8.0, 'the notes list'),
    ('agent', ' ', 5.0, 'the empty Agent screen'),
]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--host', default='192.168.1.22')
    parser.add_argument('--user', default='pi')
    parser.add_argument('--only', default=None, help='run one case by name')
    args = parser.parse_args()

    password = os.environ.get('MIXOS_SSH_PASSWORD') or getpass.getpass(
        f'{args.user}@{args.host} password: ')
    if not password:
        raise SystemExit('No password supplied; set MIXOS_SSH_PASSWORD or type one.')

    failures = 0
    with tempfile.TemporaryDirectory(prefix='mixos-smoke-') as temporary:
        remote = Remote(args.host, args.user, password, Path(temporary))
        remote.run('mkdir -p ~/mixos-smoke && cat > ~/mixos-smoke/driver.py',
                   data=DRIVER.encode('utf-8'))
        for name, keys, seconds, description in CASES:
            if args.only and args.only != name:
                continue
            print(f'\n=== {name} — {description} ===')
            answer = remote.text(
                f'python3 ~/mixos-smoke/driver.py {APP_DIR}/{name} '
                f'{keys!r} {seconds} 2>&1',
                timeout=int(seconds) + 60, check=False)
            print(answer)
            if 'PROBLEM' in answer or 'BYTES 0' in answer:
                failures += 1
            elif 'ALTSCREEN True' not in answer:
                print('  (it drew, but never entered the alternate screen)')
                failures += 1

    print('\nAll three drew something and none left a traceback.'
          if not failures else f'\n{failures} of the interfaces did not start cleanly.')
    return 1 if failures else 0


if __name__ == '__main__':
    os.environ.setdefault('PYTHONIOENCODING', 'utf-8')
    sys.exit(main())
