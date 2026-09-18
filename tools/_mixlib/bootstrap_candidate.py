"""Offline candidate facts for a SEPARATE, externally authorized first-B route.

validate_candidate(artifacts, expected_sha256=..., target_slot='ota_1',
                   confirmation_contract_sha256=None) consumes immutable bytes.
FIXED_ARTIFACTS plus source/<basename> and build_input/<basename> are required;
those two inventories must exactly cover the completed-build receipt. Report
paths are labels only, NEVER opened. The sdkconfig input is build_input/sdkconfig.
Every supplied byte is pinned by the independent expected_sha256 mapping. Pins
are selection/integrity inputs, NOT authentication or approval. A caller must
preserve/authenticate the original receipt and complete input inventory; matching
self-written JSON cannot prove a compilation happened or a program is safe.

No I/O, device, process, build, approval issuance or old-purpose widening. The
old binary identity is checked here; its separate reviewed evidence still has
qualify-current-a-no-write purpose. An external new route must combine that
bounded evidence with these candidate facts and explicit operation/risk approval.
Neither original A ELF nor arbitrary broken hardware is an input prerequisite.

The optional confirmation_contract_sha256 is an OPAQUE external review binding,
not a verified protocol. None means the parent still needs to supply it. Even
when present, confirmation_contract_verified stays False: this module does not
assume the archived early-RTC candidate has a corrected confirmation protocol.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
import hashlib
import json
import re
import struct

from . import bootloader_evidence as old_evidence
from . import ota_bootstrap as policy
from mixos_esp_update import LAYOUT, REQUIRED_CONFIG, SCHEMA as RELEASE_SCHEMA

SCHEMA = 'mixos-bootstrap-candidate-static/v1'
FIXED_ARTIFACTS = frozenset({
    'candidate_bin', 'candidate_elf', 'manifest', 'generated_config',
    'build_report', 'build_receipt', 'link_report', 'old_bootloader', 'partition_table',
})
BUILD_INPUTS = frozenset({'sdkconfig', 'sdkconfig.defaults', 'partitions.csv',
                          'CMakeLists.txt', 'dependencies.lock'})
EARLY_HOOK = '__esp_system_init_fn_mix_health_early_rtc'
REQUIRED_FUNCTIONS = frozenset({EARLY_HOOK, 'mix_health_init', 'esp_clk_init',
                              'call_start_cpu0', 'start_cpu0_default',
                              'do_core_init', 'do_system_init_fn', 'app_main'})
CONFIG_POLICY = {
    **{key.removeprefix('CONFIG_'): value for key, value in REQUIRED_CONFIG.items()},
    'IDF_TARGET': 'esp32s3', 'IDF_TARGET_ARCH': 'xtensa',
    'IDF_TARGET_ESP32S3': True, 'ESPTOOLPY_FLASHSIZE': '8MB',
    'BOOTLOADER_WDT_TIME_MS': 30000, 'BOOTLOADER_OFFSET_IN_FLASH': 0,
    'PARTITION_TABLE_OFFSET': 0x8000, 'BOOTLOADER_APP_ANTI_ROLLBACK': False,
    'BOOTLOADER_REGION_PROTECTION_ENABLE': True, 'BOOTLOADER_APP_TEST': False,
    'BOOTLOADER_FACTORY_RESET': False, 'BOOTLOADER_SKIP_VALIDATE_ALWAYS': False,
    'BOOTLOADER_SKIP_VALIDATE_IN_DEEP_SLEEP': False,
    'BOOTLOADER_SKIP_VALIDATE_ON_POWER_ON': False, 'EFUSE_VIRTUAL': False,
    'SECURE_BOOT': False, 'SECURE_FLASH_ENC_ENABLED': False,
    'SECURE_SIGNED_APPS_NO_SECURE_BOOT': False,
}
UNPROVEN = (
    'old_9000ms_pre_takeover_timing_and_margin',
    'physical_watchdog_expiry_and_reset_path',
    'candidate_boot_and_no_button_recovery_on_existing_hardware',
    'rollback_persistence_and_power_loss_safety',
    'receiver_and_maintenance_confirmation_contract',
    'runtime_health_and_communication',
    'installed_bytes_chip_revision_rom_efuse_and_debug_state',
    'receipt_authenticity_compilation_and_complete_dependency_closure',
)
EXTERNAL_REQUIREMENTS = (
    'separate_old_binary_evidence_with_original_purpose_and_limitations',
    'candidate_bound_confirmation_contract_validation',
    'explicit_acceptance_of_each_unproven_runtime_risk',
    'operation_specific_authorization_and_live_identity_safety_controls',
    'immutable_package_pinning_this_validator_and_its_dependencies',
)


class CandidateRefused(ValueError):
    """Missing, changed, ambiguous, unsupported or out-of-scope input."""


def _require(condition, message):
    if not condition:
        raise CandidateRefused(message)


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def _encoded(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'),
                      allow_nan=False).encode('utf-8')


def _equal(a, b):
    # Python's ordinary container equality incorrectly equates True and 1.
    return _encoded(a) == _encoded(b)


def _digest(value):
    _require(type(value) is str and re.fullmatch('[0-9a-f]{64}', value), 'Invalid full SHA256')
    return value


def _json(data):
    return old_evidence._json_object(data)


def _integer(value, label):
    _require(type(value) is int and 0 <= value <= 0xffffffff, 'Invalid integer: ' + label)
    return value


def _address(value):
    _require(type(value) is str and re.fullmatch('0x[0-9a-f]{1,8}', value), 'Invalid address')
    return int(value, 16)


def _slice(data, offset, size):
    _integer(offset, 'offset')
    _integer(size, 'size')
    _require(size > 0 and offset + size <= len(data), 'Out-of-bounds artifact range')
    return data[offset:offset + size]


def _record(record, data):
    _require(type(record) is dict and type(record.get('path')) is str and record['path'],
             'Build record needs a path label')
    _require(type(record.get('bytes')) is int and record['bytes'] == len(data)
             and _digest(record.get('sha256')) == _sha(data), 'Build record size/SHA256 mismatch')


def _validate_old(data):
    _require(_sha(data) == old_evidence.OLD_SHA256, 'Exact old bootloader SHA256 required')
    old_evidence._validate_image(data)


class _Elf:
    """Bounded ELF32 little-endian Xtensa section/symbol reader, no objdump."""
    def __init__(self, data):
        self.data = data
        _require(len(data) >= 52 and data[:7] == b'\x7fELF\x01\x01\x01', 'ELF32 LE required')
        h = struct.unpack_from('<HHIIIIIHHHHHH', data, 16)
        _require(h[:3] == (2, 94, 1) and h[7] == 52 and h[10] == 40,
                 'Executable Xtensa ELF header required')
        self.entry = h[3]
        offset, count = h[5], h[11]
        _require(0 < count <= 4096, 'Bounded ELF section table required')
        _slice(data, offset, count * 40)
        self.sections = [struct.unpack_from('<10I', data, offset + i * 40) for i in range(count)]
        self.symbols = {}
        for section in self.sections:
            if section[1] != 8 and section[5]:  # SHT_NOBITS has no file payload.
                _slice(data, section[4], section[5])
            if section[1] != 2:  # SHT_SYMTAB; do not execute tools named in a report.
                continue
            _require(section[9] == 16 and section[5] % 16 == 0
                     and section[6] < count, 'Invalid ELF symbol table')
            strings = self.sections[section[6]]
            _require(strings[1] == 3, 'Invalid ELF symbol string table')
            names = _slice(data, strings[4], strings[5])
            for at in range(section[4], section[4] + section[5], 16):
                name, address, size, info, _, index = struct.unpack_from('<IIIBBH', data, at)
                _require(name < len(names) and names.find(b'\0', name) >= 0, 'Invalid ELF symbol name')
                if not name or not index:
                    continue
                text = names[name:names.index(b'\0', name)].decode('utf-8', 'strict')
                self.symbols.setdefault(text, []).append((address, size, info & 15, index))

    def symbol(self, name, function=False):
        matches = self.symbols.get(name, [])
        _require(len(matches) == 1, 'Missing/ambiguous ELF symbol: ' + name)
        symbol = matches[0]
        if function:
            _require(symbol[2] == 2 and symbol[1] > 0 and symbol[3] < len(self.sections)
                     and self.sections[symbol[3]][2] & 4, 'Executable full function required: ' + name)
        return symbol

    def offset(self, address, size):
        matches = [s[4] + address - s[3] for s in self.sections
                   if s[1] == 1 and s[2] & 2 and s[3] <= address
                   and address + size <= s[3] + s[5]]
        _require(len(matches) == 1, 'Ambiguous/unmapped ELF address range')
        return matches[0]


def _segments(image):
    # Called only AFTER the shared strict image parser validated all bounds.
    offset, result = 24, []
    for _ in range(image[1]):
        address, size = struct.unpack_from('<II', image, offset)
        offset += 8
        if address:
            result.append((address, size, offset))
        offset += size
    return result


def _validate_link(report, image, elf_bytes, manifest):
    _require(report['schema'] == 'mixos-candidate-early-rtc-link-audit/v1', 'Unsupported link report')
    _require(_equal(report['candidate'], manifest['app'])
             and report['elf_sha256'] == _sha(elf_bytes)
             and _equal(report['build_provenance'], manifest['provenance']), 'Link report binding mismatch')
    for key, value in {'old_bootloader_nominal_watchdog_ms': 9000,
                       'app_config_nominal_watchdog_ms': 30000,
                       'deployment_authorized': False, 'device_access': False,
                       'pre_takeover_timing_proven': False}.items():
        _require(_equal(report.get(key), value), 'Link report scope/budget mismatch: ' + key)
    elf = _Elf(elf_bytes)
    _require(elf.entry == struct.unpack_from('<I', image, 4)[0], 'BIN/ELF entry mismatch')
    segments = _segments(image)

    def check_range(record):
        address, size = _address(record['address']), _integer(record['size'], 'range size')
        bo = [offset + address - start for start, length, offset in segments
              if start <= address and address + size <= start + length]
        _require(len(bo) == 1, 'Unmapped/ambiguous BIN range')
        eo = elf.offset(address, size)
        _require(type(record['binary_offset']) is int and type(record['elf_offset']) is int
                 and record['binary_offset'] == bo[0] and record['elf_offset'] == eo,
                 'Report offsets do not match BIN/ELF address mapping')
        data = _slice(image, bo[0], size)
        _require(data == _slice(elf_bytes, eo, size)
                 and _sha(data) == _digest(record['sha256']), 'Link range bytes/SHA256 mismatch')
        _require(record.get('bin_elf_exact') is True, 'Contradictory link range assertion')
        return data

    functions = report['functions']
    _require(type(functions) is list and len(functions) <= 128, 'Bounded link functions required')
    names = set()
    for record in functions:
        name = record['name']
        _require(type(name) is str and name not in names, 'Duplicate link function')
        names.add(name)
        symbol = elf.symbol(name, function=True)
        _require(symbol[:2] == (_address(record['address']), _integer(record['size'], 'function size')),
                 'Report does not cover full ELF function: ' + name)
        check_range(record)
    _require(REQUIRED_FUNCTIONS <= names, 'Missing startup/health link functions')
    array = report['init_array']
    start = elf.symbol('_esp_system_init_fn_array_start')[0]
    end = elf.symbol('_esp_system_init_fn_array_end')[0]
    _require(_address(array['address']) == start and end - start == array['size']
             and 0 < end - start <= 4096 and (end - start) % 8 == 0, 'Invalid init array bounds')
    raw = check_range(array)
    descriptors = report['descriptors']
    _require(type(descriptors) is list and len(descriptors) == len(raw) // 8, 'Init descriptor count mismatch')
    first_cpu0 = None
    for i, descriptor in enumerate(descriptors):
        fn, cores, stage = struct.unpack_from('<IHH', raw, i * 8)
        _require(cores in (1, 2, 3) and stage in (0, 1), 'Invalid init descriptor cores/stage')
        _require(_equal(descriptor, dict(index=i, address=hex(start + i * 8),
                     callback=descriptor['callback'], fn=hex(fn), cores=cores, stage=stage)),
                 'Init descriptor differs from actual bytes')
        _require(elf.symbol(descriptor['callback'], function=True)[0] == fn, 'Init callback symbol mismatch')
        if first_cpu0 is None and cores & 1 and stage == 0:
            first_cpu0 = descriptor
    _require(first_cpu0 is not None and first_cpu0['callback'] == EARLY_HOOK
             and _equal(report['early_hook'], first_cpu0), 'Early hook is not first CPU0 core descriptor')
    return tuple(sorted(names))


def _validate_config(generated, report, manifest, sdkconfig):
    effective = {'CONFIG_' + k: v for k, v in generated.items()}
    _require(_equal(report['effective_config'], effective), 'Effective/generated config mismatch')
    for key, value in CONFIG_POLICY.items():
        _require(_equal(generated.get(key), value), 'Unsafe/missing candidate configuration: ' + key)
    _require(_equal(manifest['effective_config'], {k: effective[k] for k in REQUIRED_CONFIG}),
             'Manifest effective config mismatch')
    # Validate source Kconfig settings as well as the generated compiler JSON.
    settings = {}
    for line in sdkconfig.decode('utf-8', 'strict').splitlines():
        setting = re.fullmatch(r'CONFIG_([A-Za-z0-9_]+)=(.*)', line)
        unset = re.fullmatch(r'# CONFIG_([A-Za-z0-9_]+) is not set', line)
        if not setting and not unset:
            continue
        key = (setting or unset)[1]
        _require(key not in settings, 'Duplicate sdkconfig setting: ' + key)
        text = setting[2] if setting else 'n'
        if text in ('y', 'n'):
            value = text == 'y'
        elif text.startswith('"'):
            value = json.loads(text)
        else:
            value = int(text, 16 if text.startswith('0x') else 10)
        settings[key] = value
    for key, value in CONFIG_POLICY.items():
        _require(_equal(settings.get(key), value), 'sdkconfig safety setting mismatch: ' + key)


def _validate_build(files, report, receipt, manifest):
    _require(report.get('status') == 'cross-built' and report.get('target') == 'esp32s3'
             and report.get('ota_capable') is True and report.get('partition_layout') == 'ab',
             'Completed ESP32-S3 A/B build report required')
    _require(receipt.get('schema') == 'mixos-local-build/v1', 'Completed build receipt required')
    _record(report['build_attestation'], files['build_receipt'])
    artifact_keys = {'build_app': 'candidate_bin', 'elf': 'candidate_elf',
                     'partition_table': 'partition_table', 'sdkconfig_generated': 'generated_config'}
    _require(_equal(receipt['artifacts'], {key: report[key] for key in artifact_keys}),
             'Receipt artifacts differ from build report')
    for key, name in artifact_keys.items():
        _record(report[key], files[name])
    _require(_equal(receipt['inputs'], {key: report[key] for key in ('sources', 'build_inputs')}),
             'Receipt inputs differ from build report')
    expected = set(FIXED_ARTIFACTS)
    for group, prefix in (('sources', 'source/'), ('build_inputs', 'build_input/')):
        records = report[group]
        _require(type(records) is list and 0 < len(records) <= 512, 'Bounded build input inventory required')
        names = set()
        for record in records:
            name = record['path'].replace('\\', '/').rsplit('/', 1)[-1]
            _require(re.fullmatch('[A-Za-z0-9_.-]+', name) and name not in ('.', '..')
                     and name.casefold() not in names, 'Duplicate/invalid build input name')
            names.add(name.casefold())
            expected.add(prefix + name)
            _record(record, files[prefix + name])
        if group == 'build_inputs':
            _require(names == {n.casefold() for n in BUILD_INPUTS}, 'Incomplete build input set')
        else:
            _require({'cmakelists.txt', 'idf_component.yml', 'main.c', 'mix_health.c',
                      'mix_health.h', 'mix_ota.c', 'mix_ota.h', 'mix_ota_tx.c', 'mix_ota_tx.h'} <= names,
                     'Missing main/startup/OTA source input')
    _require(set(files) == expected, 'Artifact inventory must exactly cover receipt inputs')
    _record(report['sdkconfig'], files['build_input/sdkconfig'])
    provenance = manifest['provenance']
    _require(provenance['mode'] == 'exact-source-and-effective-build-configuration', 'Build provenance mode required')
    for key, name in {'build_report_sha256': 'build_report', 'sdkconfig_sha256': 'build_input/sdkconfig',
                      'partition_table_sha256': 'partition_table', 'elf_file_sha256': 'candidate_elf'}.items():
        _require(_digest(provenance[key]) == _sha(files[name]), 'Manifest provenance hash mismatch: ' + key)


@dataclass(frozen=True)
class CandidateEvidence:
    """Static facts only; construction or possession is never a capability."""
    artifact_hashes: tuple[tuple[str, str], ...]
    candidate_sha256: str
    candidate_elf_sha256: str
    candidate_bytes: int
    linked_functions: tuple[str, ...]
    confirmation_contract_sha256: str | None
    schema: str = field(default=SCHEMA, init=False)
    target_slot: str = field(default='ota_1', init=False)
    target_address: int = field(default=0x610000, init=False)
    target_capacity: int = field(default=0x1f0000, init=False)
    chip: str = field(default='esp32s3', init=False)
    chip_id: int = field(default=9, init=False)
    old_bootloader_sha256: str = field(default=old_evidence.OLD_SHA256, init=False)
    old_binary_evidence_route: str = field(default=old_evidence.ROUTE, init=False)
    old_binary_evidence_purpose: str = field(default=old_evidence.QUALIFICATION_PURPOSE, init=False)
    old_nominal_watchdog_ms: int = field(default=9000, init=False)
    app_config_nominal_watchdog_ms: int = field(default=30000, init=False)
    historical_config_recovered: bool = field(default=False, init=False)
    confirmation_contract_verified: bool = field(default=False, init=False)
    deployment_authorized: bool = field(default=False, init=False)
    unproven: tuple[str, ...] = field(default=UNPROVEN, init=False)
    required_external_evidence: tuple[str, ...] = field(default=EXTERNAL_REQUIREMENTS, init=False)
    static_findings: tuple[str, ...] = field(default=(
        'BIN checksums, ESP32-S3 segments/MMU mapping and B capacity pass shared strict validation.',
        'BIN embedded ELF SHA256 equals the entire supplied executable Xtensa ELF SHA256.',
        'Manifest, generated safety settings, supplied inputs and completed-build receipt are byte-bound.',
        'Required full startup functions and the first CPU0 core init descriptor match BIN/ELF bytes.',
        'Old nominal 9000ms and candidate config 30000ms are distinct; no remaining-time inference.',
        'Link range matching proves static placement, not instruction semantics or callback reachability.',
    ), init=False)

    @property
    def binding_sha256(self):
        """Stable domain/version-separated digest; excludes paths opened at runtime (none)."""
        return _sha(_encoded(asdict(self)))


def validate_candidate(artifacts: Mapping[str, bytes], *, expected_sha256: Mapping[str, str],
                       target_slot: str, confirmation_contract_sha256: str | None = None) -> CandidateEvidence:
    """Return immutable facts or CandidateRefused. Never read files or grant install.

    expected_sha256 must cover exactly artifacts; its origin must be authenticated
    externally. Source/input bytes must be the originals, not today's substitutes.
    A normal IDF build report may say app_partition=ota_0 (link/build offset); that
    is NOT current-A identity. The candidate BIN/ELF must differ from protected A,
    and target_slot must explicitly be ota_1. No original A ELF is required.
    """
    try:
        _require(target_slot == 'ota_1', 'Candidate target must be B / ota_1')
        _require(isinstance(artifacts, Mapping) and isinstance(expected_sha256, Mapping), 'Explicit artifact/pin mappings required')
        files, pins = dict(artifacts), dict(expected_sha256)
        _require(FIXED_ARTIFACTS <= set(files) and set(files) == set(pins) and len(files) <= 1033,
                 'Exact complete artifact/pin mapping required')
        for name, data in files.items():
            _require(type(name) is str and type(data) is bytes and 0 < len(data) <= 32 * 1024 * 1024,
                     'Bounded immutable artifact bytes required')
            _require(_sha(data) == _digest(pins[name]), 'Pinned artifact SHA256 mismatch: ' + name)
        _validate_old(files['old_bootloader'])
        if confirmation_contract_sha256 is not None:
            _digest(confirmation_contract_sha256)
        image, elf = files['candidate_bin'], files['candidate_elf']
        _require(_sha(image) != policy.BASELINE_SHA256 and _sha(elf) != policy.BASELINE_ELF,
                 'Protected A cannot be a new B candidate')
        _require(1024 <= len(image) <= policy.APP_SIZE, 'Candidate image size exceeds release/B bounds')
        identity = policy.validate_app(image, pins['candidate_bin'], pins['candidate_elf'])
        _require(identity['elf_sha256'] != policy.BASELINE_ELF, 'Protected A ELF cannot be a candidate')
        policy.validate_table(files['partition_table'])
        _require(_sha(files['partition_table']) == policy.RECOVERED_TABLE_SHA256, 'Old-loader A/B table identity mismatch')
        manifest, report, receipt, generated, link = (_json(files[k]) for k in
            ('manifest', 'build_report', 'build_receipt', 'generated_config', 'link_report'))
        _require(manifest['schema'] == RELEASE_SCHEMA and type(manifest['protocol']) is int
                 and manifest['protocol'] == 2, 'Release v2 required')
        _require(_equal(manifest['chip'], {'name': 'esp32s3', 'id': 9})
                 and _equal(manifest['layout'], LAYOUT), 'Candidate chip/B layout mismatch')
        _require(_equal(manifest['app'], {'file': 'app.bin', 'size': len(image),
                     'sha256': _sha(image), 'elf_sha256': _sha(elf)}), 'Manifest candidate identity mismatch')
        baseline = manifest['protected_baseline']
        _require(baseline['slot'] == 'ota_0' and baseline['image_sha256'] == policy.BASELINE_SHA256
                 and baseline['elf_sha256'] == policy.BASELINE_ELF
                 and type(baseline['image_bytes']) is int and baseline['image_bytes'] == policy.BASELINE_BYTES,
                 'Protected A binding mismatch')
        _validate_build(files, report, receipt, manifest)
        _validate_config(generated, report, manifest, files['build_input/sdkconfig'])
        functions = _validate_link(link, image, elf, manifest)
        return CandidateEvidence(tuple(sorted(pins.items())), _sha(image), _sha(elf), len(image),
                                 functions, confirmation_contract_sha256)
    except CandidateRefused:
        raise
    except (ValueError, KeyError, TypeError, AttributeError, IndexError, struct.error, RecursionError) as exc:
        raise CandidateRefused('Invalid candidate evidence: ' + str(exc)) from exc
