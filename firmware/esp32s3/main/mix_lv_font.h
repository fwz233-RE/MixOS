/* SPDX-License-Identifier: MIT
 * LVGL fonts at arbitrary pixel sizes, rendered from the font partition.
 *
 * Runs inside the LVGL task, like everything that draws. Call
 * mix_lv_font_init once after ttf_font_init and after lvgl_port_init.
 */
#pragma once

#include <stdbool.h>
#include <stdlib.h>

#include "esp_err.h"
#include "lvgl.h"

/* Reports whether the font partition carries a usable font. Returns
 * ESP_ERR_NOT_FOUND when it does not, which is not fatal: mix_lv_font() then
 * answers with LVGL's built-in font and the interface stays readable in
 * ASCII. */
esp_err_t mix_lv_font_init(void);

bool mix_lv_font_ready(void);

/* A font at this pixel size. Sizes are cached, so asking repeatedly is cheap;
 * the caller must not free the result. Never returns NULL. */
const lv_font_t *mix_lv_font(int px);
