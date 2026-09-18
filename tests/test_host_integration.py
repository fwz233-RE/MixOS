"""Bounded host integration regressions. Fake USB/clock/service only.

Never execute generated provisioning commands, systemd, SSH, or real serial.
"""
import hashlib
import io
import json
import os
from pathlib import Path
import shlex
import signal
import subprocess
import sys
import tempfile
import types
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'tools'), str(ROOT / 'linux')]
import bootstrap_ota_on_pi as bootstrap
import install_ota_policy as installer
import mixos_esp_update as native
import ota_esp
import ota_v2 as v2
import test_ota_supervisor as supervisor
from test_mixos_esp_update import make_image, manifest
from test_ota_v2 import Clock, fixture


class MeasurementTests(unittest.TestCase):
    def open_empty(self, **options):
        updater, device, data, clock = fixture(**options)
        device.running = device.boot = 1
        device.flags = 0  # no durable transaction at all
        client = updater.open()
        binding = v2.Binding(bytes(range(16)), hashlib.sha256(data).digest(), len(data), 1)
        return client, device, data, clock, binding

    def test_idle_no_journal_actual_measurement_never_queries_synthetic_transaction(self):
        client, device, data, clock, binding = self.open_empty()
        result = v2.verify_actual(client, binding, bytes(range(32)), clock() + 3,
                                  heartbeat_seconds=0.25, no_journal=True)
        self.assertTrue(result['actual_file_verified'])
        self.assertEqual(result['phase'], 'idle')
        self.assertFalse(result['flags'] & v2.JOURNAL)
        self.assertEqual(result['evidence_kind'], 'running-measurement')
        self.assertEqual(device.binding, v2.EMPTY)
        self.assertNotIn(v2.Op.QUERY, device.ops)
        self.assertNotIn(v2.Op.BEGIN, device.ops)
        self.assertEqual(result['measurement']['binding'], binding.record())

    def test_busy_unrelated_journal_is_not_measurement(self):
        client, device, _, clock, binding = self.open_empty()
        device.verify_never_accepted = True
        with self.assertRaises(v2.OutcomeError):
            v2.verify_actual(client, binding, bytes(range(32)), clock() + 1,
                             heartbeat_seconds=0.25, no_journal=True)
        self.assertEqual(device.binding, v2.EMPTY)

    def test_no_journal_wrong_sha_elf_slot_or_short_file_refuses(self):
        for changed in ('sha', 'elf', 'slot', 'size'):
            with self.subTest(changed=changed):
                client, device, data, clock, binding = self.open_empty()
                expected = bytes(range(32))
                if changed == 'sha':
                    binding = v2.Binding(binding.transaction, bytes(32), len(data), 1)
                elif changed == 'elf':
                    expected = bytes(32)
                elif changed == 'slot':
                    device.boot = 0
                else:
                    binding = v2.Binding(binding.transaction, binding.sha256, len(data)-32, 1)
                with self.assertRaises(v2.OutcomeError):
                    v2.verify_actual(client, binding, expected, clock() + 3,
                                     heartbeat_seconds=0.25, no_journal=True)

    def test_first_b_to_a_from_empty_journal_requires_known_b_package(self):
        updater, device, data, _ = fixture()
        old = make_image(4096)
        device.data = old
        device.running = device.boot = 1
        value = manifest(data)
        value['protected_baseline'] = dict(slot='ota_0', image_sha256=native.BASELINE_SHA,
                                           image_bytes=native.BASELINE_SIZE, elf_sha256=native.BASELINE_ELF)
        value['verified_replacement'] = dict(slot='ota_1', image_sha256=hashlib.sha256(old).hexdigest(),
                                             image_bytes=len(old), elf_sha256=bytes(range(32)).hex())
        events = []
        result = updater.run(data, value, events.append, allow_replace_baseline=True)
        self.assertEqual(result['state'], 'confirmed')
        self.assertEqual(device.running, 0)
        self.assertEqual(bytes(device.received), data)
        self.assertEqual(device.ops.count(v2.Op.BEGIN), 1)
        self.assertLess(device.ops.index(v2.Op.VERIFY_RUNNING), device.ops.index(v2.Op.RELEASE_BASELINE))
        proof = next(x['evidence'] for x in events if x['event'] == 'replacement-measured')
        self.assertEqual(proof['phase'], 'idle')
        self.assertEqual(proof['evidence_kind'], 'running-measurement')

    def test_unknown_fallback_never_releases_baseline(self):
        for replacement in (None, dict(slot='ota_1', image_sha256='a'*64,
                                       image_bytes=4096, elf_sha256=bytes(range(32)).hex())):
            updater, device, data, _ = fixture()
            device.running = device.boot = 1
            value = manifest(data)
            value['protected_baseline'] = dict(slot='ota_0', image_sha256=native.BASELINE_SHA,
                                               image_bytes=native.BASELINE_SIZE, elf_sha256=native.BASELINE_ELF)
            if replacement:
                value['verified_replacement'] = replacement
            with self.assertRaises(v2.OutcomeError):
                updater.run(data, value, lambda event: None, allow_replace_baseline=True)
            self.assertNotIn(v2.Op.RELEASE_BASELINE, device.ops)
            self.assertNotIn(v2.Op.BEGIN, device.ops)


