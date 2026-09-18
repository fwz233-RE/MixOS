#!/usr/bin/env python3
"""Read-only inventory of the CM5 that will run the AI deck's Linux side.

Everything the deck plan assumes about that machine - how much eMMC is left for
a 2.6 GB model, how much RAM the language model may use, what the ESP32's USB
audio device is actually called, whether ``pi`` may talk to NetworkManager, and
which compilers exist for building term-ime - is a question about one specific
device. Guessing any of it wastes a deployment.

This tool asks, and only asks. It runs a fixed list of commands, never uses
sudo, never writes to the device, and stores the answers locally:

    $env:MIXOS_SSH_PASSWORD='...'
    python tools/inventory_pi.py --host 192.168.1.22

The raw answers land in ``build/inventory/pi-<timestamp>.json`` and a readable
summary is written into ``docs/AI_DECK.md`` between explicit markers, so a
rerun updates the document without disturbing the prose around it.
"""
from __future__ import annotations

import argparse
import getpass
import json
import os
import shlex
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOC = ROOT / 'docs/AI_DECK.md'
BEGIN = '<!-- inventory:begin -->'
END = '<!-- inventory:end -->'

# name -> shell command. Every one of these reads; none of them changes state.
PROBES: dict[str, str] = {
    'hostname': 'hostname',
    'os': 'cat /etc/os-release',
    'kernel': 'uname -a',
    'model': 'cat /proc/device-tree/model 2>/dev/null | tr -d "\\0"',
    'cpuinfo': 'lscpu 2>/dev/null || cat /proc/cpuinfo',
    'memory': 'free -m',
    'swap': 'cat /proc/swaps',
    'disk': 'df -h -x tmpfs -x devtmpfs',
    'blockdevices': 'lsblk -o NAME,SIZE,TYPE,MOUNTPOINT 2>/dev/null',
    'home_free': 'df -B1 --output=avail "$HOME" | tail -1',
    'playback_devices': 'aplay -l 2>&1',
    'capture_devices': 'arecord -l 2>&1',
    'alsa_cards': 'cat /proc/asound/cards 2>&1',
    'pipewire': 'systemctl --user is-active pipewire 2>&1; pactl info 2>&1 | head -20',
    'usb': 'lsusb 2>&1',
    'nmcli_version': 'nmcli --version 2>&1',
    'nmcli_permissions': 'nmcli general permissions 2>&1',
    'nmcli_radio': 'nmcli radio 2>&1',
    'wifi_status': 'nmcli -t -f ACTIVE,SSID,SIGNAL device wifi 2>&1 | head -10',
    'groups': 'id',
    'compilers': 'for t in gcc g++ cc cmake make ninja pkg-config git rsync; do '
                 'printf "%s: " "$t"; command -v "$t" >/dev/null && "$t" --version 2>&1 | head -1 '
                 '|| echo "not installed"; done',
    'python': 'python3 --version 2>&1; python3 -c "import sys;print(sys.executable)" 2>&1; '
              'python3 -m venv --help >/dev/null 2>&1 && echo "venv: available" || echo "venv: missing"',
    'python_packages': 'python3 -m pip list 2>/dev/null | head -40',
    'systemd_units': 'systemctl list-units --type=service --state=running --no-pager --no-legend 2>&1 | head -30',
    'mixosd': 'systemctl is-active mixosd 2>&1; systemctl is-enabled mixosd 2>&1',
    'listening_ports': 'ss -ltn 2>&1 | head -20',
    'time': 'date -Is; timedatectl 2>&1 | head -8',
    'term_ime': 'command -v term-ime >/dev/null && term-ime --version 2>&1 || echo "term-ime: not installed"',
    'existing_models': 'ls -la "$HOME/.cache/huggingface" 2>&1 | head -20; '
                       'command -v litert-lm >/dev/null && echo "litert-lm: present" || echo "litert-lm: absent"',
}

# Values a human wants at a glance, pulled out of the raw answers below.
HEADLINES = [
    ('Host / model', ('hostname', 'model')),
    ('OS and kernel', ('os', 'kernel')),
    ('Memory', ('memory', 'swap')),
    ('Disk', ('disk', 'home_free')),
    ('ESP32 USB audio', ('alsa_cards', 'playback_devices', 'capture_devices')),
    ('NetworkManager access for pi', ('groups', 'nmcli_version', 'nmcli_permissions')),
    ('Build toolchain', ('compilers', 'python')),
    ('AI stack already present', ('existing_models', 'term_ime', 'listening_ports')),
]


