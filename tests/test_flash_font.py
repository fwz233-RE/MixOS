"""Hardware-free font updater boundary and backup checks."""
import hashlib
import inspect
from pathlib import Path
import struct
import sys
import contextlib
import io
import json
import shlex
import subprocess
import tempfile
import time
import types
import unittest
from unittest import mock

TOOLS = Path(__file__).resolve().parents[1] / 'tools'
sys.path.insert(0, str(TOOLS))
import flash_font_on_pi as updater


def font():
    tags = [b'cmap', b'glyf', b'loca', b'head', b'hhea', b'hmtx', b'maxp']
    start = 12 + 16 * len(tags)
    return (struct.pack('>IHHHH', 0x10000, len(tags), 0, 0, 0) +
            b''.join(struct.pack('>4sIII', tag, 0, start + i * 4, 4) for i, tag in enumerate(tags)) +
            b'\0' * (4 * len(tags)))


def partition_table(layout='legacy'):
    rows = {
        'legacy': [(1, 2, 0x9000, 0x6000, b'nvs'), (1, 1, 0xf000, 0x1000, b'phy_init'),
                   (0, 0, 0x10000, 0x200000, b'factory'), (1, 0x82, 0x210000, 0x400000, b'font')],
        'ab': [(1, 2, 0x9000, 0x6000, b'nvs'), (1, 1, 0xf000, 0x1000, b'phy_init'),
               (0, 0x10, 0x10000, 0x1f0000, b'ota_0'), (1, 0, 0x200000, 0x2000, b'otadata'),
               (1, 0x82, 0x210000, 0x400000, b'font'), (0, 0x11, 0x610000, 0x1f0000, b'ota_1')],
    }[layout]
    entries = b''.join(struct.pack('<HBBII16sI', 0x50aa, kind, subtype, offset, size, label, 0)
                       for kind, subtype, offset, size, label in rows)
    md5 = b'\xeb\xeb' + b'\xff' * 14 + hashlib.md5(entries).digest()
    return (entries + md5).ljust(0xc00, b'\xff')


