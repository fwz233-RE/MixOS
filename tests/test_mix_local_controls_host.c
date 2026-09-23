/* Host-only main.c policy tests. Functions and touch/draw bridges below are
 * extracted verbatim by test_mix_local_controls.py into temporary headers.
 * Platform/UI/audio/keyboard calls are stubs using real public declarations.
 * Keyboard reports are device REQUESTED levels, never measured physical PWM.
 */
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "esp_err.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "mix_ui.h"
#include "mix_keyboard.h"
#include "mix_link.h"
#include "audio.h"
#include "gt911.h"

#define CHECK(expr) do { if (!(expr)) { \
    fprintf(stderr, "%s:%d: %s\n", __func__, __LINE__, #expr); exit(1); \
} } while (0)

enum operation { DUTY_SET, DUTY_UPDATE, AUDIO_SET, KBD_SET, KBD_STEP,
    INPUT_RESET, FEEDBACK, UI_VOLUME, LOCK_KEY, UI_TICK, UI_TEXT, LINK_TEXT,
    NOTICE, TOUCH };
struct call { enum operation op; int value; };
static struct call calls[1024];
static unsigned call_count;
static void note(enum operation op, int value) {
    CHECK(call_count < sizeof(calls) / sizeof(calls[0]));
    calls[call_count++] = (struct call){op, value};
}
static unsigned count(enum operation op) {
    unsigned n = 0;
    for (unsigned i = 0; i < call_count; ++i) if (calls[i].op == op) ++n;
    return n;
}
static int last(enum operation op) {
    for (unsigned i = call_count; i; --i) if (calls[i-1].op == op) return calls[i-1].value;
    CHECK(false); return 0;
}
static unsigned first(enum operation op) {
    for (unsigned i = 0; i < call_count; ++i) if (calls[i].op == op) return i;
    CHECK(false); return 0;
}

static const char *TAG = "local-controls-test";
static mix_view_t view;
static uint32_t now;
static bool locked, awake = true, draw_healthy = true, next_draw_success = true;
static bool terminal_visible, link_accepts = true;
static esp_err_t set_error, update_error, audio_error;
static int staged_duty = 1023, committed_duty = 1023;
static int reported_requested_level = 4, queued_set = -1;
static unsigned queued_steps;
static mix_ui_feedback_t feedback_kind;
static int feedback_value;
static char notice[96];
static uint8_t ui_bytes[32], link_bytes[32];
static size_t ui_len, link_len;

int64_t esp_timer_get_time(void) { return (int64_t)now * 1000; }
#define LEDC_LOW_SPEED_MODE 1
#define LEDC_CHANNEL_0 0
static esp_err_t ledc_set_duty(int mode, int channel, uint32_t duty) {
    CHECK(mode == LEDC_LOW_SPEED_MODE && channel == LEDC_CHANNEL_0 && duty <= 1023);
    note(DUTY_SET, (int)duty);
    if (set_error == ESP_OK) staged_duty = (int)duty;
    return set_error;
}
static esp_err_t ledc_update_duty(int mode, int channel) {
    CHECK(mode == LEDC_LOW_SPEED_MODE && channel == LEDC_CHANNEL_0);
    note(DUTY_UPDATE, staged_duty);
    if (update_error == ESP_OK) committed_duty = staged_duty;
    return update_error;
}
esp_err_t audio_set_volume(int percent) { note(AUDIO_SET, percent); return audio_error; }
bool mix_ui_locked(void) { return locked; }
bool mix_ui_display_awake(void) { return awake; }
bool mix_ui_draw_healthy(void) { return draw_healthy; }
bool mix_ui_terminal_visible(void) { return terminal_visible; }
void mix_ui_lock_key(void) {
    note(LOCK_KEY, 0);
    if (!locked) { locked = true; awake = false; } else awake = !awake;
}
void mix_ui_tick(const mix_view_t *v, uint32_t ms) {
    CHECK(v == &view && ms == now);
    note(UI_TICK, 0); draw_healthy = next_draw_success;
}
void mix_ui_feedback(mix_ui_feedback_t kind, int value) {
    feedback_kind = kind; feedback_value = value; note(FEEDBACK, value);
}
void mix_ui_volume(int percent) { note(UI_VOLUME, percent); }
void mix_ui_notice(const char *text) { snprintf(notice, sizeof(notice), "%s", text); note(NOTICE, 0); }
void mix_ui_key(const uint8_t *bytes, size_t len) {
    CHECK(len <= sizeof(ui_bytes)); memcpy(ui_bytes, bytes, len); ui_len = len; note(UI_TEXT, (int)len);
}
bool mix_link_input(const uint8_t *bytes, size_t len) {
    CHECK(len <= sizeof(link_bytes)); memcpy(link_bytes, bytes, len); link_len = len; note(LINK_TEXT, (int)len);
    return link_accepts;
}
void mix_keyboard_reset_input(void) { note(INPUT_RESET, 0); }
int mix_keyboard_backlight_level(void) { return reported_requested_level; }
/* This stub models only the documented command queue. Tests assert main's
 * absolute-set calls and their order; driver/transport correctness is separate.
 * In particular, queuing/transmitting NEVER fabricates a device report. */
