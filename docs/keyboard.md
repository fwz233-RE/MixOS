# MixOS keyboard implementation and integration

## Ownership and interfaces

All edits for this work are confined to the keyboard-owned paths listed in `docs/IMPLEMENTATION.md`; original `D:/TheEndDEvice/TypixDeck-*` repositories and ESP main `CMakeLists.txt` are unchanged.

Public ESP files:

- `firmware/esp32s3/main/mix_input.h` / `.c`: portable C11 keyboard interpretation with no ESP/QMK includes.
- `firmware/esp32s3/main/mix_keyboard.h` / `.c`: ESP-IDF new I2C master driver integration, single-main-task polling.
- `protocol/KEYBOARD_V1.md`: authoritative v1 framing, CRC, sequence, ACK, recovery and backlight semantics.

Input API follows `IMPLEMENTATION.md` exactly: `mix_key_cb(int action,const uint8_t *bytes,size_t len,void *ctx)`, `mix_input_init`, `mix_input_event`, `mix_input_tick`, `mix_input_reset`. Action IDs are TEXT=0, HOME=1, BRIGHT_UP=2, BRIGHT_DOWN=3, VOLUME_UP=4, VOLUME_DOWN=5, BACKLIGHT=6. Text is length-delimited (can contain NUL for Ctrl+Space); consume/copy callback bytes immediately. Special callbacks have NULL bytes and zero length.

Driver API: `mix_keyboard_init(bus,cb,ctx)`, `mix_keyboard_tick(now_ms)`, `mix_keyboard_reset_input()`, `mix_keyboard_online()`, `mix_keyboard_overflows()`, `mix_keyboard_backlight_step()`. Initialization only registers the device and initializes local state; the first tick probes it. It accepts the existing shared I2C bus and never installs a new bus or changes sensor configuration. Call all APIs from the app main task, never ISR/USB callback. Call tick every 20 ms. Main integration owns routing text to the UI/terminal and actions to display brightness, Linux volume, HOME and queued `mix_keyboard_backlight_step()`. Parent integration must add both `.c` files to ESP compilation; this keyboard task deliberately does not change main CMake.

Every link loss, overflow, incompatible version, CRC/sequence/matrix error or `mix_keyboard_reset_input()` enters release-wait. The driver drains without emitting and resumes only from an ACKed atomic all-up boundary. Portable `mix_input_reset()` preserves physical held knowledge, clears repeats and suppresses until release; driver uses `mix_input_init(cb,ctx)` only after observing a complete all-up boundary to discard unknown startup keys safely. Standalone portable callers should supply releases after reset rather than immediately replaying pressed state. A callback may call driver reset; remaining events in that batch are suppressed. Driver init should occur only once during application startup (re-init can replace its IDF device handle).

## Layout

Physical rows/columns are zero based. All five space contacts R5C3..7 are one logical key, tracked by individual bits, not a counter; duplicates and duplicate releases cannot underflow. There is no sticky modifier or layer. Both physical shifts are independently tracked. Fn takes precedence over Sym when both are held.

| Position | Base | Fn | Sym (when Fn absent) |
|---|---|---|---|
| R0C1 square | HOME action | same | same |
| R0C2 triangle / C3 cross | volume up/down actions | same | same |
| R0C7 circle / C8 clover | display brightness up/down actions | same | same |
| R0C9 diamond | keyboard backlight action | STM32-local rescue chord, no ESP action | same rescue alternative |
| R1C0..9 | 1234567890 | F1..F10 | base |
| R1C10 | DEL byte (backspace) | Delete sequence | Delete sequence |
| R2C0 | Tab | Escape | Tab |
| R2C1..10 | qwertyuiop | O/P become F11/F12; rest base | Q/W/E/R/T/Y/U/I become `/ ? ` + backtick + ` ~ - _ = +`; O/P base |
| R3C0 | hold Fn | hold Fn | hold Fn |
| R3C1..9 | asdfghjkl | base | S/D/F/G/H/J/K/L become comma, dot, backslash, pipe, [, ], {, } |
| R3C10 | CR (Enter) | same | same |
| R4C0 / C10 | left/right Shift | same | same |
| R4C1..8 | zxcvbnm; | base | V/B/N/M/semicolon become <, >, single quote, double quote, colon |
| R4C9 | Up | PageUp | Up |
| R5C0 / C1 / C2 | Sym / Ctrl / Alt | same | same |
| R5C3..7 | Space | one BACKLIGHT action per logical press | Space |
| R5C8 / C9 / C10 | Left / Down / Right | Home / PageDown / End | base |

