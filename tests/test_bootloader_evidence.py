"""Offline tests: synthetic inputs are parser fixtures, NEVER trust evidence.

Actual-positive tests read the existing audit/binaries without changing them.
If those archived inputs are absent, they explicitly skip; they never generate,
repin or normalize replacement evidence. Run with Python -B to avoid pycache.
"""
from dataclasses import FrozenInstanceError
import hashlib
import json
from pathlib import Path
import struct
import sys
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
from _mixlib import bootloader_evidence as evidence

AUDIT = ROOT / 'build/deploy/ota-acceptance-20260916-1749/bootloader-analysis'
PATHS = {
    'old_bootloader': AUDIT.parent / 'old-bootloader.bin',
    'reference_bootloader': ROOT / 'firmware/esp32s3/build/bootloader/bootloader.bin',
    'reference_config': ROOT / 'firmware/esp32s3/build/bootloader/config/sdkconfig.json',
    'reference_elf': ROOT / 'firmware/esp32s3/build/bootloader/bootloader.elf',
    **{name: AUDIT / name for name in evidence.ARTIFACT_SHA256 if name.endswith('.json')},
}
# Deliberately literal, independent layout values for parser fixtures.
SEGMENTS = ((24, 0x3FCE2810, 5476), (5508, 0x403C8700, 4),
            (5520, 0x403C8704, 3364), (8892, 0x403CB700, 12076))


def sha(data):
    return hashlib.sha256(data).hexdigest()


def reseal(data):
    """In-memory mutation fixture only; no binary or approval is ever written."""
    result = bytearray(data)
    checksum = 0xEF
    for offset, _, size in SEGMENTS:
        for byte in result[offset + 8:offset + 8 + size]:
            checksum ^= byte
    result[20991] = checksum
    result[20992:] = hashlib.sha256(result[:20992]).digest()
    return bytes(result)


def synthetic_image():
    result = bytearray(21024)
    result[:24] = bytes.fromhex('e904023f28893c40ee000000090000000063000000000001')
    for offset, address, size in SEGMENTS:
        struct.pack_into('<II', result, offset, address, size)
    result[0x1FAC:0x1FAE] = b'\x0c\x9c'
    return reseal(result)


def config_bytes(config):
    return json.dumps(config, sort_keys=True).encode()


def synthetic_config():
    # Only a parser fixture, never the pinned real sdkconfig.
    return {
        'IDF_TARGET': 'esp32s3',
        'BOOTLOADER_APP_ROLLBACK_ENABLE': True,
        'BOOTLOADER_APP_ANTI_ROLLBACK': False,
        'BOOTLOADER_OFFSET_IN_FLASH': 0,
        'PARTITION_TABLE_OFFSET': 32768,
        'BOOTLOADER_REGION_PROTECTION_ENABLE': True,
        'BOOTLOADER_APP_TEST': False,
        'BOOTLOADER_FACTORY_RESET': False,
        'BOOTLOADER_SKIP_VALIDATE_ALWAYS': False,
        'BOOTLOADER_SKIP_VALIDATE_IN_DEEP_SLEEP': False,
        'BOOTLOADER_SKIP_VALIDATE_ON_POWER_ON': False,
        'BOOTLOADER_WDT_ENABLE': True,
        'BOOTLOADER_WDT_DISABLE_IN_USER_CODE': True,
        'BOOTLOADER_WDT_TIME_MS': 30000,
        'EFUSE_VIRTUAL': False,
        'SECURE_BOOT': False,
        'SECURE_FLASH_ENC_ENABLED': False,
        'SECURE_SIGNED_APPS_NO_SECURE_BOOT': False,
    }


