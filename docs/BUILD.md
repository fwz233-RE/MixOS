# Build and verification

## Host baseline

Run the CMake and Python commands in the root `README.md`. They are hardware-free and may be run from WSL Ubuntu 22.04.

## ESP32-S3

The intended toolchain is ESP-IDF 5.4.2 with the ESP32-S3 target. Use the project-local tool setup described in `docs/HOST_TOOLS.md`. Before any build, inspect the resolved component lock and verify that the ES8389 codec overlay is selected by CMake. A successful configure/build must produce the application, bootloader, and partition artifacts without modifying the original ESP repository.

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
