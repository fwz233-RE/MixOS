# Integrated device deployment

The delivery consists of the ESP32 display/application, its raw MiSans font partition, the STM32 I2C keyboard firmware, and the existing unprivileged Linux terminal service. Build success, upload/staging, verified flash contents, and actual input/display behavior are separate states; this document does not treat one as proof of another.

## Current device and preserved state

- CM5: `typixdeck`, `192.168.1.22`, Debian 13. Internal USB Host routing must remain selected. The ESP32 and keyboard are on physical ports `5-1.2` and `5-1.1` respectively. External USB gadget routing disconnects both internal devices.
- `mixosd.service` is installed under `/opt/mixos/linux`, runs as `pi:dialout`, and serves the existing 80×28 ordinary-user Bash terminal over the ESP's CDC interface. The Linux desktop remains installed; `multi-user.target` is the reversible headless boot choice.
- The previously deployed ESP application is preserved in `build/esp32s3/previous-mixos_esp32s3.bin`, SHA-256 `9593904eab8c1bcaaef9c383abc9a5308ae4beeece94989436360289eafc675b`. A previous full 8 MiB backup remains on the CM5 at `/home/pi/mixos-flash-20260910-191910/original-flash-8MB.bin`. A new deployment makes a fresh backup rather than assuming that historical snapshot is current.
- The earlier ESP deployment passed the chip-side write hash and application heartbeat. Its later independent app readback did not complete; no independent-readback success is retroactively claimed.

## Why the Chinese fix includes an application update

The previous application never called `ttf_font_init()` before initializing the UI. Consequently, a valid font partition alone could not enable Chinese. The fix initializes the font, retains an ASCII recovery interface if loading fails, and uses glyph metrics when fitting proportional MiSans glyphs into fixed terminal cells. The font builder records actual UI coverage and a host FreeType load check. See `FONT_BUILD.md` for repertoire/alias/license limitations.

## Build evidence

- ESP: `build/esp32s3/font-app-build.json`, preserved previous binary, and new application binary/build log.
- Font: `build/font/MiSans-Normal-gb2312.ttf` and its adjacent manifest. Staging verifies current `mix_ui.c` against the manifest using only exact raw/LF/CRLF representations; arbitrary source changes are rejected. Font and application binaries remain exact-hash checked.
- Keyboard: `build/keyboard/manifest.json`, both keymap binaries, ELF/map files and memory reports. `target_build_verified` requires the pinned source and automated checks. `hardware_verified` remains false until actual device evidence is obtained.

## Keyboard clock correction and boot result — 2026-09-11 13:11 (UTC+8)

A keyboard-local HSI48 oscillator-enable correction was clean-built for both
keymaps; actual ARM disassembly confirms the enable and readiness wait.
The raw image is 15,920 bytes, SHA-256
`8ec21047a264da7c968070812af30dc2b675a1ee67e7e8cb5cf3762492388706`.
The previous complete build is preserved at
`/home/pi/mixos-keyboard-build-before-clock-20260911`.
The full host suite passed **205 tests, zero skips; CTest 2/2**, including the
actual font and renderer. An initial incremental build retained stale board
objects; it was rejected before staging. Future target builds use `--clean`.

Receipt `build/deploy/mixos-keyboard-20260911-050842.json` was started exactly
once with the keyboard already in ROM device 8; no physical button was needed.
The worker backed up all 32 KiB (SHA-256
`83787b1b472d0452825fe4732c141a6a50227a99379b4e89ed571b24bc495920`),
wrote 16 KiB of application-covered pages once, and verified all 32 KiB
(SHA-256 `a6a89ddeb1c4ffd8e2184beaa9ce4913f526022d0a9e3500a06897f6b49238d7`),
including every untouched tail/EEPROM byte. Its single leave request returned
zero, but the kernel then recorded ROM `0483:df11`, serial `FFFFFFFEFFFF`,
**device 21** at `5-1.1`. Application startup, I2C communication and physical
input remain unverified. The clock fix did not resolve ROM return; no repeat
write or leave was issued. The ESP remains `303a:80c3`, `TD0720`, device 20;
`mixosd.service` remains active. Evidence:
`build/deploy/keyboard-20260911-clock-deployment.json`.

## Stage once, then authorize from the host

`tools/deploy_display.py --stage` uploads the verified app/font package and saves a receipt under `build/deploy`. `tools/deploy_keyboard.py --stage` does the same for the verified default keyboard build. Staging does not stop services, reset chips, change USB routing, or write flash.

