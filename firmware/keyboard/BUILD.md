# Reproducing the STM32 keyboard build

Run the entire target build, memory audit, pinned-source verification and native
keyboard tests with one command from PowerShell:

    wsl.exe -d Ubuntu-22.04 -- /home/fwz233/mixos-qmk-venv/bin/python /mnt/d/TheEndDEvice/MixOS/tools/build_keyboard.py

The script never flashes, opens a serial device, or uses SSH. It copies only the
MixOS keyboard into the separate pinned QMK checkout; `.git`, `tests`, `tools`,
and `release` are excluded. Originals outside MixOS are not build inputs.

## Toolchain and dependencies

The verified environment uses Ubuntu 22.04 under Windows Subsystem for Linux,
ARM GCC 10.3.1 (20210621), GNU binutils 2.38, newlib, GNU make and the QMK Python
requirements in `/home/fwz233/mixos-qmk-venv`. The QMK CLI package is 1.2.0;
the firmware source is **QMK 0.28.0**, not the CLI package version.

QMK must be checked out at the commit in `QMK_PIN.json`. Initialize all four
required pinned submodules:

    git submodule update --init --depth 1 lib/chibios lib/chibios-contrib lib/printf lib/lufa

LUFA is required even for this ARM/ChibiOS target because QMK includes its USB
HID descriptor type definitions. This does not enable physical-key USB input.
The build script verifies the QMK commit and each required submodule revision
before invoking the real target compiler.

The underlying commands are:

    qmk compile --clean -kb keebdeck_6r11c -km default -j 4
    qmk compile --clean -kb keebdeck_6r11c -km diag -j 4
    python firmware/keyboard/tools/verify_qmk.py
    python -m unittest discover -s tests -p 'test_keyboard*.py' -v

Both target builds now use `--clean`. A new include-path override such as
`mcuconf.h` is not listed in pre-existing compiler dependency files, so an
incremental build can otherwise retain the old generic-board object code.
The first rebuild during this investigation reproduced that stale binary;
it was rejected for deployment. Regression coverage requires clean compilation.

Use `--qmk-root PATH` for another exact-revision checkout, or `--jobs N` to
change parallelism. `--build-only` is for compiler debugging and deliberately
cannot set `target_build_verified=true`.

## Startup clock correction (2026-09-11)

The keyboard-local `mcuconf.h` now enables HSI48, the internal 48 MHz
oscillator already selected for USB by QMK's generic F042 board. The inherited
system clock remains HSI/2 multiplied by 12 (48 MHz), and I2C1 remains on HSI
(8 MHz); the custom I2C slave retains exclusive ownership of I2C1. No rescue,
Bootmagic, EEPROM or ROM bootloader logic is changed. See `STARTUP.md` for the
compiled-source evidence, boot marker flow and the limits of this correction.

The clean target rebuild completed on 2026-09-11: both keymaps, memory audits,
pinned-source verification and all 16 keyboard tests passed. `QMK_PIN.json`
now has `target_build_verified=true`; `hardware_verified` remains false.
The prior complete target directory is preserved on the CM5 at
`/home/pi/mixos-keyboard-build-before-clock-20260911`. The sizes below describe
the new clock-corrected build. Actual ARM disassembly contains the HSI48ON
write and HSI48RDY wait (see `build/deploy/keyboard-20260911-clock-machine-code.md`).
Raw-only export compares the complete firmware source file set as well as
hashes, so an added `mcuconf.h` cannot silently reuse a stale verification.

Run host-only regression tests without replacing any target artifacts:

    wsl.exe -d Ubuntu-22.04 -u fwz233 -- /home/fwz233/mixos-host-venv/bin/python -B -m unittest discover -s /mnt/d/TheEndDEvice/MixOS/tests -p 'test_keyboard*.py' -v

