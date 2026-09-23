// 中文 TTF 渲染（FreeType + raw sfnt TrueType 子集，font 分区 mmap 直读）
//
// 用法：ttf_font_init() 一次 → ttf_draw_text() 往 RGB565 framebuffer 画 UTF-8
// 文本（中英混排）。字形按 (codepoint,size) 缓存在 PSRAM，首次渲染 ~1-3ms/字，
// 命中后是纯 alpha 混合。仅限单任务使用（本工程 = app_main 主循环）。
#pragma once

#include <stdbool.h>
#include <stdint.h>
#include "esp_err.h"

// 映射 font 分区并初始化 FreeType。失败（分区空/字体损坏）返回错误码，
// 此后 ttf_draw_text 静默不画，调用方可回退 ASCII 点阵。
esp_err_t ttf_font_init(void);
// Single-task API: init is idempotent; failure releases mmap/FreeType/cache and
// may be retried. Deinit is idempotent and invalidates all cached glyphs.
void ttf_font_deinit(void);

bool ttf_font_ready(void);

// The mapped font partition, or NULL before a successful init. Valid until
// ttf_font_deinit.
#include <stddef.h>
const void *ttf_font_data(size_t *len);

// ---------------------------------------------------------------------------
// Glyph access, for building an lv_font_t on top of this cache.
//
// LVGL 9.2's own FreeType binding cannot be used here. Its ESP component
// declares only "REQUIRES esp_timer", so enabling LV_USE_FREETYPE compiles its
// binding without FreeType's headers reachable; and the lv_fs-backed path it
// would need (LV_FREETYPE_USE_LVGL_PORT) redefines FT_Stream_Open and the
// FreeType allocators, which the espressif/freetype component already
// provides. Rather than run a second FreeType, mix_lv_font.c wraps the cache
// below, which is already tuned for this device.
//
// Same single-task rule as the rest of this file. Once LVGL owns drawing, the
// only caller is the LVGL task; init runs before that task exists.
// ---------------------------------------------------------------------------
typedef struct {
    const uint8_t *bitmap;   // w*h 8-bit alpha, NULL for blank glyphs
    int16_t w, h;            // bitmap size in pixels
    int16_t left, top;       // FreeType bitmap_left / bitmap_top bearings
    int16_t advance;         // horizontal pen advance in pixels
} ttf_glyph_t;

typedef struct {
    int16_t ascent;          // above the baseline, positive
    int16_t descent;         // below the baseline, positive
    int16_t line_height;     // at least ascent + descent
} ttf_metrics_t;

// Metrics at a pixel size. False when the font is unavailable.
bool ttf_font_metrics(int size, ttf_metrics_t *out);

// One rendered glyph. The bitmap stays owned by the cache. Consume/copy it
// before any subsequent glyph-loading call (ttf_font_glyph, ttf_text_width or
// either draw function), which may evict it on a miss. Cache hits and metrics
// queries do not invalidate bitmaps; deinit always does. LVGL's adapter copies
// into its draw buffer immediately, rather than retaining this pointer.
// False when the font is unavailable or the glyph cannot render.
bool ttf_font_glyph(uint32_t codepoint, int size, ttf_glyph_t *out);

// Draw one codepoint inside a fixed cell only; caller supplies its background.
// Font metrics define a shared baseline. One uniform scale fits actual glyph
// extents, bearings/advance and bold expansion without stretching either axis.
// A fitting bitmap stays at its native pixel size; font line-box whitespace is
// not a reason to shrink it. Caller supplies one/two columns for narrow/wide
// codepoints. Cells are at most 64x64, height at least 2.
// No framebuffer write escapes the cell or framebuffer; size is 1..255 px.
void ttf_draw_cell(uint16_t *fb, int fb_w, int fb_h, int x, int y,
                   int cell_w, int cell_h, int size, uint16_t color,
                   uint32_t codepoint, bool bold);

// 在 fb（fb_w×fb_h RGB565）的 (x,y) 处画 UTF-8 文本，y 为文本行顶部。
// size 为像素字号（任意值，矢量缩放）。返回绘制后的 x 前进量（像素宽）。
int ttf_draw_text(uint16_t *fb, int fb_w, int fb_h,
                  int x, int y, int size, uint16_t color, const char *utf8);

// Draw UTF-8 text with an explicit vertical clip. This is used by the UI
// when a settings scroll exposes only a narrow strip; it prevents a glyph
// crossing that strip from touching rows outside the partial present.
int ttf_draw_text_clipped(uint16_t *fb, int fb_w, int fb_h,
                          int clip_y0, int clip_y1, int x, int y, int size,
                          uint16_t color, const char *utf8);

// 文本像素宽（不画，用于居中/右对齐）
int ttf_text_width(int size, const char *utf8);
