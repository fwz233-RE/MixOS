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

// Draw one codepoint inside a fixed cell only; caller supplies its background.
// Font metrics define a shared baseline; bearings and descenders are fitted,
// including bold expansion. Cells are at most 64x64, height at least 2.
// No framebuffer write escapes the cell or framebuffer; size is 1..255 px.
void ttf_draw_cell(uint16_t *fb, int fb_w, int fb_h, int x, int y,
                   int cell_w, int cell_h, int size, uint16_t color,
                   uint32_t codepoint, bool bold);

// 在 fb（fb_w×fb_h RGB565）的 (x,y) 处画 UTF-8 文本，y 为文本行顶部。
// size 为像素字号（任意值，矢量缩放）。返回绘制后的 x 前进量（像素宽）。
int ttf_draw_text(uint16_t *fb, int fb_w, int fb_h,
                  int x, int y, int size, uint16_t color, const char *utf8);

// 文本像素宽（不画，用于居中/右对齐）
int ttf_text_width(int size, const char *utf8);
