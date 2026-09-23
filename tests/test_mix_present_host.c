/* Actual production presenter linked against deterministic SDK/RTOS stubs.
 * Every scenario runs in a fresh process: no production reset/test-only API.
 */
#include <assert.h>
#include <limits.h>
#include <stdarg.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "mix_present.h"
#include "esp_lcd_panel_rgb.h"
#include "freertos/semphr.h"

#define PIXELS ((size_t)MIX_PRESENT_WIDTH * MIX_PRESENT_HEIGHT)
#define FRAME_BYTES (PIXELS * sizeof(uint16_t))
#define BODY_FIRST_PIXEL ((size_t)MIX_PRESENT_BODY_Y * MIX_PRESENT_WIDTH)
#define BODY_END_PIXEL ((size_t)MIX_PRESENT_BODY_END * MIX_PRESENT_WIDTH)
#define BODY_PIXELS (BODY_END_PIXEL - BODY_FIRST_PIXEL)
#define BOTTOM_NAV_BYTES ((PIXELS - BODY_END_PIXEL) * sizeof(uint16_t))
_Static_assert(MIX_PRESENT_WIDTH == 1024 && MIX_PRESENT_HEIGHT == 768,
               "presenter requires a 1024x768 display");
_Static_assert(MIX_PRESENT_BODY_Y == 0 && MIX_PRESENT_BODY_END == 648,
               "body must be [0,648), bottom navigation/status [648,768)");
_Static_assert(MIX_PRESENT_MAX_RECTS == 8, "batch API permits at most eight rectangles");
#define GUARD 0xcafeu
#define DRAW_ERROR ((esp_err_t)0x4567)

struct mock_panel { int unused; };
static struct mock_panel panel;
struct guarded_frame {
    uint16_t before[16];
    uint16_t pixels[1024 * 768];
    uint16_t after[16];
};
static struct guarded_frame buffers[2];
static uint16_t canvas[1024 * 768];
static uint16_t old_front[1024 * 768];
static uint16_t locked_copy[2][1024 * 768];
static uint16_t expected_fade[1024 * 768];
static const uint16_t *expected_image = canvas;
static uint16_t fade_background;
static uint32_t fade_pixels;

/* Independent screen/stale-pixel model for deferred calls. It never computes
 * production's rectangle compaction: the complete canvas is the oracle, and
 * only the most recently submitted rectangles may be stale in the other FB. */
static uint16_t model_screen[1024 * 768];
static uint8_t model_stale[1024 * 768];
static uint8_t model_needed[1024 * 768];
static bool check_deferred_handoff;

static int64_t now_us;
static unsigned selected, scanning, expected_back, protected_index;
static bool protect_front;
static bool in_isr;
static int lock_depth;
static unsigned get_calls, create_calls, draw_calls, take_calls, give_calls, woken_calls;
static esp_err_t get_error, draw_error;
static unsigned invalid_buffer_mode;
static bool create_failure, init_callbacks, publish_callback;
static bool fire_after_publish;
static bool draw_returned;
static bool switch_before_error;
static unsigned early_before_select, early_after_select, future_duplicates = 1;
static int64_t draw_cost_us = 2500;
static int64_t ack_at_us = -1, ack_delay_us;
static int64_t wait_started_us;
static SemaphoreHandle_t event_handle;

enum plan { NO_ACK, DELAYED_ACK, ACK_AT_BASELINE_EXIT, ACK_AT_TAKE, SPURIOUS };
static enum plan current_plan;

static void guards_ok(void)
{
    for (unsigned b = 0; b < 2; ++b) {
        for (unsigned i = 0; i < 16; ++i) {
            assert(buffers[b].before[i] == GUARD);
            assert(buffers[b].after[i] == GUARD);
        }
    }
}

static void old_front_unchanged(void)
{
    guards_ok();
    if (protect_front) assert(memcmp(old_front, buffers[protected_index].pixels, FRAME_BYTES) == 0);
}

static bool callback(void)
{
    assert(!in_isr && lock_depth == 0);
    in_isr = true;
    bool woken = mix_present_frame_complete();
    in_isr = false;
    if (woken) ++woken_calls;
    return woken;
}

/* Model the REAL driver's ordering: select the next bounce source first,
 * then invoke the callback synchronously in ISR context. An early event may
 * happen before/inside draw; only a post-baseline future event releases the
 * test's old-front write prohibition. The callback itself must never write.
 */
static void emit_frame(bool future, unsigned copies)
{
    old_front_unchanged();
    scanning = selected;
    if (check_deferred_handoff) {
        const uint16_t *screen = scanning == protected_index ? model_screen : canvas;
        assert(memcmp(buffers[scanning].pixels, screen, FRAME_BYTES) == 0);
    }
    for (unsigned i = 0; i < copies; ++i) {
        callback();
        old_front_unchanged();
    }
    if (future) protect_front = false;
}

void mock_enter(portMUX_TYPE *mux, bool isr)
{
    assert(mux && isr == in_isr && lock_depth == 0);
    ++lock_depth;
}

void mock_exit(portMUX_TYPE *mux, bool isr)
{
    assert(mux && isr == in_isr && lock_depth == 1);
    --lock_depth;
    if (!isr && fire_after_publish) {
        fire_after_publish = false;
        assert(!callback());
    }
    if (!isr && draw_returned && current_plan == ACK_AT_BASELINE_EXIT) {
        current_plan = NO_ACK;
        emit_frame(true, future_duplicates);
    }
}

int64_t esp_timer_get_time(void)
{
    assert(lock_depth == 0 && !in_isr);
    return now_us;
}

SemaphoreHandle_t xSemaphoreCreateBinaryStatic(StaticSemaphore_t *storage)
{
    assert(lock_depth == 0 && !in_isr && !event_handle);
    ++create_calls;
    if (init_callbacks) assert(!callback());
    if (create_failure) return NULL;
    storage->token = 0;
    storage->waiting = false;
    event_handle = storage;
    fire_after_publish = publish_callback;
    return storage;
}

BaseType_t xSemaphoreGiveFromISR(SemaphoreHandle_t event, BaseType_t *woken)
{
    assert(in_isr && lock_depth == 0 && event == event_handle && event);
    assert(woken && *woken == pdFALSE);
    ++give_calls;
    if (event->token) return pdFALSE;
    event->token = 1;
    if (event->waiting) *woken = pdTRUE;
    return pdTRUE;
}

BaseType_t xSemaphoreTake(SemaphoreHandle_t event, TickType_t ticks)
{
    assert(!in_isr && lock_depth == 0 && event == event_handle && event);
    assert(ticks > 0 && ticks < UINT32_MAX); /* No polling or infinite wait. */
    ++take_calls;
    assert(take_calls < 1000); /* Catch an implementation that never terminates. */
    old_front_unchanged();
    if (current_plan == ACK_AT_TAKE) {
        current_plan = NO_ACK;
        emit_frame(true, future_duplicates);
    }
    if (event->token) {
        event->token = 0;
        return pdTRUE;
    }

    int64_t span = ((int64_t)ticks * 1000000 + TEST_TICK_HZ - 1) / TEST_TICK_HZ;
    int64_t end = now_us + span;
    event->waiting = true;
    if (ack_at_us >= 0 && ack_at_us <= end) {
        assert(ack_at_us >= now_us);
        now_us = ack_at_us;
        ack_at_us = -1;
        emit_frame(true, future_duplicates);
        assert(event->token == 1);
        event->token = 0;
        event->waiting = false;
        return pdTRUE;
    }
    if (current_plan == SPURIOUS) {
        now_us += span < 17000 ? span : 17000;
        event->waiting = false;
        old_front_unchanged();
        return pdTRUE; /* Wake hint without any actual frame counter change. */
    }
    now_us = end;
    event->waiting = false;
    old_front_unchanged();
    return pdFALSE;
}

esp_err_t esp_lcd_rgb_panel_get_frame_buffer(esp_lcd_panel_handle_t raw_panel,
                                            uint32_t count, void **fb0, ...)
{
    assert(raw_panel == &panel && count == 2 && fb0 && lock_depth == 0);
    ++get_calls;
    if (init_callbacks) assert(!callback());
    if (get_error) return get_error;
    va_list args;
    va_start(args, fb0);
    void **fb1 = va_arg(args, void **);
    va_end(args);
    assert(fb1);
    *fb0 = invalid_buffer_mode == 1 ? NULL : buffers[0].pixels;
    *fb1 = invalid_buffer_mode == 2 ? NULL :
           invalid_buffer_mode == 3 ? buffers[0].pixels : buffers[1].pixels;
    return ESP_OK;
}

esp_err_t esp_lcd_panel_draw_bitmap(esp_lcd_panel_handle_t raw_panel,
                                   int x0, int y0, int x1, int y1, const void *pixels)
{
    /* Holding a presenter spinlock across driver submission is forbidden. */
    assert(!in_isr && lock_depth == 0 && raw_panel == &panel);
    assert(x0 == 0 && y0 == 0 && x1 == MIX_PRESENT_WIDTH && y1 == MIX_PRESENT_HEIGHT);
    assert(pixels == buffers[expected_back].pixels);
    assert(memcmp(pixels, expected_image, FRAME_BYTES) == 0);
    old_front_unchanged();
    ++draw_calls;
    if (early_before_select) emit_frame(false, early_before_select);
    if (!draw_error || switch_before_error) selected = expected_back;
    if (early_after_select) emit_frame(false, early_after_select);
    now_us += draw_cost_us;
    wait_started_us = now_us;
    if (draw_error) return draw_error;
    if (current_plan == DELAYED_ACK) ack_at_us = now_us + ack_delay_us;
    draw_returned = true;
    return ESP_OK;
}

static mix_present_stats_t stats(void)
{
    mix_present_stats_t result;
    memset(&result, 0xcc, sizeof(result));
    assert(mix_present_get_stats(&result) == ESP_OK);
    return result;
}

static void initial_data(void)
{
    for (unsigned b = 0; b < 2; ++b) {
        for (unsigned i = 0; i < 16; ++i) {
            buffers[b].before[i] = GUARD;
            buffers[b].after[i] = GUARD;
        }
        for (size_t i = 0; i < PIXELS; ++i) buffers[b].pixels[i] = b ? 0x2222 : 0x1111;
    }
    for (size_t i = 0; i < PIXELS; ++i) canvas[i] = (uint16_t)(i * 17u + i / 1024 + 3u);
}

static void begin_attempt(enum plan plan, int64_t delay)
{
    assert(scanning == selected);
    protected_index = scanning;
    expected_back = scanning ^ 1u;
    memcpy(old_front, buffers[protected_index].pixels, FRAME_BYTES);
    protect_front = true;
    current_plan = plan;
    ack_delay_us = delay;
    ack_at_us = -1;
    draw_returned = false;
}

static void both_match_canvas(void)
{
    guards_ok();
    assert(memcmp(buffers[0].pixels, canvas, FRAME_BYTES) == 0);
    assert(memcmp(buffers[1].pixels, canvas, FRAME_BYTES) == 0);
    assert(scanning == expected_back && selected == expected_back);
}

static void initial_full(void)
{
    assert(mix_present_init(&panel) == ESP_OK);
    begin_attempt(DELAYED_ACK, 10000);
    assert(mix_present_rect(0, 0, 1024, 768, canvas) == ESP_OK);
    both_match_canvas();
    assert(stats().present_count == 1);
}

static void change_rect(int x0, int y0, int x1, int y1, unsigned seed)
{
    for (int y = y0; y < y1; ++y) {
        for (int x = x0; x < x1; ++x) {
            canvas[(size_t)y * 1024 + x] ^= (uint16_t)(seed | 1u);
        }
    }
}