void mix_keyboard_backlight_set(uint8_t level) {
    CHECK(level <= 8); note(KBD_SET, level); queued_set = level; queued_steps = 0;
}
void mix_keyboard_backlight_step(void) {
    note(KBD_STEP, 0);
    if (reported_requested_level >= 0) queued_steps = (queued_steps + 1) % 9;
}
static int transmit_queued_command(bool success) {
    CHECK(queued_set >= 0 || queued_steps);
    int base = queued_set >= 0 ? queued_set : reported_requested_level;
    CHECK(base >= 0);
    int requested = (base + (int)queued_steps) % 9;
    queued_steps = 0;
    if (success) queued_set = -1;
    else reported_requested_level = -1;
    return requested;
}

#include "main_local_controls.h"
static void draw_and_sync(void) {
#include "main_draw_sync.h"
}

/* Environment of the real GT911 polling block in app_main. */
static i2c_master_dev_handle_t tp = (void *)1, aw = (void *)2;
static unsigned touch_reads, touch_inits, touch_calls, touch_cancels, touch_removes, touch_resets;
static esp_err_t touch_remove_error, touch_reset_error;
static gt911_touch_t touch_frame;
static esp_err_t touch_error, touch_init_error;
static int sent_x, sent_y;
static bool sent_down;
esp_err_t gt911_init(i2c_master_dev_handle_t *out) {
    ++touch_inits; if (touch_init_error == ESP_OK) *out = (void *)1; return touch_init_error;
}
esp_err_t gt911_read(i2c_master_dev_handle_t dev, gt911_touch_t *out) {
    CHECK(dev == tp); ++touch_reads; *out = touch_frame; return touch_error;
}
esp_err_t gt911_try_read(i2c_master_dev_handle_t dev, gt911_touch_t *out) {
    return gt911_read(dev, out);
}
static struct {int x,y;bool down;} touch_sent[256];
static void (*during_touch)(void);
void mix_ui_touch(int x, int y, bool down) {
    CHECK(touch_calls<256);
    touch_sent[touch_calls].x=x;touch_sent[touch_calls].y=y;touch_sent[touch_calls].down=down;
    ++touch_calls; sent_x = x; sent_y = y; sent_down = down; note(TOUCH, down);
    if(during_touch){void (*cb)(void)=during_touch;during_touch=NULL;cb();}
}
static esp_err_t mix_i2c_rm_device(i2c_master_dev_handle_t dev) {
    CHECK(dev == tp); ++touch_removes; return touch_remove_error;
}
static esp_err_t aw9523_gt911_reset(i2c_master_dev_handle_t dev) {
    CHECK(dev == aw && !tp); ++touch_resets; return touch_reset_error;
}
#include "main_touch_recovery.h"
void mix_ui_touch_cancel(void) {
    ++touch_cancels; sent_x=sent_y=0;sent_down=false;
}
#include "main_touch_sampling.h"
static void poll_touch(uint32_t ms) {
    now=ms;
    (void)touch_wait_sample;
#include "main_touch_poll.h"
}
static void expect_feedback(mix_ui_feedback_t kind, int value) {
    CHECK(count(FEEDBACK) > 0 && feedback_kind == kind && feedback_value == value);
}
/* Fault injection for the retained compatibility synchronizer, not a product
 * gesture: physical LOCK no longer calls any UI or backlight operation. */
static void lock_now(void) { locked=true;awake=false;sync_local_lock(); }
static void seed_touch(void) {
    touch_frame = (gt911_touch_t){.count = 1, .x = 345, .y = 678}; poll_touch(100);
    CHECK(sent_x == 345 && sent_y == 678 && sent_down);
}

