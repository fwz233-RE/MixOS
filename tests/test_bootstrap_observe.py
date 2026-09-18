"""Offline observer regressions: real journal/locks/backend shape, fake effects.

Windows and Linux use temporary state; no serial device or real systemd call is
allowed. Isolated subprocess tests load the production code with python -I.
"""
from contextlib import contextmanager, nullcontext
import copy
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.dont_write_bytecode = True
sys.path[:0] = [str(ROOT / 'tools'), str(ROOT / 'linux')]
import bootstrap_observe_on_pi as observe
import bootstrap_ota_on_pi as backend_cli
import mixos_esp_update as native
from _mixlib import ota_bootstrap as policy


def sha(data): return hashlib.sha256(data).hexdigest()
def enc(value): return json.dumps(value, sort_keys=True, separators=(',', ':')).encode()


class ObserveFixture:
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.state = Path(self.temp.name)
        self.old_state = observe.STATE
        observe.STATE = self.state
        self.readback = b'R' * 0x800000
        self.source_task = self.state / 'install-health-ack-20260917'
        self.source_task.mkdir()
        self.candidate = dict(bytes=910448, sha256='7' * 64, elf_sha256='1' * 64)
        self.baseline = dict(bytes=894560, sha256='7875d9a513acb95463b72e785ebd160c70d03f85e965c3a30a93d954bb4cff5f', elf_sha256='cfacb3fe25931e918b8f46d1f840da8d57ba39ef799bd0af4629939b9185761a')
        verified = dict(schema=1, task_id='install-health-ack-20260917', kind='verified-install',
                        candidate=self.candidate, baseline=self.baseline,
                        expected_sha256=sha(self.readback), reset_sent=False,
                        execution_evidence=dict(route=observe.ROUTE,
                            candidate_evidence_sha256=observe.SOURCE_BINDING))
        self.verified_raw = enc(verified)
        (self.source_task / 'verified.json').write_bytes(self.verified_raw)
        (self.source_task / 'flash-readback-8MB.bin').write_bytes(self.readback)
        (self.source_task / 'service.json').write_bytes(enc(
            dict(kind='install', original='active', state='held-stopped', error=None)))
        self.source_package = Path(self.temp.name) / 'package'
        self.source_package.mkdir()
        self.source_approval = dict(schema=1, task_id='install-health-ack-20260917', kind='install',
            approved=True, bootloader_evidence_mode=observe.ROUTE, reset_method='official-esptool-watchdog-reset',
            allow_clear_force_download=True, managed_service=True,
            code_sha256={}, runtime_package=str(self.source_package), artifacts={
                'candidate': {'sha256': self.candidate['sha256']}},
            candidate_evidence_sha256=observe.SOURCE_BINDING,
            runtime_risk_acceptance={}, qualification_task='qualify-reviewed-binary-20260917',
            qualification_sha256='a' * 64)
        self.manifest = {'schema': 'mixos-bootstrap-install-package/v1',
                         'task_id': 'install-health-ack-20260917',
                         'remote_package': str(self.source_package), 'files': {}}
        self.approval = dict(schema=observe.SCHEMA, approved=True, task_id='observe-health-ack-20260917',
            source_task='install-health-ack-20260917', source_package=str(self.source_package),
            source_approval_sha256='a' * 64, source_manifest_sha256='b' * 64,
            source_verified_sha256=sha(self.verified_raw), expected_sha256=sha(self.readback),
            observer_sha256=sha(b'd'*32),
            expected_usb_number='49', reset_allowed=False,
            host_flash_programming_allowed=False, health_ack_allowed=True)
        self.raw = enc(self.approval)
        self.loader = lambda package, approval_sha, manifest_sha: (
            self.source_package, self.manifest, self.source_approval,
            b'manifest', enc(self.source_approval))

    def tearDown(self):
        observe.STATE = self.old_state

    def facts(self):
        return observe.validate_approval(self.raw, approval_sha256=sha(self.raw),
            observer_bytes=b'd'*32, package_loader=self.loader)

    def candidate_observation(self):
        return dict(outcome=observe.OUTCOME_B, running=dict(
            slot='ota_1', address=0x610000, state='valid', pings=4, observed_seconds=15.2,
            elf_sha256=self.candidate['elf_sha256'], actual_file_verified=True,
            measurement=dict(actual_file_verified=True, maintenance_health_acknowledged=True,
                evidence_kind='running-measurement', heartbeat_count=4, heartbeat_seconds=15.2,
                binding=dict(sha256=self.candidate['sha256'], size=self.candidate['bytes'], target=1),
                stored_sha256=self.candidate['sha256'], elf_sha256=self.candidate['elf_sha256'],
                running_slot=1, boot_slot=1, boot_id=17, image_state=2, result='ok', error=0, flags=16)))

    def baseline_observation(self):
        return dict(outcome=observe.OUTCOME_A, running=dict(
            slot='ota_0', address=0x10000, state='valid', elf_sha256=self.baseline['elf_sha256'],
            identity_queries=1, pre_identity_heartbeat=dict(pings=4, observed_seconds=15.1),
            pings=4, observed_seconds=15.2))


