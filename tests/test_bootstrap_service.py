"""Offline service ownership/crash cleanup; no real systemd or device calls."""
import json
import os
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
from _mixlib import bootstrap_service as s
from _mixlib import ota_bootstrap as p
import bootstrap_ota_on_pi as cli
import mixos_esp_update as native


class FakeService:
    def __init__(self, state='active'):
        self.current = state
        self.events = []

    def state(self):
        self.events.append('state')
        return self.current

    def stop(self):
        self.events.append('stop')
        self.current = 'inactive'

    def restore(self, original):
        self.events.append('restore:' + original)
        self.current = original
        return {'observed': original}


class UnitTests(unittest.TestCase):
    def verify(self, recovery=False, rc=0, **changes):
        fields = dict(LoadState='loaded', ActiveState='active', SubState='running',
                      MainPID=str(os.getpid()), ControlPID='0')
        fields.update(changes)
        run = mock.Mock(return_value=types.SimpleNamespace(returncode=rc,
                        stdout='\n'.join(k + '=' + v for k, v in fields.items())))
        s.verify_unit({'task_id': 'qualified-test-task'}, run=run, recovery=recovery)
        self.assertEqual(run.call_args.kwargs['timeout'], 10)

    def test_main_process(self):
        self.verify()
        self.verify(ActiveState='activating')

    def test_exec_stop_post_owns_only_control_pid(self):
        self.verify(recovery=True, ActiveState='deactivating', SubState='stop-post',
                    MainPID='0', ControlPID=str(os.getpid()))

    def test_manual_cleanup_only_after_fully_stopped(self):
        for state, sub in (('inactive', 'dead'), ('failed', 'failed')):
            self.verify(recovery=True, ActiveState=state, SubState=sub, MainPID='0')

    def test_invalid_owner_refused(self):
        for kwargs in ({'MainPID': '0'}, {'MainPID': '99999999'}, {'ControlPID': '3'},
                       {'LoadState': 'not-found'}, {'ActiveState': 'inactive'}, {'rc': 1}):
            with self.subTest(kwargs=kwargs), self.assertRaises(p.Refused):
                self.verify(**kwargs)
        for kwargs in ({}, {'ActiveState': 'deactivating', 'SubState': 'stop-post',
                            'MainPID': '0', 'ControlPID': '99999999'},
                       {'ActiveState': 'deactivating', 'SubState': 'stop-sigterm',
                        'MainPID': '0', 'ControlPID': str(os.getpid())},
                       {'ActiveState': 'inactive', 'SubState': 'dead', 'MainPID': '0', 'ControlPID': '3'}):
            with self.subTest(kwargs=kwargs), self.assertRaises(p.Refused):
                self.verify(recovery=True, **kwargs)


class OwnershipTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.registry = Path(temp.name)
        self.approval = dict(task_id='qualify-service-test', kind='qualify')
        self.path = self.registry / self.approval['task_id']
        self.path.mkdir()
        self.service = FakeService()

    def owner(self):
        return s.ServiceOwner(self.registry, self.approval, self.service)

    def record(self):
        return native.strict_json(self.path / 'service.json')

    def qualification(self):
        running = dict(state='valid', slot='ota_0', address=p.APP0, elf_sha256=p.BASELINE_ELF,
                       pings=4, observed_seconds=15.1)
        return dict(schema=1, task_id=self.path.name, kind='qualification', flash_programming=False,
                    exit=p.RESET_METHOD, app_identity=p.APP_IDENTITY, rom_identity=p.ROM_IDENTITY,
                    before_sha256='a' * 64, after_sha256='a' * 64, running_after=running)

    def test_failure_before_rom_restores_original_service(self):
        with self.assertRaisesRegex(RuntimeError, 'preflight'):
            with self.owner():
                self.assertEqual(self.record()['state'], 'held-stopped')
                raise RuntimeError('preflight')
        self.assertEqual(self.service.current, 'active')
        self.assertEqual(self.record()['state'], 'restored')

    def test_original_inactive_remains_inactive(self):
        self.service.current = 'inactive'
        with self.owner():
            pass
        self.assertEqual(self.service.current, 'inactive')
        self.assertEqual(self.record()['original'], 'inactive')

    def test_rom_uncertainty_holds_stopped(self):
        with self.assertRaises(RuntimeError):
            with self.owner():
                s.mark_rom_entry(self.path)
                raise RuntimeError('unknown entry result')
        self.assertEqual(self.service.current, 'inactive')
        self.assertEqual(self.record()['state'], 'held-stopped')
        self.assertNotIn('restore:active', self.service.events)
        self.owner().recover()
        self.assertNotIn('restore:active', self.service.events)

    def test_successful_qualification_restores_and_repeated_cleanup_has_no_effect(self):
        with self.owner():
            s.mark_rom_entry(self.path)
            native.atomic_json(self.path / 'qualification.json', self.qualification())
        self.assertEqual(self.service.current, 'active')
        self.service.current = 'inactive'  # A subsequent independent task now owns it.
        self.service.events.clear()
        self.owner().recover()
        self.assertEqual(self.service.events, [])
        self.assertEqual(self.service.current, 'inactive')

    def test_invalid_qualification_never_restarts(self):
        for field, value in (('flash_programming', True), ('task_id', 'another-task'),
                             ('after_sha256', 'b' * 64), ('exit', 'hard-reset'),
                             ('running_after', {'state': 'valid', 'elf_sha256': '0' * 64})):
            with self.subTest(field=field):
                proof = dict(self.qualification(), **{field: value})
                with self.assertRaises((p.Refused, KeyError)):
                    with self.owner():
                        if not (self.path / 'rom-entry-intent.json').exists():
                            s.mark_rom_entry(self.path)
                        native.atomic_json(self.path / 'qualification.json', proof)
                self.assertEqual(self.service.current, 'inactive')
                self.assertNotIn('restore:active', self.service.events)

    def test_stop_intent_is_durable_before_service_stop(self):
        original = self.service.stop
        def stop():
            self.assertEqual(self.record()['state'], 'stop-intent')
            original()
        self.service.stop = stop
        with self.owner():
            pass

    def test_stop_failure_restores_without_rom(self):
        def stop():
            self.service.current = 'inactive'
            raise RuntimeError('stop failed after effect')
        self.service.stop = stop
        with self.assertRaises(RuntimeError):
            with self.owner():
                self.fail('body cannot run')
        self.assertEqual(self.record()['state'], 'restored')
        self.assertEqual(self.service.current, 'active')

    def test_simulated_process_death_recovers_durable_stop_intent(self):
        native.atomic_json(self.path / 'service.json', dict(schema=1, task_id=self.path.name,
                           kind='qualify', original='active', state='stop-intent'))
        self.service.current = 'inactive'
        self.owner().recover()
        self.assertEqual(self.service.current, 'active')

    def test_failed_restore_is_recorded_and_cleanup_can_retry_without_hardware(self):
        owner = self.owner()
        owner.__enter__()
        with mock.patch.object(self.service, 'restore', side_effect=RuntimeError('start failed')):
            with self.assertRaises(RuntimeError):
                owner.recover()
        self.assertEqual(self.record()['state'], 'restore-failed')
        self.owner().recover()
        self.assertEqual(self.record()['state'], 'restored')

    def test_rom_marker_is_one_shot_and_precedes_entry_call(self):
        backend = cli.PiBackend({}, types.SimpleNamespace(path=self.path))
        backend.assert_stopped = mock.Mock()
        backend.app_device = '/dev/fake'
        def enter(*args, **kwargs):
            self.assertIs(kwargs['open_port'].__self__, backend)
            self.assertTrue((self.path / 'rom-entry-intent.json').is_file())
            raise RuntimeError('uncertain')
        with mock.patch.object(cli, 'physical_identity', return_value=p.APP_IDENTITY), \
             mock.patch.object(cli.font, 'enter_download_direct', side_effect=enter), \
             self.assertRaises(RuntimeError):
            backend.enter_boot()
        with self.assertRaises(FileExistsError):
            s.mark_rom_entry(self.path)

    def setup_boot(self):
        source = self.registry / 'install-service-test'
        source.mkdir()
        native.atomic_json(source / 'service.json', dict(schema=1, task_id=source.name, kind='install',
                           original='active', state='held-stopped'))
        candidate = dict(sha256='b' * 64, elf_sha256='c' * 64, bytes=10000)
        verified = dict(kind='verified-install', task_id=source.name, expected_sha256='d' * 64,
                        candidate=candidate)
        native.atomic_json(source / 'verified.json', verified)
        self.approval.update(kind='boot-only', source_task=source.name, expected_sha256='d' * 64,
                             source_verified_sha256=p.sha((source / 'verified.json').read_bytes()))
        self.service.current = 'inactive'
        return candidate

    def test_boot_only_inherits_install_service_state_and_holds_without_proof(self):
        self.setup_boot()
        with self.owner():
            pass
        self.assertEqual(self.record()['original'], 'active')
        self.assertEqual(self.record()['state'], 'held-stopped')
        self.assertEqual(self.service.current, 'inactive')

    def test_boot_success_requires_full_health_and_actual_file_measurement(self):
        candidate = self.setup_boot()
        with self.owner():
            running = dict(state='valid', slot='ota_1', address=p.APP1,
                           elf_sha256=candidate['elf_sha256'], pings=4, observed_seconds=15.1,
                           actual_file_verified=True, measurement={'actual_file_verified': True})
            native.atomic_json(self.path / 'boot-result.json', dict(kind='boot-only-result',
                source_task=self.approval['source_task'], running=running,
                reset_method=p.RESET_METHOD, flash_programming=False))
        self.assertEqual(self.service.current, 'active')
        self.assertEqual(self.record()['state'], 'restored')

    def test_boot_does_not_accept_running_service_or_unknown_source(self):
        self.setup_boot()
        self.service.current = 'active'
        with self.assertRaises(p.Refused):
            self.owner().__enter__()
        self.service.current = 'inactive'
        source = self.registry / self.approval['source_task'] / 'service.json'
        value = native.strict_json(source); value['state'] = 'restored'
        native.atomic_json(source, value)
        with self.assertRaises(p.Refused):
            self.owner().__enter__()
        self.assertNotIn('stop', self.service.events)

    def test_missing_record_cleanup_is_untouched(self):
        self.assertEqual(self.owner().recover(), {'state': 'untouched'})
        self.assertEqual(self.service.events, [])

    def test_recovery_cli_requires_execution_and_approval(self):
        with mock.patch.object(cli, 'PiBackend') as backend:
            for args in (['--recover-service'], ['--recover-service', '--execute']):
                with self.assertRaises(p.Refused):
                    cli.main(args)
            backend.assert_not_called()


if __name__ == '__main__':
    unittest.main()