Shift uppercases letters and US punctuation; Ctrl emits control letters, `@`..`_`, Ctrl+Space/2 as NUL and Ctrl+? as DEL. Alt prefixes ordinary text with Escape. Navigation and function keys use standard xterm escape sequences with Shift/Alt/Ctrl modifier parameters. First repeat is 450 ms after press, then 50 ms, one emission maximum per tick, with wrap-safe millisecond arithmetic. Latest repeatable pressed key wins; releasing it does not resume an older held key. Special actions never repeat. No repeat occurs while the driver is recovering or draining more than 32 queued events. Fn/Sym/Shift changes affect subsequent repeats; Fn+Space cannot repeatedly invoke backlight.

## STM32 safety and upstream verification

QMK pin: tag `0.28.0`, immutable commit `a63fd7f01cdabd9ce85bb09ae2b573fd3b8e60aa`; `git ls-remote` confirmed tag mapping. `firmware/keyboard/tools/verify_qmk.py` verifies SHA-256 and source fragments in seven public upstream files. Successful execution on 2026-09-09 established:

1. `quantum/matrix.c`: `matrix_scan()` calls `debounce()` before `matrix_scan_kb()` and returns debounced state.
2. `quantum/keyboard.c`: `matrix_task()` calls scan, ignores rows rejected by `has_ghost_in_row()`, then calls `action_exec()` for each physical transition.
3. `quantum/action.c`: event `pre_process_record_quantum()` executes before tapping, or before `process_record()` when `NO_ACTION_TAPPING` is set.
4. `quantum/quantum.c`: prehook dispatch calls modules then `pre_process_record_kb()` before later processors; this board returns false without user dispatch. Normal `process_record_kb()` also returns false defensively.
5. Layer-0 keymap nonzero entries define real switches for QMK ghost filtering. Blanks stay `KC_NO`; real switches, including Fn/Sym/shifts and **every** space dome, use `QK_USER_0`. No legacy direct `register_code(KC_SPC)` user callback exists. `diag` includes the same silent keymap. COMMAND/CONSOLE/EXTRAKEY/MOUSEKEY/NKRO are off; tapping/oneshot/macro actions are disabled. No legacy consumer usage or QK_BOOT key remains.
6. Bootmagic executes separately during initialization. Runtime rescue uses accepted physical state plus the debounced matrix, requires exactly Fn+diamond or Sym+diamond for three seconds, and rejects stale ghost-suppressed release/extra-key states. `NO_USB_STARTUP_CHECK` prevents the ChibiOS USB suspend loop from pausing keyboard service; enumeration waiting is forbidden. Backlight stays enabled. This provides no USB input fallback.

The source checks do not constitute a full call-graph proof for arbitrary QMK features/keymaps. Only the pinned version and supplied two keymaps are supported; enabling additional modules, direct USB code or processors requires a new isolation audit. QMK still enumerates a HID keyboard interface; this policy suppresses physical input reports, not USB descriptors or bootloader USB.

## Tests and tools

Host test entry: `tests/test_keyboard.py` (`unittest`, no package dependencies). It builds real sources against C11 host stubs with `-Wall -Wextra -Werror -pedantic -fsanitize=address,undefined` and runs:

- `tests/test_input.c`: base/Fn/Sym, both shifts, Ctrl/Alt, xterm sequences, all five spaces, repeats, timer wrap, reset and all 256x256 coordinate bounds.
- `firmware/keyboard/tests/test_transport.c`: 128-event FIFO, overflow state/sequence, non-destructive snapshots, prefix ACK retry, wrap, command lengths/backlight and known CRC vector.
- `firmware/keyboard/tests/test_driver.c`: real ESP driver with stubbed IDF API and real STM32 transport; max three transactions, 20 ms rate limiting, 1 s offline throttle, ACK failure, resync, overflow, session/MCU reset, callback reset and event-after-all-up-snapshot race.
- `firmware/keyboard/tests/test_stm32.c`: real board/IRQ C with fake QMK/register types; physical prehook, blank/tick rejection, five spaces, repeated/short read, ACK, atomic capture, malformed writes, deferred backlight, rescue timeout/extra keys/stale release/wrap.
- Static checks of both keymaps, feature flags, physical masks and hook placement.

Tools discovered and used:

- Windows Python: `C:/Users/123/AppData/Local/Programs/Python/Python312/python.exe` (upstream verifier).
- Native test compiler/runtime: `wsl.exe -d Ubuntu-22.04`, `/usr/bin/cc` (GCC 11.4.0), `/usr/bin/python3` (3.10.12).
- Also available: `C:/Program Files/LLVM/bin/clang.exe` (not used for target builds).

Run tests from PowerShell:

`wsl.exe -d Ubuntu-22.04 -- bash -lc 'cd /mnt/d/TheEndDEvice/MixOS && python3 -m unittest discover -s tests -p "test_keyboard*.py" -v'`

Run upstream verifier:

`& 'C:/Users/123/AppData/Local/Programs/Python/Python312/python.exe' 'D:/TheEndDEvice/MixOS/firmware/keyboard/tools/verify_qmk.py'`

The verifier downloads read-only HTTPS source with up to three attempts and rejects any changed hash/order. It does not modify upstream or cache source in the project. Native tests use temporary executable output and delete it afterward. A missing host compiler is reported as skipped tests, never as compiled coverage.

## Outstanding target and hardware acceptance

**Host tests passed; target build and hardware validation are outstanding.** No `qmk`, `arm-none-eabi-gcc`, `idf.py` or Xtensa toolchain was available on the inspected host/WSL PATH. No QMK core checkout exists inside this keyboard clone. Both changed target firmwares must be built before deployment. The real ESP main CMake integration belongs to the parent workstream.

- Build pinned default and diag with correct submodules; inspect linker map for 32 KiB flash / 6 KiB SRAM including stacks. Transport struct is 416 host bytes; FIFO 384 bytes; compile-time persistent transport budget <=576 bytes is **not** a full F042 RAM proof.
- Compile ESP against the project's actual ESP-IDF version and verify I2C timeout tick granularity and shared-bus scheduling (`sdkconfig.defaults` currently specifies `CONFIG_FREERTOS_HZ=1000`). Three configured 5 ms calls bound requested waits, not hard real-time wall-clock execution.
- Capture USB ordinary and consumer reports while pressing every key/space dome, with ESP connected/disconnected, Linux booting, USB suspended and held modifiers. No physical key input may reach USB.
- Exercise Bootmagic (physical Tab at R2C0), both local three-second rescue chords without ESP, short/canceled chords, real matrix ghosting, blank intersections, and all five actual space contacts.
- Logic-analyzer test I2C repeated-start/address/NACK/STOP/overrun/error flags, clock stretching and ISR priority. MCU register stubs do not model electrical register side effects.
- Burst at least 140 transitions, force mid-read disconnect/ACK failure/MCU reset, hold Ctrl/Shift/Fn during outage, and verify no replay/repeat until all up; test CRC corruption and backlight 0..8 without EEPROM writes.
- Legacy release script is disabled because original prebuilt binaries violate the new USB policy. No new release binary or flashing/hardware claim is provided.
