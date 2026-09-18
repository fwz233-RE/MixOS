/* Host-only UI integration harness. Built by test_preview_ui.py with SDK stubs.
 *
 * The firmware UI is included as a translation unit so the test can inspect the
 * page, section and passphrase state directly. Everything below the UI - the
 * panel, NVS, the font, the battery log and the USB link - is replaced by a
 * stub that records what the UI asked for, so a drawing mistake shows up as a
 * failed assertion instead of a corrupted panel.
 */
#include <assert.h>
#include <stdlib.h>
#include <string.h>
#include "../firmware/esp32s3/main/mix_ui.c"

static unsigned draws, full_draws, allocations;
static int last_y0, last_y1;
static long presented_rows;          /* rows handed to the panel since the last reset */
static uint16_t *guarded;
static bool fake_font = true;
static int cell_w_seen, cell_h_seen, cell_size_seen;

/* ---------- platform stubs ---------- */
void *heap_caps_malloc(size_t n, unsigned caps) {
    (void)caps; allocations++; assert(n == 1572864);
    guarded = calloc(1, n + 4); assert(guarded);
    guarded[0] = 0xa55a; guarded[n / 2 + 1] = 0x5aa5;
    return guarded + 1;
}
esp_err_t esp_lcd_panel_draw_bitmap(esp_lcd_panel_handle_t p, int x0, int y0, int x1, int y1,
                                    const void *pixels) {
    assert(p); assert(x0 == 0 && x1 == W && y0 >= 0 && y1 <= H && y1 > y0);
    assert(pixels == fb + (size_t)y0 * W);
    assert(guarded[0] == 0xa55a && guarded[786433] == 0x5aa5);
    draws++; presented_rows += y1 - y0;
    if (y0 == 0 && y1 == H) full_draws++;
    last_y0 = y0; last_y1 = y1; return ESP_OK;
}

/* The status bar prints the first four bytes of the ELF hash so the running
 * firmware can be told apart on the device. Fixed values here: the test
 * asserts layout, not which binary was compiled. */
const esp_app_desc_t *esp_app_get_description(void) {
    static const esp_app_desc_t desc = { .date = "Jan  1 2026", .time = "00:00:00",
                                         .app_elf_sha256 = { 0xe7, 0xcc, 0x4d, 0xe3 } };
    return &desc;
}

/* A faithful little NVS: keys start absent, so the UI's own defaults are
 * actually exercised, and a value the UI writes is read back the way the real
 * flash-backed store would return it. */
typedef struct { char key[16]; uint8_t value; bool present; } nvs_entry_t;
static nvs_entry_t nvs_store[8], nvs_staged[8];
esp_err_t nvs_open(const char *ns, int mode, nvs_handle_t *h) {
    assert(strcmp(ns, "mixui") == 0); (void)mode; *h = 1; return ESP_OK;
}
esp_err_t nvs_get_u8(nvs_handle_t h, const char *key, uint8_t *v) {
    (void)h;
    for (unsigned i = 0; i < 8; i++)
        if (nvs_store[i].present && !strcmp(nvs_store[i].key, key)) { *v = nvs_store[i].value; return ESP_OK; }
    return ESP_ERR_NVS_NOT_FOUND;
}
esp_err_t nvs_set_u8(nvs_handle_t h, const char *key, uint8_t v) {
    (void)h;
    for (unsigned i = 0; i < 8; i++)
        if (!nvs_staged[i].present || !strcmp(nvs_staged[i].key, key)) {
            snprintf(nvs_staged[i].key, sizeof(nvs_staged[i].key), "%s", key);
            nvs_staged[i].value = v; nvs_staged[i].present = true; return ESP_OK;
        }
    return ESP_ERR_NO_MEM;
}
esp_err_t nvs_commit(nvs_handle_t h) {
    (void)h;
    for (unsigned i = 0; i < 8; i++) if (nvs_staged[i].present) nvs_store[i] = nvs_staged[i];
    return ESP_OK;
}
void nvs_close(nvs_handle_t h) { (void)h; }

