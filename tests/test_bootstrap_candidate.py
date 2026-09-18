"""Pure parser fixtures are NOT build evidence or approvals.

Synthetic tests mock only the old-loader identity check (tested independently);
all candidate bytes, receipts, ELF symbols, descriptor ranges and hashes undergo
real parsing. Archived tests read originals only and never repair stale inputs.
Run: py -3.12 -B -m unittest discover -s tests -p test_bootstrap_candidate.py -v
"""
from dataclasses import FrozenInstanceError, asdict
import hashlib
import json
from pathlib import Path
import struct
import sys
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
from _mixlib import bootstrap_candidate as c
from update_esp import AB_LAYOUT, encode_partition_binary

OLD = ROOT / 'build/deploy/ota-acceptance-20260916-1749/old-bootloader.bin'
BUILD = ROOT / 'build/candidates/early-rtc-20260917'
REPORTS = ROOT / 'build/candidate-reports/early-rtc-20260917'
RELEASE = ROOT / 'build/releases/esp32-ota-early-rtc-20260917'


def sha(data):
    return hashlib.sha256(data).hexdigest()


def encoded(value):
    return json.dumps(value, sort_keys=True).encode()


def record(name, data):
    return dict(path='original-build/' + name, bytes=len(data), sha256=sha(data))


def reseal(image):
    image = bytearray(image)
    offset, checksum = 24, 0xef
    for _ in range(image[1]):
        _, size = struct.unpack_from('<II', image, offset)
        offset += 8
        for byte in image[offset:offset + size]:
            checksum ^= byte
        offset += size
    at = offset | 15
    image[at] = checksum
    image[at + 1:] = hashlib.sha256(image[:at + 1]).digest()
    return bytes(image)


def binary_fixture():
    """Minimal linked Xtensa ELF + ESP image, no compiler/files/device needed."""
    function_names = [c.EARLY_HOOK] + sorted(c.REQUIRED_FUNCTIONS - {c.EARLY_HOOK})
    functions = {name: (0x40374000 + i * 16, 16) for i, name in enumerate(function_names)}
    drom = bytearray(1024)
    struct.pack_into('<I', drom, 0, 0xabcd5432)
    drom[48:80] = b'mixos_esp32s3'.ljust(32, b'\0')
    drom[180] = 16
    struct.pack_into('<IHH', drom, 256, 0x40374000, 1, 0)
    code = bytes(range(128))
    symbols = [(name, address, size, 0x12, 2) for name, (address, size) in functions.items()]
    symbols += [('_esp_system_init_fn_array_start', 0x3c000120, 0, 0x10, 1),
                ('_esp_system_init_fn_array_end', 0x3c000128, 0, 0x10, 1)]
    names, entries = b'\0', bytes(16)
    for name, address, size, info, section in symbols:
        entries += struct.pack('<IIIBBH', len(names), address, size, info, 0, section)
        names += name.encode() + b'\0'
    string_at = 0x600
    symbols_at = (string_at + len(names) + 3) & ~3
    sections_at = symbols_at + len(entries)
    elf = bytearray(sections_at + 5 * 40)
    elf[:16] = b'\x7fELF\x01\x01\x01' + bytes(9)
    struct.pack_into('<HHIIIIIHHHHHH', elf, 16, 2, 94, 1, 0x40374000,
                     0, sections_at, 0, 52, 0, 0, 40, 5, 0)
    elf[0x100:0x500] = drom
    elf[0x500:0x580] = code
    elf[string_at:string_at + len(names)] = names
    elf[symbols_at:symbols_at + len(entries)] = entries
    sections = [(0, 0, 0, 0, 0, 0, 0, 0, 0, 0),
                (0, 1, 2, 0x3c000020, 0x100, 1024, 0, 0, 4, 0),
                (0, 1, 6, 0x40374000, 0x500, 128, 0, 0, 4, 0),
                (0, 3, 0, 0, string_at, len(names), 0, 0, 1, 0),
                (0, 2, 0, 0, symbols_at, len(entries), 3, 1, 4, 16)]
    for i, values in enumerate(sections):
        struct.pack_into('<10I', elf, sections_at + i * 40, *values)
    elf = bytes(elf)
    drom[144:176] = hashlib.sha256(elf).digest()
    header = bytearray(24)
    header[:4] = bytes([0xe9, 2, 2, 0x3f])
    struct.pack_into('<I', header, 4, 0x40374000)
    struct.pack_into('<H', header, 12, 9)
    header[23] = 1
    image = bytes(header) + struct.pack('<II', 0x3c000020, 1024) + drom
    image += struct.pack('<II', 0x40374000, len(code)) + code
    image = reseal(image + bytes((len(image) | 15) - len(image) + 33))
    ranges = [dict(name=name, address=hex(address), size=size, binary_offset=1064 + i * 16,
                   elf_offset=0x500 + i * 16, bin_elf_exact=True, sha256=sha(code[i * 16:i * 16 + size]))
              for i, (name, (address, size)) in enumerate(functions.items())]
    array = dict(address='0x3c000120', size=8, binary_offset=288,
                 elf_offset=512, bin_elf_exact=True, sha256=sha(drom[256:264]))
    descriptor = dict(index=0, address='0x3c000120', callback=c.EARLY_HOOK,
                      fn='0x40374000', cores=1, stage=0)
    return image, elf, ranges, array, descriptor