class ObservePolicyTests(ObserveFixture, unittest.TestCase):
    def test_exact_approval_inherits_candidate_and_saved_readback(self):
        facts = self.facts()
        self.assertEqual(facts['candidate'], self.candidate)
        self.assertEqual(facts['baseline'], self.baseline)
        self.assertEqual(facts['verified']['expected_sha256'], sha(self.readback))

    def test_missing_or_true_reset_flags_are_refused(self):
        for key, value in (('reset_allowed', True), ('host_flash_programming_allowed', True),
                           ('health_ack_allowed', False), ('candidate_evidence_sha256', 'e'*64)):
            altered = dict(self.approval, **{key: value})
            with self.subTest(key=key), self.assertRaises(observe.Refused):
                observe.validate_approval(enc(altered), observer_bytes=b'd'*32,
                    package_loader=self.loader)

    def test_source_package_candidate_and_execution_binding_are_not_replaceable(self):
        altered = dict(self.source_approval, candidate_evidence_sha256='e'*64)
        loader = lambda *args: (self.source_package, self.manifest, altered,
                                b'manifest', enc(altered))
        with self.assertRaisesRegex(observe.Refused, 'Source approval'):
            observe.validate_approval(self.raw, observer_bytes=b'd'*32, package_loader=loader)

    def test_candidate_result_requires_real_health_ack_measurement(self):
        facts = self.facts()
        running = self.candidate_observation()['running']
        result, code = observe.classify_observation(dict(outcome=observe.OUTCOME_B, running=running), facts)
        self.assertEqual(code, 0)
        self.assertFalse(result['reset_sent'])
        self.assertFalse(result['host_flash_programming'])
        self.assertEqual(result['cause'], 'unexplained')
        for field in ('actual_file_verified', 'measurement'):
            bad = dict(running)
            if field == 'actual_file_verified': bad[field] = False
            else: bad[field] = dict(actual_file_verified=True, maintenance_health_acknowledged=False)
            with self.subTest(field=field), self.assertRaises(observe.Refused):
                observe.classify_observation(dict(outcome=observe.OUTCOME_B, running=bad), facts)

    def test_exact_a_result_is_failure_not_b_success_but_restores_policy(self):
        facts = self.facts()
        running = dict(slot='ota_0', address=0x10000, state='valid',
            elf_sha256=self.baseline['elf_sha256'], identity_queries=1,
            pre_identity_heartbeat=dict(pings=4, observed_seconds=15.1),
            pings=4, observed_seconds=15.2)
        result, code = observe.classify_observation(dict(outcome=observe.OUTCOME_A, running=running), facts)
        self.assertEqual(code, 2)
        self.assertTrue(observe.restore_allowed(result))
        self.assertEqual(result['cause'], 'unexplained')
        self.assertFalse(result['reset_sent'])

    def test_unknown_or_falsely_reset_receipts_never_restore_service(self):
        facts = self.facts()
        running = dict(slot='ota_0', address=0x10000, state='valid',
            elf_sha256=self.baseline['elf_sha256'], identity_queries=1,
            pre_identity_heartbeat=dict(pings=4, observed_seconds=15),
            pings=4, observed_seconds=15)
        result, _ = observe.classify_observation(dict(outcome=observe.OUTCOME_A, running=running), facts)
        for mutation in (dict(outcome='unknown'), dict(reset_sent=True),
                         dict(host_flash_programming=True), dict(cause='rollback-proven')):
            with self.subTest(mutation=mutation), self.assertRaises(observe.Refused):
                observe.restore_allowed(dict(result, **mutation))

    def test_observer_never_calls_device_or_system_for_pure_checks(self):
        with mock.patch('subprocess.run', side_effect=AssertionError('systemd')), \
             mock.patch('serial_transport.SerialTransport', side_effect=AssertionError('serial')):
            facts = self.facts()
            self.assertEqual(facts['source_task'], self.source_task)
            self.assertTrue(observe.restore_allowed(
                observe.classify_observation(self.candidate_observation(), facts)[0]))


    def test_real_backend_output_uses_nested_health_ack(self):
        expected = self.candidate_observation()['running']
        identity = dict(slot='ota_1', address=0x610000, state='valid',
                        elf_sha256=bytes.fromhex(self.candidate['elf_sha256']))
        link = SimpleNamespace(clock=lambda: 0.0, epoch=1, session=2,
                               handshake=lambda timeout: None, sleep=lambda delay: None)
        with mock.patch.object(backend_cli.ota_esp, 'identify', return_value=identity), \
             mock.patch.object(backend_cli.ota_v2, 'verify_actual', return_value=expected['measurement']):
            running = backend_cli.observe_app_health(link, self.candidate, 1)
        self.assertNotIn('maintenance_health_acknowledged', running)
        result, code = observe.classify_observation(dict(outcome=observe.OUTCOME_B, running=running), self.facts())
        self.assertEqual(code, 0)
        self.assertTrue(result['running']['measurement']['maintenance_health_acknowledged'])

    def test_missing_nested_measurement_proofs_never_use_defaults(self):
        for field in ('maintenance_health_acknowledged', 'actual_file_verified', 'evidence_kind'):
            observed = self.candidate_observation()
            observed['running']['maintenance_health_acknowledged'] = True
            del observed['running']['measurement'][field]
            with self.subTest(field=field), self.assertRaises(observe.Refused):
                observe.classify_observation(observed, self.facts())

    def test_changed_candidate_file_boot_health_and_identity_are_refused(self):
        mutations = [('binding', dict(sha256='f'*64, size=self.candidate['bytes'], target=1)),
                     ('stored_sha256', 'f'*64), ('elf_sha256', 'f'*64), ('boot_slot', 0),
                     ('running_slot', 0), ('boot_id', 0), ('boot_id', True),
                     ('image_state', 1), ('flags', 0), ('result', 'unknown'), ('error', 1)]
        for field, value in mutations:
            observed = self.candidate_observation()
            observed['running']['measurement'][field] = value
            with self.subTest(field=field, value=value), self.assertRaises(observe.Refused):
                observe.classify_observation(observed, self.facts())

    def test_nonfinite_and_short_heartbeat_windows_are_refused(self):
        for value in (float('nan'), float('inf'), 14.9, '20'):
            for observed in (self.baseline_observation(), self.candidate_observation()):
                observed['running']['observed_seconds'] = value
                with self.subTest(value=value, outcome=observed['outcome']), self.assertRaises(observe.Refused):
                    observe.classify_observation(observed, self.facts())

    def test_duplicate_and_nonfinite_json_are_refused(self):
        for raw in (b'{"schema":1,"schema":2}', b'{"pings":NaN}', b'{"seconds":Infinity}'):
            with self.subTest(raw=raw), self.assertRaises(observe.Refused):
                observe.strict_json(raw)

    def test_source_readback_and_verified_hashes_remain_mandatory(self):
        (self.source_task / 'flash-readback-8MB.bin').write_bytes(b'X' + self.readback[1:])
        with self.assertRaisesRegex(observe.Refused, 'readback mismatch'):
            self.facts()
        (self.source_task / 'flash-readback-8MB.bin').write_bytes(self.readback)
        (self.source_task / 'verified.json').write_bytes(self.verified_raw + b' ')
        with self.assertRaisesRegex(observe.Refused, 'receipt hash mismatch'):
            self.facts()