class BootstrapObservationTests(unittest.TestCase):
    def observe(self, pending=False, missing_hash=False):
        updater, device, data, clock = fixture(stay_pending=pending)
        device.running = device.boot = 1
        device.state = 1
        device.valid_at = clock() + 20
        device.flags = 0
        device.verify_never_accepted = missing_hash
        link = ota_esp.Link(device, clock=clock, sleep=clock.sleep, random_sessions=True)
        expected = dict(bytes=len(data), sha256=hashlib.sha256(data).hexdigest(),
                        elf_sha256=bytes(range(32)).hex())
        return link, device, expected, clock

    def test_fresh_identity_after_twenty_seconds_pending_then_valid_and_hash(self):
        link, device, expected, clock = self.observe()
        result = bootstrap.observe_app_health(link, expected, 1)
        self.assertEqual(result['state'], 'valid')
        self.assertGreaterEqual(result['observed_seconds'], 15)
        self.assertGreater(clock.now, 35)
        self.assertLess(clock.now, 121)
        self.assertTrue(result['actual_file_verified'])
        self.assertTrue(result['measurement']['actual_file_verified'])
        self.assertEqual(device.binding, v2.EMPTY)
        identity_frames = [f for f in device.writes if f.type == ota_esp.T.OTA_IDENTIFY]
        self.assertEqual(len(identity_frames), 2)

    def test_pending_receiver_gets_measurement_before_confirmation(self):
        link, device, expected, clock = self.observe(pending=True)
        original = device.handle
        first_measurement = []
        def handle(frame):
            if (frame.type == ota_esp.T.OTA_REQUEST and frame.payload[1] == v2.Op.VERIFY_RUNNING
                    and not first_measurement):
                first_measurement.append((clock(), device.state))
                device.stay_pending = False
                device.valid_at = clock() + 20
            return original(frame)
        device.handle = handle
        result = bootstrap.observe_app_health(link, expected, 1)
        self.assertEqual(first_measurement[0][1], 1)
        self.assertLess(first_measurement[0][0], 2)
        self.assertEqual(result['state'], 'valid')
        self.assertGreaterEqual(result['observed_seconds'], 15)

    def test_pending_forever_is_finite_and_cannot_be_boot_success(self):
        link, _, expected, clock = self.observe(pending=True)
        with self.assertRaises(v2.OutcomeError) as error:
            bootstrap.observe_app_health(link, expected, 1)
        self.assertEqual(error.exception.state, 'pending')
        self.assertLess(clock.now, 122)

    def test_identity_and_valid_heartbeats_without_actual_hash_are_insufficient(self):
        link, _, expected, clock = self.observe(missing_hash=True)
        with self.assertRaises(v2.OutcomeError):
            bootstrap.observe_app_health(link, expected, 1)
        self.assertLess(clock.now, 122)

    def test_epoch_change_cannot_mix_identity_samples(self):
        link, _, expected, _ = self.observe()
        def changed(link, timeout):
            link.epoch += 1
            return dict(slot='ota_1', address=bootstrap.policy.APP1, state='valid', elf_sha256=bytes(range(32)))
        with mock.patch.object(bootstrap.ota_esp, 'identify', side_effect=changed), \
                self.assertRaisesRegex(bootstrap.policy.Refused, 'epoch'):
            bootstrap.observe_app_health(link, expected, 1)


