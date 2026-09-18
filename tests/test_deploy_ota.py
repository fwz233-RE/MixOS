"""Offline SSH-wrapper tests. Native result, not SSH exit, decides success.

The old shell stop/run/start assertion is deliberately replaced: service
ownership is now the native worker's tested responsibility. Build checks use
self-contained fixtures so concurrent firmware edits do not invalidate tests.
"""
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

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'tools'), str(ROOT / 'linux')]
import deploy_ota
import mixos_esp_update as native
from test_mixos_esp_update import make_image, manifest


class BuildCheckTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.image = self.root / 'app.bin'
        self.image.write_bytes(make_image())
        source = self.root / 'firmware/esp32s3/main/main.c'
        source.parent.mkdir(parents=True)
        source.write_bytes(b'void app_main(void) {}\n')
        self.source = source
        self.report = self.root / 'build/esp32s3/font-app-build.json'
        self.report.parent.mkdir(parents=True)
        self.value = {'status': 'cross-built', 'target': 'esp32s3',
                      'build_app': {'sha256': deploy_ota.digest(self.image)},
                      'sources': [{'path': str(source), 'sha256': deploy_ota.digest(source)}],
                      'ota_capable': True, 'partition_layout': 'ab', 'app_partition_bytes': native.SLOT_SIZE}
        self.report.write_text(json.dumps(self.value))
        patch = mock.patch.object(deploy_ota, 'ROOT', self.root)
        patch.start()
        self.addCleanup(patch.stop)

    def test_current_cross_build_is_accepted(self):
        result = deploy_ota.build_check(self.image)
        self.assertEqual(result['mode'], 'usb_ota')
        self.assertEqual(result['partition_layout'], 'ab')
        self.assertEqual(result['app_sha256'], deploy_ota.digest(self.image))

    def test_edited_sources_and_foreign_images_are_refused(self):
        self.source.write_bytes(b'changed')
        with self.assertRaisesRegex(ValueError, 'changed since the recorded build'):
            deploy_ota.build_check(self.image)
        other = self.root / 'other.bin'
        other.write_bytes(make_image(5000))
        with self.assertRaisesRegex(ValueError, 'not the current cross-build'):
            deploy_ota.build_check(other)

    def test_a_single_slot_build_cannot_be_pushed_over_usb(self):
        self.value['ota_capable'] = False
        self.report.write_text(json.dumps(self.value))
        with self.assertRaisesRegex(ValueError, 'no A/B slots'):
            deploy_ota.build_check(self.image)


class RemoteCommandTests(unittest.TestCase):
    def test_wrapper_calls_only_native_job_and_status(self):
        script = deploy_ota.remote_command('/home/pi/mixos-ota-release', deploy_ota.DEVICE, 60.0)
        command = shlex.split(script)
        self.assertEqual(command[:3], ['/usr/bin/python3', '-I', '-u'])
        self.assertIn('apply', command)
        self.assertIn('--wait', command)
        self.assertEqual(command[command.index('--package') + 1], '/home/pi/mixos-ota-release')
        self.assertNotIn('systemctl', script)
        self.assertNotIn('sudo', script)
        self.assertNotIn('ota_esp.py', script)
        self.assertNotIn('--allow-replace-baseline', script)
        explicit = deploy_ota.remote_command('/home/pi/package', deploy_ota.DEVICE, 60, True)
        self.assertIn('--allow-replace-baseline', explicit)
        status = shlex.split(deploy_ota.status_command('/home/pi/package', '1' * 32))
        self.assertIn('status', status)
        self.assertNotIn('apply', status)


class MainTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.addCleanup(self.temp.cleanup)
        for name in deploy_ota.PAYLOAD.values():
            destination = self.root / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes((ROOT / name).read_bytes())
        self.image = self.root / 'app-under-test.bin'
        self.image.write_bytes(make_image())
        self.manifest = self.root / 'manifest.json'
        self.manifest.write_text(json.dumps(manifest(self.image.read_bytes())))
        self.result = {'schema': 'mixos-esp-job/v2', 'job': '7' * 32,
                       'state': 'complete', 'durable': True, 'error': None,
                       'firmware': {'state': 'confirmed'}, 'service': {'state': 'restored'}}

    def run_main(self, extra=(), returncodes=None, result=None, output=None):
        commands = []
        codes = list(returncodes or [])
        native_result = self.result if result is None else result
        def run(command, **kwargs):
            if command[0] == 'scp':
                with zipfile.ZipFile(command[-2]) as archive:
                    self.uploaded = {name: archive.read(name) for name in archive.namelist()}
            commands.append((command, kwargs))
            return subprocess.CompletedProcess(command, codes.pop(0) if codes else 0,
                                               json.dumps(native_result) if output is None else output, '')
        argv = ['--image', str(self.image), '--manifest', str(self.manifest), '--skip-build-check', *extra]
        with mock.patch.object(deploy_ota, 'ROOT', self.root), \
                mock.patch.dict(os.environ, {'MIXOS_SSH_PASSWORD': 'fake-test-only'}), \
                mock.patch.object(deploy_ota.subprocess, 'run', side_effect=run), \
                mock.patch('sys.stdout', io.StringIO()), mock.patch('sys.stderr', io.StringIO()):
            status = deploy_ota.main(argv)
        return status, commands

    def receipt(self):
        found = sorted((self.root / 'build/deploy').glob('mixos-ota-*.json'))
        self.assertEqual(len(found), 1)
        return json.loads(found[0].read_text())

    def test_dry_run_validates_without_touching_network_or_service(self):
        with mock.patch.object(deploy_ota.subprocess, 'run') as run, \
                mock.patch.object(deploy_ota, 'ROOT', self.root), \
                mock.patch('sys.stdout', io.StringIO()):
            self.assertEqual(deploy_ota.main(['--image', str(self.image), '--skip-build-check', '--dry-run']), 0)
        run.assert_not_called()
        self.assertFalse((self.root / 'build/deploy').exists())

    def test_invalid_image_device_or_missing_manifest_never_reaches_network(self):
        junk = self.root / 'junk.bin'
        junk.write_bytes(b'MZ' + b'\0' * 8192)
        choices = (['--image', str(junk), '--skip-build-check', '--dry-run'],
                   ['--image', str(self.image), '--skip-build-check', '--dry-run', '--device', '/dev/ttyACM0'],
                   ['--image', str(self.image), '--skip-build-check'])
        for argv in choices:
            with self.subTest(argv=argv), mock.patch.object(deploy_ota.subprocess, 'run') as run, \
                    mock.patch('sys.stderr', io.StringIO()):
                with self.assertRaises(SystemExit):
                    deploy_ota.main(argv)
                run.assert_not_called()

    def test_an_image_larger_than_slot_is_refused(self):
        big = self.root / 'big.bin'
        big.write_bytes(make_image(deploy_ota.SLOT_BYTES + 1))
        with mock.patch.object(deploy_ota.subprocess, 'run') as run, mock.patch('sys.stderr', io.StringIO()):
            with self.assertRaises(SystemExit):
                deploy_ota.main(['--image', str(big), '--skip-build-check', '--dry-run'])
            run.assert_not_called()

    def test_success_uploads_manifest_and_shared_native_runtime_records_native_result(self):
        status, commands = self.run_main()
        self.assertEqual(status, 0)
        self.assertEqual(sorted(self.uploaded), sorted(['app.bin', 'manifest.json', *deploy_ota.PAYLOAD]))
        self.assertEqual(self.uploaded['app.bin'], self.image.read_bytes())
        receipt = self.receipt()
        self.assertEqual(receipt['result'], self.result)
        self.assertNotIn('state', receipt)  # no second firmware-success interpretation
        self.assertTrue(receipt['staging'].startswith('/home/pi/mixos-ota-'))
        self.assertEqual(receipt['hashes']['app.bin'], deploy_ota.digest(self.image))
        self.assertTrue(all('sudo -S' not in ' '.join(command) for command, _ in commands))

    def test_remote_exit_zero_without_result_is_unknown(self):
        status, _ = self.run_main(output='')
        self.assertEqual(status, 1)
        result = self.receipt()['result']
        self.assertEqual(result['firmware']['state'], 'unknown')
        self.assertIn('do not reflash blindly', result['error']['message'])

    def test_failed_firmware_service_or_durability_returns_same_native_failure(self):
        self.result['service'] = {'state': 'restore-failed'}
        self.result['error'] = {'code': 'service-restore', 'message': 'start failed'}
        status, _ = self.run_main(returncodes=[0, 0, 0, 1])
        self.assertEqual(status, 1)
        self.assertEqual(self.receipt()['result']['firmware']['state'], 'confirmed')
        self.assertEqual(self.receipt()['result']['service']['state'], 'restore-failed')

    def test_ssh_disconnect_cannot_return_success(self):
        status, _ = self.run_main(returncodes=[0, 0, 0, 255], output='')
        self.assertEqual(status, 1)
        self.assertIn('independent', self.receipt()['transport_error'])

    def test_failed_upload_stops_before_submit(self):
        with self.assertRaisesRegex(RuntimeError, 'Upload failed'):
            self.run_main(returncodes=[0, 1])


if __name__ == '__main__':
    unittest.main()