class ObserveExecutionTests(ObserveFixture, unittest.TestCase):
    """Exercise execute/recovery with real Journal, DeviceLock and SystemdService."""
    def setUp(self):
        super().setUp()
        self.approval['observer_sha256'] = sha(Path(observe.__file__).read_bytes())
        self.raw = enc(self.approval)
        self.approval_path = self.state / 'observer-approval.json'
        self.approval_path.write_bytes(self.raw)
        self.lock_root = self.state / 'locks'
        self.lock_root.mkdir()
        self.task = self.state / self.approval['task_id']
        self.events = []
        self.locked = False
        self.active = False
        self.recovery = False
        self.start_error = None
        self.observed = self.candidate_observation()
        self.context = dict(unit='mixos-bootstrap-' + self.approval['task_id'] + '.service',
                            invocation_id='a'*32, host_boot_id='12345678-1234-1234-1234-123456789abc')
        self.real_lock = native.DeviceLock
        self.real_backend = backend_cli.PiBackend
        test = self

        class TrackingLock:
            def __init__(self, directory, device):
                self.inner = test.real_lock(directory, device)
            def __enter__(self):
                self.inner.__enter__()
                test.assertFalse(test.locked)
                test.locked = True
                test.events.append('lock')
                return self
            def __exit__(self, *exc):
                test.events.append('unlock')
                test.locked = False
                return self.inner.__exit__(*exc)

        class FakeBackend:
            def __init__(self, approval, journal):
                test.assertTrue(test.locked)
                test.assertEqual(backend_cli.esp.ROOT, journal.path)
                test.assertEqual(json.loads((journal.path / 'approval.json').read_bytes()), approval)
            def observe_boot(self, candidate, baseline):
                test.assertTrue(test.locked)
                test.assertEqual(candidate, test.candidate)
                test.events.append('observe')
                if isinstance(test.observed, BaseException):
                    raise test.observed
                return test.observed
            def close(self):
                test.assertTrue(test.locked)
                test.events.append('close')

        original_validate = observe.validate_approval
        original_save = policy.Journal.save
        original_claim = observe._claim

        def validate(*args, **kwargs):
            return original_validate(*args, **kwargs, package_loader=self.loader)
        def save(journal, name, value):
            self.assertTrue(self.locked)
            self.events.append('save:' + name)
            return original_save(journal, name, value)
        def claim(path, value):
            self.assertTrue(self.locked)
            self.events.append('claim:' + Path(path).name)
            return original_claim(path, value)
        def execution_context(task_id, run, *, recovery=False):
            self.assertTrue(self.locked)
            self.assertEqual(task_id, self.approval['task_id'])
            self.assertEqual(recovery, self.recovery)
            return copy.deepcopy(self.context)
        def usb(location):
            self.assertTrue(self.locked)
            self.assertFalse(self.recovery, 'recovery must not read USB hardware')
            self.assertEqual(location, '5-1.2')
            return '49'

        patches = [mock.patch.object(observe, '_require_worker'),
                   mock.patch.object(observe, '_check_state'),
                   mock.patch.object(observe, '_protected_observer', side_effect=lambda path: path),
                   mock.patch.object(observe, 'validate_approval', side_effect=validate),
                   mock.patch.object(observe, '_load_source', return_value=(policy, backend_cli, native)),
                   mock.patch.object(observe, '_execution_context', side_effect=execution_context),
                   mock.patch.object(observe, '_app_usb_number', side_effect=usb),
                   mock.patch.object(native, 'DEFAULT_LOCKS', self.lock_root),
                   mock.patch.object(native, 'DeviceLock', TrackingLock),
                   mock.patch.object(backend_cli, 'PiBackend', FakeBackend),
                   mock.patch.object(backend_cli.esp, 'ROOT', self.source_package),
                   mock.patch.object(policy.Journal, 'save', save),
                   mock.patch.object(observe, '_claim', side_effect=claim),
                   mock.patch('serial_transport.SerialTransport', side_effect=AssertionError('serial device access')),
                   mock.patch('subprocess.run', side_effect=self.run_systemd)]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def run_systemd(self, command, **kwargs):
        self.assertTrue(self.locked, command)
        if command == ['/usr/bin/sudo', '-n', '/usr/bin/systemctl', 'start', 'mixosd.service']:
            self.assertTrue((self.task / 'service-restore-claim.json').exists())
            self.assertTrue((self.task / 'observe-result.json').exists())
            with self.assertRaises((native.JobError, OSError)):
                with self.real_lock(self.lock_root, native.DEVICE):
                    self.fail('competing maintenance acquired the device lock')
            self.events.append('start')
            if self.start_error:
                raise self.start_error
            self.active = True
            return subprocess.CompletedProcess(command, 0, stdout='', stderr='')
        self.assertEqual(command[:2], ['/usr/bin/systemctl', 'show'])
        unit = command[2]
        active = self.active if unit == 'mixosd.service' else False
        fields = dict(LoadState='loaded', ActiveState='active' if active else 'inactive',
                      SubState='running' if active else 'dead', MainPID='321' if active else '0',
                      ControlPID='0', Result='success', ExecMainStatus='0')
        return subprocess.CompletedProcess(command, 0, stdout='\n'.join(k+'='+v for k, v in fields.items()), stderr='')

    def execute(self):
        return observe.execute(self.approval_path, sha(self.raw), execute=True)

    def recover(self):
        self.recovery = True
        return observe.execute(self.approval_path, sha(self.raw), execute=True, recover=True)

    def save_pending_proof(self):
        with mock.patch.object(observe, '_finish_service', side_effect=InterruptedError('before restoration')):
            with self.assertRaises(InterruptedError):
                self.execute()
        self.assertTrue((self.task / 'observe-result.json').exists())
        self.assertNotIn('start', self.events)

    def test_execution_persists_approval_result_and_service_before_unlock(self):
        result = self.execute()
        self.assertEqual(result['outcome'], observe.OUTCOME_B)
        self.assertEqual(result['service']['state'], 'restored')
        self.assertEqual(json.loads((self.task / 'service.json').read_bytes()), result['service'])
        self.assertEqual(self.events.count('start'), 1)
        self.assertLess(self.events.index('close'), self.events.index('save:observe-result.json'))
        self.assertLess(self.events.index('save:observe-result.json'), self.events.index('start'))
        self.assertLess(self.events.index('claim:service.json'), self.events.index('unlock'))

    def test_baseline_is_exit_two_but_has_durable_service_restore(self):
        self.observed = self.baseline_observation()
        result = self.execute()
        self.assertEqual(result['result_code'], 2)
        self.assertEqual(result['service']['state'], 'restored')

    def test_completed_cleanup_is_idempotent_and_never_restarts_later_stopped_service(self):
        result = self.execute()
        self.active = False  # A later maintenance owner has stopped mixosd.
        before = self.events.count('start')
        for _ in range(2):
            recovered = self.recover()
            self.assertEqual(recovered['service'], result['service'])
            self.assertFalse(recovered['device_opened'])
        self.assertFalse(self.active)
        self.assertEqual(self.events.count('start'), before)

    def test_recovery_of_saved_proof_takes_lock_and_starts_once(self):
        self.save_pending_proof()
        result = self.recover()
        self.assertEqual(result['service']['state'], 'restored')
        self.assertEqual(self.events.count('observe'), 1)
        self.assertEqual(self.events.count('start'), 1)
        self.assertEqual(self.events[-1], 'unlock')

    def test_unknown_observation_is_consumed_but_never_restores(self):
        self.observed = dict(outcome='unknown')
        with self.assertRaises(observe.Refused):
            self.execute()
        result = self.recover()
        self.assertEqual(result['service']['state'], 'held-stopped')
        self.assertNotIn('start', self.events)
        self.assertFalse((self.task / 'observe-result.json').exists())
        self.assertIn('close', self.events)
        self.recovery = False
        with self.assertRaisesRegex(observe.Refused, 'already consumed'):
            self.execute()

    def test_backend_failure_always_closes_and_cleanup_grants_nothing(self):
        self.observed = RuntimeError('unknown health')
        with self.assertRaises(RuntimeError):
            self.execute()
        self.assertIn('close', self.events)
        self.assertEqual(self.recover()['service']['state'], 'held-stopped')
        self.assertNotIn('start', self.events)

    def test_real_backend_failure_audits_task_without_masking_original_error(self):
        original_error = OSError(5, 'synthetic CDC open failure')
        original_open = Path.open
        source_before = set(self.source_package.rglob('*'))

        def protected_open(path, *args, **kwargs):
            # Model the deployed immutable source even on Windows. The real
            # audit helper must never attempt to append anywhere inside it.
            if path.is_relative_to(self.source_package):
                raise PermissionError('immutable source package')
            if path.name == 'flash-audit.jsonl':
                self.assertTrue(self.locked)
                self.assertEqual(path.parent, self.task)
            return original_open(path, *args, **kwargs)

        with mock.patch.object(backend_cli, 'PiBackend', self.real_backend), \
             mock.patch.object(self.real_backend, 'prepare', side_effect=AssertionError('prepare forbidden')) as prepare, \
             mock.patch.object(self.real_backend, 'assert_stopped'), \
             mock.patch.object(self.real_backend, 'open_application', side_effect=original_error), \
             mock.patch.object(backend_cli.esp, 'wait_port', return_value=('/no-device', policy.APP_IDENTITY)), \
             mock.patch.object(backend_cli, 'operation_timeout', side_effect=lambda *args: nullcontext()), \
             mock.patch.object(backend_cli.time, 'sleep'), \
             mock.patch.object(Path, 'open', protected_open), \
             mock.patch('builtins.print'), \
             self.assertRaises(OSError) as caught:
            self.execute()
        self.assertIs(caught.exception, original_error)
        prepare.assert_not_called()
        records = [json.loads(line) for line in (self.task / 'flash-audit.jsonl').read_text().splitlines()]
        self.assertEqual(len(records), 3)
        for record in records:
            self.assertEqual(record['event'], 'bootstrap_app_observation_failed')
            self.assertEqual(record['stage'], 'cdc-open')
            self.assertEqual(record['error_type'], 'OSError')
            self.assertEqual(record['errno'], 5)
            self.assertEqual(record['reason'], str(original_error))
        failure = json.loads((self.task / 'observation-failed.json').read_bytes())
        self.assertEqual(failure['error'], str(original_error))
        self.assertEqual(set(self.source_package.rglob('*')), source_before)
        self.assertFalse((self.task / 'observe-result.json').exists())
        self.assertNotIn('start', self.events)
        self.assertEqual(self.recover()['service']['state'], 'held-stopped')

    def test_usb_change_refuses_result_and_service(self):
        with mock.patch.object(observe, '_app_usb_number', side_effect=['49', '50']):
            with self.assertRaisesRegex(observe.Refused, 'enumeration changed'):
                self.execute()
        self.assertEqual(self.recover()['service']['state'], 'held-stopped')
        self.assertNotIn('start', self.events)

    def test_failed_start_is_durably_spent_and_never_retried(self):
        self.start_error = subprocess.TimeoutExpired('systemctl', 15)
        with self.assertRaises(subprocess.TimeoutExpired):
            self.execute()
        self.assertTrue((self.task / 'service-restore-failed.json').exists())
        self.start_error = None
        self.active = True  # A timeout cannot prove which process started it.
        with self.assertRaisesRegex(observe.Refused, 'spent/unknown'):
            self.recover()
        self.assertEqual(self.events.count('start'), 1)

    def test_failure_to_persist_completion_never_retries_start(self):
        claim = observe._claim
        def fail_completion(path, value):
            if Path(path).name == 'service.json':
                raise OSError('completion fsync failed')
            return claim(path, value)
        with mock.patch.object(observe, '_claim', side_effect=fail_completion):
            with self.assertRaisesRegex(OSError, 'completion fsync'):
                self.execute()
        with self.assertRaisesRegex(observe.Refused, 'spent/unknown'):
            self.recover()
        self.assertEqual(self.events.count('start'), 1)

    def test_failed_result_save_cannot_authorize_recovery(self):
        save = policy.Journal.save
        def fail_save(journal, name, value):
            if name == 'observe-result.json':
                raise OSError('result fsync failed')
            return save(journal, name, value)
        with mock.patch.object(policy.Journal, 'save', fail_save):
            with self.assertRaisesRegex(OSError, 'result fsync'):
                self.execute()
        self.assertEqual(self.recover()['service']['state'], 'held-stopped')
        self.assertNotIn('start', self.events)

    def test_result_save_error_after_bytes_exist_still_blocks_cleanup(self):
        save = policy.Journal.save
        def fail_after_save(journal, name, value):
            saved = save(journal, name, value)
            if name == 'observe-result.json':
                raise OSError('result directory fsync failed')
            return saved
        with mock.patch.object(policy.Journal, 'save', fail_after_save):
            with self.assertRaisesRegex(OSError, 'directory fsync'):
                self.execute()
        self.assertTrue((self.task / 'observe-result.json').exists())
        with self.assertRaisesRegex(observe.Refused, 'not durably published'):
            self.recover()
        self.assertNotIn('start', self.events)

    def test_service_changed_before_restore_is_not_taken_over(self):
        self.save_pending_proof()
        self.active = True
        with self.assertRaisesRegex(observe.Refused, 'ownership changed'):
            self.recover()
        self.assertFalse((self.task / 'service-restore-claim.json').exists())
        self.assertNotIn('start', self.events)

    def test_source_and_task_claims_remain_one_use_even_after_success(self):
        self.execute()
        self.active = False
        with self.assertRaisesRegex(observe.Refused, 'already consumed'):
            self.execute()
        self.assertEqual(self.events.count('observe'), 1)
        self.assertEqual(self.events.count('start'), 1)

    def test_changed_source_service_hold_blocks_recovery(self):
        self.save_pending_proof()
        (self.source_task / 'service.json').write_bytes(enc(
            dict(kind='install', original='active', state='restored', error=None)))
        with self.assertRaisesRegex(observe.Refused, 'held stopped'):
            self.recover()
        self.assertNotIn('start', self.events)

    def test_spent_restore_intent_without_start_is_unknown(self):
        self.save_pending_proof()
        raw = (self.task / 'observe-result.json').read_bytes()
        binding = observe._binding(dict(approval=self.approval), sha(self.raw), self.context)
        (self.task / 'service-restore-claim.json').write_bytes(enc(dict(binding, observation_sha256=sha(raw))))
        with self.assertRaisesRegex(observe.Refused, 'spent/unknown'):
            self.recover()
        self.assertNotIn('start', self.events)

    def test_recovery_refuses_changed_invocation_or_host_boot(self):
        self.save_pending_proof()
        original = dict(self.context)
        for field, value in (('invocation_id', 'b'*32),
                             ('host_boot_id', 'ffffffff-1234-1234-1234-123456789abc')):
            self.context = dict(original, **{field: value})
            with self.subTest(field=field), self.assertRaisesRegex(observe.Refused, 'claim binding'):
                self.recover()
        self.assertNotIn('start', self.events)

    def test_recovery_refuses_tampered_result_or_claim(self):
        self.save_pending_proof()
        path = self.task / 'observe-result.json'
        original = json.loads(path.read_bytes())
        for field, value in (('task_id', 'observe-other-task'), ('source_task', 'other-source'),
                             ('approval_sha256', 'f'*64), ('usb_number_after', '50'),
                             ('source_verified_sha256', 'f'*64), ('result_code', 2)):
            path.write_bytes(enc(dict(original, **{field: value})))
            with self.subTest(field=field), self.assertRaises(observe.Refused):
                self.recover()
        self.assertNotIn('start', self.events)

    def test_foreign_source_claim_cannot_recover(self):
        self.save_pending_proof()
        path = self.state / ('observe-use-' + self.approval['source_task'] + '.json')
        claim = json.loads(path.read_bytes())
        claim['task_id'] = 'observe-another-task'
        path.write_bytes(enc(claim))
        with self.assertRaisesRegex(observe.Refused, 'claim binding'):
            self.recover()
        self.assertNotIn('start', self.events)

    def test_recovery_respects_real_competing_device_lock(self):
        self.save_pending_proof()
        with self.real_lock(self.lock_root, native.DEVICE):
            with self.assertRaises((native.JobError, OSError)):
                self.recover()
        self.assertNotIn('start', self.events)

    def test_preflight_rejects_hash_before_importing_source(self):
        with mock.patch.object(observe, '_load_source') as load:
            with self.assertRaisesRegex(observe.Refused, 'approval hash'):
                observe.execute(self.approval_path, '0'*64, preflight=True)
            load.assert_not_called()
        self.assertEqual(self.events, [])

    def test_default_validation_and_preflight_never_claim_or_observe(self):
        before = set(self.state.iterdir())
        audit_root_before = backend_cli.esp.ROOT
        result = observe.execute(self.approval_path, sha(self.raw))
        self.assertTrue(result['dry_run'])
        result = observe.execute(self.approval_path, sha(self.raw), preflight=True)
        self.assertTrue(result['imports_verified'])
        self.assertEqual(set(self.state.iterdir()), before)
        self.assertEqual(backend_cli.esp.ROOT, audit_root_before)
        self.assertEqual(self.events, [])


class ObserveFilesystemTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.package = self.root / 'package'
        self.package.mkdir()
        (self.package / 'tools').mkdir()
        self.file = self.package / 'tools' / 'worker.py'
        self.file.write_bytes(b'# synthetic immutable module\n')
        self.file.chmod(0o444)
        self.addCleanup(lambda: self.file.chmod(0o600))
        self.original_lstat = Path.lstat

    @contextmanager
    def protected_metadata(self, *, unsafe=None, mode=None, uid=None):
        # Ownership is synthetic; directory link counts, traversal and file
        # modes are real. No root escalation is needed on Windows or WSL.
        def info(path, *args, **kwargs):
            values = list(self.original_lstat(path, *args, **kwargs))
            values[4] = values[5] = 0
            if stat.S_ISDIR(values[0]):
                values[0] = stat.S_IFDIR | 0o755
            if path == unsafe:
                if mode is not None:
                    values[0] = stat.S_IFMT(values[0]) | mode
                if uid is not None:
                    values[4] = uid
            return os.stat_result(values)
        with mock.patch.object(Path, 'lstat', info):
            yield

    def test_real_directory_link_count_is_valid(self):
        if os.name == 'posix':
            self.assertGreaterEqual(self.package.stat().st_nlink, 3)
        self.assertEqual(observe._canonical(self.package, directory=True), self.package)
        with self.protected_metadata():
            self.assertEqual(observe._check_root_tree(self.package), self.package)

    def test_observer_file_requires_root_0444_and_protected_ancestors(self):
        with self.protected_metadata():
            self.assertEqual(observe._protected_observer(self.file), self.file)
        for unsafe, mode, uid in ((self.file, 0o644, None), (self.file, None, 1000),
                                   (self.root, 0o777, None), (self.root, None, 1000)):
            with self.subTest(unsafe=unsafe, mode=mode, uid=uid), self.protected_metadata(
                    unsafe=unsafe, mode=mode, uid=uid), self.assertRaises(observe.Refused):
                observe._protected_observer(self.file)

    def test_writable_or_nonroot_source_ancestors_are_refused(self):
        for mode, uid in ((0o777, None), (0o775, None), (None, 1000)):
            with self.subTest(mode=mode, uid=uid), self.protected_metadata(unsafe=self.root, mode=mode, uid=uid):
                with self.assertRaisesRegex(observe.Refused, 'ancestor'):
                    observe._check_root_tree(self.package)

    def test_writable_or_nonroot_nested_directory_is_refused(self):
        for mode, uid in ((0o777, None), (None, 1000)):
            with self.subTest(mode=mode, uid=uid), self.protected_metadata(
                    unsafe=self.package / 'tools', mode=mode, uid=uid):
                with self.assertRaises(observe.Refused):
                    observe._check_root_tree(self.package)

    def test_regular_files_still_reject_multiple_hardlinks(self):
        alias = self.package / 'alias.py'
        os.link(self.file, alias)
        self.addCleanup(lambda: alias.chmod(0o600))
        with self.assertRaises(observe.Refused):
            observe._canonical(self.file)
        with self.protected_metadata(), self.assertRaises(observe.Refused):
            observe._check_root_tree(self.package)

    @unittest.skipUnless(os.name == 'posix', 'Linux symlink/FIFO semantics')
    def test_symlink_and_special_source_children_are_refused(self):
        alias = self.package / 'alias'
        alias.symlink_to(self.package / 'tools', target_is_directory=True)
        with self.protected_metadata(), self.assertRaises(observe.Refused):
            observe._check_root_tree(self.package)
        alias.unlink()
        os.mkfifo(alias)
        with self.protected_metadata(), self.assertRaises(observe.Refused):
            observe._check_root_tree(self.package)

    def test_complete_manifest_checks_modes_hashes_and_file_set(self):
        approval_raw = enc(dict(task_id='install-health-ack-20260917', approved=True, kind='install'))
        approval = self.package / 'approval.json'
        approval.write_bytes(approval_raw)
        approval.chmod(0o444)
        self.addCleanup(lambda: approval.chmod(0o600))
        records = {path.relative_to(self.package).as_posix():
                   dict(bytes=path.stat().st_size, sha256=sha(path.read_bytes()), mode='0444')
                   for path in (self.file, approval)}
        manifest_raw = enc(dict(schema='mixos-bootstrap-install-package/v1',
            task_id='install-health-ack-20260917', remote_package=str(self.package), files=records))
        manifest = self.package / 'manifest.json'
        manifest.write_bytes(manifest_raw)
        manifest.chmod(0o444)
        self.addCleanup(lambda: manifest.chmod(0o600))
        with self.protected_metadata():
            observe.validate_package(self.package, sha(approval_raw), sha(manifest_raw))
            with self.assertRaisesRegex(observe.Refused, 'manifest hash'):
                observe.validate_package(self.package, sha(approval_raw), '0'*64)
        with self.protected_metadata(unsafe=manifest, mode=0o644), self.assertRaises(observe.Refused):
            observe.validate_package(self.package, sha(approval_raw), sha(manifest_raw))
        extra = self.package / 'extra.py'
        extra.write_bytes(b'bad')
        extra.chmod(0o444)
        self.addCleanup(lambda: extra.chmod(0o600))
        with self.protected_metadata(), self.assertRaisesRegex(observe.Refused, 'file set changed'):
            observe.validate_package(self.package, sha(approval_raw), sha(manifest_raw))


