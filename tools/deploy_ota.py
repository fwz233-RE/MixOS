#!/usr/bin/env python3
"""Update the ESP32-S3 over USB in one command, through the CM5, over SSH.

This is the routine path once the device runs the A/B layout. Nothing is
erased until the whole image arrived and its SHA-256 matched, the running slot
is never touched, and a build that does not prove itself is rolled back by the
bootloader. There is no backup ceremony, no ROM download mode, no esptool and
no 4 MiB font rewrite, because none of those are involved.

    export MIXOS_SSH_PASSWORD=...
    tools/deploy_ota.py --image firmware/esp32s3/build/mixos_esp32s3.bin

Compare with tools/deploy_display.py, which is the once-per-device serial
operation that installs the bootloader, the A/B partition table and the font.

Exit status is the update's status: this tool runs to completion instead of
submitting a detached job, because an interrupted OTA leaves the running build
in place by construction.
"""
import argparse
import base64
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import time
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parent))

from deploy_display import digest, source_digest_matches  # noqa: E402
import ota_esp  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
# The CDC interface published by this device's udev rule and mixosd unit.
DEVICE = '/dev/serial/by-id/usb-TypixDeck_TypixDeck_UAC+CDC_TD0720-if03'
SERVICE = 'mixosd.service'
# Every A/B application slot in firmware/esp32s3/partitions.csv.
SLOT_BYTES = 0x1F0000
# What travels to the Pi. The updater imports protocol.py and mixosd.py from a
# sibling linux/ directory, so the package mirrors the repository layout.
PAYLOAD = {'tools/ota_esp.py': 'tools/ota_esp.py',
           'linux/protocol.py': 'linux/protocol.py',
           'linux/mixosd.py': 'linux/mixosd.py'}


def build_check(image):
    """Refuse to push anything the local build report does not vouch for.

    An OTA cannot damage the device, but it can waste a trip by installing an
    image whose sources were edited after the last cross-build, which is the
    mistake that is easy to make and hard to see afterwards.
    """
    path = ROOT / 'build/esp32s3/font-app-build.json'
    report = json.loads(path.read_text(encoding='utf-8'))
    if (report.get('status') != 'cross-built' or report.get('target') != 'esp32s3'
            or report.get('build_app', {}).get('sha256') != digest(image)
            or not report.get('sources')):
        raise ValueError('This image is not the current cross-build; rebuild before updating')
    if not report.get('ota_capable'):
        raise ValueError('The build report says this firmware has no A/B slots; '
                         'rebuild against the A/B partitions.csv')
    for source in report['sources']:
        name = source['path'].replace('\\', '/').rsplit('/', 1)[-1]
        if not source_digest_matches(ROOT / 'firmware/esp32s3/main' / name, source['sha256']):
            raise ValueError('ESP source changed since the recorded build: ' + name)
    return {'mode': 'usb_ota', 'app_sha256': digest(image),
            'app_partition_bytes': report.get('app_partition_bytes'),
            'partition_layout': report.get('partition_layout'),
            'build_report_sha256': digest(path)}


