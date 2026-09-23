#include "mix_present.h"

#include <stddef.h>
#include <string.h>
#include "esp_attr.h"
#include "esp_idf_version.h"
#include "esp_lcd_panel_rgb.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "sdkconfig.h"

#if !defined(CONFIG_IDF_TARGET_ESP32S3) || ESP_IDF_VERSION != ESP_IDF_VERSION_VAL(5, 4, 2)
#error "Re-audit RGB bounce framebuffer handoff before changing target or ESP-IDF"
#endif
#if configSUPPORT_STATIC_ALLOCATION != 1
#error "mix_present requires a statically allocated ISR-safe semaphore"
#endif

/* IDF 5.4.2 esp_lcd_panel_rgb.c: driver-owned draw_bitmap changes cur_fb_index
 * without copying. At a bounce frame boundary, fill_bounce_buffer assigns
 * bb_fb_index = cur_fb_index BEFORE on_frame_buf_complete. Only then is the
 * old source safe to mirror. This contract does NOT apply to non-bounce DMA.
 * Keep the ISR and its shared state in internal memory even with IRAM safety.
 */
static DRAM_ATTR portMUX_TYPE s_mux = portMUX_INITIALIZER_UNLOCKED;
static DRAM_ATTR StaticSemaphore_t s_event_storage;
static DRAM_ATTR SemaphoreHandle_t s_event;
static DRAM_ATTR uint32_t s_frames;
static DRAM_ATTR bool s_isr_ready;

/* Task-owned state. No ISR accesses panel/framebuffer pointers or stats. */
static esp_lcd_panel_handle_t s_panel;
static uint16_t *s_fb[2];
static unsigned s_front;
static bool s_have_full_frame;
/* A deferred present leaves the released front buffer stale in the submitted
 * regions instead of mirroring them immediately. The next call copies these
 * regions together with its own update before it can submit, so the two driver
 * buffers remain safe without paying a second PSRAM copy on every scroll tick. */
static mix_present_rect_t s_pending[MIX_PRESENT_MAX_RECTS];
static unsigned s_pending_count;
static mix_present_stats_t s_stats;
static mix_present_wait_hook_t s_wait_hook;
static void *s_wait_ctx;

void mix_present_set_wait_hook(mix_present_wait_hook_t hook, void *ctx)
{
    s_wait_hook = hook;
    s_wait_ctx = ctx;
}

/* One half-open dirty span per body row [BODY_Y,BODY_END), never bottom nav.
 * No second canvas: the caller lends its frozen final image until end/cancel.
 * All fade state is task-owned, including the pre-shifted RGB565 lookup tables.
 */
static struct {
    const uint16_t *canvas;
    uint16_t x0[MIX_PRESENT_BODY_END - MIX_PRESENT_BODY_Y];
    uint16_t x1[MIX_PRESENT_BODY_END - MIX_PRESENT_BODY_Y];
    uint16_t red[32], green[64], blue[32];
    uint32_t pixels;
    uint16_t background;
} s_fade;
_Static_assert(sizeof(s_fade) <= 8192, "fade scratch exceeds 8 KiB");

void mix_present_fade_end(void)
{
    s_fade.canvas = NULL;
    s_fade.pixels = 0;
}

static uint32_t elapsed_us(int64_t start)
{
    uint64_t elapsed = (uint64_t)(esp_timer_get_time() - start);
    return elapsed > UINT32_MAX ? UINT32_MAX : (uint32_t)elapsed;
}

esp_err_t mix_present_init(esp_lcd_panel_handle_t raw_panel)
{
    if (s_stats.initialized) return ESP_ERR_INVALID_STATE;
    if (!raw_panel) return ESP_ERR_INVALID_ARG;

    void *fb0 = NULL;
    void *fb1 = NULL;
    esp_err_t err = esp_lcd_rgb_panel_get_frame_buffer(raw_panel, 2, &fb0, &fb1);
    if (err != ESP_OK) return err;
    if (!fb0 || !fb1 || fb0 == fb1) return ESP_ERR_INVALID_STATE;

    SemaphoreHandle_t event = xSemaphoreCreateBinaryStatic(&s_event_storage);
    if (!event) return ESP_ERR_NO_MEM;
    s_panel = raw_panel;
    s_fb[0] = fb0;
    s_fb[1] = fb1;
    s_front = 0;
    s_have_full_frame = false;
    s_pending_count = 0;
    s_stats.initialized = 1;

    portENTER_CRITICAL(&s_mux);
    s_event = event;
    s_frames = 0;
    s_isr_ready = true;
    portEXIT_CRITICAL(&s_mux);
    return ESP_OK;
}

