#pragma once
#include <stdbool.h>
#include <stdint.h>
#include "esp_err.h"
#include "mix_input.h"
esp_err_t mix_keyboard_init(mix_key_cb cb, void *ctx);
void mix_keyboard_tick(uint32_t now_ms);
void mix_keyboard_reset_input(void);
bool mix_keyboard_online(void);
uint32_t mix_keyboard_overflows(void);
void mix_keyboard_backlight_step(void);
