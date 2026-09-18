"""Offline bootstrap safety tests. No serial/network/reset API is executed."""
from dataclasses import replace
import contextlib
import hashlib
import io
import json
from pathlib import Path
import struct
import sys
import tempfile
import types
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
from _mixlib import ota_bootstrap as b
import bootstrap_ota_on_pi as cli
from update_esp import AB_LAYOUT, encode_partition_binary


def app(tag=1):
    header = bytearray(24)
    header[0:4] = bytes([0xE9, 2, 2, 0x3F])
    struct.pack_into('<I', header, 4, 0x40370000)
    struct.pack_into('<H', header, 12, 9)
    struct.pack_into('<H', header, 17, 99)
    header[23] = 1
    descriptor = bytearray(256)
    struct.pack_into('<I', descriptor, 0, 0xABCD5432)
    descriptor[48:80] = b'mixos_esp32s3'.ljust(32, b'\0')
    descriptor[144:176] = bytes([tag]) * 32
    descriptor[180] = 16  # 64 KiB MMU pages
    payload = bytes(descriptor)
    code = bytes([tag]) * 4
    data = (bytes(header) + struct.pack('<II', 0x3C000020, len(payload)) + payload
            + struct.pack('<II', 0x40370000, len(code)) + code)
    checksum = 0xEF
    for byte in payload + code:
        checksum ^= byte
    data += bytes((len(data) | 15) - len(data)) + bytes([checksum])
    return data + hashlib.sha256(data).digest()


def table():
    entries = encode_partition_binary(AB_LAYOUT)[:len(AB_LAYOUT) * 32]
    return (entries + b'\xeb\xeb' + b'\xff' * 14 + hashlib.md5(entries).digest()).ljust(0xC00, b'\xff')


def sector(seq, state=b.VALID, label=b'\xff' * 20):
    return struct.pack('<I20sII', seq, label, state, b.ota_crc(seq)).ljust(b.SECTOR, b'\xff')


def fixture(selected=0, seq=9):
    old = app()
    before = bytearray(b'\xff' * b.FLASH_SIZE)
    before[:16] = b'BOOTLOADER-TEST!'.ljust(16, b'\0')
    before[b.TABLE:b.TABLE + 0xC00] = table()
    before[b.APP0:b.APP0 + len(old)] = old
    before[b.APP1:b.APP1 + 256] = b'OLD-INACTIVE'.ljust(256, b'\0')
    before[0x9000:0x9010] = b'PROTECTED-NVS!!!'
    for index, n in ((selected, seq), (1 - selected, seq - 3)):
        offset = b.OTADATA + index * b.SECTOR
        before[offset:offset + b.SECTOR] = sector(n)
    trust = b.Trust(b.sha(before[:b.TABLE]), b.sha(table()), b.sha(old), len(old), bytes([1] * 32).hex())
    return bytes(before), trust


def running(trust, slot=0, candidate=None):
    image = candidate or b.baseline_description(trust)
    return dict(elf_sha256=image['elf_sha256'], slot='ota_' + str(slot), state='valid',
                address=b.APP0 if slot == 0 else b.APP1, pings=4, observed_seconds=15.1)


def qualification(trust, before, identifier='qualify-offline-0001'):
    return dict(schema=1, task_id=identifier, kind='qualification', flash_programming=False,
                entry='PREPARE_UPDATE/ENTER_BOOT', exit=b.RESET_METHOD, app_identity=b.APP_IDENTITY,
                rom_identity=b.ROM_IDENTITY, block_size=0x800, transport_version=b.TRANSPORT_VERSION,
                trust_binding=trust.binding(), before_sha256=b.sha(before), after_sha256=b.sha(before),
                running_before=running(trust), running_after=running(trust))


class FakeBackend:
    def __init__(self, before, trust, fail=None, corrupt=None, block_size=0x800):
        self.memory = bytearray(before)
        self.trust, self.fail, self.corrupt = trust, fail, corrupt
        self.block_size = block_size
        self.events, self.writes = [], []
        self.closed, self.resets = 0, 0

    def event(self, label):
        self.events.append(label)
        if label == self.fail:
            raise RuntimeError(label)

    def assert_stopped(self, source=None):
        self.event('stopped' if source is None else 'stopped:' + source)

    def healthy_app(self, expected, slot):
        self.event('healthy:' + str(slot))
        result = running(self.trust, slot, expected)
        if slot == 1:
            result.update(actual_file_verified=True, measurement={'actual_file_verified': True})
        return result

    def observe_boot(self, expected, baseline):
        self.event('observe-boot')
        return dict(outcome='candidate-confirmed', running=self.healthy_app(expected, 1))

    def enter_boot(self):
        self.event('enter')

    def connect(self, fresh, expected=None):
        self.event('connect:fresh' if fresh else 'connect:existing')
        if not fresh:
            b.require(expected == self.continuity(), 'continuity mismatch')

    def read_full(self, phase):
        self.event('read:' + phase)
        data = bytearray(self.memory)
        if self.corrupt and self.corrupt[0] == phase:
            data[self.corrupt[1]] ^= 1
        if self.fail == 'short:' + phase:
            return bytes(data[:-1])
        return bytes(data)

    def guard(self):
        self.event('guard')

    def validate_candidate(self, data):
        self.event('candidate-hardware')

    def write_once(self, offset, data):
        label = 'write:app' if offset == b.APP1 else 'write:metadata'
        self.event(label)
        self.writes.append((offset, len(data)))
        self.memory[offset:offset + len(data)] = data
        if self.fail == 'torn:app' and offset == b.APP1:
            self.memory[offset + len(data) - 1] ^= 1
            raise RuntimeError('torn app')
        if self.fail == 'torn:metadata' and offset != b.APP1:
            self.memory[offset:offset + b.SECTOR] = data[:28] + b'\xff' * (b.SECTOR - 28)
            raise RuntimeError('torn metadata')

    def read_region(self, offset, length):
        self.event('read:app' if offset == b.APP1 else 'read:metadata')
        data = bytearray(self.memory[offset:offset + length])
        if self.corrupt and self.corrupt[0] == ('app' if offset == b.APP1 else 'metadata'):
            data[0] ^= 1
        return bytes(data)

    def continuity(self):
        return dict(usb_number='42', identity=b.ROM_IDENTITY, version=b.TRANSPORT_VERSION,
                    stub_sha256=cli.display_transport.STUB_SHA256, device='/dev/offline')

    def assert_reset_safe(self):
        self.event('reset-safe')

    def watchdog_reset(self):
        self.event('reset')
        self.resets += 1

    def close(self):
        self.events.append('close')
        self.closed += 1


