"""Python USB frames through production C link and the vendored cJSON parser."""
import json
import subprocess
import unittest

from _support import ROOT, host_command, host_run, posix_path, require_host_cc
from test_link_update import HEADERS
from protocol import Frame, Channel as C, Type as T

OUT = ROOT / 'build/host-wifi-rate'
JSON_DIR = ROOT / '.tools/esp-idf-clean/components/json/cJSON'
MAIN = ROOT / 'firmware/esp32s3/main'


class WifiRateWireTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cc = require_host_cc()
        if not (JSON_DIR / 'cJSON.c').is_file():
            raise unittest.SkipTest('vendored cJSON source unavailable; real JSON parser required')
        OUT.mkdir(parents=True, exist_ok=True)
        for name, contents in HEADERS.items():
            if name == 'cJSON.h':
                contents = (JSON_DIR / name).read_text(encoding='utf-8')
            path = OUT / 'stubs' / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(contents, encoding='utf-8')
        # Reuse the queue/SDK fixture while replacing only its no-op JSON API.
        fixture = (ROOT / 'tests/test_link_update_host.c').read_text(encoding='utf-8')
        begin = fixture.index('cJSON *cJSON_ParseWithLength(')
        end = fixture.index('\n\n/* A model of mix_ota.c:', begin)
        fixture = fixture[:begin] + fixture[end:]
        fixture = fixture.replace('../firmware/esp32s3/main/mix_link.c',
                                  posix_path(MAIN / 'mix_link.c'))
        (OUT / 'link_fixture.h').write_text(fixture, encoding='utf-8')
        cls.exe = OUT / 'wifi_rate_harness'
        host_run([cc, '-std=c11', '-Wall', '-Wextra', '-Werror', '-Wno-misleading-indentation',
                  '-g', '-O1', '-fsanitize=address,undefined', '-fno-omit-frame-pointer', '-no-pie',
                  '-I'+posix_path(OUT / 'stubs'), '-I'+posix_path(OUT),
                  posix_path(ROOT / 'tests/test_wifi_rate_wire_host.c'),
                  posix_path(MAIN / 'mix_protocol.c'), posix_path(MAIN / 'mix_terminal.c'),
                  posix_path(JSON_DIR / 'cJSON.c'), '-lm', '-o', posix_path(cls.exe)])

    def run_wire(self, values, tail=()):
        lines = []
        for seq, value in enumerate(values, 2):
            payload = value if isinstance(value, bytes) else json.dumps(value, separators=(',', ':')).encode()
            wire = Frame(C.STATUS, T.STATUS, 101, 0, seq, payload).encode()
            lines.append(f'{1000+(seq-2)*2} {wire.hex()}')
        lines.extend(tail)
        result = subprocess.run(host_command([posix_path(self.exe)]), input='\n'.join(lines)+'\n',
                                capture_output=True, text=True, encoding='utf-8', timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout+result.stderr)
        output = result.stdout.splitlines()
        self.assertEqual(output[0], 'READY 101')
        parsed = []
        for line in output[1:]:
            parts = line.split(' ')
            parsed.append((bool(int(parts[0])), bool(int(parts[1])), bool(int(parts[2])),
                           float(parts[3]), int(parts[4]), bytes.fromhex(parts[5]).decode('utf-8')))
        return parsed

    @staticmethod
    def connected(rate=1234.5):
        return {'wifi': {'connected': True, 'ssid': 'lab', 'signal': 71, 'rx_bps': rate}}

    def test_valid_idle_missing_disconnected_and_unknown(self):
        values = [self.connected(), self.connected(0),
                  {'wifi': {'connected': True, 'ssid': 'lab', 'signal': 71}},
                  {'wifi': {'connected': False, 'rx_bps': 999}}, {}, {'wifi': None}]
        answers = self.run_wire(values)
        self.assertEqual(answers[0], (True, True, True, 1234.5, 71, 'lab'))
        self.assertEqual(answers[1][2:4], (True, 0))
        self.assertEqual(answers[2][0:4], (True, True, False, -1))
        self.assertEqual(answers[3][0:4], (True, False, False, -1))
        self.assertEqual(answers[4][0:4], (False, False, False, -1))
        self.assertEqual(answers[5][0:4], (False, False, False, -1))

    def test_invalid_json_types_negative_nonfinite_and_float_overflow_clear_rate(self):
        invalid = [None, True, False, '1200', [], {}, -1, 1e39, 1e300]
        for value in invalid:
            with self.subTest(rate=value):
                answers = self.run_wire([self.connected(), self.connected(value)])
                self.assertTrue(answers[0][2])
                self.assertEqual(answers[1][2:4], (False, -1))
        for payload in (b'{"wifi":{"connected":true,"rx_bps":1e999}}', b'{broken'):
            self.assertEqual(self.run_wire([self.connected(), payload])[-1][2:4], (False, -1))

    def test_stale_rate_expires_even_with_live_heartbeats_and_disconnect(self):
        result = self.run_wire([self.connected()], tail=('T 6999', 'T 7000'))
        self.assertTrue(result[1][2])
        self.assertEqual(result[2][2:4], (False, -1))
        result = self.run_wire([self.connected()], tail=('D 1001', 'R 1002'))
        self.assertEqual(result[1][0:4], (False, False, False, -1))
        self.assertEqual(result[2][0:4], (False, False, False, -1))

    def test_wire_routing_epoch_sequence_crc_and_fragmentation(self):
        valid = Frame(C.STATUS, T.STATUS, 101, 0, 2,
                      json.dumps(self.connected(), separators=(',', ':')).encode()).encode()
        ignored = [Frame(C.STATUS, T.STATUS, 100, 0, 3, b'{}').encode(),
                   Frame(C.STATUS, T.STATUS, 101, 0, 2, b'{}').encode(),
                   Frame(C.STATUS, T.STATUS, 101, 5, 4, b'{}').encode(),
                   Frame(C.NET, T.STATUS, 101, 0, 5, b'{}').encode()]
        corrupt = bytearray(Frame(C.STATUS, T.STATUS, 101, 0, 6, b'{}').encode())
        corrupt[-3] ^= 1
        lines = ['1000 '+valid[:9].hex(), '1001 '+valid[9:].hex()]
        lines.extend(f'{1002+i} {frame.hex()}' for i, frame in enumerate(ignored+[bytes(corrupt)]))
        result = subprocess.run(host_command([posix_path(self.exe)]), input='\n'.join(lines)+'\n',
                                capture_output=True, text=True, encoding='utf-8', timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout+result.stderr)
        output = result.stdout.splitlines()[1:]
        self.assertEqual(output[0].split()[2], '0')
        self.assertTrue(all(line.split()[2:4] == ['1', '1234.5'] for line in output[1:]), output)

    def test_status_age_expiry_across_uint32_wrap(self):
        payload = json.dumps(self.connected(), separators=(',', ':')).encode()
        wire = Frame(C.STATUS, T.STATUS, 101, 0, 2, payload).encode()
        stamp = 2**32-3000
        lines = [f'{stamp} {wire.hex()}', 'T 2999', 'T 3000']
        result = subprocess.run(host_command([posix_path(self.exe)]), input='\n'.join(lines)+'\n',
                                capture_output=True, text=True, encoding='utf-8', timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout+result.stderr)
        output = result.stdout.splitlines()[1:]
        self.assertEqual([line.split()[2] for line in output], ['1', '1', '0'])

    def test_json_escaped_maximum_ssid_is_decoded_by_real_parser(self):
        for ssid in ('"\\'*16, '网'*10, '😀'*8, '\x01'*32):
            value = self.connected()
            value['wifi']['ssid'] = ssid
            result = self.run_wire([value])[0]
            self.assertEqual(result[-1], ssid)
            self.assertEqual(result[2:4], (True, 1234.5))


if __name__ == '__main__':
    unittest.main()