The display launcher accepts `--start RECEIPT`; this explicit host command authorizes the update, with no mandatory on-device confirmation in the new firmware. First migration from the old application may require hardware ESP BOOT + S3_EN entry because the old application's confirmation policy is still running. It temporarily stops `mixosd`, starts a detached single-execution job, and restarts the service on completion or failure. The worker verifies the old app, exact live partition table, chip/security identity and a fresh 8 MiB backup before touching flash. With the explicit new-app option it writes only the 2 MiB app partition and 4 MiB font partition. Its full 8 MiB readback must exactly equal the expected image, including all unchanged bootloader/table/NVS/other bytes. The original app-only updater remains unchanged.

The keyboard requires the local Fn+diamond or Sym+diamond rescue chord when running an application; fresh inventory confirming it is already in ROM DFU requires no additional physical entry action. Its actual ROM serial is obtained with `tools/deploy_keyboard.py --inventory RECEIPT`, then passed to `--start RECEIPT --serial EXACT_SERIAL`. The root-owned isolated worker makes a complete 32 KiB backup, writes only application-covered 1 KiB pages, verifies all 32 KiB including unchanged tail pages, then requests DFU exit. It leaves `mixosd`, the ESP, USB routing and power alone. See `KEYBOARD_FLASH.md` for the exact safeguards.

Both launchers have `--status RECEIPT`. A submitted systemd job is not a successful flash. After a disconnect or failure, inspect that same job and its audit; never blindly repeat `--start`. Backups are retained, and neither tool performs automatic rollback, unprotect or chip erase.

## Current host-authorized migration — 2026-09-11

The user explicitly removed mandatory screen confirmation. New app `bf60ca6891029d011d1d5c8cddb59ae4e711a1404b017ab042a65abeac7819e5` (857008 bytes) automatically grants a valid maintenance request; the matching one-use ENTER_BOOT is still required. See `ESP_HOST_UPDATE.md`. Host `--execute`, exact identity/security checks, fresh full backup, single-submission writes and complete readback remain mandatory.

Receipt `build/deploy/mixos-display-20260911-040257.json` was started once after fresh hardware ROM enumeration as `303a:1001`, device 18, with the exact MAC and USB path. It includes pinned esptool 5.4.0 and modern ESP32-S3 stub 1.2.2, including the released USB full-packet termination fix. All 55 esptool package files match the pinned official source distribution. Its isolated import with a controlled configuration and artifact dry run passed on the CM5 without opening hardware. Full regression passed: **198 Python tests, zero skips; CTest 2/2**, with the actual font and renderer enabled. The fresh 8 MiB backup completed in approximately 165 seconds and was durably saved with SHA-256 `34bd99cae7f4a555281f19a4d65ddb5f3d6ac7dec4129f4ea32223adb51276a0`. Live layout/app validation passed; the 2 MiB app and 4 MiB font partitions were each written once and passed chip-side hashes. The complete 8 MiB readback then exactly matched the expected image, SHA-256 `b7d8e82940788c2040f6a54f99e138ba6e4a53db9e59e232c11648f5a0509f8b`, including all unchanged protected bytes. Both complete snapshots were copied to `build/deploy/display-20260911-040257-{original-flash,readback}-8MB.bin` and their local hashes verified.

The original job retains its truthful exit-code failure: its default RTS reset did not leave hardware-entered ROM within 40 seconds, after readback had already passed. It was not restarted. A separately claimed boot-only operation checked the stopped job, exact stored readback, unchanged USB device 18, exact MAC, zero security flags and cleared force-download register, then issued one official ESP32-S3 watchdog reset without programming flash. The new application enumerated as `303a:80c3`, `TD0720`, device 20. A separate post-flash check passed HELLO plus three heartbeats over 15.016 seconds and observed automatic UPDATE_READY approximately 0.200 seconds after PREPARE without local touch/key input. That check sent no ENTER_BOOT and performed no flash programming. `mixosd.service` was restored and confirmed active. The physical LCD appearance and keyboard input remain unobserved.

The prior read-only diagnostic at 11:40:10 (UTC+8) failed to connect after a software reset (`termios` input/output error); it performed no flash read or write and restored the old application USB `303a:80c3`, `TD0720`, device 17. The subsequent hardware ESP BOOT + S3_EN entry produced fresh ROM device 18 for the migration, without screen confirmation or a countdown. The keyboard remains ROM device 8, unchanged. Evidence: `build/deploy/display-20260911-host-authorized.json`.

## Previous attempt — 2026-09-11 10:47 (UTC+8)

