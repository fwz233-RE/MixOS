#include "ttf_font.h"

#include <string.h>
#include <stdlib.h>
#include <limits.h>

#include "esp_heap_caps.h"
#include "esp_log.h"
#include "esp_partition.h"

#include <ft2build.h>
#include FT_FREETYPE_H

static const char *TAG = "TTF";

static FT_Library s_lib;
static FT_Face    s_face;
static bool       s_ready;
static bool       s_mapped;
static esp_partition_mmap_handle_t s_map;
static int        s_cur_size = -1;   // face 当前 FT_Set_Pixel_Sizes 的字号
// The mapped partition itself. LVGL's FreeType binding opens fonts through
// lv_fs rather than from memory, so mix_lv_font serves this same mapping as a
// read-only file instead of mapping the partition a second time.
static const void *s_data;
static size_t      s_data_len;

// ---------------------------------------------------------------------------
// 字形缓存：开放寻址哈希表，key = codepoint<<8 | size（size ≤ 255）。
// 不做淘汰——本 UI 全部页面的 (字符,字号) 组合 < 千级；万一写满就整表清空重来。
// ---------------------------------------------------------------------------
typedef struct {
    uint32_t key;        // 0 = 空槽
    int16_t  w, h;       // 位图尺寸
    int16_t  left, top;  // FreeType bitmap_left / bitmap_top
    int16_t  adv;        // 水平前进量（像素）
    uint8_t *bmp;        // 8bpp alpha，PSRAM
} glyph_t;

#define CACHE_CAP 2048
static glyph_t *s_cache;      // PSRAM
static int s_cache_n;
static size_t s_cache_bytes;
#define GLYPH_BYTES_MAX (512u * 1024u)

static void cache_flush(void)
{
    if (s_cache) {
        for (int i = 0; i < CACHE_CAP; i++) free(s_cache[i].bmp);
        memset(s_cache, 0, CACHE_CAP * sizeof(glyph_t));
    }
    s_cache_n = 0;
    s_cache_bytes = 0;
}

static bool set_size(int size)
{
    if (!s_ready || size <= 0 || size > 255) return false;
    if (size != s_cur_size) {
        if (FT_Set_Pixel_Sizes(s_face, 0, (FT_UInt)size) != 0) return false;
        s_cur_size = size;
    }
    return true;
}

