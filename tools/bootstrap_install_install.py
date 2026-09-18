#!/usr/bin/env python3
"""Offline first-install/boot-only package preflight; NEVER starts a task.

Uses the unchanged qualification installer's archive/filesystem primitives,
NOT its qualify-only payload validator or public install function. Stage this
script AND bootstrap_qualification_install.py beside it. Authenticate both
independently. --primitives-sha256 is required even for preflight; the companion
is hash-checked BEFORE executing its code, and never loaded from the archive.
For root execution both scripts must already be canonical root-owned 0444
single-link files beneath protected, traversable ancestors, with Python -I.
Default is read-only. --install consumes an external approval and only publishes
an immutable package/unit and daemon-reloads; no start/enable/claims/task state.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys
import types

SCHEMA = 'mixos-bootstrap-install-package/v1'
ROUTE = 'mixos-reviewed-candidate-bootstrap/v1'
PRIMITIVES_NAME = 'bootstrap_qualification_install.py'
SCRIPT_NAME = 'bootstrap_install_install.py'
RISK_ACCEPTANCE = {
    'unmeasured_candidate_startup': True,
    'possible_manual_recovery': True,
    'no_fault_injection': True,
    'preserve_baseline_a': True,
}
COMMON_FIELDS = frozenset({'schema', 'approved', 'task_id', 'kind', 'bootloader_evidence_mode',
    'reset_method', 'allow_clear_force_download', 'managed_service', 'code_sha256',
    'runtime_package', 'artifacts'})
EXTRA_COMMON = frozenset({'candidate_evidence_sha256', 'runtime_risk_acceptance'})
KIND_FIELDS = {'install': frozenset({'qualification_task', 'qualification_sha256'}),
               'boot-only': frozenset({'source_task', 'source_verified_sha256', 'expected_sha256'})}


def _protected_script(path):
    """Small trust bootstrap: no imports from staging before these checks."""
    if not (sys.platform == 'linux' and sys.flags.isolated and os.geteuid() == 0):
        raise ValueError('Root installer requires isolated (-I) Linux root')
    if not path.is_absolute() or path.resolve(strict=True) != path:
        raise ValueError('Canonical installer/companion required')
    info = path.lstat()
    if not (stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and info.st_uid == info.st_gid == 0
            and stat.S_IMODE(info.st_mode) == 0o444):
        raise ValueError('Independently staged root-owned 0444 installer/companion required')
    for parent in path.parents:
        info = parent.lstat()
        if not (stat.S_ISDIR(info.st_mode) and info.st_uid == 0 and not info.st_mode & 0o022
                and info.st_mode & 0o001):
            raise ValueError('Protected, traversable root-owned script ancestors required')


def _load_primitives(expected_sha256=None, *, protected=False):
    script = Path(__file__).absolute()
    path = script.parent / PRIMITIVES_NAME
    if protected:
        _protected_script(script)
        _protected_script(path)
    if path.resolve(strict=True) != path:
        raise ValueError('Canonical primitive companion required')
    before = path.lstat()
    if not (stat.S_ISREG(before.st_mode) and before.st_nlink == 1 and before.st_size <= 1024 * 1024):
        raise ValueError('Bounded single-link primitive companion required')
    fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_BINARY', 0))
    with os.fdopen(fd, 'rb') as stream:
        opened = os.fstat(stream.fileno())
        raw = stream.read(1024 * 1024 + 1)
        after = os.fstat(stream.fileno())
    if not ((opened.st_dev, opened.st_ino) == (before.st_dev, before.st_ino)
            and len(raw) == before.st_size and after.st_nlink == 1
            and (after.st_size, after.st_mtime_ns) == (before.st_size, before.st_mtime_ns)):
        raise ValueError('Primitive companion changed during read')
    actual = hashlib.sha256(raw).hexdigest()
    if expected_sha256 is not None and (not isinstance(expected_sha256, str)
            or not re.fullmatch('[0-9a-f]{64}', expected_sha256) or actual != expected_sha256):
        raise ValueError('External primitive companion SHA256 mismatch')
    module = types.ModuleType('_reviewed_bootstrap_install_primitives')
    module.__file__ = str(path)
    # Only independently authenticated sibling bytes, never archive code.
    exec(compile(raw, str(path), 'exec'), module.__dict__)
    module.reviewed_source_sha256 = actual
    return module


# Library imports are trusted local development code, not the remote CLI path.
# The CLI authenticates the companion explicitly in main before loading it.
safe = _load_primitives() if __name__ != '__main__' else None


def task_id(value):
    safe.require(isinstance(value, str) and re.fullmatch('[a-z0-9][a-z0-9-]{7,79}', value),
                 'Invalid explicit task ID')
    return value


def task_spec(value, kind):
    safe.require(kind in KIND_FIELDS, 'Only explicit install or boot-only kind permitted')
    safe.require(type(value) is dict and set(value) == EXTRA_COMMON | KIND_FIELDS[kind],
                 'Exact kind-specific task-spec fields required')
    safe.digest(value['candidate_evidence_sha256'])
    risk = value['runtime_risk_acceptance']
    safe.require(type(risk) is dict and set(risk) == set(RISK_ACCEPTANCE)
                 and all(risk[key] is True for key in RISK_ACCEPTANCE),
                 'Exact explicit runtime risk acceptance required')
    if kind == 'install':
        task_id(value['qualification_task'])
        safe.digest(value['qualification_sha256'])
    else:
        task_id(value['source_task'])
        safe.digest(value['source_verified_sha256'])
        safe.digest(value['expected_sha256'])
    return value


def artifact_names(names):
    safe.require(type(names) in (dict, set, frozenset) and safe.ARTIFACT_NAMES <= set(names)
                 and {'candidate', 'candidate_review'} <= set(names),
                 'All fourteen recovery/original inputs plus candidate and candidate_review required')
    for name in names:
        safe.relative_name(name)
    safe.require(any(name.startswith('candidate_evidence/') for name in names),
                 'Pinned candidate_evidence/* materials required')
    safe.require(not set(names).intersection(safe.directory_names(names)), 'Artifact file/directory collision')
    return set(names)


def validate_payload(files, approval_sha256):
    """Own route validator. Never call or relax qualification.validate_payload."""
    safe.require('manifest.json' in files and 'approval.json' in files, 'Missing manifest/approval')
    for name in files:
        safe.relative_name(name)
    manifest = safe.strict_json(files['manifest.json'])
    safe.require(set(manifest) == {'schema', 'task_id', 'remote_package', 'files',
                 'installer_sha256', 'primitives_sha256'} and manifest['schema'] == SCHEMA,
                 'Unsupported install package manifest')
    safe.digest(manifest['installer_sha256'])
    safe.digest(manifest['primitives_sha256'])
    payload = {name: data for name, data in files.items() if name != 'manifest.json'}
    records = manifest['files']
    safe.require(type(records) is dict and all(type(r) is dict and set(r) == {'bytes', 'sha256', 'mode'}
                 and type(r['bytes']) is int and r['bytes'] >= 0 for r in records.values())
                 and records == safe.file_records(payload), 'Manifest exact file set/size/hash/mode mismatch')
    safe.require(safe.sha(files['approval.json']) == safe.digest(approval_sha256),
                 'Externally pinned approval SHA256 mismatch')
    approval = safe.strict_json(files['approval.json'])
    kind = approval.get('kind')
    safe.require(type(kind) is str and kind in KIND_FIELDS, 'Only install/boot-only approved route permitted')
    safe.require(set(approval) == COMMON_FIELDS | EXTRA_COMMON | KIND_FIELDS[kind]
                 and type(approval['schema']) is int and approval['schema'] == 1
                 and type(approval['approved']) is bool, 'Exact install approval schema required')
    task_spec({key: approval[key] for key in EXTRA_COMMON | KIND_FIELDS[kind]}, kind)
    safe.require(approval['bootloader_evidence_mode'] == ROUTE, 'Explicit reviewed candidate route required')
    identifier = task_id(approval['task_id'])
    source = approval['qualification_task'] if kind == 'install' else approval['source_task']
    safe.require(identifier != source, 'Task must differ from its prerequisite/source task')
    remote = safe.posix_absolute(approval['runtime_package'])
    safe.require(remote.parts[1:2] == ('opt',) and len(remote.parts) >= 4 and remote.name == identifier,
                 'Task-named immutable package beneath protected /opt parent required')
    safe.require(manifest['task_id'] == identifier and manifest['remote_package'] == str(remote),
                 'Manifest task/path mismatch')
    safe.require(approval['reset_method'] == safe.RESET_METHOD
                 and approval['allow_clear_force_download'] is True and approval['managed_service'] is True,
                 'Explicit managed/reset policy required')
    code = approval['code_sha256']
    safe.require(type(code) is dict and safe.MINIMUM_CODE <= set(code)
                 and all(safe.code_name(name) for name in code), 'Complete approved local code set required')
    names = artifact_names(approval['artifacts'])
    expected = set(code) | safe.NATIVE_EXTRA | set(safe.RUNTIME_SHA256) | {'approval.json', 'manifest.json'}
    expected |= {'artifacts/' + name for name in names}
    safe.require(set(files) == expected, 'Unexpected or missing package file')
    for name, expected_hash in code.items():
        safe.require(safe.sha(files[name]) == safe.digest(expected_hash), 'Code hash mismatch: ' + name)
    for name, record in approval['artifacts'].items():
        safe.require(type(record) is dict and set(record) == {'path', 'sha256'}
                     and record['path'] == str(remote / 'artifacts' / name), 'Artifact path/schema mismatch: ' + name)
        safe.require(safe.sha(files['artifacts/' + name]) == safe.digest(record['sha256']),
                     'Artifact hash mismatch: ' + name)
    for name, expected_hash in {**safe.ORIGINAL_SHA256, **safe.RECOVERY_SHA256}.items():
        safe.require(safe.sha(files['artifacts/' + name]) == expected_hash, 'Fixed original SHA256 mismatch: ' + name)
    for name, expected_hash in safe.RUNTIME_SHA256.items():
        safe.require(safe.sha(files[name]) == expected_hash, 'Pinned runtime SHA256 mismatch: ' + name)
    return manifest, approval


def preflight(archive, archive_sha256, approval_sha256):
    raw = safe.read_regular(archive, safe.MAX_ARCHIVE)
    safe.require(safe.sha(raw) == safe.digest(archive_sha256), 'Externally pinned archive SHA256 mismatch')
    files = safe.unpack_checked(raw)
    manifest, approval = validate_payload(files, approval_sha256)
    return files, manifest, approval


def unit_text(approval, approval_sha256):
    remote = str(safe.posix_absolute(approval['runtime_package']))
    task_id(approval['task_id'])
    safe.digest(approval_sha256)
    safe.require(approval['kind'] in KIND_FIELDS and approval['bootloader_evidence_mode'] == ROUTE,
                 'Install/boot-only unit route required')
    command = (f'/usr/bin/python3 -I -B {remote}/tools/bootstrap_ota_on_pi.py '
               f'--approval {remote}/approval.json --approval-sha256 {approval_sha256} --execute')
    return ('[Unit]\nDescription=Reviewed candidate bootstrap ' + approval['kind'] + '\n'
            '[Service]\nType=exec\nUser=pi\nGroup=dialout\n'
            f'WorkingDirectory={remote}\nExecStart={command}\n'
            f'ExecStopPost={command} --recover-service\n'
            'Restart=no\nRuntimeMaxSec=1500s\nTimeoutStartSec=1500s\n'
            'TimeoutStopSec=60s\nKillMode=control-group\nUMask=0077\n').encode('ascii')


def prerequisite_unused(approval):
    """Read-only global claim check; execution owns the eventual atomic claim."""
    if approval['kind'] == 'install':
        category, source = 'qualification-use', approval['qualification_task']
    else:
        category, source = 'reset-use', approval['source_task']
    safe.require_absent(safe.STATE / (category + '-' + task_id(source) + '.json'))


def install_environment(approval):
    # Generic protected parent/account/registry/destination/unit checks only.
    # This helper does NOT call the original qualify-only payload validator.
    remote, unit = safe.install_environment(approval)
    prerequisite_unused(approval)
    return remote, unit


def authenticate_installers(manifest):
    script = Path(__file__).absolute()
    _protected_script(script)
    _protected_script(script.parent / PRIMITIVES_NAME)
    safe.require(safe.sha(safe.read_regular(script)) == manifest['installer_sha256'],
                 'Installer differs from archive-bound reviewed installer')
    safe.require(safe.sha(safe.read_regular(script.parent / PRIMITIVES_NAME))
                 == manifest['primitives_sha256'] == safe.reviewed_source_sha256,
                 'Primitive companion differs from reviewed manifest/loaded bytes')


def install(files, approval, approval_sha256):
    safe.require(approval.get('approved') is True, 'External approval required; draft cannot be installed')
    manifest, checked = validate_payload(files, approval_sha256)
    safe.require(checked == approval, 'Approval changed after preflight')
    authenticate_installers(manifest)
    remote, unit = install_environment(approval)
    contents = unit_text(approval, approval_sha256)
    # Deliberately small independent writer: safe.install hard-codes qualify.
    # All actual filesystem primitives below are unchanged, reviewed helpers.
    remote.mkdir(mode=0o700)
    os.chown(remote, 0, 0, follow_symlinks=False)
    directories = sorted(safe.directory_names(files), key=lambda name: (name.count('/'), name))
    for name in directories:
        path = remote / name
        path.mkdir(mode=0o700)
        os.chown(path, 0, 0, follow_symlinks=False)
    for name, data in sorted(files.items()):
        safe.new_root_file(remote / name, data, 0o444)
    for name in reversed(directories):
        path = remote / name
        path.chmod(0o555)
        safe.sync_directory(path)
    remote.chmod(0o555)
    safe.sync_directory(remote)
    safe.sync_directory(remote.parent)
    safe.verify_installed(remote, files)
    prerequisite_unused(approval)
    safe.unit_absent(unit.name)
    safe.new_root_file(unit, contents, 0o444)
    safe.sync_directory(unit.parent)
    safe.subprocess.run(['/usr/bin/systemctl', 'daemon-reload'], check=True, timeout=30)
    return dict(installed=True, started=False, enabled=False, task_state_created=False,
                claims_created=False, package=str(remote), unit=str(unit))


def main(argv=None):
    global safe
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--archive', required=True, type=Path)
    parser.add_argument('--archive-sha256', required=True)
    parser.add_argument('--approval-sha256', required=True)
    parser.add_argument('--primitives-sha256', required=True,
                        help='Independent pin for the unchanged sibling qualification installer')
    parser.add_argument('--install', action='store_true', help='Separate root publication only; NEVER start/enable')
    args = parser.parse_args(argv)
    root = sys.platform == 'linux' and os.geteuid() == 0
    safe = _load_primitives(args.primitives_sha256, protected=args.install or root)
    files, manifest, approval = preflight(args.archive, args.archive_sha256, args.approval_sha256)
    safe.require(safe.sha(safe.read_regular(Path(__file__).absolute())) == manifest['installer_sha256'],
                 'Installer differs from archive-bound reviewed installer')
    safe.require(manifest['primitives_sha256'] == args.primitives_sha256, 'Manifest primitive SHA256 mismatch')
    if args.install:
        result = install(files, approval, args.approval_sha256)
    else:
        result = dict(dry_run=True, installed=False, started=False, device_opened=False,
                      task_state_created=False, claims_created=False, approved=approval['approved'],
                      task_id=approval['task_id'], archive_sha256=args.archive_sha256,
                      approval_sha256=args.approval_sha256, primitives_sha256=args.primitives_sha256,
                      remote_package=manifest['remote_package'], files=len(files),
                      host_preconditions_checked=False, candidate_semantics_validated=False,
                      unit=unit_text(approval, args.approval_sha256).decode('ascii'))
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception as exc:
        print('STOP: ' + str(exc) + '; preserve partial package; no automatic retry.', file=sys.stderr)
        raise SystemExit(2)