static void brightness_bounds(void) {
    CHECK(brightness == 10);
    adjust_brightness(-100); CHECK(brightness == 1 && committed_duty == 102);
    expect_feedback(MIX_UI_BRIGHTNESS, 10);
    adjust_brightness(100); CHECK(brightness == 10 && committed_duty == 1023);
    expect_feedback(MIX_UI_BRIGHTNESS, 100);
    awake = false; CHECK(apply_brightness(-3) == ESP_OK);
    CHECK(brightness == 7 && committed_duty == 0);
    awake = true; CHECK(apply_brightness(0) == ESP_OK && committed_duty == 716);
}
static void brightness_set_failure(void) {
    brightness = 5; set_error = ESP_FAIL; adjust_brightness(1);
    CHECK(brightness == 5 && committed_duty == 1023 && count(DUTY_UPDATE) == 0);
    expect_feedback(MIX_UI_BRIGHTNESS, -1);
    set_error = ESP_OK; adjust_brightness(1);
    CHECK(brightness == 6 && committed_duty == 613); expect_feedback(MIX_UI_BRIGHTNESS, 60);
}
static void brightness_update_failure(void) {
    brightness = 5; update_error = ESP_FAIL; adjust_brightness(-1);
    CHECK(brightness == 5 && committed_duty == 1023 && staged_duty == 409);
    CHECK(count(DUTY_SET) == 1 && count(DUTY_UPDATE) == 1); expect_feedback(MIX_UI_BRIGHTNESS, -1);
    update_error = ESP_OK; adjust_brightness(-1);
    CHECK(brightness == 4 && committed_duty == 409); expect_feedback(MIX_UI_BRIGHTNESS, 40);
}
static void volume_failure(void) {
    CHECK(volume == 60); audio_error = ESP_FAIL; adjust_volume(5);
    CHECK(volume == 60 && last(AUDIO_SET) == 65 && count(UI_VOLUME) == 0);
    expect_feedback(MIX_UI_VOLUME, -1);
    audio_error = ESP_OK; adjust_volume(5);
    CHECK(volume == 65 && last(UI_VOLUME) == 65); expect_feedback(MIX_UI_VOLUME, 65);
}
static void volume_bounds(void) {
    adjust_volume(-1000); CHECK(volume == 0 && last(AUDIO_SET) == 0 && last(UI_VOLUME) == 0);
    adjust_volume(1000); CHECK(volume == 100 && last(AUDIO_SET) == 100 && last(UI_VOLUME) == 100);
    key_event(MIX_KEY_VOLUME_DOWN, NULL, 0, NULL); CHECK(volume == 95); expect_feedback(MIX_UI_VOLUME, 95);
    key_event(MIX_KEY_VOLUME_UP, NULL, 0, NULL); CHECK(volume == 100); expect_feedback(MIX_UI_VOLUME, 100);
    key_event(MIX_KEY_BRIGHT_DOWN, NULL, 0, NULL); CHECK(brightness == 9);
    key_event(MIX_KEY_BRIGHT_UP, NULL, 0, NULL); CHECK(brightness == 10);
}
static void lock_immediate(void) {
    adjust_keyboard_light(); adjust_keyboard_light();
    CHECK(light_feedback_target == 6);
    /* Model an older queued driver step as well as main's latest target. */
    mix_keyboard_backlight_step(); CHECK(queued_steps > 0);
    unsigned lock_start = call_count;
    lock_now();
    CHECK(committed_duty == 0 && brightness == 10); /* before any UI tick */
    CHECK(count(UI_TICK) == 0 && count(INPUT_RESET) > 0);
    CHECK(saved_keyboard_light == 4 && !light_feedback_pending);
    CHECK(last(KBD_SET) == 0 && queued_set == 0 && queued_steps == 0);
    unsigned zero_set = lock_start;
    while (zero_set < call_count && !(calls[zero_set].op == KBD_SET && calls[zero_set].value == 0)) ++zero_set;
    CHECK(zero_set < call_count && first(KBD_STEP) < zero_set && zero_set < first(DUTY_SET));
    CHECK(transmit_queued_command(true) == 0);
    CHECK(reported_requested_level == 4 && count(FEEDBACK) == 0);
}
static void lock_restore(void) {
    reported_requested_level = 7; brightness = 6; lock_now();
    reported_requested_level = 0; sync_local_lock(); CHECK(saved_keyboard_light == 7);
    locked = false; awake = true; draw_and_sync();
    CHECK(last(KBD_SET) == 7 && queued_set == 7 && saved_keyboard_light == 7);
    CHECK(committed_duty == 613 && brightness == 6 && count(INPUT_RESET) >= 2);
    unsigned sets = count(KBD_SET); sync_local_lock(); CHECK(count(KBD_SET) == sets);
    CHECK(transmit_queued_command(true) == 7 && reported_requested_level == 0);
    reported_requested_level = 2; lock_now(); reported_requested_level = 0;
    locked = false; awake = true; draw_and_sync(); CHECK(last(KBD_SET) == 2);
}
static void lock_reconnect(void) {
    reported_requested_level = 6; lock_now();
    CHECK(transmit_queued_command(false) == 0 && queued_set == 0);
    sync_local_lock(); CHECK(saved_keyboard_light == 6 && queued_set == 0);
    reported_requested_level = 8; sync_local_lock();
    CHECK(last(KBD_SET) == 0 && saved_keyboard_light == 6);
    CHECK(transmit_queued_command(true) == 0);
    reported_requested_level = 0; unsigned sets = count(KBD_SET); sync_local_lock();
    CHECK(count(KBD_SET) == sets && committed_duty == 0);
    reported_requested_level = -1; sync_local_lock();
    reported_requested_level = 5; sync_local_lock(); CHECK(last(KBD_SET) == 0);
    CHECK(saved_keyboard_light == 6); /* reconnect reports must not overwrite saved level */
    locked = false; awake = true; draw_and_sync(); CHECK(last(KBD_SET) == 6);
}
static void lock_offline(void) {
    reported_requested_level = -1; lock_now();
    CHECK(saved_keyboard_light == -1 && queued_set == 0 && count(KBD_SET) == 1);
    reported_requested_level = 8; sync_local_lock(); CHECK(last(KBD_SET) == 0);
    unsigned sets = count(KBD_SET); reported_requested_level = 0;
    locked = false; awake = true; draw_and_sync();
    CHECK(count(KBD_SET) == sets && saved_keyboard_light == -1);
}
static void locked_keys(void) {
    /* The reserved physical key is inert even with pending backlight work. */
    const uint8_t bytes[] = {'a'};
    adjust_keyboard_light();unsigned before=call_count;
    for(int i=0;i<4;i++)key_event(MIX_KEY_LOCK,NULL,0,NULL);
    CHECK(!locked&&awake&&call_count==before&&light_feedback_pending);
    CHECK(committed_duty==1023&&volume==60&&brightness==10);
    key_event(MIX_KEY_TEXT,bytes,sizeof(bytes),NULL);CHECK(count(UI_TEXT)==1);
    key_event(MIX_KEY_VOLUME_DOWN,NULL,0,NULL);CHECK(volume==55);
    key_event(MIX_KEY_BRIGHT_DOWN,NULL,0,NULL);CHECK(brightness==9);
    CHECK(count(LOCK_KEY)==0&&count(INPUT_RESET)==0);
    /* Retained defensive guard: an injected legacy state still isolates text. */
    lock_now();before=call_count;
    for(int action=MIX_KEY_TEXT;action<=MIX_KEY_BACKLIGHT;action++)
        key_event(action,bytes,sizeof(bytes),NULL);
    CHECK(call_count==before);
}
static void wake_draw_gate(void) {
    lock_now(); reported_requested_level = 0; call_count = 0;
    awake=true;draw_healthy=false;CHECK(locked&&count(DUTY_SET)==0);
    next_draw_success = false; draw_and_sync(); CHECK(committed_duty == 0 && count(DUTY_SET) == 0);
    sync_local_lock(); CHECK(count(DUTY_SET) == 0);
    call_count = 0; next_draw_success = true; draw_and_sync();
    CHECK(committed_duty == 1023 && first(UI_TICK) < first(DUTY_SET));
    CHECK(count(KBD_SET) == 0); /* wake lock screen, not unlock keyboard */
    awake=false;sync_local_lock();CHECK(committed_duty==0);
    CHECK(count(UI_TICK) == 1); /* darkness is immediate, without another render */
}
static void lock_ledc_retry(void) {
    set_error = ESP_FAIL; lock_now(); CHECK(committed_duty == 1023 && count(DUTY_UPDATE) == 0);
    set_error = ESP_OK; update_error = ESP_FAIL; sync_local_lock(); CHECK(committed_duty == 1023);
    update_error = ESP_OK; sync_local_lock(); CHECK(committed_duty == 0);
    reported_requested_level = 0; awake = true; update_error = ESP_FAIL; draw_and_sync();
    CHECK(committed_duty == 0 && brightness == 10);
    update_error = ESP_OK; sync_local_lock(); CHECK(committed_duty == 1023);
    unsigned updates = count(DUTY_UPDATE); sync_local_lock(); CHECK(count(DUTY_UPDATE) == updates);
}
static void keyboard_rapid(void) {
    reported_requested_level = 2; now = 100;
    key_event(MIX_KEY_BACKLIGHT, NULL, 0, NULL);
    now = 110; key_event(MIX_KEY_BACKLIGHT, NULL, 0, NULL);
    now = 120; key_event(MIX_KEY_BACKLIGHT, NULL, 0, NULL);
    CHECK(light_feedback_pending && light_feedback_target == 5 && light_feedback_at == 120);
    CHECK(count(KBD_STEP) + count(KBD_SET) == 3 && count(FEEDBACK) == 0);
    CHECK(transmit_queued_command(true) == 5 && reported_requested_level == 2);
    reported_requested_level = 3; sync_local_lock(); CHECK(light_feedback_pending && count(FEEDBACK) == 0);
    reported_requested_level = 4; sync_local_lock(); CHECK(light_feedback_pending && count(FEEDBACK) == 0);
    reported_requested_level = 5; sync_local_lock(); expect_feedback(MIX_UI_KEYBOARD_LIGHT, 5);
    CHECK(!light_feedback_pending); sync_local_lock(); CHECK(count(FEEDBACK) == 1);
}
static void keyboard_stale_report(void) {
    reported_requested_level = 2; now = 100;
    adjust_keyboard_light(); CHECK(transmit_queued_command(true) == 3);
    /* A successful write does not refresh the validated report. Another key
     * arriving before report 3 must request 4, not send 3 a second time. */
    now = 120; adjust_keyboard_light(); CHECK(light_feedback_target == 4);
    CHECK(transmit_queued_command(true) == 4);
    reported_requested_level = 3; sync_local_lock(); CHECK(count(FEEDBACK) == 0);
    now = 140; adjust_keyboard_light(); CHECK(light_feedback_target == 5);
    CHECK(transmit_queued_command(true) == 5);
    reported_requested_level = 4; sync_local_lock(); CHECK(count(FEEDBACK) == 0);
    reported_requested_level = 5; sync_local_lock(); expect_feedback(MIX_UI_KEYBOARD_LIGHT, 5);
    CHECK(!light_feedback_pending);
}
static void keyboard_wrap(void) {
    reported_requested_level = 8; adjust_keyboard_light(); CHECK(light_feedback_target == 0);
    adjust_keyboard_light(); CHECK(light_feedback_target == 1);
    CHECK(transmit_queued_command(true) == 1);
    sync_local_lock(); CHECK(count(FEEDBACK) == 0);
    reported_requested_level = 1; sync_local_lock(); expect_feedback(MIX_UI_KEYBOARD_LIGHT, 1);
    reported_requested_level = 6; adjust_keyboard_light(); CHECK(light_feedback_target == 7);
}
static void keyboard_report(void) {
    reported_requested_level = 3; adjust_keyboard_light(); CHECK(light_feedback_target == 4);
    CHECK(transmit_queued_command(true) == 4); sync_local_lock();
    CHECK(reported_requested_level == 3 && count(FEEDBACK) == 0 && light_feedback_pending);
    /* Simulate the device's validated requested-level report, not PWM readback. */
    reported_requested_level = 4; sync_local_lock(); expect_feedback(MIX_UI_KEYBOARD_LIGHT, 4);
    CHECK(!light_feedback_pending);
}
static void keyboard_offline(void) {
    reported_requested_level = -1; adjust_keyboard_light();
    expect_feedback(MIX_UI_KEYBOARD_LIGHT, -1); CHECK(count(KBD_STEP) == 0 && !light_feedback_pending);
    reported_requested_level = 2; adjust_keyboard_light(); CHECK(light_feedback_pending);
    reported_requested_level = -1; sync_local_lock();
    CHECK(!light_feedback_pending && count(FEEDBACK) == 2); expect_feedback(MIX_UI_KEYBOARD_LIGHT, -1);
}
static void keyboard_timeout(void) {
    now = 100; reported_requested_level = 2; adjust_keyboard_light();
    now = 899; sync_local_lock(); CHECK(light_feedback_pending && count(FEEDBACK) == 0);
    /* Current production contract uses elapsed > 800: exactly 800 still waits. */
    now = 900; sync_local_lock(); CHECK(light_feedback_pending && count(FEEDBACK) == 0);
    now = 901; sync_local_lock(); expect_feedback(MIX_UI_KEYBOARD_LIGHT, -1);
    CHECK(!light_feedback_pending && count(FEEDBACK) == 1);
    reported_requested_level = 3; sync_local_lock(); CHECK(count(FEEDBACK) == 1);
}
static void keyboard_timeout_restart(void) {
    now = 100; reported_requested_level = 2; adjust_keyboard_light();
    now = 850; adjust_keyboard_light(); CHECK(light_feedback_at == 850 && light_feedback_target == 4);
    now = 901; sync_local_lock(); CHECK(light_feedback_pending && count(FEEDBACK) == 0);
    now = 1651; sync_local_lock(); CHECK(!light_feedback_pending); expect_feedback(MIX_UI_KEYBOARD_LIGHT, -1);
}
static void keyboard_timeout_wrap(void) {
    now = UINT32_MAX - 400; adjust_keyboard_light();
    now += 800; sync_local_lock(); CHECK(light_feedback_pending && count(FEEDBACK) == 0);
    ++now; sync_local_lock(); CHECK(!light_feedback_pending); expect_feedback(MIX_UI_KEYBOARD_LIGHT, -1);
}
static void ime_routing(void) {
    key_event(MIX_KEY_IME_TOGGLE,NULL,0,NULL);
    CHECK(count(UI_TEXT)==0&&count(LINK_TEXT)==0);
    terminal_visible=true;view.terminal_open=true;view.linux_online=true;
    view.running_app=MIX_APP_NOTES;
    key_event(MIX_KEY_IME_TOGGLE,NULL,0,NULL);
    CHECK(count(LINK_TEXT)==1&&link_len==sizeof(MIX_IME_TOGGLE_SEQUENCE)-1);
    CHECK(!memcmp(link_bytes,MIX_IME_TOGGLE_SEQUENCE,link_len));
    for(int guard=0;guard<8;guard++) {
        locked=guard==0;view.maintenance_busy=guard==1;view.ota_state=guard==2;
        view.linux_online=guard!=3;view.terminal_open=guard!=4;
        terminal_visible=guard!=5;view.running_app=guard==6?MIX_APP_SHELL:guard==7?MIX_APP_TRANSLATE:MIX_APP_NOTES;
        key_event(MIX_KEY_IME_TOGGLE,NULL,0,NULL);
        CHECK(count(LINK_TEXT)==1&&count(UI_TEXT)==0);
    }
    view.running_app=MIX_APP_NOTES;link_accepts=false;
    key_event(MIX_KEY_IME_TOGGLE,NULL,0,NULL);
    CHECK(count(NOTICE)==1);
}
static void app_back_routing(void) {
    app_back(MIX_APP_NOTES); CHECK(count(LINK_TEXT)==0);
    terminal_visible=true;view.terminal_open=true;view.linux_online=true;
    for(int app=0;app<MIX_APP_COUNT;app++) {
        view.running_app=(uint8_t)app;
        unsigned n=count(LINK_TEXT);app_back(app);
        if(app==MIX_APP_AGENT){CHECK(count(LINK_TEXT)==n);continue;}
        CHECK(count(LINK_TEXT)==n+1&&link_len==1);
        CHECK(link_bytes[0]==(app==MIX_APP_NOTES?0x11:0x1b));
    }
    view.running_app=MIX_APP_NOTES;
    for(int guard=0;guard<7;guard++) {
        unsigned n=count(LINK_TEXT);
        locked=guard==0;view.maintenance_busy=guard==1;view.ota_state=guard==2;
        view.linux_online=guard!=3;view.terminal_open=guard!=4;
        terminal_visible=guard!=5;
        app_back(guard==6?MIX_APP_SHELL:MIX_APP_NOTES);
        CHECK(count(LINK_TEXT)==n);
    }
    link_accepts=false;app_back(MIX_APP_NOTES);
    CHECK(count(NOTICE)==1&&strstr(notice,"unavailable"));
}
static void text_routing(void) {
    const uint8_t bytes[] = {'a', 0, 0xe4, 0xbd, 0xa0};
    key_event(MIX_KEY_TEXT, bytes, sizeof(bytes), NULL);
    CHECK(count(UI_TEXT) == 1 && count(LINK_TEXT) == 0 && ui_len == sizeof(bytes));
    CHECK(!memcmp(ui_bytes, bytes, sizeof(bytes)));
    terminal_visible = true; view.terminal_open = true;
    key_event(MIX_KEY_TEXT, bytes, sizeof(bytes), NULL);
    CHECK(count(LINK_TEXT) == 1 && link_len == sizeof(bytes) && !memcmp(link_bytes, bytes, sizeof(bytes)));
    view.maintenance_busy = true; key_event(MIX_KEY_TEXT, bytes, sizeof(bytes), NULL);
    CHECK(count(UI_TEXT) == 2 && count(LINK_TEXT) == 1);
    view.maintenance_busy = false; view.terminal_open = false;
    key_event(MIX_KEY_TEXT, bytes, sizeof(bytes), NULL); CHECK(count(UI_TEXT) == 3);
    view.terminal_open = true; link_accepts = false;
    key_event(MIX_KEY_TEXT, bytes, sizeof(bytes), NULL);
    CHECK(count(LINK_TEXT) == 2 && count(NOTICE) == 1 && !strcmp(notice, "Terminal input unavailable"));
}
static void touch_release(void) {
    seed_touch(); touch_frame = (gt911_touch_t){.count = 1, .x = 500, .y = 700}; poll_touch(116);
    touch_frame = (gt911_touch_t){0}; poll_touch(132);
    CHECK(touch_calls == 3 && sent_x == 500 && sent_y == 700 && !sent_down);
    touch_frame = (gt911_touch_t){.count = 0, .x = 999, .y = 999}; poll_touch(148);
    CHECK(sent_x == 500 && sent_y == 700 && !sent_down);
}
static void touch_error_case(void) {
    seed_touch(); touch_error = ESP_FAIL; poll_touch(116);
    CHECK(sent_x == 0 && sent_y == 0 && !sent_down && touch_x == 0 && touch_y == 0);
    CHECK(touch_calls==1 && touch_cancels==1); /* no synthetic touch-up event */
    touch_error = ESP_OK; touch_frame = (gt911_touch_t){0}; poll_touch(132);
    CHECK(sent_x == 0 && sent_y == 0 && !sent_down);
    touch_frame = (gt911_touch_t){.count = 1, .x = 42, .y = 66}; poll_touch(148);
    touch_frame = (gt911_touch_t){0}; poll_touch(164); CHECK(sent_x == 42 && sent_y == 66 && !sent_down);
    touch_error = ESP_ERR_TIMEOUT; poll_touch(180); CHECK(sent_x == 0 && sent_y == 0 && !sent_down);
}
static void touch_no_frame(void) {
    seed_touch(); poll_touch(115); CHECK(touch_reads == 1);
    touch_error = ESP_ERR_NOT_FOUND; poll_touch(116); CHECK(touch_calls == 1);
    poll_touch(600); CHECK(touch_calls == 1); /* elapsed == 500 */
    poll_touch(601); CHECK(touch_reads == 3); /* 16 ms polling guard */
    poll_touch(616); CHECK(touch_calls == 1 && touch_cancels == 0 && sent_x == 345 && sent_y == 678 && sent_down);
    poll_touch(1200); CHECK(touch_cancels == 0 && !tp_errors && !touch_resets);
    /* A stationary finger may produce no fresh frame before it moves again. */
    touch_error = ESP_OK; touch_frame = (gt911_touch_t){.count=1,.x=345,.y=450};
    poll_touch(1216); CHECK(sent_down && sent_y == 450 && touch_cancels == 0);
    touch_frame = (gt911_touch_t){0}; poll_touch(1232);
    CHECK(!sent_down && sent_x == 345 && sent_y == 450 && touch_calls == 3);
}
static void touch_retry_wrap(void) {
    tp = NULL; touch_init_error = ESP_FAIL; poll_touch(5000); CHECK(touch_inits == 0);
    poll_touch(5001); CHECK(touch_inits == 1 && touch_reads == 0 && last_tp_retry == 5001);
    touch_init_error = ESP_OK; poll_touch(10001); CHECK(touch_inits == 1);
    poll_touch(10002); CHECK(touch_inits == 2 && touch_reads == 1);
    last_tp_poll = UINT32_MAX - 15;
    touch_x = 444; touch_y = 666; tp_down=true; touch_error = ESP_ERR_NOT_FOUND;
    unsigned before = touch_cancels; poll_touch(0); CHECK(touch_reads == 2 && touch_cancels == before);
    poll_touch(201); CHECK(touch_cancels == before && tp_down && touch_x == 444 && touch_y == 666);
}

