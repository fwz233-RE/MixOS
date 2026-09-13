// SPDX-License-Identifier: GPL-2.0-or-later
#pragma once

// Inherit the pinned GENERIC_STM32_F042X6 configuration. QMK searches the
// keyboard directory before the board configs directory.
#include_next <mcuconf.h>

// USB selects HSI48, but the generic board leaves its oscillator disabled.
// Enable it during stm32_clock_init(), including on a true cold start.
// Keep SYSCLK = HSI/2 * 12 (48 MHz) and I2C1 = HSI (8 MHz) unchanged.
#undef STM32_HSI48_ENABLED
#define STM32_HSI48_ENABLED TRUE
