"""Offline bootstrap policy and injected, single-attempt orchestration.

No device access on import. An injected backend owns bounded hardware effects;
callers must hold the shared flash lock throughout each operation. Only the
new CLI supplies a production backend. No ordinary OTA_END is ever used.

IDF 5.4 references: app_update/esp_ota_ops.c (esp_rewrite_ota_data),
bootloader_support/src/bootloader_common_loader.c and esp_flash_partitions.h.
CRC covers ONLY the sequence, not the state. We refuse malformed live records
rather than adopting IDF's fallback/equal-sequence behavior as write authority.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
import struct
import zlib

from .durable import durable_new, sync_directory

FLASH_SIZE = 0x800000
SECTOR = 0x1000
APP0 = 0x10000
APP1 = 0x610000
APP_SIZE = 0x1F0000
OTADATA = 0x200000
TABLE = 0x8000
UINT32_MAX = 0xFFFFFFFF
NEW, PENDING_VERIFY, VALID, INVALID, ABORTED = range(5)
BASELINE_SHA256 = '7875d9a513acb95463b72e785ebd160c70d03f85e965c3a30a93d954bb4cff5f'
BASELINE_BYTES = 894560
BASELINE_ELF = 'cfacb3fe25931e918b8f46d1f840da8d57ba39ef799bd0af4629939b9185761a'
# The migration receipt pins this bootloader; the 2026-09-16 full recovery
# comparison proves it was not changed. The CLI also checks its exact padded
# 32 KiB bytes against that recovery snapshot, not just an image-header prefix.
RECOVERED_BOOT_SHA256 = '6ea16d3717dbe339973b44109f4bd9bd50d6000418e58f677cd5b9e45116531d'
RECOVERED_TABLE_SHA256 = '2666056ebdd9cd132485b0ebeb3d6fdf7eeb384fb069c40be45bf00a58301b09'
RECOVERED_FULL_SHA256 = 'df9c108f6248f2cfde22f097187beaeef5d76dbfd34a173140671c164939a73e'
APP_IDENTITY = dict(vid='303a', pid='80c3', serial='TD0720', location='5-1.2')
ROM_IDENTITY = dict(vid='303a', pid='0009', serial='70:04:1d:d8:54:14', location='5-1.2')
TRANSPORT_VERSION = '5.4.0'
RESET_METHOD = 'official-esptool-watchdog-reset'
CANDIDATE_ROUTE = 'mixos-reviewed-candidate-bootstrap/v1'


class Refused(ValueError):
    """A guard failed; preserve all evidence and never retry automatically."""


def require(condition, message):
    if not condition:
        raise Refused(message)


def sha(data):
    return hashlib.sha256(data).hexdigest()


def encoded(value):
    return (json.dumps(value, sort_keys=True, separators=(',', ':')) + '\n').encode()


def digest(value):
    require(isinstance(value, str) and re.fullmatch('[0-9a-f]{64}', value), 'Invalid SHA256')
    return value


def task_id(value):
    require(isinstance(value, str) and re.fullmatch('[a-z0-9][a-z0-9-]{7,79}', value), 'Invalid task ID')
    return value


def ota_crc(sequence):
    """esp_rom_crc32_le(UINT32_MAX, little-endian ota_seq, 4)."""
    return zlib.crc32(struct.pack('<I', sequence), UINT32_MAX) & UINT32_MAX


@dataclass(frozen=True)
class Record:
    seq: int
    label: bytes
    state: int
    crc: int

    @classmethod
    def parse(cls, sector):
        require(len(sector) == SECTOR, 'Otadata record needs exactly one sector')
        return cls(*struct.unpack_from('<I20sII', sector))

    def idf_valid(self):
        # Deliberately mirrors IDF, including acceptance of unknown states and
        # seq=0. Those cases are rejected separately by our stricter planner.
        return (self.seq != UINT32_MAX and self.state not in (INVALID, ABORTED)
                and self.crc == ota_crc(self.seq))


def idf_active(sectors):
    """Read-only IDF selection oracle; ties choose sector zero, no wrap logic."""
    require(len(sectors) == 2, 'Exactly two otadata sectors required')
    records = [Record.parse(s) for s in sectors]
    valid = [i for i, record in enumerate(records) if record.idf_valid()]
    return max(valid, key=lambda i: records[i].seq) if valid else None


def select_baseline(data):
    require(len(data) == 2 * SECTOR, 'Exact 8 KiB otadata required')
    sectors = (data[:SECTOR], data[SECTOR:])
    records = [Record.parse(s) for s in sectors]
    for sector, record in zip(sectors, records):
        require(record.idf_valid() and record.state == VALID and 0 < record.seq < UINT32_MAX,
                'Both live records must have valid CRC, nonzero sequence and VALID state')
        require(record.label == b'\xff' * 20 and sector[32:] == b'\xff' * (SECTOR - 32),
                'Unexpected otadata label/padding; manual review required')
    require(records[0].seq != records[1].seq, 'Equal/ambiguous live sequences')
    require(abs(records[0].seq - records[1].seq) < 0x80000000,
            'Ambiguous sequence wrap/history')
    selected = idf_active(sectors)
    require((records[selected].seq - 1) % 2 == 0, 'Selected baseline must be ota_0')
    require((records[1 - selected].seq - 1) % 2 == 1, 'Other record must describe ota_1')
    return selected, records


def validate_table(table):
    # Reuse the existing audited layout/MD5 policy without changing its files.
    from flash_font_on_pi import validate_table as existing_validate_table
    require(existing_validate_table(table)['name'] == 'ab', 'Exact A/B layout required')


def validate_app(data, expected_sha256, expected_elf=None):
    """Pure strict ESP32-S3 image validation, including every segment/digest.

    Hash approval is still required: a valid image format is not evidence that
    its receiver or runtime health confirmation is safe. The caller's immutable
    candidate review must approve those separately.
    """
    require(sha(data) == digest(expected_sha256), 'Application SHA256 mismatch')
    require(288 <= len(data) <= APP_SIZE and data[0] == 0xE9 and 1 <= data[1] <= 16,
            'Invalid application header/size')
    require(struct.unpack_from('<H', data, 12)[0] == 9 and data[23] == 1,
            'ESP32-S3 image with appended SHA256 required')
    require(struct.unpack_from('<I', data, 32)[0] == 0xABCD5432,
            'Missing application descriptor')
    require(struct.unpack_from('<I', data, 28)[0] >= 256, 'Truncated application descriptor segment')
    require(struct.unpack_from('<I', data, 36)[0] == 0, 'Nonzero secure version unsupported')
    require(data[80:112].split(b'\0', 1)[0] == b'mixos_esp32s3', 'Unexpected application project')
    elf = data[176:208].hex()
    require(elf not in ('00' * 32, 'ff' * 32), 'Missing application ELF identity')
    if expected_elf is not None:
        require(elf == digest(expected_elf), 'Application ELF identity mismatch')
    offset, checksum = 24, 0xEF
    mapped = set()
    loaded = []
    # Accept ordinary IDF S3 app ranges, never ROM/MMIO or external RAM loads.
    # Both A/B offsets are 64 KiB aligned. Boot validation still occurs at boot.
    ranges = ((0x3C000000, 0x3D000000, 'drom'), (0x42000000, 0x42800000, 'irom'),
              (0x3FC88000, 0x3FD00000, 'dram'), (0x40370000, 0x403E0000, 'iram'),
              (0x600FE000, 0x60100000, 'rtc'))
    require(data[212] == 16, 'Only the reviewed 64 KiB MMU page size is supported')
    for index in range(data[1]):
        require(offset + 8 <= len(data), 'Truncated segment header')
        address, size = struct.unpack_from('<II', data, offset)
        offset += 8
        require(size % 4 == 0 and size <= APP_SIZE and offset + size <= len(data)
                and address + size <= 0x100000000, 'Invalid image segment')
        if address == 0:
            require(index != 0 and not any(data[offset:offset + size]), 'Nonzero/first padding segment')
        else:
            matches = [kind for start, end, kind in ranges if start <= address < end and address + size <= end]
            require(len(matches) == 1 and size > 0 and address % 4 == 0, 'Unsupported S3 load address')
            kind = matches[0]
            require(index != 0 or kind == 'drom', 'Descriptor must be in the first DROM segment')
            if kind in ('drom', 'irom'):
                require(kind not in mapped and address % 0x10000 == offset % 0x10000,
                        'Mapped flash segment is duplicate or misaligned')
                mapped.add(kind)
            require(not any(address < end and start < address + size for start, end, _ in loaded),
                    'Overlapping image segments')
            loaded.append((address, address + size, kind))
        for byte in data[offset:offset + size]:
            checksum ^= byte
        offset += size
    entry = struct.unpack_from('<I', data, 4)[0]
    require(any(start <= entry < end and kind in ('iram', 'irom', 'rtc') for start, end, kind in loaded),
            'Entry point is outside a loaded executable segment')
    checksum_at = offset | 15
    require(checksum_at + 33 == len(data), 'Truncated image or unsupported trailing bytes/signature')
    require(not any(data[offset:checksum_at]) and data[checksum_at] == checksum,
            'ESP image checksum/padding mismatch')
    require(data[-32:] == hashlib.sha256(data[:-32]).digest(), 'Appended image SHA256 mismatch')
    return {'sha256': sha(data), 'bytes': len(data), 'elf_sha256': elf}


@dataclass(frozen=True)
class Trust:
    """Explicit evidence, not a claim derived from the currently connected chip."""
    boot_region_sha256: str
    table_sha256: str
    baseline_sha256: str = BASELINE_SHA256
    baseline_bytes: int = BASELINE_BYTES
    baseline_elf: str = BASELINE_ELF
    rollback: bool = True

    def binding(self):
        return sha(encoded(self.__dict__))


def validate_baseline(before, trust):
    require(len(before) == FLASH_SIZE, 'Fresh backup must be exactly 8 MiB')
    require(trust.rollback is True, 'Trusted rollback bootloader required')
    require(sha(before[:TABLE]) == digest(trust.boot_region_sha256), 'Live bootloader trust mismatch')
    table = before[TABLE:TABLE + 0xC00]
    require(sha(table) == digest(trust.table_sha256), 'Live partition table hash mismatch')
    validate_table(table)
    require(288 <= trust.baseline_bytes <= APP_SIZE, 'Invalid baseline size')
    validate_app(before[APP0:APP0 + trust.baseline_bytes], trust.baseline_sha256, trust.baseline_elf)
    return select_baseline(before[OTADATA:OTADATA + 2 * SECTOR])


@dataclass(frozen=True)
class Plan:
    app: bytes
    metadata: bytes
    metadata_offset: int
    active_sector: int
    old_seq: int
    next_seq: int
    before_sha256: str
    staged_sha256: str
    expected_sha256: str
    candidate: dict

    def summary(self):
        return dict(active_sector=self.active_sector, old_seq=self.old_seq, next_seq=self.next_seq,
                    candidate=self.candidate, candidate_state='NEW',
                    writes=[dict(offset=APP1, bytes=len(self.app), sha256=sha(self.app)),
                            dict(offset=self.metadata_offset, bytes=SECTOR, sha256=sha(self.metadata))],
                    before_sha256=self.before_sha256, staged_sha256=self.staged_sha256,
                    expected_sha256=self.expected_sha256)


def plan_bootstrap(before, candidate, candidate_sha256, trust, block_size=0x800):
    """No effects: plan only inactive ota_1 then the inactive 4 KiB record."""
    selected, records = validate_baseline(before, trust)
    description = validate_app(candidate, candidate_sha256)
    require(description['sha256'] != trust.baseline_sha256, 'Candidate is the baseline')
    require(block_size == 0x800, 'Only verified 0x800 USB-OTG geometry; refuse 0x4000/padding')
    old_seq = records[selected].seq
    next_seq = old_seq + 1  # selected ota_0 has odd seq; next even seq chooses ota_1
    require(old_seq < next_seq < UINT32_MAX - 1 and (next_seq - 1) % 2 == 1,
            'Sequence exhaustion/wrap; manual review required')
    metadata = struct.pack('<I20sII', next_seq, records[1 - selected].label, NEW, ota_crc(next_seq))
    metadata = metadata.ljust(SECTOR, b'\xff')
    app = candidate.ljust(APP_SIZE, b'\xff')
    staged = bytearray(before)
    staged[APP1:APP1 + APP_SIZE] = app
    metadata_offset = OTADATA + (1 - selected) * SECTOR
    expected = bytearray(staged)
    expected[metadata_offset:metadata_offset + SECTOR] = metadata
    active_offset = OTADATA + selected * SECTOR
    require(expected[active_offset:active_offset + SECTOR] == before[active_offset:active_offset + SECTOR],
            'Selected record changed')
    require(idf_active((expected[OTADATA:OTADATA + SECTOR],
                        expected[OTADATA + SECTOR:OTADATA + 2 * SECTOR])) == 1 - selected,
            'Candidate record is not selected by IDF')
    return Plan(app, metadata, metadata_offset, selected, old_seq, next_seq,
                sha(before), sha(staged), sha(expected), description)


def validate_running(running, image, slot):
    require(isinstance(running, dict) and running.get('elf_sha256') == image['elf_sha256']
            and running.get('slot') == 'ota_' + str(slot) and running.get('state') == 'valid'
            and running.get('address') == (APP0 if slot == 0 else APP1)
            and running.get('pings', 0) >= 3 and running.get('observed_seconds', 0) >= 15,
            'Exact valid application identity and healthy heartbeat required')


def baseline_description(trust):
    return dict(sha256=trust.baseline_sha256, bytes=trust.baseline_bytes, elf_sha256=trust.baseline_elf)


def validate_qualification(proof, trust, write_task):
    require(isinstance(proof, dict) and proof.get('schema') == 1 and proof.get('kind') == 'qualification'
            and proof.get('task_id') != write_task and proof.get('flash_programming') is False
            and proof.get('entry') == 'PREPARE_UPDATE/ENTER_BOOT'
            and proof.get('exit') == RESET_METHOD and proof.get('app_identity') == APP_IDENTITY
            and proof.get('rom_identity') == ROM_IDENTITY and proof.get('block_size') == 0x800
            and proof.get('transport_version') == TRANSPORT_VERSION
            and proof.get('trust_binding') == trust.binding(), 'Missing/mismatched separate no-write qualification')
    task_id(proof.get('task_id'))
    require(proof.get('before_sha256') == proof.get('after_sha256'), 'Qualification changed flash')
    digest(proof.get('before_sha256'))
    validate_running(proof.get('running_before'), baseline_description(trust), 0)
    validate_running(proof.get('running_after'), baseline_description(trust), 0)


class Journal:
    """One exclusive directory per immutable task; an interrupted claim is spent.

    A trusted supervisor owns the parent directory/approval files. Never delete
    a failed task directory or change IDs to implement a retry. The CLI fixes
    this registry at /var/lib/mixos/bootstrap-ota; offline tests use temp dirs.
    """
    def __init__(self, registry, identifier, kind, approval_sha256):
        self.registry = Path(registry)
        self.identifier = task_id(identifier)
        self.path = self.registry / self.identifier
        self.path.mkdir(mode=0o700, exist_ok=False)
        sync_directory(self.registry)
        self.save('task-claim.json', dict(task_id=identifier, kind=kind,
                                        approval_sha256=digest(approval_sha256)))

    def save(self, name, value):
        require(Path(name).name == name, 'Invalid artifact name')
        return durable_new(self.path / name, encoded(value))

    def snapshot(self, name, data):
        require(len(data) == FLASH_SIZE, 'Incomplete snapshot; no write allowed')
        durable_new(self.path / name, data)
        durable_new(self.path / (name + '.sha256'), (sha(data) + '  ' + name + '\n').encode())

    def consume(self, category, identifier, value):
        require(category in ('qualification-use', 'reset-use'), 'Unknown claim category')
        # Global to the registry, not per destination workdir.
        return durable_new(self.registry / (category + '-' + task_id(identifier) + '.json'), encoded(value))


def qualify(backend, journal, trust):
    """Separate approved no-programming entry/exit test. Reset is an effect."""
    backend.assert_stopped()
    running = backend.healthy_app(baseline_description(trust), 0)
    validate_running(running, baseline_description(trust), 0)
    backend.enter_boot()
    try:
        backend.connect(fresh=True)
        before = backend.read_full('qualification-backup')
        journal.snapshot('original-flash-8MB.bin', before)  # survive ANY later refusal
        validate_baseline(before, trust)
        require(backend.block_size == 0x800, 'Qualification needs 0x800 USB-OTG geometry')
        backend.guard()
        after = backend.read_full('qualification-readback')
        journal.snapshot('qualification-readback-8MB.bin', after)
        require(after == before, 'No-write qualification full-flash comparison failed')
        backend.assert_stopped()
        backend.guard()
        backend.assert_reset_safe()
        journal.consume('reset-use', journal.identifier, dict(expected_sha256=sha(after), method=RESET_METHOD))
        journal.save('reset-claim.json', dict(expected_sha256=sha(after), method=RESET_METHOD))
        backend.watchdog_reset()
    finally:
        backend.close()
    result = backend.healthy_app(baseline_description(trust), 0)
    validate_running(result, baseline_description(trust), 0)
    proof = dict(schema=1, task_id=journal.identifier, kind='qualification', flash_programming=False,
                 entry='PREPARE_UPDATE/ENTER_BOOT', exit=RESET_METHOD, app_identity=APP_IDENTITY,
                 rom_identity=ROM_IDENTITY, block_size=0x800, transport_version=TRANSPORT_VERSION,
                 trust_binding=trust.binding(), before_sha256=sha(before), after_sha256=sha(after),
                 running_before=running, running_after=result)
    journal.save('qualification.json', proof)
    return proof


def validate_execution_evidence(evidence):
    """Validate a new-route binding, not an authority to access a device."""
    if evidence is None:
        return None  # Historical-build callers retain their separate provenance gate.
    require(type(evidence) is dict and set(evidence) == {'route', 'candidate_evidence_sha256'}
            and evidence['route'] == CANDIDATE_ROUTE, 'Invalid candidate execution evidence route')
    digest(evidence['candidate_evidence_sha256'])
    return dict(evidence)


def install(backend, journal, trust, candidate, candidate_sha256, qualification, *, execution_evidence=None):
    """Single attempt. Returns verified flash evidence, NEVER resets/retries.

    Metadata is a commit point. Prior to that point any failure leaves the
    selected ota_0 record untouched. If a write tears after all 32 record bytes
    reached flash, IDF may already choose the fully read-back candidate; an ACK
    timeout cannot undo that. CRC does not protect state against arbitrary bit
    corruption. No stronger atomicity claim is made than the IDF format permits.
    """
    execution_evidence = validate_execution_evidence(execution_evidence)
    validate_app(candidate, candidate_sha256)
    validate_qualification(qualification, trust, journal.identifier)
    backend.assert_stopped()
    journal.consume('qualification-use', qualification['task_id'], dict(write_task=journal.identifier,
                    qualification_sha256=sha(encoded(qualification)), candidate_sha256=candidate_sha256))
    running = backend.healthy_app(baseline_description(trust), 0)
    validate_running(running, baseline_description(trust), 0)
    backend.enter_boot()
    try:
        backend.connect(fresh=True)
        before = backend.read_full('backup')
        journal.snapshot('original-flash-8MB.bin', before)
        plan = plan_bootstrap(before, candidate, candidate_sha256, trust, backend.block_size)
        backend.validate_candidate(candidate)
        backend.assert_stopped()
        backend.guard()
        journal.save('write-claim.json', plan.summary())
        journal.save('app-write-start.json', dict(offset=APP1, bytes=APP_SIZE))
        backend.write_once(APP1, plan.app)
        app_readback = backend.read_region(APP1, APP_SIZE)
        durable_new(journal.path / 'app-readback.bin', app_readback)
        require(app_readback == plan.app, 'Full ota_1 readback mismatch; metadata untouched')
        validate_app(app_readback[:len(candidate)], candidate_sha256)
        # The whole flash is compared BEFORE selecting the candidate, so an
        # out-of-range app erase cannot silently destroy the rollback image.
        staged = backend.read_full('precommit')
        journal.snapshot('precommit-flash-8MB.bin', staged)
        expected = bytearray(before)
        expected[APP1:APP1 + APP_SIZE] = plan.app
        require(staged == expected, 'Precommit full-flash mismatch; metadata untouched')
        backend.assert_stopped()
        backend.guard()
        journal.save('metadata-commit-claim.json', dict(offset=plan.metadata_offset, bytes=SECTOR,
                                                      sha256=sha(plan.metadata), state='NEW'))
        backend.write_once(plan.metadata_offset, plan.metadata)
        metadata = backend.read_region(OTADATA, 2 * SECTOR)
        expected[plan.metadata_offset:plan.metadata_offset + SECTOR] = plan.metadata
        require(metadata == expected[OTADATA:OTADATA + 2 * SECTOR], 'Otadata readback mismatch; no reset')
        after = backend.read_full('final')
        journal.snapshot('flash-readback-8MB.bin', after)
        require(after == expected, 'Final exact 8 MiB comparison failed; no reset')
        backend.guard()
        verified = dict(schema=1, task_id=journal.identifier, kind='verified-install',
                        expected_sha256=sha(after), candidate=plan.candidate,
                        trust_binding=trust.binding(), baseline=baseline_description(trust),
                        plan=plan.summary(),
                        transport=backend.continuity(), reset_sent=False)
        if execution_evidence is not None:
            verified['execution_evidence'] = execution_evidence
        journal.save('verified.json', verified)
        return verified
    finally:
        backend.close()


def boot_baseline(source_verified, stored):
    """Bind preserved A bytes to the approved installation's trust and record.

    Old install receipts without this extra binding remain B-success-only;
    they cannot be retrofitted into fallback permission by the observer.
    """
    baseline = source_verified.get('baseline')
    if baseline is None:
        return None
    require(isinstance(baseline, dict) and set(baseline) == {'sha256', 'bytes', 'elf_sha256'}
            and type(baseline['bytes']) is int and 288 <= baseline['bytes'] <= APP_SIZE,
            'Invalid preserved baseline binding')
    require(len(stored) == FLASH_SIZE and sha(stored) == source_verified['expected_sha256'],
            'Boot baseline requires the exact verified full readback')
    validate_app(stored[APP0:APP0 + baseline['bytes']], baseline['sha256'], baseline['elf_sha256'])
    table = stored[TABLE:TABLE + 0xC00]
    validate_table(table)
    trust = Trust(sha(stored[:TABLE]), sha(table), baseline['sha256'],
                  baseline['bytes'], baseline['elf_sha256'])
    require(trust.binding() == source_verified.get('trust_binding'),
            'Preserved baseline differs from installation trust')
    active = source_verified.get('plan', {}).get('active_sector')
    require(type(active) is int and active in (0, 1), 'Preserved A metadata sector is unknown')
    raw = stored[OTADATA + active * SECTOR:OTADATA + (active + 1) * SECTOR]
    record = Record.parse(raw)
    require(record.idf_valid() and record.state == VALID and record.seq > 0
            and (record.seq - 1) % 2 == 0
            and record.seq == source_verified['plan'].get('old_seq')
            and record.label == b'\xff' * 20 and raw[32:] == b'\xff' * (SECTOR - 32),
            'Original A VALID metadata was not preserved')
    return baseline


def validate_boot_fallback(proof, source_verified, stored):
    """Exact A restoration is useful recovery evidence, NEVER B success."""
    baseline = boot_baseline(source_verified, stored)
    require(type(proof) is dict and validate_execution_evidence(proof.get('execution_evidence'))
            == validate_execution_evidence(source_verified.get('execution_evidence')),
            'Fallback evidence route must match the source installation')
    require(baseline is not None and isinstance(proof, dict)
            and proof.get('kind') == 'boot-only-fallback'
            and proof.get('source_task') == source_verified.get('task_id')
            and proof.get('outcome') == 'baseline-restored'
            and proof.get('expected_sha256') == source_verified.get('expected_sha256')
            and proof.get('baseline') == baseline
            and proof.get('reset_method') == RESET_METHOD
            and proof.get('flash_programming') is False
            and proof.get('candidate_confirmed') is False
            and proof.get('rollback_mechanism_verified') is False,
            'Invalid baseline restoration proof')
    running = proof.get('running', {})
    validate_running(running, baseline, 0)
    before = running.get('pre_identity_heartbeat', {})
    require(running.get('identity_queries') == 1 and before.get('pings', 0) >= 3
            and before.get('observed_seconds', 0) >= 15,
            'Fallback requires single identity query between healthy intervals')
    return baseline


def boot_only(backend, journal, source_path, source_verified, expected_sha256, *, execution_evidence=None):
    """Independent permission: no upload, erase, app write or flash-job retry.

    The supervisor must attest the *source* job is stopped with zero PIDs.
    The stored exact 8 MiB readback and a new device read must both match.
    A reset claim is global per source and is spent before invoking the reset.
    """
    execution_evidence = validate_execution_evidence(execution_evidence)
    require(validate_execution_evidence(source_verified.get('execution_evidence')) == execution_evidence,
            'Boot-only must inherit the exact installation evidence route and candidate binding')
    source_path = Path(source_path)
    require(source_verified.get('schema') == 1 and source_verified.get('kind') == 'verified-install'
            and source_verified.get('task_id') == source_path.name
            and source_verified.get('reset_sent') is False
            and source_verified.get('expected_sha256') == digest(expected_sha256),
            'Boot-only requires exact successful install evidence')
    require(journal.identifier != source_path.name, 'Boot-only must be a separate task')
    stored = (source_path / 'flash-readback-8MB.bin').read_bytes()
    require(len(stored) == FLASH_SIZE and sha(stored) == expected_sha256, 'Stored readback mismatch')
    baseline = boot_baseline(source_verified, stored)
    backend.assert_stopped(source_path.name)
    journal.consume('reset-use', source_path.name, dict(boot_task=journal.identifier,
                    expected_sha256=expected_sha256, method=RESET_METHOD))
    try:
        backend.connect(fresh=False, expected=source_verified['transport'])
        backend.guard()
        live = backend.read_full('boot-only-check')
        journal.snapshot('boot-check-8MB.bin', live)
        require(live == stored, 'Boot-only live full-flash mismatch; reset refused')
        backend.assert_stopped(source_path.name)
        backend.guard()
        backend.assert_reset_safe()
        journal.save('reset-claim.json', dict(source_task=source_path.name,
                     expected_sha256=expected_sha256, method=RESET_METHOD, flash_programming=False))
        backend.watchdog_reset()
    finally:
        backend.close()
    if baseline is None:
        running = backend.healthy_app(source_verified['candidate'], 1)
    else:
        observed = backend.observe_boot(source_verified['candidate'], baseline)
        require(observed.get('outcome') in ('candidate-confirmed', 'baseline-restored'),
                'Unknown boot observation outcome')
        running = observed['running']
        if observed['outcome'] == 'baseline-restored':
            result = dict(kind='boot-only-fallback', source_task=source_path.name,
                          outcome='baseline-restored', expected_sha256=expected_sha256,
                          baseline=baseline, running=running, candidate_confirmed=False,
                          rollback_mechanism_verified=False,
                          reset_method=RESET_METHOD, flash_programming=False)
            if execution_evidence is not None:
                result['execution_evidence'] = execution_evidence
            validate_boot_fallback(result, source_verified, live)
            journal.save('boot-fallback.json', result)
            return result
    validate_running(running, source_verified['candidate'], 1)
    require(running.get('actual_file_verified') is True
            and running.get('measurement', {}).get('actual_file_verified') is True,
            'Boot-only requires v2 actual-running file measurement after reboot')
    if execution_evidence is not None:
        require(running.get('measurement', {}).get('maintenance_health_acknowledged') is True,
                'Candidate route requires maintenance health acknowledgement before success')
    result = dict(kind='boot-only-result', source_task=source_path.name, running=running,
                  reset_method=RESET_METHOD, flash_programming=False)
    if execution_evidence is not None:
        result['execution_evidence'] = execution_evidence
    journal.save('boot-result.json', result)
    return result
