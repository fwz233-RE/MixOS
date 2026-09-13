// SPDX-License-Identifier: GPL-2.0-or-later
#pragma once
#include <stdbool.h>
#include <stdint.h>
#define MIX_ROWS 6
#define MIX_COLS 11
static inline bool mix_matrix_real(uint8_t row, uint8_t col) {
    return row < MIX_ROWS && col < MIX_COLS &&
           (row != 0 || ((0x038eU >> col) & 1U));
}
