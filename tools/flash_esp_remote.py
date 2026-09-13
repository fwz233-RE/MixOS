#!/usr/bin/env python3
"""Run the audited app-only updater on a Pi over SSH. Passwords are never saved.

Requires paramiko on the PC and esptool + pyserial on the Pi. Build first.
Default is inspection only; --execute explicitly authorizes reset and flashing.
"""
import argparse
import getpass
import hashlib
import json
import os
import re
from pathlib import Path
import shlex
import socket
import sys
import time
import subprocess
import tempfile
import zipfile

ROOT = Path(__file__).resolve().parents[1]


def connect(host, user, password, address=None):
    import paramiko
    for attempt in range(3):
        client = paramiko.SSHClient()
        client.load_system_host_keys()
        # Host must already have been verified with OpenSSH (known_hosts).
        sock = None
        try:
            # Alternate address still MUST present the trusted host's same key.
            if address:
                sock = socket.create_connection((address, 22), timeout=60)
            client.connect(host, username=user, password=password, sock=sock,
                           look_for_keys=False, allow_agent=False,
                           timeout=60, banner_timeout=60, auth_timeout=60,
                           disabled_algorithms={'kex': ['curve25519-sha256@libssh.org']})
            client.get_transport().set_keepalive(10)
            return client
        except (OSError, paramiko.SSHException, EOFError) as exc:
            client.close()
            if sock is not None:
                sock.close()
            if isinstance(exc, (paramiko.AuthenticationException, paramiko.BadHostKeyException)) or attempt == 2:
                raise
            print(f'SSH connection attempt {attempt + 1} failed: {type(exc).__name__}: {exc}', flush=True)
            time.sleep(70 * (attempt + 1))  # Allow sshd temporary per-source penalties to expire.


def run(client, command, timeout=1800, input_data=None):
    channel = client.get_transport().open_session(timeout=30)
    channel.set_combine_stderr(True)
    channel.exec_command(command)
    if input_data is not None:
        channel.sendall(input_data)
        channel.shutdown_write()
    deadline = time.monotonic() + timeout
    try:
        while True:
            if channel.recv_ready():
                print(channel.recv(8192).decode('utf-8', 'replace'), end='', flush=True)
            elif channel.exit_status_ready():
                return channel.recv_exit_status()
            elif time.monotonic() > deadline or channel.closed:
                raise RuntimeError('Remote command timed out/disconnected; inspect remote audit before retrying.')
            else:
                time.sleep(0.05)
    finally:
        channel.close()


def detached_command(staging, user, command):
    """The service runs as the dialout user, independent of SSH, without retries."""
    job = staging.rsplit('/', 1)[-1]
    if not re.fullmatch(r'mixos-flash-\d{8}-\d{6}', job):
        raise ValueError('Unexpected staging/job name')
    return shlex.join(['sudo', '-S', '-p', '', 'systemd-run', '--unit', job,
                       '--uid', user, '--property=Type=exec', '--property=Restart=no',
                       '--property=RuntimeMaxSec=1800', '--property=UMask=0077',
                       '--property=WorkingDirectory=' + staging,
                       '--property=StandardOutput=append:' + staging + '/worker.log',
                       '--property=StandardError=inherit', '--'] + command)


def job_status_command(job):
    if not re.fullmatch(r'mixos-flash-\d{8}-\d{6}', job):
        raise ValueError('Use the exact mixos-flash-YYYYMMDD-HHMMSS job name')
    return ('systemctl show ' + shlex.quote(job + '.service') +
            ' -p LoadState -p ActiveState -p SubState -p Result -p ExecMainStatus; '
            'for f in worker.log flash-audit.jsonl; do '
            'printf "\\n=== %s ===\\n" "$f"; tail -n 35 "$HOME"/' + job + '/"$f"; done')


def show_job(client, job):
    command = job_status_command(job)
    print('STATUS ONLY: no reset, upload, or flash retry.', flush=True)
    return run(client, command, timeout=60)


def worker_command(a, staging, digest, image):
    """Arguments for the Pi-side worker, identical for both SSH backends."""
    table = image.parent / 'partition_table/partition-table.bin'
    command = ['python3', '-u', staging + '/tools/flash_esp_on_pi.py', '--serial', a.serial,
               '--boot-mode', a.boot_mode, '--sha256', digest, '--partition-sha256',
               hashlib.sha256(table.read_bytes()).hexdigest()]
    if a.migrate:
        command += ['--migrate', '--bootloader-sha256',
                    hashlib.sha256((image.parent / 'bootloader/bootloader.bin').read_bytes()).hexdigest()]
    if a.esptool_wheel:
        command += ['--esptool-wheel-sha256', hashlib.sha256(a.esptool_wheel.read_bytes()).hexdigest()]
    if a.resume_probe:
        command += ['--resume-probe', a.resume_probe]
    if a.probe_download:
        command.append('--probe-download')
    return command


