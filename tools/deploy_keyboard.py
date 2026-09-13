#!/usr/bin/env python3
"""Stage or launch the verified keyboard build over trusted native OpenSSH.

Default operations never switch USB mode. --start requires the exact observed
STM32 ROM serial; the single-use worker backs up and verifies before leaving DFU.
"""
import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def package_installer(staging, package, hashes, *, display=False):
    """Generate a root installer; user-controlled bytes are hashed before publishing."""
    if not isinstance(hashes, dict) or not hashes:
        raise ValueError('Missing artifact hashes')
    for name, digest in hashes.items():
        if (not isinstance(name, str) or name.startswith('/') or '\\' in name
                or any(part in ('', '.', '..') for part in name.split('/'))
                or not re.fullmatch(r'[0-9a-f]{64}', digest)):
            raise ValueError('Unsafe artifact name or hash')
    return f'''from pathlib import Path
import hashlib, os, pwd, stat
src = Path({staging!r})
dst = Path({package!r})
expected = {hashes!r}
def trusted_mkdir(path):
    if not path.exists():
        trusted_mkdir(path.parent)
        path.mkdir(mode=0o755)
    if path.resolve(strict=True) != path:
        raise ValueError('Symlink/noncanonical trusted directory: ' + str(path))
    info = path.stat()
    if info.st_uid != 0 or info.st_mode & 0o022 or not stat.S_ISDIR(info.st_mode):
        raise ValueError('Untrusted root package directory: ' + str(path))
    if path != path.parent:
        trusted_mkdir(path.parent)
data = {{n: (src / n).read_bytes() for n in expected}}
if not all(hashlib.sha256(data[n]).hexdigest() == h for n, h in expected.items()):
    raise ValueError('Staged artifact changed')
trusted_mkdir(dst.parent)
dst.mkdir(mode=0o755)
for name, content in data.items():
    path = dst / name
    path.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
    with path.open('xb') as output:
        output.write(content)
        output.flush()
        os.fsync(output.fileno())
    path.chmod(0o644)
(dst / 'worker.log').touch(mode=0o600, exist_ok=False)
if {display!r}:
    if any(not (p.stat().st_mode & 0o001) for p in (dst, *dst.parents)):
        raise ValueError('Display package ancestors must be traversable by the pi worker')
    account = pwd.getpwnam('pi')
    work = dst / 'work'
    work.mkdir(mode=0o700)
    os.chown(work, account.pw_uid, account.pw_gid)
else:
    trusted_mkdir(Path('/var/lib/mixos/keyboard-flash'))
for directory in sorted([p for p in dst.rglob('*') if p.is_dir()] + [dst, dst.parent], key=lambda p: len(p.parts), reverse=True):
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--stage', action='store_true')
    mode.add_argument('--inventory', type=Path, metavar='RECEIPT')
    mode.add_argument('--start', type=Path, metavar='RECEIPT')
    mode.add_argument('--status', type=Path, metavar='RECEIPT')
    parser.add_argument('--serial', help='Exact 12-hex ROM serial obtained after physical DFU entry')
    parser.add_argument('--host', default='192.168.1.22')
    args = parser.parse_args()
    if args.start and not re.fullmatch(r'[0-9A-Fa-f]{12}', args.serial or ''):
        parser.error('--start requires the exact observed --serial')
    password = os.environ.get('MIXOS_SSH_PASSWORD')
    if not password:
        parser.error('Set MIXOS_SSH_PASSWORD for the pi account')
    options = ['-o', 'ConnectionAttempts=1', '-o', 'ConnectTimeout=20', '-o', 'ServerAliveInterval=10',
               '-o', 'ServerAliveCountMax=3', '-o', 'StrictHostKeyChecking=yes',
               '-o', 'PreferredAuthentications=password', '-o', 'PubkeyAuthentication=no',
               '-o', 'NumberOfPasswordPrompts=1']
    with tempfile.TemporaryDirectory(prefix='mixos-keyboard-') as temporary:
        helper = Path(temporary) / ('askpass.cmd' if os.name == 'nt' else 'askpass.sh')
        helper.write_text('@echo off\necho %MIXOS_SSH_PASSWORD%\n' if os.name == 'nt' else
                          '#!/bin/sh\nprintf "%s\\n" "$MIXOS_SSH_PASSWORD"\n')
        helper.chmod(0o700)
        env = dict(os.environ, SSH_ASKPASS=str(helper), SSH_ASKPASS_REQUIRE='force', DISPLAY='unused:0')

        def ssh(script, sudo=False):
            encoded = base64.b64encode(script.encode()).decode()
            command = 'echo ' + encoded + ' | base64 -d | bash'
            if sudo:
                command = "sudo -S -p '' bash -c " + shlex.quote(command)
            result = subprocess.run(['ssh', '-T'] + options + ['pi@' + args.host, command], env=env,
                                    input=password + '\n' if sudo else '', text=True, timeout=120)
            if result.returncode:
                raise RuntimeError('Remote operation failed; inspect before retrying any start')

        if args.stage:
            manifest_path = ROOT / 'build/keyboard/manifest.json'
            manifest = json.loads(manifest_path.read_text())
            # QMK's .bin includes a DFU suffix; stage only the verified raw ELF payload.
            image = ROOT / 'build/keyboard/keebdeck_6r11c_default.raw.bin'
            if (manifest.get('target_build_verified') is not True or
                    manifest.get('qmk_commit') != 'a63fd7f01cdabd9ce85bb09ae2b573fd3b8e60aa' or
                    manifest.get('hashes', {}).get(image.name) != sha(image)):
                raise ValueError('A verified pinned target build and matching default image are required')
            from flash_keyboard_on_pi import validate_image
            validate_image(image.read_bytes(), sha(image))
            job = 'mixos-keyboard-' + time.strftime('%Y%m%d-%H%M%S', time.gmtime())
            staging = '/home/pi/' + job
            files = {'image.bin': image, 'manifest.json': manifest_path,
                     'flash_keyboard_on_pi.py': ROOT / 'tools/flash_keyboard_on_pi.py'}
            hashes = {name: sha(path) for name, path in files.items()}
            ssh('set -eu\nmkdir -m 700 ' + staging)
            for name, path in files.items():
                result = subprocess.run(['scp'] + options + [str(path), 'pi@' + args.host + ':' + staging + '/' + name],
                                        env=env, timeout=180)
                if result.returncode:
                    raise RuntimeError('Upload failed; no worker started')
            verify = ("from pathlib import Path; import hashlib; " + f"p=Path({staging!r}); expected={hashes!r}; " +
                      "assert all(hashlib.sha256((p/n).read_bytes()).hexdigest()==h for n,h in expected.items())")
            ssh(shlex.join(['python3', '-c', verify]))
            receipt = ROOT / 'build/deploy' / (job + '.json')
            receipt.parent.mkdir(parents=True, exist_ok=True)
            receipt.write_text(json.dumps({'host': args.host, 'job': job, 'staging': staging,
                                           'hashes': hashes, 'state': 'staged_only'}, indent=2) + '\n')
            print('STAGED_ONLY: ' + str(receipt), flush=True)
            return
        receipt = json.loads((args.start or args.status or args.inventory).read_text())
        job = receipt['job']
        if not re.fullmatch(r'mixos-keyboard-\d{8}-\d{6}', job) or receipt['host'] != args.host:
            raise ValueError('Invalid receipt/host')
        staging = '/home/pi/' + job
        package = '/var/lib/mixos/keyboard-packages/' + job
        workdir = '/var/lib/mixos/keyboard-flash/' + job
        if args.inventory:
            ssh(shlex.join(['python3', '-I', staging + '/flash_keyboard_on_pi.py']))
            return
        if args.status:
            ssh('systemctl show ' + job + '.service -p LoadState -p ActiveState -p SubState -p Result -p ExecMainStatus; '
                'tail -n 50 ' + package + '/worker.log; '
                'test ! -f ' + workdir + '/audit.jsonl || tail -n 20 ' + workdir + '/audit.jsonl', sudo=True)
            return
        # Install exactly the receipt's reviewed bytes into a new root-owned
        # package. The root worker uses -I and only the Python standard library.
        if set(receipt['hashes']) != {'image.bin', 'manifest.json', 'flash_keyboard_on_pi.py'}:
            raise ValueError('Unexpected keyboard package contents')
        installer = package_installer(staging, package, receipt['hashes'])
        ssh(shlex.join(['python3', '-I', '-c', installer]), sudo=True)
        command = ['systemd-run', '--unit', job, '--property=Type=exec', '--property=Restart=no',
                   '--property=RuntimeMaxSec=300', '--property=UMask=0077',
                   '--property=StandardOutput=append:' + package + '/worker.log', '--property=StandardError=inherit',
                   '--', '/usr/bin/python3', '-I', package + '/flash_keyboard_on_pi.py',
                   '--image', package + '/image.bin', '--sha256', receipt['hashes']['image.bin'],
                   '--serial', args.serial, '--usb-path', '5-1.1', '--mcu', 'STM32F042G6U6',
                   '--workdir', workdir, '--execute', '--leave']
        print('Launching once: ' + job + '. Inspect status after disconnect; do not repeat start.', flush=True)
        ssh(shlex.join(command), sudo=True)
        print('SUBMITTED: this is not flash success; inspect the full readback result.', flush=True)


if __name__ == '__main__':
    main()
