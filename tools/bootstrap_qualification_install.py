#!/usr/bin/env python3
"""Standalone, stdlib-only qualification package preflight; never starts a task.

Default: verify externally pinned archive/approval hashes and print a plan.
--install additionally requires an externally approved document, isolated Linux
root, protected existing parents, an unused task ID and an absent systemd unit.
Only daemon-reload is performed. No enable/start, device access, provisioning,
task-state creation, retry, approval issuance or historical-evidence generation.
The administrator must authenticate THIS installer before running it as root;
a checksum printed by an untrusted package is not independent authorization.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess
import sys
import tarfile

SCHEMA = 'mixos-qualification-package/v1'
ROUTE = 'mixos-reviewed-bootloader-binary-evidence/v1'
RESET_METHOD = 'official-esptool-watchdog-reset'
MAX_ARCHIVE = 128 * 1024 * 1024
MAX_FILE = 16 * 1024 * 1024
UNIT_DIRECTORY = Path('/etc/systemd/system')
STATE = Path('/var/lib/mixos/bootstrap-ota')
MAIN_CODE = frozenset('tools/' + name for name in (
    'bootstrap_ota_on_pi.py', 'flash_font_on_pi.py', 'flash_esp_on_pi.py',
    'update_esp.py', 'display_transport.py', 'ota_esp.py', 'ota_v2.py',
    'mixos_esp_update.py'))
MINIMUM_CODE = MAIN_CODE | frozenset({
    'tools/_mixlib/__init__.py', 'tools/_mixlib/application_cdc.py',
    'tools/_mixlib/bootstrap_service.py', 'tools/_mixlib/bootloader_evidence.py',
    'tools/_mixlib/durable.py', 'tools/_mixlib/guards.py', 'tools/_mixlib/ota_bootstrap.py',
    'linux/protocol.py', 'linux/serial_transport.py', 'linux/mixosd.py',
    'linux/netctl.py', 'linux/headless.py',
})
NATIVE_EXTRA = frozenset({'linux/mixos-esp-update'})
# Duplicated intentionally: remote preflight must NOT import untrusted archive
# code. Offline tests enforce equality with the production validator/runtime.
ORIGINAL_SHA256 = {
    'bootloader': '6ea16d3717dbe339973b44109f4bd9bd50d6000418e58f677cd5b9e45116531d',
    'reference_bootloader': '78648f9881f35dd593f7c42c75e0bf82c380c569ce1a4985618ef512a184ef2f',
    'reference_config': '62586acf011fd1810d6cb44161c4ff6924ec4982bf53520721251e2a9b269cc2',
    'reference_elf': '07ed8215976c13f850cdb604097843579ff77f569ff4fe2f2f5ac193d7af9cc2',
    'bounded-recheck.json': '3c252cf99df1e07e9396165291a593aac775a2af5f227c0e9911994c011f0026',
    'audited-behavior.json': '0c1ee119f5441edbc102d5c3aa4b4758f9b4531adbc5d0ba60612d2f3744c304',
    'byte-comparison.json': 'b9303f56ea14fdd0789ea0bdc6dbc0d324748aa8ae4895fe13121d1a8057e4e5',
    'semantic-verification.json': '0529ddf544e65092751d0d5961b347c335cb95fc66820ae12a5ae6356fe708cd',
    'rollback-paths.json': '7170a1eebf5eb4f5f8fd18c0ee5e2e39a4c3b1202b0f0a23e32890bf796e702d',
    'startup-takeover.json': '14ffaa29031e827254acd9381e8760774b68874c157dc7fa225618d0a3a9295c',
}
RECOVERY_SHA256 = {
    'recovery_flash': 'df9c108f6248f2cfde22f097187beaeef5d76dbfd34a173140671c164939a73e',
    'migration_receipt': 'd694b2e699927e4d20f3c14cff6830a3e3c01b15dbf69843affff7237279f3c3',
}
ARTIFACT_NAMES = frozenset(ORIGINAL_SHA256) | frozenset(RECOVERY_SHA256) | {
    'recovery_verification', 'recovery_boot'}
RUNTIME_SHA256 = {
    'esptool.whl': '0a08e50b745eb33764365c4aa332f7ae9da0bf95ebda1f24d0adcd8e7cbac119',
    'runtime/esp_pylib.whl': 'be04824c2da8d0af3ae891f93d0e4059c14d5f2c3c828b19a1c12d916c6a4d74',
    'runtime/click.whl': '63c132bbbed01578a06712a2d1f497bb62d9c1c0d329b7903a866228027263b2',
    'runtime/rich_click.whl': '365e7a9d0adb42e41ea832a0a12e02c44c079a26536dee688125eb9814f97274',
}


class Refused(ValueError):
    pass


def require(condition, message):
    if not condition:
        raise Refused(message)


def sha(data):
    return hashlib.sha256(data).hexdigest()


def digest(value):
    require(isinstance(value, str) and re.fullmatch('[0-9a-f]{64}', value), 'Lowercase SHA256 required')
    return value


def encoded(value):
    return (json.dumps(value, sort_keys=True, indent=2, ensure_ascii=True) + '\n').encode('ascii')


def strict_json(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            require(key not in result, 'Duplicate JSON key: ' + key)
            result[key] = value
        return result

    def invalid(value):
        raise Refused('Non-finite JSON: ' + value)

    try:
        result = json.loads(raw, object_pairs_hook=pairs, parse_constant=invalid)
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise Refused('Invalid JSON: ' + str(exc)) from exc
    require(type(result) is dict, 'JSON object required')
    return result


def task_id(value):
    require(isinstance(value, str) and re.fullmatch('[a-z0-9][a-z0-9-]{7,79}', value), 'Invalid task ID')
    require('qualify-reviewed-binary-' in value, 'Explicit qualify-reviewed-binary purpose required in task ID')
    return value


def posix_absolute(value):
    require(isinstance(value, str) and value.startswith('/') and not value.startswith('//')
            and re.fullmatch(r'/[A-Za-z0-9_./-]+', value), 'Plain POSIX absolute path required')
    path = PurePosixPath(value)
    require(str(path) == value and '..' not in path.parts and '.' not in path.parts,
            'Canonical POSIX absolute path required')
    return path


def relative_name(value):
    require(isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9_./-]+', value), 'Unsafe archive name')
    path = PurePosixPath(value)
    require(not path.is_absolute() and str(path) == value and '..' not in path.parts
            and '.' not in path.parts and value != '.', 'Unsafe archive path')
    return value


def code_name(name):
    return (name in MAIN_CODE or re.fullmatch(r'(tools/_mixlib|linux)/[A-Za-z0-9_]+\.py', name))


def directory_names(files):
    return {str(parent) for name in files for parent in PurePosixPath(name).parents if str(parent) != '.'}


def read_regular(path, limit=MAX_FILE):
    """Reject links and read once through a checked handle; never follow a link."""
    path = Path(path)
    require(path.is_absolute() and path.resolve(strict=True) == path, 'Canonical absolute source required')
    before = path.lstat()
    require(stat.S_ISREG(before.st_mode) and before.st_nlink == 1 and before.st_size <= limit,
            'Regular, single-link, bounded file required: ' + str(path))
    fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_BINARY', 0))
    with os.fdopen(fd, 'rb') as stream:
        opened = os.fstat(stream.fileno())
        require((opened.st_dev, opened.st_ino) == (before.st_dev, before.st_ino), 'Source replaced while opening')
        raw = stream.read(limit + 1)
        after = os.fstat(stream.fileno())
    require(len(raw) == before.st_size <= limit and after.st_nlink == 1
            and (after.st_size, after.st_mtime_ns) == (before.st_size, before.st_mtime_ns),
            'Source changed while reading')
    return raw


def tar_bytes(files):
    """One deterministic USTAR encoding; all metadata fixed, no extension records."""
    require(len(files) <= 256 and sum(map(len, files.values())) <= MAX_ARCHIVE // 2, 'Package too large')
    dirs = directory_names(files)
    require(not dirs.intersection(files), 'File/directory collision')
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode='w', format=tarfile.USTAR_FORMAT) as archive:
        for name in sorted(set(files) | dirs):
            relative_name(name)
            info = tarfile.TarInfo(name)
            info.uid = info.gid = info.mtime = 0
            info.uname = info.gname = ''
            info.mode = 0o555 if name in dirs else 0o444
            info.type = tarfile.DIRTYPE if name in dirs else tarfile.REGTYPE
            data = files.get(name, b'')
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data) if name in files else None)
    return stream.getvalue()


def file_records(files):
    return {name: dict(bytes=len(data), sha256=sha(data), mode='0444') for name, data in sorted(files.items())}


def unpack_checked(raw):
    """Preflight only, in memory. Never tar.extract()/extractall()."""
    require(len(raw) <= MAX_ARCHIVE, 'Archive too large')
    files, seen, dirs = {}, set(), set()
    try:
        with tarfile.open(fileobj=io.BytesIO(raw), mode='r:') as archive:
            for item in archive:
                name = relative_name(item.name)
                require(name not in seen and len(seen) < 512, 'Duplicate/excess archive member')
                seen.add(name)
                require(item.type in (tarfile.REGTYPE, tarfile.DIRTYPE) and not item.linkname
                        and not item.pax_headers, 'Only plain regular files/directories permitted')
                require(item.uid == item.gid == item.mtime == 0 and not item.uname and not item.gname,
                        'Unexpected archive owner/time metadata')
                require(item.mode == (0o555 if item.isdir() else 0o444), 'Unexpected archive mode')
                require(0 <= item.size <= MAX_FILE and (not item.isdir() or item.size == 0), 'Invalid archive size')
                if item.isdir():
                    dirs.add(name)
                else:
                    with archive.extractfile(item) as stream:
                        files[name] = stream.read(MAX_FILE + 1)
                    require(len(files[name]) == item.size, 'Truncated archive file')
    except (tarfile.TarError, OSError, EOFError) as exc:
        raise Refused('Invalid tar: ' + str(exc)) from exc
    require(dirs == directory_names(files), 'Archive directory set differs')
    require(tar_bytes(files) == raw, 'Noncanonical archive or hidden/trailing records')
    return files


def validate_payload(files, approval_sha256):
    require('manifest.json' in files and 'approval.json' in files, 'Missing manifest/approval')
    manifest = strict_json(files['manifest.json'])
    require(set(manifest) == {'schema', 'task_id', 'remote_package', 'files', 'installer_sha256'}
            and manifest['schema'] == SCHEMA, 'Unsupported package manifest')
    digest(manifest['installer_sha256'])
    payload = {name: data for name, data in files.items() if name != 'manifest.json'}
    require(manifest['files'] == file_records(payload), 'Manifest exact file set/size/hash/mode mismatch')
    require(sha(files['approval.json']) == digest(approval_sha256), 'Externally pinned approval SHA256 mismatch')
    approval = strict_json(files['approval.json'])
    require(set(approval) == {'schema', 'approved', 'task_id', 'kind', 'bootloader_evidence_mode',
            'reset_method', 'allow_clear_force_download', 'managed_service', 'code_sha256',
            'runtime_package', 'artifacts'} and type(approval['schema']) is int and approval['schema'] == 1,
            'Exact qualification approval schema required')
    require(type(approval['approved']) is bool, 'Explicit approval boolean required')
    require(approval['kind'] == 'qualify' and approval['bootloader_evidence_mode'] == ROUTE,
            'Only explicit qualify + reviewed binary evidence route permitted')
    identifier = task_id(approval['task_id'])
    remote = posix_absolute(approval['runtime_package'])
    require(remote.parts[1:2] == ('opt',) and len(remote.parts) >= 4 and remote.name == identifier,
            'A purpose-named package below a protected /opt parent is required')
    require(manifest['task_id'] == identifier and manifest['remote_package'] == str(remote), 'Manifest task/path mismatch')
    require(approval['reset_method'] == RESET_METHOD and approval['allow_clear_force_download'] is True
            and approval['managed_service'] is True, 'Explicit managed qualification/reset policy required')
    code = approval['code_sha256']
    require(type(code) is dict and MINIMUM_CODE <= set(code) and all(code_name(name) for name in code),
            'Complete bootstrap/native/linux/_mixlib code hash set required')
    artifacts = approval['artifacts']
    require(type(artifacts) is dict and set(artifacts) == ARTIFACT_NAMES, 'Exact qualification artifact set required')
    expected = set(code) | NATIVE_EXTRA | set(RUNTIME_SHA256) | {'approval.json', 'manifest.json'}
    expected |= {'artifacts/' + name for name in ARTIFACT_NAMES}
    require(set(files) == expected, 'Unexpected or missing package file')
    for name, expected_hash in code.items():
        require(sha(files[name]) == digest(expected_hash), 'Code hash mismatch: ' + name)
    for name, record in artifacts.items():
        require(type(record) is dict and set(record) == {'path', 'sha256'}
                and record['path'] == str(remote / 'artifacts' / name), 'Artifact path/schema mismatch: ' + name)
        require(sha(files['artifacts/' + name]) == digest(record['sha256']), 'Artifact hash mismatch: ' + name)
    for name, expected_hash in {**ORIGINAL_SHA256, **RECOVERY_SHA256}.items():
        require(sha(files['artifacts/' + name]) == expected_hash, 'Fixed original SHA256 mismatch: ' + name)
    for name, expected_hash in RUNTIME_SHA256.items():
        require(sha(files[name]) == expected_hash, 'Pinned runtime SHA256 mismatch: ' + name)
    return manifest, approval


def preflight(archive, archive_sha256, approval_sha256):
    raw = read_regular(archive, MAX_ARCHIVE)
    require(sha(raw) == digest(archive_sha256), 'Externally pinned archive SHA256 mismatch')
    files = unpack_checked(raw)
    manifest, approval = validate_payload(files, approval_sha256)
    return files, manifest, approval


def unit_text(approval, approval_sha256):
    # Restricted paths/ID make systemd expansion (%/$/whitespace/quotes) impossible.
    remote = str(posix_absolute(approval['runtime_package']))
    task_id(approval['task_id'])
    digest(approval_sha256)
    command = (f'/usr/bin/python3 -I -B {remote}/tools/bootstrap_ota_on_pi.py '
               f'--approval {remote}/approval.json --approval-sha256 {approval_sha256} --execute')
    return ('[Unit]\nDescription=Reviewed binary current-A no-write qualification\n'
            '[Service]\nType=exec\nUser=pi\nGroup=dialout\n'
            f'WorkingDirectory={remote}\nExecStart={command}\n'
            f'ExecStopPost={command} --recover-service\n'
            'Restart=no\nRuntimeMaxSec=1500s\nTimeoutStartSec=1500s\n'
            'TimeoutStopSec=60s\nKillMode=control-group\nUMask=0077\n').encode('ascii')


def protected_directory(path):
    path = Path(path)
    require(path.is_absolute() and path.resolve(strict=True) == path, 'Canonical existing directory required')
    for current in (path, *path.parents):
        info = current.lstat()
        require(stat.S_ISDIR(info.st_mode) and info.st_uid == 0 and not info.st_mode & 0o022
                and info.st_mode & 0o001, 'Root-owned protected, pi-traversable ancestors required: ' + str(current))


def require_absent(path):
    require(not os.path.lexists(path), 'Existing destination/task refused: ' + str(path))


def unit_absent(name):
    result = subprocess.run(['/usr/bin/systemctl', 'show', name, '--no-pager', '-p',
                             'LoadState,ActiveState,SubState,MainPID,ControlPID,UnitFileState'],
                            capture_output=True, text=True, timeout=10, check=False)
    fields = dict(line.split('=', 1) for line in result.stdout.splitlines() if '=' in line)
    require(result.returncode in (0, 4) and fields.get('LoadState') == 'not-found'
            and fields.get('ActiveState') == 'inactive' and fields.get('SubState') == 'dead'
            and fields.get('MainPID') == fields.get('ControlPID') == '0'
            and fields.get('UnitFileState', '') == '', 'Existing/active/unknown systemd unit refused')


def install_environment(approval):
    require(sys.platform == 'linux' and sys.flags.isolated and os.geteuid() == 0,
            'Installation requires isolated (-I) Linux root')
    import grp
    import pwd
    worker = pwd.getpwnam('pi')
    group = grp.getgrnam('dialout')
    require(worker.pw_uid != 0 and (worker.pw_gid == group.gr_gid or 'pi' in group.gr_mem),
            'Ordinary pi account with dialout membership required')
    remote = Path(approval['runtime_package'])
    protected_directory(remote.parent)
    protected_directory(UNIT_DIRECTORY)
    protected_directory(STATE.parent)
    require(STATE.resolve(strict=True) == STATE, 'Canonical existing state registry required')
    info = STATE.lstat()
    require(stat.S_ISDIR(info.st_mode) and info.st_uid == worker.pw_uid and not info.st_mode & 0o077,
            'Existing private pi state registry required; never provisioned here')
    require_absent(STATE / approval['task_id'])
    for category in ('reset-use', 'qualification-use'):
        require_absent(STATE / (category + '-' + approval['task_id'] + '.json'))
    # No task files, locks, sudo policy, registry directories or authorization
    # records are made; a surviving global claim also blocks reuse.
    require_absent(remote)
    name = 'mixos-bootstrap-' + approval['task_id'] + '.service'
    require_absent(UNIT_DIRECTORY / name)
    unit_absent(name)
    return remote, UNIT_DIRECTORY / name


def sync_directory(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def new_root_file(path, data, mode):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'wb') as stream:
        stream.write(data)
        stream.flush()
        os.chown(path, 0, 0, follow_symlinks=False)
        os.fchmod(stream.fileno(), mode)
        os.fsync(stream.fileno())


def verify_installed(root, files):
    expected_dirs = directory_names(files)
    observed_files, observed_dirs = set(), set()
    for path in root.rglob('*'):
        name = path.relative_to(root).as_posix()
        info = path.lstat()
        require(info.st_uid == info.st_gid == 0, 'Installed owner mismatch')
        if stat.S_ISDIR(info.st_mode):
            require(stat.S_IMODE(info.st_mode) == 0o555, 'Installed directory mode mismatch')
            observed_dirs.add(name)
        else:
            require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1
                    and stat.S_IMODE(info.st_mode) == 0o444, 'Unsafe installed file')
            require(name in files and read_regular(path) == files[name], 'Installed file bytes differ')
            observed_files.add(name)
    info = root.lstat()
    require(stat.S_ISDIR(info.st_mode) and info.st_uid == info.st_gid == 0
            and stat.S_IMODE(info.st_mode) == 0o555, 'Installed root protection mismatch')
    require(observed_files == set(files) and observed_dirs == expected_dirs, 'Installed exact set mismatch')


def install(files, approval, approval_sha256):
    require(approval['approved'] is True, 'External approval required; draft cannot be installed')
    # The public writer also rechecks the entire payload, not just the CLI.
    _, checked = validate_payload(files, approval_sha256)
    require(checked == approval, 'Approval changed after preflight')
    remote, unit = install_environment(approval)
    contents = unit_text(approval, approval_sha256)
    # Exclusive root directory acts as a reservation. Partial failures are left
    # for manual inspection and block reuse; no rollback/retry can erase proof.
    remote.mkdir(mode=0o700)
    os.chown(remote, 0, 0, follow_symlinks=False)
    directories = sorted(directory_names(files), key=lambda name: (name.count('/'), name))
    for name in directories:
        path = remote / name
        path.mkdir(mode=0o700)
        os.chown(path, 0, 0, follow_symlinks=False)
    for name, data in sorted(files.items()):
        new_root_file(remote / name, data, 0o444)
    for name in reversed(directories):
        path = remote / name
        path.chmod(0o555)
        sync_directory(path)
    remote.chmod(0o555)
    sync_directory(remote)
    sync_directory(remote.parent)
    verify_installed(remote, files)
    # Recheck immediately before publication; new_root_file is also O_EXCL.
    unit_absent(unit.name)
    new_root_file(unit, contents, 0o644)
    sync_directory(unit.parent)
    subprocess.run(['/usr/bin/systemctl', 'daemon-reload'], check=True, timeout=30)
    return dict(installed=True, started=False, enabled=False, task_state_created=False,
                package=str(remote), unit=str(unit))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--archive', required=True, type=Path)
    parser.add_argument('--archive-sha256', required=True)
    parser.add_argument('--approval-sha256', required=True)
    parser.add_argument('--install', action='store_true', help='Separate authorized root installation; NEVER start')
    args = parser.parse_args(argv)
    files, manifest, approval = preflight(args.archive, args.archive_sha256, args.approval_sha256)
    require(sha(read_regular(Path(__file__).resolve())) == manifest['installer_sha256'],
            'Installer differs from archive-bound reviewed installer')
    if args.install:
        script = Path(__file__).resolve()
        protected_directory(script.parent)
        info = script.lstat()
        require(info.st_uid == info.st_gid == 0 and stat.S_IMODE(info.st_mode) == 0o444,
                'Independently staged root-owned 0444 installer required')
        result = install(files, approval, args.approval_sha256)
    else:
        result = dict(dry_run=True, installed=False, started=False, device_opened=False,
                      task_state_created=False, approved=approval['approved'], task_id=approval['task_id'],
                      archive_sha256=args.archive_sha256, approval_sha256=args.approval_sha256,
                      remote_package=manifest['remote_package'], files=len(files),
                      host_preconditions_checked=False,
                      unit=unit_text(approval, args.approval_sha256).decode('ascii'))
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception as exc:
        print('STOP: ' + str(exc) + '; preserve partial package; no automatic retry.', file=sys.stderr)
        raise SystemExit(2)