class BaselineObservationTests(unittest.TestCase):
    def fixture(self):
        updater, device, data, clock = fixture()
        device.running = device.boot = 0
        device.state = 2
        device.valid_at = 0
        link = ota_esp.Link(device, clock=clock, sleep=clock.sleep, random_sessions=True)
        expected = dict(bytes=len(data), sha256=hashlib.sha256(data).hexdigest(), elf_sha256=bytes(range(32)).hex())
        return link, device, expected, clock

    def test_single_identity_between_two_healthy_intervals(self):
        link, device, expected, clock = self.fixture()
        original = device.handle
        at = []
        def handle(frame):
            if frame.type == ota_esp.T.OTA_IDENTIFY:
                at.append(clock())
            return original(frame)
        device.handle = handle
        result = bootstrap.observe_app_health(link, expected, 0)
        self.assertEqual(result['identity_queries'], 1)
        self.assertEqual(len(at), 1)
        self.assertGreaterEqual(at[0], 20)
        self.assertGreaterEqual(clock(), 40)
        self.assertGreaterEqual(result['observed_seconds'], 15)
        self.assertGreaterEqual(result['pre_identity_heartbeat']['observed_seconds'], 15)

    def test_no_initial_heartbeat_never_queries_identity(self):
        link, device, expected, _ = self.fixture()
        device.no_heartbeats = True
        with self.assertRaisesRegex(bootstrap.policy.Refused, 'heartbeats'):
            bootstrap.observe_app_health(link, expected, 0)
        self.assertFalse(any(f.type == ota_esp.T.OTA_IDENTIFY for f in device.writes))

    def test_missing_identity_is_not_retried(self):
        link, device, expected, clock = self.fixture()
        original = device.handle
        queries = []
        def handle(frame):
            if frame.type == ota_esp.T.OTA_IDENTIFY:
                queries.append(frame)
                return
            return original(frame)
        device.handle = handle
        with self.assertRaises(ota_esp.Timeout):
            bootstrap.observe_app_health(link, expected, 0)
        self.assertEqual(len(queries), 1)
        self.assertLess(clock(), 30)

    def test_identity_then_heartbeat_failure_refuses(self):
        link, device, expected, _ = self.fixture()
        original = device.handle
        def handle(frame):
            original(frame)
            if frame.type == ota_esp.T.OTA_IDENTIFY:
                device.no_heartbeats = True
        device.handle = handle
        with self.assertRaisesRegex(bootstrap.policy.Refused, 'heartbeats'):
            bootstrap.observe_app_health(link, expected, 0)

    def test_wrong_elf_or_pending_baseline_refuses(self):
        for changed in ('elf', 'state'):
            link, device, expected, _ = self.fixture()
            if changed == 'elf': expected['elf_sha256'] = 'a' * 64
            else: device.state = 1
            with self.assertRaisesRegex(bootstrap.policy.Refused, 'identity and VALID'):
                bootstrap.observe_app_health(link, expected, 0)

    def test_short_total_budget_does_not_start_identity_query(self):
        link, device, expected, clock = self.fixture()
        started = clock()
        with self.assertRaisesRegex(bootstrap.policy.Refused, 'budget'):
            bootstrap.observe_app_health(link, expected, 0, timeout=35)
        self.assertLessEqual(clock() - started, 21)
        self.assertFalse(any(f.type == ota_esp.T.OTA_IDENTIFY for f in device.writes))


