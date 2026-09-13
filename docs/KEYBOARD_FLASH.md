# Safe STM32 keyboard deployment on the Pi

`tools/flash_keyboard_on_pi.py` is a one-shot, fail-closed worker for the **STM32F042G6U6 keyboard only**. Python standard library only; reviewed `dfu-util` **0.11** is required. No arguments inspect Linux USB inventory. Supplying a complete image/identity request still only inspects unless `--execute` is present. The worker does not use SSH, stop `mixosd`, reset the USB hub, switch the host, or communicate with the ESP32. Portable fake-device tests validate safeguards; actual flash/readback and the unresolved application-startup result are recorded separately in `DEPLOYMENT.md`.

## Fixed target and evidence

| Property | Required value |
|---|---|
| Keyboard physical Linux USB path | `5-1.1` (bus 5, hub port 1, downstream port 1) |
| ESP32 physical USB path, never targeted | `5-1.2` |
| Official keyboard runtime identity | `c182:6b11` |
| STM32 system-ROM DFU identity | `0483:df11` |
| MCU, explicitly confirmed from board/build provenance | `STM32F042G6U6` |
| Total internal flash | `0x08000000` through `0x08007fff`, 32 KiB |
| MixOS application / EEPROM reservation | First 30 KiB / final 2 KiB |
| Erase granularity | 32 pages of 1 KiB |
| SRAM | `0x20000000` through `0x200017ff`, 6 KiB |
| DFU interface | configuration 1, interface 0, alternate 0, DfuSe 1.1a |

The worker independently compares Linux sysfs identity and binary USB descriptors with a pinned `dfu-util --list` result. It requires a nonempty exact 12-hex-character **ROM DFU serial**, one ROM DFU device system-wide, and the 32 KiB internal-flash layout (`@Internal Flash /0x08000000/032*001Kg`, or equivalent contiguous groups of 1 KiB pages). Unknown formats, other sizes, missing upload support, other alternate settings, serial/path mismatches, and re-enumeration cause a stop. The USB device number and descriptor hash must remain unchanged throughout the transaction.

Every invocation, including each listing, is restricted with `-d ,0483:df11 -a 0 -p 5-1.1 -S EXACT_SERIAL -c 1 -i 0 -n DEVICE_NUMBER`. The leading comma in `-d ,0483:df11` is intentional: **a single `-d 0483:df11` also permits arbitrary DFU-mode IDs in dfu-util 0.11**. Runtime devices must never be selected or detached by this worker.

USB identity and a memory layout are corroboration, not cryptographic chip authentication. ROM DFU does not uniquely attest the package marking or guarantee a genuine STM32F042. `--mcu` records the separately established board identity; it does not probe debug registers or option bytes. A matching serial cannot prevent a malicious device from impersonating the keyboard. Physical USB topology changes require a separately reviewed code/configuration update, not a permissive command-line override.

## Entering system-ROM DFU

The read-only original `TypixDeck-keyboard-firmware/keymaps/default/keymap.c` maps **Fn + diamond** and **Sym + diamond** to QMK `QK_BOOT`; its original `release/flash_kbd.sh` documents both combinations. The original keymap has no custom three-second hold timer. The old batch script is unsuitable: it matches any ROM device, writes immediately, leaves before readback, and loops for another device.

The MixOS keyboard source deliberately changes rescue to hold exactly **Fn (row 3, column 0) + diamond (0,9)** or **Sym (5,0) + diamond** for **three seconds**, with no additional real keys. Its bootmagic alternative is **Tab (2,0) held during power-on**. These are STM32-local paths and do not require ESP or Linux cooperation. The worker does not synthesize the chord: host-injected keyboard input cannot physically press the STM32 matrix. The deployed original firmware/build may differ, so successful ROM enumeration is checked automatically rather than assumed.

A physical entry action is necessary if the keyboard is still running an application. **If fresh inventory already shows the exact keyboard in ROM DFU, no entry chord or physical button is needed for that transaction.** There is no supported automatic host-to-ROM command in this scope. A single preparation/entry followed by one automated deployment replaces repeated manual flash/readback checks. A target that cannot enter ROM safely remains blocked; this tool does not change BOOT0, power-cycle the shared hub, or try unprotect.