bool ttf_font_ready(void) { return fake_font; }
int ttf_text_width(int size, const char *s) { (void)size; return (int)strlen(s) * 12; }
int ttf_draw_text(uint16_t *buf, int w, int h, int x, int y, int size, uint16_t color, const char *s) {
    (void)size; (void)s;
    assert(buf == fb && w == W && h == H);
    if (x >= 0 && x < w && y >= 0 && y < h) buf[y * w + x] = color;
    return 12;
}
void ttf_draw_cell(uint16_t *buf, int w, int h, int x, int y, int cell_w, int cell_h, int size,
                   uint16_t color, uint32_t cp, bool bold) {
    (void)cp; (void)bold;
    assert(buf == fb && w == W && h == H);
    /* The cell must match the geometry preset the UI claims to be using, and
     * must never reach outside the framebuffer. */
    assert(cell_w == cell_w_seen || cell_w == 2 * cell_w_seen);
    assert(cell_h == cell_h_seen && size == cell_size_seen);
    assert(x >= 0 && x + cell_w <= w && y >= 0 && y + cell_h <= h);
    buf[y * w + x] = color;
}
/* The icon path renders a real glyph bitmap. A solid square is enough: the test
 * cares that the icon is centred inside the framebuffer, not what it depicts. */
bool ttf_font_glyph(uint32_t cp, int size, ttf_glyph_t *out) {
    (void)cp;
    if (!fake_font || size <= 0) return false;
    static uint8_t alpha[64 * 64];
    int side = size > 64 ? 64 : size;
    memset(alpha, 0xff, (size_t)side * side);
    *out = (ttf_glyph_t){ .bitmap = alpha, .w = (int16_t)side, .h = (int16_t)side,
                          .left = 0, .top = (int16_t)side, .advance = (int16_t)side };
    return true;
}
int batt_log_get(batt_sample_t *out, int max) {
    assert(max == BATT_LOG_CAP);
    for (int i = 0; i < max; i++)
        out[i] = (batt_sample_t){.mv = (uint16_t)(3000 + i), .ma = (int16_t)(i - 360),
                                 .soc = (int8_t)(i % 101), .plugged = 1};
    return max;
}

/* The link is a stub: the UI may only read network state, never reach past it. */
static mix_net_entry_t networks[MIX_NET_MAX];
static int network_count;
static bool net_busy;
static char net_message[96];
int mix_link_net_list(const mix_net_entry_t **out) { *out = networks; return network_count; }
bool mix_link_net_busy(void) { return net_busy; }
const char *mix_link_net_message(void) { return net_message; }

/* ---------- helpers ---------- */
/* The UI refreshes its telemetry snapshot once a second, so a test that wants
 * a changed view to be visible has to let that much time pass. */
static uint32_t clk = 10000;
static uint32_t step(uint32_t ms) { clk += ms; return clk; }
static void tap(int x, int y) { mix_ui_touch(x, y, true); mix_ui_touch(x, y, false); }
/* Reports which call site saw the stray action, and what it was. A bare assert
 * here only ever named this line, which says nothing about the cause. */
static void no_action_at(int line) {
    mix_action_t a;
    if (mix_ui_take_action(&a)) {
        fprintf(stderr, "unexpected action kind=%d value=%d (called from line %d)\n",
                (int)a.kind, (int)a.value, line);
        assert(0);
    }
}
#define no_action() no_action_at(__LINE__)
static void drain(void) { mix_action_t a; while (mix_ui_take_action(&a)) {} }
static void note_geometry(void) {
    cell_w_seen = geom()->cw; cell_h_seen = geom()->ch; cell_size_seen = geom()->font;
}
/* Adopt the caller's view and let the pending repaint be drawn and presented. */
static void settle(mix_view_t *v) {
    step(1200); repaint = true;
    for (int i = 0; i < 12; i++) mix_ui_tick(v, step(1));
    assert(!repaint);
}
static void type(const char *s) {
    for (const char *p = s; *p; p++) mix_ui_key((const uint8_t *)p, 1);
}

