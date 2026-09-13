"""Offline checks for the app-only flash safety gates; never opens hardware.

These exercise pure validation logic (snapshot shape, partition table
agreement, image headers). The module under test no longer imports fcntl at
startup, so the checks run on any platform instead of skipping everywhere but
Linux.
"""
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
import update_esp


def table_image(layout='ab'):
    """A real partition-table image for the named audited layout.

    The fixture used to be ``bytes([0xAA]) * 0xC00``, which stopped being
    parseable once verify_snapshot began identifying the live layout. Building
    the table from the same definition the tool validates against keeps the
    fixture honest.
    """
    return update_esp.encode_partition_binary(update_esp.LAYOUTS[layout]['rows'])


class FlashChecks(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import flash_esp_on_pi
        cls.updater = flash_esp_on_pi

    def snapshot(self, layout='ab'):
        """An 8 MiB flash image that verify_snapshot should accept."""
        data = bytearray(b'\xff' * 0x800000)
        table = table_image(layout)
        data[0] = data[0x10000] = 0xE9
        data[0x8000:0x8C00] = table
        data[0x210000:0x210004] = b'\x00\x01\x00\x00'
        return data, table

    def test_valid_snapshot(self):
        for layout in ('legacy', 'ab'):
            with self.subTest(layout=layout):
                data, table = self.snapshot(layout)
                live = self.updater.verify_snapshot(data, table, migrate=False)
                self.assertEqual(live['name'], layout)

    def test_layout_mismatch_requires_migrate(self):
        """A legacy device offered an A/B table must be told to migrate."""
        data, _ = self.snapshot('legacy')
        ab_table = table_image('ab')
        with self.assertRaises(ValueError) as caught:
            self.updater.verify_snapshot(data, ab_table, migrate=False)
        self.assertIn('--migrate', str(caught.exception))
        live = self.updater.verify_snapshot(data, ab_table, migrate=True)
        self.assertEqual(live['name'], 'legacy')

    def test_unreadable_live_table(self):
        data, table = self.snapshot()
        data[0x8000:0x8C00] = b'\xAA' * 0xC00
        with self.assertRaises(ValueError):
            self.updater.verify_snapshot(data, table, migrate=True)

    def test_missing_font(self):
        data, table = self.snapshot()
        data[0x210000:0x210004] = b'\xff' * 4
        with self.assertRaises(ValueError):
            self.updater.verify_snapshot(data, table, migrate=False)

    def test_incomplete_backup(self):
        data, table = self.snapshot()
        with self.assertRaises(ValueError):
            self.updater.verify_snapshot(data[:-1], table, migrate=False)

    def test_bad_bootloader(self):
        data, table = self.snapshot()
        data[0] = 0
        with self.assertRaises(ValueError):
            self.updater.verify_snapshot(data, table, migrate=False)

    def test_built_table_must_be_full_size(self):
        data, table = self.snapshot()
        with self.assertRaises(ValueError):
            self.updater.verify_snapshot(data, table[:-1], migrate=False)

    def test_download_probe_never_invokes_esptool(self):
        import hashlib
        u = self.updater
        ident = dict(vid='303a', pid='80c3', serial='TD0720', location='5-1.2')
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            table = table_image('legacy')
            (root / 'partition-table.bin').write_bytes(table)
            argv = ['probe', '--serial', 'TD0720', '--boot-mode', 'legacy', '--sha256', 'test',
                    '--partition-sha256', hashlib.sha256(table).hexdigest(), '--probe-download']
            # The real lock needs fcntl. Substituting a recording context keeps
            # the assertion that the probe runs under the update lock while
            # letting the check run on any development machine.
            lock = MagicMock()
            with (patch.object(u, 'ROOT', root), patch.object(sys, 'argv', argv),
                  patch.object(sys, 'platform', 'linux'),
                  patch.object(u, 'device_lock', lock),
                  patch.object(u.os, 'geteuid', return_value=1000, create=True),
                  patch.object(u.Path, 'home', return_value=root),
                  patch.object(u, 'validate_image'),
                  patch.object(u, 'validate_partitions',
                               return_value=dict(update_esp.LAYOUTS['legacy'], name='legacy')),
                  patch.object(u.shutil, 'which', return_value='/usr/bin/fuser'),
                  patch.object(u.shutil, 'disk_usage', return_value=SimpleNamespace(free=64 * 1024 * 1024)),
                  patch.object(u.subprocess, 'run', return_value=SimpleNamespace(stdout='inactive')),
                  patch.object(u, 'ports', return_value=[('/dev/ttyACM0', ident)]),
                  patch.object(u, 'enter_download') as enter,
                  patch.object(u, 'wait_port', return_value=('/dev/ttyACM0', dict(ident, pid='1001'))),
                  patch.object(u, 'flash_session') as esp, patch.object(u, 'audit') as audit):
                u.main()
                lock.assert_called_once()
                enter.assert_called_once()
                esp.assert_not_called()
                self.assertEqual(audit.call_args.args[0], 'download_probe_verified')

    def test_probe_resume_checks_identity_hash_and_single_use(self):
        import json
        u = self.updater
        app = dict(vid='303a', pid='80c3', serial='TD0720', location='5-1.2')
        rom = dict(vid='303a', pid='0009', serial='70:04:1d:d8:54:14', location='5-1.2')
        records = [dict(event='preflight_ok', identity=app, app_sha256='digest'),
                   dict(event='rom_identified', identity=rom), dict(event='download_probe_verified')]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            job = 'mixos-flash-20260910-165812'
            source = root / job
            source.mkdir()
            audit = source / 'flash-audit.jsonl'
            audit.write_text('\n'.join(json.dumps(r) for r in records))
            with patch.object(u, 'ROOT', root / 'new-job'):
                self.assertEqual(u.load_probe(job, 'TD0720', 'digest'), (source, app, rom))
                for name, serial, digest in [('../other', 'TD0720', 'digest'),
                                              (job, 'wrong', 'digest'), (job, 'TD0720', 'wrong')]:
                    with self.assertRaises(ValueError):
                        u.load_probe(name, serial, digest)
                (source / 'resume-claim.json').write_text('{}')
                with self.assertRaises(ValueError):
                    u.load_probe(job, 'TD0720', 'digest')
                (source / 'resume-claim.json').unlink()
                audit.write_text(json.dumps(dict(event='aborted')))
                with self.assertRaises(ValueError):
                    u.load_probe(job, 'TD0720', 'digest')

    def test_esptool_package_rejects_hash_and_missing_stub(self):
        import hashlib
        import zipfile
        u = self.updater
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            wheel = root / 'tool.whl'
            with zipfile.ZipFile(wheel, 'w') as archive:
                archive.writestr('esptool/__init__.py', '# no stub')
            with patch.object(u, 'ROOT', root):
                with self.assertRaises(ValueError):
                    u.prepare_esptool(wheel, 'wrong')
                with self.assertRaises(ValueError):
                    u.prepare_esptool(wheel, hashlib.sha256(wheel.read_bytes()).hexdigest())

    def test_single_usb_session_and_backup_before_write(self):
        import hashlib
        u = self.updater
        identity = dict(vid='303a', pid='0009', serial='70:04:1d:d8:54:14', location='5-1.2')
        for valid in (True, False):
            with self.subTest(valid=valid), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                image = root / 'app.bin'
                image.write_bytes(b'app-data')
                data, table = self.snapshot()
                if not valid:
                    data[0x210000:0x210004] = b'\xff' * 4  # font header gone -> refuse to write
                chip = Mock()
                chip.get_security_info.return_value = dict(flags=0, chip_id=9, flash_crypt_cnt=0)
                chip.read_mac.return_value = bytes.fromhex('70041dd85414')
                chip.run_stub.return_value = chip
                actions = []

                def operation(argv, esp):
                    self.assertIs(esp, chip)
                    self.assertIn('no_reset_stub', argv)
                    self.assertIn('--no-stub', argv)  # Never load a second stub onto the live session.
                    if 'read_flash' in argv:
                        i = argv.index('read_flash')
                        offset, size, target = argv[i+1:i+4]
                        Path(target).write_bytes(data if offset == '0x0' else image.read_bytes())
                        actions.append('backup' if offset == '0x0' else 'readback')
                    else:
                        self.assertIn('write_flash', argv)
                        self.assertIn('0x10000', argv)
                        self.assertNotIn('erase_flash', argv)
                        actions.append('write')
                tool = SimpleNamespace(get_default_connected_device=Mock(return_value=chip),
                                       main=operation, __version__='4.7.0')
                with (patch.object(u, 'ROOT', root), patch.object(u, 'ESP_ENV', None),
                      patch.dict(sys.modules, esptool=tool), patch.object(u, 'audit'),
                      patch.object(u, 'idle_port'), patch.object(u, 'physical_identity', return_value=identity)):
                    if valid:
                        u.flash_session('/dev/ttyACM0', identity, image, table,
                                        hashlib.sha256(image.read_bytes()).hexdigest())
                        self.assertEqual(actions, ['backup', 'write', 'readback'])
                        chip.hard_reset.assert_called_once()
                    else:
                        with self.assertRaises(ValueError):
                            u.flash_session('/dev/ttyACM0', identity, image, table, 'digest')
                        self.assertEqual(actions, ['backup'])
                        chip.hard_reset.assert_not_called()
                    tool.get_default_connected_device.assert_called_once()
                    chip._port.close.assert_called_once()

    def test_security_flags(self):
        self.updater.validate_security('Flags: 0x00000000 (0b0)')
        for text in ('Flags: 0x1', '', 'Secure Boot: unknown'):
            with self.subTest(text=text), self.assertRaises(ValueError):
                self.updater.validate_security(text)


if __name__ == '__main__':
    unittest.main()
