"""Local launcher tests. All SSH/SCP/systemd subprocesses are mocked."""
import base64
import importlib
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
keyboard = importlib.import_module('deploy_keyboard')
display = importlib.import_module('deploy_display')

# The flash workers import tools/_mixlib, so it is part of the display package.
# Derived rather than listed, for the same reason the launcher derives it: a new
# module in _mixlib must not silently drop out of the package.
MIXLIB = ['tools/_mixlib/' + path.name for path in sorted((TOOLS / '_mixlib').glob('*.py'))]
# tools/update_esp.py imports linux/mixosd.py, which imports its neighbours, so
# the daemon's own package travels with the workers. Derived for the same
# reason as MIXLIB: staging netctl.py only after it broke a deployment is the
# failure this list exists to prevent.
LINUX_MODULES = ['linux/' + path.name for path in sorted((TOOLS.parent / 'linux').glob('*.py'))]


def remote_script(argv):
    words = shlex.split(argv[-1])
    if words[0] == 'sudo':
        words = shlex.split(words[-1])
    return base64.b64decode(words[1]).decode()


class LauncherTests(unittest.TestCase):
    def synthetic_build(self, root):
        """Unit-test fixture only; never attest or refresh real build evidence.

        The archived default build intentionally stays stale while a new
        candidate is built elsewhere. Launcher unit tests must not require
        overwriting that archive just to match today's source tree.
        """
        def put(name, data):
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data if isinstance(data, bytes) else data.encode())
            return path
        app = put('firmware/esp32s3/build/mixos_esp32s3.bin', b'SYNTHETIC TEST APP, NOT FLASHABLE')
        old = put('build/esp32s3/current-device-app.bin', b'SYNTHETIC TEST RECOVERY')
        ui = put('firmware/esp32s3/main/mix_ui.c', '/* synthetic launcher source */\n')
        font = put('build/font/MiSans-Normal-gb2312.ttf', b'SYNTHETIC FONT, NOT DEPLOYABLE')
        table = None
        for name in ('firmware/esp32s3/partitions.csv',
                     'firmware/esp32s3/build/partition_table/partition-table.bin'):
            table = put(name, (TOOLS.parent / name).read_bytes())
        report = {'status': 'cross-built', 'target': 'esp32s3',
                  'app': {'sha256': display.digest(app)},
                  'build_app': {'sha256': display.digest(app)},
                  'sources': [{'path': str(ui), 'sha256': display.digest(ui)}],
                  'fixture_only': True}
        put('build/esp32s3/font-app-build.json', json.dumps(report))
        put('build/font/MiSans-Normal-gb2312.ttf.manifest.json', json.dumps({
            'status': 'verified', 'output_sha256': display.digest(font),
            'ui_source_sha256': display.digest(ui), 'ui_coverage_missing': [],
            'required_coverage_missing': [],
            'load_verification': {'partition_padded_load': 'passed'}, 'fixture_only': True}))
        return app, old, table

    def test_installer_is_valid_python_and_rejects_unsafe_paths(self):
        script = keyboard.package_installer('/home/pi/stage', '/opt/package/job', {'tools/worker.py': 'a' * 64}, display=True)
        compile(script, '<installer>', 'exec')
        self.assertIn('dst.mkdir(mode=0o755)', script)
        self.assertIn("path.open('xb')", script)
        self.assertIn('path.resolve(strict=True) != path', script)
        self.assertIn('info.st_uid != 0', script)
        self.assertIn('os.fsync', script)
        for name in ('../worker.py', '/etc/passwd', 'tools/../x', 'tools\\x', 'tools//x'):
            with self.subTest(name=name), self.assertRaises(ValueError):
                keyboard.package_installer('/home/pi/stage', '/opt/package/job', {name: 'a' * 64})

    def start(self, module, hashes, prefix, extra=()):
        with tempfile.TemporaryDirectory() as temp:
            job = prefix + '-20260911-014000'
            receipt = Path(temp) / 'receipt.json'
            receipt.write_text(json.dumps({'job': job, 'host': '192.168.1.22',
                                          'staging': '/home/pi/' + job, 'hashes': hashes}))
            commands = []
            def run(command, **kwargs):
                commands.append(command)
                return subprocess.CompletedProcess(command, 0)
            with mock.patch.dict(os.environ, {'MIXOS_SSH_PASSWORD': 'fake-test-only'}), \
                    mock.patch.object(sys, 'argv', [module.__file__, '--start', str(receipt), *extra]), \
                    mock.patch.object(module.subprocess, 'run', side_effect=run):
                module.main()
            return [remote_script(command) for command in commands]

    def test_keyboard_installs_before_one_nonrestarting_isolated_launch(self):
        scripts = self.start(keyboard, {n: 'a' * 64 for n in ('image.bin', 'manifest.json', 'flash_keyboard_on_pi.py')},
                             'mixos-keyboard', ('--serial', '123456789ABC'))
        self.assertEqual(len(scripts), 2)
        installer = shlex.split(scripts[0])
        self.assertIn('-I', installer)
        compile(installer[-1], '<installer>', 'exec')
        command = shlex.split(scripts[1])
        self.assertEqual(command[0], 'systemd-run')
        self.assertIn('--property=Restart=no', command)
        self.assertEqual(command.count('--execute'), 1)
        self.assertIn('--leave', command)
        self.assertEqual(command[command.index('--serial') + 1], '123456789ABC')
        self.assertNotIn('mixosd', scripts[1])
        self.assertNotIn('/home/pi/', scripts[1])

    def test_display_installs_root_owned_package_and_restart_cleanup(self):
        names = ['tools/flash_font_on_pi.py', 'tools/flash_esp_on_pi.py', 'tools/update_esp.py',
                 'tools/ota_esp.py',
                 'firmware/esp32s3/partitions.csv',
                 'font.ttf', 'font-manifest.json', 'new-app.bin',
                 'firmware/esp32s3/build/mixos_esp32s3.bin', 'partition-table.bin', 'launch.sh',
                 'tools/display_transport.py', *MIXLIB, *LINUX_MODULES, *display.transport.PACKAGES]
        scripts = self.start(display, {n: 'a' * 64 for n in names}, 'mixos-display')
        self.assertEqual(len(scripts), 2)
        compile(shlex.split(scripts[0])[-1], '<installer>', 'exec')
        command = shlex.split(scripts[1])
        self.assertIn('--property=Restart=no', command)
        self.assertIn('--property=ExecStopPost=/usr/bin/systemctl start mixosd.service', command)
        self.assertEqual(command[-2], '/bin/bash')
        self.assertTrue(command[-1].startswith('/opt/mixos-display-packages/'))
        self.assertNotIn('/home/pi/', scripts[1])

    def test_display_resume_option_propagates_only_to_one_new_job(self):
        names = ['tools/flash_font_on_pi.py', 'tools/flash_esp_on_pi.py', 'tools/update_esp.py',
                 'tools/ota_esp.py',
                 'firmware/esp32s3/partitions.csv',
                 'font.ttf', 'font-manifest.json', 'new-app.bin',
                 'firmware/esp32s3/build/mixos_esp32s3.bin', 'partition-table.bin', 'launch.sh',
                 'tools/display_transport.py', *MIXLIB, *LINUX_MODULES, *display.transport.PACKAGES]
        source = '/opt/mixos-display-packages/mixos-display-20260910-175627/work/session'
        scripts = self.start(display, {n: 'a' * 64 for n in names}, 'mixos-display',
                             ('--resume-no-write-source', source))
        self.assertEqual(len(scripts), 2)
        command = shlex.split(scripts[-1])
        self.assertEqual(command[-2:], ['--resume-no-write-source', source])
        self.assertIn('--property=Restart=no', command)
        self.assertIn('--property=ExecStopPost=/usr/bin/systemctl start mixosd.service', command)

    def test_display_resume_rejects_other_modes_and_unanchored_paths(self):
        for args in (['--stage', '--resume-no-write-source', '/opt/anything'],
                     ['--start', 'unused.json', '--resume-no-write-source', '/home/pi/source'],
                     ['--start', 'unused.json', '--resume-no-write-source',
                      '/opt/mixos-display-packages/mixos-display-20260910-175627/work/session/..']):
            with self.subTest(args=args), mock.patch.object(sys, 'argv', [display.__file__, *args]), \
                    mock.patch.object(display.subprocess, 'run') as run:
                with self.assertRaises(SystemExit):
                    display.main()
                run.assert_not_called()

    def test_keyboard_stage_selects_verified_raw_artifact(self):
        source = TOOLS.parent
        image = source / 'build/keyboard/keebdeck_6r11c_default.raw.bin'
        manifest = source / 'build/keyboard/manifest.json'
        if not image.is_file() or not manifest.is_file():
            self.skipTest('Local keyboard raw export is not present')
        recorded = json.loads(manifest.read_text())
        self.assertIs(recorded['target_build_verified'], True)
        self.assertEqual(recorded['qmk_commit'], 'a63fd7f01cdabd9ce85bb09ae2b573fd3b8e60aa')
        self.assertEqual(keyboard.sha(image), recorded['hashes'][image.name])
        self.assertEqual(keyboard.sha(image), recorded['raw_exports']['default']['raw_sha256'])
        self.assertEqual(recorded['source_hashes']['mcuconf.h'],
                         keyboard.sha(source / 'firmware/keyboard/mcuconf.h'))
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for path in (image, manifest, source / 'tools/flash_keyboard_on_pi.py'):
                dest = root / path.relative_to(source)
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(path.read_bytes())
            commands = []
            def run(command, **kwargs):
                commands.append(command)
                return subprocess.CompletedProcess(command, 0)
            with mock.patch.object(keyboard, 'ROOT', root), \
                    mock.patch.dict(os.environ, {'MIXOS_SSH_PASSWORD': 'fake-test-only'}), \
                    mock.patch.object(sys, 'argv', [keyboard.__file__, '--stage']), \
                    mock.patch.object(keyboard.subprocess, 'run', side_effect=run):
                keyboard.main()
            uploads = [command for command in commands if command[0] == 'scp']
            self.assertEqual(Path(uploads[0][-2]).name, 'keebdeck_6r11c_default.raw.bin')
            receipt = json.loads(next((root / 'build/deploy').glob('*.json')).read_text())
            self.assertEqual(receipt['hashes']['image.bin'], keyboard.sha(image))
            self.assertFalse(any('systemd-run' in remote_script(command)
                                 for command in commands if command[0] == 'ssh'))

    def test_historical_release_cannot_authorize_new_runtime_or_app(self):
        prior = TOOLS.parent / 'build/deploy/mixos-display-20260910-175627.json'
        with mock.patch.dict(os.environ, {'MIXOS_SSH_PASSWORD': 'fake-test-only'}), \
                mock.patch.object(sys, 'argv', [display.__file__, '--stage', '--stage-from-receipt', str(prior)]), \
                mock.patch.object(display.subprocess, 'run') as run:
            with self.assertRaises(ValueError):
                display.main()
            run.assert_not_called()

    def test_current_build_matches_sources_and_rejects_mutation(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            app, old, _ = self.synthetic_build(root)
            with mock.patch.object(display, 'ROOT', root), \
                    mock.patch.object(display, 'DEPLOYED_APP_SHA256', display.digest(old)):
                result = display.current_build_check(app, old)
                self.assertEqual(result['mode'], 'current_verified_build')
                self.assertFalse(result['screen_confirmation_required'])
                self.assertEqual(result['device_app_sha256'], display.DEPLOYED_APP_SHA256)
                with mock.patch.object(display, 'source_digest_matches', return_value=False):
                    with self.assertRaises(ValueError):
                        display.current_build_check(app, old)
                arbitrary_old = root / 'other.bin'
                arbitrary_old.write_bytes(b'unrelated historical app')
                with self.assertRaisesRegex(ValueError, 'recorded deployed'):
                    display.current_build_check(app, arbitrary_old)
                # Real byte mutation must also fail without mocking the gate.
                (root / 'firmware/esp32s3/main/mix_ui.c').write_text('changed after build')
                with self.assertRaisesRegex(ValueError, 'source changed'):
                    display.current_build_check(app, old)

    def test_migrate_requires_an_ab_build(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            app, old, table = self.synthetic_build(root)
            with mock.patch.object(display, 'ROOT', root), \
                    mock.patch.object(display, 'DEPLOYED_APP_SHA256', display.digest(old)):
                self.assertTrue(display.current_build_check(app, old, table, True)['migrate_to_ab'])
                legacy = root / 'legacy.bin'
                legacy.write_bytes(b'\xff' * 0xc00)
                with self.assertRaises(ValueError):
                    display.current_build_check(app, old, legacy, True)

    def test_historical_receipt_option_requires_stage(self):
        with mock.patch.object(sys, 'argv', [display.__file__, '--start', 'unused', '--stage-from-receipt', 'prior']), \
                mock.patch.object(display.subprocess, 'run') as run:
            with self.assertRaises(SystemExit):
                display.main()
            run.assert_not_called()

    def test_display_stage_generates_exact_old_new_hash_and_safe_launcher(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            app, old, _ = self.synthetic_build(root)
            paths = ['tools/display_transport.py', 'tools/flash_font_on_pi.py',
                     'tools/flash_esp_on_pi.py', 'tools/update_esp.py', 'tools/ota_esp.py']
            source = TOOLS.parent
            paths.extend(str(path.relative_to(source))
                         for path in display.transport.local_packages(source).values())
            # The workers import tools/_mixlib, so staging copies it too.
            paths.extend(str(path.relative_to(source)).replace('\\', '/')
                         for path in sorted((source / 'tools/_mixlib').glob('*.py')))
            # Same for the daemon package that update_esp.py imports.
            paths.extend(str(path.relative_to(source)).replace('\\', '/')
                         for path in sorted((source / 'linux').glob('*.py')))
            for name in set(paths):
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes((source / name).read_bytes())
            old_hash = display.digest(old)
            new_hash = display.digest(app)
            launches = []
            def run(command, **kwargs):
                if command[0] == 'scp':
                    with zipfile.ZipFile(command[-2]) as archive:
                        launches.append(archive.read('launch.sh').decode())
                        self.assertEqual(archive.read('firmware/esp32s3/build/mixos_esp32s3.bin'),
                                         (root / 'build/esp32s3/current-device-app.bin').read_bytes())
                        self.assertIn('tools/ota_esp.py', archive.namelist())
                        for name, (_, expected) in display.transport.PACKAGES.items():
                            self.assertEqual(display.hashlib.sha256(archive.read(name)).hexdigest(), expected)
                        self.assertIn('tools/display_transport.py', archive.namelist())
                return subprocess.CompletedProcess(command, 0)
            with mock.patch.object(display, 'ROOT', root), \
                    mock.patch.object(display, 'DEPLOYED_APP_SHA256', old_hash), \
                    mock.patch.dict(os.environ, {'MIXOS_SSH_PASSWORD': 'fake-test-only'}), \
                    mock.patch.object(sys, 'argv', [display.__file__, '--stage']), \
                    mock.patch.object(display.subprocess, 'run', side_effect=run):
                display.main()
            self.assertEqual(len(launches), 1)
            launch = launches[0]
            self.assertNotIn('/home/pi/', launch)
            self.assertIn('"$@"', launch)
            self.assertIn('[[ $# -eq 2 && "$1" == "--resume-no-write-source" ]]', launch)
            self.assertEqual(launch.count('systemctl stop mixosd.service'), 1)
            line = next(line for line in launch.splitlines() if line.startswith('exec runuser'))
            command = shlex.split(line)
            self.assertIn('-I', command)
            self.assertEqual(command[command.index('--app-sha256') + 1], old_hash)
            self.assertEqual(command[command.index('--new-app-sha256') + 1], new_hash)
            receipt = json.loads(next((root / 'build/deploy').glob('mixos-display-*.json')).read_text())
            self.assertEqual(receipt['artifact_provenance']['esptool_version'], '5.4.0')
            self.assertIs(receipt['artifact_provenance']['screen_confirmation_required'], False)
            self.assertEqual(command.count('--execute'), 1)
            self.assertTrue(command[command.index('--workdir') + 1].endswith('/work/session'))
            claim = next(line for line in launch.splitlines() if line.startswith('/usr/bin/python3'))
            compile(shlex.split(claim)[-1], '<claim>', 'exec')


if __name__ == '__main__':
    unittest.main()
