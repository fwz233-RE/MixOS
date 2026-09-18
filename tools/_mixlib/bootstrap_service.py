"""Service ownership for explicitly approved, systemd-managed ROM tasks.

The caller must hold native DeviceLock throughout. No serial/reset/write API is
provided here. A durable ROM-entry marker prevents blind service restart after
an uncertain operation; successful qualification/boot proof permits recovery.
"""
import os
from pathlib import Path
import subprocess

from . import ota_bootstrap as policy
from .durable import durable_new
import mixos_esp_update as native


def verify_unit(approval, run=subprocess.run, recovery=False):
    unit = 'mixos-bootstrap-' + policy.task_id(approval['task_id']) + '.service'
    result = run(['systemctl', 'show', unit, '--no-pager', '-p',
                  'LoadState,ActiveState,SubState,MainPID,ControlPID'],
                 capture_output=True, text=True, timeout=10)
    fields = dict(line.split('=', 1) for line in result.stdout.splitlines() if '=' in line)
    valid = result.returncode == 0 and fields.get('LoadState') == 'loaded'
    if recovery:
        # ExecStopPost is a control process while the unit is deactivating;
        # requiring an inactive unit here would disable crash cleanup entirely.
        stop_post = (fields.get('ActiveState') == 'deactivating'
                     and fields.get('SubState') == 'stop-post'
                     and fields.get('ControlPID') == str(os.getpid()))
        stopped = (fields.get('ActiveState') in ('inactive', 'failed')
                   and fields.get('SubState') in ('dead', 'failed')
                   and fields.get('ControlPID') == '0')
        valid = valid and fields.get('MainPID') == '0' and (stop_post or stopped)
    else:
        valid = (valid and fields.get('ActiveState') in ('activating', 'active')
                 and fields.get('MainPID') == str(os.getpid())
                 and fields.get('ControlPID') == '0')
    policy.require(valid, 'Managed bootstrap process/unit ownership mismatch')