class FontUpdateTests(unittest.TestCase):
    def validate(self, data):
        return updater.validate_font(data, hashlib.sha256(data).hexdigest())

    def test_partition_padding(self):
        data = font()
        padded = self.validate(data)
        self.assertEqual(len(padded), 0x400000)
        self.assertEqual(padded[:len(data)], data)
        self.assertEqual(set(padded[len(data):]), {255})
        self.assertEqual(updater.FONT_OFFSET, 0x210000)

    def test_font_default_never_selects_app(self):
        payload = self.validate(font())
        table = partition_table()
        self.assertEqual(updater.write_regions(payload, table), [(0x210000, payload)])
        with self.assertRaises(ValueError):
            updater.write_regions(payload[:-1], table)

    def test_explicit_app_selection_is_bounded(self):
        payload = self.validate(font())
        table = partition_table()
        app = b'\xe9' + b'A' * 255
        regions = updater.write_regions(payload, table, app)
        self.assertEqual([r[0] for r in regions], [0x10000, 0x210000])
        self.assertEqual(len(regions[0][1]), 0x200000)
        self.assertEqual(regions[0][1][:len(app)], app)
        self.assertEqual(0x10000 + len(regions[0][1]), 0x210000)
        with self.assertRaises(ValueError):
            updater.write_regions(payload, table, b'\xe9' * (0x200000 + 1))

    def test_app_slot_shrinks_to_the_ab_table_so_otadata_survives(self):
        payload = self.validate(font())
        app = b'\xe9' + b'A' * 255
        regions = dict(updater.write_regions(payload, partition_table('ab'), app))
        self.assertEqual(len(regions[0x10000]), 0x1f0000)
        # An app padded to the old 0x200000 would erase otadata at 0x200000.
        self.assertEqual(0x10000 + len(regions[0x10000]), 0x200000)
        with self.assertRaises(ValueError):
            updater.write_regions(payload, partition_table('ab'), b'\xe9' * (0x1f0000 + 1))

    def test_migration_plans_ordered_regions_that_spare_nvs_and_font(self):
        payload = self.validate(font())
        table = partition_table('ab')
        app = b'\xe9' + b'A' * 255
        boot = b'\xe9' + b'B' * 255
        regions = updater.write_regions(payload, table, app, boot)
        # App first (still bootable under the old table), font next, then the
        # switch-over: otadata, table, and the bootloader last.
        self.assertEqual([offset for offset, _ in regions],
                         [0x10000, 0x210000, 0x200000, 0x8000, 0x0])
        planned = dict(regions)
        self.assertEqual(planned[0x200000], b'\xff' * 0x2000)
        self.assertEqual(planned[0x8000][:0xc00], table)
        self.assertEqual(set(planned[0x8000][0xc00:]), {0xff})
        self.assertEqual(len(planned[0x8000]), 0x1000)
        self.assertEqual(planned[0x0][:len(boot)], boot)
        self.assertEqual(len(planned[0x0]), 0x8000)
        # nvs (0x9000) and phy_init (0xf000) fall in no planned region at all.
        for reserved in (0x9000, 0xf000):
            self.assertFalse(any(offset <= reserved < offset + len(data) for offset, data in regions))

    def test_migration_refuses_a_factory_only_table_or_a_missing_app(self):
        payload = self.validate(font())
        boot = b'\xe9' + b'B' * 255
        app = b'\xe9' + b'A' * 255
        with self.assertRaises(ValueError):
            updater.write_regions(payload, partition_table(), app, boot)
        with self.assertRaises(ValueError):
            updater.write_regions(payload, partition_table('ab'), None, boot)
        for bad in (b'', b'\xaa' + b'B' * 255, b'\xe9' * (0x8000 + 1)):
            with self.subTest(bootloader=bad[:2]), self.assertRaises(ValueError):
                updater.write_regions(payload, partition_table('ab'), app, bad)

    def test_wrong_digest(self):
        with self.assertRaises(ValueError):
            updater.validate_font(font(), '0' * 64)

    def test_reject_non_ttf_and_truncated(self):
        for data in (b'', font()[:30], b'OTTO' + font()[4:], b'ttcf' + font()[4:]):
            with self.subTest(header=data[:4]), self.assertRaises(ValueError):
                self.validate(data)

    def test_reject_oversize(self):
        with self.assertRaises(ValueError):
            self.validate(font() + b'\0' * 0x400000)

    def test_reject_outside_table(self):
        data = bytearray(font())
        struct.pack_into('>I', data, 20, 0xfffffff0)
        with self.assertRaises(ValueError):
            self.validate(data)

    def test_reject_table_in_directory(self):
        data = bytearray(font())
        struct.pack_into('>I', data, 20, 12)
        with self.assertRaises(ValueError):
            self.validate(data)

    def test_reject_duplicate_and_missing_tables(self):
        data = bytearray(font())
        data[28:32] = data[12:16]
        with self.assertRaises(ValueError):
            self.validate(data)
        data = bytearray(font())
        data[12:16] = b'abcd'
        with self.assertRaises(ValueError):
            self.validate(data)

    def test_available_deployment_artifacts_pass_actual_preflight(self):
        root = TOOLS.parent
        old = root / 'build/esp32s3/previous-mixos_esp32s3.bin'
        new = root / 'firmware/esp32s3/build/mixos_esp32s3.bin'
        table = new.parent / 'partition_table/partition-table.bin'
        face = root / 'build/font/MiSans-Normal-gb2312.ttf'
        if not all(path.is_file() for path in (old, new, table, face)):
            self.skipTest('Local deployment build artifacts are not present')
        partition = updater.validate_partitions(root / 'firmware/esp32s3/partitions.csv')
        updater.validate_image(old, '9593904eab8c1bcaaef9c383abc9a5308ae4beeece94989436360289eafc675b',
                               'esp32s3', partition)
        updater.validate_image(new, hashlib.sha256(new.read_bytes()).hexdigest(), 'esp32s3', partition)
        updater.validate_table(table.read_bytes())
        self.validate(face.read_bytes())
        print(json.dumps({'offline_artifact_sha256': {
            str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in (TOOLS / 'flash_font_on_pi.py', TOOLS / 'deploy_display.py', new, face)
        }}, sort_keys=True))

    def test_live_snapshot_checks_layout_and_app(self):
        table = partition_table()
        app = b'\xe9' + b'A' * 255
        flash = bytearray(b'\xff' * 0x800000)
        flash[0] = 0xe9
        flash[0x8000:0x8c00] = table
        flash[0x10000:0x10100] = app
        flash[0x210000:0x210004] = b'\0\1\0\0'
        updater.validate_backup(flash, table, app)
        with self.assertRaises(ValueError):
            updater.validate_backup(flash[:-1], table, app)
        with self.assertRaises(ValueError):
            updater.validate_backup(flash, table, app + b'B')
        with self.assertRaises(ValueError):
            updater.validate_backup(flash, b'X' * 0xc00, app)

    def test_migration_requires_a_legacy_device_and_the_ab_table(self):
        app = b'\xe9' + b'A' * 255
        flash = bytearray(b'\xff' * 0x800000)
        flash[0] = 0xe9
        flash[0x10000:0x10100] = app
        flash[0x210000:0x210004] = b'\0\1\0\0'
        flash[0x8000:0x8c00] = partition_table()
        # A legacy device with the staged A/B table is exactly the migration.
        self.assertEqual(updater.validate_backup(flash, partition_table('ab'), app, True)['name'], 'legacy')
        # Migrating needs the new table staged, not the one already running.
        with self.assertRaises(ValueError):
            updater.validate_backup(flash, partition_table(), app, True)
        # A device that is already A/B has nothing to migrate.
        flash[0x8000:0x8c00] = partition_table('ab')
        with self.assertRaises(ValueError):
            updater.validate_backup(flash, partition_table('ab'), app, True)

    def test_migration_readback_names_the_ab_result(self):
        before = bytearray(bytes(range(256)) * 32768)
        after = bytearray(before)
        after[0x8000:0x8c00] = partition_table('ab')
        after[0x200000:0x202000] = b'\xff' * 0x2000
        self.assertEqual(updater.verify_migrated(after, before),
                         {'layout': 'ab', 'boot_slot': 'ota_0', 'otadata': 'blank',
                          'carried_over': ['nvs', 'phy_init']})
        unblanked = bytearray(after)
        unblanked[0x200000] = 0
        with self.assertRaises(RuntimeError):
            updater.verify_migrated(unblanked, before)
        moved = bytearray(after)
        moved[0x9000] ^= 1
        with self.assertRaises(RuntimeError):
            updater.verify_migrated(moved, before)
        stale = bytearray(after)
        stale[0x8000:0x8c00] = partition_table()
        with self.assertRaises(ValueError):
            updater.verify_migrated(stale, before)