class OwnershipTests(unittest.TestCase):
    # Reuse fixture construction, not an inherited duplicate test suite.
    setUp = supervisor.SupervisorTests.setUp
    factory = supervisor.SupervisorTests.factory
    run_job = supervisor.SupervisorTests.run_job

    def test_failed_lock_preserves_active_journal_byte_for_byte(self):
        result = native.strict_json(self.job / 'result.json')
        result['state'] = 'running'
        native.atomic_json(self.job / 'result.json', result)
        before = (self.job / 'result.json').read_bytes()
        with native.DeviceLock(self.locks, native.DEVICE):
            receipt = self.run_job()
        self.assertTrue(receipt['command_receipt'])
        self.assertEqual((self.job / 'result.json').read_bytes(), before)
        self.assertEqual(self.updates, [])

    def test_result_is_reread_under_lock_and_success_guard_never_restops(self):
        service = supervisor.FakeService()
        success = dict(self.result, state='complete', durable=True,
                       firmware={'state': 'confirmed'}, service={'state': 'restored'}, error=None)
        real_lock = native.DeviceLock
        class FinishingLock:
            def __init__(inner, *args):
                inner.lock = real_lock(*args)
            def __enter__(inner):
                inner.lock.__enter__()
                native.atomic_json(self.job / 'result.json', success)
                return inner
            def __exit__(inner, *args):
                inner.lock.__exit__(*args)
        result = self.run_job(service, lock_factory=FinishingLock)
        self.assertEqual(native.result_exit_code(result), 0)
        self.assertEqual(service.events, [])
        self.assertEqual(self.updates, [])
        self.assertEqual(native.strict_json(self.job / 'result.json'), success)

    def test_changed_package_runtime_or_job_identity_refuses_before_service(self):
        for name in ('package/app.bin', 'package/manifest.json', 'runtime/tools/ota_v2.py'):
            path = self.job / name
            old = path.read_bytes()
            try:
                path.write_bytes(old + b'changed')
                service = supervisor.FakeService()
                with self.assertRaisesRegex(native.JobError, 'digest mismatch'):
                    self.run_job(service)
                self.assertEqual(service.events, [])
            finally:
                path.write_bytes(old)
        result = native.strict_json(self.job / 'result.json')
        result['device'] += '-changed'
        native.atomic_json(self.job / 'result.json', result)
        with self.assertRaisesRegex(native.JobError, 'identity/device'):
            self.run_job()

    def test_close_failure_does_not_skip_service_restore_or_final_journal(self):
        updater = self.factory()
        updater.close = lambda self: (_ for _ in ()).throw(OSError('close failed'))
        service = supervisor.FakeService()
        result = self.run_job(service, updater_factory=updater)
        self.assertEqual(result['firmware']['state'], 'confirmed')
        self.assertEqual(result['service']['state'], 'restored')
        self.assertEqual(result['error']['code'], 'transport-close')
        self.assertEqual(native.read_status(self.state, result['job'])['exit_code'], 1)

    def test_recovery_retries_restore_failure_without_updating_firmware(self):
        result = self.run_job(supervisor.FakeService(restore_error=True))
        service = supervisor.FakeService(state='inactive')
        result = native.recover_worker(self.job, service=service)
        self.assertEqual(service.events, ['restore:active'])
        self.assertEqual(result['firmware']['state'], 'confirmed')
        self.assertEqual(native.result_exit_code(result), 0)

    def test_successful_resume_is_observation_without_systemd_or_service_actions(self):
        result = self.run_job()
        before = (self.job / 'result.json').read_bytes()
        run = mock.Mock(side_effect=AssertionError('systemd forbidden'))
        launch = mock.Mock(side_effect=AssertionError('submission forbidden'))
        observed = native.prepare_resume(self.job, run=run)
        self.assertEqual(native.submit_job(self.job, observed, launch=launch), result)
        run.assert_not_called()
        launch.assert_not_called()
        self.assertEqual((self.job / 'result.json').read_bytes(), before)

    def test_submission_timeout_does_not_overwrite_possibly_accepted_worker(self):
        success = dict(self.result, state='complete', durable=True,
                       firmware={'state': 'confirmed'}, service={'state': 'restored'}, error=None)
        def accepted_then_timeout(path):
            native.atomic_json(path / 'result.json', success)
            raise subprocess.TimeoutExpired('mocked-systemd-run', 30)
        receipt = native.submit_job(self.job, self.result, launch=accepted_then_timeout)
        self.assertTrue(receipt['command_receipt'])
        self.assertEqual(receipt['state'], 'unknown')
        self.assertEqual(native.strict_json(self.job / 'result.json'), success)
        self.assertEqual(native.read_status(self.state, self.result['job'])['exit_code'], 0)

    def test_wait_timeout_is_finite_receipt_and_never_changes_queued_job(self):
        before = (self.job / 'result.json').read_bytes()
        clock = Clock()
        result = native.wait_status(self.state, self.result['job'], timeout=1, clock=clock, sleep=clock.sleep)
        self.assertLess(clock.now, 2.1)
        self.assertTrue(result['command_receipt'])
        self.assertEqual(result['observed_state'], 'queued')
        self.assertEqual(native.result_exit_code(result), 1)
        self.assertEqual((self.job / 'result.json').read_bytes(), before)

    def test_success_guards_recheck_durability_without_restarting_service(self):
        self.run_job()
        service = supervisor.FakeService()
        run = mock.Mock(return_value=subprocess.CompletedProcess([], 0,
            'LoadState=not-found\nActiveState=inactive\n', ''))
        with mock.patch.object(native.os, 'fsync', side_effect=OSError('disk unavailable')):
            result = self.run_job(service)
            resumed = native.prepare_resume(self.job, run=run)
        self.assertEqual(service.events, [])
        self.assertEqual(native.result_exit_code(result), 1)
        self.assertEqual(native.result_exit_code(resumed), 1)
        self.assertFalse(result['durable'])
        self.assertFalse(resumed['durable'])

    def test_resume_submission_distinguishes_prior_failure_from_new_worker(self):
        prior = self.run_job(updater_factory=self.factory(v2.OutcomeError('old unknown result')))
        before = (self.job / 'result.json').read_bytes()
        launch = mock.Mock()
        receipt = native.submit_job(self.job, prior, launch=launch)
        launch.assert_called_once_with(self.job)
        self.assertEqual(receipt['error']['code'], 'resume-submitted')
        self.assertTrue(receipt['command_receipt'])
        self.assertEqual((self.job / 'result.json').read_bytes(), before)

    def test_worker_sigterm_restores_and_finishes_journal(self):
        updater = self.factory()
        def interrupted(*args, **kwargs):
            signal.getsignal(signal.SIGTERM)(signal.SIGTERM, None)
        updater.run = interrupted
        service = supervisor.FakeService()
        result = native.run_worker(self.job, service=service, updater_factory=updater,
                                   connect=lambda: self.fail('device forbidden'))
        self.assertEqual(result['service']['state'], 'restored')
        self.assertEqual(result['error']['code'], 'InterruptedError')
        self.assertEqual(native.read_status(self.state, result['job'])['state'], 'complete')

    def test_overdue_status_is_unknown_without_journal_mutation(self):
        result = native.strict_json(self.job / 'result.json')
        result['created_at'] = 1
        native.atomic_json(self.job / 'result.json', result)
        before = (self.job / 'result.json').read_bytes()
        receipt = native.read_status(self.state, result['job'])
        self.assertEqual(receipt['error']['code'], 'job-overdue')
        self.assertTrue(receipt['command_receipt'])
        self.assertEqual((self.job / 'result.json').read_bytes(), before)

    @unittest.skipUnless(os.name == 'posix', 'requires real POSIX flock on temporary files')
    def test_legacy_and_native_locks_exclude_both_directions(self):
        from _mixlib.guards import device_lock, LockBusy
        legacy = self.locks / 'legacy-flash.lock'
        with device_lock(legacy):
            with self.assertRaises(native.JobError):
                with native.DeviceLock(self.locks, native.DEVICE):
                    self.fail('legacy owner bypassed')
        with native.DeviceLock(self.locks, native.DEVICE):
            with self.assertRaises(LockBusy):
                with device_lock(legacy):
                    self.fail('native owner bypassed')

    def test_inspect_sigterm_and_close_errors_restore_with_signals_masked(self):
        for interrupted in (False, True):
            service = supervisor.FakeService()
            link = types.SimpleNamespace(transport=mock.Mock())
            link.transport.close.side_effect = OSError('close failed')
            original_restore = service.restore
            def restore(original):
                self.assertEqual(signal.getsignal(signal.SIGTERM), signal.SIG_IGN)
                return original_restore(original)
            service.restore = restore
            def identify(*args):
                if interrupted:
                    signal.getsignal(signal.SIGTERM)(signal.SIGTERM, None)
                return dict(elf_sha256=bytes(range(32)))
            old = signal.getsignal(signal.SIGTERM)
            with mock.patch.object(native.ota_esp, 'identify', side_effect=identify):
                result = native.inspect_device(native.DEVICE, legacy=True, service=service,
                                               connect=lambda: link, lock_root=self.locks)
            self.assertEqual(result['service']['state'], 'restored')
            self.assertEqual(result['exit_code'], 1)
            self.assertEqual(signal.getsignal(signal.SIGTERM), old)


