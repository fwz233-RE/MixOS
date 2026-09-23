"""Local release provenance checks; synthetic files only, no devices/toolchain."""
import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import esp_font_build as build
from _support import ROOT
import sys
sys.path.insert(0, str(ROOT / 'tools'))
import esp_release as release
import test_mixos_esp_update as fixtures


class ReleaseProvenanceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.project = self.root / 'firmware/esp32s3'
        self.report_path = self.root / 'build/esp32s3/font-app-build.json'
        self.receipt_path = self.report_path.with_name('completed-build.json')
        self.app = self.project / 'build/mixos_esp32s3.bin'
        self.put(self.app, fixtures.make_image())
        self.put(self.app.with_suffix('.elf'), b'local test ELF')
        self.put(self.project / 'build/partition_table/partition-table.bin', b'test table')
        self.config = {k.removeprefix('CONFIG_'): v for k, v in release.SAFETY.items()}
        self.put(self.project / 'build/config/sdkconfig.json', json.dumps(self.config))
        for name in ('sdkconfig', 'sdkconfig.defaults', 'partitions.csv', 'CMakeLists.txt', 'dependencies.lock'):
            self.put(self.project / name, name)
        for name in ('main.c', 'mix_ota.c', 'mix_ota_tx.c', 'mix_health.c', 'mix_ota.h', 'CMakeLists.txt', 'idf_component.yml'):
            self.put(self.project / 'main' / name, name)
        self.put(self.project / 'components/usb_device_uac/usb_device_uac.c', 'local USB source')
        self.put(self.project / 'components/usb_device_uac/include/usb_device_uac.h', 'local USB interface')
        self.put(self.root / 'build/esp32s3/current-device-app.bin', b'preserved recovery')
        self.put(self.root / 'docs/esp32-recovery-baseline.json', json.dumps({
            'slot': 'ota_0', 'image_bytes': 18,
            'image_sha256': hashlib.sha256(b'preserved recovery').hexdigest(),
            'elf_sha256': 'a' * 64}))
        self.report = {'status': 'cross-built', 'target': 'esp32s3', 'ota_capable': True,
                       'partition_layout': 'ab', 'effective_config': dict(release.SAFETY),
                       'sources': [build.info(p) for p in sorted((self.project / 'main').iterdir())],
                       'build_inputs': [build.info(self.project / n) for n in
                                        ('sdkconfig', 'sdkconfig.defaults', 'partitions.csv', 'CMakeLists.txt', 'dependencies.lock')],
                       'component_sources': [dict(build.info(p), component_path=p.relative_to(self.project).as_posix())
                                             for p in sorted((self.project / 'components').rglob('*')) if p.is_file()],
                       'build_app': build.info(self.app), 'elf': build.info(self.app.with_suffix('.elf')),
                       'sdkconfig': build.info(self.project / 'sdkconfig'),
                       'partition_table': build.info(self.project / 'build/partition_table/partition-table.bin'),
                       'sdkconfig_generated': build.info(self.project / 'build/config/sdkconfig.json')}
        self.receipt = {'schema': 'mixos-local-build/v1',
                        'inputs': {key: self.report[key] for key in ('sources', 'build_inputs', 'component_sources')},
                        'artifacts': {key: self.report[key] for key in
                                      ('build_app', 'elf', 'partition_table', 'sdkconfig_generated')}}
        self.save_receipt()
        self.save_report()

    def put(self, path, data):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data.encode('utf-8') if isinstance(data, str) else data)

    def save_receipt(self):
        self.put(self.receipt_path, json.dumps(self.receipt))
        self.report['build_attestation'] = build.info(self.receipt_path)

    def save_report(self):
        self.put(self.report_path, json.dumps(self.report))

    def test_generates_full_hash_binding_without_device_calls(self):
        value = release.generate(self.root)
        self.assertEqual(value['app']['sha256'], release.digest(self.app))
        self.assertEqual(value['app']['elf_sha256'], bytes(range(32)).hex())
        self.assertNotIn('verified_replacement', value)
        self.assertEqual(value['effective_config'], release.native.REQUIRED_CONFIG)

    def test_new_source_missing_from_report_is_refused(self):
        self.put(self.project / 'main/new_worker.c', 'unreported')
        with self.assertRaisesRegex(ValueError, 'source set'):
            release.generate(self.root)

    def test_modified_existing_source_is_refused(self):
        self.put(self.project / 'main/mix_ota.c', 'modified')
        with self.assertRaisesRegex(ValueError, 'changed'):
            release.generate(self.root)

    def test_modified_config_is_refused(self):
        self.put(self.project / 'sdkconfig.defaults', 'unsafe')
        with self.assertRaisesRegex(ValueError, 'changed'):
            release.generate(self.root)

    def test_modified_application_is_refused(self):
        self.put(self.app, b'stale build')
        with self.assertRaisesRegex(ValueError, 'changed'):
            release.generate(self.root)

    def test_missing_compile_receipt_is_refused(self):
        self.report.pop('build_attestation')
        self.save_report()
        with self.assertRaisesRegex(ValueError, 'changed'):
            release.generate(self.root)

    def test_report_cannot_rebind_new_source_to_old_compile(self):
        self.put(self.project / 'main/mix_ota.c', 'after compilation')
        self.report['sources'] = [build.info(p) for p in sorted((self.project / 'main').iterdir())]
        self.save_report()
        with self.assertRaisesRegex(ValueError, 'stable compilation'):
            release.generate(self.root)

    def test_unsafe_effective_config_is_refused(self):
        self.report['effective_config']['CONFIG_LCD_RGB_ISR_IRAM_SAFE'] = True
        self.save_report()
        with self.assertRaisesRegex(ValueError, 'safety'):
            release.generate(self.root)

    def test_generated_config_mismatch_is_refused(self):
        self.config['EXTRA_SETTING'] = 'unreported'
        self.put(self.project / 'build/config/sdkconfig.json', json.dumps(self.config))
        self.report['sdkconfig_generated'] = build.info(self.project / 'build/config/sdkconfig.json')
        self.receipt['artifacts']['sdkconfig_generated'] = self.report['sdkconfig_generated']
        self.save_receipt(); self.save_report()
        with self.assertRaisesRegex(ValueError, 'generated compiler'):
            release.generate(self.root)

    def test_recovery_image_change_is_refused(self):
        self.put(self.root / 'build/esp32s3/current-device-app.bin', b'candidate cannot overwrite baseline')
        with self.assertRaisesRegex(ValueError, 'baseline'):
            release.generate(self.root)

    def test_known_b_package_binding_requires_real_package_validation(self):
        package = self.root / 'known-b'
        data = fixtures.make_image()
        self.put(package / 'app.bin', data)
        self.put(package / 'manifest.json', json.dumps(fixtures.manifest(data)))
        value = release.generate(self.root, package)
        self.assertEqual(value['verified_replacement'], {
            'slot': 'ota_1', 'image_bytes': len(data),
            'image_sha256': hashlib.sha256(data).hexdigest(), 'elf_sha256': bytes(range(32)).hex()})
        self.put(package / 'app.bin', data[:-1] + bytes([data[-1] ^ 1]))
        with self.assertRaises(release.native.JobError):
            release.generate(self.root, package)

    def isolated_fixture(self):
        artifacts = self.root / 'build/isolated-candidate'
        reports = self.root / 'build/isolated-reports'
        for name in ('mixos_esp32s3.bin', 'mixos_esp32s3.elf',
                     'partition_table/partition-table.bin', 'config/sdkconfig.json'):
            self.put(artifacts / name, (self.project / 'build' / name).read_bytes())
        self.put(reports / 'completed-build.json', self.receipt_path.read_bytes())
        self.put(reports / 'font-app-build.json', self.report_path.read_bytes())
        return artifacts, reports

    def isolated_configuration_fixture(self):
        artifacts, reports = self.isolated_fixture()
        config = artifacts / 'sdkconfig'
        self.put(config, 'CONFIG_LV_DEF_REFR_PERIOD=33\n')
        report = copy.deepcopy(self.report)
        receipt = copy.deepcopy(self.receipt)
        report['sdkconfig'] = build.info(config)
        report['build_inputs'][0] = build.info(config)
        receipt['inputs']['build_inputs'] = report['build_inputs']
        self.put(reports / 'completed-build.json', json.dumps(receipt))
        report['build_attestation'] = build.info(reports / 'completed-build.json')
        self.put(reports / 'font-app-build.json', json.dumps(report))
        return artifacts, reports, config

    def test_isolated_configuration_binds_actual_input_not_stable_default(self):
        artifacts, reports, config = self.isolated_configuration_fixture()
        value = release.generate(self.root, build_dir=artifacts, report_dir=reports,
                                 isolated_config=True)
        self.assertEqual(value['provenance']['sdkconfig_sha256'], release.digest(config))
        self.assertEqual((self.project / 'sdkconfig').read_text(), 'sdkconfig')
        with self.assertRaisesRegex(ValueError, 'changed'):
            release.generate(self.root, build_dir=artifacts, report_dir=reports)
        self.put(config, 'edited since compile')
        with self.assertRaisesRegex(ValueError, 'changed'):
            release.generate(self.root, build_dir=artifacts, report_dir=reports,
                             isolated_config=True)

    def test_isolated_configuration_requires_isolated_directories(self):
        with self.assertRaisesRegex(ValueError, 'requires'):
            release.generate(self.root, isolated_config=True)

    def test_build_command_pins_complete_custom_configuration(self):
        import idf_env
        artifacts = self.root / 'path with space/build'
        config = artifacts / 'sdkconfig'
        command = idf_env.build_command(self.root, artifacts, config)
        self.assertIn('SDKCONFIG=' + idf_env.to_posix(config), command)
        self.assertIn('SDKCONFIG_DEFAULTS=' + idf_env.to_posix(self.project / 'sdkconfig.defaults'), command)
        with self.assertRaisesRegex(ValueError, 'requires'):
            idf_env.build_command(self.root, sdkconfig=config)

    def test_driver_binds_and_rejects_changed_isolated_configuration(self):
        artifacts, reports, config = self.isolated_configuration_fixture()
        with mock.patch.object(build, 'ROOT', self.root), \
                mock.patch.object(build, 'APP', artifacts / self.app.name), \
                mock.patch.object(build, 'DEST', reports), \
                mock.patch.object(build, 'ISOLATED_CONFIG', True):
            before = build.input_records()
            self.assertEqual(before['build_inputs'][0], build.info(config))
            build.attest_build(before)
            build.checked_attestation()
            self.put(config, 'edited during compile')
            with self.assertRaisesRegex(RuntimeError, 'changed during compilation'):
                build.attest_build(before)
            with self.assertRaisesRegex(RuntimeError, 'changed'):
                build.checked_attestation()

    def test_isolated_build_keeps_original_artifacts_and_reports_unchanged(self):
        artifacts, reports = self.isolated_fixture()
        original = self.app.read_bytes(), self.report_path.read_bytes(), self.receipt_path.read_bytes()
        value = release.generate(self.root, build_dir=artifacts, report_dir=reports)
        self.assertEqual(value['app']['sha256'], release.digest(artifacts / self.app.name))
        self.assertEqual(original, (self.app.read_bytes(), self.report_path.read_bytes(), self.receipt_path.read_bytes()))

    def test_isolated_validation_never_uses_default_image(self):
        artifacts, reports = self.isolated_fixture()
        self.put(artifacts / self.app.name, b'changed isolated candidate')
        with self.assertRaisesRegex(ValueError, 'changed'):
            release.generate(self.root, build_dir=artifacts, report_dir=reports)

    def test_isolated_config_and_receipt_must_match(self):
        artifacts, reports = self.isolated_fixture()
        self.put(artifacts / 'config/sdkconfig.json', '{}')
        with self.assertRaisesRegex(ValueError, 'changed'):
            release.generate(self.root, build_dir=artifacts, report_dir=reports)

    def test_isolated_paths_are_paired(self):
        with self.assertRaisesRegex(ValueError, 'together'):
            release.generate(self.root, build_dir=self.root / 'new')
        with self.assertRaisesRegex(ValueError, 'together'):
            release.generate(self.root, report_dir=self.root / 'new')

    def test_build_command_explicit_isolated_output(self):
        import idf_env
        isolated = self.root / 'path with space/build'
        command = idf_env.build_command(self.root, isolated)
        self.assertIn('-B', command)
        self.assertIn(idf_env.to_posix(isolated), command)
        self.assertNotIn(' -B ', idf_env.build_command(self.root))

    def test_isolated_driver_rejects_protected_output_overlap(self):
        with mock.patch.object(build, 'ROOT', self.root), \
                mock.patch.object(build, 'PRESERVED', self.root / 'build/esp32s3'):
            for base in (self.project / 'build', self.root / 'build/esp32s3'):
                for forbidden in (base, base.parent, base / 'nested'):
                    for pair in ((forbidden, self.root / 'reports'),
                                 (self.root / 'candidate', forbidden)):
                        with self.subTest(pair=pair), self.assertRaisesRegex(ValueError, 'preserve'):
                            build.isolated_paths(*pair)

    def test_isolated_driver_rejects_build_report_overlap(self):
        for pair in ((self.root / 'new', self.root / 'new'),
                     (self.root / 'new', self.root / 'new/reports'),
                     (self.root / 'new/build', self.root / 'new')):
            with self.subTest(pair=pair), self.assertRaisesRegex(ValueError, 'preserve'):
                build.isolated_paths(*pair)

    def test_isolated_driver_accepts_separate_resolved_paths(self):
        paths = self.root / 'candidate', self.root / 'reports'
        with mock.patch.object(build, 'ROOT', self.root), \
                mock.patch.object(build, 'PRESERVED', self.root / 'build/esp32s3'):
            self.assertEqual(build.isolated_paths(*paths), tuple(p.resolve() for p in paths))

    def test_driver_rejects_source_changes_during_build(self):
        with mock.patch.object(build, 'ROOT', self.root), mock.patch.object(build, 'APP', self.app), \
                mock.patch.object(build, 'DEST', self.report_path.parent):
            before = build.input_records()
            self.put(self.project / 'main/mix_ota.c', 'edited during compile')
            with self.assertRaisesRegex(RuntimeError, 'changed during compilation'):
                build.attest_build(before)

    def test_report_only_requires_unchanged_completed_build(self):
        with mock.patch.object(build, 'ROOT', self.root), mock.patch.object(build, 'APP', self.app), \
                mock.patch.object(build, 'DEST', self.report_path.parent):
            build.attest_build(build.input_records())
            self.assertEqual(build.checked_attestation()['sha256'], release.digest(self.receipt_path))
            self.put(self.app, b'later binary')
            with self.assertRaisesRegex(RuntimeError, 'changed'):
                build.checked_attestation()

    def test_component_edits_after_build_are_refused(self):
        self.put(self.project / 'components/usb_device_uac/usb_device_uac.c', 'changed detach code')
        with self.assertRaisesRegex(ValueError, 'changed'):
            release.generate(self.root)

    def test_missing_or_added_component_coverage_is_refused(self):
        self.report.pop('component_sources')
        self.save_report()
        with self.assertRaisesRegex(ValueError, 'component source coverage'):
            release.generate(self.root)
        self.report['component_sources'] = self.receipt['inputs']['component_sources']
        self.save_report()
        self.put(self.project / 'components/usb_device_uac/new_restart.c', 'unreported')
        with self.assertRaisesRegex(ValueError, 'component source set'):
            release.generate(self.root)

    def test_duplicate_or_unsafe_component_path_is_refused(self):
        original = copy.deepcopy(self.report['component_sources'])
        for records in (original + original[:1], [dict(original[0], component_path='../outside.c')]):
            self.report['component_sources'] = records
            self.save_report()
            with self.assertRaisesRegex(ValueError, 'component source set'):
                release.generate(self.root)

    def test_component_report_cannot_rebind_old_compilation(self):
        path = self.project / 'components/usb_device_uac/usb_device_uac.c'
        self.put(path, 'edited after build')
        self.report['component_sources'] = [dict(build.info(p), component_path=p.relative_to(self.project).as_posix())
                                           for p in sorted((self.project / 'components').rglob('*')) if p.is_file()]
        self.save_report()
        with self.assertRaisesRegex(ValueError, 'stable compilation'):
            release.generate(self.root)

    def test_component_change_during_build_is_refused(self):
        with mock.patch.object(build, 'ROOT', self.root), mock.patch.object(build, 'APP', self.app), \
                mock.patch.object(build, 'DEST', self.report_path.parent):
            before = build.input_records()
            self.put(self.project / 'components/usb_device_uac/include/usb_device_uac.h', 'changed during build')
            with self.assertRaisesRegex(RuntimeError, 'changed during compilation'):
                build.attest_build(before)

    def test_duplicate_input_coverage_is_refused(self):
        self.report['build_inputs'].append(copy.deepcopy(self.report['build_inputs'][0]))
        self.save_report()
        with self.assertRaisesRegex(ValueError, 'coverage'):
            release.generate(self.root)


if __name__ == '__main__':
    unittest.main()
