#!/usr/bin/env python3
"""Observe an already running ESP application without ROM/reset/Flash effects.

This is a separate, one-use task after an install writer has produced an exact
verified.json. It validates the immutable writer package and saved readback
before importing its code, then calls only PiBackend.observe_boot(). It never
calls prepare(), connect(), esptool, enter_boot(), watchdog_reset(), read_full(),
write_once(), BEGIN, or any Flash operation.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import math
import os
from pathlib import Path
import re
import stat
import subprocess
import sys

SCHEMA = 'mixos-observe-existing-app/v1'
KIND = 'observe-existing'
OUTCOME_B = 'candidate-confirmed'
OUTCOME_A = 'baseline-restored'
ROUTE = 'mixos-reviewed-candidate-bootstrap/v1'
SOURCE_BINDING = '6fd6c522d1be081c7c8527fcd5931ba042ba5ea030d6c661f58f1336c3c21d38'
STATE = Path('/var/lib/mixos/bootstrap-ota')
SOURCE_REQUIRED = frozenset({'schema', 'task_id', 'kind', 'approved', 'bootloader_evidence_mode',
    'reset_method', 'allow_clear_force_download', 'managed_service', 'code_sha256',
    'runtime_package', 'artifacts', 'candidate_evidence_sha256', 'runtime_risk_acceptance',
    'qualification_task', 'qualification_sha256'})
APPROVAL_FIELDS = frozenset({'schema', 'approved', 'task_id', 'source_task', 'source_package',
    'source_approval_sha256', 'source_manifest_sha256', 'source_verified_sha256',
    'expected_sha256', 'observer_sha256', 'expected_usb_number',
    'reset_allowed', 'host_flash_programming_allowed', 'health_ack_allowed'})


class Refused(ValueError):
    """An observation is not sufficiently bound or is outside this route."""


def require(condition, message):
    if not condition:
        raise Refused(message)


def digest(value):
    require(type(value) is str and re.fullmatch(r'[0-9a-f]{64}', value), 'Invalid SHA256')
    return value


def sha(data):
    return hashlib.sha256(data).hexdigest()


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()


def strict_json(raw):
    def pairs(items):
        out = {}
        for key, value in items:
            require(key not in out, 'Duplicate JSON field: ' + key)
            out[key] = value
        return out
    try:
        value = json.loads(raw, object_pairs_hook=pairs,
                           parse_constant=lambda value: require(False, 'Non-finite JSON number'))
    except (ValueError, UnicodeError) as exc:
        raise Refused('Invalid JSON: ' + str(exc)) from exc
    require(type(value) is dict, 'JSON object required')
    return value


def _canonical(path, *, directory=False):
    path = Path(path)
    require(path.is_absolute() and path.resolve(strict=True) == path, 'Canonical absolute path required')
    info = path.lstat()
    require((stat.S_ISDIR(info.st_mode) if directory else
             stat.S_ISREG(info.st_mode) and info.st_nlink == 1)
            and not stat.S_ISLNK(info.st_mode),
            'Regular non-link path required: ' + str(path))
    return path


def _protected_ancestors(path):
    for parent in path.parents:
        info = parent.lstat()
        require(stat.S_ISDIR(info.st_mode) and info.st_uid == info.st_gid == 0
                and not info.st_mode & 0o022,
                'Root ancestor is not protected: ' + str(parent))


def _protected_observer(path):
    path = _canonical(path)
    info = path.lstat()
    require(info.st_uid == info.st_gid == 0 and stat.S_IMODE(info.st_mode) == 0o444,
            'Observer/approval must be root:root immutable 0444')
    _protected_ancestors(path)
    return path


def _read(path, limit=32 * 1024 * 1024):
    path = _canonical(path)
    info = path.lstat()
    require(info.st_size <= limit, 'Artifact too large: ' + str(path))
    return path.read_bytes()


def _check_root_tree(path):
    root = _canonical(path, directory=True)
    _protected_ancestors(root)
    def walk_error(error):
        raise error
    for current, dirs, files in os.walk(root, followlinks=False, onerror=walk_error):
        current_path = Path(current)
        info = current_path.lstat()
        require(info.st_uid == info.st_gid == 0 and not info.st_mode & 0o022 and stat.S_ISDIR(info.st_mode),
                'Source package directory is not protected: ' + str(current_path))
        for name in dirs + files:
            child = current_path / name
            child_info = child.lstat()
            safe_type = (stat.S_ISDIR(child_info.st_mode) or
                         (stat.S_ISREG(child_info.st_mode) and child_info.st_nlink == 1
                          and stat.S_IMODE(child_info.st_mode) == 0o444))
            require(safe_type and not stat.S_ISLNK(child_info.st_mode)
                    and not child_info.st_mode & 0o022
                    and child_info.st_uid == child_info.st_gid == 0,
                    'Source package contains unsafe child: ' + str(child))
    return root


def _relative_name(name):
    require(type(name) is str and name and not name.startswith('/') and '\\' not in name,
            'Invalid package relative name')
    parts = name.split('/')
    require(all(part not in ('', '.', '..') for part in parts), 'Unsafe package path')
    return name


def validate_package(package, approval_sha256, manifest_sha256):
    """Validate the installed source package entirely before importing it."""
    package = _check_root_tree(package)
    manifest_path = package / 'manifest.json'
    approval_path = package / 'approval.json'
    manifest_raw = _read(manifest_path, 4 * 1024 * 1024)
    approval_raw = _read(approval_path, 2 * 1024 * 1024)
    require(sha(manifest_raw) == digest(manifest_sha256), 'Source manifest hash mismatch')
    require(sha(approval_raw) == digest(approval_sha256), 'Source approval hash mismatch')
    manifest = strict_json(manifest_raw)
    approval = strict_json(approval_raw)
    require(manifest.get('schema') == 'mixos-bootstrap-install-package/v1'
            and manifest.get('task_id') == approval.get('task_id')
            and manifest.get('remote_package') == str(package),
            'Source manifest task/path binding mismatch')
    records = manifest.get('files')
    require(type(records) is dict and 'approval.json' in records, 'Source package file records missing')
    expected = set(records) | {'manifest.json'}
    actual = set()
    for current, _, names in os.walk(package, followlinks=False):
        for name in names:
            actual.add((Path(current) / name).relative_to(package).as_posix())
    require(actual == expected, 'Source package file set changed')
    for name, record in records.items():
        _relative_name(name)
        raw = _read(package / name, 32 * 1024 * 1024)
        require(type(record) is dict and set(record) == {'bytes', 'sha256', 'mode'}
                and type(record['bytes']) is int and record['bytes'] == len(raw)
                and digest(record['sha256']) == sha(raw)
                and type(record['mode']) is str and record['mode'] == '0444'
                and stat.S_IMODE((package / name).lstat().st_mode) == 0o444,
                'Source package record mismatch: ' + name)
    require(approval.get('approved') is True and approval.get('kind') == 'install',
            'Source package must be an approved install package')
    return package, manifest, approval, manifest_raw, approval_raw


def _source_verified(source_task, expected_sha256, source_verified_sha256):
    task = STATE / source_task
    _canonical(task, directory=True)
    verified_path = task / 'verified.json'
    verified_raw = _read(verified_path, 2 * 1024 * 1024)
    require(sha(verified_raw) == digest(source_verified_sha256), 'Source verified receipt hash mismatch')
    verified = strict_json(verified_raw)
    require(verified.get('schema') == 1 and verified.get('kind') == 'verified-install'
            and verified.get('task_id') == source_task and verified.get('reset_sent') is False,
            'Source verified install receipt required')
    require(verified.get('expected_sha256') == digest(expected_sha256), 'Source full-readback binding mismatch')
    readback = _read(task / 'flash-readback-8MB.bin', 8 * 1024 * 1024)
    require(len(readback) == 0x800000 and sha(readback) == expected_sha256,
            'Saved source full readback mismatch')
    service_path = task / 'service.json'
    service = strict_json(_read(service_path, 64 * 1024))
    require(service.get('kind') == 'install' and service.get('original') == 'active'
            and service.get('state') == 'held-stopped' and service.get('error') is None,
            'Source service must remain held stopped with original active state')
    return task, verified, readback, service


def validate_approval(raw, *, approval_sha256=None, observer_bytes=None, package_loader=validate_package):
    """Pure policy plus explicitly injected filesystem reads; no device effect."""
    approval = strict_json(raw)
    require(approval.get('schema') == SCHEMA and approval.get('approved') is True,
            'Approved existing-app observation required')
    require(set(approval) == APPROVAL_FIELDS, 'Exact observer approval fields required')
    require(type(approval['task_id']) is str
            and re.fullmatch(r'[a-z0-9][a-z0-9-]{7,79}', approval['task_id'])
            and approval['task_id'] != approval['source_task'], 'Distinct valid task IDs required')
    require(approval['source_task'] == 'install-health-ack-20260917',
            'Only the explicitly completed source install is accepted')
    require(type(approval['source_package']) is str and Path(approval['source_package']).is_absolute(),
            'Absolute source package required')
    for key in ('source_approval_sha256', 'source_manifest_sha256', 'source_verified_sha256',
                'expected_sha256', 'observer_sha256'):
        digest(approval[key])
    require(approval.get('source_task') == 'install-health-ack-20260917',
            'Only the explicitly completed source install is accepted')
    require(type(approval['expected_usb_number']) is str and approval['expected_usb_number'].isdecimal()
            and 0 < int(approval['expected_usb_number']) < 128, 'Invalid expected USB number')
    require(approval['reset_allowed'] is False and approval['host_flash_programming_allowed'] is False
            and approval['health_ack_allowed'] is True, 'Observe-only effect policy required')
    if approval_sha256 is not None:
        require(sha(raw) == digest(approval_sha256), 'Observer approval hash mismatch')
    if observer_bytes is not None:
        require(sha(observer_bytes) == approval['observer_sha256'], 'Observer code hash mismatch')
    package, manifest, source_approval, manifest_raw, source_approval_raw = package_loader(
        approval['source_package'], approval['source_approval_sha256'], approval['source_manifest_sha256'])
    require(source_approval.get('task_id') == approval['source_task']
            and set(source_approval) == SOURCE_REQUIRED
            and source_approval.get('runtime_package') == str(package)
            and source_approval.get('candidate_evidence_sha256') == SOURCE_BINDING
            and source_approval.get('bootloader_evidence_mode') == ROUTE,
            'Source approval is not the exact candidate install')
    source_task, verified, readback, service = _source_verified(
        approval['source_task'], approval['expected_sha256'], approval['source_verified_sha256'])
    evidence = verified.get('execution_evidence')
    require(evidence == {'route': ROUTE, 'candidate_evidence_sha256': SOURCE_BINDING},
            'Source execution evidence binding mismatch')
    candidate = verified.get('candidate')
    baseline = verified.get('baseline')
    require(type(candidate) is dict and type(baseline) is dict, 'Source candidate/baseline facts missing')
    require(candidate.get('slot', 'ota_1') in ('ota_1', None), 'Candidate must be B')
    require(baseline.get('sha256') == '7875d9a513acb95463b72e785ebd160c70d03f85e965c3a30a93d954bb4cff5f'
            and baseline.get('elf_sha256') == 'cfacb3fe25931e918b8f46d1f840da8d57ba39ef799bd0af4629939b9185761a',
            'Protected A binding mismatch')
    candidate_record = source_approval.get('artifacts', {}).get('candidate', {})
    require(candidate_record.get('sha256') == candidate.get('sha256'), 'Source package candidate mismatch')
    return dict(approval=approval, package=package, manifest=manifest, source_approval=source_approval,
                source_task=source_task, verified=verified, readback=readback, service=service,
                candidate=candidate, baseline=baseline, source_manifest_sha256=sha(manifest_raw),
                source_approval_sha256=sha(source_approval_raw))


def _heartbeat(record):
    return (type(record) is dict and type(record.get('pings')) is int
            and record['pings'] >= 3
            and type(record.get('observed_seconds')) in (int, float)
            and math.isfinite(record['observed_seconds']) and record['observed_seconds'] >= 15)


def classify_observation(observed, facts):
    """Turn the existing host observer result into a non-reset receipt."""
    require(type(observed) is dict and observed.get('outcome') in (OUTCOME_A, OUTCOME_B),
            'Unknown existing-app observation outcome')
    running = observed.get('running')
    require(type(running) is dict, 'Observation running identity missing')
    if observed['outcome'] == OUTCOME_A:
        require(running.get('slot') == 'ota_0' and running.get('state') == 'valid'
                and running.get('address') == 0x10000
                and running.get('elf_sha256') == facts['baseline']['elf_sha256'],
                'Observed A is not the exact protected baseline')
        require(type(running.get('identity_queries')) is int and running['identity_queries'] == 1
                and _heartbeat(running.get('pre_identity_heartbeat')) and _heartbeat(running),
                'A observation requires healthy windows around one identity query')
        result_code = 2
    else:
        measurement = running.get('measurement')
        require(type(measurement) is dict, 'Observed B measurement missing')
        require(running.get('slot') == 'ota_1' and running.get('address') == 0x610000
                and running.get('state') == 'valid'
                and running.get('elf_sha256') == facts['candidate']['elf_sha256']
                and running.get('actual_file_verified') is True
                and measurement.get('maintenance_health_acknowledged') is True
                and measurement.get('actual_file_verified') is True
                and measurement.get('evidence_kind') == 'running-measurement',
                'Observed B lacks exact file/health acknowledgement proof')
        binding = measurement.get('binding')
        require(type(binding) is dict, 'Observed B measurement binding missing')
        require(all(type(binding.get(key)) is int for key in ('size', 'target'))
                and all(type(measurement.get(key)) is int for key in
                        ('running_slot', 'boot_slot', 'boot_id', 'image_state', 'error', 'flags'))
                and binding.get('sha256') == facts['candidate']['sha256']
                and binding.get('size') == facts['candidate']['bytes'] and binding.get('target') == 1
                and measurement.get('stored_sha256') == facts['candidate']['sha256']
                and measurement.get('elf_sha256') == facts['candidate']['elf_sha256']
                and measurement.get('running_slot') == measurement.get('boot_slot') == 1
                and type(measurement.get('boot_id')) is int and measurement['boot_id'] > 0
                and measurement.get('image_state') == 2 and measurement.get('result') == 'ok'
                and type(measurement.get('error')) is int and measurement['error'] == 0
                and type(measurement.get('flags')) is int and measurement['flags'] & 16,
                'Observed B measurement is not exact candidate/VALID/same-boot ACK proof')
        require(_heartbeat(running),
                'Observed B heartbeat window is too short')
        result_code = 0
    return dict(schema=1, kind='observed-existing-app', outcome=observed['outcome'],
                source_task=facts['approval']['source_task'], running=running,
                reset_sent=False, host_flash_programming=False, cause='unexplained',
                source_verified_sha256=facts['approval']['source_verified_sha256'],
                expected_sha256=facts['approval']['expected_sha256'],
                candidate_evidence_sha256=SOURCE_BINDING,
                result_code=result_code), result_code


def restore_allowed(result):
    require(type(result) is dict and result.get('kind') == 'observed-existing-app'
            and result.get('outcome') in (OUTCOME_A, OUTCOME_B)
            and result.get('reset_sent') is False and result.get('host_flash_programming') is False
            and result.get('cause') == 'unexplained', 'Only exact observation proof restores service')
    return True


def _fields(run, unit):
    result = run(['/usr/bin/systemctl', 'show', unit, '--no-pager', '-p',
                  'LoadState,ActiveState,SubState,MainPID,ControlPID,Result,ExecMainStatus,InvocationID'],
                 capture_output=True, text=True, timeout=10, check=True)
    return dict(line.split('=', 1) for line in result.stdout.splitlines() if '=' in line)


def _require_stopped(run, unit):
    fields = _fields(run, unit)
    require(fields.get('LoadState') == 'loaded' and fields.get('ActiveState') in ('inactive', 'failed')
            and fields.get('SubState') in ('dead', 'failed') and fields.get('MainPID') == '0'
            and fields.get('ControlPID') == '0', 'Required source unit is not stopped')
    return fields


def _app_usb_number(location):
    path = Path('/sys/bus/usb/devices') / location / 'devnum'
    raw = path.read_text().strip()
    require(raw.isdecimal() and 0 < int(raw) < 128, 'Invalid application USB number')
    return raw


def _claim(path, value):
    path = Path(path)
    require(not path.exists(), 'Observer source-use claim already exists')
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    with os.fdopen(fd, 'wb') as stream:
        stream.write(encoded(value)); stream.flush(); os.fsync(stream.fileno())
    if os.name == 'posix':
        fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try: os.fsync(fd)
        finally: os.close(fd)


def _require_worker():
    require(sys.platform == 'linux' and sys.flags.isolated and os.geteuid() != 0,
            'Execution/preflight requires an isolated ordinary Linux worker, not root')


def _check_state():
    root = _canonical(STATE, directory=True)
    _protected_ancestors(root)
    info = root.lstat()
    require(info.st_uid == os.geteuid() and not info.st_mode & 0o077,
            'State registry must be private to the worker (0700)')


def _load_source(facts):
    """Resolve every project import from the checked package, even under -I."""
    package = facts['package']
    paths = [package / 'tools', package / 'linux']
    records = facts['manifest']['files']
    require({'tools/_mixlib/__init__.py', 'tools/_mixlib/ota_bootstrap.py',
             'tools/bootstrap_ota_on_pi.py', 'tools/mixos_esp_update.py'} <= set(records),
            'Source package lacks the required observer backend modules')
    roots = {name.split('/')[1].removesuffix('.py') for name in records
             if name.startswith(('tools/', 'linux/')) and name.endswith('.py')}

    def verify_loaded():
        for name, module in tuple(sys.modules.items()):
            if name.split('.')[0] not in roots:
                continue
            origin = getattr(module, '__file__', None)
            require(origin is not None, 'Source module has no verified origin: ' + name)
            origin = Path(origin).resolve()
            require(origin.is_relative_to(package)
                    and origin.relative_to(package).as_posix() in records,
                    'Source module was loaded outside the verified package: ' + name)
            for search_path in getattr(module, '__path__', ()):
                require(Path(search_path).resolve() == origin.parent,
                        'Source package search path is not verified: ' + name)

    verify_loaded()
    # Never create __pycache__ in the immutable writer package.
    sys.dont_write_bytecode = True
    sys.path[:0] = [str(path) for path in paths]
    policy = importlib.import_module('_mixlib.ota_bootstrap')
    cli = importlib.import_module('bootstrap_ota_on_pi')
    native = importlib.import_module('mixos_esp_update')
    verify_loaded()
    return policy, cli, native


def _execution_context(task_id, run, *, recovery=False):
    """Only this invocation's worker/ExecStopPost may use its health evidence."""
    unit = 'mixos-bootstrap-' + task_id + '.service'
    fields = _fields(run, unit)
    if recovery:
        owned = (fields.get('ActiveState') == 'deactivating'
                 and fields.get('SubState') == 'stop-post'
                 and fields.get('MainPID') == '0'
                 and fields.get('ControlPID') == str(os.getpid()))
    else:
        owned = (fields.get('ActiveState') in ('activating', 'active')
                 and fields.get('MainPID') == str(os.getpid())
                 and fields.get('ControlPID') == '0')
    invocation = fields.get('InvocationID', '')
    require(fields.get('LoadState') == 'loaded' and owned
            and re.fullmatch(r'[0-9a-f]{32}', invocation) and invocation != '0' * 32,
            'Observer process/systemd invocation ownership mismatch')
    boot = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
    require(re.fullmatch(r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}', boot),
            'Invalid host boot identity')
    return dict(unit=unit, invocation_id=invocation, host_boot_id=boot)