def fixture():
    image, elf, functions, array, descriptor = binary_fixture()
    entries = encode_partition_binary(AB_LAYOUT)[:len(AB_LAYOUT) * 32]
    table = (entries + b'\xeb\xeb' + b'\xff' * 14 + hashlib.md5(entries).digest()).ljust(0xc00, b'\xff')
    config = dict(c.CONFIG_POLICY)
    sdk = '\n'.join('CONFIG_' + k + '=' + ('y' if v is True else 'n' if v is False else json.dumps(v))
                    for k, v in config.items()).encode()
    files = dict(candidate_bin=image, candidate_elf=elf, generated_config=encoded(config),
                 old_bootloader=b'SYNTHETIC OLD IDENTITY MOCK ONLY', partition_table=table)
    for name in c.BUILD_INPUTS:
        files['build_input/' + name] = sdk if name == 'sdkconfig' else ('fixture ' + name).encode()
    for name in ('main.c', 'mix_health.c', 'mix_health.h', 'mix_ota.c', 'mix_ota.h',
                 'mix_ota_tx.c', 'mix_ota_tx.h', 'CMakeLists.txt', 'idf_component.yml'):
        files['source/' + name] = ('fixture source ' + name).encode()
    report = dict(status='cross-built', target='esp32s3', ota_capable=True, partition_layout='ab',
                  app_partition='ota_0', effective_config={'CONFIG_' + k: v for k, v in config.items()},
                  sources=[record(k, v) for k, v in files.items() if k.startswith('source/')],
                  build_inputs=[record(k, v) for k, v in files.items() if k.startswith('build_input/')],
                  sdkconfig=record('sdkconfig', sdk))
    for key, name in {'build_app': 'candidate_bin', 'elf': 'candidate_elf',
                      'partition_table': 'partition_table', 'sdkconfig_generated': 'generated_config'}.items():
        report[key] = record(name, files[name])
    files['build_receipt'] = encoded(dict(schema='mixos-local-build/v1',
        inputs={k: report[k] for k in ('sources', 'build_inputs')},
        artifacts={k: report[k] for k in ('build_app', 'elf', 'partition_table', 'sdkconfig_generated')}))
    report['build_attestation'] = record('completed-build.json', files['build_receipt'])
    files['build_report'] = encoded(report)
    manifest = dict(schema='mixos-esp-release/v2', protocol=2, chip=dict(name='esp32s3', id=9),
        layout=c.LAYOUT, app=dict(file='app.bin', size=len(image), sha256=sha(image), elf_sha256=sha(elf)),
        effective_config=dict(c.REQUIRED_CONFIG),
        provenance=dict(mode='exact-source-and-effective-build-configuration',
                        build_report_sha256=sha(files['build_report']), sdkconfig_sha256=sha(sdk),
                        partition_table_sha256=sha(table), elf_file_sha256=sha(elf)),
        protected_baseline=dict(slot='ota_0', image_sha256=c.policy.BASELINE_SHA256,
                                elf_sha256=c.policy.BASELINE_ELF, image_bytes=c.policy.BASELINE_BYTES))
    files['manifest'] = encoded(manifest)
    files['link_report'] = encoded(dict(schema='mixos-candidate-early-rtc-link-audit/v1',
        candidate=manifest['app'], elf_sha256=sha(elf), build_provenance=manifest['provenance'],
        functions=functions, init_array=array, descriptors=[descriptor], early_hook=descriptor,
        old_bootloader_nominal_watchdog_ms=9000, app_config_nominal_watchdog_ms=30000,
        deployment_authorized=False, device_access=False, pre_takeover_timing_proven=False))
    return files