bool IRAM_ATTR mix_present_frame_complete(void)
{
    SemaphoreHandle_t event = NULL;
    portENTER_CRITICAL_ISR(&s_mux);
    if (s_isr_ready) {
        ++s_frames;
        event = s_event;
    }
    portEXIT_CRITICAL_ISR(&s_mux);

    /* The static semaphore is never deleted/reinitialized once published.
     * A delayed give after fault latching is harmless. A token is only a wake
     * hint; the protected counter, not a stale token, acknowledges a frame. */
    BaseType_t higher_priority_woken = pdFALSE;
    if (event) xSemaphoreGiveFromISR(event, &higher_priority_woken);
    return higher_priority_woken == pdTRUE;
}

static uint32_t frame_count(void)
{
    portENTER_CRITICAL(&s_mux);
    uint32_t count = s_frames;
    portEXIT_CRITICAL(&s_mux);
    return count;
}

static esp_err_t wait_for_frame(uint32_t baseline, int64_t wait_start)
{
    const int64_t deadline = wait_start + MIX_PRESENT_TIMEOUT_MS * INT64_C(1000);
    int64_t next_sample = wait_start;
    for (;;) {
        uint32_t count = frame_count();
        int64_t now = esp_timer_get_time();
        int64_t remaining = deadline - now;
        if (remaining <= 0) return ESP_ERR_TIMEOUT;
        if (count != baseline) return ESP_OK;

        /* Sampling is task-context and cannot mutate the borrowed canvas.
         * Keep the SAME deadline, and recheck both time and acknowledgement
         * after a hook: the ISR can release the buffer while input is read. */
        if (s_wait_hook && now >= next_sample && remaining >= 20000) {
            next_sample = now + 8000;
            s_wait_hook(s_wait_ctx);
            continue;
        }
        if (s_wait_hook && remaining >= 20000 && next_sample > now &&
            remaining > next_sample - now) remaining = next_sample - now;
        /* Round up, never busy-spin on a stale token or zero-tick sleep. */
        TickType_t ticks = (TickType_t)((remaining * configTICK_RATE_HZ + 999999) / 1000000);
        (void)xSemaphoreTake(s_event, ticks);
    }
}

static void copy_rect(uint16_t *dst, const uint16_t *canvas,
                      int x0, int y0, int x1, int y1)
{
    size_t row_bytes = (size_t)(x1 - x0) * sizeof(*dst);
    for (int y = y0; y < y1; ++y) {
        size_t offset = (size_t)y * MIX_PRESENT_WIDTH + (size_t)x0;
        memcpy(dst + offset, canvas + offset, row_bytes);
    }
    s_stats.bytes_copied += (uint64_t)row_bytes * (unsigned)(y1 - y0);
}

/* Mirror the completed RGB565 update into the released driver buffer. */
static void mirror_rect(uint16_t *dst, const uint16_t *src,
                        int x0, int y0, int x1, int y1)
{
    size_t row_bytes = (size_t)(x1 - x0) * sizeof(*dst);
    for (int y = y0; y < y1; ++y) {
        size_t offset = (size_t)y * MIX_PRESENT_WIDTH + (size_t)x0;
        memcpy(dst + offset, src + offset, row_bytes);
    }
    s_stats.bytes_copied += (uint64_t)row_bytes * (unsigned)(y1 - y0);
}

static bool overlaps_fb(const void *data, size_t bytes)
{
    const uintptr_t fb_bytes = MIX_PRESENT_WIDTH * MIX_PRESENT_HEIGHT * sizeof(uint16_t);
    uintptr_t begin = (uintptr_t)data;
    if (begin > UINTPTR_MAX - bytes) return true;
    for (unsigned i = 0; i < 2; ++i) {
        uintptr_t fb = (uintptr_t)s_fb[i];
        if (begin < fb + fb_bytes && fb < begin + bytes) return true;
    }
    return false;
}

static bool canvas_overlaps_fb(const uint16_t *canvas)
{
    return overlaps_fb(canvas, MIX_PRESENT_WIDTH * MIX_PRESENT_HEIGHT * sizeof(*canvas));
}

