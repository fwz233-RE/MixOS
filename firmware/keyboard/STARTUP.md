# STM32F042 startup investigation — 2026-09-11

## Confirmed defect and minimal correction

The previously built keyboard inherits QMK 0.28.0's
`platforms/chibios/boards/GENERIC_STM32_F042X6/configs/mcuconf.h`.
That configuration selects `STM32_USBSW_HSI48` but sets
`STM32_HSI48_ENABLED` to `FALSE`. The F042 capability registry explicitly
supports HSI48. In the pinned ChibiOS STM32F0 clock driver,
`stm32_clock_init()` enables HSI48 and waits for `HSI48RDY` only when that
configuration flag is true. Neither the inspected QMK USB initialization nor
ChibiOS USBv1 start path compensates by enabling HSI48.

The old configuration therefore leaves the selected USB oscillator off after
a cold reset. More precisely, it **omits enabling** HSI48; it does not explicitly
clear HSI48ON. `RCC->CR2` is not wholly reset by `stm32_clock_init()`, so a warm
handoff from ROM could inherit an already enabled oscillator and mask this
bug. This distinction matters when interpreting a DFU leave.

The stock QMK STM32 DFU implementation stores a magic word in retained SRAM
and then resets. This can leave the application interpreting a stale request
and jumping back into ROM after a ROM leave. The keyboard now uses the custom
bootloader implementation in `stm32_bootloader.c`: it ignores the legacy
startup marker and performs the ROM vector handoff directly only when a real
runtime bootloader request occurs. This preserves rescue/Bootmagic entry while
avoiding the retained-marker loop.

- HSI/2 multiplied by 12 for the 48 MHz system, AHB and APB clocks;
- the separate 8 MHz HSI clock for I2C1 and its existing TIMINGR setting;
- disabled ChibiOS I2C1 driver ownership, because the keyboard implements its
  own slave interrupt handler;
- disabled external oscillators (F0/F1 remain matrix pins);
- USB's HSI48 source and existing suspend/no-host behavior.

No clock-source migration or PLL retuning is included. The rescue and Bootmagic
entry paths remain enabled, but their ROM handoff is now direct rather than
marker-and-reset based. EEPROM storage and matrix ownership are unchanged.
Enabling the oscillator is necessary; USB clock accuracy/clock-recovery behavior
and actual cold-start enumeration still require separate validation. This patch
does not add a clock recovery system (CRS) configuration or claim USB timing
compliance.

## Effective build evidence

The QMK build's `cflags.txt` identifies `GENERIC_STM32_F042X6`,
`BOOTLOADER_CUSTOM`, and ROM base `0x1FFFC400`. Its keyboard include directory
precedes the generic board configs directory. The `.d` dependencies for the
compiled board and STM32F0 `hal_lld.c` name that generic `mcuconf.h`.
`builddefs/build_keyboard.mk` adds keyboard paths to the include search;
`platforms/chibios/platform.mk` appends the generic board configs. Therefore a
keyboard-root override is supported without modifying the pinned checkout.

Read-only disassembly of the prior image (preserved on the CM5 under
`/home/pi/mixos-keyboard-build-before-clock-20260911`) shows `__early_init`
at `0x08002740`.
Its clock initialization writes only HSI14ON (bit 0) in RCC CR2 and waits for
HSI14RDY; there is no HSI48ON (bit 16) enable/readiness sequence. The native
regression test preprocesses the actual pinned clock driver and proves this
sequence is present with the new header and absent with the upstream header.

The subsequent clean build passed both targets, memory audits, pinned-source
verification and 16 keyboard tests. Both new raw images are byte-identical,
15,920 bytes, SHA-256
`8ec21047a264da7c968070812af30dc2b675a1ee67e7e8cb5cf3762492388706`.
Actual new ARM code at `0x08002838`–`0x0800284A` sets HSI48ON and waits for
HSI48RDY; `build/deploy/keyboard-20260911-clock-machine-code.md` records the
inspection. An initial incremental build silently retained generic-board
objects; it was rejected before staging or programming. The launcher now
uses QMK `compile --clean` for both targets so new include-path overrides
cannot be omitted through stale dependency files.

## ROM bootloader marker and startup order