class CrcAndSelectionTests(unittest.TestCase):
    def test_idf_crc_against_independent_bitwise_rom_implementation(self):
        for seq in (0, 1, 2, 6, 9, 10, 17, 18, 0xFFFFFFFD, 0xFFFFFFFF):
            # ROM complements the input UINT32_MAX and output, reflected poly.
            crc = 0
            for byte in struct.pack('<I', seq):
                crc ^= byte
                for _ in range(8):
                    crc = (crc >> 1) ^ (0xEDB88320 if crc & 1 else 0)
            self.assertEqual(b.ota_crc(seq), crc ^ 0xFFFFFFFF)
        self.assertEqual(b.ota_crc(0), 0xFFFFFFFF)
        self.assertEqual(b.ota_crc(1), 0x4743989A)

    def test_idf_selection_crc_state_and_tie_behavior(self):
        self.assertEqual(b.idf_active((sector(9), sector(6))), 0)
        self.assertEqual(b.idf_active((sector(6), sector(9))), 1)
        self.assertEqual(b.idf_active((sector(9), sector(9))), 0)
        self.assertEqual(b.idf_active((sector(9), sector(10, b.NEW))), 1)
        for state in (b.INVALID, b.ABORTED):
            self.assertEqual(b.idf_active((sector(9), sector(10, state))), 0)
        self.assertEqual(b.idf_active((sector(9), b'\xff' * b.SECTOR)), 0)
        self.assertIsNone(b.idf_active((b'\xff' * b.SECTOR,) * 2))

    def test_crc_does_not_cover_state_and_unknown_state_is_not_safe_authority(self):
        # Important limitation of IDF's format, not papered over by our tests.
        data = sector(10, b.UINT32_MAX)
        self.assertTrue(b.Record.parse(data).idf_valid())
        self.assertEqual(b.idf_active((sector(9), data)), 1)
        with self.assertRaises(b.Refused):
            b.select_baseline(sector(9) + data)

    def test_strict_planner_rejects_bad_ambiguous_or_pending_records(self):
        cases = [sector(9) + sector(9), sector(0) + sector(6), sector(10) + sector(7),
                 sector(9) + b'\xff' * b.SECTOR, sector(9) + sector(7),
                 sector(0xFFFFFFFD) + sector(2), sector(9) + sector(6, label=b'x' * 20)]
        for state in (b.NEW, b.PENDING_VERIFY, b.INVALID, b.ABORTED, 0xFFFFFFFF, 7):
            cases.append(sector(9) + sector(6, state))
        corrupt = bytearray(sector(9) + sector(6)); corrupt[28] ^= 1; cases.append(bytes(corrupt))
        corrupt = bytearray(sector(9) + sector(6)); corrupt[100] = 0; cases.append(bytes(corrupt))
        for data in cases:
            with self.subTest(first=data[:4].hex()), self.assertRaises(b.Refused):
                b.select_baseline(data)

    def test_prefix_torn_commit_keeps_old_record_or_fully_validated_candidate(self):
        for active in (0, 1):
            before, trust = fixture(active)
            plan = b.plan_bootstrap(before, app(2), b.sha(app(2)), trust)
            old = before[b.OTADATA + active * b.SECTOR:b.OTADATA + (active + 1) * b.SECTOR]
            for count in (*range(33), 0x800, b.SECTOR):
                torn = plan.metadata[:count].ljust(b.SECTOR, b'\xff')
                sectors = [None, None]; sectors[active] = old; sectors[1 - active] = torn
                selected = b.idf_active(sectors)
                self.assertEqual(selected, active if count < 32 else 1 - active)
                self.assertEqual(sectors[active], old)
            # Rollback bootloader changes NEW -> PENDING on first boot; an
            # unconfirmed next boot marks ABORTED and selects untouched ota_0.
            self.assertEqual(b.idf_active((sector(9), sector(10, b.PENDING_VERIFY))), 1)
            self.assertEqual(b.idf_active((sector(9), sector(10, b.ABORTED))), 0)