static void stays_locked(esp_err_t error)
{
    unsigned calls = draw_calls;
    mix_present_stats_t previous = stats();
    assert(previous.locked == 1 && previous.failure_count == 1 && previous.last_error == error);
    memcpy(locked_copy[0], buffers[0].pixels, FRAME_BYTES);
    memcpy(locked_copy[1], buffers[1].pixels, FRAME_BYTES);
    change_rect(0, 0, 1024, 768, 0x4321);
    assert(mix_present_rect(0, 0, 1024, 768, canvas) == error);
    const mix_present_rect_t rects[] = {{0, 0, 2, 2}, {1022, 766, 1024, 768}};
    assert(mix_present_rects(rects, 2, canvas) == error);
    assert(mix_present_rects(NULL, 0, NULL) == error);
    assert(mix_present_rects_deferred(rects, 2, canvas) == error);
    const mix_present_rect_t full = {0, 0, 1024, 768};
    assert(mix_present_rects_deferred(&full, 1, canvas) == error);
    assert(mix_present_rects_deferred(NULL, 0, NULL) == error);
    assert(mix_present_rects_deferred(rects, UINT_MAX, canvas) == error);
    mix_present_fade_end();
    mix_present_fade_end();
    assert(mix_present_fade_begin(canvas, 0x39e7) == error);
    assert(mix_present_fade_step(0) == error);
    assert(mix_present_fade_step(255) == error);
    assert(mix_present_init(&panel) == ESP_ERR_INVALID_STATE);
    unsigned gives = give_calls;
    scanning = selected; /* A late REAL handoff still cannot clear the fault. */
    assert(!callback());
    assert(!callback());
    assert(give_calls == gives);
    assert(mix_present_rect(5, 7, 9, 11, canvas) == error);
    assert(mix_present_rects_deferred(&full, 1, canvas) == error);
    assert(draw_calls == calls);
    assert(memcmp(locked_copy[0], buffers[0].pixels, FRAME_BYTES) == 0);
    assert(memcmp(locked_copy[1], buffers[1].pixels, FRAME_BYTES) == 0);
    mix_present_stats_t after = stats();
    assert(after.bytes_copied == previous.bytes_copied);
    assert(after.present_count == previous.present_count);
    assert(after.failure_count == previous.failure_count);
    assert(after.frame_count == previous.frame_count);
    assert(after.last_us == previous.last_us && after.wait_us == previous.wait_us);
    guards_ok();
}

static void timed_out(void)
{
    assert(mix_present_rect(0, 0, 1024, 768, canvas) == ESP_ERR_TIMEOUT);
    old_front_unchanged();
    assert(stats().bytes_copied == FRAME_BYTES);
    assert(stats().present_count == 0 && stats().failure_count == 1);
    int64_t waited = now_us - wait_started_us;
    assert(waited >= MIX_PRESENT_TIMEOUT_MS * 1000);
    assert(waited <= MIX_PRESENT_TIMEOUT_MS * 1000 + (1000000 + TEST_TICK_HZ - 1) / TEST_TICK_HZ);
    assert(stats().wait_us == (uint32_t)waited);
    stays_locked(ESP_ERR_TIMEOUT);
}

static void test_init(void)
{
    assert(!callback());
    assert(mix_present_get_stats(NULL) == ESP_ERR_INVALID_ARG);
    assert(stats().initialized == 0 && stats().frame_count == 0);
    assert(mix_present_rect(0, 0, 1024, 768, canvas) == ESP_ERR_INVALID_STATE);
    assert(mix_present_init(NULL) == ESP_ERR_INVALID_ARG);
    assert(get_calls == 0);
    init_callbacks = true;
    get_error = ESP_FAIL;
    assert(mix_present_init(&panel) == ESP_FAIL);
    assert(create_calls == 0 && !event_handle);
    get_error = ESP_OK;
    for (invalid_buffer_mode = 1; invalid_buffer_mode <= 3; ++invalid_buffer_mode) {
        assert(mix_present_init(&panel) == ESP_ERR_INVALID_STATE);
        assert(create_calls == 0 && !event_handle);
    }
    invalid_buffer_mode = 0;
    create_failure = true;
    assert(mix_present_init(&panel) == ESP_ERR_NO_MEM);
    assert(!event_handle && !callback() && stats().initialized == 0);
    create_failure = false;
    publish_callback = true;
    assert(mix_present_init(&panel) == ESP_OK);
    assert(stats().initialized == 1 && stats().frame_count == 1);
    assert(give_calls == 1 && woken_calls == 0);
    unsigned gets = get_calls;
    assert(mix_present_init(&panel) == ESP_ERR_INVALID_STATE);
    assert(get_calls == gets && create_calls == 2);
    for (size_t i = 0; i < PIXELS; ++i) {
        assert(buffers[0].pixels[i] == 0x1111);
        assert(buffers[1].pixels[i] == 0x2222);
    }
    assert(stats().bytes_copied == 0 && draw_calls == 0);
    begin_attempt(DELAYED_ACK, 10000); /* Publication's stale token cannot ack. */
    assert(mix_present_rect(0, 0, 1024, 768, canvas) == ESP_OK);
    both_match_canvas();
    assert(stats().wait_us == 10000);
}

static void test_full(bool delayed)
{
    assert(mix_present_init(&panel) == ESP_OK);
    begin_attempt(DELAYED_ACK, delayed ? 130000 : 10000);
    assert(mix_present_rect(0, 0, 1024, 768, canvas) == ESP_OK);
    both_match_canvas();
    mix_present_stats_t st = stats();
    assert(st.present_count == 1 && !st.locked && !st.failure_count && st.last_error == ESP_OK);
    assert(st.bytes_copied == 2 * FRAME_BYTES && st.frame_count == 1);
    assert(st.wait_us == (uint32_t)ack_delay_us && st.total_wait_us == st.wait_us);
    assert(st.last_us == (uint32_t)(ack_delay_us + draw_cost_us));
    assert(st.max_us == st.last_us && st.total_us == st.last_us);
    assert(woken_calls == 1 && take_calls == 1);
}

static void test_partial(void)
{
    initial_full();
    uint64_t expected_bytes = 2 * FRAME_BYTES;
    uint64_t total_wait = 10000;
    uint32_t maximum = 12500;
    const int rects[][4] = {
        {13, 5, 17, 10}, {1023, MIX_PRESENT_HEIGHT - 1, 1024, MIX_PRESENT_HEIGHT},
        {0, 0, 1, 1}, {0, MIX_PRESENT_BODY_END - 1, 1024, MIX_PRESENT_BODY_END},
        {11, MIX_PRESENT_BODY_END - 1, 16, MIX_PRESENT_BODY_END + 1},
        {0, MIX_PRESENT_BODY_END, 1024, MIX_PRESENT_HEIGHT},
        {0, MIX_PRESENT_BODY_Y, 1024, MIX_PRESENT_BODY_END},
        {501, 20, 509, 768}, {0, 0, 1024, 768}
    };
    for (unsigned i = 0; i < 36; ++i) {
        const int *r = rects[i % (sizeof(rects) / sizeof(rects[0]))];
        change_rect(r[0], r[1], r[2], r[3], 11 * i + 5);
        begin_attempt(DELAYED_ACK, 1000 + i * 1000);
        assert(mix_present_rect(r[0], r[1], r[2], r[3], canvas) == ESP_OK);
        both_match_canvas();
        expected_bytes += (uint64_t)(r[2] - r[0]) * (unsigned)(r[3] - r[1]) * 4;
        total_wait += (uint64_t)ack_delay_us;
        mix_present_stats_t st = stats();
        if (st.last_us > maximum) maximum = st.last_us;
        assert(st.bytes_copied == expected_bytes && st.present_count == i + 2);
        assert(st.frame_count == i + 2 && st.max_us == maximum);
        assert(st.total_wait_us == total_wait);
        assert(st.total_us == total_wait + (uint64_t)draw_cost_us * (i + 2));
    }
}

static void check_invalid(void)
{
    const int rects[][4] = {
        {-1, 0, 1, 1}, {0, -1, 1, 1}, {0, 0, 1025, 1}, {0, 0, 1, 769},
        {0, 0, 0, 1}, {0, 0, 1, 0}, {10, 1, 2, 3}, {1, 10, 3, 2},
        {INT_MIN, 0, INT_MAX, 768}, {0, INT_MIN, 1024, INT_MAX},
        {1024, MIX_PRESENT_HEIGHT - 1, 1025, MIX_PRESENT_HEIGHT},
        {0, 768, 1, 769}, {0, 0, -1, 1}
    };
    unsigned calls = draw_calls;
    mix_present_stats_t previous = stats();
    for (unsigned i = 0; i < sizeof(rects) / sizeof(rects[0]); ++i) {
        const int *r = rects[i];
        assert(mix_present_rect(r[0], r[1], r[2], r[3], canvas) == ESP_ERR_INVALID_ARG);
    }
    assert(mix_present_rect(0, 0, 1024, 768, NULL) == ESP_ERR_INVALID_ARG);
    assert(mix_present_rect(0, 0, 1024, 768, buffers[0].pixels) == ESP_ERR_INVALID_ARG);
    assert(mix_present_rect(0, 0, 1024, 768, buffers[1].pixels + 7) == ESP_ERR_INVALID_ARG);
    assert(draw_calls == calls && stats().bytes_copied == previous.bytes_copied);
    assert(stats().present_count == previous.present_count && !stats().locked);
    guards_ok();
}

static void test_invalid(void)
{
    assert(mix_present_init(&panel) == ESP_OK);
    check_invalid();
    assert(mix_present_rect(0, 0, 1024, MIX_PRESENT_HEIGHT - 1, canvas) == ESP_ERR_INVALID_STATE);
    assert(mix_present_rect(0, MIX_PRESENT_BODY_Y, 1024, MIX_PRESENT_BODY_END,
                            canvas) == ESP_ERR_INVALID_STATE);
    assert(mix_present_rect(0, MIX_PRESENT_BODY_END, 1024, MIX_PRESENT_HEIGHT,
                            canvas) == ESP_ERR_INVALID_STATE);
    assert(mix_present_rect(1, 0, 1024, 768, canvas) == ESP_ERR_INVALID_STATE);
    assert(!draw_calls && !stats().bytes_copied);
    begin_attempt(DELAYED_ACK, 10000);
    assert(mix_present_rect(0, 0, 1024, 768, canvas) == ESP_OK);
    check_invalid();
    both_match_canvas();
}

static void test_early(bool duplicate)
{
    assert(mix_present_init(&panel) == ESP_OK);
    emit_frame(false, duplicate ? 5 : 1);
    early_before_select = duplicate ? 5 : 1;
    early_after_select = duplicate ? 5 : 1;
    begin_attempt(NO_ACK, 0);
    timed_out();
    assert(stats().frame_count == (duplicate ? 15u : 3u));
    assert(take_calls == 2); /* One stale token, then one bounded blocking take. */
}

static void test_stale(void)
{
    initial_full();
    emit_frame(false, 3); /* Idle callbacks leave one binary token. */
    change_rect(7, 11, 13, 19, 0x5678);
    begin_attempt(NO_ACK, 0);
    assert(mix_present_rect(7, 11, 13, 19, canvas) == ESP_ERR_TIMEOUT);
    old_front_unchanged();
    assert(stats().present_count == 1);
    assert(stats().bytes_copied == 2 * FRAME_BYTES + (13 - 7) * (19 - 11) * 2);
    stays_locked(ESP_ERR_TIMEOUT);
}

static void test_race(enum plan plan)
{
    assert(mix_present_init(&panel) == ESP_OK);
    future_duplicates = 4;
    begin_attempt(plan, 0);
    assert(mix_present_rect(0, 0, 1024, 768, canvas) == ESP_OK);
    both_match_canvas();
    assert(stats().frame_count == 4 && stats().wait_us == 0);
    assert(woken_calls == 0); /* Arrived before blocking, so ISR must return false. */
    assert(take_calls == (plan == ACK_AT_TAKE ? 1u : 0u));
    early_before_select = 0;
    early_after_select = 0;
    future_duplicates = 1;
    change_rect(0, 0, 1, 1, 33);
    begin_attempt(DELAYED_ACK, 20000); /* Remaining duplicate token cannot ack. */
    assert(mix_present_rect(0, 0, 1, 1, canvas) == ESP_OK);
    both_match_canvas();
    assert(stats().wait_us == 20000 && woken_calls == 1);
}