The pinned ARMv6-M reset handler calls QMK's `__early_init` before initializing
ordinary data/BSS and before entering `main`. QMK's early wrapper invokes:

1. `early_hardware_init_pre()` and its enabled
   `enter_bootloader_mode_if_requested()` check;
2. the renamed generic board early initialization, which initializes GPIO and
   runs `stm32_clock_init()`;
3. `early_hardware_init_post()`.

The STM32 DFU marker defaults to `__ram0_end__ - 4`. The existing linked image
sets `__ram0_end__` to `0x20001800`, making the marker address `0x200017FC`.
The expected value is `0xDEADBEEF`. The same disassembly confirms the compare
at the start of `__early_init`, clearing the marker before disabling interrupts,
clearing SysTick/NVIC state, loading the ROM stack pointer and branching through
the vector at `0x1FFFC400`. A marker-triggered ROM branch occurs **before** the
clock code changed by this patch. Software bootloader requests set that marker
and issue a system reset. Marker observation on the actual device is separate
from this read-only source/ELF inspection.

Later, QMK sets up HAL/the scheduler and initializes USB; the configured build
does not wait for enumeration. Bootmagic runs during keyboard initialization,
scans the matrix, and if row 2/column 0 is held, calls `eeconfig_disable()` before
requesting the bootloader. The two-key, three-second local rescue is independent
and is preserved. The reported unchanged EEPROM bytes before programming and
after the first leave do not demonstrate that Bootmagic ran. They are not grounds
to remove Bootmagic or rescue.

## Additional verified startup interactions

The ARMv6-M reset entry masks interrupts, initializes MSP and PSP, and writes
CONTROL before early board initialization. Its default exception and exit
handlers loop rather than deliberately entering ROM. Ordinary inherited stack
or CONTROL state alone is therefore not an established cause if the reset
entry was reached.

The pinned STM32F0 `hal_lld_init()` resets all APB2 peripherals except DBGMCU,
including SYSCFG. The keyboard's later `board_init()` only sets the USB pin
remapping bit. Address-zero vector mapping must therefore be established
through this peripheral reset and before exceptions are enabled, not merely
at ROM handoff. Neither the actual mapping nor the peripheral-reset outcome
has been measured; an early remap-only patch would be speculative.

The generic board header configures PB8/BOOT0 as a push-pull output with ODR
high during early GPIO initialization. Its configured pulldown does not make
an actively driven output low. Later, ROW2COL matrix initialization makes PB8
(row 3) an input with pullup. These are concrete configuration facts, but the
actual boot-sampled voltage, option-byte settings and reset-versus-branch ROM
leave behavior are unknown. No PB8 change is justified as a confirmed fix by
these facts alone.

The next evidence needed is the F042-specific ROM leave/empty-check behavior,
actual boot/watchdog option settings, and address-zero mapping/reset status.
The current 32 KiB application-only worker does not inspect ROM, option bytes
or peripheral registers. Any new capture requires a separately reviewed,
read-only path; no option-byte write, force/unprotect, repeated leave, or
shared-hub reset is part of this investigation.

## Interpretation and remaining evidence

The HSI48 omission is a confirmed cold-start USB configuration defect. It does
not itself implement a reset or ROM branch, and it does not prove why repeated
DFU leave operations reenumerated `0483:df11`. In particular, the marker decision
precedes this oscillator setup, and a ROM handoff may leave HSI48 running.
The unchanged EEPROM evidence likewise does not identify the ROM reentry cause.
Keep these questions separate until hardware evidence establishes which startup
path executes.

The correction is now clean-built and deployed once. The full 32 KiB readback
matches the expected clock-corrected image and unchanged tail. However, the
single leave at 2026-09-11 13:11 (UTC+8) again reenumerated ROM `0483:df11`,
now device 21. The clock fix therefore did not resolve ROM return. No repeat
write or leave was performed. `target_build_verified` is true;
`hardware_verified` remains false. See
`build/deploy/keyboard-20260911-clock-deployment.json` for exact hashes and audit
paths. The EEPROM preservation check for this transaction preceded leave;
EEPROM after this latest leave has not been freshly uploaded. ESP firmware and
sibling repositories were not changed by this keyboard investigation.