static void touch_recovery(void) {
    seed_touch(); touch_error=ESP_ERR_TIMEOUT;
    poll_touch(116); poll_touch(132); CHECK(tp_errors==2 && touch_calls==1 && touch_cancels==2);
    touch_error=ESP_ERR_NOT_FOUND; poll_touch(148); CHECK(tp_errors==0 && touch_resets==0);
    touch_error=ESP_FAIL; poll_touch(164); poll_touch(180); poll_touch(196);
    CHECK(tp_errors==3 && tp && touch_resets==0);
    unsigned reads=touch_reads; poll_touch(5000); CHECK(touch_reads==reads);
    touch_error=ESP_OK; touch_frame=(gt911_touch_t){0}; poll_touch(5001);
    CHECK(touch_removes==1 && touch_resets==1 && touch_inits==1 && tp_errors==0 && tp);
    CHECK(touch_reads==reads+1 && !sent_down);
}
static void touch_recovery_failures(void) {
    tp_errors=3; touch_remove_error=ESP_FAIL; poll_touch(5001);
    CHECK(tp && touch_removes==1 && touch_resets==0 && touch_reads==0);
    poll_touch(10001); CHECK(touch_removes==1); /* five-second retry limit */
    touch_remove_error=ESP_OK; touch_reset_error=ESP_FAIL; poll_touch(10002);
    CHECK(!tp && touch_removes==2 && touch_resets==1 && touch_inits==0);
    touch_reset_error=ESP_OK; touch_init_error=ESP_FAIL; poll_touch(15003);
    CHECK(!tp && touch_inits==1 && touch_reads==0);
    touch_init_error=ESP_OK; poll_touch(20004);
    CHECK(tp && touch_inits==2 && touch_reads==1 && tp_errors==0);
}