class StrictImageTests(unittest.TestCase):
    def test_valid_structure_is_not_identity_evidence(self):
        raw = synthetic_image()
        evidence._validate_image(raw)
        artifacts = {name: b'NOT ORIGINAL EVIDENCE' for name in evidence.ARTIFACT_SHA256}
        artifacts['old_bootloader'] = raw
        with self.assertRaisesRegex(evidence.EvidenceRefused, 'SHA256 mismatch: old_bootloader'):
            evidence.validate_reviewed_binary_evidence(artifacts, purpose=evidence.QUALIFICATION_PURPOSE)

    def test_every_header_byte_is_fixed(self):
        for offset in range(24):
            with self.subTest(offset=offset):
                raw = bytearray(synthetic_image())
                raw[offset] ^= 1
                with self.assertRaisesRegex(evidence.EvidenceRefused, 'header'):
                    evidence._validate_image(reseal(raw))

    def test_all_segment_layouts_fixed_even_after_rehash(self):
        for offset, address, size in SEGMENTS:
            for field, value in ((0, 0), (0, 0x60008000), (0, 0x403C8700),
                                 (0, address + 4), (4, 0), (4, size + 4),
                                 (4, 0xFFFFFFFC), (4, size - 1)):
                if field == 0 and value == address:
                    continue
                with self.subTest(segment=offset, field=field, value=value):
                    raw = bytearray(synthetic_image())
                    struct.pack_into('<I', raw, offset + field, value)
                    with self.assertRaisesRegex(evidence.EvidenceRefused, 'segment layout'):
                        evidence._validate_image(reseal(raw))

    def test_truncation_extension_and_flash_padding_refused(self):
        image = synthetic_image()
        for raw in (b'', image[:23], image[:32], image[:20992], image[:-1],
                    image + b'\0', image.ljust(0x8000, b'\xff')):
            with self.subTest(length=len(raw)):
                with self.assertRaisesRegex(evidence.EvidenceRefused, '21024-byte'):
                    evidence._validate_image(raw)

    def test_payload_corruption_detected_by_xor(self):
        raw = bytearray(synthetic_image())
        raw[0x3500] ^= 1
        with self.assertRaisesRegex(evidence.EvidenceRefused, 'XOR checksum'):
            evidence._validate_image(bytes(raw))

    def test_xor_tamper_detected_even_with_recomputed_digest(self):
        raw = bytearray(synthetic_image())
        raw[20991] ^= 1
        raw[20992:] = hashlib.sha256(raw[:20992]).digest()
        with self.assertRaisesRegex(evidence.EvidenceRefused, 'XOR checksum'):
            evidence._validate_image(bytes(raw))

    def test_digest_tamper_detected(self):
        for offset in (20992, 21023):
            with self.subTest(offset=offset):
                raw = bytearray(synthetic_image())
                raw[offset] ^= 1
                with self.assertRaisesRegex(evidence.EvidenceRefused, 'Appended image SHA256'):
                    evidence._validate_image(bytes(raw))

    def test_padding_not_a_mask(self):
        raw = bytearray(synthetic_image())
        raw[20976] = 1
        with self.assertRaisesRegex(evidence.EvidenceRefused, 'padding'):
            evidence._validate_image(reseal(raw))

    def test_mutable_or_nonbyte_images_refused(self):
        for raw in (None, 'image', bytearray(synthetic_image()), memoryview(synthetic_image())):
            with self.subTest(kind=type(raw).__name__):
                with self.assertRaises(evidence.EvidenceRefused):
                    evidence._validate_image(raw)