static void test_error(bool switched)
{
    assert(mix_present_init(&panel) == ESP_OK);
    begin_attempt(NO_ACK, 0);
    draw_error = DRAW_ERROR;
    switch_before_error = switched;
    early_after_select = switched ? 2 : 0;
    assert(mix_present_rect(0, 0, 1024, 768, canvas) == DRAW_ERROR);
    old_front_unchanged();
    assert(stats().bytes_copied == FRAME_BYTES && stats().present_count == 0);
    assert(stats().wait_us == 0 && take_calls == 0);
    assert(stats().last_us == (uint32_t)draw_cost_us);
    stays_locked(DRAW_ERROR);
}

static const mix_present_rect_t batch_separated[] = {
    {7, 11, 13, 19}, {401, 201, 432, 207}, {899, 741, 930, 761}
};

static uint64_t batch_bytes(const mix_present_rect_t *rects, unsigned count)
{
    uint64_t bytes = 0;
    for (unsigned i = 0; i < count; ++i) {
        bytes += (uint64_t)(rects[i].x1 - rects[i].x0) *
                 (unsigned)(rects[i].y1 - rects[i].y0) * sizeof(uint16_t);
    }
    return bytes; /* Sum, deliberately NOT the union of overlapping pixels. */
}

static void batch_reference(const mix_present_rect_t *rects, unsigned count)
{
    memcpy(expected_fade, buffers[scanning].pixels, FRAME_BYTES);
    for (unsigned i = 0; i < count; ++i) {
        for (int y = rects[i].y0; y < rects[i].y1; ++y) {
            for (int x = rects[i].x0; x < rects[i].x1; ++x) {
                size_t p = (size_t)y * MIX_PRESENT_WIDTH + x;
                expected_fade[p] = canvas[p];
            }
        }
    }
    expected_image = expected_fade;
}

static void checked_batch(const mix_present_rect_t *rects, unsigned count,
                          enum plan plan, int64_t delay)
{
    batch_reference(rects, count);
    mix_present_stats_t before = stats();
    unsigned draws = draw_calls, takes = take_calls;
    begin_attempt(plan, delay);
    assert(mix_present_rects(rects, count, canvas) == ESP_OK);
    assert(draw_calls == draws + 1);
    assert(take_calls - takes <= 2); /* A stale token may add one wake, not a wait. */
    assert(memcmp(buffers[0].pixels, expected_image, FRAME_BYTES) == 0);
    assert(memcmp(buffers[1].pixels, expected_image, FRAME_BYTES) == 0);
    assert(scanning == expected_back && selected == expected_back);
    mix_present_stats_t after = stats();
    uint32_t waited = plan == DELAYED_ACK ? (uint32_t)delay : 0;
    assert(after.wait_us == waited && after.total_wait_us == before.total_wait_us + waited);
    assert(after.last_us == waited + (uint32_t)draw_cost_us);
    assert(after.total_us == before.total_us + after.last_us);
    assert(after.max_us == (before.max_us > after.last_us ? before.max_us : after.last_us));
    assert(after.present_count == before.present_count + 1);
    assert(after.failure_count == before.failure_count && !after.locked);
    assert(after.frame_count == before.frame_count + future_duplicates);
    assert(after.bytes_copied == before.bytes_copied + 2 * batch_bytes(rects, count));
    assert(after.pixels_blended == before.pixels_blended && after.last_error == ESP_OK);
    guards_ok();
}

static void test_batch_regions(const char *name)
{
    const mix_present_rect_t small[] = {{13, 5, 17, 10}, {19, 6, 21, 9}};
    const mix_present_rect_t overlap[] = {
        {10, 10, 30, 30}, {15, 20, 40, 40}, {10, 10, 30, 30}, {18, 21, 19, 22}
    };
    const mix_present_rect_t edges[] = {
        {0, 0, 1024, 1}, {0, 767, 1024, 768}, {0, 1, 1, 767}, {1023, 1, 1024, 767},
        {11, MIX_PRESENT_BODY_END - 1, 16, MIX_PRESENT_BODY_END + 1}
    };
    const mix_present_rect_t maximum[MIX_PRESENT_MAX_RECTS] = {
        {0, 0, 1, 1}, {1023, 0, 1024, 1}, {0, 767, 1, 768}, {1023, 767, 1024, 768},
        {40, 90, 80, 100}, {70, 99, 82, 200}, {500, 648, 540, 650}, {501, 20, 509, 768}
    };
    const mix_present_rect_t *rects = batch_separated;
    unsigned count = 3;
    if (!strcmp(name, "batch_small")) { rects = small; count = 2; }
    if (!strcmp(name, "batch_overlap")) { rects = overlap; count = 4; }
    if (!strcmp(name, "batch_edges")) { rects = edges; count = 5; }
    if (!strcmp(name, "batch_max")) { rects = maximum; count = MIX_PRESENT_MAX_RECTS; }
    initial_full();
    for (unsigned n = 0; n < 12; ++n) {
        /* Poison even the UNREQUESTED canvas pixels: a bounding-box/full-frame
         * copy must fail, even if both resulting driver images were equal. */
        change_rect(0, 0, 1024, 768, 0x1111u + n * 37u);
        unsigned takes = take_calls;
        checked_batch(rects, count, DELAYED_ACK, 1000 + n * 11000);
        assert(take_calls == takes + 1); /* Exactly one real frame wait per batch. */
    }
}

static void same_stats(mix_present_stats_t a, mix_present_stats_t b)
{
    assert(a.bytes_copied == b.bytes_copied && a.pixels_blended == b.pixels_blended);
    assert(a.total_us == b.total_us && a.total_wait_us == b.total_wait_us);
    assert(a.present_count == b.present_count && a.failure_count == b.failure_count);
    assert(a.last_us == b.last_us && a.max_us == b.max_us && a.wait_us == b.wait_us);
    assert(a.frame_count == b.frame_count && a.initialized == b.initialized);
    assert(a.locked == b.locked && a.last_error == b.last_error);
}

typedef esp_err_t (*present_batch_fn)(const mix_present_rect_t *, unsigned,
                                      const uint16_t *);
typedef void (*reject_batch_fn)(const mix_present_rect_t *, unsigned,
                                const uint16_t *, esp_err_t);

static void rejected_via(present_batch_fn present,
                         const mix_present_rect_t *rects, unsigned count,
                         const uint16_t *source, esp_err_t error)
{
    unsigned draws = draw_calls, takes = take_calls;
    unsigned old_selected = selected, old_scanning = scanning;
    mix_present_stats_t before = stats();
    memcpy(locked_copy[0], buffers[0].pixels, FRAME_BYTES);
    memcpy(locked_copy[1], buffers[1].pixels, FRAME_BYTES);
    assert(present(rects, count, source) == error);
    assert(memcmp(locked_copy[0], buffers[0].pixels, FRAME_BYTES) == 0);
    assert(memcmp(locked_copy[1], buffers[1].pixels, FRAME_BYTES) == 0);
    assert(draw_calls == draws && take_calls == takes);
    assert(selected == old_selected && scanning == old_scanning);
    same_stats(before, stats());
    guards_ok();
}

static void rejected_batch(const mix_present_rect_t *rects, unsigned count,
                           const uint16_t *source, esp_err_t error)
{
    rejected_via(mix_present_rects, rects, count, source, error);
}

static void rejected_deferred(const mix_present_rect_t *rects, unsigned count,
                              const uint16_t *source, esp_err_t error)
{
    rejected_via(mix_present_rects_deferred, rects, count, source, error);
}

static void check_batch_invalid_via(reject_batch_fn reject)
{
    mix_present_rect_t rects[MIX_PRESENT_MAX_RECTS + 1];
    for (unsigned i = 0; i < MIX_PRESENT_MAX_RECTS + 1; ++i) rects[i] = batch_separated[i % 3];
    reject(rects, 0, canvas, ESP_ERR_INVALID_ARG);
    reject(rects, MIX_PRESENT_MAX_RECTS + 1, canvas, ESP_ERR_INVALID_ARG);
    reject(rects, UINT_MAX, canvas, ESP_ERR_INVALID_ARG);
    reject(NULL, 1, canvas, ESP_ERR_INVALID_ARG);
    reject(NULL, 0, NULL, ESP_ERR_INVALID_ARG);
    reject(rects, 3, NULL, ESP_ERR_INVALID_ARG);
    const mix_present_rect_t invalid[] = {
        {-1, 0, 1, 1}, {0, -1, 1, 1}, {0, 0, 1025, 1}, {0, 0, 1, 769},
        {0, 0, 0, 1}, {0, 0, 1, 0}, {10, 1, 2, 3}, {1, 10, 3, 2},
        {INT_MIN, 0, INT_MAX, 768}, {0, INT_MIN, 1024, INT_MAX},
        {1024, 767, 1025, 768}, {0, 768, 1, 769}, {0, 0, -1, 1}, {0, 0, 1, -1}
    };
    /* Middle and last bad descriptors cannot leave even the valid prefix in
     * back. Canvas differs from BOTH driver buffers before these checks. */
    for (unsigned bad_at = 0; bad_at < MIX_PRESENT_MAX_RECTS; ++bad_at) {
        mix_present_rect_t saved = rects[bad_at];
        for (unsigned i = 0; i < sizeof(invalid) / sizeof(invalid[0]); ++i) {
            rects[bad_at] = invalid[i];
            reject(rects, MIX_PRESENT_MAX_RECTS, canvas, ESP_ERR_INVALID_ARG);
        }
        rects[bad_at] = saved;
    }
    const uint16_t *invalid_canvas[] = {
        buffers[0].pixels, buffers[1].pixels, buffers[0].pixels + 7,
        buffers[1].pixels + PIXELS - 1,
        (const uint16_t *)((uintptr_t)buffers[0].pixels - 2),
        (const uint16_t *)((const uint8_t *)canvas + 1),
        (const uint16_t *)(UINTPTR_MAX - 1)
    };
    for (unsigned i = 0; i < sizeof(invalid_canvas) / sizeof(invalid_canvas[0]); ++i) {
        reject(rects, 3, invalid_canvas[i], ESP_ERR_INVALID_ARG);
    }
    const mix_present_rect_t *invalid_rects[] = {
        (const mix_present_rect_t *)(const void *)buffers[0].pixels,
        (const mix_present_rect_t *)(const void *)buffers[1].pixels,
        (const mix_present_rect_t *)((uintptr_t)buffers[0].pixels - sizeof(int)),
        (const mix_present_rect_t *)((uintptr_t)buffers[1].pixels + FRAME_BYTES - sizeof(int)),
        (const mix_present_rect_t *)((const uint8_t *)rects + 1),
        (const mix_present_rect_t *)(UINTPTR_MAX & ~(uintptr_t)(_Alignof(mix_present_rect_t) - 1))
    };
    for (unsigned i = 0; i < sizeof(invalid_rects) / sizeof(invalid_rects[0]); ++i) {
        reject(invalid_rects[i], 3, canvas, ESP_ERR_INVALID_ARG);
    }
}

static void check_batch_invalid(void)
{
    check_batch_invalid_via(rejected_batch);
}

static void test_batch_invalid(void)
{
    assert(mix_present_init(&panel) == ESP_OK);
    check_batch_invalid();
    const mix_present_rect_t full = {0, 0, 1024, 768};
    checked_batch(&full, 1, DELAYED_ACK, 10000);
    change_rect(0, 0, 1024, 768, 0x4531);
    check_batch_invalid();
    checked_batch(batch_separated, 3, DELAYED_ACK, 10000); /* Rejection never locks. */
}