static void wait_sample(uint32_t ms,int contacts,int y,esp_err_t error) {
    now=ms;touch_frame=(gt911_touch_t){.count=contacts,.x=500,.y=y};touch_error=error;
    touch_wait_sample(NULL);
}
static void touch_wait_edges(void) {
    wait_sample(100,1,650,ESP_OK);
    wait_sample(116,1,450,ESP_OK);
    wait_sample(132,0,999,ESP_OK);
    CHECK(!touch_calls && !touch_cancels && touch_count==3);
    dispatch_touch();
    CHECK(touch_calls==3 && !touch_count);
    CHECK(touch_sent[0].down && touch_sent[0].y==650);
    CHECK(touch_sent[1].down && touch_sent[1].y==450);
    CHECK(!touch_sent[2].down && touch_sent[2].y==450);
    /* Another complete tap in the same wait must remain a separate gesture. */
    wait_sample(148,1,200,ESP_OK);wait_sample(164,0,0,ESP_OK);dispatch_touch();
    CHECK(touch_calls==5 && touch_sent[3].down && !touch_sent[4].down);
}
static void touch_wait_fault(void) {
    wait_sample(100,1,650,ESP_OK);
    wait_sample(116,0,0,ESP_FAIL);
    wait_sample(132,0,0,ESP_FAIL);
    wait_sample(148,0,0,ESP_FAIL);
    wait_sample(6000,0,0,ESP_OK);
    CHECK(tp_errors==3 && !touch_resets && !touch_inits && !touch_calls && !touch_cancels);
    dispatch_touch();CHECK(touch_calls==1 && touch_cancels==3);
    poll_touch(6001);CHECK(touch_inits==1 && touch_resets==1 && !sent_down);
}
static void touch_wait_overflow(void) {
    for(unsigned i=0;i<TOUCH_QUEUE_CAP+2;i++)wait_sample(100+i*16,1,600-(int)i,ESP_OK);
    CHECK(!touch_cancels && !touch_calls && touch_count==3);
    wait_sample(100+(TOUCH_QUEUE_CAP+2)*16,0,999,ESP_OK);
    dispatch_touch();CHECK(touch_cancels==1 && touch_calls==3 && !sent_down && !touch_count);
}
static void nested_sample(void) {wait_sample(116,0,999,ESP_OK);}
static void touch_wait_reentrant(void) {
    wait_sample(100,1,650,ESP_OK);during_touch=nested_sample;
    dispatch_touch();
    CHECK(touch_calls==1 && sent_down && touch_count==1);
    dispatch_touch();CHECK(touch_calls==2 && !sent_down && !touch_count && sent_y==650);
}