class ConfigAndScopeTests(unittest.TestCase):
    def test_reference_semantic_fixture_valid_only_as_config(self):
        evidence._validate_reference_config(config_bytes(synthetic_config()))

    def test_every_safety_setting_required_and_typed(self):
        config = synthetic_config()
        for name, original in config.items():
            alternatives = [None]
            if type(original) is not str:
                alternatives.append(str(original))
            if type(original) is bool:
                alternatives += [not original, int(original)]
            elif type(original) is int:
                alternatives += [original + 1, bool(original), float(original)]
            else:
                alternatives += ['esp32', True]
            for alternative in alternatives:
                with self.subTest(name=name, value=alternative):
                    changed = dict(config)
                    changed[name] = alternative
                    with self.assertRaisesRegex(evidence.EvidenceRefused, name):
                        evidence._validate_reference_config(config_bytes(changed))
            with self.subTest(missing=name):
                missing = dict(config)
                del missing[name]
                with self.assertRaisesRegex(evidence.EvidenceRefused, name):
                    evidence._validate_reference_config(config_bytes(missing))

    def test_old_budget_cannot_be_substituted_into_reference_config(self):
        changed = synthetic_config()
        changed['BOOTLOADER_WDT_TIME_MS'] = 9000
        with self.assertRaisesRegex(evidence.EvidenceRefused, 'BOOTLOADER_WDT_TIME_MS'):
            evidence._validate_reference_config(config_bytes(changed))

    def test_ambiguous_or_invalid_json_refused(self):
        for raw in (b'{"BOOTLOADER_WDT_ENABLE":false,"BOOTLOADER_WDT_ENABLE":true}',
                    b'{"x":NaN}', b'{"x":Infinity}', b'[]', b'null', b'{', b'\xff'):
            with self.subTest(raw=raw):
                with self.assertRaises(evidence.EvidenceRefused):
                    evidence._validate_reference_config(raw)

    def test_install_and_arbitrary_purpose_refused_before_artifacts(self):
        for purpose in ('install', 'install-b', 'qualify', 'boot-only', '', True, None):
            with self.subTest(purpose=purpose):
                with self.assertRaisesRegex(evidence.EvidenceRefused, 'separate candidate compatibility gate'):
                    evidence.validate_reviewed_binary_evidence({}, purpose=purpose)

    def test_arbitrary_review_boolean_is_not_evidence(self):
        for artifacts in ({}, {'reviewed': True}, {'approved': True}, None, True):
            with self.subTest(artifacts=artifacts):
                with self.assertRaises(evidence.EvidenceRefused):
                    evidence.validate_reviewed_binary_evidence(artifacts, purpose=evidence.QUALIFICATION_PURPOSE)

    def test_pins_cannot_be_modified_in_place(self):
        with self.assertRaises(TypeError):
            evidence.ARTIFACT_SHA256['old_bootloader'] = '0' * 64


class OriginalPinnedArtifactTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        missing = [str(path) for path in PATHS.values() if not path.is_file()]
        if missing:
            raise unittest.SkipTest('Original local evidence unavailable (not fabricated): ' + ', '.join(missing))
        cls.artifacts = {name: path.read_bytes() for name, path in PATHS.items()}

    def validate(self, files=None):
        return evidence.validate_reviewed_binary_evidence(
            self.artifacts if files is None else files, purpose=evidence.QUALIFICATION_PURPOSE)

    def test_actual_pinned_artifacts_positive_without_issuing_approval(self):
        result = self.validate()
        self.assertEqual(result.old_sha256, evidence.OLD_SHA256)
        self.assertEqual(result.reference_sha256, evidence.REFERENCE_SHA256)
        self.assertEqual(result.reference_config_sha256, evidence.REFERENCE_CONFIG_SHA256)
        self.assertEqual(result.old_nominal_startup_watchdog_ms, 9000)
        self.assertEqual(result.reference_nominal_startup_watchdog_ms, 30000)
        self.assertEqual((result.equal_reviewed_full_functions, result.reviewed_full_functions), (47, 48))
        self.assertEqual(result.artifact_hashes, tuple(sorted(evidence.ARTIFACT_SHA256.items())))
        self.assertEqual(result.old_boot_region_sha256, sha(self.artifacts['old_bootloader'].ljust(32768, b'\xff')))
        self.assertFalse(result.deployment_authorized)
        self.assertFalse(result.historical_config_recovered)
        self.assertFalse(result.whole_behavior_equivalent)
        self.assertFalse(result.candidate_compatibility_proven)
        self.assertEqual(result.use, 'qualify-current-a-no-write')
        self.assertTrue(any('startup timing' in item for item in result.limitations))
        with self.assertRaises(FrozenInstanceError):
            result.deployment_authorized = True

    def test_every_original_artifact_hash_enforced(self):
        for name in self.artifacts:
            with self.subTest(name=name):
                files = dict(self.artifacts)
                changed = bytearray(files[name])
                changed[-1] ^= 1
                files[name] = bytes(changed)
                with self.assertRaisesRegex(evidence.EvidenceRefused, 'SHA256 mismatch: ' + name):
                    self.validate(files)

    def test_each_review_is_mandatory(self):
        for name in evidence.ARTIFACT_SHA256:
            with self.subTest(missing=name):
                files = dict(self.artifacts)
                del files[name]
                with self.assertRaisesRegex(evidence.EvidenceRefused, 'Exact pinned artifact set'):
                    self.validate(files)

    def test_generated_review_assertion_cannot_replace_any_report(self):
        for name in self.artifacts:
            if not name.endswith('.json'):
                continue
            with self.subTest(name=name):
                files = dict(self.artifacts)
                files[name] = config_bytes({'reviewed': True, 'approved': True,
                                            'old_sha256': evidence.OLD_SHA256,
                                            'reference_sha256': evidence.REFERENCE_SHA256})
                with self.assertRaisesRegex(evidence.EvidenceRefused, 'SHA256 mismatch'):
                    self.validate(files)

    def test_review_boolean_or_mask_is_not_an_allowed_parameter(self):
        for name, value in (('reviewed', True), ('approved', True), ('mask_segments', [0, 2])):
            with self.subTest(name=name):
                with self.assertRaisesRegex(evidence.EvidenceRefused, 'Exact pinned artifact set'):
                    self.validate(dict(self.artifacts, **{name: value}))
        with self.assertRaises(TypeError):
            evidence.validate_reviewed_binary_evidence(self.artifacts,
                                                       purpose=evidence.QUALIFICATION_PURPOSE, reviewed=True)

    def test_original_config_format_is_pinned_not_only_semantics(self):
        files = dict(self.artifacts)
        files['reference_config'] += b'\n'
        with self.assertRaisesRegex(evidence.EvidenceRefused, 'SHA256 mismatch: reference_config'):
            self.validate(files)

    def test_binary_roles_cannot_be_swapped_or_normalized(self):
        for old_name, source in (('old_bootloader', 'reference_bootloader'),
                                 ('reference_bootloader', 'old_bootloader')):
            with self.subTest(role=old_name):
                files = dict(self.artifacts)
                files[old_name] = files[source]
                with self.assertRaisesRegex(evidence.EvidenceRefused, 'SHA256 mismatch'):
                    self.validate(files)

    def test_both_original_instructions_checked_even_after_reseal(self):
        old, reference = self.artifacts['old_bootloader'], self.artifacts['reference_bootloader']
        for role in (0, 1):
            for opcode in (b'\x00\x00', b'\x1c\xec' if role == 0 else b'\x0c\x9c'):
                with self.subTest(role=role, opcode=opcode.hex()):
                    pair = [old, reference]
                    changed = bytearray(pair[role])
                    changed[0x1FAC:0x1FAE] = opcode
                    pair[role] = reseal(changed)
                    with self.assertRaisesRegex(evidence.EvidenceRefused, 'instruction at 0x1fac'):
                        evidence._validate_pair(*pair)

    def test_unspecified_payload_differences_refused_after_valid_reseal(self):
        original_pair = [self.artifacts['old_bootloader'], self.artifacts['reference_bootloader']]
        # Descriptor reserved bytes, rodata, all three executable segments,
        # adjacent WDT code, and non-ELF payload alignment bytes: none masked.
        for role in (0, 1):
            for offset in (0x21, 0x60, 0x88, 0x158C, 0x158F, 0x1598,
                           0x1FAE, 0x22BB, 0x22C4, 0x51EF):
                with self.subTest(role=role, offset=hex(offset)):
                    pair = list(original_pair)
                    changed = bytearray(pair[role])
                    changed[offset] ^= 1
                    pair[role] = reseal(changed)
                    evidence._validate_image(pair[role])
                    with self.assertRaisesRegex(evidence.EvidenceRefused, 'byte differences'):
                        evidence._validate_pair(*pair)

    def test_allowlisted_date_byte_still_requires_original_value(self):
        old, reference = self.artifacts['old_bootloader'], self.artifacts['reference_bootloader']
        for role in (0, 1):
            pair = [old, reference]
            changed = bytearray(pair[role])
            changed[0x4D] = ord('9')
            pair[role] = reseal(changed)
            with self.assertRaises(evidence.EvidenceRefused):
                evidence._validate_pair(*pair)

    def test_same_tamper_in_both_images_cannot_hide_behind_pair_comparison(self):
        files = dict(self.artifacts)
        for name in ('old_bootloader', 'reference_bootloader'):
            changed = bytearray(files[name])
            changed[0x3500] ^= 1
            files[name] = reseal(changed)
            evidence._validate_image(files[name])
        with self.assertRaisesRegex(evidence.EvidenceRefused, 'SHA256 mismatch'):
            self.validate(files)

    def test_both_images_require_checksums_and_layout(self):
        original_pair = [self.artifacts['old_bootloader'], self.artifacts['reference_bootloader']]
        for role in (0, 1):
            for offset, expected in ((0x18, 'segment layout'), (0x51F0, 'padding'),
                                     (0x51FF, 'XOR checksum'), (0x5200, 'Appended image SHA256')):
                with self.subTest(role=role, offset=hex(offset)):
                    pair = list(original_pair)
                    changed = bytearray(pair[role])
                    changed[offset] ^= 1
                    pair[role] = bytes(changed)
                    with self.assertRaisesRegex(evidence.EvidenceRefused, expected):
                        evidence._validate_pair(*pair)

    def test_nonbyte_empty_and_oversized_artifacts_refused(self):
        for value in (None, True, bytearray(self.artifacts['old_bootloader']), b'', b'x' * (2 * 1024 * 1024 + 1)):
            with self.subTest(kind=type(value).__name__):
                files = dict(self.artifacts)
                files['old_bootloader'] = value
                with self.assertRaisesRegex(evidence.EvidenceRefused, 'Original bounded artifact bytes'):
                    self.validate(files)

    def test_report_function_assertions_checked_against_actual_bytes(self):
        report = json.loads(self.artifacts['bounded-recheck.json'])
        old, reference = self.artifacts['old_bootloader'], self.artifacts['reference_bootloader']
        report['full_functions_rechecked'][0]['old_sha256'] = '0' * 64
        with self.assertRaisesRegex(evidence.EvidenceRefused, 'function bytes mismatch'):
            evidence._recheck_functions(report, old, reference)

    def test_validation_has_no_file_or_device_effects_and_preserves_inputs(self):
        snapshot = dict(self.artifacts)
        with mock.patch('builtins.open', side_effect=AssertionError('Unexpected file access')):
            result = self.validate()
        self.assertFalse(result.deployment_authorized)
        self.assertEqual(self.artifacts, snapshot)
        for name, path in PATHS.items():
            with self.subTest(name=name):
                self.assertEqual(sha(path.read_bytes()), evidence.ARTIFACT_SHA256[name])

    def test_real_artifacts_still_cannot_authorize_b_install(self):
        with self.assertRaisesRegex(evidence.EvidenceRefused, 'separate candidate compatibility gate'):
            evidence.validate_reviewed_binary_evidence(self.artifacts, purpose='install')


if __name__ == '__main__':
    unittest.main()