class ConfigurationAndProvisionTests(unittest.TestCase):
    def test_every_required_config_is_strict_boolean_and_required(self):
        with tempfile.TemporaryDirectory() as directory:
            package = Path(directory)
            data = make_image()
            (package / 'app.bin').write_bytes(data)
            for key, expected in native.REQUIRED_CONFIG.items():
                for value in (None, not expected, int(expected)):
                    with self.subTest(key=key, value=value):
                        doc = manifest(data)
                        if value is None:
                            del doc['effective_config'][key]
                        else:
                            doc['effective_config'][key] = value
                        (package / 'manifest.json').write_text(json.dumps(doc))
                        with self.assertRaisesRegex(native.JobError, 'safety configuration'):
                            native.load_release(package)

    def test_nan_infinite_and_excessive_deadlines_refused_offline(self):
        for value in (float('nan'), float('inf'), -1, 0, 1201):
            with self.assertRaises(native.JobError):
                native.finite_seconds(value, 'test')
        with mock.patch.object(native, 'read_status') as status, mock.patch('sys.stdout', io.StringIO()):
            self.assertEqual(native.main(['status', '--job', '1'*32, '--wait', '--wait-timeout', 'nan']), 1)
            status.assert_not_called()

    def test_provisioning_is_print_only_valid_shell_and_narrow_permissions(self):
        with mock.patch('subprocess.run', side_effect=AssertionError('execution forbidden')):
            lines = installer.commands('mixos-test')
        text = '\n'.join(lines)
        for line in lines:
            if not line.startswith('#'):
                shlex.split(line)
        self.assertIn(native.device_key(native.DEVICE) + '.lock', text)
        self.assertIn('0660 root dialout', text)
        self.assertIn('-o mixos-test -g dialout -m 0700', text)
        self.assertIn('loginctl enable-linger mixos-test', text)
        policy = next(line for line in lines if 'NOPASSWD' in line)
        self.assertIn('/usr/bin/systemctl stop mixosd.service, /usr/bin/systemctl start mixosd.service', policy)
        self.assertNotIn('NOPASSWD: ALL', policy)
        self.assertIn('NOT installed', text)
        for user in ('root', 'x ALL=(ALL)', 'a;id', 'a\nb'):
            with self.assertRaises(ValueError):
                installer.commands(user)

    def test_unknown_control_process_is_not_stopped_service(self):
        for pid in ('', '42'):
            answer = subprocess.CompletedProcess([], 0,
                'LoadState=loaded\nActiveState=inactive\nSubState=dead\nMainPID=0\nControlPID=' + pid + '\n', '')
            with self.assertRaises(native.JobError):
                native.SystemdService(run=mock.Mock(return_value=answer)).state()


if __name__ == '__main__':
    unittest.main()
