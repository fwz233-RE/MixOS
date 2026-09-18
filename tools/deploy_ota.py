#!/usr/bin/env python3
"""Optional SSH transport for the same Linux-native ESP update job/status API.

A validated release directory (app.bin + manifest.json) is the deployable unit.
Use --package for a release without a source checkout, or --image --manifest to
package a checked local build. --image --dry-run retains the old local build
consistency check. --skip-build-check never replaces the required release
manifest for an actual apply. The worker is systemd-managed and survives SSH
loss; this wrapper never stops services, opens serial, or interprets firmware
success itself. No ROM/bootstrap operation is selected by a timeout.
"""
import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import time
import uuid
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parent))
from deploy_display import digest, source_digest_matches
import ota_esp
import mixos_esp_update as native

ROOT = Path(__file__).resolve().parents[1]
DEVICE = native.DEVICE
SERVICE = native.SERVICE
SLOT_BYTES = native.SLOT_SIZE
PAYLOAD = {name: name for name in native.RUNNER_FILES}
# Kept for the existing installation/package compatibility check. The serial
# transport itself is now standalone and imports neither daemon nor netctl.
PAYLOAD.update({'linux/netctl.py': 'linux/netctl.py', 'linux/mixosd.py': 'linux/mixosd.py'})


def build_check(image):
    """Attest local source/image consistency, independently of runtime safety."""
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


def remote_command(package, device, timeout, allow_replace_baseline=False):
    """Ordinary-user submit/follow only; systemd owns the worker's lifetime."""
    command = ['/usr/bin/python3', '-I', '-u', package + '/tools/mixos_esp_update.py',
               'apply', '--package', package, '--device', device,
               '--timeout', str(timeout), '--wait']
    if allow_replace_baseline:
        command.append('--allow-replace-baseline')
    return shlex.join(command) + '\n'


def status_command(package, job):
    if not native.JOB_ID.fullmatch(job):
        raise ValueError('invalid native job ID')
    return shlex.join(['/usr/bin/python3', '-I', '-u', package + '/tools/mixos_esp_update.py',
                       'status', '--job', job]) + '\n'