## Permissions and invocation

The ordinary `pi` account can read the initial sysfs inventory without privilege:

```sh
python3 tools/flash_keyboard_on_pi.py
```

The complete inspection or execution runs as an **isolated privileged Python worker**, using a reviewed, root-owned copy of this script and the distribution's `/usr/bin/dfu-util`. An administrator must stage the script under a trusted path such as `/opt/mixos-keyboard/flash_keyboard_on_pi.py`, and create `/var/lib/mixos/keyboard-flash` root-owned and not group/world writable. All ancestors of the audit parent must also be root-owned and not group/world writable. Never grant passwordless sudo for a user-writable Python script. `python3 -I` ignores user site packages and Python environment configuration; dfu-util subprocesses receive a minimal environment and no shell. The worker does not silently invoke sudo or retry a failed permission check.

Existing administrative sudo permission lets `pi` launch the worker as below. A restricted sudo policy should permit a separately reviewed root-owned launcher with fixed interpreter/script paths; allowing arbitrary `sudo python3` grants general root execution. Permission to sudo **only** dfu-util is not permission to launch the entire Python worker: an administrator must provide the worker launcher or approve this scoped installation. The host daemon needs no elevated permission or restart. Privileged audit artifacts stay root-private (workdir mode 0700), and can be copied out by an administrator afterward.

Use the approved build's independently recorded SHA-256 and actual ROM serial from inventory. Values below are placeholders, not approvals or discovered hardware facts. The application must be a **raw `.bin` linked at `0x08000000`**, not ELF, Intel HEX, a DfuSe container, or an image with a DFU suffix.

```sh
sudo -n /usr/bin/python3 -I /opt/mixos-keyboard/flash_keyboard_on_pi.py \
  --image /opt/mixos-keyboard/keyboard.bin \
  --sha256 APPROVED_64_HEX_IMAGE_SHA256 \
  --serial ACTUAL_12_HEX_ROM_SERIAL \
  --usb-path 5-1.1 --mcu STM32F042G6U6 \
  --workdir /var/lib/mixos/keyboard-flash/inspection-UNIQUE_ID
```

After supplying the already approved identity and image, one execution performs every safety check automatically. A preliminary complete inspection is optional; execution repeats its checks itself. Use a different, **nonexistent** directory for every request:

```sh
sudo -n /usr/bin/python3 -I /opt/mixos-keyboard/flash_keyboard_on_pi.py \
  --image /opt/mixos-keyboard/keyboard.bin \
  --sha256 APPROVED_64_HEX_IMAGE_SHA256 \
  --serial ACTUAL_12_HEX_ROM_SERIAL \
  --usb-path 5-1.1 --mcu STM32F042G6U6 \
  --workdir /var/lib/mixos/keyboard-flash/deployment-UNIQUE_ID \
  --execute --leave
```

Omit `--leave` to keep the verified target in ROM DFU. With `--leave`, only after durable readback verification, dfu-util 0.11 **command mode** sends `-s 0x08000000:leave` with no download file. This sets the application address and submits the zero-length DFU leave request; it does not re-download a page. The worker never appends `:leave` or `-R` to the programming command. A successful leave request is not proof that the application booted or that input works.

## Automated transaction and audit artifacts