Job `mixos-display-20260911-024626` **received fresh local confirmation** at 10:46:48, identified the exact ESP ROM/MAC and 8 MiB flash, and started the backup with pinned esptool 4.8.1. It reported 64/128/192 KiB in approximately 1.24/2.46/3.69 seconds, then stopped at 10:47:04 with `Packet content transfer stopped (received 4033 bytes)`. No backup was published, no write claim exists, and no app/font flash writes started. Worker/control PIDs are zero. `mixosd` restarted, but the ESP remains USB-OTG ROM `303a:0009`, device 16; no app link is claimed. The prior slow measurement used `303a:1001`, so the current faster partial read cannot be attributed solely to the tool upgrade. No programming or consent replay was attempted after failure. User was told they could leave the device powered with Host retained; no further physical action requested. Evidence: `build/deploy/display-20260911-024626-attempt.json`.

## Previous attempts — 2026-09-11 10:29–10:36 (UTC+8)

Job `mixos-display-20260911-022937` subsequently started at 10:33:32 but also timed out waiting for local approval at 10:35:02 without writing. The user reported no dialog. A separate HELLO/PONG-only trace confirmed the old application remained responsive. Source inspection established that the host waits 90 seconds but firmware grants only a 30-second pending-confirmation window; a waiting log alone does not establish popup display. Evidence: `build/deploy/display-20260911-022937-attempt.json`.

- The old ESP application recovered before this attempt and passed HELLO plus three heartbeats over 13 seconds.
- Display-only tooling now pins a locally built wheel from official esptool 4.8.1 sources, including the released USB Serial/JTAG read-flush fix. Wheel SHA-256: `d554ff923993071819b60b9e3d4b20185de5ef28a7134407ded742b469dfd5fb`. The app-only updater and firmware/font bytes are unchanged. Existing read deadlines and single-submission writes are unchanged. CM5 isolated import/default-stub loading passed; actual improved read throughput remains unverified.
- Regression: 180 Python tests passed with no skips; CTest 2/2 passed.
- Job `mixos-display-20260911-021351` requested local confirmation at 10:18:43 and exited at 10:20:13 because no confirmation arrived. Its stopped process IDs are zero. It has no boot-send, ROM, backup or write events and no flash writes.
- The kernel recorded the ESP entering `303a:0009` download mode at 10:27:20, after the job had stopped. The user reported clicking late and a black screen. Firmware has not been updated by this job.
- Fresh receipt `build/deploy/mixos-display-20260911-022937.json` is **staged only**, with no active confirmation countdown. The user was asked to short-press **S3_EN alone**, BOOT released and Host retained. Verify the old application returns and wait for user readiness before starting the fresh job. Do not restart the expired job or reuse its consent.
- Evidence: `build/deploy/display-20260911-attempt.json`. Keyboard remains ROM `0483:df11`, device 8; no keyboard operations were performed in this session.

## Earlier deployment and recovery state — 2026-09-11 (UTC+8)

The receipts below identify the original packages and one subsequent bounded recovery attempt. Both display jobs are stopped; neither wrote flash. The original display consent was consumed by the one-use recovery claim, and neither job may be blindly restarted.