def _binding(facts, approval_sha256, context):
    return dict(task_id=facts['approval']['task_id'], source_task=facts['approval']['source_task'],
                approval_sha256=approval_sha256, execution=context,
                reset_sent=False, host_flash_programming=False)


def _result(observed, facts, approval_sha256, context, usb_after):
    result, _ = classify_observation(observed, facts)
    result.update(_binding(facts, approval_sha256, context))
    result.update(usb_number_before=facts['approval']['expected_usb_number'], usb_number_after=usb_after)
    require(result['usb_number_after'] == result['usb_number_before'],
            'APP USB enumeration changed during observation')
    return result


def _restore_service(native, run):
    # The production helper uses absolute systemctl and sudo -n for start.
    return native.SystemdService(run=run).restore('active')


def _finish_service(facts, approval_sha256, context, native, run, *, recovery=False):
    """Caller holds both DeviceLock inodes until all receipts are durable.

    The observation is immutable. service.json is a separate durable completion
    receipt. A spent restore claim without completion is unknown, never retried.
    """
    task = _canonical(STATE / facts['approval']['task_id'], directory=True)
    require(not (task / 'observation-failed.json').exists(),
            'Observation failed or was not durably published; no service restoration')
    binding = _binding(facts, approval_sha256, context)
    task_claim = strict_json(_read(task / 'task-claim.json'))
    require(task_claim == dict(task_id=task.name, kind=KIND, approval_sha256=approval_sha256),
            'Observer task claim binding mismatch')
    require(strict_json(_read(task / 'approval.json')) == facts['approval'],
            'Saved observer approval mismatch')
    require(strict_json(_read(STATE / ('observe-use-' + facts['approval']['source_task'] + '.json')))
            == binding, 'Source observation claim binding mismatch')
    raw = _read(task / 'observe-result.json', 2 * 1024 * 1024)
    result = strict_json(raw)
    expected = _result(result, facts, approval_sha256, context, facts['approval']['expected_usb_number'])
    require(encoded(result) == encoded(expected), 'Observation result/task/approval/USB binding mismatch')
    restore_allowed(result)
    receipt_binding = dict(binding, observation_sha256=sha(raw))
    intent_path = task / 'service-restore-claim.json'
    service_path = task / 'service.json'
    require(not (task / 'service-restore-failed.json').exists(),
            'Service restore attempt is spent/unknown; no retry')
    if service_path.exists():
        require(strict_json(_read(intent_path)) == receipt_binding, 'Service restore claim mismatch')
        service = strict_json(_read(service_path))
        require(service == dict(receipt_binding, original='active', state='restored',
                                observed='active', error=None), 'Service completion binding mismatch')
        return dict(result, service=service, device_opened=not recovery)
    # Even a failed/timed-out start has spent the only allowed attempt. In
    # particular, never infer success by observing a service started elsewhere.
    require(not intent_path.exists(), 'Service restore attempt is spent/unknown; no retry')
    require(_execution_context(task.name, run, recovery=recovery) == context,
            'Observer invocation changed before service restoration')
    _require_stopped(run, 'mixos-bootstrap-' + facts['approval']['source_task'] + '.service')
    require(native.SystemdService(run=run).state() == 'inactive',
            'Service ownership changed before restoration')
    _claim(intent_path, receipt_binding)
    try:
        restored = _restore_service(native, run)
        service = dict(receipt_binding, **restored)
        require(restored == dict(original='active', state='restored', observed='active', error=None),
                'Service restoration outcome is unknown')
        _claim(service_path, service)
    except BaseException as exc:
        _claim(task / 'service-restore-failed.json',
               dict(receipt_binding, state='restore-failed', error=str(exc)))
        raise
    return dict(result, service=service, device_opened=not recovery)


