#pragma once
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include "esp_lcd_panel_ops.h"
#include "mix_view.h"

/* Main-task-only API. Parent owns panel, sampling, transport and hardware.
 * Exactly one 1024x768 RGB565 PSRAM framebuffer; no hardware probes in init.
 * Call key only for LOCAL input, never terminal output. Query terminal_visible
 * BEFORE dispatching a key: modal confirmation must not reach the remote PTY.
 */
esp_err_t mix_ui_init(esp_lcd_panel_handle_t panel);
void mix_ui_tick(const mix_view_t *view, uint32_t now_ms);
void mix_ui_touch(int x, int y, bool down);
void mix_ui_key(const uint8_t *bytes, size_t len);
void mix_ui_home_toggle(void);
/* Bounded firmware-authored UTF-8; controls become spaces. */
void mix_ui_notice(const char *utf8);
bool mix_ui_take_action(mix_action_t *out);
bool mix_ui_terminal_visible(void);
