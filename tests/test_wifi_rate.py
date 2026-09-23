"""Live kernel receive-counter sampling and bounded STATUS wire contract."""
import json
from pathlib import Path
import struct
import unittest
from unittest.mock import patch

from _support import ROOT
import sys
sys.path.insert(0, str(ROOT / 'linux'))
import mixosd
import netctl
from protocol import Channel as C, Type as T, Frame
from test_linux import FakeNet, FakeShell, drain

STATE = {'connected': True, 'ssid': 'lab', 'signal': 71, 'interface': 'wlan0'}


class WifiRateTests(unittest.TestCase):
    def setUp(self):
        self.now = 100.0
        self.metrics = mixosd.HostMetrics(clock=lambda: self.now)
        self.counter = self.enterContext(patch.object(netctl, '_interface_bytes', return_value=1000))
        self.carrier = self.enterContext(patch.object(netctl, '_interface_link', return_value=7))
        self.enterContext(patch.object(Path, 'read_text', side_effect=OSError('no proc fixtures')))
        self.nmcli = self.enterContext(patch.object(netctl, '_run', side_effect=AssertionError('inline nmcli')))

    def sample(self, seconds=2, counter=None):
        self.now += seconds
        if counter is not None:
            self.counter.return_value = counter
        return self.metrics.sample()

    def connected(self):
        self.counter.return_value = 1000
        self.metrics.report_network(STATE, '10.0.0.5')
        self.assertNotIn('rx_bps', self.metrics.sample()['wifi'])
        self.assertEqual(self.sample(counter=1200)['wifi']['rx_bps'], 100.0)

    def test_live_samples_change_between_networkmanager_refreshes_and_idle_is_zero(self):
        self.connected()
        self.assertEqual(self.sample(counter=5200)['wifi']['rx_bps'], 2000.0)
        self.assertEqual(self.sample(counter=5200)['wifi']['rx_bps'], 0.0)
        self.metrics.report_network(dict(STATE, rx_bytes=999999, rx_bps=999999), '10.0.0.5')
        self.assertEqual(self.sample(counter=5400)['wifi']['rx_bps'], 100.0)
        self.assertEqual(self.counter.call_count, 5)
        self.counter.assert_called_with('wlan0', 'rx')
        self.nmcli.assert_not_called()

    def test_unknown_disconnected_and_missing_interface_keep_old_state_semantics(self):
        self.assertNotIn('wifi', self.metrics.sample())
        for state in ({'connected': False, 'ssid': '', 'signal': None},
                      {'connected': True, 'ssid': 'lab', 'signal': 71}):
            self.now += 1
            self.metrics.report_network(state, '')
            self.assertEqual(self.metrics.sample()['wifi'], state)
        self.counter.assert_not_called()
        self.metrics.report_network(None, '')
        self.assertNotIn('wifi', self.sample())

    def test_unknown_and_disconnect_require_new_baseline(self):
        for state in (None, {'connected': False, 'ssid': '', 'signal': None}):
            with self.subTest(state=state):
                self.now += 1
                self.metrics.report_network(STATE, '')
                self.sample()
                self.metrics.report_network(state, '')
                self.sample()
                self.metrics.report_network(STATE, '')
                self.assertNotIn('rx_bps', self.sample(counter=9000)['wifi'])
                self.assertEqual(self.sample(counter=9200)['wifi']['rx_bps'], 100)

    def test_interface_ssid_and_kernel_index_changes_reset_the_pair(self):
        self.connected()
        for updates in ({'interface': 'wlan1'}, {'ssid': 'other'}, {'interface': 'wlan0'}):
            self.metrics.report_network(dict(STATE, **updates), '')
            self.assertNotIn('rx_bps', self.sample(counter=2000)['wifi'])
            self.assertEqual(self.sample(counter=2200)['wifi']['rx_bps'], 100)
        self.carrier.return_value = (8, 1)
        self.assertNotIn('rx_bps', self.sample(counter=3000)['wifi'])
        self.assertEqual(self.sample(counter=3200)['wifi']['rx_bps'], 100)
        self.metrics.report_network(STATE, '')
        self.carrier.return_value = (8, 3)
        self.assertNotIn('rx_bps', self.sample(counter=3400)['wifi'])

    def test_carrier_loss_and_removal_during_read_never_report_zero(self):
        self.connected()
        self.carrier.return_value = None
        before = self.counter.call_count
        self.assertNotIn('rx_bps', self.sample()['wifi'])
        self.assertEqual(self.counter.call_count, before)
        self.carrier.return_value = 7
        self.assertNotIn('rx_bps', self.sample(counter=5000)['wifi'])
        self.carrier.side_effect = [7, 8]
        self.assertNotIn('rx_bps', self.sample(counter=5200)['wifi'])
        self.carrier.side_effect = None
        self.assertNotIn('rx_bps', self.sample(counter=5400)['wifi'])

    def test_invalid_counter_values_withdraw_rate_and_baseline(self):
        self.connected()
        for bad in (None, True, False, -1, 1.0, '123', float('nan'), float('inf'), 2**64):
            with self.subTest(counter=bad):
                self.metrics.report_network(STATE, '')
                self.counter.return_value = bad
                self.assertNotIn('rx_bps', self.sample()['wifi'])
                self.assertNotIn('rx_bps', self.sample(counter=1000)['wifi'])
                self.assertEqual(self.sample(counter=1200)['wifi']['rx_bps'], 100)

    def test_counter_reset_establishes_new_pair_without_wrap_estimate(self):
        self.connected()
        self.assertNotIn('rx_bps', self.sample(counter=5)['wifi'])
        self.assertEqual(self.sample(counter=205)['wifi']['rx_bps'], 100)

    def test_stale_state_and_long_sampling_gap_require_a_fresh_pair(self):
        self.connected()
        self.now += mixosd.WIFI_STATE_MAX_AGE
        before = self.counter.call_count
        self.assertNotIn('rx_bps', self.metrics.sample()['wifi'])
        self.assertEqual(self.counter.call_count, before)
        self.metrics.report_network(STATE, '')
        self.assertNotIn('rx_bps', self.sample(counter=9000)['wifi'])
        self.assertNotIn('rx_bps', self.sample(seconds=7, counter=11000)['wifi'])
        self.assertEqual(self.sample(counter=11200)['wifi']['rx_bps'], 100)

    def test_refresh_after_unobserved_staleness_does_not_resurrect_old_pair(self):
        self.connected()
        self.now += 20
        self.metrics.report_network(STATE, '')
        self.assertNotIn('rx_bps', self.sample(counter=9000)['wifi'])

    def test_nonmonotonic_and_nonfinite_clock_require_new_observation(self):
        for bad in (102.0, 101.0, float('nan'), float('inf'), -float('inf')):
            with self.subTest(clock=bad):
                self.now = 100
                self.metrics = mixosd.HostMetrics(clock=lambda: self.now)
                self.connected()
                self.now = bad
                self.assertNotIn('rx_bps', self.metrics.sample()['wifi'])
                self.now = 104
                self.assertNotIn('rx_bps', self.metrics.sample()['wifi'])
                self.metrics.report_network(STATE, '')
                self.assertNotIn('rx_bps', self.sample(counter=4000)['wifi'])
                self.assertEqual(self.sample(counter=4200)['wifi']['rx_bps'], 100)

    def test_delayed_helper_observation_is_not_timestamped_on_receipt(self):
        self.metrics.report_network(STATE, '', observed_at=self.now-14)
        self.assertNotIn('rx_bps', self.metrics.sample()['wifi'])
        self.assertNotIn('rx_bps', self.sample(counter=5000)['wifi'])
        for at in (self.now-16, self.now+1, float('nan'), float('inf'), True, '100'):
            self.metrics.report_network(STATE, '10.0.0.5', observed_at=at)
            self.assertNotIn('wifi', self.metrics.sample())

    def test_old_helper_observation_and_clock_regression_between_samples(self):
        self.connected()
        self.metrics.report_network(STATE, '', observed_at=99)
        self.assertNotIn('rx_bps', self.sample(counter=5000)['wifi'])
        self.metrics.report_network(STATE, '')
        self.assertNotIn('rx_bps', self.sample(counter=5200)['wifi'])
        self.assertEqual(self.sample(counter=5400)['wifi']['rx_bps'], 100)
        self.now -= 1
        self.metrics.report_network(STATE, '')
        self.assertNotIn('rx_bps', self.sample(counter=5600)['wifi'])

    def test_connect_and_forget_invalidate_before_helper_completion(self):
        for kind in (T.NET_CONNECT, T.NET_FORGET):
            with self.subTest(kind=kind):
                link = mixosd.Link(FakeShell, metrics=self.metrics,
                                  clock=lambda: self.now, net=FakeNet())
                link.rx.epoch = 1
                self.metrics.report_network(STATE, '')
                self.sample()
                link.network(Frame(C.NET, kind, 1, 7, 1, b'\x03lab'))
                self.assertNotIn('wifi', self.sample())
                link.next_state_refresh = self.now+10
                link.net_answer(kind, 7, {'ok': False, 'error': 'failed'})
                self.assertEqual(link.next_state_refresh, 0)

    def test_status_requests_measure_each_time_and_epoch_resets_cancel_identity(self):
        link = mixosd.Link(FakeShell, metrics=self.metrics, clock=lambda: self.now, net=FakeNet())
        link.handle(Frame(C.CONTROL, T.HELLO, 33, 0, 1, struct.pack('<HH', 512, 4096)))
        drain(link.tx)
        self.metrics.report_network(STATE, '')
        rates = []
        for seq, count in enumerate((1000, 1200, 5200), 2):
            self.now += 2
            self.counter.return_value = count
            link.handle(Frame(C.STATUS, T.STATUS_REQUEST, 33, 0, seq))
            frames = drain(link.tx)
            self.assertEqual(len(frames), 1)
            rates.append(json.loads(frames[0].payload)['wifi'].get('rx_bps'))
        self.assertEqual(rates, [None, 100, 2000])
        link.net.start('state', 0, {'verb': 'state'}, 12)
        link.handle(Frame(C.CONTROL, T.HELLO, 34, 0, 1, struct.pack('<HH', 512, 4096)))
        self.assertFalse(link.net.busy())
        self.assertNotIn('wifi', self.sample())
        self.metrics.report_network(STATE, '')
        self.sample()
        link.reset()
        self.assertNotIn('wifi', self.sample())

    def test_ssid_byte_limit_and_maximum_json_escaping_keep_status_bounded(self):
        link = mixosd.Link(FakeShell, clock=lambda: self.now, net=FakeNet())
        link.rx.epoch = 1
        for ssid in ('\x01'*32, '😀'*32, '\\"'*16, '网'*32):
            with self.subTest(ssid=repr(ssid)):
                self.now += 2
                self.metrics.report_network(dict(STATE, ssid=ssid), '255.255.255.255')
                value = self.sample()
                self.assertLessEqual(len(value['wifi']['ssid'].encode()), 32)
                value.update(uptime_s=4294967294.99, cpu_pct=100.0,
                             mem_used_kib=4294967294, mem_total_kib=4294967294,
                             time_s=4294967294, tz_offset_min=-1440)
                value['wifi']['rx_bps'] = 3.4028234663852886e38
                link.send_json(C.STATUS, T.STATUS, 0, value)
                wire = drain(link.tx)[0]
                self.assertLessEqual(len(wire.payload), 512)
                self.assertEqual(json.loads(wire.payload)['wifi']['rx_bps'], value['wifi']['rx_bps'])
                self.assertEqual(value['wifi']['ssid'], self.metrics._bounded_ssid(ssid))


