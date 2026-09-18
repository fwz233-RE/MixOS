"""Offline tests for the native v2 release/job API. No serial, SSH or systemd."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'tools'), str(ROOT / 'linux')]
import mixos_esp_update as native
import ota_esp
import ota_v2
from protocol import Type


def make_image(size=4096):
    data = bytearray((i * 11 + 3) & 0xff for i in range(size))
    data[:4] = b'\xe9\x01\0\0'
    data[12:14] = (9).to_bytes(2, 'little')
    struct = __import__('struct')
    struct.pack_into('<I', data, ota_esp.APP_DESC_OFFSET, ota_esp.APP_DESC_MAGIC)
    data[ota_esp.APP_DESC_OFFSET + ota_esp.DESC_ELF_SHA:
         ota_esp.APP_DESC_OFFSET + ota_esp.DESC_ELF_SHA + 32] = bytes(range(32))
    return bytes(data)


def manifest(data):
    return {'schema': native.SCHEMA, 'protocol': 2,
            'chip': {'name': 'esp32s3', 'id': 9},
            'app': {'file': 'app.bin', 'size': len(data),
                    'sha256': hashlib.sha256(data).hexdigest(),
                    'elf_sha256': bytes(range(32)).hex()},
            'layout': native.LAYOUT,
            'effective_config': {'CONFIG_BOOTLOADER_APP_ROLLBACK_ENABLE': True,
                                 'CONFIG_LCD_RGB_ISR_IRAM_SAFE': False,
                                 'CONFIG_GDMA_ISR_IRAM_SAFE': False,
                                 'CONFIG_SPIRAM_XIP_FROM_PSRAM': True,
                                 'CONFIG_SPIRAM_FETCH_INSTRUCTIONS': True,
                                 'CONFIG_SPIRAM_RODATA': True,
                                 'CONFIG_ESP_SYSTEM_PANIC_PRINT_REBOOT': True,
                                 'CONFIG_ESP_SYSTEM_PANIC_PRINT_HALT': False,
                                 'CONFIG_ESP_TASK_WDT_EN': True,
                                 'CONFIG_ESP_TASK_WDT_INIT': True,
                                 'CONFIG_ESP_TASK_WDT_PANIC': True,
                                 'CONFIG_BOOTLOADER_WDT_ENABLE': True,
                                 'CONFIG_BOOTLOADER_WDT_DISABLE_IN_USER_CODE': True},
            'provenance': {'mode': 'test', 'build_report_sha256': '1' * 64,
                           'sdkconfig_sha256': '2' * 64,
                           'partition_table_sha256': '3' * 64}}


class ReleaseTests(unittest.TestCase):
    def test_release_requires_full_hash_binding_and_elf_identity(self):
        with tempfile.TemporaryDirectory() as d:
            package = Path(d)
            data = make_image()
            (package / 'app.bin').write_bytes(data)
            value = manifest(data)
            (package / 'manifest.json').write_text(json.dumps(value))
            release = native.load_release(package)
            self.assertEqual(release.manifest['app']['sha256'], hashlib.sha256(data).hexdigest())
            value['app']['sha256'] = 'abcd'
            (package / 'manifest.json').write_text(json.dumps(value))
            with self.assertRaises(native.JobError):
                native.load_release(package)

    def test_dry_run_does_not_manage_service_or_device(self):
        with tempfile.TemporaryDirectory() as d:
            package = Path(d)
            data = make_image()
            (package / 'app.bin').write_bytes(data)
            (package / 'manifest.json').write_text(json.dumps(manifest(data)))
            with mock.patch.object(native, 'launch_job') as launch:
                self.assertEqual(native.main(['apply', '--package', str(package),
                                              '--device', '/dev/serial/by-id/usb-TypixDeck_TypixDeck_UAC+CDC_TD0720-if03',
                                              '--state-root', str(package / 'state'), '--lock-root', str(package / 'locks'),
                                              '--dry-run']), 0)
                launch.assert_not_called()


class ProtocolV2Tests(unittest.TestCase):
    def test_new_wire_ids_are_exposed_without_changing_existing_ids(self):
        self.assertEqual(Type.CAPS_QUERY, 77)
        self.assertEqual(Type.CAPS, 78)
        self.assertEqual(Type.OTA_REQUEST, 79)
        self.assertEqual(Type.LOG, 80)
        self.assertEqual(Type.OTA_RESPONSE, 81)

    def test_request_is_exactly_72_bytes(self):
        binding = ota_v2.Binding(bytes(range(16)), bytes(range(32)), 123, 1)
        payload = ota_v2.request_payload(ota_v2.Op.BEGIN, 42, binding, 7)
        self.assertEqual(len(payload), 72)
        self.assertEqual(payload[0:2], b'\x02\x01')
        self.assertEqual(int.from_bytes(payload[60:64], 'little'), 7)

    def test_response_preserves_unknown_result_and_full_fields(self):
        import struct
        data = bytearray(192)
        data[:4] = bytes((2, 3, 5, 5))
        struct.pack_into('<I', data, 4, 9)
        data[8:24] = bytes(range(16))
        data[24:56] = bytes(range(32))
        struct.pack_into('<III', data, 56, 10, 10, 12)
        data[68:72] = bytes((1, 1, 255, 255))
        struct.pack_into('<iI', data, 72, -44, 7)
        data[80:112] = bytes(range(32))
        data[112:144] = bytes(range(32, 64))
        data[144:151] = b'pending'
        result = ota_v2.Response.decode(data)
        self.assertEqual(result.result, ota_v2.Result.ERROR)
        self.assertEqual(result.error, -44)
        self.assertEqual(result.message, 'pending')
        self.assertEqual(result.state, 255)

    def test_caps_requires_all_safety_features(self):
        import struct
        data = bytearray(36)
        data[:2] = b'\x02\x05'
        struct.pack_into('<HH', data, 2, 508, 8)
        struct.pack_into('<I', data, 8, 7)
        struct.pack_into('<IIIII', data, 16, 0x10000, native.SLOT_SIZE,
                         0x610000, native.SLOT_SIZE, 0)
        caps = ota_v2.Capabilities.decode(data)
        with self.assertRaises(ota_v2.OutcomeError):
            caps.require(native.LAYOUT)


class SafetyTests(unittest.TestCase):
    def test_result_exit_requires_firmware_service_and_durable_journal(self):
        base = {'state': 'complete', 'durable': True,
                'firmware': {'state': 'confirmed'},
                'service': {'state': 'restored'}, 'error': None}
        self.assertEqual(native.result_exit_code(base), 0)
        for key, value in (('durable', False), ('firmware', {'state': 'pending'}),
                           ('service', {'state': 'restore-failed'}),
                           ('error', {'code': 'unknown'})):
            item = dict(base)
            item[key] = value
            self.assertNotEqual(native.result_exit_code(item), 0)

    def test_service_restore_requires_observed_original_state(self):
        class Fake:
            def __init__(self):
                self.actions = []
            def command(self, action):
                self.actions.append(action)
                if action == 'show':
                    return 'LoadState=loaded\nActiveState=inactive\nSubState=dead\nMainPID=0\nControlPID=0\n'
                return ''
        service = native.SystemdService(run=lambda *a, **k: type('R', (), {'returncode': 0, 'stdout': 'LoadState=loaded\nActiveState=inactive\nSubState=dead\nMainPID=0\nControlPID=0\n', 'stderr': ''})())
        self.assertEqual(service.restore('inactive')['state'], 'restored')

    def test_persistent_key_is_stable_across_reenumeration(self):
        one = '/dev/serial/by-id/usb-TypixDeck_TypixDeck_UAC+CDC_TD0720-if03'
        self.assertEqual(native.device_key(one), native.device_key(one))
        with self.assertRaises(ValueError):
            native.device_key('/dev/ttyACM0')


if __name__ == '__main__':
    unittest.main()