class PlanningTests(unittest.TestCase):
    def setUp(self):
        self.before, self.trust = fixture()
        self.candidate = app(2)

    def plan(self, before=None, trust=None, **kwargs):
        return b.plan_bootstrap(self.before if before is None else before, self.candidate,
                               b.sha(self.candidate), trust or self.trust, **kwargs)

    def test_exact_inactive_ranges_new_state_and_no_hardcoded_sequence(self):
        for active, seq in ((0, 9), (1, 17), (0, 101)):
            before, trust = fixture(active, seq)
            plan = self.plan(before, trust)
            self.assertEqual(plan.old_seq, seq)
            self.assertEqual(plan.next_seq, seq + 1)
            self.assertEqual(plan.active_sector, active)
            self.assertEqual(plan.metadata_offset, b.OTADATA + (1 - active) * b.SECTOR)
            self.assertEqual(b.Record.parse(plan.metadata).state, b.NEW)
            self.assertEqual(len(plan.metadata), 0x1000)
            self.assertEqual(len(plan.app), 0x1F0000)
            self.assertEqual([v['offset'] for v in plan.summary()['writes']], [b.APP1, plan.metadata_offset])

    def test_geometry_never_pads_metadata_beyond_4k(self):
        for size in (0, 0x400, 0x1000, 0x4000):
            with self.subTest(size=size), self.assertRaisesRegex(b.Refused, '0x800'):
                self.plan(block_size=size)

    def test_sequence_exhaustion_is_refused_without_wrapping(self):
        before, trust = fixture(seq=0xFFFFFFFD)
        with self.assertRaisesRegex(b.Refused, 'exhaustion'):
            self.plan(before, trust)

    def test_unknown_bootloader_table_or_baseline_is_refused(self):
        for offset in (0, 0x7000, b.TABLE, b.APP0 + 70):
            changed = bytearray(self.before); changed[offset] ^= 1
            with self.subTest(offset=offset), self.assertRaises(ValueError):
                self.plan(bytes(changed))
        with self.assertRaises(b.Refused):
            self.plan(trust=replace(self.trust, rollback=False))
        with self.assertRaises(b.Refused):
            self.plan(self.before[:-1])

    def test_full_image_validation_rejects_format_and_digest_corruption(self):
        bad = [self.candidate[:-1], self.candidate + b'x']
        for offset, value in ((0, 0), (1, 0), (12, 8), (23, 0), (32, 0), (36, 1),
                              (80, 0), (28, 0xFC), (len(self.candidate) - 33, 0)):
            changed = bytearray(self.candidate); changed[offset] = value; bad.append(bytes(changed))
        for data in bad:
            with self.subTest(prefix=data[:24].hex()), self.assertRaises(b.Refused):
                b.validate_app(data, b.sha(data))
        with self.assertRaises(b.Refused):
            b.validate_app(self.candidate, '0' * 64)
        with self.assertRaises(b.Refused):
            b.validate_app(self.candidate, b.sha(self.candidate), '0' * 64)

    def test_known_partition_md5_is_required(self):
        changed = bytearray(self.before)
        changed[b.TABLE + 6 * 32 + 16] ^= 1
        trust = replace(self.trust, table_sha256=b.sha(changed[b.TABLE:b.TABLE + 0xC00]))
        with self.assertRaises(ValueError):
            self.plan(bytes(changed), trust)

    def test_valid_image_description_has_elf_identity(self):
        result = b.validate_app(self.candidate, b.sha(self.candidate))
        self.assertEqual(result['elf_sha256'], '02' * 32)

    def test_recovery_app_artifact_when_available_matches_strict_parser(self):
        paths = [ROOT / 'build/esp32s3/current-device-app.bin', ROOT / 'build/esp32s3/mixos_esp32s3.bin']
        for path in paths:
            if path.is_file() and b.sha(path.read_bytes()) == b.BASELINE_SHA256:
                data = path.read_bytes()
                self.assertEqual(len(data), b.BASELINE_BYTES)
                b.validate_app(data, b.BASELINE_SHA256, b.BASELINE_ELF)
                return
        self.skipTest('Exact historical recovery app binary is not present locally')

    def test_address_alignment_mmu_and_entrypoint_checks_survive_rehash(self):
        for offset, fmt, value in ((24, '<I', 0x60000000), (24, '<I', 0x3C000024),
                                   (4, '<I', 0x40000000), (212, '<B', 15),
                                   (288, '<I', 0x4036FFFC)):
            data = bytearray(self.candidate)
            struct.pack_into(fmt, data, offset, value)
            # Recompute the ESP checksum/hash: rejection must be semantic,
            # not merely detecting stale hashes after this mutation.
            checksum, cursor = 0xEF, 24
            for _ in range(data[1]):
                size = struct.unpack_from('<I', data, cursor + 4)[0]
                cursor += 8
                for byte in data[cursor:cursor + size]:
                    checksum ^= byte
                cursor += size
            data[cursor | 15] = checksum
            data[-32:] = hashlib.sha256(data[:-32]).digest()
            with self.subTest(offset=offset, value=value), self.assertRaises(b.Refused):
                b.validate_app(bytes(data), b.sha(data))


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.registry = Path(self.temp.name)
        self.before, self.trust = fixture()
        self.candidate = app(2)
        self.proof = qualification(self.trust, self.before)
        self.count = 0

    def journal(self, kind='install'):
        self.count += 1
        return b.Journal(self.registry, kind + '-test-task-' + str(self.count), kind, 'a' * 64)

    def install(self, backend=None, journal=None, proof=None):
        self.backend = backend or FakeBackend(self.before, self.trust)
        self.job = journal or self.journal()
        return b.install(self.backend, self.job, self.trust, self.candidate, b.sha(self.candidate),
                         self.proof if proof is None else proof)

    def test_success_readback_before_commit_and_no_automatic_reset(self):
        result = self.install()
        events = self.backend.events
        self.assertLess(events.index('stopped'), events.index('enter'))
        self.assertLess(events.index('read:backup'), events.index('write:app'))
        self.assertLess(events.index('candidate-hardware'), events.index('write:app'))
        self.assertLess(events.index('read:app'), events.index('write:metadata'))
        self.assertLess(events.index('read:precommit'), events.index('write:metadata'))
        self.assertLess(events.index('read:metadata'), events.index('read:final'))
        self.assertEqual(self.backend.writes, [(b.APP1, b.APP_SIZE), (b.OTADATA + b.SECTOR, b.SECTOR)])
        self.assertEqual(self.backend.resets, 0)
        self.assertEqual(self.backend.closed, 1)
        self.assertEqual(result['kind'], 'verified-install')
        after = self.backend.memory
        expected = bytearray(self.before)
        plan = b.plan_bootstrap(self.before, self.candidate, b.sha(self.candidate), self.trust)
        expected[b.APP1:] = plan.app
        expected[plan.metadata_offset:plan.metadata_offset + b.SECTOR] = plan.metadata
        self.assertEqual(after, expected)
        self.assertEqual((self.job.path / 'original-flash-8MB.bin').read_bytes(), self.before)
        self.assertTrue((self.job.path / 'verified.json').is_file())

    def test_different_task_cannot_reuse_qualification(self):
        self.install()
        backend = FakeBackend(self.before, self.trust)
        with self.assertRaises(FileExistsError):
            self.install(backend)
        self.assertNotIn('enter', backend.events)
        self.assertEqual(backend.writes, [])

    def test_duplicate_task_is_refused_durably_before_effects(self):
        first = self.journal()
        with self.assertRaises(FileExistsError):
            b.Journal(self.registry, first.identifier, 'install', 'b' * 64)
        self.assertTrue((first.path / 'task-claim.json').exists())

    def test_no_qualification_no_device_effects(self):
        backend = FakeBackend(self.before, self.trust)
        with self.assertRaises(b.Refused):
            self.install(backend, proof={})
        self.assertEqual(backend.events, [])

    def test_qualification_binds_identity_transport_baseline_health_and_no_writes(self):
        changes = dict(flash_programming=True, entry='hardware-buttons', exit='hard-reset',
                       block_size=0x4000, transport_version='4.7.0', trust_binding='b' * 64,
                       after_sha256='b' * 64, rom_identity=dict(b.ROM_IDENTITY, pid='1001'),
                       running_after=dict(running(self.trust), state='pending-verify'))
        for key, value in changes.items():
            with self.subTest(key=key), self.assertRaises(b.Refused):
                b.validate_qualification(dict(self.proof, **{key: value}), self.trust, 'install-other-task')
        with self.assertRaises(b.Refused):
            b.validate_qualification(self.proof, self.trust, self.proof['task_id'])

    def test_all_prewrite_failures_stop_without_writes_or_resets(self):
        for failure in ('stopped', 'healthy:0', 'enter', 'connect:fresh', 'read:backup',
                        'short:backup', 'candidate-hardware', 'guard'):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as root:
                self.registry = Path(root)
                backend = FakeBackend(self.before, self.trust, fail=failure)
                with self.assertRaises((RuntimeError, ValueError)):
                    self.install(backend)
                self.assertEqual(backend.writes, [])
                self.assertEqual(backend.resets, 0)

    def test_full_backup_survives_live_layout_app_and_geometry_refusal(self):
        for offset in (b.TABLE, b.APP0, 0):
            with self.subTest(offset=offset), tempfile.TemporaryDirectory() as root:
                self.registry = Path(root)
                changed = bytearray(self.before); changed[offset] ^= 1
                backend = FakeBackend(changed, self.trust)
                with self.assertRaises(ValueError):
                    self.install(backend)
                self.assertEqual((self.job.path / 'original-flash-8MB.bin').read_bytes(), changed)
                self.assertTrue((self.job.path / 'original-flash-8MB.bin.sha256').exists())
                self.assertEqual(backend.writes, [])
        with tempfile.TemporaryDirectory() as root:
            self.registry = Path(root)
            backend = FakeBackend(self.before, self.trust, block_size=0x4000)
            with self.assertRaises(b.Refused):
                self.install(backend)
            self.assertEqual((self.job.path / 'original-flash-8MB.bin').read_bytes(), self.before)
            self.assertEqual(backend.writes, [])

    def test_backup_fsync_failure_prevents_flash(self):
        journal = self.journal()
        original = b.durable_new
        def fail(path, data):
            if Path(path).name == 'original-flash-8MB.bin.sha256':
                raise OSError('disk failed')
            return original(path, data)
        with mock.patch.object(b, 'durable_new', side_effect=fail), self.assertRaises(OSError):
            self.install(journal=journal)
        self.assertEqual(self.backend.writes, [])
        self.assertEqual((journal.path / 'original-flash-8MB.bin').read_bytes(), self.before)

    def test_failed_claim_fsync_is_spent(self):
        with mock.patch('os.fsync', side_effect=OSError('fsync failure')), self.assertRaises(OSError):
            b.Journal(self.registry, 'one-spent-task', 'install', 'a' * 64)
        with self.assertRaises(FileExistsError):
            b.Journal(self.registry, 'one-spent-task', 'install', 'a' * 64)

    def test_write_readback_and_precommit_failures_never_touch_metadata(self):
        for failure, corrupt in (('write:app', None), ('torn:app', None), ('read:app', None),
                                 (None, ('app', 0)), (None, ('precommit', b.APP0)),
                                 (None, ('precommit', 0x220000)), ('read:precommit', None)):
            with self.subTest(failure=failure, corrupt=corrupt), tempfile.TemporaryDirectory() as root:
                self.registry = Path(root)
                backend = FakeBackend(self.before, self.trust, failure, corrupt)
                with self.assertRaises((RuntimeError, ValueError)):
                    self.install(backend)
                self.assertNotIn('write:metadata', backend.events)
                self.assertEqual(backend.memory[b.OTADATA:b.OTADATA + 2 * b.SECTOR],
                                 self.before[b.OTADATA:b.OTADATA + 2 * b.SECTOR])
                self.assertEqual(backend.resets, 0)

    def test_commit_failure_torn_metadata_and_final_corruption_never_reset(self):
        for failure, corrupt in (('write:metadata', None), ('torn:metadata', None),
                                 (None, ('metadata', 0)), ('read:final', None),
                                 (None, ('final', 0x9000)), (None, ('final', b.APP0)),
                                 (None, ('final', b.OTADATA)), (None, ('final', b.APP1))):
            with self.subTest(failure=failure, corrupt=corrupt), tempfile.TemporaryDirectory() as root:
                self.registry = Path(root)
                backend = FakeBackend(self.before, self.trust, failure, corrupt)
                with self.assertRaises((RuntimeError, ValueError)):
                    self.install(backend)
                self.assertEqual(backend.resets, 0)
                self.assertFalse((self.job.path / 'verified.json').exists())
                self.assertEqual(backend.memory[b.OTADATA:b.OTADATA + b.SECTOR],
                                 self.before[b.OTADATA:b.OTADATA + b.SECTOR])
                if failure == 'torn:metadata':
                    self.assertEqual(b.idf_active((backend.memory[b.OTADATA:b.OTADATA + b.SECTOR],
                        backend.memory[b.OTADATA + b.SECTOR:b.OTADATA + 2 * b.SECTOR])), 0)

    def test_durability_failure_before_commit_never_touches_metadata(self):
        original = b.durable_new
        def fail(path, data):
            if Path(path).name == 'metadata-commit-claim.json':
                raise OSError('disk full')
            return original(path, data)
        with mock.patch.object(b, 'durable_new', side_effect=fail), self.assertRaises(OSError):
            self.install()
        self.assertEqual(self.backend.writes, [(b.APP1, b.APP_SIZE)])
        self.assertEqual(self.backend.resets, 0)

    def test_no_write_qualification_records_round_trip(self):
        backend = FakeBackend(self.before, self.trust)
        job = self.journal('qualify')
        proof = b.qualify(backend, job, self.trust)
        b.validate_qualification(proof, self.trust, 'other-write-task')
        self.assertEqual(backend.writes, [])
        self.assertEqual(backend.resets, 1)
        self.assertLess(backend.events.index('read:qualification-readback'), backend.events.index('reset'))
        self.assertTrue((job.path / 'reset-claim.json').is_file())
        self.assertTrue((job.path / 'qualification.json').is_file())

    def test_failed_qualification_exit_cannot_authorize_install(self):
        backend = FakeBackend(self.before, self.trust, fail='reset')
        job = self.journal('qualify')
        with self.assertRaises(RuntimeError):
            b.qualify(backend, job, self.trust)
        self.assertFalse((job.path / 'qualification.json').exists())
        self.assertTrue((job.path / 'reset-claim.json').exists())
        self.assertEqual(backend.writes, [])

    def test_qualification_corruption_no_reset(self):
        backend = FakeBackend(self.before, self.trust, corrupt=('qualification-readback', 0x9000))
        job = self.journal('qualify')
        with self.assertRaises(b.Refused):
            b.qualify(backend, job, self.trust)
        self.assertEqual(backend.resets, 0)
        self.assertEqual(backend.writes, [])

    def test_boot_only_is_distinct_claim_and_never_reflashes(self):
        verified = self.install()
        source = self.job
        backend = FakeBackend(self.backend.memory, self.trust)
        job = self.journal('boot-only')
        result = b.boot_only(backend, job, source.path, verified, verified['expected_sha256'])
        self.assertEqual(result['kind'], 'boot-only-result')
        self.assertEqual(backend.writes, [])
        self.assertEqual(backend.resets, 1)
        self.assertIn('connect:existing', backend.events)
        self.assertNotIn('connect:fresh', backend.events)
        self.assertNotIn('enter', backend.events)
        self.assertLess(backend.events.index('stopped:' + source.identifier), backend.events.index('reset'))
        backend2 = FakeBackend(self.backend.memory, self.trust)
        with self.assertRaises(FileExistsError):
            b.boot_only(backend2, self.journal('boot-only'), source.path, verified, verified['expected_sha256'])
        self.assertEqual(backend2.writes, [])
        self.assertEqual(backend2.resets, 0)

    def test_boot_only_live_hash_stopped_writer_and_reset_guard_fail_closed(self):
        verified = self.install()
        source = self.job
        for failure, corrupt in (('stopped:' + source.identifier, None),
                                 ('connect:existing', None), ('reset-safe', None),
                                 (None, ('boot-only-check', 0x220000))):
            with self.subTest(failure=failure, corrupt=corrupt), tempfile.TemporaryDirectory() as root:
                self.registry = Path(root)
                backend = FakeBackend(self.backend.memory, self.trust, failure, corrupt)
                with self.assertRaises((RuntimeError, ValueError)):
                    b.boot_only(backend, self.journal('boot-only'), source.path, verified, verified['expected_sha256'])
                self.assertEqual(backend.resets, 0)
                self.assertEqual(backend.writes, [])

    def test_boot_only_wrong_stored_evidence_prevents_connection(self):
        verified = self.install()
        source = self.job
        backend = FakeBackend(self.backend.memory, self.trust)
        with self.assertRaises(b.Refused):
            b.boot_only(backend, self.journal('boot-only'), source.path, verified, '0' * 64)
        self.assertEqual(backend.events, [])
        (source.path / 'flash-readback-8MB.bin').write_bytes(b'bad')
        with self.assertRaises(b.Refused):
            b.boot_only(backend, self.journal('boot-only'), source.path, verified, verified['expected_sha256'])
        self.assertEqual(backend.events, [])


