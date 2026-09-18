"""Pin the real recovery digest across C/Python/JSON and the production worker.

The old C RELEASE_BASELINE fixture copied MIX_BASELINE_SHA, so an incorrect
production array matched an equally incorrect request. These requests and
expected values come from the independently verified recovery digest instead.
No device, release candidate, archive or recovery evidence is modified.
"""
import ast
import json
import re
import unittest

from _support import ROOT, host_run, posix_path
import test_ota_firmware as firmware_tests

APP_SHA = '7875d9a513acb95463b72e785ebd160c70d03f85e965c3a30a93d954bb4cff5f'
ELF_SHA = 'cfacb3fe25931e918b8f46d1f840da8d57ba39ef799bd0af4629939b9185761a'


class BaselineBindingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.header = (ROOT / 'firmware/esp32s3/main/mix_ota_baseline.h').read_text(encoding='utf-8')
        cls.native = ast.parse((ROOT / 'tools/mixos_esp_update.py').read_text(encoding='utf-8'))
        cls.baseline = json.loads((ROOT / 'docs/esp32-recovery-baseline.json').read_text(encoding='utf-8'))

    def native_literal(self, name):
        # Inspect only literal constants; importing the updater is unnecessary.
        values = [node.value for node in self.native.body if isinstance(node, ast.Assign)
                  and any(isinstance(target, ast.Name) and target.id == name
                          for target in node.targets)]
        self.assertEqual(len(values), 1, name)
        return ast.literal_eval(values[0])

    def header_string(self, name):
        values = re.findall(r'^#define\s+' + re.escape(name) + r'\s+"([0-9a-f]{64})"\s*$',
                            self.header, re.MULTILINE)
        self.assertEqual(len(values), 1, name)
        return values[0]

    def header_uint(self, name):
        values = re.findall(r'^#define\s+' + re.escape(name) + r'\s+(\d+)u\s*$',
                            self.header, re.MULTILINE)
        self.assertEqual(len(values), 1, name)
        return int(values[0])

    def test_image_sha_header_hex_array_native_and_recovery_json(self):
        arrays = re.findall(r'MIX_BASELINE_SHA\[32\]\s*=\s*\{([^}]+)\}', self.header)
        self.assertEqual(len(arrays), 1)
        array = bytes(int(token.strip(), 0) for token in arrays[0].split(',') if token.strip())
        self.assertEqual(len(array), 32)
        for source, value in (
                ('header HEX', self.header_string('MIX_BASELINE_SHA_HEX')),
                ('header byte array', array.hex()),
                ('native BASELINE_SHA', self.native_literal('BASELINE_SHA')),
                ('recovery JSON', self.baseline['image_sha256'])):
            with self.subTest(source=source):
                self.assertEqual(value, APP_SHA)

    def test_elf_digest_header_native_and_recovery_json(self):
        for source, value in (
                ('header ELF', self.header_string('MIX_BASELINE_ELF_HEX')),
                ('native BASELINE_ELF', self.native_literal('BASELINE_ELF')),
                ('recovery JSON', self.baseline['elf_sha256'])):
            with self.subTest(source=source):
                self.assertEqual(value, ELF_SHA)

    def test_image_size_and_protected_slot_remain_pinned(self):
        self.assertEqual(self.header_uint('MIX_BASELINE_BYTES'), 894560)
        self.assertEqual(self.native_literal('BASELINE_SIZE'), 894560)
        self.assertEqual(self.baseline['image_bytes'], 894560)
        self.assertEqual(self.header_uint('MIX_BASELINE_SLOT'), 0)
        self.assertEqual(self.baseline['slot'], 'ota_0')
        self.assertEqual(self.baseline['state'], 'valid')


class BaselineReleaseFirmwareTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Compile the actual C receiver/worker with SDK stubs and real SHA-256.
        firmware_tests.OtaFirmwareTests.setUpClass()

    def scenario(self, name):
        output = host_run([posix_path(firmware_tests.OtaFirmwareTests.exe), name])
        self.assertIn('PASS ' + name, output)

    def test_independent_real_digest_releases_durably_without_flash_writes(self):
        self.scenario('release-known-baseline')

    def test_historical_wrong_array_digest_is_refused_without_flash_writes(self):
        self.scenario('release-rejects-obsolete-digest')

    def test_real_digest_still_requires_verified_replacement(self):
        self.scenario('release-requires-measurement')


if __name__ == '__main__':
    unittest.main()
