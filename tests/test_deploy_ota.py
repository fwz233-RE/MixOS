"""Drive tools/deploy_ota.py without SSH, a Pi, or a device.

Everything here is local: subprocess is mocked, so no ssh/scp ever runs and no
serial port is opened. What is checked is the part that decides whether an
update is safe to start and what the Pi is actually told to do.
"""
import hashlib
import importlib
import io
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
import zipfile

TOOLS = Path(__file__).resolve().parents[1] / 'tools'
sys.path.insert(0, str(TOOLS))
deploy_ota = importlib.import_module('deploy_ota')

REPO = TOOLS.parent
IMAGE = REPO / 'firmware/esp32s3/build/mixos_esp32s3.bin'


def image_bytes(size=4096, chip=9):
    """The smallest thing inspect_image() accepts as an ESP32-S3 application."""
    data = bytearray(b'\0' * size)
    data[0] = 0xE9
    data[12:14] = chip.to_bytes(2, 'little')
    return bytes(data)


class BuildCheckTests(unittest.TestCase):
    def test_current_cross_build_is_accepted(self):
        if not IMAGE.is_file():
            self.skipTest('Local ESP cross-build is not present')
        result = deploy_ota.build_check(IMAGE)
        self.assertEqual(result['mode'], 'usb_ota')
        self.assertEqual(result['partition_layout'], 'ab')
        self.assertEqual(result['app_sha256'], deploy_ota.digest(IMAGE))

    def test_edited_sources_and_foreign_images_are_refused(self):
        if not IMAGE.is_file():
            self.skipTest('Local ESP cross-build is not present')
        with mock.patch.object(deploy_ota, 'source_digest_matches', return_value=False):
            with self.assertRaisesRegex(ValueError, 'changed since the recorded build'):
                deploy_ota.build_check(IMAGE)
        with tempfile.TemporaryDirectory() as temp:
            other = Path(temp) / 'other.bin'
            other.write_bytes(image_bytes())
            with self.assertRaisesRegex(ValueError, 'not the current cross-build'):
                deploy_ota.build_check(other)

    def test_a_single_slot_build_cannot_be_pushed_over_usb(self):
        """Without ota_0/ota_1 there is nothing to receive the image."""
        if not IMAGE.is_file():
            self.skipTest('Local ESP cross-build is not present')
        report = json.loads((REPO / 'build/esp32s3/font-app-build.json').read_text(encoding='utf-8'))
        report['ota_capable'] = False
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / 'build/esp32s3').mkdir(parents=True)
            (root / 'build/esp32s3/font-app-build.json').write_text(json.dumps(report), encoding='utf-8')
            (root / 'firmware/esp32s3/main').mkdir(parents=True)
            for source in report['sources']:
                name = source['path'].replace('\\', '/').rsplit('/', 1)[-1]
                (root / 'firmware/esp32s3/main' / name).write_bytes(
                    (REPO / 'firmware/esp32s3/main' / name).read_bytes())
            with mock.patch.object(deploy_ota, 'ROOT', root):
                with self.assertRaisesRegex(ValueError, 'no A/B slots'):
                    deploy_ota.build_check(IMAGE)


class RemoteCommandTests(unittest.TestCase):
    def test_service_stands_aside_and_comes_back_with_the_update_status(self):
        script = deploy_ota.remote_command('/home/pi/mixos-ota-20260913-101500',
                                           deploy_ota.DEVICE, 60.0)
        lines = [line for line in script.splitlines() if line.strip()]
        self.assertEqual(lines[1], 'systemctl stop mixosd.service')
        self.assertTrue(lines[2].startswith('runuser -u pi -- '))
        self.assertEqual(lines[3], 'status=$?')
        self.assertEqual(lines[4], 'systemctl start mixosd.service')
        self.assertEqual(lines[5], 'exit $status')
        # The service is restarted unconditionally, so a failed update never
        # leaves the terminal host stopped.
        self.assertNotIn('set -e', script)
        command = shlex.split(lines[2])
        self.assertEqual(command[:4], ['runuser', '-u', 'pi', '--'])
        self.assertIn('-I', command)
        self.assertTrue(command[command.index('--image') + 1].endswith('/app.bin'))
        self.assertTrue(command[command.index('--device') + 1].startswith('/dev/serial/by-id/'))
        # The updater must not restart mixosd itself; the wrapper owns that.
        self.assertNotIn('--service', command)


class MainTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.addCleanup(self.temp.cleanup)
        for name in deploy_ota.PAYLOAD.values():
            destination = self.root / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes((REPO / name).read_bytes())
        self.image = self.root / 'app-under-test.bin'
        self.image.write_bytes(image_bytes())

    def run_main(self, extra=(), returncodes=None):
        """Run main() with every ssh/scp call mocked. Returns (status, commands)."""
        commands = []
        codes = list(returncodes or [])

        def run(command, **kwargs):
            if command[0] == 'scp':
                # The staging directory disappears when main() returns, so the
                # uploaded archive has to be inspected while it still exists.
                with zipfile.ZipFile(command[-2]) as archive:
                    self.uploaded = {name: archive.read(name) for name in archive.namelist()}
            commands.append((command, kwargs))
            return subprocess.CompletedProcess(command, codes.pop(0) if codes else 0)

        argv = ['--image', str(self.image), '--skip-build-check', *extra]
        with mock.patch.object(deploy_ota, 'ROOT', self.root), \
                mock.patch.dict(os.environ, {'MIXOS_SSH_PASSWORD': 'fake-test-only'}), \
                mock.patch.object(deploy_ota.subprocess, 'run', side_effect=run):
            status = deploy_ota.main(argv)
        return status, commands

    def receipt(self):
        found = sorted((self.root / 'build/deploy').glob('mixos-ota-*.json'))
        self.assertEqual(len(found), 1)
        return json.loads(found[0].read_text())

    def test_dry_run_validates_without_touching_the_network(self):
        with mock.patch.object(deploy_ota.subprocess, 'run') as run, \
                mock.patch.object(deploy_ota, 'ROOT', self.root), \
                mock.patch('sys.stdout', io.StringIO()):
            self.assertEqual(deploy_ota.main(['--image', str(self.image),
                                              '--skip-build-check', '--dry-run']), 0)
        run.assert_not_called()
        self.assertFalse((self.root / 'build/deploy').exists())

    def test_a_non_application_image_never_reaches_the_device(self):
        junk = self.root / 'junk.bin'
        junk.write_bytes(b'MZ' + b'\0' * 8192)
        for argv in (['--image', str(junk), '--skip-build-check', '--dry-run'],
                     ['--image', str(self.image), '--skip-build-check', '--dry-run',
                      '--device', '/dev/ttyACM0']):
            with self.subTest(argv=argv), mock.patch.object(deploy_ota.subprocess, 'run') as run, \
                    mock.patch.object(deploy_ota, 'ROOT', self.root), \
                    mock.patch('sys.stderr', io.StringIO()), mock.patch('sys.stdout', io.StringIO()):
                with self.assertRaises(SystemExit):
                    deploy_ota.main(argv)
                run.assert_not_called()

    def test_an_image_larger_than_a_slot_is_refused(self):
        big = self.root / 'big.bin'
        big.write_bytes(image_bytes(deploy_ota.SLOT_BYTES + 1))
        with mock.patch.object(deploy_ota.subprocess, 'run') as run, \
                mock.patch.object(deploy_ota, 'ROOT', self.root), \
                mock.patch('sys.stderr', io.StringIO()), mock.patch('sys.stdout', io.StringIO()):
            with self.assertRaises(SystemExit):
                deploy_ota.main(['--image', str(big), '--skip-build-check', '--dry-run'])
            run.assert_not_called()

    def test_successful_update_uploads_exactly_the_payload_and_records_it(self):
        with mock.patch('sys.stdout', io.StringIO()):
            status, commands = self.run_main()
        self.assertEqual(status, 0)
        self.assertEqual(len([command for command, _ in commands if command[0] == 'scp']), 1)
        self.assertEqual(sorted(self.uploaded), sorted(['app.bin', *deploy_ota.PAYLOAD]))
        self.assertEqual(self.uploaded['app.bin'], self.image.read_bytes())
        self.assertEqual(hashlib.sha256(self.uploaded['tools/ota_esp.py']).hexdigest(),
                         deploy_ota.digest(REPO / 'tools/ota_esp.py'))
        receipt = self.receipt()
        self.assertEqual(receipt['state'], 'installed')
        self.assertEqual(receipt['hashes']['app.bin'], deploy_ota.digest(self.image))
        self.assertEqual(receipt['artifact_provenance']['mode'], 'unverified_image')
        self.assertTrue(receipt['staging'].startswith('/home/pi/mixos-ota-'))

    def test_a_failed_update_reports_the_remote_status_and_keeps_evidence(self):
        # mkdir, scp, bootstrap, then the update itself fails.
        with mock.patch('sys.stdout', io.StringIO()), mock.patch('sys.stderr', io.StringIO()) as err:
            status, commands = self.run_main(returncodes=[0, 0, 0, 1])
        self.assertEqual(status, 1)
        self.assertEqual(self.receipt()['state'], 'failed')
        self.assertIn('still running its previous build', err.getvalue())
        self.assertIn('--migrate', err.getvalue())

    def test_a_failed_upload_stops_before_the_device_is_opened(self):
        with mock.patch('sys.stdout', io.StringIO()):
            with self.assertRaisesRegex(RuntimeError, 'Upload failed'):
                self.run_main(returncodes=[0, 1])


if __name__ == '__main__':
    unittest.main()