class LocalRecoveryTests(unittest.TestCase):
    def test_real_legacy_package_cannot_authenticate_historical_boot_config(self):
        artifacts = ROOT / 'build/deploy/ota-acceptance-20260916-1749/qualification-package-v9/artifacts'
        receipt = artifacts / 'migration_receipt.json'
        if not receipt.is_file():
            self.skipTest('Archived migration receipt is not present locally')
        raw = receipt.read_bytes()
        self.assertEqual(b.sha(raw), cli.RECOVERED_MIGRATION_SHA256)
        migration = cli.strict_json(raw)
        self.assertEqual(migration['hashes']['bootloader.bin'], b.RECOVERED_BOOT_SHA256)
        self.assertNotIn('bootloader_build_receipt_sha256', migration['artifact_provenance'])
        # The real September 16 config does not become September 13 build evidence.
        files = dict(migration_receipt=raw, boot_config=(artifacts / 'boot_config.json').read_bytes())
        with self.assertRaisesRegex(b.Refused, 'provenance missing'):
            cli.recovery_trust(files)
        report = ROOT / 'build/deploy/ota-acceptance-20260916-1749/bootloader-analysis/semantic-verification.json'
        if report.is_file():
            files['bootloader_build_receipt'] = report.read_bytes()
            with self.assertRaisesRegex(b.Refused, 'provenance missing'):
                cli.recovery_trust(files)
        self.assertEqual(receipt.read_bytes(), raw)

    def test_recorded_recovery_full_snapshot_crc_layout_and_trusted_bootloader(self):
        prefix = ROOT / 'build/deploy/recovery-20260916-024325-readback-8MB.bin'
        if not prefix.is_file():
            self.skipTest('Historical full recovery readback is not present locally')
        flash = prefix.read_bytes()
        self.assertEqual(b.sha(flash), b.RECOVERED_FULL_SHA256)
        boot_end = 24
        for _ in range(flash[1]):
            length = struct.unpack_from('<I', flash, boot_end + 4)[0]
            boot_end += 8 + length
        boot_end = (boot_end | 15) + 1 + (32 if flash[23] else 0)
        self.assertEqual(b.sha(flash[:boot_end]), b.RECOVERED_BOOT_SHA256)
        trust = b.Trust(b.sha(flash[:b.TABLE]), b.RECOVERED_TABLE_SHA256)
        selected, records = b.validate_baseline(flash, trust)
        self.assertEqual(records[selected].seq, 9)  # Historical fixture only; never a planning constant.
        self.assertEqual(records[1 - selected].seq, 6)
        plan = b.plan_bootstrap(flash, app(2), b.sha(app(2)), trust)
        self.assertEqual(plan.next_seq, 10)
        self.assertEqual(b.Record.parse(plan.metadata).state, b.NEW)


