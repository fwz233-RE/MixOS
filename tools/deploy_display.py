#!/usr/bin/env python3
"""Stage/start/inspect the combined display app+font update using trusted OpenSSH.

Staging never stops services or resets devices. Starting runs once in a detached
systemd job, with automatic mixosd restart and explicit host authorization.
No on-screen confirmation is required by the new firmware.
The receipt identifies the exact uploaded artifacts; never blindly retry start.
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
import zipfile

from deploy_keyboard import package_installer
import display_transport as transport

ROOT = Path(__file__).resolve().parents[1]
PYTHON = 'python3'
ESPTOOL_WHEEL = '.tools/flash-packages-5.4/esptool-5.4.0-py3-none-any.whl'
ESPTOOL_SHA256 = transport.WHEEL_SHA256
PREVIOUS_ESPTOOL_SHA256 = 'e3fb7d617498e4ebd6843f7c2e65dab862e048aa57a451f7924d92b02f62c7bd'
# The build the device is actually running, recorded from its own readback. It
# is what the Pi-side worker expects to find in the backup before it writes
# anything, so it is deliberately a pinned constant rather than "whatever the
# last local build happened to produce".
#
# 2026-09-13: refreshed after the value went stale. The 2026-09-12 recovery
# flash replaced the application but left this constant on the build before it,
# so the worker read a full 8 MiB backup, found the live app did not match, and
# correctly refused to write. The current value is the application segment of
# the readback whose full-image hash the device reproduced exactly during job
# mixos-display-20260913-103821.
#
# 2026-09-15: stale again, for a new reason. Between 09-13 and 09-15 the device
# was updated repeatedly over USB OTA, which is a separate tool that does not
# maintain this constant, so it still described the 09-12 recovery flash. Two
# guesses from the OTA receipts were both refused, because a receipt records
# the image that was sent, and the device had since booted a different slot.
# The way out was to take a backup and read the active slot named by otadata,
# rather than trusting any deployment record; do that again when this goes
# stale, instead of guessing.
#
# Now the icon-font update below has been written and read back byte-exact by
# job mixos-display-20260915-131011, so this is that job's new application.
# 2026-09-16: ROM recovery job mixos-display-20260916-024325 replaced ota_0.
# Fresh backup and full readback were compared byte-for-byte locally as well:
# build/deploy/recovery-20260916-024325-readback-verification.json. The bootloader,
# table, otadata, preferences, font contents and ota_1 were unchanged. This hash
# records programmed bytes; application boot is verified separately.
DEPLOYED_APP_SHA256 = '7875d9a513acb95463b72e785ebd160c70d03f85e965c3a30a93d954bb4cff5f'


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def source_digest_matches(path, expected):
    data = path.read_bytes()
    lf = data.replace(b'\r\n', b'\n')
    return expected in {hashlib.sha256(value).hexdigest()
                        for value in (data, lf, lf.replace(b'\n', b'\r\n'))}


def current_build_check(app, old_app, table=None, migrate=False):
    report = json.loads((ROOT / 'build/esp32s3/font-app-build.json').read_text(encoding='utf-8'))
    if (report.get('status') != 'cross-built' or report.get('target') != 'esp32s3'
            or report.get('app', {}).get('sha256') != digest(app)
            or report.get('build_app', {}).get('sha256') != digest(app)
            or not report.get('sources')):
        raise ValueError('Current ESP build report does not match staged applications')
    # The build report attests the NEW app, so it cannot also vouch for the old
    # one: `previous_app` there is a frozen recovery copy from an earlier
    # revision, not whatever the device happens to run today. The old app is
    # pinned separately, and the worker re-checks it against the live backup.
    if digest(old_app) != DEPLOYED_APP_SHA256:
        raise ValueError('Previous app is not the recorded deployed build')
    for source in report['sources']:
        name = source['path'].replace('\\', '/').rsplit('/', 1)[-1]
        if not source_digest_matches(ROOT / 'firmware/esp32s3/main' / name, source['sha256']):
            raise ValueError('ESP source changed since the recorded build: ' + name)
    provenance = {'mode': 'current_verified_build', 'app_sha256': digest(app),
                  'device_app_sha256': digest(old_app),
                  'build_report_previous_app_sha256': report.get('previous_app', {}).get('sha256'),
                  'screen_confirmation_required': False,
                  'esptool_version': transport.VERSION}
    if migrate:
        # A stale build directory is the trap that silently shipped a
        # factory-only table for months: the CSV said A/B while build/ still
        # held the old table. A migration that wrote that table would leave a
        # device with no OTA slots and no way to notice.
        from update_esp import identify_partition_binary, validate_partitions
        built = identify_partition_binary(table.read_bytes())
        declared = validate_partitions(ROOT / 'firmware/esp32s3/partitions.csv')
        if built['name'] != 'ab' or declared['name'] != 'ab':
            raise ValueError(f'--migrate needs an A/B build: partitions.csv is {declared["name"]} '
                             f'and build/ holds the {built["name"]} table; rebuild the firmware')
        provenance['migrate_to_ab'] = True
    return provenance


def historical_artifact_check(receipt_path, host, files, font_report, build_report_path):
    """Recover exact historical release bytes, never claim current sources were built."""
    prior = json.loads(receipt_path.read_text(encoding='utf-8'))
    job = prior.get('job', '')
    if (not re.fullmatch(r'mixos-display-\d{8}-\d{6}', job) or prior.get('host') != host
            or prior.get('staging') != '/home/pi/' + job or prior.get('state') != 'staged_only'):
        raise ValueError('Historical receipt identity/host/state mismatch')
    expected = prior.get('hashes', {})
    if set(expected) != set(files) | {'launch.sh'} or any(
            not isinstance(value, str) or not re.fullmatch(r'[0-9a-f]{64}', value)
            for value in expected.values()):
        raise ValueError('Historical receipt must contain the exact reviewed artifact set')
    # Only the corrected worker, generated launcher, and exact reviewed
    # transport upgrade may differ. Firmware/font/helper identity stays strict.
    if (expected['esptool.whl'] not in (PREVIOUS_ESPTOOL_SHA256, ESPTOOL_SHA256)
            or digest(files['esptool.whl']) != ESPTOOL_SHA256):
        raise ValueError('Historical esptool upgrade must use the exact reviewed wheel')
    for name, path in files.items():
        if name not in ('tools/flash_font_on_pi.py', 'esptool.whl') and digest(path) != expected[name]:
            raise ValueError('Historical artifact/helper hash mismatch: ' + name)
    build = json.loads(build_report_path.read_text(encoding='utf-8'))
    ui = [row for row in build.get('sources', [])
          if row.get('path', '').replace('\\', '/').endswith('/firmware/esp32s3/main/mix_ui.c')]
    if (build.get('status') != 'cross-built' or build.get('target') != 'esp32s3'
            or build.get('app', {}).get('sha256') != expected['new-app.bin']
            or build.get('build_app', {}).get('sha256') != expected['new-app.bin']
            or build.get('previous_app', {}).get('sha256') != expected['firmware/esp32s3/build/mixos_esp32s3.bin']
            or build.get('app_partition_offset') != '0x10000'
            or build.get('app_partition_bytes') != 0x200000
            or len(ui) != 1 or ui[0].get('sha256') != font_report.get('ui_source_sha256')
            or not re.fullmatch(r'[0-9a-f]{64}', font_report.get('ui_source_sha256', ''))):
        raise ValueError('Historical app build report and font UI provenance do not agree')
    current_ui = ROOT / 'firmware/esp32s3/main/mix_ui.c'
    return {'mode': 'historical_artifact_recovery', 'prior_job': job,
            'prior_receipt_sha256': digest(receipt_path), 'build_report_sha256': digest(build_report_path),
            'built_ui_source_sha256': ui[0]['sha256'],
            'current_ui_source_sha256': digest(current_ui) if current_ui.is_file() else None,
            'current_sources_claimed_built': False,
            'transport_upgrade': {'previous_wheel_sha256': expected['esptool.whl'],
                                  'wheel_sha256': ESPTOOL_SHA256, 'version': '4.8.1'},
            'unchanged_artifact_hashes': {name: expected[name] for name in files
                                          if name not in ('tools/flash_font_on_pi.py', 'esptool.whl')}}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    mode = p.add_mutually_exclusive_group(required=True)
    mode.add_argument('--stage', action='store_true')
    mode.add_argument('--start', type=Path, metavar='RECEIPT')
    mode.add_argument('--status', type=Path, metavar='RECEIPT')
    p.add_argument('--host', default='192.168.1.22')
    p.add_argument('--stage-from-receipt', type=Path, metavar='PRIOR_RECEIPT',
                   help='Only with --stage: recover exact historical release artifacts; current source may differ')
    p.add_argument('--resume-no-write-source',
                   help='Only with --start: exact stopped prior /opt/.../work/session path')
    p.add_argument('--migrate', action='store_true',
                   help='Only with --stage: also move the device from the factory-only layout to A/B, '
                        'after which firmware updates go over USB with tools/ota_esp.py')
    a = p.parse_args()
    if a.stage_from_receipt and not a.stage:
        p.error('--stage-from-receipt requires --stage')
    if a.migrate and not a.stage:
        p.error('--migrate requires --stage')
    if a.migrate and a.stage_from_receipt:
        p.error('--migrate rewrites the bootloader and partition table from the current build, '
                'so it cannot recover a historical release')
    if a.resume_no_write_source and (not a.start or not re.fullmatch(
            r'/opt/mixos-display-packages/mixos-display-\d{8}-\d{6}/work/session',
            a.resume_no_write_source)):
        p.error('--resume-no-write-source requires --start and an exact prior package work/session path')
    password = os.environ.get('MIXOS_SSH_PASSWORD')
    if not password:
        raise ValueError('Set MIXOS_SSH_PASSWORD for the existing pi account')
    options = ['-o', 'ConnectionAttempts=1', '-o', 'ConnectTimeout=20', '-o', 'ServerAliveInterval=10',
               '-o', 'ServerAliveCountMax=3', '-o', 'StrictHostKeyChecking=yes',
               '-o', 'PreferredAuthentications=password', '-o', 'PubkeyAuthentication=no',
               '-o', 'NumberOfPasswordPrompts=1']
    with tempfile.TemporaryDirectory(prefix='mixos-deploy-') as temporary:
        temp = Path(temporary)
        helper = temp / ('askpass.cmd' if os.name == 'nt' else 'askpass.sh')
        helper.write_text('@echo off\necho %MIXOS_SSH_PASSWORD%\n' if os.name == 'nt' else
                          '#!/bin/sh\nprintf "%s\\n" "$MIXOS_SSH_PASSWORD"\n')
        helper.chmod(0o700)
        env = dict(os.environ, SSH_ASKPASS=str(helper), SSH_ASKPASS_REQUIRE='force', DISPLAY='unused:0')

        def ssh(script, sudo=False, timeout=120):
            encoded = base64.b64encode(script.encode()).decode()
            command = 'echo ' + encoded + ' | base64 -d | bash'
            if sudo:
                command = "sudo -S -p '' bash -c " + shlex.quote(command)
            result = subprocess.run(['ssh', '-T'] + options + ['pi@' + a.host, command],
                                    env=env, input=(password + '\n') if sudo else '',
                                    text=True, timeout=timeout)
            if result.returncode:
                raise RuntimeError(f'SSH operation exited {result.returncode}; inspect job before any retry')

        if a.stage:
            font = ROOT / 'build/font/MiSans-Normal-gb2312.ttf'
            old_app = ROOT / 'build/esp32s3/current-device-app.bin'
            app = ROOT / 'firmware/esp32s3/build/mixos_esp32s3.bin'
            table = app.parent / 'partition_table/partition-table.bin'
            manifest = font.with_suffix(font.suffix + '.manifest.json')
            report = json.loads(manifest.read_text(encoding='utf-8'))
            if (report.get('status') != 'verified' or report.get('output_sha256') != digest(font)
                    or (not a.stage_from_receipt and
                        not source_digest_matches(ROOT / 'firmware/esp32s3/main/mix_ui.c', report.get('ui_source_sha256')))
                    or report.get('ui_coverage_missing') != [] or report.get('required_coverage_missing') != []
                    or report.get('load_verification', {}).get('partition_padded_load') != 'passed'):
                raise ValueError('Rebuild the font: current UI coverage/load manifest is required')
            files = {name: ROOT / name for name in [
                'tools/flash_font_on_pi.py', 'tools/flash_esp_on_pi.py', 'tools/update_esp.py',
                'tools/display_transport.py', 'firmware/esp32s3/partitions.csv']}
            # tools/update_esp.py imports linux/mixosd.py, which imports its own
            # neighbours. protocol.py was staged and netctl.py was not, so on
            # 2026-09-15 the job died at import in exactly the way the missing
            # _mixlib had. Glob for the same reason given below: a module added
            # next to mixosd.py must not silently break deployment again.
            files.update({'linux/' + path.name: path
                          for path in sorted((ROOT / 'linux').glob('*.py'))})
            # Both flash workers import tools/_mixlib. Staging the callers
            # without their own support package made the job die at import,
            # after the launcher had already stopped mixosd. Globbing rather
            # than listing the three modules keeps the package complete when a
            # module is added to _mixlib.
            mixlib = sorted((ROOT / 'tools/_mixlib').glob('*.py'))
            if not any(path.name == '__init__.py' for path in mixlib):
                raise ValueError('tools/_mixlib is missing its package __init__.py')
            files.update({'tools/_mixlib/' + path.name: path for path in mixlib})
            files.update({'font.ttf': font, 'font-manifest.json': manifest, 'new-app.bin': app,
                          'firmware/esp32s3/build/mixos_esp32s3.bin': old_app, 'partition-table.bin': table,
                          'esptool.whl': ROOT / ESPTOOL_WHEEL})
            if not a.stage_from_receipt:
                # Ship the USB updater next to the firmware that understands it.
                # A historical release predates it and stays byte-exact.
                files['tools/ota_esp.py'] = ROOT / 'tools/ota_esp.py'
            if a.migrate:
                # The migration is the only operation that rewrites anything
                # below the application, so the bootloader is staged only when
                # it was explicitly asked for.
                files['bootloader.bin'] = app.parent / 'bootloader/bootloader.bin'
            files.update(transport.local_packages(ROOT))
            transport.verify_packages(files)
            if digest(files['esptool.whl']) != ESPTOOL_SHA256:
                raise ValueError('Display transport wheel differs from reviewed package')
            provenance = None
            if a.stage_from_receipt:
                provenance = historical_artifact_check(a.stage_from_receipt, a.host, files, report,
                                                      ROOT / 'build/esp32s3/font-app-build.json')
                print('HISTORICAL_ARTIFACT_RECOVERY: exact prior release bytes; current sources are not claimed built.', flush=True)
            else:
                provenance = current_build_check(app, old_app, table, a.migrate)
            if digest(old_app) != DEPLOYED_APP_SHA256:
                raise ValueError('Previous app is not the recorded deployed build')
            job = 'mixos-display-' + time.strftime('%Y%m%d-%H%M%S', time.gmtime())
            staging = '/home/pi/' + job
            package = '/opt/mixos-display-packages/' + job
            command = ['/usr/bin/python3', '-I', '-u', package + '/tools/flash_font_on_pi.py', '--execute',
                       '--workdir', package + '/work/session',
                       '--sha256', digest(font), '--app-sha256', digest(old_app),
                       '--new-app-sha256', digest(app), '--partition-sha256', digest(table)]
            if a.migrate:
                command += ['--migrate', '--bootloader-sha256', digest(files['bootloader.bin'])]
            launch = ('#!/bin/bash\nset -euo pipefail\n' +
                      'if [[ $# -ne 0 ]]; then [[ $# -eq 2 && "$1" == "--resume-no-write-source" ]] || exit 64; fi\n' +
                      'cd ' + shlex.quote(package) + '\n' +
                      "/usr/bin/python3 -I -c \"from pathlib import Path; p=Path('start-claim'); f=p.open('x'); f.write('Single launch only\\\\n'); f.flush(); import os; os.fsync(f.fileno()); f.close()\"\n" +
                      'systemctl stop mixosd.service\n' +
                      'exec runuser -u pi -- ' + shlex.join(command) + ' "$@"\n')
            launch_path = temp / 'launch.sh'
            launch_path.write_text(launch, newline='\n')
            files['launch.sh'] = launch_path
            hashes = {name: digest(path) for name, path in files.items()}
            if provenance and any(hashes[name] != value for name, value in provenance.get('unchanged_artifact_hashes', {}).items()):
                raise ValueError('Historical artifact changed during staging')
            archive = temp / 'display.zip'
            with zipfile.ZipFile(archive, 'w', zipfile.ZIP_DEFLATED) as z:
                for name, path in files.items():
                    z.write(path, name)
            if provenance:
                with zipfile.ZipFile(archive, 'r') as z:
                    if any(hashlib.sha256(z.read(name)).hexdigest() != value for name, value in hashes.items()):
                        raise ValueError('Packaged bytes differ from validated staging hashes')
            ssh('set -eu\nmkdir -m 700 ' + shlex.quote(staging))
            uploaded = subprocess.run(['scp'] + options + [str(archive), 'pi@' + a.host + ':' + staging + '/package.zip'],
                                      env=env, timeout=300)
            if uploaded.returncode:
                raise RuntimeError('Upload failed; no service stopped and no worker started')
            bootstrap = ("from pathlib import Path; import hashlib,zipfile; " +
                         f"p=Path({staging!r}); z=p/'package.zip'; " +
                         f"assert hashlib.sha256(z.read_bytes()).hexdigest()=={digest(archive)!r}; " +
                         "zipfile.ZipFile(z).extractall(p); (p/'worker.log').touch(mode=0o600)")
            ssh(shlex.join([PYTHON, '-c', bootstrap]))
            receipt = ROOT / 'build/deploy' / (job + '.json')
            receipt.parent.mkdir(parents=True, exist_ok=True)
            receipt.write_text(json.dumps({'job': job, 'host': a.host, 'staging': staging, 'hashes': hashes,
                                           'state': 'staged_only', 'artifact_provenance': provenance}, indent=2) + '\n')
            print('STAGED_ONLY: ' + str(receipt), flush=True)
            if a.migrate:
                print('This package also migrates the device to the A/B layout. It is a one-time '
                      'operation: afterwards use tools/deploy_ota.py, which updates the firmware '
                      'over USB in one command. See docs/ESP_OTA.md.',
                      flush=True)
            return
        receipt = json.loads((a.start or a.status).read_text())
        job = receipt['job']
        if not re.fullmatch(r'mixos-display-\d{8}-\d{6}', job) or receipt['host'] != a.host:
            raise ValueError('Invalid receipt/host')
        staging = '/home/pi/' + job
        if receipt['staging'] != staging:
            raise ValueError('Invalid staging path')
        package = '/opt/mixos-display-packages/' + job
        if a.resume_no_write_source == package + '/work/session':
            raise ValueError('Resume requires a different prior job')
        if a.status:
            ssh('systemctl show ' + job + '.service -p LoadState -p ActiveState -p SubState -p Result -p ExecMainStatus; '
                'systemctl is-active mixosd.service; '
                'tail -n 40 ' + package + '/worker.log; '
                'test ! -f ' + package + '/work/session/flash-audit.jsonl || tail -n 20 ' + package + '/work/session/flash-audit.jsonl', sudo=True)
            return
        required = {'tools/flash_font_on_pi.py', 'tools/flash_esp_on_pi.py', 'tools/update_esp.py',
                    'firmware/esp32s3/partitions.csv',
                    'font.ttf', 'font-manifest.json', 'new-app.bin',
                    'firmware/esp32s3/build/mixos_esp32s3.bin', 'partition-table.bin', 'esptool.whl', 'launch.sh'}
        required.add('tools/display_transport.py')
        # Mirror the staging side for the daemon's own package too.
        required.update('linux/' + path.name for path in sorted((ROOT / 'linux').glob('*.py')))
        # Mirror the staging side: the workers' support package travels with
        # them, and the exact-set check below still pins which modules may be
        # present rather than accepting anything under tools/_mixlib/.
        required.update('tools/_mixlib/' + path.name
                        for path in sorted((ROOT / 'tools/_mixlib').glob('*.py')))
        required.update(transport.PACKAGES)
        provenance = receipt.get('artifact_provenance') or {}
        if provenance.get('mode') != 'historical_artifact_recovery':
            required.add('tools/ota_esp.py')
        if provenance.get('migrate_to_ab'):
            required.add('bootloader.bin')
        if set(receipt['hashes']) != required:
            raise ValueError('Unexpected display package contents')
        # Copy from user staging into an exclusive root-owned package BEFORE any
        # root shell executes its launcher. Never execute a /home/pi launch script.
        installer = package_installer(staging, package, receipt['hashes'], display=True)
        ssh(shlex.join(['/usr/bin/python3', '-I', '-c', installer]), sudo=True)
        args = ['systemd-run', '--unit', job, '--property=Type=exec', '--property=Restart=no',
                '--property=RuntimeMaxSec=1800', '--property=UMask=0077',
                '--property=WorkingDirectory=' + package,
                '--property=StandardOutput=append:' + package + '/worker.log',
                '--property=StandardError=inherit',
                '--property=ExecStopPost=/usr/bin/systemctl start mixosd.service',
                '--', '/bin/bash', package + '/launch.sh']
        if a.resume_no_write_source:
            args.extend(['--resume-no-write-source', a.resume_no_write_source])
        print('Launching once: ' + job + '. Check status after any disconnect; never blindly repeat start.', flush=True)
        ssh(shlex.join(args), sudo=True)
        print(('SUBMITTED: exact prior no-write consent will be checked; no repeat local consent is sent.'
               if a.resume_no_write_source else 'SUBMITTED: host command authorizes this update; no screen confirmation requested.') +
              ' Submission is not flash success.', flush=True)


if __name__ == '__main__':
    main()
