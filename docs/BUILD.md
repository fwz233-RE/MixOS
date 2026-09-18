# Build and verification

## Host baseline

Run the CMake and Python commands in the root `README.md`. They are hardware-free and may be run from WSL Ubuntu 22.04.

## ESP32-S3

The intended toolchain is ESP-IDF 5.4.2 with the ESP32-S3 target. Use the project-local tool setup described in `docs/HOST_TOOLS.md`. Before any build, inspect the resolved component lock and verify that the ES8389 codec overlay is selected by CMake. A successful configure/build must produce the application, bootloader, and partition artifacts without modifying the original ESP repository.

### Build through the driver, never `idf.py` alone

```powershell
py -3.12 tests/esp_font_build.py build
```

This is the only supported way to produce a deployable application. It runs the
same vendored `idf.py build` under WSL through `tools/idf_env.py`, and then does
the parts a bare `idf.py build` cannot: preserve the recovery images before the
build directory is touched, save the output to `build/esp32s3/font-app-build.log`,
validate the image checksum and embedded hash with esptool, confirm the font
entry points are actually linked, read the app slot out of the build's own
partition table, and write `build/esp32s3/font-app-build.json`.

That last file is the deployment manifest, and it is the point of the whole
exercise. It is the only record that ties an application binary to the sources
it was built from. Every deployment path reads it and refuses to install
anything it does not describe:

| Refusal | Meaning |
| --- | --- |
| `This image is not the current cross-build; rebuild before updating` | the manifest's `build_app.sha256` is not the hash of the image being pushed |
| `ESP source changed since the recorded build: NAME` | a file under `firmware/esp32s3/main` was edited after the manifest was written |
| `The build report says this firmware has no A/B slots` | the build used the factory-only `partitions.csv`, so there is nothing to OTA into |

Building by hand is the easy way to lose an afternoon, because nothing looks
wrong: `idf.py build` succeeds, `firmware/esp32s3/build/mixos_esp32s3.bin` is a
correct new binary, and the manifest silently keeps describing the *previous*
one. The mismatch only surfaces at the next `tools/deploy_ota.py`, as the first
refusal above. This happened on 2026-09-13.

The fix is to re-run the driver, not to reach for `--skip-build-check`. That
flag exists for installing an image the local build deliberately does not
describe, and it switches off the source-drift check in the same motion. The
driver's rebuild is incremental, so recovering from a bare `idf.py build` costs
a link and a report, not a full compile.

Using `idf.py build` directly while chasing a compile error is fine. Just make
the driver the last build before any deployment, so the artifacts and the
manifest describe the same thing.

### Portable v2 release and stable build evidence

新发布入口为 `py -3.12 tools/esp_release.py --output <新目录>`。它检查整个 `main` 源码／头文件集合、项目构建输入、应用／ELF／分区表、生成配置及安全配置，不接受遗漏新增文件的旧报告。

构建驱动在编译前后比较输入，并把成功编译的输入和产物绑定到 `build/esp32s3/completed-build.json`。编译过程中修改源码或配置会拒绝发布，需要完成一次输入稳定的增量构建。`report` 只能重新核实既有成功构建，不能用当前源码 hash 为旧二进制补造来源。

本地验证为 `py -3.12 tools/mixos_esp_update.py inspect --package <目录>` 与 `apply --package <目录> --dry-run`。这些检查不连接设备。实际安装仍需独立授权，不因打包成功而自动执行。

首次 B→A 更新若需要解除旧 A 保护，可用 `--replacement-package <已知 B 包>` 给新包绑定明确的 B 镜像；该参数不宣称已观察设备。实际更新时仍需验证运行 B 的文件 SHA、ELF 和 VALID，并明确提供 `--allow-replace-baseline`。缺少匹配已知包时保持保护。

### Font partition

The `font` partition is a raw 4 MiB region at `0x210000`; firmware memory-maps it and passes the complete region to FreeType. A source font larger than 4 MiB must never be truncated. Build the bounded MiSans image with:

```powershell
python tools/build_font.py D:\AI\MiSans-Normal.ttf build\font\MiSans-Normal-gb2312.ttf
```

The builder includes printable ASCII, Latin-1, the GB2312 repertoire, and MixOS interface symbols. It reopens the generated TTF, verifies every selected Unicode mapping, enforces the partition limit, and writes a SHA-256 manifest next to the output. The generated font can be written independently of the application only after the live partition offset and size have been rechecked and the current ESP flash has a recovery backup. A supplied font remains subject to its own license; absence of embedded TTF license metadata is not proof of redistribution permission.

No device connection or flash operation is part of this local phase.

## STM32/QMK

Use the pinned QMK source and the ARM cross compiler specified in `docs/SOURCES.md`. Confirm that the build uses the MixOS keyboard copy and that the normal USB keyboard report path is disabled. Bootmagic and the documented local rescue combination must remain independent of ESP or Linux.

## Warnings and limitations

The portable host build is authoritative for protocol and parser behavior only. It does not prove GPIO mux safety, LCD timing, audio codec operation, I2C electrical recovery, USB endpoint coexistence, PSRAM placement, or battery-gauge calibration. Those require staged hardware validation.