static glyph_t *cache_get(uint32_t cp, int size)
{
    if (!set_size(size)) return NULL;
    if (cp == 0 || cp > 0x10ffff || (cp >= 0xd800 && cp <= 0xdfff)) cp = 0xfffd;
    uint32_t key = (cp << 8) | (uint32_t)(size & 0xFF);
    uint32_t idx = (key * 2654435761u) & (CACHE_CAP - 1);
    for (int probe = 0; probe < CACHE_CAP; probe++, idx = (idx + 1) & (CACHE_CAP - 1)) {
        if (s_cache[idx].key == key) return &s_cache[idx];
        if (s_cache[idx].key == 0) {
            // 未命中：渲染进这个空槽
            if (s_cache_n > CACHE_CAP - 64) {
                ESP_LOGW(TAG, "字形缓存写满（%d），整表清空", s_cache_n);
                cache_flush();
                idx = (key * 2654435761u) & (CACHE_CAP - 1);
            }
            if (FT_Load_Char(s_face, cp, FT_LOAD_RENDER) != 0) return NULL;
            FT_GlyphSlot g = s_face->glyph;
            if (g->bitmap.pixel_mode != FT_PIXEL_MODE_GRAY || g->bitmap.num_grays != 256 ||
                g->bitmap.width > INT16_MAX || g->bitmap.rows > INT16_MAX) return NULL;
            size_t bytes = (size_t)g->bitmap.width * g->bitmap.rows;
            if (bytes > GLYPH_BYTES_MAX) return NULL;
            if (s_cache_bytes + bytes > GLYPH_BYTES_MAX) {
                cache_flush();
                idx = (key * 2654435761u) & (CACHE_CAP - 1);
            }
            glyph_t *e = &s_cache[idx];
            e->key  = key;
            e->w    = (int16_t)g->bitmap.width;
            e->h    = (int16_t)g->bitmap.rows;
            e->left = (int16_t)g->bitmap_left;
            e->top  = (int16_t)g->bitmap_top;
            e->adv  = (int16_t)(g->advance.x >> 6);
            e->bmp  = NULL;
            if (e->w > 0 && e->h > 0) {
                e->bmp = heap_caps_malloc((size_t)e->w * e->h,
                                          MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
                if (!e->bmp) { e->key = 0; return NULL; }
                s_cache_bytes += bytes;
                for (int row = 0; row < e->h; row++) {
                    memcpy(e->bmp + row * e->w,
                           g->bitmap.buffer + row * g->bitmap.pitch, e->w);
                }
            }
            s_cache_n++;
            return e;
        }
    }
    return NULL;
}

// ---------------------------------------------------------------------------
// UTF-8 → codepoint（非法序列按 '?' 处理并前进 1 字节）
// ---------------------------------------------------------------------------
static uint32_t utf8_next(const char **p)
{
    const uint8_t *s = (const uint8_t *)*p;
    uint32_t cp;
    int n;
    if (s[0] < 0x80)       { cp = s[0];         n = 1; }
    else if (s[0] < 0xE0)  { cp = s[0] & 0x1F;  n = 2; }
    else if (s[0] < 0xF0)  { cp = s[0] & 0x0F;  n = 3; }
    else                   { cp = s[0] & 0x07;  n = 4; }
    for (int i = 1; i < n; i++) {
        if ((s[i] & 0xC0) != 0x80) { *p += 1; return '?'; }
        cp = (cp << 6) | (s[i] & 0x3F);
    }
    *p += n;
    return cp;
}

// ---------------------------------------------------------------------------
// 初始化：mmap font 分区 → FT_New_Memory_Face
// ---------------------------------------------------------------------------
void ttf_font_deinit(void)
{
    s_ready = false;
    cache_flush();
    free(s_cache);
    s_cache = NULL;
    if (s_face) FT_Done_Face(s_face);
    s_face = NULL;
    if (s_lib) FT_Done_FreeType(s_lib);
    s_lib = NULL;
    if (s_mapped) esp_partition_munmap(s_map);
    s_mapped = false;
    s_map = 0;
    s_data = NULL;
    s_data_len = 0;
    s_cur_size = -1;
}

esp_err_t ttf_font_init(void)
{
    if (s_ready) return ESP_OK;
    ttf_font_deinit();
    const esp_partition_t *part = esp_partition_find_first(
        ESP_PARTITION_TYPE_DATA, ESP_PARTITION_SUBTYPE_ANY, "font");
    if (!part) {
        ESP_LOGE(TAG, "找不到 font 分区");
        return ESP_ERR_NOT_FOUND;
    }
    if (part->size < 12 || part->size > LONG_MAX) return ESP_ERR_INVALID_SIZE;
    const void *ptr = NULL;
    esp_err_t err = esp_partition_mmap(part, 0, part->size,
                                       ESP_PARTITION_MMAP_DATA, &ptr, &s_map);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "font 分区 mmap 失败: %s", esp_err_to_name(err));
        return err;
    }
    s_mapped = true;
    // Only raw sfnt TrueType, as produced by build_font.py, is supported here.
    static const uint8_t signature[] = {0, 1, 0, 0};
    if (!ptr || memcmp(ptr, signature, sizeof(signature)) != 0) {
        ESP_LOGE(TAG, "Font partition is empty or not raw sfnt TrueType");
        err = ESP_ERR_INVALID_STATE;
        goto fail;
    }
    if (FT_Init_FreeType(&s_lib) != 0) {
        ESP_LOGE(TAG, "FT_Init_FreeType 失败");
        err = ESP_FAIL;
        goto fail;
    }
    // FreeType 按 sfnt 表内偏移访问，分区尾部 0xFF 填充无害
    if (FT_New_Memory_Face(s_lib, ptr, (FT_Long)part->size, 0, &s_face) != 0) {
        ESP_LOGE(TAG, "FT_New_Memory_Face 失败（TTF 损坏？）");
        err = ESP_FAIL;
        goto fail;
    }
    if (!FT_IS_SCALABLE(s_face) || FT_Select_Charmap(s_face, FT_ENCODING_UNICODE) != 0) {
        ESP_LOGE(TAG, "Font requires scalable Unicode outlines");
        err = ESP_ERR_INVALID_STATE;
        goto fail;
    }
    s_cache = heap_caps_calloc(CACHE_CAP, sizeof(glyph_t),
                               MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
    if (!s_cache) {
        err = ESP_ERR_NO_MEM;
        goto fail;
    }
    s_ready = true;
    s_data = ptr;
    s_data_len = (size_t)part->size;
    ESP_LOGI(TAG, "TTF 就绪：%s %s，%ld 字形，分区 %lu KB @0x%lx",
             s_face->family_name ? s_face->family_name : "?",
             s_face->style_name ? s_face->style_name : "",
             (long)s_face->num_glyphs,
             (unsigned long)(part->size / 1024), (unsigned long)part->address);
    return ESP_OK;
fail:
    ttf_font_deinit();
    return err;
}