class RecoveryEvidenceTests(unittest.TestCase):
    def setUp(self):
        before, trust = fixture()
        bootloader = b'\xe9' + b'BOOT' * 63
        flash = bytearray(before)
        flash[:b.TABLE] = bootloader.ljust(b.TABLE, b'\xff')
        self.flash = bytes(flash)
        self.trust = replace(trust, boot_region_sha256=b.sha(flash[:b.TABLE]))
        verification = dict(readback_sha256=b.sha(flash), app_sha256=trust.baseline_sha256,
                            app_bytes=trust.baseline_bytes, app_elf_sha256=trust.baseline_elf)
        verification.update({key: True for key in ('all_other_flash_unchanged', 'bootloader_unchanged',
            'partition_table_unchanged', 'nvs_and_phy_unchanged', 'otadata_unchanged', 'ota1_unchanged', 'font_unchanged')})
        config = dict(BOOTLOADER_APP_ROLLBACK_ENABLE=True, BOOTLOADER_APP_ANTI_ROLLBACK=False,
                      BOOTLOADER_WDT_ENABLE=True, BOOTLOADER_WDT_TIME_MS=9000, BOOTLOADER_OFFSET_IN_FLASH=0,
                      BOOTLOADER_SKIP_VALIDATE_ALWAYS=False, BOOTLOADER_SKIP_VALIDATE_ON_POWER_ON=False,
                      BOOTLOADER_SKIP_VALIDATE_IN_DEEP_SLEEP=False, EFUSE_VIRTUAL=False)
        boot = dict(device=b.APP_IDENTITY, running=running(trust), heartbeat={})
        # Synthetic history only: this is not a receipt for the real device.
        self.files = dict(recovery_flash=self.flash, recovery_verification=b.encoded(verification),
                          recovery_boot=b.encoded(boot), bootloader=bootloader, boot_config=b.encoded(config),
                          bootloader_source_snapshot=b'SYNTHETIC archived source/config/build scripts',
                          bootloader_build_log=b'SYNTHETIC completed build log')
        receipt = dict(schema='mixos-bootloader-build/v1', target='esp32s3',
                       inputs={'source_snapshot': self.record('bootloader_source_snapshot')},
                       artifacts={name: self.record(name) for name in ('bootloader', 'boot_config')},
                       build=dict(builder='synthetic-builder', command='synthetic-build-command',
                                  toolchain='synthetic-toolchain', log=self.record('bootloader_build_log')))
        self.files['bootloader_build_receipt'] = b.encoded(receipt)
        migration = dict(hashes={'bootloader.bin': b.sha(bootloader)}, artifact_provenance={
            'migrate_to_ab': True, 'bootloader_build_receipt_sha256': b.sha(b.encoded(receipt))})
        self.files['migration_receipt'] = b.encoded(migration)
        original_trust = b.Trust
        patches = [mock.patch.object(cli, 'RECOVERED_MIGRATION_SHA256', b.sha(self.files['migration_receipt'])),
                   mock.patch.multiple(b, RECOVERED_FULL_SHA256=b.sha(flash),
                      RECOVERED_BOOT_SHA256=b.sha(bootloader), RECOVERED_TABLE_SHA256=b.sha(table()),
                      BASELINE_SHA256=trust.baseline_sha256, BASELINE_BYTES=trust.baseline_bytes,
                      BASELINE_ELF=trust.baseline_elf),
                   mock.patch.object(b, 'Trust', side_effect=lambda boot, tab: original_trust(
                       boot, tab, trust.baseline_sha256, trust.baseline_bytes, trust.baseline_elf))]
        for patch in patches:
            patch.start(); self.addCleanup(patch.stop)

    def record(self, name, data=None):
        data = self.files[name] if data is None else data
        return dict(path='historical/' + name, bytes=len(data), sha256=b.sha(data))

    @contextlib.contextmanager
    def synthetic_history(self, files):
        """Model an independently authenticated test history, never real evidence."""
        migration = json.loads(files['migration_receipt'])
        migration['artifact_provenance']['bootloader_build_receipt_sha256'] = b.sha(files['bootloader_build_receipt'])
        files = dict(files, migration_receipt=b.encoded(migration))
        with mock.patch.object(cli, 'RECOVERED_MIGRATION_SHA256', b.sha(files['migration_receipt'])):
            yield files

    def approval_file(self, root, files, kind='qualify'):
        artifacts = {}
        for name, data in files.items():
            path = (root / name).resolve()
            path.write_bytes(data)
            artifacts[name] = dict(path=str(path), sha256=b.sha(data))
        approval = dict(schema=1, approved=True, task_id='synthetic-provenance-test',
                        kind=kind, managed_service=True, artifacts=artifacts)
        raw = b.encoded(approval)
        path = (root / 'approval.json').resolve()
        path.write_bytes(raw)
        return ['--approval', str(path), '--approval-sha256', b.sha(raw)]

    def test_full_recovery_bytes_receipt_config_and_boot_identity_bound(self):
        with mock.patch.object(cli.subprocess, 'run') as run, \
             mock.patch.object(cli, 'PiBackend') as backend:
            self.assertEqual(cli.recovery_trust(self.files), self.trust)
        run.assert_not_called(); backend.assert_not_called()

    def test_valid_synthetic_history_offline_approval_flow(self):
        with tempfile.TemporaryDirectory() as root, \
             mock.patch.object(cli, 'PiBackend') as backend, \
             mock.patch.object(cli.subprocess, 'run') as run, \
             contextlib.redirect_stdout(io.StringIO()) as out:
            args = self.approval_file(Path(root), self.files)
            self.assertEqual(cli.main(args), 0)
            self.assertTrue(json.loads(out.getvalue())['dry_run'])
            self.assertFalse(json.loads(out.getvalue())['device_opened'])
        backend.assert_not_called(); run.assert_not_called()

    def test_unrelated_safe_config_and_even_json_whitespace_are_not_build_outputs(self):
        config = json.loads(self.files['boot_config'])
        config['BOOTLOADER_WDT_TIME_MS'] = 30000  # Safety booleans still identical.
        for data in (b.encoded(config), self.files['boot_config'] + b' ',
                     b.encoded(dict(config, UNRELATED_BUILD_OPTION=True))):
            with self.subTest(data=data), self.assertRaisesRegex(b.Refused, 'mismatch: boot_config'):
                cli.recovery_trust(dict(self.files, boot_config=data))

    def test_each_archived_artifact_is_required_and_exactly_hashed(self):
        names = ('bootloader_build_receipt', 'bootloader_source_snapshot', 'bootloader_build_log',
                 'bootloader', 'boot_config')
        for name in names:
            files = dict(self.files); del files[name]
            with self.subTest(name=name, fault='missing'), self.assertRaisesRegex(b.Refused, name):
                cli.recovery_trust(files)
            for value in (None, b'', self.files[name] + b'!', b'x' * len(self.files[name])):
                with self.subTest(name=name, fault=value), self.assertRaises(b.Refused):
                    cli.recovery_trust(dict(self.files, **{name: value}))

    def test_missing_recorded_provenance_and_hash_only_claims_are_refused(self):
        for provenance in (None, [], {}, {'migrate_to_ab': True},
                           {'migrate_to_ab': True, 'bootloader_build_receipt_sha256': None},
                           {'migrate_to_ab': True, 'bootloader_build_receipt_sha256': 'A' * 64},
                           {'migrate_to_ab': True, 'bootloader_build_receipt_sha256': '0' * 64}):
            migration = json.loads(self.files['migration_receipt'])
            migration['artifact_provenance'] = provenance
            raw = b.encoded(migration)
            with self.subTest(provenance=provenance), \
                 mock.patch.object(cli, 'RECOVERED_MIGRATION_SHA256', b.sha(raw)), \
                 self.assertRaises(b.Refused):
                cli.recovery_trust(dict(self.files, migration_receipt=raw))
        receipt = b.encoded(dict(bootloader_sha256=b.sha(self.files['bootloader']),
                                boot_config_sha256=b.sha(self.files['boot_config']), approved=True))
        with self.synthetic_history(dict(self.files, bootloader_build_receipt=receipt)) as files, \
             self.assertRaisesRegex(b.Refused, 'schema'):
            cli.recovery_trust(files)

    def test_new_self_written_receipt_cannot_retrofit_historical_migration(self):
        original = json.loads(self.files['migration_receipt'])
        del original['artifact_provenance']['bootloader_build_receipt_sha256']
        raw = b.encoded(original)
        # Even a well-formed receipt cannot be attached after the fact.
        with mock.patch.object(cli, 'RECOVERED_MIGRATION_SHA256', b.sha(raw)):
            with self.assertRaisesRegex(b.Refused, 'provenance missing'):
                cli.recovery_trust(dict(self.files, migration_receipt=raw))
            with self.assertRaisesRegex(b.Refused, 'retrospective edits'):
                cli.recovery_trust(self.files)

    def test_build_receipt_schema_and_source_build_provenance_are_strict(self):
        cases = [('schema', None), ('schema', 1), ('schema', True), ('schema', 'mixos-local-build/v1'),
                 ('schema', 'semantic-equivalence/v1'), ('target', 'esp32'),
                 ('inputs', None), ('inputs', {}), ('inputs', []),
                 ('artifacts', None), ('artifacts', {}), ('artifacts', []),
                 ('build', None), ('build', {}), ('build', [])]
        for key, value in cases:
            receipt = json.loads(self.files['bootloader_build_receipt']); receipt[key] = value
            with self.subTest(key=key, value=value), \
                 self.synthetic_history(dict(self.files, bootloader_build_receipt=b.encoded(receipt))) as files, \
                 self.assertRaises(b.Refused):
                cli.recovery_trust(files)
        for field in ('builder', 'command', 'toolchain'):
            for value in (None, '', '  ', True, [], 42):
                receipt = json.loads(self.files['bootloader_build_receipt']); receipt['build'][field] = value
                with self.subTest(field=field, value=value), \
                     self.synthetic_history(dict(self.files, bootloader_build_receipt=b.encoded(receipt))) as files, \
                     self.assertRaisesRegex(b.Refused, 'Recorded builder'):
                    cli.recovery_trust(files)
        for field in ('schema', 'target', 'inputs', 'artifacts', 'build'):
            receipt = json.loads(self.files['bootloader_build_receipt']); del receipt[field]
            with self.subTest(missing=field), \
                 self.synthetic_history(dict(self.files, bootloader_build_receipt=b.encoded(receipt))) as files, \
                 self.assertRaisesRegex(b.Refused, 'schema'):
                cli.recovery_trust(files)

    def test_each_build_record_requires_strict_size_hash_and_historical_label(self):
        for section, name in (('inputs', 'source_snapshot'), ('build', 'log'),
                              ('artifacts', 'bootloader'), ('artifacts', 'boot_config')):
            for field, value in (('path', ''), ('path', None), ('path', []), ('bytes', True),
                                  ('bytes', 0), ('bytes', -1), ('bytes', 2.5), ('bytes', '32'),
                                  ('bytes', 12345), ('sha256', 'A' * 64), ('sha256', '0' * 64),
                                  ('sha256', None), ('sha256', 'bad')):
                receipt = json.loads(self.files['bootloader_build_receipt'])
                receipt[section][name][field] = value
                with self.subTest(record=name, field=field, value=value), \
                     self.synthetic_history(dict(self.files, bootloader_build_receipt=b.encoded(receipt))) as files, \
                     self.assertRaises(b.Refused):
                    cli.recovery_trust(files)
            for value in (None, [], {}, {'path': 'current-file', 'sha256': '0' * 64}):
                receipt = json.loads(self.files['bootloader_build_receipt']); receipt[section][name] = value
                with self.subTest(record=name, value=value), \
                     self.synthetic_history(dict(self.files, bootloader_build_receipt=b.encoded(receipt))) as files, \
                     self.assertRaises(b.Refused):
                    cli.recovery_trust(files)

    def test_provenance_for_a_different_bootloader_cannot_authorize_recovered_bytes(self):
        other = self.files['bootloader'][:-1] + b'!'
        receipt = json.loads(self.files['bootloader_build_receipt'])
        receipt['artifacts']['bootloader'] = self.record('bootloader', other)
        with self.synthetic_history(dict(self.files, bootloader=other,
                                    bootloader_build_receipt=b.encoded(receipt))) as files, \
             self.assertRaisesRegex(b.Refused, 'exact recovered bootloader'):
            cli.recovery_trust(files)

    def test_malformed_nonobject_duplicate_and_invalid_encoding_receipts_fail_closed(self):
        for raw in (b'{', b'null', b'[]', b'true', b'1', b'"text"', b'\xff',
                    b'{"schema":1,"schema":2}', b'{"build":{"log":{},"log":{}}}'):
            with self.subTest(raw=raw), \
                 self.synthetic_history(dict(self.files, bootloader_build_receipt=raw)) as files, \
                 self.assertRaises(b.Refused):
                cli.recovery_trust(files)
            with self.subTest(migration=raw), self.assertRaises(b.Refused):
                cli.recovery_trust(dict(self.files, migration_receipt=raw))

    def test_malformed_config_and_recovery_objects_fail_closed(self):
        for name in ('boot_config', 'recovery_verification', 'recovery_boot'):
            for raw in (b'{', b'null', b'[]', b'true', b'1', b'\xff', b'{"a":1,"a":2}'):
                files = dict(self.files, **{name: raw})
                if name == 'boot_config':
                    receipt = json.loads(files['bootloader_build_receipt'])
                    receipt['artifacts'][name] = self.record(name, raw)
                    files['bootloader_build_receipt'] = b.encoded(receipt)
                with self.subTest(name=name, raw=raw), self.synthetic_history(files) as files, \
                     self.assertRaises(b.Refused):
                    cli.recovery_trust(files)

    def test_cli_refuses_legacy_approvals_before_all_service_and_device_effects(self):
        migration = json.loads(self.files['migration_receipt'])
        del migration['artifact_provenance']['bootloader_build_receipt_sha256']
        legacy = {name: data for name, data in self.files.items() if name not in (
            'bootloader_build_receipt', 'bootloader_source_snapshot', 'bootloader_build_log')}
        legacy['migration_receipt'] = b.encoded(migration)
        for kind in ('qualify', 'install'):
            for recovery in (False, True):
                with self.subTest(kind=kind, recover_service=recovery), tempfile.TemporaryDirectory() as root, \
                     contextlib.ExitStack() as stack:
                    args = self.approval_file(Path(root), legacy, kind) + ['--execute']
                    if recovery:
                        args.append('--recover-service')
                    stack.enter_context(mock.patch.object(cli, 'RECOVERED_MIGRATION_SHA256',
                                                         b.sha(legacy['migration_receipt'])))
                    stack.enter_context(mock.patch.object(cli.sys, 'platform', 'linux'))
                    stack.enter_context(mock.patch.object(cli.sys, 'flags', types.SimpleNamespace(isolated=1)))
                    stack.enter_context(mock.patch.object(cli.os, 'geteuid', return_value=1000, create=True))
                    stack.enter_context(mock.patch.object(cli, 'timeouts_enforced', return_value=True))
                    stack.enter_context(mock.patch.object(cli.shutil, 'which', return_value='/synthetic/fuser'))
                    protected = stack.enter_context(mock.patch.object(cli.font, 'trusted_bytes',
                                                                      side_effect=lambda p: p.read_bytes()))
                    sentinels = [stack.enter_context(mock.patch.object(obj, name)) for obj, name in (
                        (cli, 'verify_code'), (cli, 'verify_state_directory'), (cli, 'verify_unit'),
                        (cli, 'ServiceOwner'), (cli, 'check_service'), (cli, 'PiBackend'),
                        (cli, 'validate_image'), (cli, 'durable_new'), (b, 'Journal'),
                        (cli.native, 'DeviceLock'), (cli.subprocess, 'run'), (cli.esp, 'ports'))]
                    with self.assertRaisesRegex(b.Refused, 'provenance missing'):
                        cli.main(args)
                    self.assertEqual(protected.call_count, len(legacy) + 1)
                    for sentinel in sentinels:
                        sentinel.assert_not_called()

    def test_execute_loads_all_synthetic_provenance_as_protected_approval_artifacts(self):
        # Only platform/ownership boundaries are simulated. Real local_bytes and
        # recovery_trust run, then a sentinel stops before any service/device API.
        with tempfile.TemporaryDirectory() as root, contextlib.ExitStack() as stack:
            args = self.approval_file(Path(root), self.files) + ['--execute']
            stack.enter_context(mock.patch.object(cli.sys, 'platform', 'linux'))
            stack.enter_context(mock.patch.object(cli.sys, 'flags', types.SimpleNamespace(isolated=1)))
            stack.enter_context(mock.patch.object(cli.os, 'geteuid', return_value=1000, create=True))
            stack.enter_context(mock.patch.object(cli, 'timeouts_enforced', return_value=True))
            stack.enter_context(mock.patch.object(cli.shutil, 'which', return_value='/synthetic/fuser'))
            protected = stack.enter_context(mock.patch.object(cli.font, 'trusted_bytes',
                                                              side_effect=lambda p: p.read_bytes()))
            code = stack.enter_context(mock.patch.object(cli, 'verify_code',
                                                        side_effect=b.Refused('synthetic preflight complete')))
            sentinels = [stack.enter_context(mock.patch.object(obj, name)) for obj, name in (
                (cli, 'verify_state_directory'), (cli, 'verify_unit'), (cli, 'ServiceOwner'),
                (cli, 'check_service'), (cli, 'PiBackend'), (cli, 'durable_new'),
                (b, 'Journal'), (cli.native, 'DeviceLock'), (cli.subprocess, 'run'))]
            with self.assertRaisesRegex(b.Refused, 'synthetic preflight complete'):
                cli.main(args)
            code.assert_called_once()
            self.assertEqual({call.args[0].name for call in protected.call_args_list},
                             set(self.files) | {'approval.json'})
            for sentinel in sentinels:
                sentinel.assert_not_called()

    def test_approval_hash_pins_build_receipt_before_provenance_validation(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            args = self.approval_file(root, self.files)
            (root / 'bootloader_build_receipt').write_bytes(b'{}')
            with mock.patch.object(cli, 'recovery_trust') as trust, \
                 self.assertRaisesRegex(b.Refused, 'Artifact hash mismatch'):
                cli.main(args)
            trust.assert_not_called()

    def test_bad_recovery_snapshot_cannot_supply_trust(self):
        files = dict(self.files, recovery_flash=self.flash[:-1])
        with self.assertRaises(b.Refused):
            cli.recovery_trust(files)
        changed = bytearray(self.flash); changed[0x7000] ^= 1
        with self.assertRaises(b.Refused):
            cli.recovery_trust(dict(self.files, recovery_flash=bytes(changed)))

    def test_unbound_bootloader_or_rollback_config_refused(self):
        with self.assertRaises(b.Refused):
            cli.recovery_trust(dict(self.files, bootloader=self.files['bootloader'] + b'\0'))
        for field in ('BOOTLOADER_APP_ROLLBACK_ENABLE', 'BOOTLOADER_APP_ANTI_ROLLBACK',
                      'BOOTLOADER_SKIP_VALIDATE_ALWAYS', 'BOOTLOADER_SKIP_VALIDATE_ON_POWER_ON',
                      'BOOTLOADER_SKIP_VALIDATE_IN_DEEP_SLEEP', 'EFUSE_VIRTUAL', 'BOOTLOADER_WDT_ENABLE',
                      'BOOTLOADER_OFFSET_IN_FLASH'):
            config = json.loads(self.files['boot_config']); config[field] = not config[field]
            data = b.encoded(config)
            receipt = json.loads(self.files['bootloader_build_receipt'])
            receipt['artifacts']['boot_config'] = self.record('boot_config', data)
            files = dict(self.files, boot_config=data, bootloader_build_receipt=b.encoded(receipt))
            with self.subTest(field=field), self.synthetic_history(files) as files, \
                 self.assertRaisesRegex(b.Refused, 'rollback and full image validation'):
                cli.recovery_trust(files)

    def test_partial_recovery_or_wrong_boot_health_does_not_authorize(self):
        for field in ('otadata_unchanged', 'all_other_flash_unchanged', 'bootloader_unchanged'):
            value = json.loads(self.files['recovery_verification']); value[field] = False
            with self.subTest(field=field), self.assertRaises(b.Refused):
                cli.recovery_trust(dict(self.files, recovery_verification=b.encoded(value)))
        boot = json.loads(self.files['recovery_boot']); boot['running']['elf_sha256'] = '0' * 64
        with self.assertRaises(b.Refused):
            cli.recovery_trust(dict(self.files, recovery_boot=b.encoded(boot)))


class AdapterTests(unittest.TestCase):
    def backend(self):
        backend = cli.PiBackend({'kind': 'install'}, None)
        backend.block_size = 0x800
        backend.assert_stopped = mock.Mock()
        backend.guard = mock.Mock()
        backend.esptool = types.SimpleNamespace(ESPLoader=types.SimpleNamespace(CHIP_DETECT_MAGIC_REG_ADDR=1))
        backend.chip = mock.Mock()
        return backend

    def test_actual_wire_helper_submits_each_block_once_and_metadata_exact_4k(self):
        backend = self.backend()
        backend.written = [b.APP1]
        data = sector(10, b.NEW)
        backend.chip.flash_md5sum.return_value = hashlib.md5(data).hexdigest()
        backend.chip.ESP_CMDS = {'FLASH_DATA': 3}
        backend.write_once(b.OTADATA + b.SECTOR, data)
        backend.chip.flash_begin.assert_called_once_with(b.SECTOR, b.OTADATA + b.SECTOR)
        self.assertEqual(backend.chip.check_command.call_count, 2)
        self.assertEqual([len(c.args[2]) for c in backend.chip.check_command.call_args_list], [0x810, 0x810])
        backend.chip.flash_block.assert_not_called()
        backend.chip.flash_finish.assert_not_called()
        backend.chip.hard_reset.assert_not_called()
        backend.chip.watchdog_reset.assert_not_called()

    def test_uncertain_block_is_never_retried_or_reset(self):
        backend = self.backend()
        backend.written = [b.APP1]
        backend.chip.ESP_CMDS = {'FLASH_DATA': 3}
        backend.chip.check_command.side_effect = RuntimeError('uncertain block')
        with self.assertRaises(RuntimeError):
            backend.write_once(b.OTADATA, sector(10, b.NEW))
        self.assertEqual(backend.chip.check_command.call_count, 1)
        self.assertTrue(backend.writing)
        backend.chip.watchdog_reset.assert_not_called()
        with self.assertRaises(b.Refused):
            backend.write_once(b.OTADATA, sector(10, b.NEW))

    def test_adapter_rejects_forbidden_offsets_geometry_and_duplicate(self):
        for offset, size, geometry in ((b.APP0, b.APP_SIZE, 0x800), (0, 0x8000, 0x800),
            (0x8000, b.SECTOR, 0x800), (0x9000, b.SECTOR, 0x800), (0x210000, 0x400000, 0x800),
            (b.OTADATA, 0x2000, 0x800), (b.APP1, b.APP_SIZE, 0x4000)):
            backend = self.backend(); backend.block_size = geometry
            with self.subTest(offset=offset, geometry=geometry), self.assertRaises(b.Refused):
                backend.write_once(offset, b'\xff' * size)
            backend.chip.flash_begin.assert_not_called()

    def test_reset_only_permission_and_claim_required(self):
        backend = self.backend()
        with self.assertRaises(b.Refused):
            backend.watchdog_reset()
        backend.chip.watchdog_reset.assert_not_called()

    def test_reset_uses_official_watchdog_and_only_volatile_force_flag(self):
        with tempfile.TemporaryDirectory() as root:
            job = b.Journal(root, 'boot-only-offline', 'boot-only', 'a' * 64)
            job.save('reset-claim.json', {'method': b.RESET_METHOD})
            backend = cli.PiBackend(dict(kind='boot-only', reset_method=b.RESET_METHOD,
                                       allow_clear_force_download=True), job)
            backend.guard = mock.Mock()
            chip = mock.Mock(RTC_CNTL_OPTION1_REG=1, RTC_CNTL_FORCE_DOWNLOAD_BOOT_MASK=4)
            chip.read_reg.side_effect = [4, 0]
            backend.chip = chip
            backend.watchdog_reset()
            chip.write_reg.assert_called_once_with(1, 0, 4)
            chip.watchdog_reset.assert_called_once_with()
            chip.hard_reset.assert_not_called()
            chip.flash_begin.assert_not_called()
            chip.run_stub.assert_not_called()

    def test_service_unknown_control_pid_or_active_refused(self):
        for fields in ('', 'LoadState=not-found\nActiveState=inactive\nSubState=dead\nMainPID=0\nControlPID=0',
                       'LoadState=loaded\nActiveState=inactive\nSubState=dead\nMainPID=0\nControlPID=9',
                       'LoadState=loaded\nActiveState=active\nSubState=running\nMainPID=4\nControlPID=0'):
            result = types.SimpleNamespace(returncode=0, stdout=fields)
            with mock.patch.object(cli.subprocess, 'run', return_value=result), self.assertRaises(b.Refused):
                cli.check_service('mixosd.service')

    def test_service_stopped_status_checked_with_timeout(self):
        result = types.SimpleNamespace(returncode=0, stdout=
            'LoadState=loaded\nActiveState=inactive\nSubState=dead\nMainPID=0\nControlPID=0')
        with mock.patch.object(cli.subprocess, 'run', return_value=result) as run:
            cli.check_service('mixosd.service')
        self.assertEqual(run.call_args.kwargs['timeout'], 10)

    def test_cli_default_and_help_have_no_device_or_subprocess_effects(self):
        with mock.patch.object(cli, 'PiBackend') as backend, mock.patch.object(cli.subprocess, 'run') as run, \
             mock.patch.object(cli.esp, 'ports') as ports, contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(cli.main([]), 0)
            self.assertTrue(json.loads(out.getvalue())['dry_run'])
            with self.assertRaises(SystemExit) as exit_code:
                cli.main(['--help'])
            self.assertEqual(exit_code.exception.code, 0)
        backend.assert_not_called(); run.assert_not_called(); ports.assert_not_called()

    def test_execute_without_approval_is_rejected_before_hardware(self):
        with mock.patch.object(cli, 'PiBackend') as backend, self.assertRaises(b.Refused):
            cli.main(['--execute'])
        backend.assert_not_called()

    def test_duplicate_json_fields_rejected(self):
        with self.assertRaises(b.Refused):
            cli.strict_json(b'{"approved":false,"approved":true}')

    def connect_backend(self, *, existing=False, swapped=False, bad_geometry=False):
        backend = self.backend()
        backend.dev, backend.identity = '/dev/offline', b.ROM_IDENTITY
        backend._identity_check = mock.Mock()
        backend._usb_number = mock.Mock(return_value='42')
        backend._security = mock.Mock()
        port = mock.Mock(closed=False)
        port.fileno.return_value = 7
        rom = mock.Mock(IS_STUB=False, sync_stub_detected=existing)
        rom._port = port
        stub = mock.Mock(IS_STUB=True, FLASH_WRITE_SIZE=0x4000 if bad_geometry else 0x800)
        stub._port = mock.Mock() if swapped else port
        stub.flash_id.return_value = 0x174020
        rom.run_stub.return_value = stub
        rom.STUB_CLASS.return_value = stub
        backend.chip = None
        return backend, rom, stub

    def connect_with_mocks(self, backend, rom, fresh, expected=None):
        connect = mock.Mock(return_value=rom)
        with mock.patch.dict(sys.modules, {'esptool.cmds': types.SimpleNamespace(connect_esp=connect)}), \
             mock.patch.object(cli.esp, 'idle_port'), \
             mock.patch.object(cli.esp, 'wait_port', return_value=('/dev/offline', b.ROM_IDENTITY)), \
             mock.patch.object(cli.os, 'fstat', return_value=types.SimpleNamespace(st_rdev=55)):
            backend.connect(fresh=fresh, expected=expected)
        connect.assert_called_once_with(port='/dev/offline', connect_attempts=1, open_port_attempts=1,
                                        initial_baud=115200, chip='esp32s3', before='no-reset')

    def test_adapter_fresh_rom_upload_preserves_handle_and_checks_security(self):
        backend, rom, stub = self.connect_backend()
        self.connect_with_mocks(backend, rom, True)
        backend._security.assert_called_once_with()
        rom.run_stub.assert_called_once_with()
        self.assertIs(backend.original_port, stub._port)
        self.assertIs(backend.chip, stub)
        self.assertEqual(backend.block_size, 0x800)
        stub.flash_set_parameters.assert_called_once_with(b.FLASH_SIZE)
        backend.guard.assert_called_once_with()

    def test_stale_stub_or_replaced_handle_refused(self):
        for existing, swapped in ((True, False), (False, True)):
            backend, rom, stub = self.connect_backend(existing=existing, swapped=swapped)
            with self.subTest(existing=existing, swapped=swapped), self.assertRaises(b.Refused):
                self.connect_with_mocks(backend, rom, True)
            stub.flash_spi_attach.assert_not_called()
            if existing:
                rom.run_stub.assert_not_called()
            else:
                stub._port.close.assert_called_once_with()

    def test_boot_only_adapter_reuses_only_verified_stub_and_never_uploads(self):
        backend, rom, stub = self.connect_backend(existing=True)
        backend.usb_number = '42'
        self.connect_with_mocks(backend, rom, False, backend.continuity())
        rom.run_stub.assert_not_called()
        rom.STUB_CLASS.assert_called_once_with(rom)
        self.assertIs(backend.chip, stub)
        backend, rom, stub = self.connect_backend(existing=False)
        backend.usb_number = '42'
        with self.assertRaises(b.Refused):
            self.connect_with_mocks(backend, rom, False, backend.continuity())
        rom.run_stub.assert_not_called()

    def test_boot_only_adapter_different_usb_enumeration_refused_before_connect(self):
        backend, rom, stub = self.connect_backend(existing=True)
        backend.usb_number = '43'
        with self.assertRaises(b.Refused):
            self.connect_with_mocks(backend, rom, False, backend.continuity())
        rom.run_stub.assert_not_called()
        rom.STUB_CLASS.assert_not_called()

    def test_usb_jtag_and_wrong_mac_cannot_pass_identity_guard(self):
        backend = self.backend()
        for value in (dict(b.ROM_IDENTITY, pid='1001'), dict(b.ROM_IDENTITY, serial='70:04:1d:d8:54:15'),
                      dict(b.ROM_IDENTITY, location='5-1.1')):
            with mock.patch.object(cli, 'physical_identity', return_value=value), self.assertRaises(b.Refused):
                backend._identity_check()

    def test_uncertain_app_write_latch_forbids_later_metadata(self):
        backend = self.backend()
        backend.written = [b.APP1]
        backend.writing = True
        with self.assertRaises(b.Refused):
            backend.write_once(b.OTADATA, sector(10, b.NEW))
        backend.chip.flash_begin.assert_not_called()

    def test_current_handle_security_is_uncached_and_mac_bound(self):
        backend = self.backend()
        backend.chip.get_security_info.return_value = dict(flags=0, chip_id=9, flash_crypt_cnt=0)
        backend.chip.read_mac.return_value = bytes.fromhex('70041dd85414')
        backend._security()
        backend.chip.get_security_info.assert_called_once_with(cache=False)
        for key, value in (('flags', 1), ('chip_id', 8), ('flash_crypt_cnt', 1)):
            backend.chip.get_security_info.return_value = dict(flags=0, chip_id=9, flash_crypt_cnt=0, **{})
            backend.chip.get_security_info.return_value[key] = value
            with self.subTest(key=key), self.assertRaises(b.Refused):
                backend._security()

    def test_bounded_read_short_chunk_refused(self):
        backend = self.backend()
        backend.chip.read_flash.return_value = b'bad'
        with self.assertRaises(b.Refused):
            backend.read_region(0, b.FLASH_SIZE)
        backend.chip.read_flash.assert_called_once_with(0, cli.READ_CHUNK)

    def test_boot_only_never_uploads_stub_in_implementation(self):
        # Runtime fault ordering is exercised above. The hardware constructor
        # is only used with mocked chip handles in this offline test suite.
        import inspect
        source = inspect.getsource(cli.PiBackend.connect)
        self.assertIn("before='no-reset'", source)
        self.assertIn('connect_attempts=1', source)
        self.assertNotIn('hard_reset(', source)


if __name__ == '__main__':
    unittest.main()