def run_probes(host: str, user: str, password: str, timeout: int) -> dict[str, object]:
    """Ask every question in one SSH session and return the raw answers."""
    # One script, one connection: a per-probe connection would multiply a slow
    # Wi-Fi link by thirty.
    parts = []
    for name, command in PROBES.items():
        parts.append(f'printf "\\n===MIXOS===%s\\n" {shlex.quote(name)}')
        parts.append(f'{{ {command} ; }} 2>&1 || true')
    script = 'set +e\n' + '\n'.join(parts) + '\n'

    with tempfile.TemporaryDirectory(prefix='mixos-ssh-') as temporary:
        helper = Path(temporary) / ('askpass.cmd' if os.name == 'nt' else 'askpass.sh')
        helper.write_text('@echo off\necho %MIXOS_SSH_PASSWORD%\n' if os.name == 'nt' else
                          '#!/bin/sh\nprintf "%s\\n" "$MIXOS_SSH_PASSWORD"\n')
        helper.chmod(0o700)
        env = dict(os.environ, MIXOS_SSH_PASSWORD=password, SSH_ASKPASS=str(helper),
                   SSH_ASKPASS_REQUIRE='force', DISPLAY=os.environ.get('DISPLAY') or 'unused:0')
        options = ['-o', 'ConnectionAttempts=1', '-o', 'ConnectTimeout=20',
                   '-o', 'ServerAliveInterval=10', '-o', 'ServerAliveCountMax=6',
                   '-o', 'StrictHostKeyChecking=accept-new',
                   '-o', 'PreferredAuthentications=password',
                   '-o', 'PubkeyAuthentication=no', '-o', 'NumberOfPasswordPrompts=1']
        import base64
        encoded = base64.b64encode(script.encode()).decode()
        result = subprocess.run(['ssh', '-T'] + options + [f'{user}@{host}',
                                'echo ' + encoded + ' | base64 -d | bash'],
                                env=env, capture_output=True, text=True,
                                encoding='utf-8', errors='replace', timeout=timeout)
    if result.returncode:
        raise RuntimeError(f'SSH inventory exited {result.returncode}: '
                           f'{(result.stderr or result.stdout)[-500:]}')

    answers: dict[str, str] = {}
    current = None
    for line in result.stdout.splitlines():
        if line.startswith('===MIXOS==='):
            current = line[len('===MIXOS==='):].strip()
            answers[current] = ''
            continue
        if current is not None:
            answers[current] += line + '\n'
    return {name: answers.get(name, '').strip() for name in PROBES}


def render(host: str, user: str, answers: dict[str, object], when: str) -> str:
    """A readable summary, with every answer quoted exactly as the device gave it."""
    lines = [BEGIN,
             f'*Collected {when} from `{user}@{host}` by `tools/inventory_pi.py`. '
             f'Read-only: no sudo, no writes, no installs.*',
             '']
    for title, names in HEADLINES:
        lines.append(f'### {title}')
        lines.append('')
        for name in names:
            text = str(answers.get(name, '')).strip()
            lines.append(f'`{name}`')
            lines.append('')
            lines.append('```')
            lines.append(text if text else '(no output)')
            lines.append('```')
            lines.append('')
    remaining = [n for n in PROBES if not any(n in names for _, names in HEADLINES)]
    lines.append('### Everything else asked')
    lines.append('')
    for name in remaining:
        text = str(answers.get(name, '')).strip()
        lines.append(f'<details><summary><code>{name}</code></summary>')
        lines.append('')
        lines.append('```')
        lines.append(text if text else '(no output)')
        lines.append('```')
        lines.append('')
        lines.append('</details>')
        lines.append('')
    lines.append(END)
    return '\n'.join(lines)


def splice(document: str, block: str) -> str:
    """Replace the inventory block, leaving the surrounding prose untouched."""
    if BEGIN in document and END in document:
        head = document.split(BEGIN, 1)[0]
        tail = document.split(END, 1)[1]
        return head + block + tail
    return document.rstrip('\n') + '\n\n' + block + '\n'


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--host', default='192.168.1.22')
    parser.add_argument('--user', default='pi')
    parser.add_argument('--timeout', type=int, default=180)
    parser.add_argument('--print-only', action='store_true',
                        help='show the summary without touching docs/AI_DECK.md')
    args = parser.parse_args()

    password = os.environ.get('MIXOS_SSH_PASSWORD') or getpass.getpass(f'{args.user}@{args.host} password: ')
    if not password:
        raise SystemExit('No password supplied; set MIXOS_SSH_PASSWORD or type one.')

    when = time.strftime('%Y-%m-%d %H:%M:%S %Z')
    answers = run_probes(args.host, args.user, password, args.timeout)

    raw_dir = ROOT / 'build/inventory'
    raw_dir.mkdir(parents=True, exist_ok=True)
    raw = raw_dir / ('pi-' + time.strftime('%Y%m%d-%H%M%S') + '.json')
    raw.write_text(json.dumps({'host': args.host, 'user': args.user, 'collected': when,
                               'answers': answers}, ensure_ascii=False, indent=2) + '\n',
                   encoding='utf-8')

    block = render(args.host, args.user, answers, when)
    if args.print_only:
        print(block)
        return 0
    DOC.parent.mkdir(parents=True, exist_ok=True)
    existing = DOC.read_text(encoding='utf-8') if DOC.exists() else ''
    DOC.write_text(splice(existing, block), encoding='utf-8')
    print(f'Raw answers: {raw.relative_to(ROOT)}')
    print(f'Summary written into: {DOC.relative_to(ROOT)}')
    return 0


if __name__ == '__main__':
    os.environ.setdefault('PYTHONIOENCODING', 'utf-8')
    sys.exit(main())
