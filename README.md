# MixOS

MixOS is an isolated firmware and host-service project for the dual-chip device. The ESP32-S3 owns the display, touch input, local navigation, terminal rendering, power status, and audio-facing device behavior. The STM32 scans the keyboard and reports bounded input events over I2C. A Linux `mixosd` service provides one ordinary-user PTY session, host metrics, and fixed-scope jobs over the existing USB CDC link.

## Current hardware status — 2026-09-18

The ESP32-S3 completed **10 consecutive button-free A/B application updates** on the test device. Each update verified the complete application-file SHA-256, ELF identity, expected running and boot slots, a new boot ID, actual VALID state, maintenance health acknowledgement, sustained heartbeats, and Linux service restoration. The operator confirmed that the final `532717dd` build displays normally.

The native Linux command `mixos-esp-update` is installed on the test device. A submitted update runs as a persistent systemd job and does not depend on Windows or a continuous SSH connection. Two unanswered runtime-measurement requests during the ten-update sequence recovered through strictly bounded, read-only remeasurement; their exact underlying loss point remains unproven. Previous failed sequences remain recorded and were not counted toward the successful ten.

The latest full regression ran 1065 tests on each platform: Linux passed 1061 with 4 skips; Windows passed 1050 with 15 skips. This is normal-update acceptance, **not a guarantee of recovery from every hang or power loss**. Deliberate fault/power-loss tests remain pending; the operator chose to preserve the working device until a spare board or an independent reset channel is available. See [deployment evidence and limitations](docs/DEPLOYMENT.md), [updater usage](docs/ESP_OTA.md), and [implementation details](docs/ESP_OTA_V2.md).

Raw hardware evidence, full Flash backups, build outputs, and local toolchains are retained outside Git under ignored directories. The repository contains source, tests, documentation, and evidence references; publishing it does not also upload those recovery files.

## Historical hardware milestone — 2026-09-11

**The new ESP application and font are deployed and full-readback-verified (2026-09-11).** Mandatory display-update screen confirmation has been removed: an explicit host update command now authorizes maintenance. The new app/font were written once after a fresh 8 MiB backup; the complete 8 MiB readback matched the expected image, including all unchanged protected bytes. The default RTS reset left the chip in download mode, so a separately recorded, identity-checked watchdog reset started the new application without reflashing. The new firmware passed a 15-second heartbeat check and returned automatic `UPDATE_READY` about 0.2 seconds after PREPARE, without local touch/key input or another boot request. `mixosd` is active. The STM32 USB-clock correction was then clean-built and flashed once without physical buttons; full 32 KiB readback passed, but its application still returned to ROM (USB device 21). Keyboard startup/input health remains unresolved; actual LCD appearance and keyboard input are not claimed verified. See `docs/DEPLOYMENT.md`, `docs/ESP_HOST_UPDATE.md`, and `build/deploy/RELEASE.json` for evidence.

The following describes the earlier ESP revision, not current device availability: On 2026-09-10, the ESP32-S3 application was written at `0x10000` after an 8 MiB recovery backup and live partition/font checks; esptool's chip-side data hash passed, and MixOS completed HELLO/ACK plus seven PING/PONG heartbeats over 15 seconds. A later independent application readback did not complete because the USB link re-enumerated abnormally, so no readback SHA256 is claimed. The Linux host service is installed as an unprivileged `pi:dialout` systemd service, the machine boots to `multi-user.target`, and post-reboot bidirectional CDC traffic was observed. The original source repositories remain outside this directory and unchanged.

## Layout

- `firmware/esp32s3/` — independent ESP-IDF application copy.
- `firmware/keyboard/` — independent QMK keyboard copy.
- `linux/` — protocol library, daemon, PTY service, and deployment helpers.
- `protocol/` — versioned USB and keyboard wire contracts.
- `tests/` — portable C and Python tests.
- `tools/preview/` — offline UI preview.
- `docs/` — build, recovery, Linux, UI, and implementation notes.

## Local tests

From WSL Ubuntu 22.04:

```sh
cd /mnt/d/TheEndDEvice/MixOS
cmake -S tests -B build/host
cmake --build build/host
ctest --test-dir build/host --output-on-failure
python3 -m venv /home/fwz233/mixos-host-venv
/home/fwz233/mixos-host-venv/bin/pip install -r tools/requirements-host.txt
/home/fwz233/mixos-host-venv/bin/python -m unittest discover -s tests -v
```

The current host suite covers the COBS/CRC protocol, terminal parsing, power integration, keyboard state handling, STM32 safety hooks, Linux PTY behavior, flow control, reconnects, maintenance authorization, updater preflight, and UI preview contracts.

## Build and deployment status

The ESP32-S3 application has been cross-built and an earlier revision deployed to the test unit. A subsequent integrated revision fixes font initialization and terminal glyph fitting. Both STM32 keyboard keymaps now have real pinned target builds, with ELF/map memory evidence under `build/keyboard`; a build is not a hardware flash claim. The Linux protocol and PTY suite also passes natively on the CM5, including the Linux-only raw PTY cases. The exact deployed service and udev configurations are `linux/mixosd.typixdeck.service` and `linux/99-mixos.rules.typixdeck`; generic installations must still derive their own device identity rather than copying those values. For the combined app/font/keyboard package, protected backup/readback workflow, and actual deployment records, see `docs/DEPLOYMENT.md`. The verified ROM recovery flashing procedure is `docs/ESP32_RECOVERY_FLASH.md`. Build instructions are in `docs/HOST_TOOLS.md` and `docs/BUILD.md`.

## Updating the display firmware

Routine ESP32-S3 updates use the Linux-native command `mixos-esp-update apply --package /path/to/release --timeout 60 --health-timeout 180 --wait`. It validates a self-contained release, coordinates exclusive CDC access with `mixosd`, transfers to the inactive application slot, and verifies the actual running image and service restoration before reporting success. Use `mixos-esp-update status --job <job-id>` to inspect an existing job; uncertain outcomes require transaction reconciliation, not a blind reflash. `tools/deploy_ota.py` is an optional SSH wrapper around the same updater.

A/B rollback protects an unconfirmed candidate when a reset allows the bootloader to act; it does not imply that every later hang of a VALID image automatically switches slots. The current test device already has the A/B layout and needs no migration. Historical single-slot migration and first-safe-install procedures are separate, explicitly authorized operations, not automatic fallback paths for routine update failures. See [ESP_OTA.md](docs/ESP_OTA.md) for prerequisites and recovery boundaries.

## Protocol

USB CDC uses protocol v1 in `protocol/USB_V1.md`: COBS frames terminated by zero, bounded payloads, CRC32, fresh connection epochs, cumulative terminal credit, and explicit maintenance messages. The protocol is transport integrity and sequencing, not authentication.

## License and provenance

Source provenance and license locations are recorded in `docs/SOURCES.md`. The root license does not replace the licenses of bundled third-party components, fonts, or keyboard firmware; retain their accompanying notices and comply with their respective terms. The MixOS copies are independent working trees; no changes are made to the original repositories by this project.