static void test_batch_first(void)
{
    const mix_present_rect_t full = {0, 0, 1024, 768};
    rejected_batch(&full, 1, canvas, ESP_ERR_INVALID_STATE);
    assert(mix_present_init(&panel) == ESP_OK);
    const mix_present_rect_t tiled[] = {{0, 0, 1024, 384}, {0, 384, 1024, 768}};
    const mix_present_rect_t full_plus[] = {{0, 0, 1024, 768}, {7, 11, 13, 19}};
    const mix_present_rect_t full_twice[] = {{0, 0, 1024, 768}, {0, 0, 1024, 768}};
    const mix_present_rect_t bad_middle[] = {{0, 0, 1024, 768}, {1, 1, 1, 2}, {0, 0, 1, 1}};
    rejected_batch(batch_separated, 1, canvas, ESP_ERR_INVALID_STATE);
    rejected_batch(batch_separated, 3, canvas, ESP_ERR_INVALID_STATE);
    rejected_batch(tiled, 2, canvas, ESP_ERR_INVALID_STATE);
    rejected_batch(full_plus, 2, canvas, ESP_ERR_INVALID_STATE);
    rejected_batch(full_twice, 2, canvas, ESP_ERR_INVALID_STATE);
    rejected_batch(bad_middle, 3, canvas, ESP_ERR_INVALID_ARG);
    checked_batch(&full, 1, DELAYED_ACK, 10000);
    both_match_canvas();
    change_rect(0, 0, 1024, 768, 0x5821);
    checked_batch(tiled, 2, DELAYED_ACK, 10000); /* Legal only AFTER first full. */
    both_match_canvas();
}

static void test_batch_fault(const char *name)
{
    initial_full();
    change_rect(0, 0, 1024, 768, 0x7653);
    batch_reference(batch_separated, 3);
    mix_present_stats_t before = stats();
    unsigned draws = draw_calls, takes = take_calls;
    enum plan plan = NO_ACK;
    int64_t delay = 0;
    if (!strcmp(name, "batch_spurious")) plan = SPURIOUS;
    if (!strcmp(name, "batch_deadline")) { plan = DELAYED_ACK; delay = 150000; }
    if (!strcmp(name, "batch_late")) { plan = DELAYED_ACK; delay = 160000; }
    if (!strcmp(name, "batch_early")) {
        emit_frame(false, 3);
        early_before_select = 4;
        early_after_select = 5;
    }
    if (!strcmp(name, "batch_stale")) {
        /* Leave a token from a successful batch, not just idle callbacks. */
        future_duplicates = 4;
        checked_batch(batch_separated, 3, ACK_AT_BASELINE_EXIT, 0);
        future_duplicates = 1;
        change_rect(0, 0, 1024, 768, 0x8213);
        batch_reference(batch_separated, 3);
        before = stats();
        draws = draw_calls;
        takes = take_calls;
    }
    bool error = strstr(name, "draw_error") != NULL;
    begin_attempt(plan, delay);
    if (error) {
        draw_error = DRAW_ERROR;
        switch_before_error = strstr(name, "switched") != NULL;
        early_after_select = switch_before_error ? 2 : 0;
    }
    esp_err_t expected_error = error ? DRAW_ERROR : ESP_ERR_TIMEOUT;
    assert(mix_present_rects(batch_separated, 3, canvas) == expected_error);
    /* Even an on-time driver handoff coincident with the expired deadline
     * must NOT cause a mirror, regardless of protect_front being released. */
    assert(memcmp(old_front, buffers[protected_index].pixels, FRAME_BYTES) == 0);
    old_front_unchanged();
    assert(memcmp(buffers[expected_back].pixels, expected_image, FRAME_BYTES) == 0);
    assert(draw_calls == draws + 1);
    assert(stats().bytes_copied == before.bytes_copied + batch_bytes(batch_separated, 3));
    assert(stats().present_count == before.present_count);
    assert(stats().pixels_blended == before.pixels_blended);
    assert(stats().failure_count == 1);
    if (error) {
        assert(stats().wait_us == 0 && takes == take_calls);
        assert(stats().last_us == (uint32_t)draw_cost_us);
    } else {
        assert(stats().wait_us >= 150000);
        assert(stats().wait_us <= 150000 + (1000000 + TEST_TICK_HZ - 1) / TEST_TICK_HZ);
        assert(take_calls - takes <= 10);
        if (!strcmp(name, "batch_early") || !strcmp(name, "batch_stale")) {
            assert(take_calls == takes + 2);
        }
    }
    stays_locked(expected_error);
}

static void test_batch_race(enum plan plan)
{
    initial_full();
    change_rect(0, 0, 1024, 768, 0x8937);
    future_duplicates = 4;
    checked_batch(batch_separated, 3, plan, 0);
    future_duplicates = 1;
    change_rect(0, 0, 1024, 768, 0x7619);
    checked_batch(batch_separated, 3, DELAYED_ACK, 20000);
}

/* Independent per-channel reference; deliberately uses per-pixel division. */
static uint16_t reference_pixel(uint16_t target, uint16_t background, unsigned a)
{
    const unsigned shifts[] = {0, 5, 11}, masks[] = {31, 63, 31};
    uint16_t result = 0;
    for (unsigned c = 0; c < 3; ++c) {
        unsigned t = (target >> shifts[c]) & masks[c];
        unsigned b = (background >> shifts[c]) & masks[c];
        result |= (uint16_t)(((t * a + b * (255 - a) + 127) / 255) << shifts[c]);
    }
    return result;
}

static void fade_reference(unsigned opacity)
{
    /* Bottom navigation/status comes from the already-presented image.
     * Only body pixels are part of the independent fade reference. */
    memcpy(expected_fade, buffers[scanning].pixels, FRAME_BYTES);
    for (size_t i = BODY_FIRST_PIXEL; i < BODY_END_PIXEL; ++i) {
        expected_fade[i] = reference_pixel(canvas[i], fade_background, opacity);
    }
    expected_image = expected_fade;
}

static uint32_t count_spans(void)
{
    uint32_t count = 0;
    for (unsigned y = MIX_PRESENT_BODY_Y; y < MIX_PRESENT_BODY_END; ++y) {
        unsigned first = 0, last = MIX_PRESENT_WIDTH;
        while (first < last && canvas[(size_t)y * 1024 + first] == fade_background) ++first;
        while (last > first && canvas[(size_t)y * 1024 + last - 1] == fade_background) --last;
        count += last - first;
    }
    return count;
}

static void fill_body(uint16_t color)
{
    for (size_t i = BODY_FIRST_PIXEL; i < BODY_END_PIXEL; ++i) canvas[i] = color;
}

static void sparse_target(void)
{
    /* First/last body rows, both x edges, and a full-width span whose middle
     * is all background. All channel values appear in the 64-pixel ramp. */
    canvas[BODY_FIRST_PIXEL] = fade_background ^ 0xffffu;
    canvas[BODY_FIRST_PIXEL + MIX_PRESENT_WIDTH - 1] = fade_background ^ 0xf81fu;
    for (unsigned x = 0; x < 64; ++x) {
        canvas[61 * 1024 + 93 + x] = (uint16_t)(((x & 31u) << 11) | (x << 5) | (31u - (x & 31u)));
    }
    canvas[400 * 1024 + 1] = 0;
    canvas[400 * 1024 + 10] = 0xffff;
    canvas[BODY_END_PIXEL - 1] = fade_background ^ 0x07e0u;
    for (unsigned y = 100; y < 120; ++y) canvas[y * 1024 + y] = fade_background ^ 1u;
}

static void checked_fade_begin(void)
{
    unsigned draws = draw_calls, takes = take_calls;
    mix_present_stats_t before = stats();
    memcpy(locked_copy[0], buffers[0].pixels, FRAME_BYTES);
    memcpy(locked_copy[1], buffers[1].pixels, FRAME_BYTES);
    assert(mix_present_fade_begin(canvas, fade_background) == ESP_OK);
    assert(memcmp(locked_copy[0], buffers[0].pixels, FRAME_BYTES) == 0);
    assert(memcmp(locked_copy[1], buffers[1].pixels, FRAME_BYTES) == 0);
    assert(draws == draw_calls && takes == take_calls);
    assert(stats().bytes_copied == before.bytes_copied);
    assert(stats().present_count == before.present_count);
    fade_pixels = count_spans();
}

static void prepare_fade(uint16_t background, bool empty, bool dense)
{
    fade_background = background;
    fill_body(background);
    initial_full();
    if (dense) {
        for (size_t i = BODY_FIRST_PIXEL; i < BODY_END_PIXEL; ++i) canvas[i] = (uint16_t)i;
    } else if (!empty) sparse_target();
    checked_fade_begin();
}

static void bottom_nav_unchanged(void)
{
    for (unsigned b = 0; b < 2; ++b) {
        assert(memcmp(buffers[b].pixels + BODY_END_PIXEL,
                      old_front + BODY_END_PIXEL, BOTTOM_NAV_BYTES) == 0);
    }
}

static void checked_fade_step(uint8_t opacity, enum plan plan)
{
    fade_reference(opacity);
    mix_present_stats_t before = stats();
    unsigned draws = draw_calls;
    begin_attempt(plan, 10000);
    assert(mix_present_fade_step(opacity) == ESP_OK);
    bottom_nav_unchanged();
    assert(draw_calls == draws + 1); /* All spans share ONE submit. */
    assert(memcmp(buffers[0].pixels, expected_fade, FRAME_BYTES) == 0);
    assert(memcmp(buffers[1].pixels, expected_fade, FRAME_BYTES) == 0);
    assert(scanning == expected_back && selected == expected_back);
    assert(stats().bytes_copied == before.bytes_copied + 4 * (uint64_t)fade_pixels);
    assert(stats().pixels_blended == before.pixels_blended +
           (opacity && opacity < 255 ? fade_pixels : 0));
    assert(stats().present_count == before.present_count + 1);
    guards_ok();
}

static void test_fade_accuracy(uint16_t background, bool dense)
{
    prepare_fade(background, false, dense);
    assert(fade_pixels > 0 && fade_pixels <= BODY_PIXELS);
    if (dense) {
        assert(fade_pixels == BODY_PIXELS);
        const uint8_t levels[] = {0, 1, 32, 63, 127, 128, 200, 254, 255, 0, 255};
        for (unsigned n = 0; n < sizeof(levels); ++n) checked_fade_step(levels[n], DELAYED_ACK);
    } else {
        for (unsigned a = 0; a <= 255; ++a) {
            checked_fade_step((uint8_t)a, DELAYED_ACK);
            /* old_front is still the previous step's image. Check monotonic
             * motion in either direction relative to each nonblack channel. */
            for (unsigned y = MIX_PRESENT_BODY_Y; y < MIX_PRESENT_BODY_END; ++y) {
                for (unsigned x = 0; x < 1024; ++x) {
                    size_t i = (size_t)y * 1024 + x;
                    if (canvas[i] == background) continue;
                    const unsigned shifts[] = {0, 5, 11}, masks[] = {31, 63, 31};
                    for (unsigned c = 0; c < 3; ++c) {
                        unsigned prev = (old_front[i] >> shifts[c]) & masks[c];
                        unsigned cur = (expected_fade[i] >> shifts[c]) & masks[c];
                        unsigned target = (canvas[i] >> shifts[c]) & masks[c];
                        unsigned bg = (background >> shifts[c]) & masks[c];
                        assert(target >= bg ? cur >= prev : cur <= prev);
                    }
                }
            }
        }
        checked_fade_step(128, DELAYED_ACK); /* Always derive from original. */
        checked_fade_step(128, DELAYED_ACK); /* Repeated opacity is stable. */
        checked_fade_step(0, DELAYED_ACK);   /* Erase previously lit spans. */
        checked_fade_step(255, DELAYED_ACK);
    }
    both_match_canvas();
    unsigned draws = draw_calls;
    mix_present_fade_end();
    assert(mix_present_fade_step(255) == ESP_ERR_INVALID_STATE);
    assert(draw_calls == draws);
}

