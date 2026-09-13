# MixOS

MixOS is an isolated firmware and host-service project for the dual-chip device. The ESP32-S3 owns the display, touch input, local navigation, terminal rendering, power status, and audio-facing device behavior. The STM32 scans the keyboard and reports bounded input events over I2C. A Linux `mixosd` service provides one ordinary-user PTY session, host metrics, and fixed-scope jobs over the existing USB CDC link.

## Hardware status

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

Routine ESP32-S3 updates go over the existing USB CDC link with one command, `tools/deploy_ota.py`, which streams the image into the unused application slot: the running build is never written, nothing is selected until the image arrived and its SHA-256 matched, and a build that fails to prove itself is rolled back by the bootloader. This requires the A/B partition layout in `firmware/esp32s3/partitions.csv`. A device still on the historic single-application layout needs one serial migration first, `tools/deploy_display.py --stage --migrate`, which rewrites the bootloader, the partition table and the application while leaving `nvs`, `phy_init` and the 4 MiB font partition byte-identical. Both paths are described in `docs/ESP_OTA.md`.

## Protocol

USB CDC uses protocol v1 in `protocol/USB_V1.md`: COBS frames terminated by zero, bounded payloads, CRC32, fresh connection epochs, cumulative terminal credit, and explicit maintenance messages. The protocol is transport integrity and sequencing, not authentication.

## License and provenance

Source provenance and license locations are recorded in `docs/SOURCES.md`. The MixOS copies are independent working trees; no changes are made to the original repositories by this project.