bool ttf_font_ready(void)
{
    return s_ready;
}

const void *ttf_font_data(size_t *len)
{
    if (len) *len = s_ready ? s_data_len : 0;
    return s_ready ? s_data : NULL;
}

bool ttf_font_metrics(int size, ttf_metrics_t *out)
{
    if (!out || !set_size(size)) return false;
    // Rounded up: a line box one pixel short clips descenders on every row.
    out->ascent      = (int16_t)((s_face->size->metrics.ascender + 63) / 64);
    out->descent     = (int16_t)((-s_face->size->metrics.descender + 63) / 64);
    out->line_height = (int16_t)((s_face->size->metrics.height + 63) / 64);
    if (out->line_height < out->ascent + out->descent)
        out->line_height = (int16_t)(out->ascent + out->descent);
    return true;
}

bool ttf_font_glyph(uint32_t codepoint, int size, ttf_glyph_t *out)
{
    if (!out) return false;
    glyph_t *e = cache_get(codepoint, size);
    if (!e) return false;
    // A blank glyph (space) is a hit with no bitmap, not a miss: it still
    // advances the pen, and reporting failure would make LVGL substitute a
    // placeholder box for every space.
    out->bitmap  = e->bmp;
    out->w       = e->w;
    out->h       = e->h;
    out->left    = e->left;
    out->top     = e->top;
    out->advance = e->adv;
    return true;
}

// RGB565 alpha 混合：dst = dst + (fg-dst)*a/255（逐通道）
static inline uint16_t blend565(uint16_t dst, uint16_t fg, uint8_t a)
{
    if (a >= 250) return fg;
    uint32_t dr = (dst >> 11) & 0x1F, dg = (dst >> 5) & 0x3F, db = dst & 0x1F;
    uint32_t fr = (fg >> 11) & 0x1F,  fgc = (fg >> 5) & 0x3F, fb = fg & 0x1F;
    uint32_t r = (dr * (255u-a) + fr * a + 127u) / 255u;
    uint32_t g = (dg * (255u-a) + fgc * a + 127u) / 255u;
    uint32_t b = (db * (255u-a) + fb * a + 127u) / 255u;
    return (uint16_t)((r << 11) | (g << 5) | b);
}