class CandidateTests(unittest.TestCase):
    def setUp(self):
        self.files = fixture()
        self.pins = {k: sha(v) for k, v in self.files.items()}
        patch = mock.patch.object(c, '_validate_old')
        self.old_mock = patch.start()
        self.addCleanup(patch.stop)

    def validate(self, **kwargs):
        return c.validate_candidate(self.files, expected_sha256=self.pins, target_slot='ota_1', **kwargs)

    def replace_json(self, name, mutate):
        value = json.loads(self.files[name])
        mutate(value)
        self.files[name] = encoded(value)
        self.pins[name] = sha(self.files[name])

    def test_static_result_is_immutable_deterministic_and_not_authority(self):
        result = self.validate()
        self.assertEqual(result, self.validate())
        self.assertEqual(len(result.binding_sha256), 64)
        reverse = c.validate_candidate(dict(reversed(list(self.files.items()))),
                    expected_sha256=dict(reversed(list(self.pins.items()))), target_slot='ota_1')
        self.assertEqual(result.binding_sha256, reverse.binding_sha256)
        self.assertEqual((result.old_nominal_watchdog_ms, result.app_config_nominal_watchdog_ms), (9000, 30000))
        self.assertFalse(result.deployment_authorized)
        self.assertFalse(result.historical_config_recovered)
        self.assertFalse(result.confirmation_contract_verified)
        self.assertIsNone(result.confirmation_contract_sha256)
        self.assertNotIn('approved', asdict(result))
        self.assertIn('candidate_boot_and_no_button_recovery_on_existing_hardware', result.unproven)
        with self.assertRaises(FrozenInstanceError):
            result.deployment_authorized = True

    def test_each_artifact_tamper_is_refused_including_bin_elf_config_report_loader_sources(self):
        for name in self.files:
            original = self.files[name]
            with self.subTest(name=name), self.assertRaisesRegex(c.CandidateRefused, 'Pinned artifact'):
                self.files[name] = original + b' '
                self.validate()
            self.files[name] = original

    def test_missing_extra_mutable_and_wrong_pins_fail_closed(self):
        for name in list(self.files):
            original = self.files.pop(name)
            with self.subTest(name=name), self.assertRaises(c.CandidateRefused):
                self.validate()
            self.files[name] = original
        for bad in ('a' * 63, 'A' * 64, None, True, '0' * 64):
            with self.subTest(pin=bad), self.assertRaises(c.CandidateRefused):
                self.pins['candidate_bin'] = bad
                self.validate()
        self.pins['candidate_bin'] = sha(self.files['candidate_bin'])
        self.files['candidate_bin'] = bytearray(self.files['candidate_bin'])
        with self.assertRaisesRegex(c.CandidateRefused, 'immutable'):
            self.validate()

    def test_unlisted_input_cannot_be_ignored_even_if_pinned(self):
        self.files['source/new.c'] = b'unlisted'
        self.pins['source/new.c'] = sha(b'unlisted')
        with self.assertRaisesRegex(c.CandidateRefused, 'exactly cover'):
            self.validate()

    def test_rehashed_bin_corruption_still_requires_image_checksum(self):
        image = bytearray(self.files['candidate_bin'])
        image[560] ^= 1
        self.files['candidate_bin'] = bytes(image)
        self.pins['candidate_bin'] = sha(image)
        with self.assertRaisesRegex(c.CandidateRefused, 'checksum'):
            self.validate()

    def test_rehashed_elf_must_match_full_embedded_identity(self):
        self.files['candidate_elf'] += b'changed debug section too'
        self.pins['candidate_elf'] = sha(self.files['candidate_elf'])
        with self.assertRaisesRegex(c.CandidateRefused, 'ELF identity'):
            self.validate()

    def test_wrong_chip_mmu_capacity_and_a_target_refused(self):
        original = self.files['candidate_bin']
        for at, value in ((12, 0), (212, 15), (4, 0xff)):
            image = bytearray(original)
            image[at] = value
            self.files['candidate_bin'] = reseal(image)
            self.pins['candidate_bin'] = sha(self.files['candidate_bin'])
            with self.subTest(at=at), self.assertRaises(c.CandidateRefused):
                self.validate()
        self.files['candidate_bin'] = original.ljust(0x1f0001, b'\0')
        self.pins['candidate_bin'] = sha(self.files['candidate_bin'])
        with self.assertRaisesRegex(c.CandidateRefused, 'release/B bounds'):
            self.validate()
        for slot in ('ota_0', '', None, 'B'):
            with self.subTest(slot=slot), self.assertRaisesRegex(c.CandidateRefused, 'target must be B'):
                c.validate_candidate(self.files, expected_sha256=self.pins, target_slot=slot)

    def test_a_elf_in_resealed_image_cannot_be_used_as_candidate(self):
        image = bytearray(self.files['candidate_bin'])
        image[176:208] = bytes.fromhex(c.policy.BASELINE_ELF)
        self.files['candidate_bin'] = reseal(image)
        self.pins['candidate_bin'] = sha(self.files['candidate_bin'])
        with self.assertRaisesRegex(c.CandidateRefused, 'ELF identity'):
            self.validate()

    def test_manifest_hash_chip_bool_and_layout_cannot_be_redeclared(self):
        original = self.files['manifest']
        mutations = [lambda m: m['app'].update(sha256='0' * 64),
                     lambda m: m['app'].update(elf_sha256='a' * 64),
                     lambda m: m['chip'].update(id=0),
                     lambda m: m.update(protocol=True),
                     lambda m: m['layout']['ota_1'].update(address=0x10000),
                     lambda m: m['provenance'].update(build_report_sha256='f' * 64),
                     lambda m: m['protected_baseline'].update(image_sha256=m['app']['sha256'])]
        for mutate in mutations:
            self.files['manifest'] = original
            self.replace_json('manifest', mutate)
            with self.subTest(mutate=mutate), self.assertRaises(c.CandidateRefused):
                self.validate()

    def test_rehashed_report_cannot_rebind_receipt_or_config(self):
        self.replace_json('build_report', lambda r: r['sources'][0].update(sha256='a' * 64))
        with self.assertRaisesRegex(c.CandidateRefused, 'Receipt inputs'):
            self.validate()

    def test_receipt_cannot_be_replaced_by_reviewed_boolean(self):
        self.replace_json('build_receipt', lambda r: r.clear())
        with self.assertRaises(c.CandidateRefused):
            self.validate()
        self.files['link_report'] = b'{"reviewed":true}'
        self.pins['link_report'] = sha(self.files['link_report'])
        with self.assertRaises(c.CandidateRefused):
            self.validate()

    def test_rehashed_link_ranges_and_descriptors_are_recomputed(self):
        original = self.files['link_report']
        mutations = [lambda r: r['functions'][0].update(sha256='a' * 64),
                     lambda r: r['functions'][0].update(binary_offset=553),
                     lambda r: r['functions'][0].update(elf_offset=769),
                     lambda r: r['functions'][0].update(size=1),
                     lambda r: r['functions'][0].update(address='0x40374004'),
                     lambda r: r['functions'].pop(),
                     lambda r: r['descriptors'][0].update(cores=2),
                     lambda r: r['descriptors'][0].update(stage=True),
                     lambda r: r.update(old_bootloader_nominal_watchdog_ms=30000),
                     lambda r: r.update(pre_takeover_timing_proven=True)]
        for mutate in mutations:
            self.files['link_report'] = original
            self.replace_json('link_report', mutate)
            with self.subTest(mutate=mutate), self.assertRaises(c.CandidateRefused):
                self.validate()

    def test_resealed_bin_and_rehashed_link_still_need_exact_elf_bytes(self):
        image = bytearray(self.files['candidate_bin'])
        image[1064] ^= 1
        image = reseal(image)
        manifest = json.loads(self.files['manifest'])
        manifest['app']['sha256'] = sha(image)
        link = json.loads(self.files['link_report'])
        link['candidate'] = manifest['app']
        link['functions'][0]['sha256'] = sha(image[1064:1080])
        with self.assertRaisesRegex(c.CandidateRefused, 'Link range bytes'):
            c._validate_link(link, image, self.files['candidate_elf'], manifest)

    def test_new_candidate_metadata_changes_evidence_binding(self):
        first = self.validate()
        self.replace_json('link_report', lambda r: r.update(review_note='untrusted annotation only'))
        second = self.validate()
        self.assertNotEqual(first.binding_sha256, second.binding_sha256)
        self.assertEqual(first.static_findings, second.static_findings)

    def test_rehashed_partition_table_corruption_is_not_compatible(self):
        table = bytearray(self.files['partition_table'])
        table[0] ^= 1
        self.files['partition_table'] = bytes(table)
        self.pins['partition_table'] = sha(table)
        with self.assertRaises(c.CandidateRefused):
            self.validate()

    def test_safe_boolean_alone_never_proves_a_link(self):
        self.files['link_report'] = b'{"reviewed":true,"bin_elf_bindings_exact":true}'
        self.pins['link_report'] = sha(self.files['link_report'])
        with self.assertRaises(c.CandidateRefused):
            self.validate()

    def test_config_requires_exact_types_and_real_generated_and_sdkconfig_settings(self):
        report, manifest = (json.loads(self.files[k]) for k in ('build_report', 'manifest'))
        config = json.loads(self.files['generated_config'])
        for key in c.CONFIG_POLICY:
            changed = dict(config)
            changed[key] = 1 if type(config[key]) is bool else 'unsafe'
            changed_report = {**report, 'effective_config': {'CONFIG_' + k: v for k, v in changed.items()}}
            with self.subTest(key=key), self.assertRaisesRegex(c.CandidateRefused, 'Unsafe/missing'):
                c._validate_config(changed, changed_report, manifest, self.files['build_input/sdkconfig'])
        with self.assertRaisesRegex(c.CandidateRefused, 'sdkconfig safety'):
            c._validate_config(config, report, manifest, b'CONFIG_IDF_TARGET="esp32s3"')
        with self.assertRaisesRegex(c.CandidateRefused, 'Duplicate sdkconfig'):
            c._validate_config(config, report, manifest,
                self.files['build_input/sdkconfig'] + b'\nCONFIG_IDF_TARGET="esp32s3"')

    def test_malformed_json_duplicate_null_and_nonfinite_fail_closed(self):
        for raw in (b'{"chip":{},"chip":{}}', b'null', b'[]', b'{"x":NaN}', b'\xff', b'{'):
            with self.subTest(raw=raw), self.assertRaises(c.CandidateRefused):
                self.files['manifest'] = raw
                self.pins['manifest'] = sha(raw)
                self.validate()

    def test_external_confirmation_binding_never_claims_protocol_fixed(self):
        first = self.validate()
        second = self.validate(confirmation_contract_sha256='b' * 64)
        self.assertNotEqual(first.binding_sha256, second.binding_sha256)
        self.assertFalse(second.confirmation_contract_verified)
        self.assertFalse(second.deployment_authorized)
        with self.assertRaises(c.CandidateRefused):
            self.validate(confirmation_contract_sha256='reviewed=true')

    def test_no_file_process_network_or_device_effects(self):
        with mock.patch('builtins.open', side_effect=AssertionError('file I/O')), \
             mock.patch('pathlib.Path.open', side_effect=AssertionError('path I/O')), \
             mock.patch('subprocess.run', side_effect=AssertionError('process')), \
             mock.patch('socket.socket', side_effect=AssertionError('network')):
            self.validate()

    def test_elf_header_and_symbol_table_fail_closed(self):
        raw = self.files['candidate_elf']
        for at, value in ((4, 2), (5, 2), (18, 0), (48, 0), (32, 0xff)):
            changed = bytearray(raw)
            changed[at] = value
            with self.subTest(at=at), self.assertRaises(c.CandidateRefused):
                c._Elf(bytes(changed))