- Display receipt: `build/deploy/mixos-display-20260910-175627.json`; staging `/home/pi/mixos-display-20260910-175627`. The Pi-side no-write preflight passed for the new app and font. App SHA-256: `c0e99db4d5cb854e5ea2bf24753fcfb17ef77f8bce7b3b1c6a20b824418e2a69`; font SHA-256: `859ec555afa5dfbd234fc80e7058aba243d85e3f32551f266d0457ea326d8bfc`.
- Keyboard receipt: `build/deploy/mixos-keyboard-20260910-180127.json`; staging `/home/pi/mixos-keyboard-20260910-180127`. It contains the **15,900-byte `.raw.bin`**, SHA-256 `932c9892566e53050636c9b06d005ce91885c908d60f6ecd0e9779d12c4b1b82`, not QMK's DFU-suffixed `.bin`. ELF audit reserves 5,080 of 6,144 SRAM bytes including both stacks; the final two flash pages remain reserved for EEPROM.
- **Keyboard flash was written and fully verified.** The worker saved all 32,768 original bytes (SHA-256 `d0af7028ce83645aa7db1225236ab84bcb01bc16aa47f67f3ecd997da0766dcf`), wrote 16,384 application-covered bytes once, and verified all 32,768 bytes including unchanged tail pages (SHA-256 `83787b1b472d0452825fe4732c141a6a50227a99379b4e89ed571b24bc495920`). Evidence remains under `/var/lib/mixos/keyboard-flash/mixos-keyboard-20260910-180127`.
- Its first DFU leave returned zero, but the kernel recorded ROM re-enumeration in the same second. A separate tested `tools/leave_verified_keyboard.py` diagnostic freshly uploaded and matched the exact full-flash hash, then issued exactly one leave-only command without programming. That command also returned zero; ROM re-enumerated as device 8 at 18:44:51 UTC. Evidence: `/var/lib/mixos/keyboard-flash/mixos-keyboard-leave-20260910-184500`. No further leave or programming retries are authorized by this diagnostic. The retained SRAM marker alone is insufficient to explain both returns. Application boot and keyboard input remain unverified.
- **The display update received local approval but stopped before any flash write.** ROM security checks passed and the stub started, then the full-backup stream stalled. The original workdir contains no backup, write claim, or write-start event. The service was stopped; `mixosd` restarted. No new application or font bytes were written.
- **ESP hardware recovery succeeded at 18:55:01 UTC.** After locating the correct side ESP BOOT and round `S3_EN` buttons, the user reset the chip. The kernel recorded device 5 disconnecting and fresh `303a:1001` device 9, serial `70:04:1D:D8:54:14`, at `5-1.2`. A bounded no-stub, no-reset, read-only `flash_id` probe successfully identified the ESP32-S3-PICO-1, exact MAC, and 8 MiB flash. Its blank screen is compatible with download mode. Keep Host selected; no further button press is currently needed.
- Normal re-staging stopped locally because current `mix_ui.c` uses LF line endings (SHA-256 `5dbb90ea96823d98cfdb09eaa7ba8b691f369c7b27981990e842d753c5005039`) while the built release recorded CRLF (`afa1e20d3c2481b32da16558b4352951774602d3e5de96deaf871652573c2697`). Reconstructing CRLF from the current 319 LF lines reproduces the exact recorded hash and 23,962-byte length; there is no other content difference. The working file is preserved. Recovery staging explicitly binds to the original approved app/font artifacts rather than rebuilding or changing their hashes.
- Recovery receipt `build/deploy/mixos-display-20260910-190739.json` was staged with `--stage --stage-from-receipt` against the original receipt. It retains every original app/font/layout/helper byte except the corrected font worker and generated launcher. Starting it once with `--resume-no-write-source` verified the original approval/no-write evidence, then exclusively consumed that source. ROM security, stub startup, explicit SPI attach, flash ID and parameters all passed. Reading the first 256 KiB backup chunk reported 65,536 bytes at 47.17 seconds, then hit its 60-second deadline. The worker exited with an audited failure at 19:09:17 UTC, before any backup publication or flash write; `mixosd` restarted. This demonstrates slow progress, not a total lack of communication. Exact cause remains unresolved. No automatic resume replay is permitted.
- A subsequent no-write `usb_reset` / chip-MAC probe / hard-reset command completed successfully, but the ESP remained `303a:1001`; application USB and heartbeat were not observed. The next minimal physical action is a short press of **S3_EN alone**, with ESP BOOT released, to leave hardware-entered download mode. Keep Host selected.
- Final automated regression: **175 Python tests passed, zero skips; CTest 2/2 passed**. Build-source verification permits only exact recorded bytes or their LF/CRLF representations; firmware and font hashes remain strict.
- Both application links are currently unavailable. Chinese display and actual I2C keyboard operation are **not verified**. `mixosd` being active does not establish a working ESP link.

At start, reviewed payloads are installed into an exclusive root-owned package. Display package: `/opt/mixos-display-packages/JOB`; its unprivileged flash worker writes backups/audit under `work/session`. Keyboard package: `/var/lib/mixos/keyboard-packages/JOB`; its privileged worker writes backups/audit under `/var/lib/mixos/keyboard-flash/JOB`. Uploaded home-directory staging never serves as a privileged executable source without hash-verified root-owned installation.

- **Validated fast recovery flash (2026-09-12):** The application-oriented launcher rejected a correctly hardware-entered ROM device because it required the running-app USB identity. A separate explicit, operator-approved recovery path stopped `mixosd`, saved a complete 8 MiB backup, wrote the corrected application package, completed `Hash of data verified`, read the full 8 MiB back, matched the 859824-byte application segment to SHA-256 `f2ffff9bb37f1c2caae1d52d9a860204eb3e43bcef8d7e07f0089949fdc7e24d`, and restored `mixosd` to active. The complete-image hash differs from the backup by design because the application was replaced. The recovery path is now documented in `docs/HOST_TOOLS.md`; it must be implemented as a reviewed tool before routine use.


A deployment record must identify the actual job receipts, artifact hashes, backup paths, readback result, and observed application/USB state. Hardware-only display and keyboard input behavior must be described as observed or unverified, not inferred solely from enumeration or build results.

Local AI model execution, independently verified CM5 power sequencing, and standalone local audio operation are not provided or certified by this app/font/keyboard deployment. The Linux service is a headless terminal/metrics/task service, not a Linux reinstallation or an AI model runtime.