int main(void) {
    mix_terminal_init();
    assert(mix_ui_init((void *)1) == ESP_OK);
    assert(allocations == 1);
    note_geometry();
    /* The readable preset is the default on a 3.2 inch panel, and the parser
     * must already agree with it before a single frame is drawn. A stored
     * preference of zero is a real value, so only a missing key may fall back. */
    assert(geometry == 1 && mix_terminal_cols() == 64 && mix_terminal_rows() == 22);

    mix_view_t v = {0};
    mix_ui_tick(&v, step(1));
    assert(full_draws == 1);
    /* Nothing changed, so nothing is redrawn. */
    unsigned n = draws;
    mix_ui_tick(&v, step(50)); mix_ui_tick(&v, step(1000));
    assert(draws == n);

    /* Every page, section, theme and language has to draw inside the buffer. */
    for (int th = 0; th < 4; th++)
        for (int lang = 0; lang < 2; lang++)
            for (int p = 0; p < 3; p++)
                for (int s = 0; s < SEC_COUNT; s++) {
                    theme = (uint8_t)th; language = (uint8_t)lang;
                    page = (page_t)p; section = (section_t)s;
                    settle(&v);
                }
    theme = 0; language = 0;

    /* A page change costs exactly one full present. It used to be revealed as a
     * six-band wipe; the bands read as tearing on a 28 Hz panel, so the wipe was
     * removed. The budget assertion outlives it: one navigation, one screen. */
    page = PAGE_HOME; section = SEC_APPEARANCE; settle(&v);
    presented_rows = 0;
    navigate(PAGE_SETTINGS);
    for (int i = 0; i < 12 && repaint; i++) mix_ui_tick(&v, step(1));
    assert(!repaint && presented_rows == H);

    /* ---- launcher ---- */
    mix_action_t a;
    page = PAGE_HOME; settle(&v); drain();
    /* Press, then release inside: the card is drawn inset, presented on its
     * own band, and the release starts exactly one application. */
    int cx, cy; card_rect(0, &cx, &cy);
    mix_ui_touch(cx + 20, cy + 20, true);
    assert(pressed_card == 0 && pressed_inside && last_y0 == cy && last_y1 == cy + CARD_H);
    mix_ui_touch(cx + 20, cy + 20, false);
    assert(mix_ui_take_action(&a) && a.kind == MIX_ACTION_APP_OPEN && a.value == MIX_APP_TRANSLATE);
    assert(page == PAGE_APP && current_app == MIX_APP_TRANSLATE);
    no_action();

    /* A finger that leaves the card starts nothing. The touch driver reports a
     * failed read as (0, 0, up), so the decision is made while still down. */
    page = PAGE_HOME; settle(&v); drain();
    card_rect(1, &cx, &cy);
    mix_ui_touch(cx + 20, cy + 20, true); assert(pressed_card == 1 && pressed_inside);
    mix_ui_touch(cx + 20, cy + CARD_H + 40, true); assert(!pressed_inside);
    mix_ui_touch(0, 0, false);
    assert(pressed_card < 0 && page == PAGE_HOME);
    no_action();

    /* Keyboard focus moves between the four cards; Enter launches the focused
     * one, and the settings card navigates without starting anything. */
    page = PAGE_HOME; home_focus = 0; settle(&v); drain();
    mix_ui_key((const uint8_t *)"\033[C", 3); assert(home_focus == 1);
    mix_ui_key((const uint8_t *)"\033[B", 3); assert(home_focus == 3);
    mix_ui_key((const uint8_t *)"\r", 1);
    assert(page == PAGE_SETTINGS); no_action();
    page = PAGE_HOME; settle(&v);
    mix_ui_key((const uint8_t *)"2", 1);
    assert(mix_ui_take_action(&a) && a.kind == MIX_ACTION_APP_OPEN && a.value == MIX_APP_NOTES);

    /* The agent card is a placeholder: it opens a screen and starts nothing. */
    page = PAGE_HOME; settle(&v); drain();
    mix_ui_key((const uint8_t *)"3", 1);
    assert(page == PAGE_APP && current_app == MIX_APP_AGENT);
    no_action();
    settle(&v);
    /* Keyboard input must not be routed at a screen that has no session. */
    assert(!mix_ui_terminal_visible());
    mix_ui_key((const uint8_t *)"\033", 1); assert(page == PAGE_HOME);

    /* Leaving an application ends it. Without this the session outlived the
     * screen showing it, the next card opened was refused by the host with
     * "terminal already open", and the old application stayed on screen under
     * the new one's title. */
    page = PAGE_HOME; v.terminal_open = false; settle(&v); drain();
    mix_ui_key((const uint8_t *)"2", 1);
    assert(mix_ui_take_action(&a) && a.kind == MIX_ACTION_APP_OPEN && a.value == MIX_APP_NOTES);
    assert(page == PAGE_APP && current_app == MIX_APP_NOTES);
    v.terminal_open = true; v.running_app = MIX_APP_NOTES; settle(&v);
    mix_ui_home_toggle();
    assert(page == PAGE_HOME);
    assert(mix_ui_take_action(&a) && a.kind == MIX_ACTION_TERMINAL_CLOSE);
    no_action();
    /* A screen with no session has nothing to close, so leaving it is silent. */
    v.terminal_open = false; page = PAGE_HOME; settle(&v); drain();
    mix_ui_key((const uint8_t *)"3", 1); assert(page == PAGE_APP);
    settle(&v);
    mix_ui_home_toggle(); assert(page == PAGE_HOME); no_action();

    /* ---- terminal rendering in both geometry presets ---- */
    const uint8_t sample[] = "ASCII \xe4\xb8\xad\xe6\x96\x87 \033[31mred\033[38;2;10;20;30mrgb\033[0m";
    for (int preset = 0; preset < GEOMETRY_COUNT; preset++) {
        page = PAGE_HOME; settle(&v); drain();
        set_geometry(preset);
        assert(geometry == preset);
        assert(mix_terminal_cols() == geometries[preset].cols);
        note_geometry(); drain();
        current_app = MIX_APP_SHELL; page = PAGE_APP;
        v.terminal_open = true; v.running_app = MIX_APP_SHELL;
        settle(&v);
        assert(view.terminal_open);
        mix_terminal_feed(sample, sizeof(sample) - 1);
        n = full_draws;
        mix_ui_tick(&v, step(60));
        /* Only the changed row is presented; the page is not repainted. */
        assert(full_draws == n);
        assert(last_y1 - last_y0 == geometries[preset].ch);
        assert(mix_ui_terminal_visible());
    }
    assert(mix_terminal_row(0)[6].width == 2 && mix_terminal_row(0)[7].width == 0);

    /* ---- a notice and terminal output can never approve anything ---- */
    mix_ui_notice("Host update ready; waiting for boot request");
    mix_ui_tick(&v, step(60));
    assert(!modal && mix_ui_terminal_visible()); no_action();
    mix_ui_notice("Enter YES \033[confirm]");
    mix_terminal_feed((const uint8_t *)"YES\r\n", 5);
    mix_ui_tick(&v, step(60));
    mix_ui_key((const uint8_t *)"\033[A", 3);
    mix_ui_key((const uint8_t *)"\r", 1);
    assert(!modal && mix_ui_terminal_visible()); no_action();
    v.terminal_open = false;

    /* ---- the A/B transfer panel moves between telemetry snapshots ---- */
    page = PAGE_SETTINGS; section = SEC_SYSTEM; settle(&v);
    n = full_draws;
    v.ota_state = 1; v.ota_percent = 7;  mix_ui_tick(&v, step(20)); assert(full_draws == n + 1);
    v.ota_percent = 63;                  mix_ui_tick(&v, step(20)); assert(full_draws == n + 2);
    mix_ui_tick(&v, step(20));           assert(full_draws == n + 2); /* same percent draws nothing */
    /* Progress on another page must not steal a repaint. */
    page = PAGE_HOME; settle(&v); n = full_draws;
    v.ota_percent = 88; mix_ui_tick(&v, step(20)); assert(full_draws == n);
    v.ota_state = 0; v.ota_percent = 0;

    /* ---- Wi-Fi: the passphrase is local, sent once and then erased ---- */
    page = PAGE_SETTINGS; go_section(SEC_NETWORK);
    network_count = 2;
    snprintf(networks[0].ssid, sizeof(networks[0].ssid), "%s", "cafe");
    networks[0].signal = 72; networks[0].secured = true; networks[0].known = false;
    snprintf(networks[1].ssid, sizeof(networks[1].ssid), "%s", "lab");
    networks[1].signal = 40; networks[1].secured = false; networks[1].known = true;
    settle(&v); drain();
    tap(BODY_X + 40, 192);                       /* the first listed network */
    assert(net_view == 1 && strcmp(net_ssid, "cafe") == 0);
    type("hunter2");
    assert(net_pass_len == 7 && strcmp(net_pass, "hunter2") == 0);
    settle(&v);                                  /* the dots have to fit and draw */
    mix_ui_key((const uint8_t *)"\r", 1);
    assert(mix_ui_take_action(&a) && a.kind == MIX_ACTION_NET_CONNECT);
    /* The parent reads it after taking the action, exactly once. */
    assert(strcmp(mix_ui_net_passphrase(), "hunter2") == 0);
    assert(strcmp(mix_ui_net_ssid(), "cafe") == 0);
    mix_ui_tick(&v, step(20));
    assert(net_pass_len == 0 && mix_ui_net_passphrase()[0] == 0);
    /* Escape abandons the entry and leaves nothing behind. */
    net_view = 0; settle(&v);
    tap(BODY_X + 40, 192); type("secret");
    mix_ui_key((const uint8_t *)"\033", 1);
    assert(net_view == 0 && net_pass_len == 0 && net_pass[0] == 0);
    no_action();
    /* Forgetting a saved network asks for local consent first. */
    settle(&v);
    tap(BODY_X + 40, 192);
    tap(BODY_X + 392, 500);
    assert(modal == MIX_ACTION_NET_FORGET); no_action();
    settle(&v);
    mix_ui_key((const uint8_t *)"\033", 1);      /* rejected */
    assert(!modal); no_action();
    net_view = 0; network_count = 0;

    /* ---- maintenance still needs local consent ---- */
    go_section(SEC_SYSTEM); settle(&v); drain();
    tap(BODY_X + 40, 570); assert(modal == MIX_ACTION_JOB_START); no_action();
    settle(&v);
    mix_ui_key((const uint8_t *)"\r", 1);
    assert(mix_ui_take_action(&a) && a.kind == MIX_ACTION_JOB_START);

    /* ---- the touch test repaints only its own rectangle ---- */
    go_section(SEC_DEVICE); settle(&v); drain();
    tap(BODY_X + BODY_W - 100, 470); assert(touch_test);
    settle(&v);
    mix_ui_touch(BODY_X + 120, 600, true);
    mix_ui_tick(&v, step(60));
    assert(last_y0 == 436 && last_y1 == 724);
    mix_ui_touch(BODY_X + 120, 600, false);
    tap(BODY_X + BODY_W - 100, 470); assert(!touch_test);

    /* ---- a missing font partition must still draw every page ---- */
    fake_font = false;
    for (int p = 0; p < 3; p++)
        for (int s = 0; s < SEC_COUNT; s++) {
            page = (page_t)p; section = (section_t)s; settle(&v);
        }

    assert(allocations == 1);
    assert(guarded[0] == 0xa55a && guarded[786433] == 0x5aa5);
    free(guarded);
    puts("UI host integration passed: 120 page combinations, present budget, launcher press/cancel, "
         "both terminal geometries, Wi-Fi passphrase lifetime, local consent, framebuffer guards");
    return 0;
}
