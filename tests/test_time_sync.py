"""Clock/timezone sampling and real USB-to-firmware holdover regressions."""
from datetime import datetime, timezone
import json
import os
import struct
import subprocess
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from _support import ROOT, host_command, host_run, posix_path, require_host_cc
from test_link_update import HEADERS
import mixosd
from protocol import Frame, Channel as C, Type as T

OUT = ROOT / 'build/host-time-sync'
JSON_DIR = ROOT / '.tools/esp-idf-clean/components/json/cJSON'
MAIN = ROOT / 'firmware/esp32s3/main'
STAMP = int(datetime(2026, 9, 20, 6, 22, 54, tzinfo=timezone.utc).timestamp())


class HostClockTests(unittest.TestCase):
    def sample(self, stamp=STAMP):
        with patch.object(mixosd.time, 'time', return_value=stamp) as wall, \
                patch.object(mixosd.Path, 'read_text', side_effect=OSError):
            result = mixosd.HostMetrics().sample()
        wall.assert_called_once_with()
        return result

    def test_offset_uses_same_timestamp_and_per_instant_offset(self):
        local = SimpleNamespace(tm_isdst=0, tm_gmtoff=20700)
        with patch.object(time, 'localtime', return_value=local) as convert, \
                patch.object(time, 'timezone', 0):
            result = self.sample()
        self.assertEqual(result['time_s'], STAMP)
        self.assertEqual(result['tz_offset_min'], 345)
        convert.assert_called_once_with(STAMP)

    def test_refreshes_libc_timezone_before_sampling(self):
        with patch.object(time, 'tzset', create=True) as reload_tz, \
                patch.object(time, 'localtime', return_value=SimpleNamespace(tm_gmtoff=28800)) as convert:
            result = self.sample()
        reload_tz.assert_called_once_with()
        convert.assert_called_once_with(STAMP)
        self.assertEqual(result['tz_offset_min'], 480)

    def test_portable_fallback_without_gmtoff(self):
        with patch.object(time, 'tzset', create=True), \
                patch.object(time, 'localtime', return_value=SimpleNamespace(tm_isdst=1)), \
                patch.object(time, 'daylight', 1), patch.object(time, 'altzone', 14400):
            self.assertEqual(self.sample()['tz_offset_min'], -240)

    def test_invalid_or_subminute_offsets_are_unknown_not_clamped(self):
        for seconds in (-721*60, 841*60, 28801):
            with self.subTest(seconds=seconds), patch.object(
                    time, 'localtime', return_value=SimpleNamespace(tm_gmtoff=seconds, tm_isdst=0)):
                self.assertIsNone(self.sample()['tz_offset_min'])

    @unittest.skipUnless(hasattr(time, 'tzset'), 'real timezone refresh requires POSIX tzset')
    def test_real_runtime_timezone_change_without_daemon_restart(self):
        try:
            with patch.dict(os.environ, {'TZ': 'UTC0'}):
                time.tzset()
                self.assertEqual(self.sample()['tz_offset_min'], 0)
                # Change configuration while the daemon remains alive. Only
                # production sampling may refresh libc, not the test itself.
                os.environ['TZ'] = 'Asia/Shanghai'
                result = self.sample()
                self.assertEqual(result['time_s'], STAMP)
                self.assertEqual(result['tz_offset_min'], 480)
        finally:
            time.tzset()

    @unittest.skipUnless(hasattr(time, 'tzset'), 'real timezone rules require POSIX tzset')
    def test_real_utc_dst_and_non_hour_zones(self):
        cases = [('UTC0', STAMP, 0), ('Asia/Shanghai', STAMP, 480),
                 ('Asia/Kathmandu', STAMP, 345), ('Etc/GMT+12', STAMP, -720),
                 ('Pacific/Kiritimati', STAMP, 840),
                 ('America/New_York', 1767225600, -300),
                 ('America/New_York', 1782864000, -240),
                 # The two instants straddle the 2026 spring DST transition.
                 ('America/New_York', 1772953199, -300),
                 ('America/New_York', 1772953200, -240)]
        try:
            for zone, stamp, expected in cases:
                with self.subTest(zone=zone, stamp=stamp), patch.dict(os.environ, {'TZ': zone}):
                    result = self.sample(stamp)
                    self.assertEqual(result['time_s'], stamp)
                    self.assertEqual(result['tz_offset_min'], expected)
        finally:
            time.tzset()


class TimeWireTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cc = require_host_cc()
        if not (JSON_DIR / 'cJSON.c').is_file():
            raise unittest.SkipTest('vendored cJSON unavailable; real JSON parser required')
        OUT.mkdir(parents=True, exist_ok=True)
        for name, contents in HEADERS.items():
            if name == 'cJSON.h':
                contents = (JSON_DIR / name).read_text(encoding='utf-8')
            dest = OUT / 'stubs' / name
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(contents, encoding='utf-8')
        fixture = (ROOT / 'tests/test_link_update_host.c').read_text(encoding='utf-8')
        begin = fixture.index('cJSON *cJSON_ParseWithLength(')
        end = fixture.index('\n\n/* A model of mix_ota.c:', begin)
        fixture = fixture[:begin] + fixture[end:]
        fixture = fixture.replace('../firmware/esp32s3/main/mix_link.c', posix_path(MAIN / 'mix_link.c'))
        (OUT / 'link_fixture.h').write_text(fixture, encoding='utf-8')
        cls.exe = OUT / 'time_sync_harness'
        host_run([cc, '-std=c11', '-Wall', '-Wextra', '-Werror', '-Wno-misleading-indentation',
                  '-g', '-O1', '-fsanitize=address,undefined', '-fno-omit-frame-pointer', '-no-pie',
                  '-I'+posix_path(OUT / 'stubs'), '-I'+posix_path(OUT),
                  posix_path(ROOT / 'tests/test_time_sync_host.c'),
                  posix_path(MAIN / 'mix_protocol.c'), posix_path(MAIN / 'mix_terminal.c'),
                  posix_path(JSON_DIR / 'cJSON.c'), '-lm', '-o', posix_path(cls.exe)])

    def run_lines(self, lines):
        result = subprocess.run(host_command([posix_path(self.exe)]), input='\n'.join(lines)+'\n',
                                capture_output=True, text=True, encoding='utf-8', timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout+result.stderr)
        output = result.stdout.splitlines()
        self.assertEqual(output[0], 'READY 101')
        return [tuple(map(int, line.split())) for line in output[1:]]

    @staticmethod
    def status(value=None, ms=1000, seq=2, epoch=101, session=0):
        if value is None:
            value = {'time_s': STAMP, 'tz_offset_min': 480}
        payload = value if isinstance(value, bytes) else json.dumps(value, separators=(',', ':')).encode()
        return f'{ms} '+Frame(C.STATUS, T.STATUS, epoch, session, seq, payload).encode().hex()

    def test_host_status_sample_reaches_firmware_without_double_offset(self):
        for offset in (0, 480, -210, 345, 840):
            with self.subTest(offset=offset), patch.object(time, 'time', return_value=STAMP), \
                    patch.object(time, 'localtime', return_value=SimpleNamespace(tm_gmtoff=offset*60)), \
                    patch.object(mixosd.Path, 'read_text', side_effect=OSError):
                sample = mixosd.HostMetrics().sample()
                values = self.run_lines([self.status(sample), 'T 2000'])
                self.assertEqual(values[-1][:2], (STAMP+1, offset))

    def test_initial_unknown_and_seconds_carried_between_polls(self):
        values = self.run_lines(['T 1000', self.status(ms=1250), 'T 2249', 'T 2250', 'T 3251'])
        self.assertEqual([v[:2] for v in values],
                         [(0, 0), (STAMP, 480), (STAMP, 480), (STAMP+1, 480), (STAMP+2, 480)])

    def test_missing_status_with_live_heartbeats_preserves_clock_and_zone(self):
        values = self.run_lines([self.status(), 'T 7000', 'T 60000', 'T 3601000'])
        self.assertEqual(values[-1][:3], (STAMP+3600, 480, 1))

    def test_disconnect_reconnect_epoch_and_delayed_status_preserve_zone(self):
        hello = Frame(C.CONTROL, T.HELLO_ACK, 102, 0, 1, struct.pack('<HH', 512, 4096))
        values = self.run_lines([self.status(), 'D 1500', 'T 3000', 'R 4000',
                                '4001 '+hello.encode().hex(), 'T 9000',
                                self.status({'time_s': STAMP+20, 'tz_offset_min': 345},
                                            ms=10000, epoch=102)])
        self.assertEqual([v[:2] for v in values[:-1]],
                         [(STAMP, 480), (STAMP, 480), (STAMP+2, 480),
                          (STAMP+3, 480), (STAMP+3, 480), (STAMP+8, 480)])
        self.assertEqual(values[-1][:3], (STAMP+20, 345, 1))

    def test_queue_fault_and_heartbeat_timeout_keep_clock_pair(self):
        for reset in ('F 2000', 'X 10001'):
            with self.subTest(reset=reset):
                values = self.run_lines([self.status(), reset])
                elapsed = (int(reset.split()[1])-1000)//1000
                self.assertEqual(values[-1][:3], (STAMP+elapsed, 480, 0))

    def test_incomplete_invalid_status_never_partially_overwrites_clock(self):
        invalid = [{}, {'time_s': STAMP+99}, {'tz_offset_min': -60}, b'{broken',
                   b'{"time_s":1e999,"tz_offset_min":0}',
                   b'{"time_s":123,"tz_offset_min":1e999}']
        for value in (None, True, False, '123', [], {}, -1, 0, 1.5, 4294967296):
            invalid.append({'time_s': value, 'tz_offset_min': 0})
        for value in (None, True, False, '480', [], {}, -721, 841, -1440, 1440, 480.5):
            invalid.append({'time_s': STAMP+99, 'tz_offset_min': value})
        for value in invalid:
            with self.subTest(value=value):
                values = self.run_lines([self.status(), self.status(value, ms=2500, seq=3), 'T 3000'])
                self.assertEqual(values[-1][:2], (STAMP+2, 480))
                fresh = self.run_lines([self.status(value)])
                self.assertEqual(fresh[-1][0], 0)

    def test_valid_offsets_and_forward_backward_host_corrections(self):
        for zone in (-720, -210, 0, 345, 480, 765, 840):
            with self.subTest(zone=zone):
                values = self.run_lines([self.status(),
                    self.status({'time_s': STAMP+3600, 'tz_offset_min': zone}, ms=2500, seq=3),
                    self.status({'time_s': STAMP-3600, 'tz_offset_min': zone}, ms=3500, seq=4), 'T 4500'])
                self.assertEqual(values[1][:2], (STAMP+3600, zone))
                self.assertEqual(values[-1][:2], (STAMP-3599, zone))

    def test_uint32_millisecond_wrap_preserves_fractional_seconds(self):
        stamp = 2**32-500
        values = self.run_lines([self.status(ms=stamp), 'T 499', 'T 500', 'T 1500'])
        self.assertEqual([v[0] for v in values], [STAMP, STAMP, STAMP+1, STAMP+2])

    def test_holdover_beyond_full_millisecond_wrap(self):
        # Ticks continue while offline; missing STATUS for 50 days must not
        # rewind the clock when the 32-bit millisecond counter wraps.
        ms = [1000+i*86400000 for i in range(1, 51)]
        values = self.run_lines([self.status(), 'D 1001']+[f'T {t % 2**32}' for t in ms])
        self.assertEqual(values[-1][:2], (STAMP+50*86400, 480))

    def test_unix_uint32_limit_does_not_wrap_to_1970(self):
        values = self.run_lines([self.status({'time_s': 2**32-1, 'tz_offset_min': 0}),
                                'T 1999', 'T 2000', 'T 3000'])
        self.assertEqual([v[0] for v in values], [2**32-1, 2**32-1, 0, 0])

    def test_old_epoch_replay_wrong_session_and_corrupt_frames_are_ignored(self):
        bad = bytearray(Frame(C.STATUS, T.STATUS, 101, 0, 5,
                             b'{"time_s":123,"tz_offset_min":0}').encode())
        bad[-3] ^= 1
        other = {'time_s': STAMP+99, 'tz_offset_min': 0}
        lines = [self.status(), self.status(other, ms=1500, seq=3, epoch=100),
                 self.status(other, ms=2000, seq=2), self.status(other, ms=2500, seq=4, session=5),
                 '3000 '+bad.hex()]
        values = self.run_lines(lines)
        self.assertEqual([v[:2] for v in values],
                         [(STAMP, 480), (STAMP, 480), (STAMP+1, 480), (STAMP+1, 480), (STAMP+2, 480)])


if __name__ == '__main__':
    unittest.main()