class FakeChip:
    IS_STUB = False
    sync_stub_detected = False
    FLASH_WRITE_SIZE = 0x4000
    ESP_CMDS = {'FLASH_DATA': 3}

    def __init__(self, before, failure=None, corrupt=None):
        self.memory = bytearray(before)
        self.failure, self.corrupt = failure, corrupt
        self._port = types.SimpleNamespace(close=mock.Mock(), timeout=None, write_timeout=None)
        self.attached = False
        self.read_calls = []
        self.begins, self.blocks, self.reads, self.resets = [], 0, 0, 0
        self.address = 0

    def get_security_info(self):
        return {'flags': 0, 'chip_id': 9, 'flash_crypt_cnt': 0}

    def read_mac(self):
        return bytes.fromhex('70041dd85414')

    def run_stub(self):
        assert not self.IS_STUB and not self.sync_stub_detected
        assert self._port.timeout == self._port.write_timeout == 10
        self.IS_STUB = True
        return self

    def flash_spi_attach(self, value):
        assert value == 0
        self.attached = True

    def change_baud(self, baud):
        pass

    def flash_id(self):
        assert self.attached
        return 0x174020

    def flash_set_parameters(self, size):
        assert size == 0x800000

    def read_flash(self, offset, size, progress_fn=None):
        assert self.attached and self._port.timeout == self._port.write_timeout == 10
        assert size == 0x40000 and offset % size == 0 and offset + size <= 0x800000
        self.read_calls.append((offset, size))
        if offset == 0:
            self.reads += 1
        if self.reads == 1 and self.failure == 'backup_timeout':
            raise TimeoutError('backup stream timeout')
        if self.reads == 2 and self.failure == 'readback':
            raise RuntimeError('readback disconnected')
        result = bytes(self.memory[offset:offset + size])
        if self.reads == 2 and self.corrupt is not None and offset <= self.corrupt < offset + size:
            data = bytearray(result)
            data[self.corrupt - offset] ^= 1
            result = bytes(data)
        if progress_fn:
            progress_fn(size, size, offset)
        return result

    def flash_begin(self, size, address):
        self.begins.append((address, size))
        if self.failure == 'erase':
            raise RuntimeError('erase failed')
        self.address = address
        self.memory[address:address + size] = b'\xff' * size

    def checksum(self, data):
        return 0

    def check_command(self, description, opcode, packet, checksum, timeout):
        assert opcode == self.ESP_CMDS['FLASH_DATA']
        self.blocks += 1
        if self.failure == 'block':
            raise RuntimeError('uncertain write')
        size, seq, zero1, zero2 = struct.unpack_from('<IIII', packet)
        assert size == len(packet) - 16 and zero1 == zero2 == 0
        offset = self.address + seq * self.FLASH_WRITE_SIZE
        self.memory[offset:offset + size] = packet[16:]

    def flash_block(self, *args):
        raise AssertionError('Retrying esptool API must never be called')

    def read_reg(self, *args, **kwargs):
        pass

    def flash_md5sum(self, address, size):
        if self.failure == 'md5':
            return '0' * 32
        return hashlib.md5(self.memory[address:address + size]).hexdigest()

    def hard_reset(self):
        self.resets += 1


class FontSessionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.work = Path(self.temp.name)
        self.table = partition_table()
        self.old = b'\xe9' + b'A' * 255
        self.new = b'\xe9' + b'B' * 511
        self.payload = updater.validate_font(font(), hashlib.sha256(font()).hexdigest())
        self.before = bytearray(bytes(range(256)) * 32768)
        self.before[0] = 0xe9
        self.before[0x8000:0x8c00] = self.table
        self.before[0x10000:0x10100] = self.old
        self.before[0x210000:0x210004] = b'\0\1\0\0'
        self.events = []
        self.identity = {'serial': '70:04:1d:d8:54:14', 'location': '5-1.2'}

    def run_session(self, chip, new=True, bootloader=None, table=None):
        connect = mock.Mock(return_value=chip)
        stub_flasher = type('StubFlasher', (), {'STUB_SUBDIRS': ['1', '2']})
        tool = types.SimpleNamespace(__version__=updater.ESPTOOL_VERSION,
                                     ESPLoader=types.SimpleNamespace(CHIP_DETECT_MAGIC_REG_ADDR=0))
        modules = {'esptool': tool, 'esptool.cmds': types.SimpleNamespace(connect_esp=connect),
                   'esptool.loader': types.SimpleNamespace(StubFlasher=stub_flasher)}
        def audit(event, **details):
            self.events.append(event)
            if event == 'display_write_start':
                self.assertTrue((self.work / 'original-flash-8MB.bin.sha256').exists())
                self.assertEqual((self.work / 'original-flash-8MB.bin').read_bytes(), self.before)
        with mock.patch.dict(sys.modules, modules), \
                mock.patch.object(updater.esp, 'ESP_ENV', {'PYTHONPATH': str(self.work)}), \
                mock.patch.object(updater.esp, 'ROOT', self.work), \
                mock.patch.object(updater.esp, 'idle_port'), \
                mock.patch.object(updater.esp, 'audit', side_effect=audit), \
                mock.patch.object(updater, 'physical_identity', return_value=self.identity), \
                contextlib.redirect_stdout(io.StringIO()):
            updater.session('/dev/fake', self.identity, self.payload,
                            self.table if table is None else table, self.old,
                            self.new if new else None, bootloader)
        connect.assert_called_once_with(port='/dev/fake', connect_attempts=3, open_port_attempts=1,
                                        initial_baud=115200, chip='esp32s3', before='no-reset')
        self.assertEqual(stub_flasher.STUB_SUBDIRS, ['2'])

    def test_exact_combined_full_flash_and_protected_ranges(self):
        chip = FakeChip(self.before)
        self.run_session(chip)
        expected = bytearray(self.before)
        expected[0x10000:0x210000] = self.new.ljust(0x200000, b'\xff')
        expected[0x210000:0x610000] = self.payload
        self.assertEqual(chip.memory, expected)
        self.assertEqual(chip.begins, [(0x10000, 0x200000), (0x210000, 0x400000)])
        self.assertEqual(chip.reads, 2)
        self.assertEqual(len(chip.read_calls), 64)
        self.assertIn('font_backup_read_complete', self.events)
        self.assertIn('font_readback_read_complete', self.events)
        self.assertEqual(chip.resets, 1)
        chip._port.close.assert_called_once()
        self.assertIn('display_readback_verified', self.events)

    def test_stale_stub_rejected_before_upload_or_flash(self):
        for attribute in ('IS_STUB', 'sync_stub_detected'):
            with self.subTest(attribute=attribute):
                chip = FakeChip(self.before)
                setattr(chip, attribute, True)
                with mock.patch.object(chip, 'run_stub') as upload:
                    with self.assertRaises(RuntimeError):
                        self.run_session(chip)
                upload.assert_not_called()
                self.assertFalse(chip.attached)
                self.assertEqual(chip.reads, 0)
                self.assertEqual(chip.begins, [])
                self.assertEqual(chip.resets, 0)
                chip._port.close.assert_called_once()

    def test_new_stub_wrapper_retains_original_port_through_readback(self):
        rom = FakeChip(self.before)
        stub = FakeChip(self.before)
        original_port = rom._port
        stub._port = original_port
        stub.IS_STUB = True
        with mock.patch.object(rom, 'run_stub', return_value=stub) as upload:
            self.run_session(rom)
        upload.assert_called_once_with()
        self.assertIs(stub._port, original_port)
        self.assertEqual(stub.reads, 2)
        self.assertEqual(stub.resets, 1)
        self.assertEqual(rom.reads, 0)
        original_port.close.assert_called_once()

    def test_untrusted_stub_result_rejected_before_flash_access(self):
        for changed_port in (False, True):
            with self.subTest(changed_port=changed_port):
                rom = FakeChip(self.before)
                stub = FakeChip(self.before)
                if changed_port:
                    stub.IS_STUB = True
                else:
                    stub._port = rom._port  # A ROM result is not a successfully loaded stub.
                with mock.patch.object(rom, 'run_stub', return_value=stub):
                    with self.assertRaises(RuntimeError):
                        self.run_session(rom)
                self.assertFalse(stub.attached)
                self.assertEqual(stub.reads, 0)
                self.assertEqual(stub.begins, [])
                self.assertEqual(stub.resets, 0)
                rom._port.close.assert_called_once()

    def test_live_session_accepts_uppercase_rom_mac(self):
        self.identity['serial'] = '70:04:1D:D8:54:14'
        chip = FakeChip(self.before)
        self.run_session(chip)
        self.assertEqual(chip.resets, 1)
        self.assertIn('display_readback_verified', self.events)

    def test_font_only_preserves_entire_old_app_partition(self):
        chip = FakeChip(self.before)
        chip.FLASH_WRITE_SIZE = 0x800  # ESP32-S3 USB-OTG stub geometry.
        self.run_session(chip, new=False)
        self.assertEqual(chip.memory[:0x210000], self.before[:0x210000])
        self.assertEqual(chip.begins, [(0x210000, 0x400000)])

    def test_migration_writes_ab_layout_and_carries_nvs_and_phy_init_across(self):
        chip = FakeChip(self.before)
        chip.FLASH_WRITE_SIZE = 0x800  # The migration needs USB-OTG stub geometry.
        boot = b'\xe9' + b'C' * 4095
        table = partition_table('ab')
        self.run_session(chip, bootloader=boot, table=table)
        expected = bytearray(self.before)
        expected[0x10000:0x200000] = self.new.ljust(0x1f0000, b'\xff')
        expected[0x210000:0x610000] = self.payload
        expected[0x200000:0x202000] = b'\xff' * 0x2000
        expected[0x8000:0x9000] = table.ljust(0x1000, b'\xff')
        expected[0:0x8000] = boot.ljust(0x8000, b'\xff')
        self.assertEqual(chip.memory, expected)
        self.assertEqual(chip.begins, [(0x10000, 0x1f0000), (0x210000, 0x400000),
                                       (0x200000, 0x2000), (0x8000, 0x1000), (0x0, 0x8000)])
        # The preferences and the 4 MiB font partition boundary are untouched.
        self.assertEqual(chip.memory[0x9000:0x10000], self.before[0x9000:0x10000])
        self.assertEqual(chip.resets, 1)
        self.assertIn('display_readback_verified', self.events)

    def test_migration_refuses_a_block_size_that_would_overrun_nvs(self):
        chip = FakeChip(self.before)  # Default 0x4000 geometry.
        with self.assertRaisesRegex(ValueError, 'USB-OTG stub geometry'):
            self.run_session(chip, bootloader=b'\xe9' + b'C' * 4095, table=partition_table('ab'))
        self.assertEqual(chip.begins, [])
        self.assertEqual(chip.resets, 0)

    def test_migration_stops_on_a_device_that_is_already_ab(self):
        chip = FakeChip(self.before)
        chip.FLASH_WRITE_SIZE = 0x800
        table = partition_table('ab')
        chip.memory[0x8000:0x8c00] = table  # Already migrated.
        with self.assertRaises(ValueError):
            self.run_session(chip, bootloader=b'\xe9' + b'C' * 4095, table=table)
        self.assertEqual(chip.begins, [])
        self.assertEqual(chip.resets, 0)

    def test_write_failure_never_retries_or_resets(self):
        chip = FakeChip(self.before, failure='block')
        with self.assertRaises(RuntimeError):
            self.run_session(chip)
        self.assertEqual(chip.blocks, 1)
        self.assertEqual(len(chip.begins), 1)
        self.assertEqual(chip.resets, 0)
        chip._port.close.assert_called_once()

    def test_erase_hash_and_readback_failures_are_terminal(self):
        for failure in ('erase', 'md5', 'readback'):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as temp:
                self.work = Path(temp)
                chip = FakeChip(self.before, failure=failure)
                with self.assertRaises(RuntimeError):
                    self.run_session(chip)
                self.assertEqual(chip.resets, 0)
                chip._port.close.assert_called_once()

    def test_full_readback_catches_protected_app_font_corruption(self):
        for offset in (0x1000, 0x9000, 0x11000, 0x220000, 0x700000):
            with self.subTest(offset=offset), tempfile.TemporaryDirectory() as temp:
                self.work = Path(temp)
                chip = FakeChip(self.before, corrupt=offset)
                with self.assertRaises(RuntimeError):
                    self.run_session(chip)
                self.assertEqual(chip.resets, 0)

    def test_old_app_mismatch_and_extra_partition_stop_before_erase(self):
        chip = FakeChip(self.before)
        chip.memory[0x10001] ^= 1
        with self.assertRaises(ValueError):
            self.run_session(chip)
        self.assertEqual(chip.begins, [])
        chip = FakeChip(self.before)
        self.table = self.table[:160] + b'X' + self.table[161:]
        with self.assertRaises(ValueError):
            self.run_session(chip)
        self.assertEqual(chip.begins, [])

    def test_resume_artifact_ownership_and_private_root_marker(self):
        path = mock.Mock(spec=Path)
        path.resolve.return_value = path
        path.lstat.return_value = types.SimpleNamespace(st_mode=0o100600, st_uid=0, st_nlink=1)
        with mock.patch.object(updater, 'trusted_package'):
            self.assertEqual(updater.trusted_bytes(path, read=False), b'')
        path.read_bytes.assert_not_called()  # Prior start-claim is root-only mode 0600.
        for uid, mode, links in ((1000, 0o100644, 1), (0, 0o100666, 1), (0, 0o100644, 2)):
            path.lstat.return_value = types.SimpleNamespace(st_mode=mode, st_uid=uid, st_nlink=links)
            with self.subTest(uid=uid, mode=mode, links=links), self.assertRaises(ValueError):
                updater.trusted_bytes(path)

    def test_backup_timeout_is_terminal_without_write(self):
        chip = FakeChip(self.before, failure='backup_timeout')
        with self.assertRaises(TimeoutError):
            self.run_session(chip)
        self.assertEqual(chip.begins, [])
        self.assertEqual(len(chip.read_calls), 1)
        self.assertEqual(chip.resets, 0)
        chip._port.close.assert_called_once()
        self.assertIn('font_backup_read_start', self.events)

    def test_read_progress_geometry_is_bound_to_requested_chunk(self):
        for total, offset in ((0x40000, 0x40000), (0x20000, 0)):
            with self.subTest(total=total, offset=offset):
                chip = FakeChip(self.before)
                def read(address, size, progress_fn):
                    progress_fn(size, total, offset)
                    return bytes(chip.memory[address:address + size])
                with mock.patch.object(chip, 'read_flash', side_effect=read):
                    with self.assertRaisesRegex(ValueError, 'progress geometry'):
                        self.run_session(chip)
                self.assertEqual(chip.begins, [])
                self.assertEqual(chip.resets, 0)
                chip._port.close.assert_called_once()

    def test_operation_timer_raises_without_retry(self):
        """The bound is real where SIGALRM exists, and never silently swallows errors.

        This used to patch signal.signal/setitimer directly, which made the
        test itself unable to run on a platform without SIGALRM  -- the same
        platform where the implementation had been quietly broken.
        """
        from _mixlib import guards

        if guards.timeouts_enforced():
            with self.assertRaises(TimeoutError):
                with guards.operation_timeout('test read', 0.05):
                    time.sleep(2)
        else:
            self.skipTest('SIGALRM timers are unavailable on this platform')

    def test_operation_timeout_propagates_the_bodys_own_error(self):
        from _mixlib import guards

        with self.assertRaises(ZeroDivisionError):
            with guards.operation_timeout('test read', 30):
                1 / 0

    def test_backup_durability_failure_prevents_erase(self):
        chip = FakeChip(self.before)
        with mock.patch.object(updater, 'durable_new', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                self.run_session(chip)
        self.assertEqual(chip.begins, [])
        self.assertEqual(chip.resets, 0)


class DirectAuthorizationTests(unittest.TestCase):
    def setUp(self):
        self.app = {'vid': '303a', 'pid': '80c3', 'serial': 'TD0720', 'location': '5-1.2'}
        self.rom = dict(self.app, pid='1001', serial='70:04:1d:d8:54:14')

    def select(self, ports):
        with mock.patch.object(updater.esp, 'ports', return_value=ports):
            return updater.select_direct_device('TD0720', '5-1.2', '70:04:1D:D8:54:14')

    def test_exact_application_and_reviewed_rom_selection(self):
        for identity in (self.app, self.rom, dict(self.rom, pid='0009'),
                         dict(self.rom, serial='70:04:1D:D8:54:14')):
            with self.subTest(identity=identity):
                self.assertEqual(self.select([('/dev/other', dict(self.app, location='5-1.1')),
                                              ('/dev/exact', identity)]),
                                 ('/dev/exact', identity, identity != self.app))

    def test_wrong_or_ambiguous_identity_rejected(self):
        identities = [dict(self.app, serial='td0720'), dict(self.app, vid='0483'),
                      dict(self.app, location='5-1.1'), dict(self.app, pid='1002'),
                      dict(self.rom, serial='70041DD85414'),
                      dict(self.rom, serial='70:04:1D:D8:54:14 '),
                      dict(self.rom, serial='70:04:1d:d8:54:15'), dict(self.rom, vid='0483')]
        for ports in ([], [('/dev/one', self.app), ('/dev/two', self.rom)],
                      *[[('/dev/wrong', identity)] for identity in identities]):
            with self.subTest(ports=ports), self.assertRaises(ValueError):
                self.select(ports)

    def test_direct_handshake_needs_only_host_prepare_and_device_ready(self):
        import update_esp
        state = update_esp.Maintenance()
        hello = update_esp.Frame(update_esp.C.CONTROL, update_esp.T.HELLO, 42, sequence=1,
                                payload=struct.pack('<HH', 512, 4096)).encode()
        ready = update_esp.Frame(update_esp.C.MAINTENANCE, update_esp.T.UPDATE_READY,
                                42, state.request, 2).encode()
        port = mock.MagicMock()
        port.__enter__.return_value = port
        port.read.side_effect = [hello, ready]
        writes = []
        def write(data):
            writes.append(bytes(data))
            return len(data)
        port.write.side_effect = write
        with mock.patch.object(update_esp, 'Maintenance', return_value=state), \
                mock.patch.object(updater.esp, 'open_port', return_value=port), \
                mock.patch.object(updater, 'physical_identity', return_value=self.app), \
                mock.patch.object(updater.esp, 'enter_download') as local_confirmation, \
                mock.patch('builtins.input', side_effect=AssertionError('No local prompt permitted')), \
                mock.patch.object(updater.esp, 'audit') as audit:
            updater.enter_download_direct('/dev/exact', self.app)
        local_confirmation.assert_not_called()
        frames = list(update_esp.Decoder().feed(b''.join(writes)))
        self.assertEqual([frame.type for frame in frames],
                         [update_esp.T.HELLO_ACK, update_esp.T.PREPARE_UPDATE, update_esp.T.ENTER_BOOT])
        self.assertEqual([frame.session for frame in frames[1:]], [state.request, state.request])
        self.assertEqual(audit.call_args_list, [
            mock.call('display_host_prepare_queued', request=state.request, epoch=42),
            mock.call('display_host_enter_boot_sent', request=state.request, epoch=42)])
        self.assertFalse(any('local' in call.args[0] or 'screen' in call.args[0]
                             for call in audit.call_args_list))
        port.flush.assert_called_once_with()

    def test_missing_host_grant_fails_without_enter_boot_or_local_prompt(self):
        import update_esp
        state = update_esp.Maintenance(clock=lambda: 0)
        port = mock.MagicMock()
        port.__enter__.return_value = port
        port.read.return_value = b''
        with mock.patch.object(update_esp, 'Maintenance', return_value=state), \
                mock.patch.object(updater.esp, 'open_port', return_value=port), \
                mock.patch.object(updater, 'physical_identity', return_value=self.app), \
                mock.patch.object(updater.time, 'monotonic', side_effect=[0, 0, 26]), \
                mock.patch.object(updater.esp, 'enter_download') as local_confirmation, \
                mock.patch('builtins.input', side_effect=AssertionError('No local prompt permitted')):
            with self.assertRaisesRegex(RuntimeError, 'grant unavailable'):
                updater.enter_download_direct('/dev/exact', self.app)
        self.assertFalse(state.boot_queued)
        port.write.assert_not_called()
        local_confirmation.assert_not_called()


class ResumeNoWriteTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.package = self.base / 'mixos-display-20260910-175627'
        self.source = self.package / 'work/session'
        self.source.mkdir(parents=True)
        self.destination = self.base / 'new-job/work/session'
        self.destination.mkdir(parents=True)
        self.identity = {'vid': '303a', 'pid': '80c3', 'serial': 'TD0720', 'location': '5-1.2'}
        self.rom = {'vid': '303a', 'pid': '1001', 'serial': '70:04:1d:d8:54:14', 'location': '5-1.2'}
        data = {'font.ttf': font(), 'firmware/esp32s3/build/mixos_esp32s3.bin': b'old-app',
                'new-app.bin': b'new-app', 'partition-table.bin': partition_table(),
                'firmware/esp32s3/partitions.csv': b'fixed csv'}
        self.requested = {name: updater.sha(value) for name, value in data.items()}
        for name, content in data.items():
            path = self.package / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
        (self.package / 'start-claim').write_text('Single launch only')
        command = ['exec', 'runuser', '-u', 'pi', '--', '/usr/bin/python3', '-I', '-u',
                   str(self.package / 'tools/flash_font_on_pi.py'), '--execute',
                   '--workdir', str(self.source), '--sha256', self.requested['font.ttf'],
                   '--app-sha256', self.requested['firmware/esp32s3/build/mixos_esp32s3.bin'],
                   '--new-app-sha256', self.requested['new-app.bin'],
                   '--partition-sha256', self.requested['partition-table.bin']]
        (self.package / 'launch.sh').write_text('#!/bin/bash\n' + shlex.join(command) + '\n')
        (self.source / 'font-job-claim.json').write_text('original single job')
        self.records = [
            {'event': 'font_preflight_ok', 'identity': self.identity,
             'font_sha256': self.requested['font.ttf'],
             'app_sha256': self.requested['firmware/esp32s3/build/mixos_esp32s3.bin']},
            {'event': 'isolated_esptool_verified'},
            {'event': 'awaiting_local_screen_confirmation'},
            {'event': 'mixos_authorized_enter_boot_sent'},
            {'event': 'font_rom_security_verified', 'mac': self.rom['serial'],
             'flags': 0, 'flash_crypt_cnt': 0, 'chip_id': 9}]
        self.save_audit()
        self.status = subprocess.CompletedProcess([], 0, 'LoadState=loaded\nActiveState=inactive\nMainPID=0\nControlPID=0\n')
        patches = [mock.patch.object(updater, 'PACKAGE_BASE', self.base),
                   mock.patch.object(updater.esp, 'ROOT', self.destination),
                   mock.patch.object(updater, 'ROOT', self.destination.parent.parent),
                   mock.patch.object(updater, 'trusted_package'),
                   mock.patch.object(updater, 'trusted_bytes', side_effect=lambda p, read=True: p.read_bytes() if read else b''),
                   mock.patch.object(updater.subprocess, 'run', side_effect=lambda *a, **k: self.status),
                   mock.patch.object(updater.esp, 'ports', return_value=[('/dev/fake', self.rom)]),
                   mock.patch.object(updater.esp, 'audit')]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def save_audit(self):
        (self.source / 'flash-audit.jsonl').write_text('\n'.join(json.dumps(r) for r in self.records) + '\n')

    def evidence(self):
        return updater.resume_evidence(self.source, self.requested, 'TD0720', '5-1.2', self.rom['serial'])

    def claim(self):
        return updater.claim_resume(self.source, self.requested, 'TD0720', '5-1.2', self.rom['serial'])

    def test_exact_prior_consent_claimed_once_without_altering_audit(self):
        before = (self.source / 'flash-audit.jsonl').read_bytes()
        self.assertEqual(self.claim(), ('/dev/fake', self.rom))
        claim = json.loads((self.source / 'resume-claim.json').read_text())
        self.assertEqual(claim['requested_hashes'], self.requested)
        self.assertEqual(claim['prior_audit_sha256'], updater.sha(before))
        self.assertEqual((self.source / 'flash-audit.jsonl').read_bytes(), before)
        with self.assertRaises(ValueError):
            self.claim()

    def test_any_write_or_unknown_event_forbids_resume(self):
        for event in ('display_write_start', 'app_write_start', 'display_chip_hash_verified',
                      'display_readback_verified', 'unknown_recovery'):
            with self.subTest(event=event):
                self.records.append({'event': event})
                self.save_audit()
                with self.assertRaises(ValueError):
                    self.evidence()
                self.records.pop()

    def test_write_claim_or_readback_file_forbids_resume(self):
        for name in ('write-claim.json', 'flash-readback-8MB.bin', 'resume-claim.json'):
            path = self.source / name
            path.write_bytes(b'proof')
            with self.subTest(name=name), self.assertRaises(ValueError):
                self.evidence()
            path.unlink()

    def test_active_or_unverifiable_previous_service_forbids_resume(self):
        for status in ('LoadState=loaded\nActiveState=active\nMainPID=123\nControlPID=0\n',
                       'LoadState=loaded\nActiveState=inactive\nMainPID=0\nControlPID=123\n', ''):
            self.status.stdout = status
            with self.subTest(status=status), self.assertRaises(ValueError):
                self.evidence()

    def test_artifact_or_immutable_launch_mismatch_forbids_resume(self):
        for name in self.requested:
            path = self.package / name
            original = path.read_bytes()
            path.write_bytes(original + b'changed')
            with self.subTest(name=name), self.assertRaises(ValueError):
                self.evidence()
            path.write_bytes(original)
        launch = self.package / 'launch.sh'
        launch.write_text(launch.read_text().replace(self.requested['new-app.bin'], '0' * 64))
        with self.assertRaises(ValueError):
            self.evidence()

    def test_missing_consent_identity_or_security_forbids_resume(self):
        changes = [(0, 'identity', dict(self.identity, serial='OTHER')),
                   (3, 'event', 'font_aborted'), (4, 'flags', 1), (4, 'chip_id', 8),
                   (4, 'flash_crypt_cnt', 1), (4, 'mac', '00:00:00:00:00:00')]
        for index, key, value in changes:
            original = self.records[index][key]
            self.records[index][key] = value
            self.save_audit()
            with self.subTest(key=key), self.assertRaises(ValueError):
                self.evidence()
            self.records[index][key] = original

    def test_reviewed_rom_pids_and_mac_case_equivalence(self):
        for pid in ('0009', '1001'):
            for observed in ('70:04:1d:d8:54:14', '70:04:1D:D8:54:14'):
                for expected in ('70:04:1d:d8:54:14', '70:04:1D:D8:54:14'):
                    with self.subTest(pid=pid, observed=observed, expected=expected):
                        identity = dict(self.rom, pid=pid, serial=observed)
                        with mock.patch.object(updater.esp, 'ports', return_value=[('/dev/fresh', identity)]):
                            result = updater.claim_resume(self.source, self.requested, 'TD0720',
                                                          '5-1.2', expected)
                        self.assertEqual(result, ('/dev/fresh', identity))
                        claim = json.loads((self.source / 'resume-claim.json').read_text())
                        self.assertEqual(claim['rom_serial'], '70:04:1d:d8:54:14')
                        self.assertEqual(claim['current_rom_identity'], identity)
                        # Each subcase represents a separate fixture, not an operational retry.
                        (self.source / 'resume-claim.json').unlink()

    def test_mac_case_equivalence_does_not_relax_identity(self):
        for identity in (dict(self.rom, serial='70:04:1D:D8:54:15'),
                         dict(self.rom, serial='70041DD85414'),
                         dict(self.rom, serial='70:04:1D:D8:54:14 '),
                         dict(self.rom, vid='0483'),
                         dict(self.rom, pid='1002'),
                         dict(self.rom, location='5-1.1')):
            with self.subTest(identity=identity), \
                    mock.patch.object(updater.esp, 'ports', return_value=[('/dev/fresh', identity)]):
                with self.assertRaises(ValueError):
                    self.claim()
                self.assertFalse((self.source / 'resume-claim.json').exists())

    def test_wrong_or_ambiguous_current_rom_does_not_consume_source(self):
        for ports in ([], [('/dev/one', self.rom), ('/dev/two', self.rom)],
                      [('/dev/one', dict(self.rom, serial='OTHER'))],
                      [('/dev/one', dict(self.rom, pid='80c3'))]):
            with mock.patch.object(updater.esp, 'ports', return_value=ports), self.assertRaises(ValueError):
                self.claim()
            self.assertFalse((self.source / 'resume-claim.json').exists())

    def test_claim_fsync_error_stops_without_replay(self):
        with mock.patch.object(updater.os, 'fsync', side_effect=OSError('disk failure')):
            with self.assertRaises(OSError):
                self.claim()
        self.assertTrue((self.source / 'resume-claim.json').exists())
        with self.assertRaises(ValueError):
            self.claim()

    def run_main(self, mode='resume', execute=True):
        current = updater.ROOT
        for name in self.requested:
            target = current / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes((self.package / name).read_bytes())
        self.destination.rmdir()  # The real main must create a fresh exclusive workdir.
        argv = ['flash_font_on_pi.py', '--workdir', str(self.destination),
                '--sha256', self.requested['font.ttf'],
                '--app-sha256', self.requested['firmware/esp32s3/build/mixos_esp32s3.bin'],
                '--new-app-sha256', self.requested['new-app.bin'],
                '--partition-sha256', self.requested['partition-table.bin']]
        if execute:
            argv.append('--execute')
        if mode == 'resume':
            argv.extend(['--resume-no-write-source', str(self.source)])
        def service(command, **kwargs):
            if command[1] == 'is-active':
                return subprocess.CompletedProcess(command, 3, 'inactive\n')
            return self.status
        with mock.patch.object(sys, 'argv', argv), \
                mock.patch.object(sys, 'platform', 'linux'), \
                mock.patch.object(updater, 'device_lock', mock.MagicMock()), \
                mock.patch.object(updater.os, 'geteuid', return_value=1000, create=True), \
                mock.patch.object(updater.Path, 'home', return_value=self.base), \
                mock.patch.object(updater, 'validate_image'), \
                mock.patch.object(updater, 'validate_partitions', return_value={}), \
                mock.patch.object(updater.shutil, 'which', return_value='/usr/bin/fuser'), \
                mock.patch.object(updater.subprocess, 'run', side_effect=service), \
                mock.patch.object(updater, 'prepare_esptool') as prepare, \
                mock.patch.object(updater.esp, 'enter_download') as consent, \
                mock.patch.object(updater, 'enter_download_direct') as direct, \
                mock.patch.object(updater, 'select_direct_device', return_value=(
                    '/dev/app' if mode == 'app' else '/dev/fake',
                    self.identity if mode == 'app' else self.rom, mode != 'app')) as select, \
                mock.patch.object(updater.esp, 'audit') as audit, \
                mock.patch('builtins.input', side_effect=AssertionError('No local prompt permitted')), \
                mock.patch.object(updater, 'session') as session, \
                mock.patch.object(updater.esp, 'wait_port', side_effect=(
                    [('/dev/fake', self.rom), ('/dev/app', self.identity)] if mode == 'app'
                    else [('/dev/app', self.identity)])) as wait, \
                mock.patch.object(updater.esp, 'verify_running', return_value={'pings': 3}), \
                contextlib.redirect_stdout(io.StringIO()):
            updater.main()
        consent.assert_not_called()
        if not execute:
            session.assert_not_called()
            prepare.assert_not_called()
            direct.assert_not_called()
            select.assert_not_called()
            wait.assert_not_called()
            audit.assert_not_called()
            self.assertFalse(self.destination.exists())
            return
        session.assert_called_once()
        # Bind the arguments by name. Indexing args[-1] silently started
        # reading the `bootloader` parameter when it was added, and the
        # assertion could not fail on a machine where this test never ran.
        bound = inspect.signature(updater.session).bind(
            *session.call_args.args, **session.call_args.kwargs)
        bound.apply_defaults()
        self.assertEqual((bound.arguments['dev'], bound.arguments['identity']),
                         ('/dev/fake', self.rom))
        self.assertEqual(bound.arguments['new_app'], b'new-app')
        self.assertIsNone(bound.arguments['bootloader'])
        self.assertIs(bound.arguments['reset_rom'], False)
        self.assertEqual((self.source / 'resume-claim.json').is_file(), mode == 'resume')
        self.assertTrue((self.destination / 'font-job-claim.json').is_file())
        authorization = audit.call_args_list[0]
        self.assertEqual(authorization.args, ('display_host_execute_authorized',))
        self.assertIs(authorization.kwargs['screen_confirmation_required'], False)
        if mode == 'app':
            direct.assert_called_once_with('/dev/app', self.identity)
        else:
            direct.assert_not_called()
        if mode == 'resume':
            select.assert_not_called()
        else:
            select.assert_called_once_with('TD0720', '5-1.2', self.rom['serial'])

    def test_main_resume_skips_duplicate_consent_but_runs_fresh_session(self):
        self.run_main()

    def test_main_direct_rom_requires_no_local_confirmation_or_reset(self):
        self.run_main('rom')

    def test_main_application_uses_host_authorization_without_local_confirmation(self):
        self.run_main('app')

    def test_main_dry_run_never_selects_or_opens_hardware(self):
        self.run_main('rom', execute=False)

    def test_noncanonical_source_and_consent_reordering_forbid_resume(self):
        with self.assertRaises(ValueError):
            updater.resume_evidence(self.source / '..', self.requested, 'TD0720', '5-1.2', self.rom['serial'])
        self.records[2], self.records[3] = self.records[3], self.records[2]
        self.save_audit()
        with self.assertRaises(ValueError):
            self.evidence()


if __name__ == '__main__':
    unittest.main()
