"""Preserve the known deployed ESP app, cross-build, and record font-fix artifacts.

Local-only build validation helper: never connects to or flashes a device.

The toolchain layout comes from tools/idf_env.py, and the WSL launcher from
tests/_support.py, so no machine name, user name or absolute project path is
written down here.
"""
import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _support import ROOT, host_command, posix_path  # noqa: E402

sys.path.insert(0, str(ROOT / "tools"))
import idf_env  # noqa: E402

APP = ROOT / "firmware/esp32s3/build/mixos_esp32s3.bin"
DEST = ROOT / "build/esp32s3"
# Recovery artifacts always remain in the original protected directory, even
# when a new candidate and its reports use isolated output directories.
PRESERVED = DEST
BUILD_DIR = None
BUILD_COMMAND = 'py -3.12 tests/esp_font_build.py build'
OLD_SHA = "9593904eab8c1bcaaef9c383abc9a5308ae4beeece94989436360289eafc675b"
OLD_BYTES = 469104
CANDIDATE_SHA = "c0e99db4d5cb854e5ea2bf24753fcfb17ef77f8bce7b3b1c6a20b824418e2a69"
CANDIDATE_BYTES = 857136


def preserve_candidate():
    """Keep the prior font-enabled candidate separately from deployed recovery."""
    PRESERVED.mkdir(parents=True, exist_ok=True)
    saved = PRESERVED / "previous-font-candidate-mixos_esp32s3.bin"
    if not saved.exists():
        for source in (APP, DEST / "mixos_esp32s3.bin"):
            record = info(source)
            if record["sha256"] != CANDIDATE_SHA or record["bytes"] != CANDIDATE_BYTES:
                raise RuntimeError("Prior font candidate differs; refusing to overwrite build artifacts")
        with saved.open("xb") as stream:
            stream.write(APP.read_bytes())
    record = info(saved)
    if record["sha256"] != CANDIDATE_SHA or record["bytes"] != CANDIDATE_BYTES:
        raise RuntimeError("Preserved font candidate does not match its known image")
    print(json.dumps(record, indent=2))
    return record