class ServiceOwner:
    def __init__(self, registry, approval, service=None, save=native.atomic_json):
        self.registry = Path(registry)
        self.approval = approval
        self.path = self.registry / policy.task_id(approval['task_id'])
        self.file = self.path / 'service.json'
        self.service = service or native.SystemdService()
        self.save = save
        self.record = None

    def __enter__(self):
        original = self.service.state()
        if self.approval['kind'] == 'boot-only':
            policy.require(original == 'inactive', 'Boot-only must begin with mixosd stopped')
            source = self.registry / policy.task_id(self.approval['source_task'])
            parent = native.strict_json(source / 'service.json')
            policy.require(parent.get('task_id') == source.name and parent.get('kind') == 'install'
                           and parent.get('original') in ('active', 'inactive')
                           and parent.get('state') == 'held-stopped', 'Source service ownership unavailable')
            original = parent['original']
        self.record = dict(schema=1, task_id=self.path.name, kind=self.approval['kind'],
                           original=original, state='stop-intent', error=None)
        self.save(self.file, self.record)
        try:
            self.service.stop()
            self.record['state'] = 'held-stopped'
            self.save(self.file, self.record)
        except BaseException:
            if self.approval['kind'] != 'boot-only':
                self._restore()
            raise
        return self

    def _proof_allows_restore(self):
        kind = self.approval['kind']
        if kind == 'qualify':
            file = self.path / 'qualification.json'
            if file.exists():
                proof = native.strict_json(file)
                policy.require(proof.get('schema') == 1 and proof.get('task_id') == self.path.name
                               and proof.get('kind') == 'qualification'
                               and proof.get('flash_programming') is False
                               and proof.get('exit') == policy.RESET_METHOD
                               and proof.get('app_identity') == policy.APP_IDENTITY
                               and proof.get('rom_identity') == policy.ROM_IDENTITY
                               and proof.get('before_sha256') == proof.get('after_sha256')
                               and native.full_hash(proof.get('after_sha256')),
                               'Invalid successful qualification proof')
                policy.validate_running(proof.get('running_after', {}),
                    policy.baseline_description(policy.Trust('0' * 64, '0' * 64)), 0)
                return True
        elif kind == 'boot-only':
            file = self.path / 'boot-result.json'
            if file.exists():
                proof = native.strict_json(file)
                source = self.registry / policy.task_id(self.approval['source_task']) / 'verified.json'
                policy.require(policy.sha(source.read_bytes()) == self.approval['source_verified_sha256'],
                               'Boot source proof changed')
                verified = native.strict_json(source)
                policy.require(verified.get('kind') == 'verified-install'
                               and verified.get('task_id') == self.approval['source_task']
                               and verified.get('expected_sha256') == self.approval['expected_sha256']
                               and proof.get('kind') == 'boot-only-result'
                               and proof.get('source_task') == self.approval['source_task']
                               and proof.get('reset_method') == policy.RESET_METHOD
                               and proof.get('flash_programming') is False
                               and proof.get('running', {}).get('actual_file_verified') is True
                               and proof.get('running', {}).get('measurement', {}).get('actual_file_verified') is True,
                               'Invalid successful boot proof')
                self._candidate_route_proof(verified, proof)
                policy.validate_running(proof.get('running', {}), verified['candidate'], 1)
                return True
            fallback = self.path / 'boot-fallback.json'
            if fallback.exists():
                source = self.registry / policy.task_id(self.approval['source_task']) / 'verified.json'
                policy.require(policy.sha(source.read_bytes()) == self.approval['source_verified_sha256'],
                               'Boot source proof changed')
                verified = native.strict_json(source)
                policy.require(verified.get('schema') == 1 and verified.get('kind') == 'verified-install'
                               and verified.get('task_id') == self.approval['source_task']
                               and verified.get('expected_sha256') == self.approval['expected_sha256'],
                               'Invalid fallback installation source')
                proof = native.strict_json(fallback)
                self._candidate_route_proof(verified, proof)
                baseline = policy.validate_boot_fallback(proof, verified,
                    (self.path / 'boot-check-8MB.bin').read_bytes())
                policy.require(baseline == dict(sha256=policy.BASELINE_SHA256,
                               bytes=policy.BASELINE_BYTES, elf_sha256=policy.BASELINE_ELF),
                               'Service fallback is restricted to the exact protected A')
                return True
        return kind != 'boot-only' and not (self.path / 'rom-entry-intent.json').exists()

    def _candidate_route_proof(self, verified, proof):
        evidence = policy.validate_execution_evidence(verified.get('execution_evidence'))
        policy.require(policy.validate_execution_evidence(proof.get('execution_evidence')) == evidence,
                       'Boot result evidence route differs from source installation')
        if evidence is not None:
            policy.require(self.approval.get('bootloader_evidence_mode') == policy.CANDIDATE_ROUTE
                           and self.approval.get('candidate_evidence_sha256') == evidence['candidate_evidence_sha256'],
                           'Service recovery approval must retain candidate evidence binding')
            if proof.get('kind') == 'boot-only-result':
                policy.require(proof.get('running', {}).get('measurement', {}).get(
                               'maintenance_health_acknowledged') is True,
                               'Successful candidate proof requires maintenance health acknowledgement')

    def _restore(self):
        try:
            observed = self.service.restore(self.record['original'])
            self.record.update(state='restored', observed=observed['observed'], error=None)
        except BaseException as exc:
            self.record.update(state='restore-failed', error=str(exc))
            self.save(self.file, self.record)
            raise
        self.save(self.file, self.record)

    def recover(self):
        if not self.file.exists():
            return dict(state='untouched')
        self.record = native.strict_json(self.file)
        policy.require(self.record.get('schema') == 1 and self.record.get('task_id') == self.path.name
                       and self.record.get('kind') == self.approval['kind']
                       and self.record.get('original') in ('active', 'inactive')
                       and self.record.get('state') in ('stop-intent', 'held-stopped', 'restored', 'restore-failed'),
                       'Service record binding mismatch')
        # A later task may now own the device. Replaying completed cleanup must
        # never change service state based on this task's old success proof.
        if self.record['state'] == 'restored':
            return self.record
        if self._proof_allows_restore():
            self._restore()
        else:
            policy.require(self.service.state() == 'inactive', 'ROM work requires service to remain stopped')
            self.record.update(state='held-stopped', error=None)
            self.save(self.file, self.record)
        return self.record

    def __exit__(self, *exc):
        with native.cleanup_signals():
            self.recover()


def mark_rom_entry(path):
    # Persist BEFORE the first possible ENTER_BOOT effect, including exceptions
    # where the host cannot know whether the device consumed the request.
    durable_new(Path(path) / 'rom-entry-intent.json', policy.encoded(dict(operation='PREPARE_UPDATE/ENTER_BOOT')))