int main(int argc, char **argv) {
    struct scenario { const char *name; void (*run)(void); } scenarios[] = {
        {"brightness_bounds", brightness_bounds}, {"brightness_set_failure", brightness_set_failure},
        {"brightness_update_failure", brightness_update_failure}, {"volume_failure", volume_failure},
        {"volume_bounds", volume_bounds}, {"lock_immediate", lock_immediate}, {"lock_restore", lock_restore},
        {"lock_reconnect", lock_reconnect}, {"lock_offline", lock_offline}, {"locked_keys", locked_keys},
        {"wake_draw_gate", wake_draw_gate}, {"lock_ledc_retry", lock_ledc_retry},
        {"keyboard_rapid", keyboard_rapid}, {"keyboard_stale_report", keyboard_stale_report},
        {"keyboard_wrap", keyboard_wrap},
        {"keyboard_report", keyboard_report}, {"keyboard_offline", keyboard_offline},
        {"keyboard_timeout", keyboard_timeout}, {"keyboard_timeout_restart", keyboard_timeout_restart},
        {"ime_routing", ime_routing}, {"app_back_routing", app_back_routing},
        {"keyboard_timeout_wrap", keyboard_timeout_wrap}, {"text_routing", text_routing},
        {"touch_wait_edges", touch_wait_edges}, {"touch_wait_fault", touch_wait_fault},
        {"touch_wait_overflow", touch_wait_overflow}, {"touch_wait_reentrant", touch_wait_reentrant},
        {"touch_release", touch_release}, {"touch_error", touch_error_case},
        {"touch_no_frame", touch_no_frame}, {"touch_retry_wrap", touch_retry_wrap},
        {"touch_recovery", touch_recovery}, {"touch_recovery_failures", touch_recovery_failures},
    };
    CHECK(argc == 2);
    for (unsigned i = 0; i < sizeof(scenarios) / sizeof(scenarios[0]); ++i) {
        if (!strcmp(argv[1], scenarios[i].name)) {
            scenarios[i].run(); printf("PASS %s\n", argv[1]); return 0;
        }
    }
    fprintf(stderr, "Unknown scenario: %s\n", argv[1]); return 2;
}