class ObserveInvocationTests(unittest.TestCase):
    def test_only_worker_or_current_execstoppost_owns_invocation(self):
        fields = dict(LoadState='loaded', ActiveState='active', SubState='running',
                      MainPID=str(os.getpid()), ControlPID='0', InvocationID='a'*32)
        with mock.patch.object(observe, '_fields', return_value=fields), \
             mock.patch.object(Path, 'read_text', return_value='12345678-1234-1234-1234-123456789abc\n'):
            context = observe._execution_context('observe-test-task', mock.Mock())
            self.assertEqual(context['invocation_id'], 'a'*32)
            with self.assertRaises(observe.Refused):
                observe._execution_context('observe-test-task', mock.Mock(), recovery=True)
            fields.update(ActiveState='deactivating', SubState='stop-post', MainPID='0', ControlPID=str(os.getpid()))
            self.assertEqual(observe._execution_context('observe-test-task', mock.Mock(), recovery=True), context)
            for altered in (dict(ControlPID='999999'), dict(MainPID='999999'),
                            dict(ActiveState='inactive', SubState='dead', ControlPID='0'),
                            dict(InvocationID=''), dict(InvocationID='0'*32)):
                with self.subTest(altered=altered), mock.patch.object(observe, '_fields', return_value=dict(fields, **altered)):
                    with self.assertRaises(observe.Refused):
                        observe._execution_context('observe-test-task', mock.Mock(), recovery=True)

    def test_worker_requires_linux_isolation_and_nonroot(self):
        for platform, isolated, uid in (('win32', True, 1000), ('linux', False, 1000), ('linux', True, 0)):
            with self.subTest(platform=platform, isolated=isolated, uid=uid), \
                 mock.patch.object(observe.sys, 'platform', platform), \
                 mock.patch.object(observe.sys, 'flags', SimpleNamespace(isolated=isolated)), \
                 mock.patch.object(observe.os, 'geteuid', return_value=uid, create=True), \
                 self.assertRaises(observe.Refused):
                observe._require_worker()

    def test_cli_recovery_needs_execute_and_preflight_is_exclusive(self):
        with self.assertRaises(observe.Refused):
            observe.execute(Path('/unused'), 'a'*64, recover=True)
        with self.assertRaises(observe.Refused):
            observe.execute(Path('/unused'), 'a'*64, execute=True, preflight=True)