static void test_fade_handoff(void)
{
    fade_background = 0x39e7;
    fill_body(fade_background);
    sparse_target();
    initial_full(); /* Begin OUT on a normally presented scene. */
    checked_fade_begin();
    checked_fade_step(180, DELAYED_ACK);
    checked_fade_step(70, DELAYED_ACK);
    checked_fade_step(0, DELAYED_ACK);
    mix_present_fade_end();
    unsigned draws = draw_calls;
    fill_body(fade_background);
    /* Incoming spans have no overlap with most outgoing ink. Nothing from
     * the outgoing scene may survive; no full present establishes equality. */
    canvas[600 * 1024 + 700] = 0xffff;
    canvas[50 * 1024 + 300] = 0;
    checked_fade_begin();
    assert(draw_calls == draws);
    checked_fade_step(80, DELAYED_ACK);
    checked_fade_step(200, DELAYED_ACK);
    checked_fade_step(255, DELAYED_ACK);
    both_match_canvas();
    mix_present_fade_end();
}

static void test_fade_empty(void)
{
    prepare_fade(0x5b29, true, false);
    assert(fade_pixels == 0);
    mix_present_stats_t before = stats();
    unsigned draws = draw_calls, takes = take_calls;
    for (unsigned a = 0; a <= 255; ++a) assert(mix_present_fade_step((uint8_t)a) == ESP_OK);
    both_match_canvas();
    assert(draws == draw_calls && takes == take_calls);
    assert(stats().bytes_copied == before.bytes_copied && stats().pixels_blended == 0);
    assert(stats().present_count == before.present_count && stats().last_us == before.last_us);
    mix_present_fade_end();
    assert(mix_present_fade_step(0) == ESP_ERR_INVALID_STATE);
}

static void test_fade_invalid(void)
{
    mix_present_fade_end();
    assert(mix_present_fade_begin(canvas, 0) == ESP_ERR_INVALID_STATE);
    assert(mix_present_fade_step(0) == ESP_ERR_INVALID_STATE);
    assert(mix_present_init(&panel) == ESP_OK);
    const uint16_t *invalid[] = {
        NULL, buffers[0].pixels, buffers[1].pixels + 7,
        (const uint16_t *)((const uint8_t *)canvas + 1),
        (const uint16_t *)(UINTPTR_MAX - 1),
        (const uint16_t *)((uintptr_t)buffers[0].pixels - 2)
    };
    for (unsigned i = 0; i < sizeof(invalid) / sizeof(invalid[0]); ++i) {
        assert(mix_present_fade_begin(invalid[i], 0) == ESP_ERR_INVALID_ARG);
    }
    assert(mix_present_fade_begin(canvas, 0) == ESP_ERR_INVALID_STATE);
    assert(mix_present_rect(0, MIX_PRESENT_BODY_Y, 1024, MIX_PRESENT_BODY_END, canvas) == ESP_ERR_INVALID_STATE);
    assert(!draw_calls && !stats().bytes_copied);
    fade_background = 0x73a5;
    fill_body(fade_background);
    begin_attempt(DELAYED_ACK, 10000);
    assert(mix_present_rect(0, 0, 1024, 768, canvas) == ESP_OK);
    sparse_target();
    checked_fade_begin();
    assert(mix_present_fade_begin(canvas, 0xffff) == ESP_ERR_INVALID_STATE);
    assert(mix_present_fade_begin(NULL, 0) == ESP_ERR_INVALID_ARG);
    checked_fade_step(128, DELAYED_ACK); /* Rejected begin retains old source/bg. */
    assert(!stats().locked);
}

static void test_fade_cancel(void)
{
    prepare_fade(0x7392, false, false);
    checked_fade_step(128, DELAYED_ACK);
    unsigned draws = draw_calls;
    mix_present_fade_end();
    mix_present_fade_end();
    assert(mix_present_fade_step(0) == ESP_ERR_INVALID_STATE);
    assert(draw_calls == draws);
    assert(memcmp(buffers[0].pixels, expected_fade, FRAME_BYTES) == 0);
    assert(memcmp(buffers[1].pixels, expected_fade, FRAME_BYTES) == 0);

    /* Re-establish the required shared background with a BODY-only normal
     * present: first-full was already established; bottom nav stays valid. */
    fill_body(fade_background);
    expected_image = canvas;
    begin_attempt(DELAYED_ACK, 10000);
    assert(mix_present_rect(0, MIX_PRESENT_BODY_Y, 1024, MIX_PRESENT_BODY_END, canvas) == ESP_OK);
    sparse_target();
    checked_fade_begin();
    assert(mix_present_rect(0, 0, 0, 1, NULL) == ESP_ERR_INVALID_ARG);
    assert(mix_present_fade_step(255) == ESP_ERR_INVALID_STATE);
    checked_fade_begin(); /* No fade step ran, so shared background is intact. */
    expected_image = canvas;
    begin_attempt(DELAYED_ACK, 10000);
    assert(mix_present_rect(0, MIX_PRESENT_BODY_Y, 1024, MIX_PRESENT_BODY_END, canvas) == ESP_OK);
    both_match_canvas();
    assert(mix_present_fade_step(0) == ESP_ERR_INVALID_STATE);
    change_rect(5, MIX_PRESENT_BODY_END + 1, 9, MIX_PRESENT_BODY_END + 5, 0x4321);
    begin_attempt(DELAYED_ACK, 10000);
    assert(mix_present_rect(5, MIX_PRESENT_BODY_END + 1, 9,
                            MIX_PRESENT_BODY_END + 5, canvas) == ESP_OK);
    both_match_canvas();
}

static void test_batch_fade_cancel(void)
{
    prepare_fade(0x7392, false, false);
    const mix_present_rect_t invalid_middle[] = {
        {0, 0, 1, 1}, {1, 1, 1, 2}, {1023, 767, 1024, 768}
    };
    for (unsigned n = 0; n < 9; ++n) {
        const mix_present_rect_t *rects = batch_separated;
        const uint16_t *source = canvas;
        unsigned count = 3;
        switch (n) {
        case 0: rects = NULL; break;
        case 1: count = 0; break;
        case 2: count = MIX_PRESENT_MAX_RECTS + 1; break;
        case 3: source = NULL; break;
        case 4: rects = invalid_middle; break;
        case 5: source = buffers[0].pixels; break;
        case 6: source = (const uint16_t *)((const uint8_t *)canvas + 1); break;
        case 7: rects = (const mix_present_rect_t *)(const void *)buffers[1].pixels; break;
        case 8: rects = (const mix_present_rect_t *)((const uint8_t *)batch_separated + 1); break;
        }
        rejected_batch(rects, count, source, ESP_ERR_INVALID_ARG);
        assert(mix_present_fade_step(255) == ESP_ERR_INVALID_STATE);
        checked_fade_begin(); /* Rejection preserves the shared background. */
    }
    checked_fade_step(128, DELAYED_ACK);
    /* A normal multi-rectangle update may leave the faded pixels outside its
     * regions intact, but must drop the borrowed fade source immediately. */
    checked_batch(batch_separated, 3, DELAYED_ACK, 10000);
    unsigned draws = draw_calls, takes = take_calls;
    mix_present_stats_t before = stats();
    assert(mix_present_fade_step(0) == ESP_ERR_INVALID_STATE);
    mix_present_fade_end();
    mix_present_fade_end();
    assert(draw_calls == draws && take_calls == takes);
    same_stats(before, stats());
    assert(memcmp(buffers[0].pixels, expected_image, FRAME_BYTES) == 0);
    assert(memcmp(buffers[1].pixels, expected_image, FRAME_BYTES) == 0);
}

static void test_fade_spans(void)
{
    prepare_fade(0x4a69, false, false);
    assert(fade_pixels < 2048); /* Wide separated content only expands its row. */
    checked_fade_step(73, DELAYED_ACK);
    checked_fade_step(255, DELAYED_ACK);
    checked_fade_step(0, DELAYED_ACK);
    for (unsigned b = 0; b < 2; ++b) {
        for (size_t i = BODY_END_PIXEL; i < PIXELS; ++i) {
            assert(buffers[b].pixels[i] == canvas[i]);
        }
    }
}

static void test_fade_body_edges(void)
{
    fade_background = 0x39e7;
    fill_body(fade_background);
    /* Every bottom-nav pixel differs from background, including y=648/767.
     * None may enter the spans or be touched by either back writes or mirror. */
    for (size_t i = BODY_END_PIXEL; i < PIXELS; ++i) {
        canvas[i] = (uint16_t)(fade_background ^ (0x8000u | (i & 0x7fffu)));
    }
    initial_full();
    canvas[BODY_FIRST_PIXEL] = 0xffff;
    canvas[BODY_FIRST_PIXEL + MIX_PRESENT_WIDTH - 1] = 0;
    canvas[BODY_END_PIXEL - MIX_PRESENT_WIDTH] = 0xf800;
    canvas[BODY_END_PIXEL - 1] = 0x07e0;
    checked_fade_begin();
    assert(fade_pixels == 2 * MIX_PRESENT_WIDTH);
    const uint8_t levels[] = {0, 1, 127, 254, 255, 0, 255};
    for (unsigned n = 0; n < sizeof(levels); ++n) checked_fade_step(levels[n], DELAYED_ACK);
    both_match_canvas();
    mix_present_fade_end();
}

static void test_fade_begin_end(void)
{
    prepare_fade(0x6db6, false, false);
    unsigned draws = draw_calls, takes = take_calls;
    mix_present_fade_end();
    assert(memcmp(locked_copy[0], buffers[0].pixels, FRAME_BYTES) == 0);
    assert(memcmp(locked_copy[1], buffers[1].pixels, FRAME_BYTES) == 0);
    change_rect(0, MIX_PRESENT_BODY_Y, 1024, MIX_PRESENT_BODY_END, 0xabcd); /* Freed canvas can be reused. */
    assert(mix_present_fade_step(255) == ESP_ERR_INVALID_STATE);
    assert(draws == draw_calls && takes == take_calls);
}

static void test_fade_fault(const char *name)
{
    prepare_fade(0x738e, false, false);
    if (!strcmp(name, "fade_stale")) {
        checked_fade_step(100, DELAYED_ACK);
        emit_frame(false, 3);
    }
    mix_present_stats_t before = stats();
    unsigned draws = draw_calls, takes = take_calls;
    fade_reference(200);
    enum plan plan = NO_ACK;
    int64_t delay = 0;
    if (!strcmp(name, "fade_spurious")) plan = SPURIOUS;
    if (!strcmp(name, "fade_deadline")) { plan = DELAYED_ACK; delay = 150000; }
    if (!strcmp(name, "fade_late")) { plan = DELAYED_ACK; delay = 160000; }
    if (!strcmp(name, "fade_early")) {
        emit_frame(false, 3);
        early_before_select = 4;
        early_after_select = 5;
    }
    bool error = strstr(name, "draw_error") != NULL;
    begin_attempt(plan, delay);
    if (error) {
        draw_error = DRAW_ERROR;
        switch_before_error = strstr(name, "switched") != NULL;
        early_after_select = switch_before_error ? 2 : 0;
    }
    esp_err_t expected_error = error ? DRAW_ERROR : ESP_ERR_TIMEOUT;
    assert(mix_present_fade_step(200) == expected_error);
    old_front_unchanged();
    bottom_nav_unchanged();
    assert(draw_calls == draws + 1);
    assert(stats().bytes_copied == before.bytes_copied + 2 * (uint64_t)fade_pixels);
    assert(stats().pixels_blended == before.pixels_blended + fade_pixels);
    assert(stats().present_count == before.present_count);
    if (error) {
        assert(stats().wait_us == 0 && take_calls == takes);
    } else {
        assert(stats().wait_us >= 150000);
        assert(stats().wait_us <= 150000 + (1000000 + TEST_TICK_HZ - 1) / TEST_TICK_HZ);
        assert(take_calls - takes <= 10);
    }
    stays_locked(expected_error);
}

static void test_fade_race(enum plan plan)
{
    prepare_fade(0x6495, false, false);
    future_duplicates = 4;
    checked_fade_step(100, plan);
    assert(stats().wait_us == 0);
    future_duplicates = 1;
    checked_fade_step(200, DELAYED_ACK);
    assert(stats().wait_us == 10000);
}

static const mix_present_rect_t deferred_full = {0, 0, 1024, 768};
static const mix_present_rect_t deferred_body = {0, 0, 1024, MIX_PRESENT_BODY_END};
static const mix_present_rect_t deferred_footer = {71, 710, 211, 731};
#define ANY_COPY_BYTES UINT64_MAX