The clock tests preprocess the real pinned F042 configuration and clock driver
with native `cc`; the unmodified upstream board is a failing negative control.
They verify HSI48 enable/readiness code, 48 MHz CPU/bus/USB clocks, the unchanged
8 MHz I2C1 source, and disabled HAL ownership of I2C1. `QMK_HOME` selects another
local checkout (the build script already supplies it). This is source-level
coverage, not an oscillator, USB or target-link test.

## Artifacts and automatic limits

`build/keyboard` contains both targets' `.bin`, `.elf`, `.map`, `.build.log`,
`.sections.txt`, `.symbols.txt`, `.size.txt` and `.memory.json` files. The ELF
files retain debugging information while runtime code keeps size optimization
and link-time optimization. Each `.bin` is 15,936 bytes: the 15,920-byte flash
image plus a 16-byte DFU suffix (not loaded into application flash). The two
keymaps produce byte-identical binaries. **Use `keebdeck_6r11c_default.raw.bin`
or `keebdeck_6r11c_diag.raw.bin` for the safe DFU worker**, not QMK's suffixed
`.bin`. Each raw deployment image is exactly 15,920 bytes. The build script
creates it directly from the ELF with `arm-none-eabi-objcopy -O binary`, validates
the QMK suffix's signature, length, identity/version and CRC, then requires
byte-for-byte equality with the suffix-stripped QMK payload. It calls only the
portable `tools/flash_keyboard_on_pi.py` function `validate_image`, never its
hardware entry points. The validator accepts both images and pads each to
16,384 bytes for programming, remaining below the EEPROM reservation.

For an already verified build, export without rebuilding or network access:

    wsl.exe -d Ubuntu-22.04 -- /home/fwz233/mixos-qmk-venv/bin/python /mnt/d/TheEndDEvice/MixOS/tools/build_keyboard.py --export-raw-only

This mode first verifies the stored source and ELF/QMK/map hashes, exports both
raw images, and runs the focused export/audit regression tests. Each target's
`.raw.validation.json` records raw and padded hashes, worker validation and
worker-source identity. The unchanged worker still rejects DFU-suffixed files.

`manifest.json` and `SHA256SUMS` record artifact
hashes; the manifest also records staged firmware source hashes and verification
script hashes. `toolchain.txt`, `submodules.txt`, `verify_qmk.log`, and `tests.log`
record the environment and validation. Earlier overwritten logs are retained in
`build/keyboard/history`.

The keyboard-local linker script limits application flash to **30 KiB**,
reserving the final **2 KiB** of the STM32F042G6U6's 32 KiB flash for QMK legacy
EEPROM emulation. It preserves the device's **6 KiB** SRAM limit. The audit
counts the loaded flash image, `.data`, `.bss`, any other occupied SRAM sections,
both linker-reserved stacks, and alignment padding. It excludes the unused
heap region from static usage; it never mistakes that region for occupied RAM.
Missing stack symbols or any capacity violation fails the build verification.

Only completion of both builds, both memory audits, the pinned-source verifier
and all keyboard tests permits `QMK_PIN.json` to set `target_build_verified`.
`hardware_verified` remains false. Host tests use address/undefined-behavior
sanitizers for the C transport, ESP-side input integration, and stubbed STM32
interrupt/callback behavior; they are not electrical tests.

## Safety scope and remaining hardware work

The physical-key interception, all-custom keymaps, blank-key mask, 128-entry
FIFO, ACK/session protocol, backlight handling, exact two-key rescue chord and
3-second threshold are preserved. USB enumeration and suspend cannot block
scanning. Pinned QMK retains a fixed 50 ms USB initialization delay; it does not
wait for a USB host to enumerate the device. No HID consumer/mouse/keyboard key
input is enabled by the diagnostic build.

Static memory accounting proves that reserved storage fits; it does not prove
runtime stack high-water marks. USB packet capture, actual I2C interrupt timing
and pull-ups, matrix ghost behavior, power-up/no-host scanning, bootloader entry
and board-level pin behavior remain hardware validation tasks. The legacy
`release/flash_kbd.sh` continues refusing batch flashing, and no firmware has
been flashed by this build procedure.
