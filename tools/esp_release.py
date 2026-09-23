#!/usr/bin/env python3
"""Generate a portable ESP release from a completed, exact local build report.

Local files only. No serial, SSH, service or network calls. The release asserts
source/configuration consistency, not hardware qualification or authenticity.
"""
import argparse
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
import mixos_esp_update as native
import ota_esp

SAFETY = {
    'CONFIG_BOOTLOADER_APP_ROLLBACK_ENABLE': True,
    'CONFIG_LCD_RGB_ISR_IRAM_SAFE': False,
    'CONFIG_GDMA_ISR_IRAM_SAFE': False,
    'CONFIG_SPIRAM_XIP_FROM_PSRAM': True,
    'CONFIG_SPIRAM_FETCH_INSTRUCTIONS': True,
    'CONFIG_SPIRAM_RODATA': True,
    'CONFIG_ESP_SYSTEM_PANIC_PRINT_REBOOT': True,
    'CONFIG_ESP_SYSTEM_PANIC_PRINT_HALT': False,
    'CONFIG_ESP_TASK_WDT_EN': True,
    'CONFIG_ESP_TASK_WDT_INIT': True,
    'CONFIG_ESP_TASK_WDT_PANIC': True,
    'CONFIG_BOOTLOADER_WDT_ENABLE': True,
    'CONFIG_BOOTLOADER_WDT_DISABLE_IN_USER_CODE': True,
}

def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()

def checked(record, path):
    if digest(path) != record.get('sha256'):
        raise ValueError('build input/artifact changed: ' + str(path))