def remote_command(package, device, timeout):
    """Stop the host service, update as the ordinary user, restart it either way.

    mixosd holds the CDC node, so it has to stand aside; the updater itself
    never runs as root, and the service comes back even when the update fails.
    """
    update = shlex.join(['/usr/bin/python3', '-I', '-u', package + '/tools/ota_esp.py',
                         '--device', device, '--image', package + '/app.bin',
                         '--timeout', str(timeout)])
    return ('set -u\n'
            'systemctl stop ' + shlex.quote(SERVICE) + '\n'
            'runuser -u pi -- ' + update + '\n'
            'status=$?\n'
            'systemctl start ' + shlex.quote(SERVICE) + '\n'
            'exit $status\n')


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--image', type=Path, default=ROOT / 'firmware/esp32s3/build/mixos_esp32s3.bin',
                   help='application .bin to install (default: the current cross-build)')
    p.add_argument('--host', default='192.168.1.22')
    p.add_argument('--device', default=DEVICE, help='CDC node on the Pi')
    p.add_argument('--timeout', type=float, default=60.0, help='per-step timeout on the Pi')
    p.add_argument('--dry-run', action='store_true',
                   help='validate the image and the build report, then stop')
    p.add_argument('--skip-build-check', action='store_true',
                   help='install an image the local build report does not describe')
    a = p.parse_args(argv)

    try:
        image, image_digest = ota_esp.inspect_image(a.image)
    except ota_esp.UpdateError as exc:
        p.error(str(exc))
    if len(image) > SLOT_BYTES:
        p.error(f'{len(image)} bytes does not fit a {SLOT_BYTES} byte slot')
    provenance = {'mode': 'unverified_image', 'app_sha256': image_digest.hex()}
    if not a.skip_build_check:
        provenance = build_check(a.image)

    print(f'image  {a.image}')
    print(f'size   {len(image)} bytes of {SLOT_BYTES} ({len(image) * 100 // SLOT_BYTES}% of a slot)')
    print(f'sha256 {image_digest.hex()}')
    if not a.device.startswith('/dev/serial/by-id/'):
        p.error('use an explicit stable /dev/serial/by-id/ identity')
    if a.dry_run:
        print('DRY RUN: nothing was uploaded and no device was opened.')
        return 0

    password = os.environ.get('MIXOS_SSH_PASSWORD')
    if not password:
        raise ValueError('Set MIXOS_SSH_PASSWORD for the existing pi account')
    options = ['-o', 'ConnectionAttempts=1', '-o', 'ConnectTimeout=20', '-o', 'ServerAliveInterval=10',
               '-o', 'ServerAliveCountMax=3', '-o', 'StrictHostKeyChecking=yes',
               '-o', 'PreferredAuthentications=password', '-o', 'PubkeyAuthentication=no',
               '-o', 'NumberOfPasswordPrompts=1']
    job = 'mixos-ota-' + time.strftime('%Y%m%d-%H%M%S', time.gmtime())
    staging = '/home/pi/' + job

    with tempfile.TemporaryDirectory(prefix='mixos-ota-') as temporary:
        temp = Path(temporary)
        helper = temp / ('askpass.cmd' if os.name == 'nt' else 'askpass.sh')
        helper.write_text('@echo off\necho %MIXOS_SSH_PASSWORD%\n' if os.name == 'nt' else
                          '#!/bin/sh\nprintf "%s\\n" "$MIXOS_SSH_PASSWORD"\n')
        helper.chmod(0o700)
        env = dict(os.environ, SSH_ASKPASS=str(helper), SSH_ASKPASS_REQUIRE='force', DISPLAY='unused:0')

        def ssh(script, sudo=False, timeout=180, check=True):
            encoded = base64.b64encode(script.encode()).decode()
            command = 'echo ' + encoded + ' | base64 -d | bash'
            if sudo:
                command = "sudo -S -p '' bash -c " + shlex.quote(command)
            result = subprocess.run(['ssh', '-T'] + options + ['pi@' + a.host, command],
                                    env=env, input=(password + '\n') if sudo else '',
                                    text=True, timeout=timeout)
            if check and result.returncode:
                raise RuntimeError(f'SSH operation exited {result.returncode}')
            return result.returncode

        files = {name: ROOT / source for name, source in PAYLOAD.items()}
        files['app.bin'] = a.image
        hashes = {name: digest(path) for name, path in files.items()}
        archive = temp / 'ota.zip'
        with zipfile.ZipFile(archive, 'w', zipfile.ZIP_DEFLATED) as z:
            for name, path in files.items():
                z.write(path, name)

        ssh('set -eu\nmkdir -m 700 ' + shlex.quote(staging))
        uploaded = subprocess.run(['scp'] + options + [str(archive), 'pi@' + a.host + ':' + staging + '/package.zip'],
                                  env=env, timeout=300)
        if uploaded.returncode:
            raise RuntimeError('Upload failed; the device was never opened')
        bootstrap = ('from pathlib import Path; import hashlib,zipfile; '
                     f'p=Path({staging!r}); z=p/"package.zip"; '
                     f'assert hashlib.sha256(z.read_bytes()).hexdigest()=={digest(archive)!r}; '
                     'zipfile.ZipFile(z).extractall(p); '
                     f'assert all(hashlib.sha256((p/n).read_bytes()).hexdigest()==h '
                     f'for n,h in {hashes!r}.items())')
        ssh(shlex.join(['python3', '-c', bootstrap]))

        print(f'updating over USB via {a.host}; the running slot stays intact')
        status = ssh(remote_command(staging, a.device, a.timeout), sudo=True,
                     timeout=max(600.0, a.timeout * 8), check=False)

        receipt = ROOT / 'build/deploy' / (job + '.json')
        receipt.parent.mkdir(parents=True, exist_ok=True)
        receipt.write_text(json.dumps({'job': job, 'host': a.host, 'staging': staging,
                                       'device': a.device, 'hashes': hashes,
                                       'state': 'installed' if not status else 'failed',
                                       'artifact_provenance': provenance}, indent=2) + '\n')
        if status:
            print(f'\ndeploy_ota: the update did not complete (exit {status}). The device is '
                  f'still running its previous build; {SERVICE} was restarted.', file=sys.stderr)
            print(f'deploy_ota: if it timed out waiting for OTA_READY, the running firmware '
                  f'predates USB updates. Install it once with '
                  f'tools/deploy_display.py --stage --migrate, then this tool works from then on.',
                  file=sys.stderr)
            print('receipt ' + str(receipt), file=sys.stderr)
            return status
        print('INSTALLED: the new build is running and confirms itself after about 20 s of '
              'health. A build that crashes before then is rolled back by the bootloader.')
        print('receipt ' + str(receipt))
    return 0


if __name__ == '__main__':
    sys.exit(main())