static void start_deferred_model(void)
{
    expected_image = canvas;
    memcpy(model_screen, buffers[scanning].pixels, FRAME_BYTES);
    memset(model_stale, 0, sizeof(model_stale));
    check_deferred_handoff = true;
}

static void mark_rects(uint8_t *mask, const mix_present_rect_t *rects, unsigned count)
{
    for (unsigned n = 0; n < count; ++n) {
        for (int y = rects[n].y0; y < rects[n].y1; ++y) {
            memset(mask + (size_t)y * MIX_PRESENT_WIDTH + rects[n].x0, 1,
                   (size_t)(rects[n].x1 - rects[n].x0));
        }
    }
}

static void change_batch(const mix_present_rect_t *rects, unsigned count, unsigned seed)
{
    for (unsigned n = 0; n < count; ++n) {
        change_rect(rects[n].x0, rects[n].y0, rects[n].x1, rects[n].y1, seed + n * 37u);
    }
}

/* Only change pixels named by the new batch before invoking this helper.
 * The oracle never reuses a possibly stale driver image as the new target. */
static uint64_t checked_deferred_update(const mix_present_rect_t *rects, unsigned count,
                                        bool deferred, uint64_t copy_bytes,
                                        enum plan plan, int64_t delay)
{
    assert(check_deferred_handoff && expected_image == canvas);
    assert(count && count <= MIX_PRESENT_MAX_RECTS);
    assert(memcmp(buffers[scanning].pixels, model_screen, FRAME_BYTES) == 0);
    memcpy(model_needed, model_stale, sizeof(model_needed));
    mark_rects(model_needed, rects, count);
    uint64_t required_bytes = 0;
    for (size_t p = 0; p < PIXELS; ++p) required_bytes += model_needed[p] * sizeof(uint16_t);
    memcpy(expected_fade, canvas, FRAME_BYTES); /* The source must stay read-only. */
    mix_present_rect_t saved[MIX_PRESENT_MAX_RECTS];
    memcpy(saved, rects, count * sizeof(*rects));
    mix_present_stats_t before = stats();
    unsigned draws = draw_calls, takes = take_calls;
    begin_attempt(plan, delay);
    esp_err_t result;
    if (deferred) {
        result = mix_present_rects_deferred(rects, count, canvas);
    } else if (count == 1) {
        /* Exercise the public single-rectangle wrapper while pending exists. */
        result = mix_present_rect(rects[0].x0, rects[0].y0, rects[0].x1, rects[0].y1, canvas);
    } else {
        result = mix_present_rects(rects, count, canvas);
    }
    assert(result == ESP_OK);
    assert(!protect_front && draw_calls == draws + 1);
    assert(scanning == expected_back && selected == expected_back);
    assert(memcmp(buffers[scanning].pixels, canvas, FRAME_BYTES) == 0);
    assert(memcmp(canvas, expected_fade, FRAME_BYTES) == 0);
    assert(memcmp(rects, saved, count * sizeof(*rects)) == 0);
    memset(model_stale, 0, sizeof(model_stale));
    if (deferred) {
        /* No mirror even AFTER ack: the whole released front stays unchanged. */
        assert(memcmp(buffers[protected_index].pixels, old_front, FRAME_BYTES) == 0);
        mark_rects(model_stale, rects, count);
    } else {
        both_match_canvas();
    }
    for (size_t p = 0; p < PIXELS; ++p) {
        if (!model_stale[p]) assert(buffers[scanning ^ 1u].pixels[p] == canvas[p]);
    }
    memcpy(model_screen, canvas, FRAME_BYTES);
    mix_present_stats_t after = stats();
    uint64_t written = after.bytes_copied - before.bytes_copied;
    if (copy_bytes != ANY_COPY_BYTES) assert(written == copy_bytes);
    assert(written >= required_bytes * (deferred ? 1u : 2u));
    assert(written <= MIX_PRESENT_MAX_RECTS * FRAME_BYTES * (deferred ? 1u : 2u));
    uint32_t waited = plan == DELAYED_ACK ? (uint32_t)delay : 0;
    assert(after.wait_us == waited && after.total_wait_us == before.total_wait_us + waited);
    assert(after.last_us == waited + (uint32_t)draw_cost_us);
    assert(after.total_us == before.total_us + after.last_us);
    assert(after.max_us == (before.max_us > after.last_us ? before.max_us : after.last_us));
    assert(after.present_count == before.present_count + 1);
    assert(after.failure_count == before.failure_count && !after.locked);
    assert(after.frame_count == before.frame_count + future_duplicates);
    assert(after.pixels_blended == before.pixels_blended && after.last_error == ESP_OK);
    assert(take_calls - takes <= 2); /* Optional stale token plus one real wait. */
    guards_ok();
    return written;
}

static void test_deferred_first(void)
{
    rejected_deferred(&deferred_full, 1, canvas, ESP_ERR_INVALID_STATE);
    rejected_deferred(NULL, 0, NULL, ESP_ERR_INVALID_STATE);
    assert(mix_present_init(&panel) == ESP_OK);
    const mix_present_rect_t tiled[] = {{0, 0, 1024, 384}, {0, 384, 1024, 768}};
    const mix_present_rect_t full_plus[] = {{0, 0, 1024, 768}, {7, 11, 13, 19}};
    const mix_present_rect_t full_twice[] = {{0, 0, 1024, 768}, {0, 0, 1024, 768}};
    const mix_present_rect_t almost_full[] = {{0, 0, 1024, 767}, {1, 0, 1024, 768}};
    const mix_present_rect_t bad_last[] = {{0, 0, 1024, 768}, {1, 1, 1, 2}};
    rejected_deferred(&deferred_body, 1, canvas, ESP_ERR_INVALID_STATE);
    rejected_deferred(&deferred_footer, 1, canvas, ESP_ERR_INVALID_STATE);
    rejected_deferred(almost_full, 1, canvas, ESP_ERR_INVALID_STATE);
    rejected_deferred(almost_full + 1, 1, canvas, ESP_ERR_INVALID_STATE);
    rejected_deferred(batch_separated, 3, canvas, ESP_ERR_INVALID_STATE);
    rejected_deferred(tiled, 2, canvas, ESP_ERR_INVALID_STATE);
    rejected_deferred(full_plus, 2, canvas, ESP_ERR_INVALID_STATE);
    rejected_deferred(full_twice, 2, canvas, ESP_ERR_INVALID_STATE);
    rejected_deferred(bad_last, 2, canvas, ESP_ERR_INVALID_ARG);
    start_deferred_model();
    checked_deferred_update(&deferred_full, 1, true, FRAME_BYTES, DELAYED_ACK, 130000);
    /* The first partial call must repair the entire uninitialized old FB. */
    change_batch(&deferred_body, 1, 0x2715);
    checked_deferred_update(&deferred_body, 1, true, FRAME_BYTES, DELAYED_ACK, 10000);
    change_batch(&deferred_body, 1, 0x5163);
    checked_deferred_update(&deferred_body, 1, true, BODY_PIXELS * 2, DELAYED_ACK, 10000);
    change_batch(&deferred_footer, 1, 0x9325);
    checked_deferred_update(&deferred_footer, 1, false,
                           2 * (BODY_PIXELS * 2 + batch_bytes(&deferred_footer, 1)),
                           DELAYED_ACK, 10000);
}

static void test_deferred_invalid(void)
{
    assert(mix_present_init(&panel) == ESP_OK);
    check_batch_invalid_via(rejected_deferred);
    start_deferred_model();
    checked_deferred_update(&deferred_full, 1, true, FRAME_BYTES, DELAYED_ACK, 10000);
    change_batch(&deferred_body, 1, 0x4531);
    check_batch_invalid_via(rejected_deferred);
    /* Rejection must preserve the first-full pending set, not merely pixels. */
    checked_deferred_update(&deferred_body, 1, true, FRAME_BYTES, DELAYED_ACK, 10000);
    change_batch(&deferred_footer, 1, 0x3175);
    check_batch_invalid_via(rejected_deferred);
    check_batch_invalid(); /* Rejected NORMAL calls must retain pending too. */
    checked_deferred_update(&deferred_footer, 1, false,
                           2 * (BODY_PIXELS * 2 + batch_bytes(&deferred_footer, 1)),
                           DELAYED_ACK, 10000);
    check_batch_invalid_via(rejected_deferred);
    both_match_canvas();
}

static void test_deferred_body(void)
{
    initial_full();
    start_deferred_model();
    const mix_present_rect_t twice[] = {deferred_body, deferred_body};
    for (unsigned n = 0; n < 48; ++n) {
        change_batch(&deferred_body, 1, 0x1231 + n * 29u);
        unsigned takes = take_calls;
        checked_deferred_update(n % 3 ? &deferred_body : twice, n % 3 ? 1 : 2,
                               true, BODY_PIXELS * sizeof(uint16_t),
                               DELAYED_ACK, 1000 + n * 2000);
        assert(take_calls == takes + 1);
    }
    change_batch(&deferred_footer, 1, 0x7391);
    checked_deferred_update(&deferred_footer, 1, false,
                           2 * (BODY_PIXELS * 2 + batch_bytes(&deferred_footer, 1)),
                           DELAYED_ACK, 10000);
    /* Once pending is cleared, normal duplicate-write accounting is exact. */
    change_batch(&deferred_body, 1, 0x8135);
    checked_deferred_update(twice, 2, false, 4 * BODY_PIXELS * 2, DELAYED_ACK, 10000);
}

static void settings_sidebar_unchanged(const uint16_t *reference)
{
    for (unsigned y = 0; y < MIX_PRESENT_BODY_END; ++y) {
        size_t offset = (size_t)y * MIX_PRESENT_WIDTH;
        const uint16_t *row = reference + (size_t)y * 280;
        assert(memcmp(canvas + offset, row, 280 * sizeof(*row)) == 0);
        for (unsigned b = 0; b < 2; ++b) {
            assert(memcmp(buffers[b].pixels + offset, row, 280 * sizeof(*row)) == 0);
        }
    }
}

static void test_deferred_settings_regions(void)
{
    /* Actual settings geometry: fixed sidebar [0,280)x[0,648), scrollable
     * body [280,1024)x[0,648), full-width footer [0,1024)x[648,768). */
    const mix_present_rect_t body = {280, 0, 1024, 648};
    const mix_present_rect_t footer = {0, 648, 1024, 768};
    const mix_present_rect_t both[] = {body, footer};
    const mix_present_rect_t reversed[] = {footer, body};
    const uint64_t body_bytes = UINT64_C(744) * 648 * sizeof(uint16_t);
    const uint64_t footer_bytes = UINT64_C(1024) * 120 * sizeof(uint16_t);
    const uint64_t combined_bytes = body_bytes + footer_bytes;
    assert(body_bytes == 964224 && footer_bytes == 245760);
    assert(combined_bytes == 1209984 && combined_bytes < FRAME_BYTES);
    static uint16_t sidebar[280 * MIX_PRESENT_BODY_END];
    initial_full();
    start_deferred_model();
    for (unsigned y = 0; y < MIX_PRESENT_BODY_END; ++y) {
        memcpy(sidebar + (size_t)y * 280, canvas + (size_t)y * MIX_PRESENT_WIDTH,
               280 * sizeof(uint16_t));
    }
    for (unsigned n = 0; n < 36; ++n) {
        unsigned phase = n % 6;
        bool update_footer = phase == 3;
        const mix_present_rect_t *rects = update_footer ? (n % 12 == 3 ? both : reversed) : &body;
        unsigned count = update_footer ? 2 : 1;
        change_batch(rects, count, 0x1357 + n * 43u);
        unsigned takes = take_calls;
        /* After a body+footer batch the very next body-only call must also
         * repair the footer in back. The following call returns to body-only
         * cost: the footer must not remain pending forever. */
        uint64_t expected_bytes = phase == 3 || phase == 4 ? combined_bytes : body_bytes;
        checked_deferred_update(rects, count, true, expected_bytes,
                               DELAYED_ACK, 1000 + n * 2000);
        assert(take_calls == takes + 1);
        settings_sidebar_unchanged(sidebar);
        if (phase == 3) {
            assert(memcmp(buffers[scanning ^ 1u].pixels + BODY_END_PIXEL,
                          canvas + BODY_END_PIXEL, BOTTOM_NAV_BYTES) != 0);
        }
        if (phase == 4) {
            for (unsigned b = 0; b < 2; ++b) {
                assert(memcmp(buffers[b].pixels + BODY_END_PIXEL,
                              canvas + BODY_END_PIXEL, BOTTOM_NAV_BYTES) == 0);
            }
        }
    }
    /* Ordinary footer presentation must repair and mirror the L shape, not
     * copy the fixed sidebar as part of a full-screen bounding rectangle. */
    change_batch(&footer, 1, 0x7531);
    checked_deferred_update(&footer, 1, false, 2 * combined_bytes, DELAYED_ACK, 10000);
    settings_sidebar_unchanged(sidebar);
    change_batch(&body, 1, 0x3715);
    checked_deferred_update(&body, 1, false, 2 * body_bytes, DELAYED_ACK, 10000);
    settings_sidebar_unchanged(sidebar);
}