def native_operation(a, password, image=None, digest=None):
    """OpenSSH fallback for links on which the Paramiko SFTP channel stalls."""
    with tempfile.TemporaryDirectory(prefix='mixos-ssh-') as temp:
        helper = Path(temp) / ('askpass.cmd' if os.name == 'nt' else 'askpass.sh')
        helper.write_text('@echo off\necho %MIXOS_SSH_PASSWORD%\n' if os.name == 'nt' else
                          '#!/bin/sh\nprintf "%s\\n" "$MIXOS_SSH_PASSWORD"\n')
        helper.chmod(0o700)
        env = dict(os.environ, MIXOS_SSH_PASSWORD=password, SSH_ASKPASS=str(helper),
                   SSH_ASKPASS_REQUIRE='force', DISPLAY=os.environ.get('DISPLAY') or 'unused:0')
        options = ['-o', 'ConnectionAttempts=1', '-o', 'ConnectTimeout=20',
                   '-o', 'ServerAliveInterval=10', '-o', 'ServerAliveCountMax=3',
                   '-o', 'StrictHostKeyChecking=yes', '-o', 'PreferredAuthentications=password',
                   '-o', 'PubkeyAuthentication=no', '-o', 'NumberOfPasswordPrompts=1']
        target = a.user + '@' + (a.address or a.host)
        if a.address:
            options += ['-o', 'HostKeyAlias=' + a.host]

        def ssh(command, sudo=False, capture=False):
            return subprocess.run(['ssh', '-T'] + options + [target, command], env=env,
                                  input=(password + '\n') if sudo else '', text=True,
                                  stdout=subprocess.PIPE if capture else None, timeout=120)

        if a.status:
            return ssh(job_status_command(a.status)).returncode
        if not a.execute:
            return ssh('hostname; python3 -m serial.tools.list_ports -v').returncode
        if not a.detach:
            raise ValueError('--native execution requires --detach; status does not')
        home_result = ssh('printf "%s" "$HOME"', capture=True)
        home = home_result.stdout.strip()
        if home_result.returncode or not re.fullmatch(r'/home/[A-Za-z0-9_.-]+', home):
            raise RuntimeError('Could not verify remote home directory')
        staging = home + '/mixos-flash-' + time.strftime('%Y%m%d-%H%M%S')
        print('Remote package and recovery backup directory: ' + staging, flush=True)
        if a.reuse_staging:
            if not re.fullmatch(r'mixos-flash-\d{8}-\d{6}', a.reuse_staging):
                raise ValueError('Invalid reusable staging name')
            expected = {
                'tools/flash_esp_on_pi.py': a.reuse_worker_sha256,
                'firmware/esp32s3/build/mixos_esp32s3.bin': digest,
                'partition-table.bin': hashlib.sha256((image.parent / 'partition_table/partition-table.bin').read_bytes()).hexdigest(),
                'esptool.whl': hashlib.sha256(a.esptool_wheel.read_bytes()).hexdigest(),
            }
            if a.migrate:
                expected['bootloader.bin'] = hashlib.sha256(
                    (image.parent / 'bootloader/bootloader.bin').read_bytes()).hexdigest()
            bootstrap = ("from pathlib import Path; import hashlib,json,shutil; "
                         f"src=Path({(home + '/' + a.reuse_staging)!r}); dst=Path({staging!r}); "
                         f"expected=json.loads({json.dumps(expected)!r}); "
                         "assert src.is_dir() and not dst.exists(); "
                         "assert all(hashlib.sha256((src/p).read_bytes()).hexdigest()==h for p,h in expected.items()); "
                         "shutil.copytree(src,dst); "
                         "[(shutil.rmtree(p) if p.is_dir() else p.unlink()) for p in [dst/'.esptool',dst/'flash-audit.jsonl',dst/'original-flash-8MB.bin',dst/'app-readback.bin'] if p.exists()]; "
                         "(dst/'worker.log').write_text(''); (dst/'worker.log').chmod(0o600)")
            if ssh('python3 -c ' + shlex.quote(bootstrap)).returncode:
                raise RuntimeError('Remote artifact reuse/hash validation failed; no worker launched')
            print('Reused and re-hashed existing remote artifacts; no SCP upload needed.', flush=True)
        else:
            files = ['tools/flash_esp_on_pi.py', 'tools/update_esp.py', 'linux/protocol.py',
                     'linux/mixosd.py', 'firmware/esp32s3/partitions.csv',
                     'firmware/esp32s3/build/mixos_esp32s3.bin']
            archive = Path(temp) / 'package.zip'
            with zipfile.ZipFile(archive, 'w', zipfile.ZIP_DEFLATED) as z:
                for relative in files:
                    z.write(ROOT / relative, relative)
                z.write(image.parent / 'partition_table/partition-table.bin', 'partition-table.bin')
                if a.migrate:
                    z.write(image.parent / 'bootloader/bootloader.bin', 'bootloader.bin')
                if a.esptool_wheel:
                    z.write(a.esptool_wheel, 'esptool.whl')
            if ssh('mkdir -m 700 ' + shlex.quote(staging)).returncode:
                raise RuntimeError('Remote staging creation failed; nothing started')
            destination = a.user + '@' + (('[' + a.address + ']') if a.address and ':' in a.address else (a.address or a.host))
            transfer = subprocess.run(['scp'] + options + [str(archive), destination + ':' + staging + '/package.zip'],
                                      env=env, timeout=240)
            if transfer.returncode:
                raise RuntimeError('Native upload failed; no worker has been launched')
            archive_digest = hashlib.sha256(archive.read_bytes()).hexdigest()
            bootstrap = ("from pathlib import Path; import hashlib,zipfile,os; "
                         f"p=Path({staging!r}); a=p/'package.zip'; "
                         f"assert hashlib.sha256(a.read_bytes()).hexdigest()=={archive_digest!r}; "
                         "zipfile.ZipFile(a).extractall(p); "
                         "(p/'worker.log').touch(mode=0o600)")
            if ssh('python3 -c ' + shlex.quote(bootstrap)).returncode:
                raise RuntimeError('Uploaded package check/extraction failed; no worker launched')
        command = worker_command(a, staging, digest, image)
        job = staging.rsplit('/', 1)[-1]
        print(f'JOB={job}; after any submission disconnect, query --status {job}, NEVER repeat --execute blindly.', flush=True)
        result = ssh(detached_command(staging, a.user, command), sudo=True)
        print(f'JOB_SUBMISSION_EXIT={result.returncode}; submission is NOT flash success.', flush=True)
        return result.returncode


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--native', action='store_true', help='Use system OpenSSH/SCP instead of Paramiko')
    p.add_argument('--reuse-staging', help='Native mode: reuse a verified remote staging directory to avoid upload')
    p.add_argument('--reuse-worker-sha256', help='Required exact worker hash with --reuse-staging')
    p.add_argument('--host', required=True)
    p.add_argument('--address', help='Alternate IP of the SAME host; verify against --host known key')
    p.add_argument('--user', default='pi')
    p.add_argument('--serial', default='TD0720', help='exact running-app USB serial')
    p.add_argument('--boot-mode', choices=['legacy', 'mixos'], default='legacy')
    p.add_argument('--resume-probe', help='Resume exact ROM from a completed download-only probe')
    p.add_argument('--esptool-wheel', type=Path, help='Upload an isolated esptool 4.7.0 wheel with the missing S3 stub')
    p.add_argument('--execute', action='store_true')
    p.add_argument('--probe-download', action='store_true', help='With --execute, test ROM entry only; no flash read/write')
    p.add_argument('--migrate', action='store_true',
                   help='One-time move to the A/B partition table so later updates use USB OTA')
    p.add_argument('--detach', action='store_true', help='Start a Pi systemd job that survives SSH loss; requires --execute and sudo')
    p.add_argument('--status', metavar='JOB', help='Read an existing job without uploading or restarting it')
    a = p.parse_args()
    if a.reuse_staging and (not a.native or not a.detach or not a.execute or not a.reuse_worker_sha256 or not a.esptool_wheel):
        p.error('--reuse-staging requires --native --execute --detach, --reuse-worker-sha256 and --esptool-wheel')
    if ((a.detach or a.probe_download or a.resume_probe or a.esptool_wheel or a.reuse_staging or a.migrate)
            and not a.execute) or (a.status and a.execute):
        p.error('Execution options require --execute; --status cannot be combined with --execute')
    if a.migrate and a.probe_download:
        p.error('--migrate cannot be combined with --probe-download')
    if a.resume_probe and a.probe_download:
        p.error('--resume-probe cannot be combined with --probe-download')
    if a.status:
        password = os.environ.get('MIXOS_SSH_PASSWORD') or getpass.getpass('SSH password: ')
        if a.native:
            return native_operation(a, password)
        client = connect(a.host, a.user, password, a.address)
        try:
            return show_job(client, a.status)
        finally:
            client.close()
    # Local preflight before connecting, even for inspection.
    from update_esp import validate_image, validate_partitions, identify_partition_binary
    build = ROOT / 'firmware/esp32s3/build'
    image = build / 'mixos_esp32s3.bin'
    digest = hashlib.sha256(image.read_bytes()).hexdigest()
    layout = validate_partitions(ROOT / 'firmware/esp32s3/partitions.csv')
    # A stale build directory is the trap that silently shipped a factory-only
    # table for months: the CSV said A/B while build/ still held the old table.
    built = identify_partition_binary((build / 'partition_table/partition-table.bin').read_bytes())
    if built['name'] != layout['name']:
        raise RuntimeError(f'build/ holds the {built["name"]} partition table but partitions.csv is '
                           f'{layout["name"]}; rebuild the firmware before flashing')
    validate_image(image, digest, 'esp32s3', layout)
    if a.migrate and layout['name'] != 'ab':
        raise RuntimeError('--migrate requires a build that targets the A/B partition table')
    password = os.environ.get('MIXOS_SSH_PASSWORD') or getpass.getpass(f'SSH password for {a.user}@{a.host}: ')
    if a.native:
        return native_operation(a, password, image, digest)
    client = connect(a.host, a.user, password, a.address)
    print('SSH authenticated; host key verified.', flush=True)
    try:
        if not a.execute:
            return run(client, 'hostname; lsusb; ls -l /dev/serial/by-id/; python3 -m esptool version; python3 -m serial.tools.list_ports -v', 60)
        channel = client.get_transport().open_session(timeout=30)
        channel.settimeout(60)
        channel.invoke_subsystem('sftp')
        import paramiko
        sftp = paramiko.SFTPClient(channel)
        home = sftp.normalize('.')
        staging = home + '/mixos-flash-' + time.strftime('%Y%m%d-%H%M%S')
        sftp.mkdir(staging, mode=0o700)
        for folder in ['tools', 'linux', 'firmware', 'firmware/esp32s3', 'firmware/esp32s3/build']:
            sftp.mkdir(staging + '/' + folder, mode=0o700)
        files = ['tools/flash_esp_on_pi.py', 'tools/update_esp.py',
                 'linux/protocol.py', 'linux/mixosd.py', 'firmware/esp32s3/partitions.csv']
        for relative in files:
            sftp.put(str(ROOT / relative), staging + '/' + relative)
        sftp.put(str(image), staging + '/firmware/esp32s3/build/mixos_esp32s3.bin')
        sftp.put(str(build / 'partition_table/partition-table.bin'), staging + '/partition-table.bin')
        if a.migrate:
            sftp.put(str(build / 'bootloader/bootloader.bin'), staging + '/bootloader.bin')
        if a.esptool_wheel:
            sftp.put(str(a.esptool_wheel), staging + '/esptool.whl')
        if a.detach:
            # Precreate as pi: the system service appends without changing ownership.
            with sftp.open(staging + '/worker.log', 'w'):
                pass
            sftp.chmod(staging + '/worker.log', 0o600)
        sftp.close()
        print(f'Remote package and recovery backup directory: {staging}', flush=True)
        command = worker_command(a, staging, digest, image)
        if a.detach:
            job = staging.rsplit('/', 1)[-1]
            print(f'JOB={job}; if SSH disconnects, use --status {job}; NEVER blindly repeat --execute.', flush=True)
            status = run(client, detached_command(staging, a.user, command), timeout=60,
                         input_data=(password + '\n').encode())
            print(f'JOB_SUBMISSION_EXIT={status}; submission is NOT flash success. Check --status {job}.', flush=True)
            return status
        status = run(client, shlex.join(command))
        print(f'REMOTE_EXIT={status}; evidence and backup retained at {staging}', flush=True)
        return status
    finally:
        client.close()


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception as exc:
        print(f'ABORTED: {type(exc).__name__}: {exc}', file=sys.stderr)
        sys.exit(1)