def parse_result(output):
    """The last native JSON result is authoritative, including queued/unknown."""
    found = None
    for line in (output or '').splitlines():
        try:
            value = json.loads(line)
        except ValueError:
            continue
        if isinstance(value, dict) and all(k in value for k in ('firmware', 'service', 'error')):
            found = value
    if found is None:
        return dict(state='unknown', durable=False, firmware={'state': 'unknown'},
                    service={'state': 'unknown'}, error={'code': 'transport-unknown',
                    'message': 'SSH returned no native result; the background job may still be running. Use status; do not reflash blindly.'})
    return found


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--image', type=Path, default=ROOT / 'firmware/esp32s3/build/mixos_esp32s3.bin')
    p.add_argument('--manifest', type=Path, help='v2 manifest produced from the effective build configuration')
    p.add_argument('--package', type=Path, help='portable app.bin + manifest.json release directory')
    p.add_argument('--host', default='192.168.1.22')
    p.add_argument('--device', default=DEVICE)
    p.add_argument('--timeout', type=float, default=60.0)
    p.add_argument('--dry-run', action='store_true', help='validate locally; no network, serial or services')
    p.add_argument('--skip-build-check', action='store_true', help='skip source consistency only, not manifest validation')
    p.add_argument('--allow-replace-baseline', action='store_true')
    p.add_argument('--status', metavar='JOB', help='read the native durable status of a previous job')
    p.add_argument('--remote-package', help='remote release directory from the prior transport receipt')
    a = p.parse_args(argv)
    try:
        native.validate_device_path(a.device)
        native.finite_seconds(a.timeout, 'timeout', 0.01, 120)
        if a.status:
            if not a.remote_package or not a.remote_package.startswith('/home/pi/'):
                raise ValueError('--status needs the prior --remote-package under /home/pi/')
            status_command(a.remote_package, a.status)
            provenance = None
        elif a.package:
            release = native.load_release(a.package)
            provenance = release.manifest['provenance']
        else:
            image, sha = ota_esp.inspect_image(a.image)
            if not a.dry_run:
                ota_esp.describe_image(image)
            if len(image) > SLOT_BYTES:
                raise ValueError(f'{len(image)} bytes does not fit a {SLOT_BYTES} byte slot')
            provenance = {'mode': 'unverified_image', 'app_sha256': sha.hex()}
            if not a.skip_build_check:
                provenance = build_check(a.image)
            if not a.dry_run and not a.manifest:
                raise ValueError('actual apply requires --manifest or --package; a raw image cannot establish a safe release')
        # A supplied manifest must be checked even for dry-run.
        if not a.status and not a.package and a.manifest:
            with tempfile.TemporaryDirectory(prefix='mixos-ota-validate-') as temporary:
                temp = Path(temporary)
                (temp / 'app.bin').write_bytes(image)
                (temp / 'manifest.json').write_bytes(a.manifest.read_bytes())
                release = native.load_release(temp)
    except (OSError, ValueError, ota_esp.UpdateError, native.JobError) as exc:
        p.error(str(exc))
    if a.dry_run:
        print(json.dumps(dict(dry_run=True, artifact_provenance=provenance,
                              firmware={'state': 'not-started'}, service={'state': 'untouched'},
                              error=None), sort_keys=True))
        return 0

    password = os.environ.get('MIXOS_SSH_PASSWORD')
    if not password:
        raise ValueError('Set MIXOS_SSH_PASSWORD for the existing pi account')
    options = ['-o', 'ConnectionAttempts=1', '-o', 'ConnectTimeout=20', '-o', 'ServerAliveInterval=10',
               '-o', 'ServerAliveCountMax=3', '-o', 'StrictHostKeyChecking=yes',
               '-o', 'PreferredAuthentications=password', '-o', 'PubkeyAuthentication=no',
               '-o', 'NumberOfPasswordPrompts=1']
    transfer = 'mixos-ota-' + time.strftime('%Y%m%d-%H%M%S', time.gmtime()) + '-' + uuid.uuid4().hex[:8]
    staging = a.remote_package if a.status else '/home/pi/' + transfer
    hashes = {}
    with tempfile.TemporaryDirectory(prefix='mixos-ota-') as temporary:
        temp = Path(temporary)
        helper = temp / ('askpass.cmd' if os.name == 'nt' else 'askpass.sh')
        helper.write_text('@echo off\necho %MIXOS_SSH_PASSWORD%\n' if os.name == 'nt' else
                          '#!/bin/sh\nprintf "%s\\n" "$MIXOS_SSH_PASSWORD"\n')
        helper.chmod(0o700)
        env = dict(os.environ, SSH_ASKPASS=str(helper), SSH_ASKPASS_REQUIRE='force', DISPLAY='unused:0')

        def ssh(script, timeout=180, check=True):
            encoded = base64.b64encode(script.encode()).decode()
            command = 'echo ' + encoded + ' | base64 -d | bash'
            answer = subprocess.run(['ssh', '-T', *options, 'pi@' + a.host, command],
                                    env=env, input='', text=True, capture_output=True, timeout=timeout)
            if check and answer.returncode:
                raise RuntimeError(f'SSH operation exited {answer.returncode}: {answer.stderr}')
            return answer

        if not a.status:
            # Archive exactly the validated bytes; never reread a mutable
            # source app/manifest after validation and call it the same package.
            files = {name: (ROOT / source).read_bytes() for name, source in PAYLOAD.items()}
            files['app.bin'] = release.image
            files['manifest.json'] = (json.dumps(release.manifest, sort_keys=True) + '\n').encode()
            hashes = {name: hashlib.sha256(data).hexdigest() for name, data in files.items()}
            archive = temp / 'ota.zip'
            with zipfile.ZipFile(archive, 'w', zipfile.ZIP_DEFLATED) as z:
                for name, data in files.items():
                    z.writestr(name, data)
            ssh('set -eu\nmkdir -m 700 ' + shlex.quote(staging))
            uploaded = subprocess.run(['scp', *options, str(archive), 'pi@' + a.host + ':' + staging + '/package.zip'],
                                      env=env, timeout=300)
            if uploaded.returncode:
                raise RuntimeError('Upload failed; the device was never opened')
            bootstrap = ('from pathlib import Path; import hashlib,zipfile; '
                         f'p=Path({staging!r}); z=p/"package.zip"; '
                         f'assert hashlib.sha256(z.read_bytes()).hexdigest()=={digest(archive)!r}; '
                         'zipfile.ZipFile(z).extractall(p); '
                         f'assert all(hashlib.sha256((p/n).read_bytes()).hexdigest()==h for n,h in {hashes!r}.items()); '
                         '(p/"linux/mixos-esp-update").chmod(0o755)')
            ssh(shlex.join(['python3', '-c', bootstrap]))
        command = (status_command(staging, a.status) if a.status else
                   remote_command(staging, a.device, a.timeout, a.allow_replace_baseline))
        transport_error = None
        try:
            answer = ssh(command, timeout=1000, check=False)
            result = parse_result(answer.stdout)
            if answer.returncode not in (0, 1, 2):
                transport_error = f'SSH exited {answer.returncode}; worker lifetime is independent'
        except subprocess.TimeoutExpired as exc:
            output = exc.stdout.decode('utf-8', 'replace') if isinstance(exc.stdout, bytes) else exc.stdout
            result = parse_result(output)
            transport_error = 'SSH wait timed out; background job outcome must be read with status'
        receipt = ROOT / 'build/deploy' / (transfer + '.json')
        native.atomic_json(receipt, dict(host=a.host, staging=staging, device=a.device, hashes=hashes,
                           artifact_provenance=provenance, result=result, transport_error=transport_error))
        print(json.dumps(result, sort_keys=True))
        print('receipt ' + str(receipt), file=sys.stderr)
        code = native.result_exit_code(result)
        if transport_error:
            print(transport_error + '; do not start another transfer blindly.', file=sys.stderr)
            return code if code else 1
        return code


if __name__ == '__main__':
    sys.exit(main())