static void test_deferred_disjoint(void)
{
    initial_full();
    start_deferred_model();
    const mix_present_rect_t header = {30, 4, 35, 8};
    const mix_present_rect_t middle = {700, 100, 705, 107};
    change_batch(&deferred_body, 1, 0x1111);
    checked_deferred_update(&deferred_body, 1, true, BODY_PIXELS * 2, DELAYED_ACK, 10000);
    change_batch(&deferred_footer, 1, 0x3215);
    checked_deferred_update(&deferred_footer, 1, true,
                           BODY_PIXELS * 2 + batch_bytes(&deferred_footer, 1),
                           DELAYED_ACK, 10000);
    change_batch(&header, 1, 0x4751);
    /* The pending body has now been repaired; carrying it forever is wrong. */
    checked_deferred_update(&header, 1, true,
                           batch_bytes(&deferred_footer, 1) + batch_bytes(&header, 1),
                           DELAYED_ACK, 10000);
    change_batch(&middle, 1, 0x5391);
    checked_deferred_update(&middle, 1, false,
                           2 * (batch_bytes(&header, 1) + batch_bytes(&middle, 1)),
                           DELAYED_ACK, 10000);
}

static void test_deferred_overlap(void)
{
    initial_full();
    start_deferred_model();
    const mix_present_rect_t overlap[] = {
        {10, 10, 20, 20}, {18, 18, 30, 30}, {10, 10, 20, 20}, {21, 21, 22, 22}
    };
    const mix_present_rect_t bridge[] = {
        {10, 10, 20, 20}, {30, 30, 40, 40}, {20, 20, 30, 30}
    };
    const mix_present_rect_t twice[] = {{10, 10, 40, 40}, {10, 10, 40, 40}};
    for (unsigned n = 0; n < 8; ++n) {
        change_batch(overlap, 4, 0x1131 + n * 37u);
        /* Duplicates and contained pixels collapse, but the remaining 10x10
         * and 12x12 rectangles must not expand into a costlier 20x20 box. */
        checked_deferred_update(overlap, 4, true, (10 * 10 + 12 * 12) * 2,
                               DELAYED_ACK, 10000);
    }
    /* The middle bridge lies inside the pending 12x12 rectangle. The 10x10
     * corner-touching rectangles remain separate because merging would add
     * writes; all unchanged canvas gaps must still survive intact. */
    change_batch(bridge, 3, 0x3597);
    checked_deferred_update(bridge, 3, true, (10 * 10 + 12 * 12 + 10 * 10) * 2,
                           DELAYED_ACK, 10000);
    change_batch(twice, 1, 0x8471);
    checked_deferred_update(twice, 2, false, 30 * 30 * 4, DELAYED_ACK, 10000);
    change_batch(twice, 1, 0x6391);
    checked_deferred_update(twice, 2, false, 30 * 30 * 8, DELAYED_ACK, 10000);
}

static void test_deferred_max(void)
{
    initial_full();
    start_deferred_model();
    mix_present_rect_t sets[2][MIX_PRESENT_MAX_RECTS];
    for (unsigned n = 0; n < MIX_PRESENT_MAX_RECTS; ++n) {
        int x = 10 + (int)n * 120, y = 20 + (int)(n % 2) * 40;
        sets[0][n] = (mix_present_rect_t){x, y, x + 5, y + 7};
        sets[1][n] = (mix_present_rect_t){x + 50, y + 110, x + 59, y + 121};
    }
    uint64_t both_bytes = batch_bytes(sets[0], MIX_PRESENT_MAX_RECTS) +
                          batch_bytes(sets[1], MIX_PRESENT_MAX_RECTS);
    for (unsigned n = 0; n < 32; ++n) {
        mix_present_rect_t *rects = sets[n % 2];
        change_batch(rects, MIX_PRESENT_MAX_RECTS, 0x5531 + n * 73u);
        uint64_t written = checked_deferred_update(rects, MIX_PRESENT_MAX_RECTS, true,
                                                  n ? ANY_COPY_BYTES : batch_bytes(rects, MIX_PRESENT_MAX_RECTS),
                                                  DELAYED_ACK, 10000);
        /* Sixteen disjoint regions exceed the eight-slot pending capacity.
         * Forced bounding boxes include gaps, which the whole-frame oracle
         * checks against the unchanged canvas, not zero-filled test memory. */
        if (n) assert(written > both_bytes);
    }
    change_batch(sets[0], MIX_PRESENT_MAX_RECTS, 0x3815);
    checked_deferred_update(sets[0], MIX_PRESENT_MAX_RECTS, false,
                           ANY_COPY_BYTES, DELAYED_ACK, 10000);
}

static uint32_t deferred_random(uint32_t *state)
{
    *state = *state * UINT32_C(1664525) + UINT32_C(1013904223);
    return *state;
}

static void test_deferred_sequences(void)
{
    initial_full();
    start_deferred_model();
    uint32_t state = UINT32_C(0x75c132ab);
    for (unsigned n = 0; n < 96; ++n) {
        mix_present_rect_t rects[MIX_PRESENT_MAX_RECTS];
        unsigned count = 1 + n % MIX_PRESENT_MAX_RECTS;
        for (unsigned r = 0; r < count; ++r) {
            unsigned x = deferred_random(&state) % MIX_PRESENT_WIDTH;
            unsigned y = deferred_random(&state) % MIX_PRESENT_HEIGHT;
            unsigned width = 1 + deferred_random(&state) % (MIX_PRESENT_WIDTH - x);
            unsigned height = 1 + deferred_random(&state) % (MIX_PRESENT_HEIGHT - y);
            rects[r] = (mix_present_rect_t){(int)x, (int)y, (int)(x + width), (int)(y + height)};
        }
        if (n % 12 == 0) rects[0] = (mix_present_rect_t){0, 0, 1, 1};
        if (n % 12 == 1) rects[0] = (mix_present_rect_t){1023, 767, 1024, 768};
        if (n % 12 == 2) rects[0] = (mix_present_rect_t){0, 647, 1024, 649};
        if (n % 12 == 3) rects[0] = (mix_present_rect_t){0, 0, 1, 768};
        if (n % 12 == 4) rects[0] = (mix_present_rect_t){1023, 0, 1024, 768};
        change_batch(rects, count, 0x9135 + n * 43u);
        checked_deferred_update(rects, count, n % 9 != 8, ANY_COPY_BYTES,
                               DELAYED_ACK, 1000 + n * 1000);
    }
    checked_deferred_update(&deferred_footer, 1, false, ANY_COPY_BYTES, DELAYED_ACK, 10000);
}

static void rejected_pending_fade(void)
{
    mix_present_stats_t before = stats();
    unsigned draws = draw_calls, takes = take_calls;
    memcpy(locked_copy[0], buffers[0].pixels, FRAME_BYTES);
    memcpy(locked_copy[1], buffers[1].pixels, FRAME_BYTES);
    assert(mix_present_fade_begin(canvas, 0x39e7) == ESP_ERR_INVALID_STATE);
    assert(mix_present_fade_step(255) == ESP_ERR_INVALID_STATE);
    mix_present_fade_end();
    mix_present_fade_end();
    assert(mix_present_fade_begin(canvas, 0x39e7) == ESP_ERR_INVALID_STATE);
    assert(draw_calls == draws && take_calls == takes);
    same_stats(before, stats());
    assert(memcmp(locked_copy[0], buffers[0].pixels, FRAME_BYTES) == 0);
    assert(memcmp(locked_copy[1], buffers[1].pixels, FRAME_BYTES) == 0);
}

static void test_deferred_fade(void)
{
    assert(mix_present_init(&panel) == ESP_OK);
    start_deferred_model();
    checked_deferred_update(&deferred_full, 1, true, FRAME_BYTES, DELAYED_ACK, 10000);
    rejected_pending_fade();
    change_batch(&deferred_body, 1, 0x3751);
    checked_deferred_update(&deferred_body, 1, true, FRAME_BYTES, DELAYED_ACK, 10000);
    rejected_pending_fade();
    rejected_batch(NULL, 0, NULL, ESP_ERR_INVALID_ARG);
    rejected_deferred(NULL, 0, NULL, ESP_ERR_INVALID_ARG);
    rejected_pending_fade();
    change_batch(&deferred_footer, 1, 0x5317);
    checked_deferred_update(&deferred_footer, 1, false,
                           2 * (BODY_PIXELS * 2 + batch_bytes(&deferred_footer, 1)),
                           DELAYED_ACK, 10000);
    check_deferred_handoff = false;
    fade_background = 0x39e7;
    checked_fade_begin();
    checked_fade_step(128, DELAYED_ACK);
    checked_fade_step(0, DELAYED_ACK);
    checked_fade_step(255, DELAYED_ACK);
    both_match_canvas();
    mix_present_fade_end();
}

static void test_deferred_fade_cancel(void)
{
    prepare_fade(0x7392, false, false);
    const mix_present_rect_t invalid_middle[] = {
        {0, 0, 1, 1}, {1, 1, 1, 2}, {1023, 767, 1024, 768}
    };
    for (unsigned n = 0; n < 9; ++n) {
        const mix_present_rect_t *rects = batch_separated;
        const uint16_t *source = canvas;
        unsigned count = 3;
        switch (n) {
        case 0: rects = NULL; break;
        case 1: count = 0; break;
        case 2: count = MIX_PRESENT_MAX_RECTS + 1; break;
        case 3: source = NULL; break;
        case 4: rects = invalid_middle; break;
        case 5: source = buffers[0].pixels; break;
        case 6: source = (const uint16_t *)((const uint8_t *)canvas + 1); break;
        case 7: rects = (const mix_present_rect_t *)(const void *)buffers[1].pixels; break;
        case 8: rects = (const mix_present_rect_t *)((const uint8_t *)batch_separated + 1); break;
        }
        rejected_deferred(rects, count, source, ESP_ERR_INVALID_ARG);
        assert(mix_present_fade_step(255) == ESP_ERR_INVALID_STATE);
        checked_fade_begin();
    }
    checked_fade_step(255, DELAYED_ACK); /* Leaves fade active, both FBs complete. */
    start_deferred_model();
    change_batch(&deferred_body, 1, 0x7123);
    checked_deferred_update(&deferred_body, 1, true, BODY_PIXELS * 2, DELAYED_ACK, 10000);
    assert(mix_present_fade_step(0) == ESP_ERR_INVALID_STATE);
    rejected_pending_fade();
}