def generate(root=ROOT, replacement_package=None, *, build_dir=None, report_dir=None,
             isolated_config=False):
    root = Path(root)
    project = root / 'firmware/esp32s3'
    if (build_dir is None) != (report_dir is None):
        raise ValueError('isolated build and report directories must be supplied together')
    if isolated_config and build_dir is None:
        raise ValueError('isolated configuration requires build and report directories')
    artifacts = Path(build_dir) if build_dir is not None else project / 'build'
    config_path = artifacts / 'sdkconfig' if isolated_config else project / 'sdkconfig'
    reports = Path(report_dir) if report_dir is not None else root / 'build/esp32s3'
    report_file = reports / 'font-app-build.json'
    report = json.loads(report_file.read_text(encoding='utf-8'))
    if (report.get('status') != 'cross-built' or report.get('target') != 'esp32s3' or
            report.get('ota_capable') is not True or report.get('partition_layout') != 'ab'):
        raise ValueError('current completed A/B cross-build report required')
    image_path = artifacts / 'mixos_esp32s3.bin'
    checked(report['build_app'], image_path)
    checked(report['elf'], image_path.with_suffix('.elf'))
    checked(report['sdkconfig'], config_path)
    checked(report['sdkconfig_generated'], artifacts / 'config/sdkconfig.json')
    table = artifacts / 'partition_table/partition-table.bin'
    checked(report['partition_table'], table)
    # Exact-set coverage prevents a stale report from omitting a newly added
    # OTA/health implementation or header while still checking old sources.
    expected = {p.name for p in (project / 'main').iterdir()
                if p.suffix in ('.c', '.h') or p.name in ('CMakeLists.txt', 'idf_component.yml')}
    sources = report.get('sources', [])
    names = [r['path'].replace('\\', '/').rsplit('/', 1)[-1] for r in sources]
    if len(names) != len(set(names)) or set(names) != expected:
        raise ValueError('build report does not cover the exact current main source set')
    for name, record in zip(names, sources):
        checked(record, project / 'main' / name)
    # Local USB component code participates in the reboot lifecycle. Bind its
    # complete build-input set as well, not just a wrapper under main/.
    expected_components = {p.relative_to(project).as_posix()
                           for p in (project / 'components').rglob('*')
                           if p.is_file() and (p.suffix in ('.c', '.h', '.cpp', '.cc', '.S', '.s', '.ld', '.cmake')
                               or p.name in ('CMakeLists.txt', 'idf_component.yml', 'sdkconfig.defaults')
                               or p.name.startswith('Kconfig'))}
    components = report.get('component_sources')
    if not isinstance(components, list) or any(not isinstance(r, dict) for r in components):
        raise ValueError('build report lacks local component source coverage')
    names = [r.get('component_path') for r in components]
    if (any(not isinstance(n, str) for n in names) or len(names) != len(set(names))
            or set(names) != expected_components):
        raise ValueError('build report does not cover the exact local component source set')
    for name, record in zip(names, components):
        checked(record, project / name)
    required_inputs = {'sdkconfig', 'sdkconfig.defaults', 'partitions.csv', 'CMakeLists.txt', 'dependencies.lock'}
    inputs = report.get('build_inputs', [])
    names = [r['path'].replace('\\', '/').rsplit('/', 1)[-1] for r in inputs]
    if len(names) != len(set(names)) or set(names) != required_inputs:
        raise ValueError('configuration/build dependency coverage incomplete')
    for name, record in zip(names, inputs):
        checked(record, config_path if name == 'sdkconfig' else project / name)
    receipt_path = reports / 'completed-build.json'
    checked(report.get('build_attestation', {}), receipt_path)
    receipt = json.loads(receipt_path.read_text(encoding='utf-8'))
    if (receipt.get('schema') != 'mixos-local-build/v1' or
            receipt.get('inputs') != {key: report[key] for key in ('sources', 'build_inputs', 'component_sources')} or
            receipt.get('artifacts') != {key: report[key] for key in
                                        ('build_app', 'elf', 'partition_table', 'sdkconfig_generated')}):
        raise ValueError('build report is not bound to stable compilation inputs and artifacts')
    config = report.get('effective_config', {})
    if any(config.get(k) is not v for k, v in SAFETY.items()):
        raise ValueError('effective compiled configuration violates OTA safety requirements')
    generated = json.loads((artifacts / 'config/sdkconfig.json').read_text(encoding='utf-8'))
    if config != {'CONFIG_' + k: v for k, v in generated.items()}:
        raise ValueError('effective config differs from generated compiler configuration')
    image, image_sha = ota_esp.inspect_image(image_path)
    description = ota_esp.describe_image(image)
    if len(image) > native.SLOT_SIZE:
        raise ValueError('image exceeds A/B slot')
    baseline_path = root / 'docs/esp32-recovery-baseline.json'
    baseline = json.loads(baseline_path.read_text(encoding='utf-8'))
    current = root / 'build/esp32s3/current-device-app.bin'
    if digest(current) != baseline['image_sha256']:
        raise ValueError('preserved current-device baseline does not match recorded recovery')
    manifest = {
        'schema': native.SCHEMA, 'protocol': 2, 'chip': {'name': 'esp32s3', 'id': 9},
        'app': {'file': 'app.bin', 'size': len(image), 'sha256': image_sha.hex(),
                'elf_sha256': description['elf_sha256'].hex()},
        'layout': native.LAYOUT, 'effective_config': {k: config[k] for k in SAFETY},
        'provenance': {'mode': 'exact-source-and-effective-build-configuration',
                       'build_report_sha256': digest(report_file),
                       'sdkconfig_sha256': digest(config_path),
                       'partition_table_sha256': digest(table),
                       'elf_file_sha256': digest(image_path.with_suffix('.elf')),
                       'rtc_diagnostic_symbols': report.get('rtc_diagnostic_symbols', []),
                       'hardware_validation': 'not performed; separate authorization required'},
        'protected_baseline': baseline,
    }
    if replacement_package is not None:
        replacement = native.load_release(replacement_package)
        app = replacement.manifest['app']
        # This is an operator-selected known package binding, not a claim that
        # packaging has measured the device. apply re-measures VALID ota_1 and
        # requires the separate --allow-replace-baseline authorization.
        manifest['verified_replacement'] = {
            'slot': 'ota_1', 'image_bytes': app['size'],
            'image_sha256': app['sha256'], 'elf_sha256': app['elf_sha256'],
        }
    return manifest

def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--output', type=Path, required=True, help='new release directory; never overwrite')
    p.add_argument('--replacement-package', type=Path,
                   help='known VALID B-slot release for later explicit A-baseline replacement; no device access')
    p.add_argument('--build-dir', type=Path, help='isolated completed candidate build directory')
    p.add_argument('--report-dir', type=Path, help='matching isolated completed-build reports')
    p.add_argument('--isolated-config', action='store_true',
                   help='validate the compiled sdkconfig inside --build-dir')
    a = p.parse_args(argv)
    manifest = generate(replacement_package=a.replacement_package,
                        build_dir=a.build_dir, report_dir=a.report_dir,
                        isolated_config=a.isolated_config)
    if a.output.exists():
        p.error('release directory already exists')
    # Bundle helper validates all app fields and copies a self-contained runner.
    import tempfile
    with tempfile.TemporaryDirectory(prefix='mixos-release-') as temp:
        metadata = Path(temp) / 'manifest.json'
        metadata.write_text(json.dumps(manifest, indent=2)+'\n', encoding='utf-8')
        image = (a.build_dir if a.build_dir is not None else ROOT/'firmware/esp32s3/build') / 'mixos_esp32s3.bin'
        native.bundle_release(image, metadata, a.output)
    print(json.dumps({'release': str(a.output), 'app': manifest['app'],
                      'device_access': False, 'hardware_validated': False}, indent=2))
    return 0

if __name__ == '__main__':
    raise SystemExit(main())