def recover_service(facts, approval_sha256, native, run=subprocess.run):
    """ExecStopPost only: both maintenance locks, bound proof, no device access."""
    with native.DeviceLock(native.DEFAULT_LOCKS, native.DEVICE), native.termination_handler():
        context = _execution_context(facts['approval']['task_id'], run, recovery=True)
        task = STATE / facts['approval']['task_id']
        # Cleanup of a pre-observation failure is harmless, and grants nothing.
        if not task.exists() or not (task / 'observe-result.json').exists():
            return dict(service=dict(state='held-stopped'), device_opened=False)
        _source_verified(facts['approval']['source_task'], facts['approval']['expected_sha256'],
                         facts['approval']['source_verified_sha256'])
        return _finish_service(facts, approval_sha256, context, native, run, recovery=True)


def execute(approval_path, approval_sha256, *, execute=False, recover=False, preflight=False):
    """Default is offline validation. --preflight also checks isolated imports.

    Preflight does not query systemd/sysfs, acquire locks, consume a task, or
    construct a backend. Execution/cleanup run in mixos-bootstrap-<task>.service.
    """
    require(not (preflight and execute) and (not recover or execute),
            'Recovery requires --execute; preflight is mutually exclusive')
    if execute or preflight:
        _require_worker()
        _check_state()
    observer_path = Path(__file__).absolute()
    if execute or preflight:
        _protected_observer(observer_path)
        _protected_observer(Path(approval_path))
    raw = _read(approval_path, 2 * 1024 * 1024)
    facts = validate_approval(raw, approval_sha256=approval_sha256,
                              observer_bytes=_read(observer_path))
    approval = facts['approval']
    task_id = approval['task_id']
    if not execute and not preflight:
        return dict(task_id=task_id, validated=True, dry_run=True, device_opened=False)
    policy, cli, native = _load_source(facts)
    if preflight:
        return dict(task_id=task_id, validated=True, preflight=True,
                    imports_verified=True, device_opened=False, source_package=str(facts['package']))
    if recover:
        return recover_service(facts, sha(raw), native, run=subprocess.run)
    source_unit = 'mixos-bootstrap-' + approval['source_task'] + '.service'
    with native.DeviceLock(native.DEFAULT_LOCKS, native.DEVICE), native.termination_handler():
        context = _execution_context(task_id, subprocess.run)
        _require_stopped(subprocess.run, source_unit)
        _require_stopped(subprocess.run, 'mixosd.service')
        # Re-read mutable source evidence and the USB number only after locking.
        _source_verified(approval['source_task'], approval['expected_sha256'],
                         approval['source_verified_sha256'])
        require(_app_usb_number('5-1.2') == approval['expected_usb_number'],
                'Current application USB number differs from approved observation')
        claim = STATE / ('observe-use-' + approval['source_task'] + '.json')
        require(not claim.exists(), 'Source observation was already consumed')
        require(not (STATE / task_id).exists(), 'Observer task already exists')
        journal = policy.Journal(STATE, task_id, KIND, sha(raw))
        journal.save('approval.json', approval)  # Journal.save encodes objects, not bytes.
        _claim(claim, _binding(facts, sha(raw), context))
        try:
            # Observe-only skips prepare(); redirect its shared audit helper
            # without staging esptool or writing into the immutable package.
            cli.esp.ROOT = journal.path
            backend = cli.PiBackend(approval, journal)
            try:
                observed = backend.observe_boot(facts['candidate'], facts['baseline'])
            finally:
                backend.close()
            result = _result(observed, facts, sha(raw), context, _app_usb_number('5-1.2'))
            journal.save('observe-result.json', result)
        except BaseException as exc:
            _claim(journal.path / 'observation-failed.json',
                   dict(_binding(facts, sha(raw), context), error=str(exc)))
            raise
        return _finish_service(facts, sha(raw), context, native, subprocess.run)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--approval', type=Path, required=True)
    parser.add_argument('--approval-sha256', required=True)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--execute', action='store_true')
    mode.add_argument('--preflight', action='store_true',
                      help='Protected isolated import check; no systemd, device, lock or task effects')
    parser.add_argument('--recover-service', action='store_true',
                        help='Same-invocation ExecStopPost cleanup; requires --execute')
    args = parser.parse_args(argv)
    try:
        result = execute(args.approval, args.approval_sha256, execute=args.execute,
                         recover=args.recover_service, preflight=args.preflight)
        print(json.dumps(result, sort_keys=True))
        if result.get('outcome') == OUTCOME_A:
            return 2
        return 0
    except Exception as exc:
        print('STOP: ' + str(exc) + '; preserve observation state; no reset or retry.', file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
