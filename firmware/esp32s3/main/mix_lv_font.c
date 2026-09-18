/* SPDX-License-Identifier: MIT
 * LVGL fonts rendered from the device's own font partition.
 *
 * Why not LVGL's FreeType binding. Two independent reasons, both checked
 * against the pinned versions (LVGL 9.2.2, esp_lvgl_port 2.4.4):
 *
 *  1. LVGL's ESP component registers itself with `REQUIRES esp_timer` and
 *     nothing else, so turning on LV_USE_FREETYPE compiles lv_freetype.c
 *     without FreeType's own headers on the include path. The component is
 *     regenerated on every dependency solve, so it cannot simply be edited.
 *  2. The only way LVGL's binding can read a font that is not a file is
 *     LV_FREETYPE_USE_LVGL_PORT, which compiles lv_ftsystem.c. That file
 *     defines FT_Stream_Open, ft_alloc, ft_free and FT_New_Memory — all of
 *     which the espressif/freetype component already provides.
 *
 * The device already has a FreeType face and a PSRAM glyph cache in
 * ttf_font.c, sized for this panel and this font subset. This file makes that
 * cache look like an lv_font_t, so there is one FreeType instance, one glyph
 * cache, and one place where a missing glyph is handled.
 *
 * Threading. LVGL runs in the esp_lvgl_port task, and ttf_font.c is
 * single-task. That holds because every caller below runs inside an LVGL draw
 * or layout pass, and ttf_font_init() completes before that task is created.
 */
#include "mix_lv_font.h"

#include <string.h>

#include "esp_log.h"
#include "ttf_font.h"

static const char *TAG = "mix_lv_font";

/* One entry per instantiated pixel size. The Material type scale asks for a
 * handful of sizes and the terminal for one or two more. */
#define MIX_FONT_SLOTS 12

typedef struct {
    lv_font_t font;   /* handed to LVGL; dsc points back at this struct */
    uint16_t  px;
    bool      used;
} slot_t;

static slot_t s_slots[MIX_FONT_SLOTS];

/* ---------- lv_font_t callbacks ---------- */

static bool glyph_dsc(const lv_font_t *font, lv_font_glyph_dsc_t *dsc,
                      uint32_t letter, uint32_t letter_next)
{
    (void)letter_next;   /* the subset carries no kerning pairs */
    const slot_t *slot = font->dsc;
    if (!slot) return false;

    /* A newline reaching the font layer is a layout concern, not a glyph; the
     * fmt_txt font answers the same way. */
    if (letter == '\0' || letter == '\n' || letter == '\r') return false;

    ttf_glyph_t glyph;
    if (!ttf_font_glyph(letter, slot->px, &glyph)) return false;

    dsc->adv_w = (uint16_t)(glyph.advance > 0 ? glyph.advance : 0);
    dsc->box_w = (uint16_t)(glyph.w > 0 ? glyph.w : 0);
    dsc->box_h = (uint16_t)(glyph.h > 0 ? glyph.h : 0);
    dsc->ofs_x = glyph.left;
    /* LVGL measures the box offset from the baseline upward; FreeType's `top`
     * is the distance from the baseline to the top of the bitmap. */
    dsc->ofs_y = (int16_t)(glyph.top - glyph.h);
    dsc->format = LV_FONT_GLYPH_FORMAT_A8;
    dsc->is_placeholder = 0;
    dsc->gid.index = letter;
    return true;
}

static const void *glyph_bitmap(lv_font_glyph_dsc_t *dsc, lv_draw_buf_t *draw_buf)
{
    const lv_font_t *font = dsc->resolved_font;
    if (!font || !draw_buf || !draw_buf->data) return NULL;
    const slot_t *slot = font->dsc;
    if (!slot) return NULL;

    ttf_glyph_t glyph;
    if (!ttf_font_glyph(dsc->gid.index, slot->px, &glyph)) return NULL;
    if (!glyph.bitmap || glyph.w <= 0 || glyph.h <= 0) return NULL;

    /* The cache stores glyphs tightly packed at w bytes per row; LVGL wants
     * them at its own A8 stride, which is padded. Copying row by row is what
     * keeps a 13-pixel-wide glyph from shearing across the buffer. */
    uint32_t stride = lv_draw_buf_width_to_stride((uint32_t)glyph.w,
                                                  LV_COLOR_FORMAT_A8);
    uint8_t *out = draw_buf->data;
    for (int row = 0; row < glyph.h; row++)
        memcpy(out + (size_t)row * stride,
               glyph.bitmap + (size_t)row * glyph.w, (size_t)glyph.w);
    return draw_buf;
}

/* ---------- lifetime ---------- */

esp_err_t mix_lv_font_init(void)
{
    if (!ttf_font_ready()) {
        /* Not fatal. mix_lv_font() then answers with LVGL's built-in font and
         * the interface stays readable, in ASCII, with no Chinese. */
        ESP_LOGW(TAG, "Font partition unavailable; LVGL will use its built-in font");
        return ESP_ERR_NOT_FOUND;
    }
    ESP_LOGI(TAG, "LVGL fonts served from the device font cache");
    return ESP_OK;
}

bool mix_lv_font_ready(void)
{
    return ttf_font_ready();
}

const lv_font_t *mix_lv_font(int px)
{
    if (!ttf_font_ready()) return LV_FONT_DEFAULT;
    if (px < 8) px = 8;
    if (px > 255) px = 255;

    int free_slot = -1;
    int nearest = -1;
    for (int i = 0; i < MIX_FONT_SLOTS; i++) {
        if (s_slots[i].used && s_slots[i].px == px) return &s_slots[i].font;
        if (!s_slots[i].used) {
            if (free_slot < 0) free_slot = i;
            continue;
        }
        if (nearest < 0 ||
            abs((int)s_slots[i].px - px) < abs((int)s_slots[nearest].px - px))
            nearest = i;
    }

    if (free_slot < 0) {
        /* Serving the closest instantiated size keeps text on screen; a NULL
         * font would blank the label instead, which reads as a broken screen
         * rather than as a slightly wrong size. */
        ESP_LOGW(TAG, "Font slots exhausted; %d px served at %d px",
                 px, nearest >= 0 ? s_slots[nearest].px : 0);
        return nearest >= 0 ? &s_slots[nearest].font : LV_FONT_DEFAULT;
    }

    ttf_metrics_t metrics;
    if (!ttf_font_metrics(px, &metrics)) {
        ESP_LOGE(TAG, "No metrics at %d px", px);
        return nearest >= 0 ? &s_slots[nearest].font : LV_FONT_DEFAULT;
    }

    slot_t *slot = &s_slots[free_slot];
    memset(&slot->font, 0, sizeof(slot->font));
    slot->px   = (uint16_t)px;
    slot->used = true;

    slot->font.get_glyph_dsc    = glyph_dsc;
    slot->font.get_glyph_bitmap = glyph_bitmap;
    slot->font.line_height      = metrics.line_height;
    /* LVGL measures base_line up from the bottom of the line box. */
    slot->font.base_line        = metrics.descent;
    slot->font.subpx            = LV_FONT_SUBPX_NONE;
    slot->font.kerning          = LV_FONT_KERNING_NONE;
    slot->font.underline_position  = (int8_t)(-metrics.descent / 2);
    slot->font.underline_thickness = (int8_t)(px >= 40 ? 3 : px >= 24 ? 2 : 1);
    slot->font.dsc              = slot;

    return &slot->font;
}
