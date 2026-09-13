# MixOS STM32 keyboard

This directory is the **USB-silent, I2C-only input** derivative of the KeebDeck 6x11 QMK keyboard. Original `D:/TheEndDEvice/TypixDeck-*` repositories are untouched. Historical original-firmware compile/flash results do **not** validate this changed firmware.

- Pinned upstream: QMK **0.28.0**, commit `a63fd7f01cdabd9ce85bb09ae2b573fd3b8e60aa`; see `QMK_PIN.json`.
- Both `default` and `diag` use the same all-custom physical keymap. They produce no normal/consumer HID input, including all five space contacts. USB descriptors may still enumerate; there is no USB keyboard fallback when ESP disconnects.
- Input interception is `pre_process_record_kb`, after QMK debounce and ghost rejection, before tapping/user/HID processing. All blank row-0 locations remain `KC_NO` for QMK ghost detection and are also rejected by physical mask.
- Bootmagic: hold **Tab (row 2, column 0)** while powering on. Runtime rescue: hold **Fn (3,0) + diamond (0,9)**, or **Sym (5,0) + diamond**, with no other real key, for **3 seconds**. Rescue and scanning continue without ESP/USB host. Ordinary short diamond and Fn+Space are ESP backlight actions, not direct QMK keycodes.
- Software backlight on PA15 remains enabled (default level 3, range 0..8); ESP writes absolute levels without EEPROM wear.
- I2C1 PB6/PB7 AF1, address `0x1f`; PA13 open-drain FIFO interrupt; USB PA11/12 remapped to PA9/10 as on the original F042 board.
- Matrix rows: PA0, PF1, PF0, PB8, PB5, PB4. Columns: PA14, PB3, PA1..PA7, PB0, PB1. PB8 is also BOOT0; real hardware handling remains necessary for ROM recovery.

Full interfaces, layout, integration and validation checklist: `../../docs/keyboard.md`. Wire format: `../../protocol/KEYBOARD_V1.md`.

## Verification and build

The changed MixOS `default` and `diag` targets have been compiled and linked against the pinned QMK revision with ARM GCC 10.3.1 under WSL Ubuntu 22.04. The automatic workflow is `python tools/build_keyboard.py` from the MixOS root using the QMK virtual environment. It stages this keyboard (excluding `.git/tests/tools/release`), compiles both targets, audits both ELF memory layouts including stacks, runs the pinned-source verifier and all `tests/test_keyboard*.py`, and records binaries, debug ELFs, linker maps, logs, tool versions and SHA-256 hashes in `build/keyboard`. See [BUILD.md](BUILD.md) for the exact one-command WSL invocation, dependencies and artifact descriptions. For deployment through the safe DFU worker, use the separately validated **`keebdeck_6r11c_default.raw.bin`** or **`keebdeck_6r11c_diag.raw.bin`** (15,900 bytes). QMK's regular `.bin` includes a 16-byte DFU suffix and is intentionally rejected by that worker. The raw export is derived from the ELF, checked against the CRC-validated QMK payload, and accepted by the worker's portable image validator.

The final two 1 KiB flash pages are reserved for QMK EEPROM emulation by `ld/STM32F042x6.ld`; the application linker region is 30 KiB rather than the full 32 KiB. Both targets use **15,900 bytes of loaded flash**. Static RAM is **2,772 bytes**, including the 416-byte transport state and the ChibiOS idle-thread working area. Adding the unchanged **1,536-byte process stack**, **768-byte interrupt stack**, and 4 bytes of alignment gives **5,080 / 6,144 bytes**, leaving **1,064 bytes** unused. The transport's 384-byte FIFO and total persistent transport storage below 576 bytes are preserved. Runtime stack high-water marks still require hardware measurement.

`tools/verify_qmk.py` independently downloads seven read-only upstream files at the pinned commit and verifies their recorded hashes and callback order; this is source verification, separate from the target build. `QMK_PIN.json` sets `target_build_verified=true` only after both builds, memory audits, source verification and native tests pass. `hardware_verified` remains **false**: no changed firmware has been flashed, no USB traffic has been captured, and no electrical I2C or matrix test was performed. The pinned USB startup has a fixed 50 ms initialization delay but no wait for host enumeration. `release/flash_kbd.sh` continues to refuse legacy batch flashing; the built images are development artifacts, not hardware-qualified releases.

SPDX: GPL-2.0-or-later for QMK-derived keyboard firmware; see `LICENSE`.