static esp_err_t finish_attempt(esp_err_t err, int64_t start, uint32_t wait_us)
{
    s_stats.last_us = elapsed_us(start);
    if (s_stats.last_us > s_stats.max_us) s_stats.max_us = s_stats.last_us;
    s_stats.total_us += s_stats.last_us;
    s_stats.wait_us = wait_us;
    s_stats.total_wait_us += wait_us;
    s_stats.last_error = err;
    if (err == ESP_OK) {
        ++s_stats.present_count;
    } else {
        /* A failed draw might already have changed the driver's selection.
         * Never infer ownership, write again, or let re-init clear this fault. */
        s_stats.locked = 1;
        mix_present_fade_end();
        ++s_stats.failure_count;
        portENTER_CRITICAL(&s_mux);
        s_isr_ready = false;
        portEXIT_CRITICAL(&s_mux);
    }
    return err;
}

static esp_err_t submit_back(unsigned back, uint32_t *wait_us)
{
    *wait_us = 0;
    /* The FULL driver-owned pointer selects the no-copy path. All dirty rows
     * are already written to back; submit exactly once for the entire batch.
     * Do not hold our spinlock across any driver call. */
    esp_err_t err = esp_lcd_panel_draw_bitmap(s_panel, 0, 0,
                                             MIX_PRESENT_WIDTH, MIX_PRESENT_HEIGHT,
                                             s_fb[back]);
    if (err != ESP_OK) return err;

    /* Capture AFTER submission: callbacks before/during draw cannot release
     * the old buffer. It is safe to conservatively wait one extra whole frame.
     * Same-core ISR allocation prevents a pre-submit ISR from being suspended
     * between bb_fb_index assignment and callback across this task's submit. */
    int64_t wait_start = esp_timer_get_time();
    uint32_t baseline = frame_count();
    err = wait_for_frame(baseline, wait_start);
    *wait_us = elapsed_us(wait_start);
    return err;
}

static bool valid_rect(const mix_present_rect_t *r)
{
    return r && r->x0 >= 0 && r->y0 >= 0 && r->x1 <= MIX_PRESENT_WIDTH &&
           r->y1 <= MIX_PRESENT_HEIGHT && r->x0 < r->x1 && r->y0 < r->y1;
}

static bool rects_touch(const mix_present_rect_t *a, const mix_present_rect_t *b)
{
    return a->x0 <= b->x1 && b->x0 <= a->x1 &&
           a->y0 <= b->y1 && b->y0 <= a->y1;
}

static mix_present_rect_t rect_union(const mix_present_rect_t *a,
                                     const mix_present_rect_t *b)
{
    mix_present_rect_t r = {
        a->x0 < b->x0 ? a->x0 : b->x0,
        a->y0 < b->y0 ? a->y0 : b->y0,
        a->x1 > b->x1 ? a->x1 : b->x1,
        a->y1 > b->y1 ? a->y1 : b->y1,
    };
    return r;
}

/* Deferred updates can carry the previous buffer's stale regions as well as
 * the new ones. Coalesce touching regions so a repeated full-body scroll does
 * one copy, not two overlapping copies. This is only used for deferred work;
 * the ordinary API keeps its exact per-rectangle accounting. */
static unsigned compact_rects(mix_present_rect_t *rects, unsigned count)
{
    bool merged;
    do {
        merged = false;
        for (unsigned i = 0; i < count && !merged; ++i) {
            for (unsigned j = i + 1; j < count; ++j) {
                if (!rects_touch(&rects[i], &rects[j])) continue;
                mix_present_rect_t united = rect_union(&rects[i], &rects[j]);
                unsigned combined = (unsigned)(united.x1 - united.x0) *
                                    (unsigned)(united.y1 - united.y0);
                unsigned separate = (unsigned)(rects[i].x1 - rects[i].x0) *
                                    (unsigned)(rects[i].y1 - rects[i].y0) +
                                    (unsigned)(rects[j].x1 - rects[j].x0) *
                                    (unsigned)(rects[j].y1 - rects[j].y0);
                /* A body+footer L shape must not drag the fixed sidebar into
                 * every scroll. Merge only when it does not increase writes. */
                if (combined > separate) continue;
                rects[i] = united;
                memmove(&rects[j], &rects[j + 1],
                        (count - j - 1) * sizeof(*rects));
                --count;
                merged = true;
                break;
            }
        }
    } while (merged);
    while (count > MIX_PRESENT_MAX_RECTS) {
        rects[0] = rect_union(&rects[0], &rects[1]);
        memmove(&rects[1], &rects[2],
                (count - 2) * sizeof(*rects));
        --count;
    }
    return count;
}

