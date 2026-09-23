#pragma once
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
enum mix_key_action {
    MIX_KEY_TEXT = 0, MIX_KEY_LOCK = 1, MIX_KEY_HOME = MIX_KEY_LOCK,
    MIX_KEY_BRIGHT_UP = 2,
    MIX_KEY_BRIGHT_DOWN = 3, MIX_KEY_VOLUME_UP = 4,
    MIX_KEY_VOLUME_DOWN = 5, MIX_KEY_BACKLIGHT = 6,
    /* Local, one-shot action. The owner decides whether a notes session may
     * receive MIX_IME_TOGGLE_SEQUENCE; never send it to WiFi fields or shells. */
    MIX_KEY_IME_TOGGLE = 7
};
/* CSI u: Unicode Space (32), Shift modifier (2). Not ordinary text or Ctrl+A. */
#define MIX_IME_TOGGLE_SEQUENCE "\033[32;2u"
typedef void (*mix_key_cb)(int action, const uint8_t *bytes, size_t len, void *ctx);
void mix_input_init(mix_key_cb cb, void *ctx);
void mix_input_event(uint8_t row, uint8_t col, bool down, uint32_t now_ms);
void mix_input_tick(uint32_t now_ms);
// Preserve physical down knowledge, clear repeat, suppress until all released.
void mix_input_reset(void);