// Cell rendering uses font ascent/descent for a shared baseline, not a UI
// offset tied to one font. Each side of the baseline fits its metric extent;
// exceptional outlines beyond those metrics still fit without moving baseline.
// Horizontal bounds include bearings AND advance. Max-alpha area sampling keeps
// thin edge strokes when a proportional outline must shrink to one terminal cell.
void ttf_draw_cell(uint16_t *fb, int fb_w, int fb_h, int x, int y,
                   int cell_w, int cell_h, int size, uint16_t color,
                   uint32_t cp, bool bold)
{
    if (!fb || fb_w <= 0 || fb_h <= 0 || cell_w <= 0 || cell_w > 64 ||
        cell_h < 2 || cell_h > 64 || x >= fb_w || y >= fb_h ||
        x <= -cell_w || y <= -cell_h || !set_size(size)) return;
    if (cp == 0 || cp == ' ') return;
    glyph_t *e = cache_get(cp, size);
    if (!e || !e->bmp || e->w <= 0 || e->h <= 0) return;
    int ascent = (int)((s_face->size->metrics.ascender + 63) / 64);
    int descent = (int)((-s_face->size->metrics.descender + 63) / 64);
    if (ascent < 1) ascent = 1;
    if (descent < 1) descent = 1;
    int line_h = ascent + descent;
    int draw_h = line_h < cell_h ? line_h : cell_h;
    int baseline = (ascent * draw_h + line_h / 2) / line_h;
    if (baseline < 1) baseline = 1;
    if (baseline >= draw_h) baseline = draw_h - 1;
    int above = e->top > ascent ? e->top : ascent;
    int below = e->h - e->top > descent ? e->h - e->top : descent;
    int xmin = e->left < 0 ? e->left : 0;
    int xmax = e->left + e->w + (bold ? 1 : 0);
    if (xmax < e->adv) xmax = e->adv;
    int natural_w = xmax - xmin;
    if (natural_w <= 0) return;
    int draw_w = natural_w < cell_w ? natural_w : cell_w;
    int left = (cell_w - draw_w) / 2, top = (cell_h - draw_h) / 2;
    for (int dy = 0; dy < draw_h; dy++) {
        int fy = y + top + dy;
        if (fy < 0 || fy >= fb_h) continue;
        int region_y = dy < baseline ? dy : dy - baseline;
        int span = dy < baseline ? above : below;
        int pixels = dy < baseline ? baseline : draw_h - baseline;
        int origin = dy < baseline ? e->top - above : e->top;
        int sy0 = origin + region_y * span / pixels;
        int sy1 = origin + ((region_y + 1) * span + pixels - 1) / pixels;
        if (sy0 < 0) sy0 = 0;
        if (sy1 > e->h) sy1 = e->h;
        for (int dx = 0; dx < draw_w; dx++) {
            int fx = x + left + dx;
            if (fx < 0 || fx >= fb_w) continue;
            int sx0 = xmin - e->left + dx * natural_w / draw_w;
            int sx1 = xmin - e->left + ((dx + 1) * natural_w + draw_w - 1) / draw_w;
            if (sx0 < 0) sx0 = 0;
            if (sx1 > e->w + (bold ? 1 : 0)) sx1 = e->w + (bold ? 1 : 0);
            uint8_t alpha = 0;
            for (int sy = sy0; sy < sy1; sy++) {
                for (int sx = sx0; sx < sx1; sx++) {
                    uint8_t a = sx < e->w ? e->bmp[sy * e->w + sx] : 0;
                    if (bold && sx > 0 && e->bmp[sy * e->w + sx - 1] > a)
                        a = e->bmp[sy * e->w + sx - 1];
                    if (a > alpha) alpha = a;
                }
            }
            if (alpha) {
                uint16_t *dst = fb + (size_t)fy * fb_w + fx;
                *dst = blend565(*dst, color, alpha);
            }
        }
    }
}

int ttf_draw_text(uint16_t *fb, int fb_w, int fb_h,
                  int x, int y, int size, uint16_t color, const char *utf8)
{
    if (!fb || !utf8 || fb_w <= 0 || fb_h <= 0 || !set_size(size)) return 0;
    // y 是行顶：基线 = y + ascender（该字号下）
    int base_y = y + (int)(s_face->size->metrics.ascender >> 6);
    int pen_x = x;
    for (const char *p = utf8; *p; ) {
        uint32_t cp = utf8_next(&p);
        glyph_t *e = cache_get(cp, size);
        if (!e) { pen_x += size / 2; continue; }
        int gx0 = pen_x + e->left;
        int gy0 = base_y - e->top;
        for (int row = 0; row < e->h; row++) {
            int fy = gy0 + row;
            if (fy < 0 || fy >= fb_h) continue;
            const uint8_t *src = e->bmp + row * e->w;
            uint16_t *dst = fb + fy * fb_w;
            for (int col = 0; col < e->w; col++) {
                int fx = gx0 + col;
                if (fx < 0 || fx >= fb_w) continue;
                uint8_t a = src[col];
                if (a) dst[fx] = blend565(dst[fx], color, a);
            }
        }
        pen_x += e->adv;
    }
    return pen_x - x;
}

int ttf_text_width(int size, const char *utf8)
{
    if (!utf8 || !set_size(size)) return 0;
    int w = 0;
    for (const char *p = utf8; *p; ) {
        uint32_t cp = utf8_next(&p);
        glyph_t *e = cache_get(cp, size);
        w += e ? e->adv : size / 2;
    }
    return w;
}
