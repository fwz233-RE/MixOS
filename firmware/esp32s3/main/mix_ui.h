#pragma once
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include "esp_lcd_panel_ops.h"
#include "mix_view.h"

/* Main-task-only API. Parent owns panel, sampling, transport and hardware.
 * Exactly one 1024x768 RGB565 PSRAM framebuffer; no hardware probes in init.
 * Call key only for LOCAL input, never terminal output. Query terminal_visible
 * BEFORE dispatching a key: modal confirmation and text entry must not reach
 * the remote PTY.
 */
esp_err_t mix_ui_init(esp_lcd_panel_handle_t panel);
void mix_ui_tick(const mix_view_t *view, uint32_t now_ms);
/* First full-frame submission must succeed. Any later failed draw makes this
 * false until a full repaint succeeds. This measures the driver API, not LCD
 * glass/backlight correctness; main separately requires fresh RGB callbacks. */
bool mix_ui_draw_healthy(void);
esp_err_t mix_ui_last_draw_error(void);
void mix_ui_touch(int x, int y, bool down);
void mix_ui_key(const uint8_t *bytes, size_t len);
void mix_ui_home_toggle(void);
/* Bounded firmware-authored UTF-8; controls become spaces. */
void mix_ui_notice(const char *utf8);
/* The parent owns the codec volume and tells the UI what it became. */
void mix_ui_volume(int percent);
bool mix_ui_take_action(mix_action_t *out);
bool mix_ui_terminal_visible(void);
/* Read-only view of the composed framebuffer, for the host's screenshot
 * request. NULL before mix_ui_init. Width and height are the panel's, and the
 * buffer is width*height RGB565 little-endian pixels.
 *
 * The caller reads this while the UI task may be drawing, so a capture can be
 * torn. That is accepted on purpose: a diagnostic must not be able to stall
 * the draw path, and a torn screenshot still answers the question it is asked.
 */
const uint16_t *mix_ui_framebuffer(size_t *bytes, uint16_t *width, uint16_t *height);
/* Valid only while draining a MIX_ACTION_NET_CONNECT or NET_FORGET action.
 * The passphrase buffer is cleared as soon as the action is taken. */
const char *mix_ui_net_ssid(void);
const char *mix_ui_net_passphrase(void);