static esp_err_t present_rects_internal(const mix_present_rect_t *rects,
                                        unsigned count,
                                        const uint16_t *canvas,
                                        bool defer_mirror)
{
    /* Cancel even on rejected calls: the owner may immediately reuse canvas. */
    mix_present_fade_end();
    if (!s_stats.initialized) return ESP_ERR_INVALID_STATE;
    if (s_stats.locked) return s_stats.last_error;
    if (!rects || !count || count > MIX_PRESENT_MAX_RECTS ||
        (uintptr_t)rects % _Alignof(mix_present_rect_t) ||
        overlaps_fb(rects, count * sizeof(*rects)) ||
        !canvas || (uintptr_t)canvas % _Alignof(uint16_t) ||
        canvas_overlaps_fb(canvas)) return ESP_ERR_INVALID_ARG;
    /* Validate the WHOLE new batch before any copy or stats update. */
    for (unsigned i = 0; i < count; ++i)
        if (!valid_rect(&rects[i])) return ESP_ERR_INVALID_ARG;
    if (!s_have_full_frame && (count != 1 || rects[0].x0 != 0 ||
        rects[0].y0 != 0 || rects[0].x1 != MIX_PRESENT_WIDTH ||
        rects[0].y1 != MIX_PRESENT_HEIGHT)) return ESP_ERR_INVALID_STATE;

    /* A deferred call leaves s_pending regions stale in the next back buffer.
     * Include them before every later submission. The current front is equal
     * to canvas outside the new regions, so it becomes the next pending set. */
    mix_present_rect_t work[MIX_PRESENT_MAX_RECTS * 2];
    unsigned work_count = s_pending_count;
    memcpy(work, s_pending, work_count * sizeof(*work));
    memcpy(work + work_count, rects, count * sizeof(*rects));
    work_count += count;
    if (defer_mirror || s_pending_count) work_count = compact_rects(work, work_count);

    int64_t start = esp_timer_get_time();
    unsigned back = s_front ^ 1u;
    for (unsigned i = 0; i < work_count; ++i)
        copy_rect(s_fb[back], canvas, work[i].x0, work[i].y0,
                  work[i].x1, work[i].y1);
    uint32_t wait_us;
    esp_err_t err = submit_back(back, &wait_us);
    if (err != ESP_OK) return finish_attempt(err, start, wait_us);

    if (defer_mirror) {
        /* The next call will update the old front with s_pending plus its new
         * regions before submitting it. Avoid a second large PSRAM copy now. */
        memcpy(s_pending, rects, count * sizeof(*s_pending));
        s_pending_count = count;
    } else {
        for (unsigned i = 0; i < work_count; ++i)
            mirror_rect(s_fb[s_front], s_fb[back], work[i].x0, work[i].y0,
                      work[i].x1, work[i].y1);
        s_pending_count = 0;
    }
    s_front = back;
    s_have_full_frame = true;
    return finish_attempt(ESP_OK, start, wait_us);
}

esp_err_t mix_present_rects(const mix_present_rect_t *rects, unsigned count,
                            const uint16_t *canvas)
{
    return present_rects_internal(rects, count, canvas, false);
}

esp_err_t mix_present_rects_deferred(const mix_present_rect_t *rects,
                                     unsigned count, const uint16_t *canvas)
{
    return present_rects_internal(rects, count, canvas, true);
}

esp_err_t mix_present_rect(int x0, int y0, int x1, int y1,
                           const uint16_t *canvas)
{
    const mix_present_rect_t rect = {x0, y0, x1, y1};
    return mix_present_rects(&rect, 1, canvas);
}

esp_err_t mix_present_fade_begin(const uint16_t *canvas, uint16_t background)
{
    if (!s_stats.initialized) return ESP_ERR_INVALID_STATE;
    if (s_stats.locked) return s_stats.last_error;
    if (!canvas || (uintptr_t)canvas % _Alignof(uint16_t) ||
        canvas_overlaps_fb(canvas)) return ESP_ERR_INVALID_ARG;
    if (!s_have_full_frame || s_pending_count || s_fade.canvas)
        return ESP_ERR_INVALID_STATE;

    /* Both driver bodies equal either the canvas (OUT) or background (IN).
     * Trust that caller precondition instead of reading scanout or scanning
     * the normal present path. Begin NEVER touches driver memory. */
    uint32_t pixels = 0;
    for (unsigned row = 0; row < MIX_PRESENT_BODY_END - MIX_PRESENT_BODY_Y; ++row) {
        const uint16_t *src = canvas + (size_t)(row + MIX_PRESENT_BODY_Y) * MIX_PRESENT_WIDTH;
        unsigned x0 = MIX_PRESENT_WIDTH, x1 = 0;
        for (unsigned x = 0; x < MIX_PRESENT_WIDTH; ++x) {
            if (src[x] != background) {
                if (x0 == MIX_PRESENT_WIDTH) x0 = x;
                x1 = x + 1;
            }
        }
        if (!x1) x0 = 0;
        s_fade.x0[row] = (uint16_t)x0;
        s_fade.x1[row] = (uint16_t)x1;
        pixels += x1 - x0;
    }
    s_fade.background = background;
    s_fade.pixels = pixels;
    s_fade.canvas = canvas;
    return ESP_OK;
}