class OldLoaderTests(unittest.TestCase):
    def test_unknown_old_bytes_cannot_be_rehashed_into_evidence(self):
        for data in (b'', b'fake old loader', bytes(21024)):
            with self.assertRaisesRegex(c.CandidateRefused, 'Exact old bootloader'):
                c._validate_old(data)

    def test_original_old_bytes_when_available_and_tamper(self):
        if not OLD.is_file():
            self.skipTest('Original old bootloader absent; no substitute generated')
        old = OLD.read_bytes()
        c._validate_old(old)
        for changed in (old + b'\xff', old[:-1], bytes([old[0] ^ 1]) + old[1:]):
            with self.assertRaises(c.CandidateRefused):
                c._validate_old(changed)
        with self.assertRaisesRegex(c.old_evidence.EvidenceRefused, 'Only current-A'):
            c.old_evidence.validate_reviewed_binary_evidence({}, purpose='install-first-b')


class ArchivedCandidateTests(unittest.TestCase):
    def setUp(self):
        paths = dict(candidate_bin=RELEASE / 'app.bin', candidate_elf=BUILD / 'mixos_esp32s3.elf',
                     manifest=RELEASE / 'manifest.json', generated_config=BUILD / 'config/sdkconfig.json',
                     partition_table=BUILD / 'partition_table/partition-table.bin', old_bootloader=OLD,
                     build_report=REPORTS / 'font-app-build.json', build_receipt=REPORTS / 'completed-build.json',
                     link_report=REPORTS / 'early-rtc-link-audit-v2.json')
        if not all(p.is_file() for p in paths.values()):
            self.skipTest('Archived candidate evidence absent; no replacement generated')
        self.files = {k: p.read_bytes() for k, p in paths.items()}

    def test_real_candidate_static_link_bytes_and_watchdog_scope(self):
        image, elf = self.files['candidate_bin'], self.files['candidate_elf']
        self.assertEqual(sha(image), 'faafb49aa8f4ae6d0e215e7b4543f6cf72e41e19e4821a7bf7bc497b492f87e3')
        self.assertEqual(sha(elf), '34580ab8658d32736905b8cf460e913bcd0f14561567a861da79842b151a4821')
        c.policy.validate_app(image, sha(image), sha(elf))
        c._validate_old(self.files['old_bootloader'])
        functions = c._validate_link(json.loads(self.files['link_report']), image, elf,
                                     json.loads(self.files['manifest']))
        self.assertEqual(set(functions), c.REQUIRED_FUNCTIONS)

    def test_actual_a_image_cannot_be_relabelled_candidate(self):
        path = ROOT / 'build/esp32s3/current-device-app.bin'
        if not path.is_file():
            self.skipTest('Original protected A BIN absent; no A ELF is required')
        image = path.read_bytes()
        self.assertEqual(sha(image), c.policy.BASELINE_SHA256)
        self.files['candidate_bin'] = image
        pins = {k: sha(v) for k, v in self.files.items()}
        with self.assertRaisesRegex(c.CandidateRefused, 'Protected A cannot'):
            c.validate_candidate(self.files, expected_sha256=pins, target_slot='ota_1')

    def test_actual_receipt_requires_original_inputs_never_fabricates_history(self):
        report = json.loads(self.files['build_report'])
        pins = {k: sha(v) for k, v in self.files.items()}
        stale = False
        for group, prefix in (('sources', 'source/'), ('build_inputs', 'build_input/')):
            for rec in report[group]:
                name = rec['path'].replace('\\', '/').rsplit('/', 1)[-1]
                path = ROOT / 'firmware/esp32s3' / ('main' if group == 'sources' else '') / name
                if not path.is_file():
                    self.skipTest('Original input absent; archive cannot be reconstructed: ' + name)
                data = path.read_bytes()
                self.files[prefix + name] = data
                pins[prefix + name] = rec['sha256']
                stale |= sha(data) != rec['sha256']
        if stale:
            with self.assertRaisesRegex(c.CandidateRefused, 'Pinned artifact SHA256 mismatch'):
                c.validate_candidate(self.files, expected_sha256=pins, target_slot='ota_1')
        else:
            result = c.validate_candidate(self.files, expected_sha256=pins, target_slot='ota_1')
            self.assertFalse(result.confirmation_contract_verified)
            self.assertFalse(result.deployment_authorized)


if __name__ == '__main__':
    unittest.main()
