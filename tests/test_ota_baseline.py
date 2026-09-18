"""Verify preserved local recovery evidence without opening any device."""
import hashlib
import json
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
APP_SHA = '7875d9a513acb95463b72e785ebd160c70d03f85e965c3a30a93d954bb4cff5f'
ELF_SHA = 'cfacb3fe25931e918b8f46d1f840da8d57ba39ef799bd0af4629939b9185761a'
BACKUP_SHA = 'fa7eacf1b20d193443d717620487eee2aa52f71e8943f49eb69590f6d4814886'
READBACK_SHA = 'df9c108f6248f2cfde22f097187beaeef5d76dbfd34a173140671c164939a73e'


class RecoveryEvidenceTests(unittest.TestCase):
    def local(self, relative):
        path = ROOT / relative
        if not path.is_file():
            self.skipTest('local recovery artifact not available: ' + relative)
        return path.read_bytes()

    def test_recovery_app_and_metadata_are_still_pinned(self):
        baseline = json.loads(self.local('docs/esp32-recovery-baseline.json'))
        app = self.local('build/esp32s3/current-device-app.bin')
        self.assertEqual(len(app), 894560)
        self.assertEqual(hashlib.sha256(app).hexdigest(), APP_SHA)
        self.assertEqual(baseline['image_sha256'], APP_SHA)
        self.assertEqual(baseline['elf_sha256'], ELF_SHA)
        self.assertEqual(baseline['backup_sha256'], BACKUP_SHA)
        self.assertEqual(baseline['readback_sha256'], READBACK_SHA)
        self.assertEqual(baseline['slot'], 'ota_0')
        self.assertEqual(baseline['state'], 'valid')

    def test_full_flash_evidence_and_preserved_regions(self):
        original = self.local('build/deploy/recovery-20260916-024325-original-8MB.bin')
        readback = self.local('build/deploy/recovery-20260916-024325-readback-8MB.bin')
        self.assertEqual(len(original), 0x800000)
        self.assertEqual(len(readback), 0x800000)
        self.assertEqual(hashlib.sha256(original).hexdigest(), BACKUP_SHA)
        self.assertEqual(hashlib.sha256(readback).hexdigest(), READBACK_SHA)
        self.assertEqual(readback[0x10000:0x10000+894560],
                         self.local('build/esp32s3/current-device-app.bin'))
        self.assertEqual(readback[:0x10000], original[:0x10000])
        self.assertEqual(readback[0x200000:], original[0x200000:])

    def test_regenerated_font_still_matches_recovered_partition(self):
        font = self.local('build/font/MiSans-Normal-gb2312.ttf')
        readback = self.local('build/deploy/recovery-20260916-024325-readback-8MB.bin')
        manifest = json.loads(self.local('build/font/MiSans-Normal-gb2312.ttf.manifest.json'))
        self.assertEqual(font, readback[0x210000:0x210000+len(font)])
        self.assertEqual(manifest['output_sha256'], hashlib.sha256(font).hexdigest())
        ui = self.local('firmware/esp32s3/main/mix_ui.c')
        self.assertEqual(manifest['ui_source_sha256'], hashlib.sha256(ui).hexdigest())

    def test_historical_boot_evidence_is_not_relabelled_as_candidate(self):
        evidence = json.loads(self.local('build/deploy/recovery-20260916-024325-boot-verification.json'))
        self.assertEqual(evidence['running']['elf_sha256'], ELF_SHA)
        self.assertEqual(evidence['running']['slot'], 'ota_0')
        self.assertEqual(evidence['running']['state'], 'valid')
        self.assertFalse(evidence['flash_programming'])
        self.assertEqual(evidence['device']['serial'], 'TD0720')


if __name__ == '__main__':
    unittest.main()
