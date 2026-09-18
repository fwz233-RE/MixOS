"""Offline supervisor fault injection: subprocess/transport are always fake."""
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'tools'), str(ROOT / 'linux')]
import mixos_esp_update as native
from test_mixos_esp_update import make_image, manifest


class FakeService:
    def __init__(self, state='active', stop_error=False, restore_error=False):
        self.original, self.current = state, state
        self.events = []
        self.stop_error, self.restore_error = stop_error, restore_error
    def state(self):
        self.events.append('state')
        return self.current
    def stop(self):
        self.events.append('stop')
        if self.stop_error:
            raise native.JobError('stop failed before serial')
        self.current = 'inactive'
    def restore(self, original):
        self.events.append('restore:' + original)
        if self.restore_error:
            raise native.JobError('start failed')
        self.current = original
        return dict(original=original, state='restored', observed=original, error=None)


class SupervisorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.package = self.root / 'package'
        self.package.mkdir()
        self.data = make_image()
        (self.package / 'app.bin').write_bytes(self.data)
        (self.package / 'manifest.json').write_text(json.dumps(manifest(self.data)))
        self.locks = self.root / 'locks'
        self.locks.mkdir()
        self.state = self.root / 'state'
        self.job, self.result = native.create_job(self.package, native.DEVICE, self.state, self.locks)
        self.updates = []

    def factory(self, error=None):
        updates = self.updates
        class Updater:
            def __init__(self, connect, **kwargs):
                self.connect = connect
            def run(self, image, value, record, **kwargs):
                updates.append((image, value, kwargs))
                record({'event': 'transaction-bound', 'binding': {'transaction': '11' * 16,
                         'sha256': value['app']['sha256'], 'size': len(image), 'target': 1},
                        'source_boot_id': 2})
                if error:
                    raise error
                return {'state': 'confirmed', 'actual_file_verified': True}
            def close(self):
                updates.append('closed')
        return Updater

    def run_job(self, service=None, **kwargs):
        return native.run_worker(self.job, service=service or FakeService(),
                                 connect=lambda: self.fail('real transport forbidden'),
                                 updater_factory=kwargs.pop('updater_factory', self.factory()),
                                 install_signals=False, **kwargs)

    def test_success_is_durable_and_restores_original_state(self):
        service = FakeService()
        result = self.run_job(service)
        self.assertEqual(native.result_exit_code(result), 0)
        self.assertEqual(service.events, ['state', 'stop', 'restore:active'])
        saved = native.read_status(self.state, result['job'])
        self.assertEqual(saved['exit_code'], 0)
        events = [event['event'] for event in saved['events']]
        self.assertLess(events.index('service-original-recorded'), events.index('service-stop-verified'))
        self.assertLess(events.index('service-stop-verified'), events.index('opening-application-cdc'))
        self.assertEqual(saved['transaction']['source_boot_id'], 2)

    def test_original_inactive_stays_inactive(self):
        service = FakeService(state='inactive')
        result = self.run_job(service)
        self.assertEqual(native.result_exit_code(result), 0)
        self.assertEqual(service.current, 'inactive')
        self.assertEqual(service.events[-1], 'restore:inactive')

    def test_stop_failure_prevents_any_transport_and_is_durable(self):
        service = FakeService(stop_error=True)
        result = self.run_job(service)
        self.assertEqual(result['firmware']['state'], 'not-started')
        self.assertNotEqual(native.result_exit_code(result), 0)
        self.assertEqual(self.updates, [])
        self.assertEqual(result['service']['state'], 'restored')

    def test_restore_failure_preserves_independent_firmware_success(self):
        result = self.run_job(FakeService(restore_error=True))
        self.assertEqual(result['firmware']['state'], 'confirmed')
        self.assertEqual(result['service']['state'], 'restore-failed')
        self.assertEqual(result['error']['code'], 'service-restore')
        self.assertEqual(native.result_exit_code(result), 1)
        self.assertEqual(native.read_status(self.state, result['job'])['exit_code'], 1)

    def test_protocol_unknown_outcome_is_not_relabelled_as_rollback(self):
        import ota_v2
        result = self.run_job(updater_factory=self.factory(ota_v2.OutcomeError('END reply lost')))
        self.assertEqual(result['firmware']['state'], 'unknown')
        self.assertEqual(result['service']['state'], 'restored')
        self.assertEqual(native.result_exit_code(result), 1)

    def test_timeout_diagnostics_are_durable_without_confirming_last_valid_measurement(self):
        from test_ota_diagnostics import valid_then_timeout
        error, client, device, clock = valid_then_timeout()
        result = self.run_job(updater_factory=self.factory(error))
        saved = native.read_status(self.state, result['job'])
        self.assertEqual(saved, result)
        self.assertTrue(saved['durable'])
        self.assertEqual(saved['state'], 'complete')
        self.assertEqual(saved['firmware']['state'], 'unknown')
        self.assertEqual(saved['firmware']['evidence'], error.response.record())
        self.assertEqual(saved['firmware']['evidence']['image_state'], 2)
        self.assertNotIn('actual_file_verified', saved['firmware'])
        self.assertEqual(saved['firmware']['diagnostics'], error.diagnostics)
        self.assertEqual(saved['error']['diagnostics'], error.diagnostics)
        self.assertEqual(saved['error']['code'], 'OutcomeError')
        self.assertEqual(saved['error']['message'], str(error))
        self.assertIn(str(error.__cause__), saved['error']['message'])
        self.assertEqual(saved['service']['state'], 'restored')
        self.assertEqual(saved['exit_code'], 1)
        self.assertEqual(native.result_exit_code(saved), 1)

    def test_raw_client_timeout_diagnostics_survive_worker_persistence(self):
        from test_ota_v2 import fixture, ota_esp
        updater, device, data, clock = fixture(no_heartbeats=True)
        client = updater.open()
        device.response = lambda *args, **kwargs: None
        with self.assertRaises(ota_esp.Timeout) as caught:
            client.query()
        error = caught.exception
        result = self.run_job(updater_factory=self.factory(error))
        saved = native.read_status(self.state, result['job'])
        self.assertEqual(saved['error']['code'], 'Timeout')
        self.assertEqual(saved['error']['message'], str(error))
        self.assertEqual(saved['error']['diagnostics'], error.diagnostics)
        self.assertEqual(saved['firmware']['diagnostics'], error.diagnostics)
        self.assertEqual(saved['firmware']['state'], 'unknown')
        self.assertEqual(saved['exit_code'], 1)

    def test_close_failure_retains_firmware_timeout_diagnostics(self):
        from test_ota_diagnostics import valid_then_timeout
        error, client, device, clock = valid_then_timeout()

        class CloseFailure(self.factory(error)):
            def close(self):
                raise OSError('simulated close failure')

        result = self.run_job(updater_factory=CloseFailure)
        saved = native.read_status(self.state, result['job'])
        self.assertEqual(saved['error']['code'], 'transport-close')
        self.assertEqual(saved['firmware']['diagnostics'], error.diagnostics)
        self.assertEqual(saved['firmware']['diagnostics']['waiting_reason'], str(error.__cause__))
        self.assertEqual(saved['service']['state'], 'restored')
        self.assertEqual(saved['exit_code'], 1)

    def test_persistent_lock_prevents_second_worker_even_without_serial_node(self):
        service = FakeService()
        with native.DeviceLock(self.locks, native.DEVICE):
            result = self.run_job(service)
        self.assertEqual(service.events, [])
        self.assertEqual(self.updates, [])
        self.assertEqual(result['exit_code'], 1)
        self.assertEqual(native.read_status(self.state, result['job'])['state'], 'queued')
        self.assertTrue(result['command_receipt'])
        count = len(list(self.locks.glob('*.lock')))
        self.assertGreaterEqual(count, 1)
        with native.DeviceLock(self.locks, native.DEVICE):
            pass
        self.assertEqual(len(list(self.locks.glob('*.lock'))), count)

    def test_failed_final_journal_never_returns_success(self):
        def save(path, result):
            if Path(path).name == 'result.json' and result.get('state') == 'complete':
                raise OSError('disk unavailable')
            native.atomic_json(path, result)
        result = self.run_job(save=save)
        self.assertEqual(result['firmware']['state'], 'confirmed')
        self.assertEqual(result['service']['state'], 'restored')
        self.assertFalse(result['durable'])
        self.assertEqual(result['error']['code'], 'journal-write')
        self.assertEqual(native.result_exit_code(result), 1)

    def test_status_cannot_trust_success_renamed_before_failed_fsync(self):
        result = self.run_job()
        self.assertEqual(native.result_exit_code(result), 0)
        with mock.patch.object(native.os, 'fsync', side_effect=OSError('storage unavailable')):
            status = native.read_status(self.state, result['job'])
        self.assertFalse(status['durable'])
        self.assertEqual(status['exit_code'], 1)
        self.assertEqual(status['error']['code'], 'journal-durability')

    def test_prior_unknown_job_blocks_new_apply_before_service_or_serial(self):
        import ota_v2
        first = self.run_job(updater_factory=self.factory(ota_v2.OutcomeError('commit unknown')))
        second_dir, second = native.create_job(self.package, native.DEVICE, self.state, self.locks)
        service = FakeService()
        result = native.run_worker(second_dir, service=service, updater_factory=self.factory(),
                                   install_signals=False)
        self.assertEqual(service.events, [])
        self.assertEqual(result['firmware']['state'], 'not-started')
        self.assertIn(first['job'], result['error']['message'])

    def test_stop_post_restores_after_unhandled_process_death(self):
        result = native.strict_json(self.job / 'result.json')
        result.update(state='running', firmware={'state': 'unknown'})
        result['service'] = {'state': 'stopped', 'original': 'active'}
        native.atomic_json(self.job / 'result.json', result)
        service = FakeService(state='inactive')
        result = native.recover_worker(self.job, service=service)
        self.assertEqual(result['firmware']['state'], 'unknown')
        self.assertEqual(result['service']['state'], 'restored')
        self.assertEqual(service.current, 'active')
        self.assertEqual(result['error']['code'], 'worker-terminated')
        self.assertEqual(native.result_exit_code(result), 1)

    def test_submitted_service_runs_ordinary_user_with_recovery_outside_ssh_scope(self):
        run = mock.Mock(side_effect=[subprocess.CompletedProcess([], 0, 'yes\n', ''),
                                     subprocess.CompletedProcess([], 0, '', '')])
        native.launch_job(self.job, run=run, uid=1000, gid=1000)
        command = run.call_args.args[0]
        self.assertEqual(command[:2], ['/usr/bin/systemd-run', '--user'])
        self.assertEqual(run.call_args_list[0].args[0],
                         ['/usr/bin/loginctl', 'show-user', '1000', '--property=Linger', '--value'])
        self.assertNotIn('/usr/bin/sudo', command)
        self.assertFalse(any(arg.startswith('--property=User=') for arg in command))
        self.assertIn('--service-type=exec', command)
        self.assertIn('_worker', command)
        self.assertTrue(any('ExecStopPost=' in arg and '_recover' in arg for arg in command))
        self.assertFalse(any(arg in command for arg in ('--scope', '--pipe', '--pty', '--wait')))
        with self.assertRaisesRegex(native.JobError, 'ordinary'):
            native.launch_job(self.job, run=run, uid=0, gid=0)

    def test_resume_after_host_reboot_checks_unit_and_recovers_service_first(self):
        run = mock.Mock(return_value=subprocess.CompletedProcess([], 0,
                         'LoadState=not-found\nActiveState=inactive\n', ''))
        service = FakeService(state='inactive')
        result = native.strict_json(self.job / 'result.json')
        result.update(state='running', firmware={'state': 'unknown'})
        result['service'] = {'state': 'stopped', 'original': 'active'}
        native.atomic_json(self.job / 'result.json', result)
        recovered = native.prepare_resume(self.job, run=run,
                      recover=lambda path: native.recover_worker(path, service=service))
        self.assertEqual(recovered['state'], 'complete')
        self.assertEqual(service.current, 'active')
        self.assertEqual(recovered['firmware']['state'], 'unknown')

    def test_resume_active_worker_is_refused_without_recovery_or_state_mutation(self):
        run = mock.Mock(return_value=subprocess.CompletedProcess([], 0,
                         'LoadState=loaded\nActiveState=active\n', ''))
        recover = mock.Mock()
        before = (self.job / 'result.json').read_bytes()
        with self.assertRaisesRegex(native.JobError, 'active'):
            native.prepare_resume(self.job, run=run, recover=recover)
        recover.assert_not_called()
        self.assertEqual((self.job / 'result.json').read_bytes(), before)

    def test_resume_dry_run_never_contacts_user_systemd(self):
        with mock.patch.object(native, 'prepare_resume') as resume, \
                mock.patch.object(native, 'launch_job') as launch, \
                mock.patch('sys.stdout', io.StringIO()):
            self.assertEqual(native.main(['apply', '--resume', self.result['job'],
                                         '--state-root', str(self.state), '--dry-run']), 0)
        resume.assert_not_called()
        launch.assert_not_called()

    def test_user_manager_without_linger_never_submits_a_background_job(self):
        run = mock.Mock(return_value=subprocess.CompletedProcess([], 0, 'no\n', ''))
        with self.assertRaisesRegex(native.JobError, 'lingering'):
            native.launch_job(self.job, run=run, uid=1000, gid=1000)
        self.assertEqual(run.call_count, 1)
        self.assertEqual(run.call_args.args[0][0], '/usr/bin/loginctl')

    def test_package_is_portable_and_runner_works_in_isolated_python(self):
        bundle = native.bundle_release(self.package / 'app.bin', self.package / 'manifest.json', self.root / 'bundle')
        # Python execution is strictly local --help, no service/network/device.
        answer = subprocess.run([sys.executable, '-I', str(bundle / 'linux/mixos-esp-update'), '--help'],
                                capture_output=True, text=True, check=False)
        self.assertEqual(answer.returncode, 0, answer.stderr)
        self.assertIn('inspect', answer.stdout)
        self.assertNotIn('deploy_display.py', '\n'.join(str(p.relative_to(bundle)) for p in bundle.rglob('*')))


