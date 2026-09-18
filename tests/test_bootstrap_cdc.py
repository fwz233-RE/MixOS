"""Regression coverage for actual bootstrap CDC adaptation and recovery bounds."""
import errno
from pathlib import Path
import sys
import types
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'tools'), str(ROOT / 'linux')]
import bootstrap_ota_on_pi as cli
from _mixlib import application_cdc as cdc
from _mixlib import ota_bootstrap as p
import mixos_esp_update as native


class ApplicationPortTests(unittest.TestCase):
    def setup_paths(self, stable_node='/dev/ttyACM0'):
        def resolve(path, strict=False):
            self.assertTrue(strict)
            return Path(stable_node if path == Path(native.DEVICE) else str(path))
        patches = [mock.patch.object(Path, 'resolve', resolve),
                   mock.patch.object(Path, 'stat', return_value=types.SimpleNamespace(st_rdev=55)),
                   mock.patch.object(cdc.os, 'fstat', return_value=types.SimpleNamespace(st_rdev=55))]
        for patch in patches:
            patch.start(); self.addCleanup(patch.stop)

    def test_tty_input_opens_stable_identity_using_real_native_adapter(self):
        self.setup_paths()
        with mock.patch('serial_transport.open_serial', return_value=44) as opened, \
             mock.patch.object(cdc.os, 'close') as close:
            with cdc.ApplicationCdc('/dev/ttyACM0', native.DEVICE) as port:
                self.assertEqual(port.fileno(), 44)
            port.close()
        opened.assert_called_once_with(native.DEVICE, assert_dtr=True)
        close.assert_called_once_with(44)

    def test_wrong_stable_node_refused_before_open(self):
        self.setup_paths('/dev/ttyACM9')
        with mock.patch('serial_transport.open_serial') as opened, self.assertRaisesRegex(ValueError, 'differ'):
            cdc.ApplicationCdc('/dev/ttyACM0', native.DEVICE)
        opened.assert_not_called()

    def test_handle_change_closes_and_refuses(self):
        self.setup_paths()
        with mock.patch('serial_transport.open_serial', return_value=44), \
             mock.patch.object(cdc.os, 'fstat', return_value=types.SimpleNamespace(st_rdev=99)), \
             mock.patch.object(cdc.os, 'close') as close, self.assertRaisesRegex(ValueError, 'changed'):
            cdc.ApplicationCdc('/dev/ttyACM0', native.DEVICE)
        close.assert_called_once_with(44)

    def test_raw_reads_distinguish_temporary_no_data_from_disconnect(self):
        port = object.__new__(cdc.ApplicationCdc); port.fd = 44
        with mock.patch.object(cdc.os, 'read', side_effect=BlockingIOError()):
            self.assertEqual(port.read(100), b'')
        with mock.patch.object(cdc.os, 'read', return_value=b''), self.assertRaisesRegex(OSError, 'disconnected'):
            port.read(100)
        with mock.patch.object(cdc.os, 'write', side_effect=BlockingIOError()):
            self.assertEqual(port.write(b'x'), 0)


class HealthRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.backend = cli.PiBackend({}, None)
        self.backend.assert_stopped = mock.Mock()
        self.clock = 0.0
        self.port = mock.MagicMock()
        self.port.__enter__.return_value = self.port
        self.port.fileno.return_value = 44
        self.backend.open_application = mock.Mock(return_value=self.port)
        self.links = []
        self.handshake_failure = None
        def link_factory(*args, **kwargs):
            link = mock.Mock(epoch=0)
            def handshake(timeout):
                if self.handshake_failure:
                    self.clock += timeout
                    raise self.handshake_failure
                link.epoch = 42
            link.handshake.side_effect = handshake
            self.links.append(link)
            return link
        patches = {
            'wait': mock.patch.object(cli.esp, 'wait_port', return_value=('/dev/ttyACM0', p.APP_IDENTITY)),
            'identity': mock.patch.object(cli, 'physical_identity', return_value=p.APP_IDENTITY),
            'audit': mock.patch.object(cli.esp, 'audit'),
            'clock': mock.patch.object(cli.time, 'monotonic', side_effect=lambda: self.clock),
            'sleep': mock.patch.object(cli.time, 'sleep', side_effect=self.advance),
            'link': mock.patch.object(cli.ota_esp, 'Link', side_effect=link_factory),
            'observe': mock.patch.object(cli, 'observe_app_health', return_value={'state': 'valid'}),
            'stat': mock.patch.object(cli.os, 'stat', return_value=types.SimpleNamespace(st_rdev=55)),
            'fstat': mock.patch.object(cli.os, 'fstat', return_value=types.SimpleNamespace(st_rdev=55)),
        }
        self.mocks = {}
        for name, patch in patches.items():
            self.mocks[name] = patch.start(); self.addCleanup(patch.stop)

    def advance(self, seconds):
        self.clock += seconds

    def test_before_hello_transient_open_can_recover_without_reset(self):
        self.backend.open_application.side_effect = [OSError(errno.ETIMEDOUT, 'CDC open'), self.port]
        self.assertEqual(self.backend.healthy_app({}, 0), {'state': 'valid'})
        self.assertEqual(self.backend.open_application.call_count, 2)
        self.assertEqual(self.backend.app_device, '/dev/ttyACM0')
        self.assertEqual(self.mocks['audit'].call_args.kwargs['stage'], 'cdc-open')

    def test_persistent_open_error_stops_at_three_attempts(self):
        self.backend.open_application.side_effect = OSError(errno.ETIMEDOUT, 'CDC open')
        with self.assertRaises(OSError): self.backend.healthy_app({}, 0)
        self.assertEqual(self.backend.open_application.call_count, 3)
        self.mocks['observe'].assert_not_called()

    def test_hello_timeout_releases_every_handle_and_has_finite_budget(self):
        self.handshake_failure = cli.ota_esp.Timeout('no HELLO')
        with self.assertRaises(cli.ota_esp.Timeout): self.backend.healthy_app({}, 0)
        self.assertEqual(self.backend.open_application.call_count, 3)
        self.assertEqual(self.port.__exit__.call_count, 3)
        self.assertEqual(self.clock, 32)
        self.mocks['observe'].assert_not_called()

    def test_error_after_hello_never_restarts_health_observation(self):
        self.mocks['observe'].side_effect = OSError('lost during health')
        with self.assertRaises(OSError): self.backend.healthy_app({}, 0)
        self.assertEqual(self.backend.open_application.call_count, 1)
        self.assertEqual(self.port.__exit__.call_count, 1)

    def test_identity_rechecked_on_every_reenumeration(self):
        self.backend.open_application.side_effect = OSError('disconnected')
        self.mocks['wait'].side_effect = [('/dev/ttyACM0', p.APP_IDENTITY),
                                         ('/dev/ttyACM1', dict(p.APP_IDENTITY, location='5-1.1'))]
        with self.assertRaises(p.Refused): self.backend.healthy_app({}, 0)
        self.assertEqual(self.backend.open_application.call_count, 1)

    def test_layout_health_and_permission_refusals_not_swallowed(self):
        for error in (p.Refused('ELF mismatch'), RuntimeError('service not stopped'), ValueError('wrong by-id')):
            self.backend.open_application.reset_mock(side_effect=True)
            self.backend.open_application.side_effect = error
            with self.subTest(error=error), self.assertRaises(type(error)):
                self.backend.healthy_app({}, 0)
            self.assertEqual(self.backend.open_application.call_count, 1)

    def test_deadline_includes_slow_enumeration(self):
        def wait(*args, **kwargs):
            self.advance(30)
            return '/dev/ttyACM0', p.APP_IDENTITY
        def opened(*args):
            self.advance(10)
            raise TimeoutError('open deadline')
        self.mocks['wait'].side_effect = wait
        self.backend.open_application.side_effect = opened
        with self.assertRaises(TimeoutError): self.backend.healthy_app({}, 0)
        self.assertEqual(self.backend.open_application.call_count, 3)
        # Real operation_timeout uses the residual budget; the fake open
        # intentionally does not enforce it, but the enumerations are bounded.
        self.assertLessEqual(self.mocks['wait'].call_args.kwargs['timeout'], 30)


if __name__ == '__main__':
    unittest.main()