static void fade_lookup(uint8_t opacity)
{
    unsigned a = opacity, inverse = 255u - a;
    unsigned r = s_fade.background >> 11;
    unsigned g = (s_fade.background >> 5) & 63u;
    unsigned b = s_fade.background & 31u;
    /* Round to nearest channel value. Division only in 128 table entries,
     * never in the pixel loop. Each step refers to the original target. */
    for (unsigned c = 0; c < 32; ++c) {
        s_fade.red[c] = (uint16_t)(((c * a + r * inverse + 127u) / 255u) << 11);
        s_fade.blue[c] = (uint16_t)((c * a + b * inverse + 127u) / 255u);
    }
    for (unsigned c = 0; c < 64; ++c) {
        s_fade.green[c] = (uint16_t)(((c * a + g * inverse + 127u) / 255u) << 5);
    }
}

static void fade_write_back(uint16_t *dst, uint8_t opacity)
{
    if (opacity != 0 && opacity != 255) fade_lookup(opacity);
    for (unsigned row = 0; row < MIX_PRESENT_BODY_END - MIX_PRESENT_BODY_Y; ++row) {
        unsigned count = s_fade.x1[row] - s_fade.x0[row];
        if (!count) continue;
        size_t offset = (size_t)(row + MIX_PRESENT_BODY_Y) * MIX_PRESENT_WIDTH + s_fade.x0[row];
        uint16_t *out = dst + offset;
        const uint16_t *src = s_fade.canvas + offset;
        if (opacity == 255) {
            memcpy(out, src, count * sizeof(*out));
        } else if (!opacity) {
            for (unsigned x = 0; x < count; ++x) out[x] = s_fade.background;
        } else {
            for (unsigned x = 0; x < count; ++x) {
                uint16_t pixel = src[x];
                out[x] = s_fade.red[pixel >> 11] |
                         s_fade.green[(pixel >> 5) & 63u] | s_fade.blue[pixel & 31u];
            }
        }
    }
    s_stats.bytes_copied += (uint64_t)s_fade.pixels * sizeof(*dst);
    if (opacity != 0 && opacity != 255) s_stats.pixels_blended += s_fade.pixels;
}

esp_err_t mix_present_fade_step(uint8_t opacity)
{
    if (!s_stats.initialized) return ESP_ERR_INVALID_STATE;
    if (s_stats.locked) return s_stats.last_error;
    if (!s_fade.canvas) return ESP_ERR_INVALID_STATE;
    /* Empty body is a no-op, including statistics, submission and wait. */
    if (!s_fade.pixels) return ESP_OK;

    int64_t start = esp_timer_get_time();
    unsigned back = s_front ^ 1u;
    fade_write_back(s_fb[back], opacity);
    uint32_t wait_us;
    esp_err_t err = submit_back(back, &wait_us);
    if (err != ESP_OK) return finish_attempt(err, start, wait_us);

    /* Read-only access to the new front is safe. Mirror its computed result,
     * rather than blending a second time; the former front is now released. */
    for (unsigned row = 0; row < MIX_PRESENT_BODY_END - MIX_PRESENT_BODY_Y; ++row) {
        if (s_fade.x0[row] == s_fade.x1[row]) continue;
        int y = (int)row + MIX_PRESENT_BODY_Y;
        mirror_rect(s_fb[s_front], s_fb[back], s_fade.x0[row], y, s_fade.x1[row], y + 1);
    }
    s_front = back;
    return finish_attempt(ESP_OK, start, wait_us);
}

esp_err_t mix_present_get_stats(mix_present_stats_t *out)
{
    if (!out) return ESP_ERR_INVALID_ARG;
    *out = s_stats;
    out->frame_count = frame_count();
    return ESP_OK;
}
