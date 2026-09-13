"""Pinned official transport upgrade checks; never opens a device."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
import deploy_display as deploy
import display_transport as transport
import flash_font_on_pi as worker


class DisplayTransportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.package = Path(self.temp.name) / 'package'
        self.work = Path(self.temp.name) / 'work'
        self.work.mkdir()
        for name, source in transport.local_packages(ROOT).items():
            target = self.package / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(source.read_bytes())
        for patch in (mock.patch.dict(os.environ),
                      mock.patch.object(worker.esp, 'ROOT', self.work),
                      mock.patch.object(worker.esp, 'ESP_ENV', {})):
            patch.start()
            self.addCleanup(patch.stop)

    def wheel(self):
        return self.package / 'esptool.whl'

    def test_package_pin_and_official_stub_changed(self):
        self.assertEqual(transport.VERSION, '5.4.0')
        self.assertEqual(worker.WHEEL_SHA256, deploy.ESPTOOL_SHA256)
        self.assertEqual(hashlib.sha256(self.wheel().read_bytes()).hexdigest(), worker.WHEEL_SHA256)
        transport.verify_packages(transport.local_packages(ROOT))
        with zipfile.ZipFile(self.wheel()) as archive:
            modern = archive.read('esptool/targets/stub_flasher/2/esp32s3.json')
            legacy = archive.read('esptool/targets/stub_flasher/1/esp32s3.json')
            self.assertEqual(hashlib.sha256(modern).hexdigest(), transport.STUB_SHA256)
            modern, legacy = json.loads(modern), json.loads(legacy)
            self.assertTrue(modern['text'])
            self.assertTrue(modern['entry'])
            self.assertNotEqual(modern['text'], legacy['text'])

    def test_isolated_import_and_fresh_vendor_directory(self):
        with mock.patch.object(worker.esp, 'audit') as audit, \
                mock.patch.object(transport.subprocess, 'run', return_value=subprocess.CompletedProcess(
                    [], 0, worker.ESPTOOL_VERSION + '\n', '')) as run:
            worker.prepare_esptool(self.wheel())
            command = run.call_args.args[0]
            self.assertEqual(command[1:4], ['-I', '-B', '-c'])
            self.assertEqual(run.call_args.kwargs['timeout'], 20)
            self.assertIn('from esptool.cmds import connect_esp', command[-1])
            audit.assert_called_once_with('isolated_esptool_verified',
                                          sha256=worker.WHEEL_SHA256, version='5.4.0')
            self.assertEqual(worker.esp.ESP_ENV['PYTHONPATH'], str(self.work / '.esptool'))
            with self.assertRaises(FileExistsError):
                worker.prepare_esptool(self.wheel())
            run.assert_called_once()

    def test_reject_unpinned_packages_before_extraction(self):
        for name in transport.PACKAGES:
            path = self.package / name
            original = path.read_bytes()
            path.write_bytes(original + b'tampered')
            with self.subTest(package=name), mock.patch.object(transport.subprocess, 'run') as run:
                with self.assertRaises(ValueError):
                    worker.prepare_esptool(self.wheel())
                self.assertFalse((self.work / '.esptool').exists())
                run.assert_not_called()
            path.write_bytes(original)

    def test_reject_dependency_failure_or_wrong_import_version(self):
        for code, output in ((1, ''), (0, '4.8.1\n'), (0, '5.4.10\n')):
            with self.subTest(code=code, output=output), tempfile.TemporaryDirectory() as directory, \
                    mock.patch.object(worker.esp, 'ROOT', Path(directory)), \
                    mock.patch.object(worker.esp, 'audit') as audit, \
                    mock.patch.object(transport.subprocess, 'run', return_value=subprocess.CompletedProcess(
                        [], code, output, '')):
                with self.assertRaises(RuntimeError):
                    worker.prepare_esptool(self.wheel())
                audit.assert_not_called()

    def test_trusted_config_replaces_ambient_config_and_ide_settings(self):
        os.environ['ESPTOOL_CFGFILE'] = '/untrusted/esptool.cfg'
        os.environ['ESPRESSIF_IDE_WS'] = 'untrusted-workspace'
        config = self.work / '.esptool/esptool.cfg'
        def check(command, **kwargs):
            self.assertEqual(os.environ['ESPTOOL_CFGFILE'], str(config))
            self.assertNotIn('ESPRESSIF_IDE_WS', os.environ)
            self.assertEqual(config.read_text(), '[esptool]\n')
            return subprocess.CompletedProcess(command, 0, '5.4.0\n', '')
        with mock.patch.object(transport.subprocess, 'run', side_effect=check), \
                mock.patch.object(worker.esp, 'audit'):
            worker.prepare_esptool(self.wheel())
        self.assertEqual(worker.esp.ESP_ENV['ESPTOOL_CFGFILE'], str(config))
        self.assertNotIn('ESPRESSIF_IDE_WS', worker.esp.ESP_ENV)
        if os.name == 'posix':
            # Windows ignores the POSIX permission bits, so the private-mode
            # check is meaningful only where the tool actually runs. The rest
            # of the assertions above are platform independent and now run
            # everywhere instead of being skipped with them.
            self.assertEqual(config.parent.stat().st_mode & 0o777, 0o700)

    def test_bulk_read_deadlines_and_write_retry_boundary_unchanged(self):
        self.assertEqual(worker.PORT_TIMEOUT, 10)
        self.assertEqual(worker.READ_CHUNK, 0x40000)
        chip = mock.Mock()
        chip.IS_STUB = True
        chip.FLASH_WRITE_SIZE = 0x4000
        chip.ESP_CMDS = {'FLASH_DATA': 3}
        chip.checksum.return_value = 0
        chip.check_command.side_effect = TimeoutError('single submission failed')
        with self.assertRaises(TimeoutError):
            worker.flash_block_once(chip, b'A' * 0x4000, 0)
        chip.check_command.assert_called_once()
        self.assertEqual(chip.check_command.call_args.args[1], 3)
        chip.flash_block.assert_not_called()


if __name__ == '__main__':
    unittest.main()
