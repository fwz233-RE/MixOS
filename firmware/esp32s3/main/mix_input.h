#pragma once
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
enum mix_key_action {
    MIX_KEY_TEXT = 0, MIX_KEY_HOME = 1, MIX_KEY_BRIGHT_UP = 2,
    MIX_KEY_BRIGHT_DOWN = 3, MIX_KEY_VOLUME_UP = 4,
    MIX_KEY_VOLUME_DOWN = 5, MIX_KEY_BACKLIGHT = 6
};
typedef void (*mix_key_cb)(int action, const uint8_t *bytes, size_t len, void *ctx);
void mix_input_init(mix_key_cb cb, void *ctx);
void mix_input_event(uint8_t row, uint8_t col, bool down, uint32_t now_ms);
void mix_input_tick(uint32_t now_ms);
// Preserve physical down knowledge, clear repeat, suppress until all released.
void mix_input_reset(void);
