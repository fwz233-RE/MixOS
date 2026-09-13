// SPDX-License-Identifier: GPL-2.0-or-later
#pragma once
#include <stdint.h>
#include <stdbool.h>
void kbd_i2c_slave_init(void);
void kbd_i2c_push_event(uint8_t row, uint8_t col, bool pressed);
void kbd_i2c_task(void);
bool kbd_i2c_key_down(uint8_t row, uint8_t col);
