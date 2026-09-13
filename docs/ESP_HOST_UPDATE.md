# ESP host-authorized update build

## Firmware behavior

This change supersedes the mandatory on-device confirmation described in older update documentation. An explicit administrator update request on the established maintenance protocol now receives `UPDATE_READY` automatically. The host remains responsible for authorizing its operator (`--execute` and the deployment launcher's existing administrative policy). The USB maintenance protocol does not cryptographically authenticate the peer.

Only an online link's `PREPARE_UPDATE` on maintenance channel 4, with a nonzero request/session ID, the current epoch, a fresh sequence and an empty payload can create a grant. The terminal is closed first. A successful READY enqueue grants exactly that request for 15 seconds. A failed enqueue cannot grant permission. Duplicate PREPARE requests cannot extend the grant or replace it with another request.

`PREPARE_UPDATE` does not boot. Only an empty `ENTER_BOOT` with the matching channel, epoch and granted request/session, a fresh sequence, and an unexpired grant requests ROM entry. The grant is consumed before the boot request is published, and the public boot-request accessor consumes that request once. Disconnect, link restart, heartbeat timeout and grant expiry revoke the grant. No new PREPARE is accepted while a boot request is still awaiting consumption.

`mix_link_update_answer()` remains as a no-op for source compatibility with the UI action dispatcher. Firmware-generated view state never requests an update-confirmation modal. The old UI modal and help strings were inspected but left byte-for-byte unchanged to avoid a font rebuild and unrelated UI changes. Other local confirmations, such as starting a host job, are unchanged.

## Scope

Only MixOS firmware, its native tests, local build/report helper, and build evidence were changed. No deployment launcher, sibling TypixDeck repository, SSH connection or hardware operation was used by this work. Host backup, identity and full-readback requirements remain the deployment owner's responsibility and were not relaxed here.

## Reproduction and evidence

From `D:\TheEndDEvice\MixOS` with Windows Python 3.12:

```text
py -3.12 -m unittest discover -s tests -p test_link_update.py -v
py -3.12 -m unittest discover -s tests -p test_preview_ui.py -v
py -3.12 -m unittest discover -s tests -p test_protocol.py -v
py -3.12 -m unittest discover -s tests -p test_linux.py -v
py -3.12 tests/esp_font_build.py build
```

The six new maintenance tests compile actual `mix_link.c`, `mix_protocol.c` and `mix_terminal.c` under WSL Ubuntu-22.04 as `fwz233`, with GCC warnings-as-errors, AddressSanitizer and UndefinedBehaviorSanitizer. SDK queue/USB/task/JSON calls are stubbed; no hardware is opened. Cases cover automatic readiness without UI input, terminal closure, malformed/offline/wrong-epoch/wrong-session requests, strict expiry including timer wraparound, sequence replay, one-time boot entry, disconnect/restart/heartbeat revocation, and queue failure. The existing UI harness also verifies that the automatic-ready notice with firmware's nonpending view does not open a modal or emit a UI action.

The application build uses the existing pinned ESP-IDF 5.4.2 / Xtensa GCC 14.2.0 WSL setup documented in `ESP_FONT_BUILD.md`. Both prior images are verified and preserved before compilation:

- `build/esp32s3/previous-mixos_esp32s3.bin`: 469104 bytes, SHA-256 `9593904eab8c1bcaaef9c383abc9a5308ae4beeece94989436360289eafc675b`.
- `build/esp32s3/previous-font-candidate-mixos_esp32s3.bin`: 857136 bytes, SHA-256 `c0e99db4d5cb854e5ea2bf24753fcfb17ef77f8bce7b3b1c6a20b824418e2a69`.

Current app sizes/hashes, exact source hashes including `mix_link.c`, independent esptool image checksum/embedded-hash validation, linked font symbols and font provenance are in `build/esp32s3/font-app-build.json`. Build output is in `build/esp32s3/font-app-build.log`.

Build completed with exit 0 on 2026-09-11. Both `firmware/esp32s3/build/mixos_esp32s3.bin` and `build/esp32s3/mixos_esp32s3.bin` are **857008 bytes** (`0xd13b0`), SHA-256 **`bf60ca6891029d011d1d5c8cddb59ae4e711a1404b017ab042a65abeac7819e5`**. The 0x200000-byte app partition has 0x12ec50 bytes free. Esptool 4.12.0 reports checksum `03` valid and embedded validation hash `17c586a5dfcfc91c8807bc73defdf23e78cfbc2259e000f92a0d29f8ca6e5768` valid. The build explicitly compiled `mix_link.c` and generated the app. It emitted existing misleading-indentation warnings, an unset component version-environment warning, and subsecond WSL-mounted-filesystem clock-skew warnings; it had no compile error.

Tests: 6 maintenance + 4 UI + 10 Python protocol + 20 Linux-host unit tests passed on Windows (3 Linux-only PTY cases skipped). The 2 cross-language protocol tests were skipped on Windows, then both passed under the existing `/home/fwz233/mixos-host-venv/bin/python` in WSL. Total: **42 passed**, with 3 Linux-only PTY tests not run in this scoped change. The native maintenance tests had no sanitizer findings.

## Font provenance caveat

No font or UI-source change is required by this firmware behavior change. The existing generated font is 1724276 bytes with SHA-256 `859ec555afa5dfbd234fc80e7058aba243d85e3f32551f266d0457ea326d8bfc`.

The pre-existing font manifest records the historical CRLF UI-source hash (`afa1e20d3c2481b32da16558b4352951774602d3e5de96deaf871652573c2697`), while the current workspace UI source uses LF. The report records the actual source hash and separately checks exact-byte versus CRLF-normalized equivalence. A normalized match is evidence of unchanged text, **not** a claim that exact-byte release validation passes. Release staging must handle the pre-existing line-ending provenance mismatch explicitly; this change does not silently rewrite the UI source or manifest.

## Hardware status

The firmware-scoped build/test work above did not operate hardware. The subsequent integrated deployment **did** write this exact app and the font once, verify a fresh complete 8 MiB readback, and start the new application using a separately recorded boot-only watchdog reset after the job's RTS reset failed to exit download mode. The running firmware passed three heartbeats over 15.016 seconds and automatically returned UPDATE_READY about 0.200 seconds after host PREPARE without local touch/key input. The post-flash readiness test sent no ENTER_BOOT and did not reflash. The Linux service was restored. Full integrated regression passed 198 Python tests with no skips and CTest 2/2. See `DEPLOYMENT.md` and `build/deploy/display-20260911-host-authorized.json` for precise evidence and the original job's preserved failure status. Physical LCD appearance and keyboard input remain unverified.