def info(path):
    data = path.read_bytes()
    return {"path": str(path), "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}


def preserve():
    PRESERVED.mkdir(parents=True, exist_ok=True)
    previous = PRESERVED / "previous-mixos_esp32s3.bin"
    if previous.exists():
        record = info(previous)
        if record["sha256"] != OLD_SHA or record["bytes"] != OLD_BYTES:
            raise RuntimeError("Existing recovery copy does not match known deployed image; refusing overwrite")
    else:
        data = APP.read_bytes()
        if len(data) != OLD_BYTES or hashlib.sha256(data).hexdigest() != OLD_SHA:
            raise RuntimeError("Build input no longer matches known deployed image; recovery copy required first")
        with previous.open("xb") as stream:
            stream.write(data)
        record = info(previous)
    print(json.dumps(record, indent=2))
    return record


def app_slot():
    """Read the slot this build will really occupy from its own partition table.

    Hard-coding 0x200000 was correct only for the historic factory-only layout.
    Under the A/B table a slot is 0x1F0000, so a literal would quietly accept an
    image that overflows the slot it is about to be written into.
    """
    import update_esp

    table = APP.parent / "partition_table/partition-table.bin"
    rows = update_esp.parse_partition_binary(table.read_bytes())
    slots = [row for row in rows if row[1] == "app" and row[3] == 0x10000]
    if len(slots) != 1:
        raise RuntimeError("Built partition table has no single app slot at 0x10000")
    name, _, subtype, offset, size = slots[0]
    layout = update_esp.identify_partition_binary(table.read_bytes())["name"]
    return {"layout": layout, "slot": name, "subtype": subtype,
            "offset": offset, "bytes": size,
            "table": info(table)}


def input_records():
    project = ROOT / 'firmware/esp32s3'
    sources = [info(path) for path in sorted((project / 'main').iterdir())
               if path.suffix in ('.c', '.h') or path.name in ('CMakeLists.txt', 'idf_component.yml')]
    inputs = [info(project / name) for name in
              ('sdkconfig', 'sdkconfig.defaults', 'partitions.csv', 'CMakeLists.txt', 'dependencies.lock')]
    components = [dict(info(path), component_path=path.relative_to(project).as_posix())
                  for path in sorted((project / 'components').rglob('*'))
                  if path.is_file() and (path.suffix in ('.c', '.h', '.cpp', '.cc', '.S', '.s', '.ld', '.cmake')
                      or path.name in ('CMakeLists.txt', 'idf_component.yml', 'sdkconfig.defaults')
                      or path.name.startswith('Kconfig'))]
    return {'sources': sources, 'build_inputs': inputs, 'component_sources': components}


def artifact_records():
    return {name: info(path) for name, path in {
        'build_app': APP, 'elf': APP.with_suffix('.elf'),
        'partition_table': APP.parent / 'partition_table/partition-table.bin',
        'sdkconfig_generated': APP.parent / 'config/sdkconfig.json',
    }.items()}


def attest_build(before):
    after = input_records()
    if before != after:
        raise RuntimeError('Build inputs changed during compilation; run a stable incremental build before release')
    receipt = {'schema': 'mixos-local-build/v1', 'inputs': after, 'artifacts': artifact_records()}
    (DEST / 'completed-build.json').write_text(json.dumps(receipt, indent=2) + '\n', encoding='utf-8')


def checked_attestation():
    path = DEST / 'completed-build.json'
    if not path.exists():
        raise RuntimeError('No stable completed-build receipt; run the build driver, not report alone')
    receipt = json.loads(path.read_text(encoding='utf-8'))
    if (receipt.get('schema') != 'mixos-local-build/v1' or
            receipt.get('inputs') != input_records() or receipt.get('artifacts') != artifact_records()):
        raise RuntimeError('Completed-build inputs or artifacts changed; rebuild before reporting')
    return info(path)


def report():
    attestation = checked_attestation()
    previous = preserve()
    previous_candidate = preserve_candidate()
    current = info(APP)
    slot = app_slot()
    if current["sha256"] == OLD_SHA:
        raise RuntimeError("Application did not change")
    if current["bytes"] > slot["bytes"] or APP.read_bytes()[0] != 0xE9:
        raise RuntimeError("App image signature/partition size validation failed")
    paths = idf_env.idf_paths(ROOT)
    image = subprocess.check_output(host_command([
        paths["python"], "-m", "esptool",
        "--chip", "esp32s3", "image_info",
        posix_path(APP)]), text=True)
    if "Checksum:" not in image or "Validation Hash:" not in image or image.count("(valid)") != 2:
        raise RuntimeError("esptool did not validate the application checksum and embedded hash")
    symbols = subprocess.check_output(host_command([
        paths["compiler_bin"] + "/xtensa-esp32s3-elf-nm",
        "--defined-only", posix_path(APP.with_suffix('.elf'))]), text=True)
    expected = {"ttf_font_init", "ttf_font_deinit", "ttf_draw_cell", "FT_New_Memory_Face"}
    linked = [line for line in symbols.splitlines() if line.split()[-1:] and line.split()[-1] in expected]
    if {line.split()[-1] for line in linked} != expected:
        raise RuntimeError("Required font entry points are absent from linked ELF")
    copy = DEST / "mixos_esp32s3.bin"
    copy.write_bytes(APP.read_bytes())
    sources = [info(path) for path in sorted((ROOT / 'firmware/esp32s3/main').iterdir())
               if path.suffix in ('.c', '.h') or path.name in ('CMakeLists.txt', 'idf_component.yml')]
    build_inputs = [info(ROOT / 'firmware/esp32s3' / name)
                    for name in ('sdkconfig', 'sdkconfig.defaults', 'partitions.csv', 'CMakeLists.txt', 'dependencies.lock')]
    build_config = json.loads((APP.parent / 'config/sdkconfig.json').read_text(encoding='utf-8'))
    effective_config = {'CONFIG_' + key: value for key, value in build_config.items()}
    rtc_layout = [line for line in symbols.splitlines()
                  if line.split()[-1:] and line.split()[-1] in ('mix_ota_rtc_diagnostics', 'retained')]
    manifest_path = ROOT / "build/font/MiSans-Normal-gb2312.ttf.manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    ui_bytes = (ROOT / "firmware/esp32s3/main/mix_ui.c").read_bytes()
    ui_sha = hashlib.sha256(ui_bytes).hexdigest()
    ui_crlf_sha = hashlib.sha256(ui_bytes.replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")).hexdigest()
    font = info(ROOT / "build/font/MiSans-Normal-gb2312.ttf")
    font_provenance = {"manifest": info(manifest_path), "font": font,
                       "font_hash_matches": font["sha256"] == manifest["output_sha256"],
                       "ui_exact_hash_matches": ui_sha == manifest["ui_source_sha256"],
                       "ui_crlf_hash_matches": ui_crlf_sha == manifest["ui_source_sha256"],
                       "ui_actual_sha256": ui_sha, "ui_manifest_sha256": manifest["ui_source_sha256"]}
    record = {"status": "cross-built", "target": "esp32s3", "idf": "5.4.2",
              "previous_font_candidate": previous_candidate, "font_provenance": font_provenance,
              "previous_app": previous, "app": info(copy), "build_app": current,
              "app_partition_offset": hex(slot["offset"]), "app_partition_bytes": slot["bytes"],
              "partition_layout": slot["layout"], "app_partition": slot["slot"],
              "partition_table": slot["table"],
              "ota_capable": slot["layout"] == "ab",
              "image_validation": image, "linked_font_symbols": linked,
              'build_attestation': attestation,
              "sources": sources, "build_inputs": build_inputs,
              "component_sources": input_records()['component_sources'],
              "effective_config": effective_config, "rtc_diagnostic_symbols": rtc_layout,
              "sdkconfig": info(ROOT / 'firmware/esp32s3/sdkconfig'),
              "sdkconfig_generated": info(APP.parent / 'config/sdkconfig.json'),
              "elf": info(APP.with_suffix('.elf')),
              "build_command": BUILD_COMMAND,
              "build_log": info(DEST / "font-app-build.log"),
              "hardware_validation": "not performed; no SSH or flashing"}
    if checked_attestation() != attestation:
        raise RuntimeError('Build receipt changed during report generation')
    (DEST / "font-app-build.json").write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(record, indent=2))


def isolated_paths(build_dir, report_dir):
    """Reject overlapping or protected outputs before invoking the compiler."""
    build_dir, report_dir = Path(build_dir).resolve(), Path(report_dir).resolve()
    protected = ((ROOT / 'firmware/esp32s3/build').resolve(), PRESERVED.resolve())
    def overlaps(left, right):
        return left == right or left in right.parents or right in left.parents
    if (overlaps(build_dir, report_dir) or
            any(overlaps(path, base) for path in (build_dir, report_dir) for base in protected)):
        raise ValueError('isolated build/report directories must preserve the original outputs')
    return build_dir, report_dir


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stage", choices=("preserve", "build", "report"))
    parser.add_argument('--build-dir', type=Path, help='isolated candidate output; requires --report-dir')
    parser.add_argument('--report-dir', type=Path, help='isolated build reports; requires --build-dir')
    args = parser.parse_args()
    if (args.build_dir is None) != (args.report_dir is None):
        parser.error('--build-dir and --report-dir must be specified together')
    if args.build_dir is not None:
        try:
            BUILD_DIR, DEST = isolated_paths(args.build_dir, args.report_dir)
        except ValueError as exc:
            parser.error(str(exc))
        APP = BUILD_DIR / 'mixos_esp32s3.bin'
        BUILD_COMMAND += ' --build-dir ' + str(BUILD_DIR) + ' --report-dir ' + str(DEST)
    if args.stage == "preserve":
        preserve()
        preserve_candidate()
    elif args.stage == "report":
        report()
    else:
        preserve()
        preserve_candidate()
        before = input_records()
        result = subprocess.run(host_command(["bash", "-lc", idf_env.build_command(ROOT, BUILD_DIR)]),
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        DEST.mkdir(parents=True, exist_ok=True)
        (DEST / "font-app-build.log").write_bytes(result.stdout)
        print(result.stdout.decode("utf-8", errors="replace"))
        if result.returncode:
            raise SystemExit(result.returncode)
        attest_build(before)
        report()