static void test_deferred_race(enum plan plan)
{
    assert(mix_present_init(&panel) == ESP_OK);
    start_deferred_model();
    future_duplicates = 4;
    checked_deferred_update(&deferred_full, 1, true, FRAME_BYTES, plan, 0);
    assert(woken_calls == 0);
    assert(take_calls == (plan == ACK_AT_TAKE ? 1u : 0u));
    future_duplicates = 1;
    change_batch(&deferred_body, 1, 0x5117);
    checked_deferred_update(&deferred_body, 1, true, FRAME_BYTES, DELAYED_ACK, 20000);
    assert(stats().wait_us == 20000 && woken_calls == 1);
    future_duplicates = 4;
    change_batch(&deferred_footer, 1, 0x6173);
    checked_deferred_update(&deferred_footer, 1, true,
                           BODY_PIXELS * 2 + batch_bytes(&deferred_footer, 1), plan, 0);
    future_duplicates = 1;
    checked_deferred_update(&deferred_footer, 1, false,
                           2 * batch_bytes(&deferred_footer, 1), DELAYED_ACK, 20000);
    assert(stats().wait_us == 20000 && woken_calls == 2);
}

static void test_deferred_fault(const char *name)
{
    bool first = strstr(name, "first_") != NULL;
    bool stale = strstr(name, "stale") != NULL;
    const mix_present_rect_t *rects = first ? &deferred_full : &deferred_footer;
    uint64_t writes = FRAME_BYTES;
    if (first) {
        assert(mix_present_init(&panel) == ESP_OK);
        start_deferred_model();
        if (stale) emit_frame(false, 3); /* Publication/idle token before first. */
    } else {
        initial_full();
        start_deferred_model();
        change_batch(batch_separated, 3, 0x1937);
        future_duplicates = stale ? 4 : 1;
        checked_deferred_update(batch_separated, 3, true, batch_bytes(batch_separated, 3),
                               stale ? ACK_AT_BASELINE_EXIT : DELAYED_ACK, stale ? 0 : 10000);
        future_duplicates = 1;
        writes = batch_bytes(batch_separated, 3) + batch_bytes(rects, 1);
    }
    enum plan plan = NO_ACK;
    int64_t delay = 0;
    if (strstr(name, "spurious")) plan = SPURIOUS;
    if (strstr(name, "deadline")) { plan = DELAYED_ACK; delay = 150000; }
    if (strstr(name, "late")) { plan = DELAYED_ACK; delay = 160000; }
    bool early = strstr(name, "early") != NULL;
    bool duplicate = strstr(name, "duplicate") != NULL;
    if (early || duplicate) {
        emit_frame(false, duplicate ? 5 : 3);
        early_before_select = duplicate ? 5 : 4;
        early_after_select = 5;
    }
    change_batch(rects, 1, 0x7321);
    mix_present_stats_t before = stats();
    unsigned draws = draw_calls, takes = take_calls;
    bool error = strstr(name, "draw_error") != NULL;
    begin_attempt(plan, delay);
    if (error) {
        draw_error = DRAW_ERROR;
        switch_before_error = strstr(name, "switched") != NULL;
        early_after_select = switch_before_error ? 2 : 0;
    }
    esp_err_t expected_error = error ? DRAW_ERROR : ESP_ERR_TIMEOUT;
    assert(mix_present_rects_deferred(rects, 1, canvas) == expected_error);
    assert(memcmp(old_front, buffers[protected_index].pixels, FRAME_BYTES) == 0);
    assert(memcmp(buffers[expected_back].pixels, canvas, FRAME_BYTES) == 0);
    old_front_unchanged();
    mix_present_stats_t after = stats();
    assert(draw_calls == draws + 1);
    assert(after.bytes_copied == before.bytes_copied + writes);
    assert(after.present_count == before.present_count && after.failure_count == 1);
    assert(after.pixels_blended == before.pixels_blended);
    assert(after.last_error == expected_error && after.locked);
    assert(after.total_wait_us == before.total_wait_us + after.wait_us);
    assert(after.last_us == (uint32_t)draw_cost_us + after.wait_us);
    assert(after.total_us == before.total_us + after.last_us);
    if (error) {
        assert(after.wait_us == 0 && takes == take_calls);
    } else {
        assert(after.wait_us >= 150000);
        assert(after.wait_us <= 150000 + (1000000 + TEST_TICK_HZ - 1) / TEST_TICK_HZ);
        assert(take_calls - takes <= 10);
        if (early || duplicate || stale) assert(take_calls == takes + 2);
    }
    stays_locked(expected_error);
}

static unsigned hook_calls;
static int64_t hook_last_us;
static bool hook_ack,hook_disable;
static void sample_hook(void *ctx)
{
    assert(ctx==&hook_calls && !in_isr && !lock_depth);
    assert(now_us-wait_started_us<=130000);
    if(hook_calls)assert(now_us-hook_last_us>=8000);
    hook_last_us=now_us; ++hook_calls;
    old_front_unchanged();
    now_us+=2000; /* bounded I2C acquisition, never drawing */
    if(hook_ack)emit_frame(true,1);
    if(hook_disable)mix_present_set_wait_hook(NULL,NULL);
}
static void test_wait_hook(const char *name)
{
    assert(mix_present_init(&panel)==ESP_OK);
    mix_present_set_wait_hook(sample_hook,&hook_calls);
    bool timeout=strstr(name,"timeout")!=NULL;
    hook_ack=strstr(name,"race")!=NULL;
    hook_disable=strstr(name,"disable")!=NULL;
    begin_attempt(timeout?SPURIOUS:DELAYED_ACK,35000);
    if(hook_ack)current_plan=NO_ACK;
    if(timeout) {
        timed_out();
        assert(hook_calls>=10 && hook_calls<=17);
        return;
    }
    assert(mix_present_rect(0,0,1024,768,canvas)==ESP_OK);
    both_match_canvas();
    if(hook_ack||hook_disable)assert(hook_calls==1);
    else assert(hook_calls>=3 && hook_calls<=5);
    assert(stats().wait_us==(hook_ack?2000u:35000u));
    /* Disabling restores the normal one-wait path, with no stale callback. */
    unsigned previous=hook_calls;
    mix_present_set_wait_hook(NULL,NULL);
    begin_attempt(DELAYED_ACK,10000);
    assert(mix_present_rect(0,0,1024,768,canvas)==ESP_OK);
    assert(hook_calls==previous);both_match_canvas();
}

int main(int argc, char **argv)
{
    assert(argc == 2);
    initial_data();
    const char *name = argv[1];
    if (!strncmp(name, "hook_", 5)) test_wait_hook(name);
    else if (!strcmp(name, "init")) test_init();
    else if (!strcmp(name, "full")) test_full(false);
    else if (!strcmp(name, "delayed")) test_full(true);
    else if (!strcmp(name, "partial")) test_partial();
    else if (!strcmp(name, "invalid")) test_invalid();
    else if (!strcmp(name, "early")) test_early(false);
    else if (!strcmp(name, "duplicate")) test_early(true);
    else if (!strcmp(name, "stale")) test_stale();
    else if (!strcmp(name, "baseline_race")) test_race(ACK_AT_BASELINE_EXIT);
    else if (!strcmp(name, "take_race")) test_race(ACK_AT_TAKE);
    else if (!strcmp(name, "draw_error")) test_error(false);
    else if (!strcmp(name, "draw_error_switched")) test_error(true);
    else if (!strcmp(name, "batch_small") || !strcmp(name, "batch_separated") ||
             !strcmp(name, "batch_overlap") || !strcmp(name, "batch_edges") ||
             !strcmp(name, "batch_max")) test_batch_regions(name);
    else if (!strcmp(name, "batch_invalid")) test_batch_invalid();
    else if (!strcmp(name, "batch_first")) test_batch_first();
    else if (!strcmp(name, "batch_fade_cancel")) test_batch_fade_cancel();
    else if (!strcmp(name, "batch_baseline_race")) test_batch_race(ACK_AT_BASELINE_EXIT);
    else if (!strcmp(name, "batch_take_race")) test_batch_race(ACK_AT_TAKE);
    else if (!strcmp(name, "batch_timeout") || !strcmp(name, "batch_early") ||
             !strcmp(name, "batch_stale") || !strcmp(name, "batch_spurious") ||
             !strcmp(name, "batch_deadline") || !strcmp(name, "batch_late") ||
             !strcmp(name, "batch_draw_error") ||
             !strcmp(name, "batch_draw_error_switched")) test_batch_fault(name);
    else if (!strcmp(name, "deferred_first")) test_deferred_first();
    else if (!strcmp(name, "deferred_invalid")) test_deferred_invalid();
    else if (!strcmp(name, "deferred_body")) test_deferred_body();
    else if (!strcmp(name, "deferred_settings_regions")) test_deferred_settings_regions();
    else if (!strcmp(name, "deferred_disjoint")) test_deferred_disjoint();
    else if (!strcmp(name, "deferred_overlap")) test_deferred_overlap();
    else if (!strcmp(name, "deferred_max")) test_deferred_max();
    else if (!strcmp(name, "deferred_sequences")) test_deferred_sequences();
    else if (!strcmp(name, "deferred_fade")) test_deferred_fade();
    else if (!strcmp(name, "deferred_fade_cancel")) test_deferred_fade_cancel();
    else if (!strcmp(name, "deferred_baseline_race")) test_deferred_race(ACK_AT_BASELINE_EXIT);
    else if (!strcmp(name, "deferred_take_race")) test_deferred_race(ACK_AT_TAKE);
    else if (!strncmp(name, "deferred_", 9) &&
             (strstr(name, "timeout") || strstr(name, "early") || strstr(name, "duplicate") ||
              strstr(name, "stale") || strstr(name, "spurious") || strstr(name, "deadline") ||
              strstr(name, "late") || strstr(name, "draw_error"))) test_deferred_fault(name);
    else if (!strcmp(name, "fade_black")) test_fade_accuracy(0, false);
    else if (!strcmp(name, "fade_color")) test_fade_accuracy(0x39e7, false);
    else if (!strcmp(name, "fade_dense")) test_fade_accuracy(0x5aa5, true);
    else if (!strcmp(name, "fade_handoff")) test_fade_handoff();
    else if (!strcmp(name, "fade_empty")) test_fade_empty();
    else if (!strcmp(name, "fade_invalid")) test_fade_invalid();
    else if (!strcmp(name, "fade_cancel")) test_fade_cancel();
    else if (!strcmp(name, "fade_spans")) test_fade_spans();
    else if (!strcmp(name, "fade_body_edges")) test_fade_body_edges();
    else if (!strcmp(name, "fade_begin_end")) test_fade_begin_end();
    else if (!strcmp(name, "fade_early")) test_fade_fault(name);
    else if (!strcmp(name, "fade_stale")) test_fade_fault(name);
    else if (!strcmp(name, "fade_spurious")) test_fade_fault(name);
    else if (!strcmp(name, "fade_deadline")) test_fade_fault(name);
    else if (!strcmp(name, "fade_late")) test_fade_fault(name);
    else if (!strcmp(name, "fade_timeout")) test_fade_fault(name);
    else if (!strcmp(name, "fade_draw_error")) test_fade_fault(name);
    else if (!strcmp(name, "fade_draw_error_switched")) test_fade_fault(name);
    else if (!strcmp(name, "fade_baseline_race")) test_fade_race(ACK_AT_BASELINE_EXIT);
    else if (!strcmp(name, "fade_take_race")) test_fade_race(ACK_AT_TAKE);
    else if (!strcmp(name, "timeout") || !strcmp(name, "spurious") ||
             !strcmp(name, "deadline") || !strcmp(name, "late")) {
        assert(mix_present_init(&panel) == ESP_OK);
        if (!strcmp(name, "spurious")) begin_attempt(SPURIOUS, 0);
        else if (!strcmp(name, "deadline")) begin_attempt(DELAYED_ACK, 150000);
        else if (!strcmp(name, "late")) begin_attempt(DELAYED_ACK, 160000);
        else begin_attempt(NO_ACK, 0);
        timed_out();
        assert(take_calls <= 10);
    } else {
        fprintf(stderr, "unknown scenario: %s\n", name);
        return 2;
    }
    guards_ok();
    assert(lock_depth == 0 && !in_isr);
    printf("PASS %s (tick_hz=%d)\n", name, TEST_TICK_HZ);
    return 0;
}