1. Verify approved SHA-256 and raw image length (192 bytes minimum, 32 KiB maximum). Check the 48-entry Cortex-M0 vector table: 8-byte-aligned initial stack strictly above SRAM base and at or below `0x20001800`; reset must be Thumb code inside the supplied image after the vector table; other vectors must be zero or valid in-image Thumb addresses. These checks are deliberately restrictive and cannot prove full firmware correctness or RAM/stack sufficiency; use the firmware build's ELF/map memory-budget evidence too.
2. Obtain a nonblocking per-keyboard lock in `/run/lock`. Create the workdir exclusively, including a durable append-only-by-convention `audit.jsonl`. Even an empty existing directory is rejected. There is no resume or retry mode. Concurrent workers with different workdirs are blocked by the same lock.
3. Corroborate sysfs, raw DFU descriptors, exact serial/path/device number, dfu-util version and the internal-flash descriptor.
4. Stage `image.bin` and `program.bin`. The latter pads only the last covered page with `0xff`; it never pads the whole 32 KiB for programming unless the image actually occupies that many pages.
5. Upload **all 32768 bytes** with `-s 0x08000000:32768 -U backup.bin`. Require exact length, fsync the backup file and directory, write/fsync `backup.bin.sha256`, and durably record its hash before any erase/download. Protected, truncated, disconnected or unreadable devices stop here. No read-unprotect fallback exists.
6. Produce `expected-full.bin`: the page-padded image followed by the original backup's untouched pages. Recheck descriptors/identity and staged artifacts. Record durable write intent. Issue exactly one `-s 0x08000000 -D program.bin`. dfu-util erases only the covered 1 KiB pages. There is no option-byte access, mass erase, unprotect, force, wait-for-reconnect or reset option.
7. Recheck identity/layout. Upload the full 32 KiB to `readback.bin`, fsync it and its SHA record. Verify three SHA-256 values: original image prefix, full page-padded programmed range, and **all 32 KiB including unchanged pages**. A mismatch prevents leave. This intentionally preserves unrelated tail pages, including any settings/EEPROM storage there; it does not erase old tail code. Any settings inside the image's covered pages are overwritten, so build provenance must establish that those pages are application-owned.
8. Record durable verification, optionally leave once, and return machine-readable JSON with hashes, artifact directory and status. Each subprocess command, output and exit status is audited. Every binary artifact has its own `.sha256` file.

Subprocesses have a 120-second timeout. A timeout, interruption, disconnect, nonzero command status, identity change, fsync failure, or verification mismatch ends the transaction without automatic rollback, reset, another write or silent retry. The last command may have partially programmed flash even when it reports failure. Preserve the audit and backup; investigate before approving any new transaction. A new workdir is not itself authorization to replay an uncertain write. dfu-util performs its own protocol status polling/clearing; the worker never launches a second attempt at a failed operation.

The guarantee is OS-level durability through file/directory fsync on the Pi. It cannot guarantee power-loss protection of a failing SD card/controller, survive arbitrary privileged artifact tampering, or serialize against other programs that ignore its lock. Audits are private and append-only by worker convention, not cryptographically tamper-proof. Keep stable power and exclude other programmers during deployment. The tool backs up application flash only, never option bytes/system ROM.

## Portable automated tests and hardware limits

```sh
python3 -m unittest discover -s tests -p 'test_flash_keyboard.py' -v
```

Tests use in-memory fake flash, synthetic USB descriptors/sysfs and a fake subprocess backend. **All 33 tests passed on Windows/Python 3.12 and Ubuntu 22.04 under WSL on 2026-09-11.** They cover identity/descriptor ambiguity, ESP exclusion, image/hash/vector rejection, 1 KiB alignment and 32 KiB bounds, default inspection-only behavior, durable backup ordering, exact selectors on every command, exclusive workdirs, lock contention/unsafe lock rejection, signal handling, programming/readback/leave sequencing, unchanged tail verification, staged-image tampering, short uploads, failures/timeouts/interruption, re-enumeration, and backup/readback/write-intent durability failures. They do not require dfu-util, sudo, SSH, USB, STM32 or ESP hardware.

**No actual hardware deployment is claimed by these tests.** Hardware-only limits remain ROM accessibility/upload permission, exact on-device descriptors and serial, supply stability, USB path persistence, DFU leave behavior, application boot, electrical keyboard/I2C operation, USB input suppression and actual runtime RAM usage. The worker automates all checks it can establish from host-visible evidence and refuses incomplete evidence instead of asking for repeated manual tests or inventing a successful deployment.

Reviewed upstream command semantics: [dfu-util manual](https://dfu-util.sourceforge.net/dfu-util.1.html), [dfu-util 0.11 main.c](https://sourceforge.net/p/dfu-util/dfu-util/ci/v0.11/tree/src/main.c), and [dfu-util 0.11 dfuse.c](https://sourceforge.net/p/dfu-util/dfu-util/ci/v0.11/tree/src/dfuse.c). Original board/keymap evidence was read from the original repository without modifying it. MixOS rescue/source background is in `firmware/keyboard/README.md` and `docs/keyboard.md`; historical hardware or original-firmware successes do not validate a new MixOS image.