class InterfaceCounterTests(unittest.TestCase):
    def test_paths_only_use_valid_interface_and_fixed_counter_name(self):
        with patch.object(Path, 'read_text', return_value='123\n') as read:
            for device in ('', '.', '..', '../wlan0', '/etc', 'wlan0/x', 'wlan0\\x',
                           'wlan0:1', 'wlan\x00', '无线', 42, None, 'x'*16):
                self.assertIsNone(netctl._interface_bytes(device, 'rx'))
                self.assertIsNone(netctl._interface_link(device))
            self.assertIsNone(netctl._interface_bytes('wlan0', '../../passwd'))
            read.assert_not_called()
            self.assertEqual(netctl._interface_bytes('wl-usb_0.1', 'rx'), 123)

    def test_reads_exact_kernel_files_and_rejects_invalid_values(self):
        paths = []
        def read(path):
            paths.append(path.as_posix())
            return {'carrier': '1', 'ifindex': '7', 'carrier_changes': '2', 'rx_bytes': '345\n'}[path.name]
        with patch.object(Path, 'read_text', read):
            self.assertEqual(netctl._interface_link('wlan0'), (7, 2))
            self.assertEqual(netctl._interface_bytes('wlan0', 'rx'), 345)
        self.assertEqual(paths, ['/sys/class/net/wlan0/carrier', '/sys/class/net/wlan0/ifindex',
                                 '/sys/class/net/wlan0/carrier_changes',
                                 '/sys/class/net/wlan0/statistics/rx_bytes'])
        for text in ('-1', str(2**64), 'nan', '1.5', ''):
            with patch.object(Path, 'read_text', return_value=text):
                self.assertIsNone(netctl._interface_bytes('wlan0', 'rx'))
        with patch.object(Path, 'read_text', side_effect=OSError('gone')):
            self.assertIsNone(netctl._interface_bytes('wlan0', 'rx'))
            self.assertIsNone(netctl._interface_link('wlan0'))

    def test_networkmanager_reports_identity_not_rate_and_requires_requested_fields(self):
        with patch.object(netctl, '_run', return_value=' :wlan1:80:other\n*:wlan0:65:my\\:lab\n'), \
                patch.object(netctl, '_interface_bytes') as counter:
            self.assertEqual(netctl.state(), dict(STATE, ssid='my:lab', signal=65))
            counter.assert_not_called()
        with patch.object(netctl, '_run', return_value='*:65:lab\n'):
            self.assertFalse(netctl.state()['connected'])
        with patch.object(netctl, '_run', return_value='*:../bad:65:lab\n'):
            self.assertNotIn('interface', netctl.state())

    def test_helper_records_observation_before_ip_lookup_on_active_interface(self):
        calls = []
        with patch.object(netctl, 'state', return_value=STATE), \
                patch.object(netctl.time, 'monotonic', side_effect=lambda: calls.append('clock') or 10), \
                patch.object(netctl, 'local_address', side_effect=lambda device: calls.append(device) or '10.0.0.5'):
            answer = netctl.handle({'verb': 'state'})
        self.assertEqual(calls, ['clock', 'wlan0'])
        self.assertEqual(answer['observed_at'], 10)
        self.assertEqual(answer['state'], STATE)


if __name__ == '__main__':
    unittest.main()