class ServiceCommandTests(unittest.TestCase):
    def test_stop_exit_failure_and_running_mainpid_never_pass(self):
        for answer in (subprocess.CompletedProcess([], 1, '', 'denied'),
                       subprocess.CompletedProcess([], 0, 'LoadState=loaded\nActiveState=active\nSubState=running\nMainPID=123\n', '')):
            def run(command, **kwargs):
                return answer
            with self.assertRaises(native.JobError):
                native.SystemdService(run=run).stop()

    def test_restore_start_exit_failure_checked(self):
        answers = [subprocess.CompletedProcess([], 0, 'LoadState=loaded\nActiveState=inactive\nSubState=dead\nMainPID=0\nControlPID=0\n', ''),
                   subprocess.CompletedProcess([], 1, '', 'start refused')]
        with self.assertRaisesRegex(native.JobError, 'start failed'):
            native.SystemdService(run=mock.Mock(side_effect=answers)).restore('active')

    def test_serial_transport_dtr_only_after_live_identity_verifier(self):
        # Mock Linux-only modules on Windows as well: no ioctl/os.open escapes.
        import types
        import serial_transport
        order = []
        constants = dict(TIOCEXCL=1, TCSANOW=2, CLOCAL=4, CREAD=8, HUPCL=16,
                         B115200=115200, TCIOFLUSH=3, TIOCMBIS=7, TIOCM_DTR=2)
        termios = types.SimpleNamespace(**constants, tcgetattr=lambda fd: [0, 0, 16, 0, 0, 0, []],
                                      tcsetattr=lambda *args: None, tcflush=lambda *args: None)
        ioctl = mock.Mock(side_effect=lambda *a: order.append(('ioctl', a)))
        fcntl = types.SimpleNamespace(LOCK_EX=1, LOCK_NB=2, flock=lambda *a: None, ioctl=ioctl)
        tty = types.SimpleNamespace(setraw=lambda *a: None)
        verifier = lambda device: order.append(('verified', device)) or 88
        with mock.patch.dict(sys.modules, {'fcntl': fcntl, 'termios': termios, 'tty': tty}), \
                mock.patch.object(serial_transport.os, 'open', return_value=44), \
                mock.patch.object(serial_transport.os, 'fstat', return_value=types.SimpleNamespace(st_rdev=88)), \
                mock.patch.object(serial_transport.os, 'O_NOCTTY', 0, create=True), \
                mock.patch.object(serial_transport.os, 'O_NONBLOCK', 0, create=True), \
                mock.patch.object(serial_transport.os, 'O_CLOEXEC', 0, create=True):
            self.assertEqual(serial_transport.open_serial(native.DEVICE, assert_dtr=True, verifier=verifier), 44)
        self.assertEqual(order[0][0], 'verified')
        self.assertEqual(ioctl.call_args.args, (44, 7, b'\x02\0\0\0'))
        self.assertEqual(ioctl.call_count, 2)


if __name__ == '__main__':
    unittest.main()