class ObserveIsolatedImportTests(unittest.TestCase):
    def run_child(self, tail):
        script = '''
import importlib.util, pathlib, sys
from unittest import mock
root = pathlib.Path(sys.argv[1]).resolve()
spec = importlib.util.spec_from_file_location('observer', root/'tools/bootstrap_observe_on_pi.py')
observer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(observer)
assert sys.flags.isolated
assert '_mixlib' not in sys.modules
records = {p.relative_to(root).as_posix(): {} for folder in ('tools', 'linux')
           for p in (root/folder).rglob('*.py')}
facts = dict(package=root, manifest=dict(files=records))
''' + tail
        with tempfile.TemporaryDirectory() as directory:
            return subprocess.run([sys.executable, '-I', '-B', '-c', script, str(ROOT)],
                                  cwd=directory, capture_output=True, text=True, timeout=30)

    def test_clean_isolated_import_loads_real_backend_and_native_without_effects(self):
        result = self.run_child('''
with mock.patch('subprocess.run', side_effect=AssertionError('system effect')), \
     mock.patch('os.open', side_effect=AssertionError('device/lock effect')):
    policy, cli, native = observer._load_source(facts)
assert pathlib.Path(policy.__file__).resolve() == root/'tools/_mixlib/ota_bootstrap.py'
assert pathlib.Path(cli.__file__).resolve() == root/'tools/bootstrap_ota_on_pi.py'
assert pathlib.Path(native.__file__).resolve() == root/'tools/mixos_esp_update.py'
assert sys.dont_write_bytecode
print('isolated source imports OK')
''')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('isolated source imports OK', result.stdout)

    def test_isolated_import_rejects_preloaded_unverified_project_module(self):
        result = self.run_child('''
import types
sys.modules['_mixlib'] = types.SimpleNamespace(__file__='/unverified/_mixlib/__init__.py')
try:
    observer._load_source(facts)
except observer.Refused as exc:
    assert 'outside the verified package' in str(exc)
else:
    raise AssertionError('preloaded unverified module accepted')
''')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == '__main__':
    unittest.main()
