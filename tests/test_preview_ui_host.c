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
#include "../firmware/esp32s3/main/mix_terminal.h"
static void recorded_terminal_clean(void);
#define mix_terminal_clean recorded_terminal_clean
#include "../firmware/esp32s3/main/mix_ui.c"
#undef mix_terminal_clean

static unsigned terminal_cleans;
static void recorded_terminal_clean(void) { terminal_cleans++; mix_terminal_clean(); }
static bool record_text, layout_metrics;
static unsigned text_count, glyph_count, width_calls;
static struct { int x, y, size, width; uint16_t color; char value[160]; } text_calls[128];
static unsigned draws, full_draws, deferred_draws, allocations, cell_draws;
static uint32_t clk = 10000, fake_present_ms;
static unsigned long long presented_pixels;
static int last_x0,last_x1;
static int last_y0, last_y1;
static long presented_rows;          /* rows handed to the panel since the last reset */
static uint16_t *guarded;
static bool fake_font = true;
static esp_err_t next_draw_error;
static int cell_w_seen, cell_h_seen, cell_size_seen;
static bool fake_fade, fake_deferred_pending;
static unsigned last_rect_count;
static mix_present_rect_t last_rects[MIX_PRESENT_MAX_RECTS];
static unsigned fade_steps;
static uint16_t frozen[W*H], screen[W*H], checkpoint[W*H], fade_background;
static unsigned fade_spans;

/* ---------- platform stubs ---------- */
void *heap_caps_malloc(size_t n, unsigned caps) {
    (void)caps; allocations++; assert(n == 1572864);
    guarded = calloc(1, n + 4); assert(guarded);
    guarded[0] = 0xa55a; guarded[n / 2 + 1] = 0x5aa5;
    return guarded + 1;
}
int64_t esp_timer_get_time(void){return (int64_t)clk*1000;}
esp_err_t mix_present_get_stats(mix_present_stats_t *out) {
    assert(out);memset(out,0,sizeof(*out));out->frame_count=clk/35;out->present_count=draws;return ESP_OK;
}
esp_err_t mix_present_init(esp_lcd_panel_handle_t p){assert(p);return ESP_OK;}
esp_err_t mix_present_rects(const mix_present_rect_t *regions,unsigned count,const uint16_t *pixels) {
    assert(regions&&count>0&&count<=MIX_PRESENT_MAX_RECTS);
    assert(pixels==fb&&guarded[0]==0xa55a&&guarded[786433]==0x5aa5);
    int x0=W,x1=0,y0=H,y1=0;
    for(unsigned i=0;i<count;i++) {
        const mix_present_rect_t *r=&regions[i];
        assert(r->x0>=0&&r->x1<=W&&r->x1>r->x0&&r->y0>=0&&r->y1<=H&&r->y1>r->y0);
        if(r->x0<x0)x0=r->x0;
        if(r->x1>x1)x1=r->x1;
        if(r->y0<y0)y0=r->y0;
        if(r->y1>y1)y1=r->y1;
        presented_rows+=r->y1-r->y0;
        presented_pixels+=(unsigned long long)(r->x1-r->x0)*(r->y1-r->y0);
    }
    draws++;
    if(count==1&&x0==0&&x1==W&&y0==0&&y1==H)full_draws++;
    if(next_draw_error!=ESP_OK) {
        esp_err_t error=next_draw_error;next_draw_error=ESP_OK;return error;
    }
    mix_present_fade_end();clk+=fake_present_ms;
    for(unsigned i=0;i<count;i++) {
        const mix_present_rect_t *r=&regions[i];
        for(int y=r->y0;y<r->y1;y++)memcpy(screen+y*W+r->x0,fb+y*W+r->x0,(r->x1-r->x0)*2);
    }
    fake_deferred_pending=false;
    last_rect_count=count;memcpy(last_rects,regions,count*sizeof(*regions));
    last_x0=x0;last_x1=x1;last_y0=y0;last_y1=y1;return ESP_OK;
}
esp_err_t mix_present_rects_deferred(const mix_present_rect_t *regions,unsigned count,const uint16_t *pixels) {
    deferred_draws++;
    esp_err_t result=mix_present_rects(regions,count,pixels);
    if(result==ESP_OK)fake_deferred_pending=true;
    return result;
}
esp_err_t mix_present_rect(int x0,int y0,int x1,int y1,const uint16_t *pixels) {
    const mix_present_rect_t r={x0,y0,x1,y1};return mix_present_rects(&r,1,pixels);
}

esp_err_t mix_present_fade_begin(const uint16_t *pixels, uint16_t background) {
    assert(pixels == fb && !fake_fade && !fake_deferred_pending);
    memcpy(frozen,pixels,sizeof(frozen));fade_background=background;fade_spans=0;
    assert(CONTENT_H == 648 && STATUS_Y == 648 && STATUS_H == 120);
    assert(!memcmp(screen+STATUS_Y*W,fb+STATUS_Y*W,STATUS_H*W*2));
    bool outgoing=!memcmp(screen,fb,sizeof(screen));
    for(int y=0;y<CONTENT_H;y++) {
        int first=W,last=0;
        for(int x=0;x<W;x++) {
            if(!outgoing)assert(screen[y*W+x]==background);
            if(fb[y*W+x]!=background){if(first==W)first=x;last=x+1;}
        }
        if(last)fade_spans+=(unsigned)(last-first);
    }
    fake_fade = true; return ESP_OK;
}
esp_err_t mix_present_fade_step(uint8_t opacity) {
    assert(fake_fade);
    /* A byte-for-byte invariant catches ANY canvas write during a fade,
     * including status updates, touch handlers and terminal dirty-row draws. */
    assert(!memcmp(frozen,fb,sizeof(frozen)));
    fade_steps++; draws++; presented_rows += CONTENT_H;
    presented_pixels += fade_spans;
    last_x0=0; last_x1=W; last_y0=0; last_y1=CONTENT_H;
    if (next_draw_error != ESP_OK) {
        esp_err_t error = next_draw_error; next_draw_error = ESP_OK; return error;
    }
    for(int i=0;i<CONTENT_H*W;i++) {
        unsigned fg=frozen[i],bg=fade_background,a=opacity;
        unsigned r=(((fg>>11)*a+(bg>>11)*(255-a)+127)/255)<<11;
        unsigned g=((((fg>>5)&63)*a+((bg>>5)&63)*(255-a)+127)/255)<<5;
        unsigned b=((fg&31)*a+(bg&31)*(255-a)+127)/255;
        screen[i]=(uint16_t)(r|g|b);
    }
    clk += fake_present_ms; return ESP_OK;
}
void mix_present_fade_end(void) { fake_fade = false; }

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
int ttf_text_width(int size, const char *s) {
    if(record_text)width_calls++;
    if(!layout_metrics)return (int)strlen(s)*12;
    /* Size-aware, deliberately wide Latin and full-width CJK estimates.
     * This checks layout contracts, not the appearance of an installed font. */
    int width=0;
    for(const unsigned char *p=(const unsigned char *)s;*p;p++) {
        if((*p&0xc0)==0x80)continue;
        width+=*p>=0x80?size:(size*62+99)/100;
    }
    return width;
}
int ttf_draw_text(uint16_t *buf, int w, int h, int x, int y, int size, uint16_t color, const char *s) {
    (void)size;
    if (record_text) {
        assert(text_count < sizeof(text_calls) / sizeof(text_calls[0]));
        text_calls[text_count].x = x; text_calls[text_count].y = y;
        text_calls[text_count].size=size; text_calls[text_count].width=ttf_text_width(size,s);
        text_calls[text_count].color=color;
        snprintf(text_calls[text_count++].value, sizeof(text_calls[0].value), "%s", s);
    }
    assert(buf == fb && w == W && h>0 && h<=H);
    assert(h == draw_limit_y);
    if (x >= 0 && x < w && y >= draw_clip_y0 && y < h) buf[y * w + x] = color;
    return 12;
}
int ttf_draw_text_clipped(uint16_t *buf, int w, int h, int clip_y0, int clip_y1,
                          int x, int y, int size, uint16_t color, const char *s) {
    assert(h==H&&clip_y0==draw_clip_y0&&clip_y1==draw_limit_y);
    return ttf_draw_text(buf,w,clip_y1,x,y,size,color,s);
}
void ttf_draw_cell(uint16_t *buf, int w, int h, int x, int y, int cell_w, int cell_h, int size,
                   uint16_t color, uint32_t cp, bool bold) {
    (void)cp; (void)bold; cell_draws++;
    assert(buf == fb && w == W && h == H);
    /* The cell must match the geometry preset the UI claims to be using, and
     * must never reach outside the framebuffer. */
    if(page==PAGE_APP&&current_app==MIX_APP_NOTES) {
        assert(cell_w==21||cell_w==42);
        assert(cell_h==40&&size==34);
        assert(mix_terminal_cols()==48&&mix_terminal_rows()==16);
    } else {
        assert(cell_w == cell_w_seen || cell_w == 2 * cell_w_seen);
        assert(cell_h == cell_h_seen && size == cell_size_seen);
    }
    assert(x >= 0 && x + cell_w <= w && y >= 0 && y + cell_h <= CONTENT_H);
    buf[y * w + x] = color;
}
/* The icon path renders a real glyph bitmap. A solid square is enough: the test
 * cares that the icon is centred inside the framebuffer, not what it depicts. */
bool ttf_font_glyph(uint32_t cp, int size, ttf_glyph_t *out) {
    (void)cp;
    if (record_text) glyph_count++;
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
static uint32_t step(uint32_t ms) { clk += ms; return clk; }
static void tap(int x, int y) { mix_ui_touch(x, y, true); mix_ui_touch(x, y, false); }
/* Fixed-page coordinates are screen coordinates: no synthetic scroll/reveal. */
static void settings_tap(int x, int y) {
    assert(page == PAGE_SETTINGS && x >= 0 && x < W && y >= 0 && y < CONTENT_H);
    tap(x, y);
}
static void theme_tap(int index) {
    settings_tap(SET_THEME_X0 + index % SET_THEME_COLS * (SET_THEME_W + SET_THEME_GAP_X) + SET_THEME_W / 2,
                 SET_THEME_Y0 + index / SET_THEME_COLS * (SET_THEME_H + SET_THEME_GAP_Y) + SET_THEME_H / 2);
}
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
    /* Legacy page-layout tests assign page directly; real navigation cancels
     * the old transition and clears the waiting flag. Match that for Home. */
    if(page==PAGE_HOME) {
        if(launch_motion.active)launch_motion_end(true);
        content_cancel();content_motion.waiting=false;
    }
    if(launch_motion.active&&launch_motion.pending)mix_ui_tick(v,clk);
    step(1200); repaint = true;
    for (int i = 0; i < 40; i++) mix_ui_tick(v, step(50));
    assert(!repaint);
}
static void type(const char *s) {
    for (const char *p = s; *p; p++) mix_ui_key((const uint8_t *)p, 1);
}

static void test_launch_motion(mix_view_t *v) {
    mix_action_t a;
    assert(motion_ease(0) == 0 && motion_ease(MOTION_SCALE) == MOTION_SCALE);
    for (unsigned t = 1; t <= MOTION_SCALE; t++) {
        assert(motion_ease(t) >= motion_ease(t - 1));
        assert(motion_ease(t) <= MOTION_SCALE);
    }
    v->terminal_open = false; v->linux_online = true;
    v->maintenance_busy = false; v->ota_state = 0;
    for (int th = 0; th < 4; th++)
        for (int lang = 0; lang < 2; lang++)
            for (int card = 0; card < 3; card++) {
                theme = (uint8_t)th; language = (uint8_t)lang;
                navigate(PAGE_HOME); settle(v); drain();
                unsigned before = full_draws,total_before=draws;
                unsigned long long pixels_before=presented_pixels;
                uint32_t started = clk;
                launch(card);
                assert(launch_motion.active && !mix_ui_terminal_visible());
                /* Requests are immediate, never queued by animation frames. */
                if (card < 2) {
                    assert(mix_ui_take_action(&a) && a.kind == MIX_ACTION_APP_OPEN);
                    assert(a.value == (card == 0 ? MIX_APP_TRANSLATE : MIX_APP_NOTES));
                }
                no_action();
                mix_ui_tick(v, clk);
                assert(full_draws == before + 1);
                unsigned previous = 0;
                for (int ms = 1; ms <= MIX_MOTION_MEDIUM; ms++) {
                    unsigned rendered = draws;
                    mix_ui_tick(v, step(1));
                    if (ms < MIX_MOTION_MEDIUM) {
                        assert(launch_motion.active);
                        assert(launch_motion.progress >= previous);
                        previous = launch_motion.progress;
                        if (ms % MOTION_FRAME_MS) assert(draws == rendered);
                        assert(!mix_ui_terminal_visible());
                    }
                    no_action();
                }
                assert((uint32_t)(clk - started) == MIX_MOTION_MEDIUM);
                assert(!launch_motion.active && !repaint);
                if(card<2) {
                    assert(full_draws - before == 2); /* setup and waiting */
                    assert(content_motion.phase == CONTENT_WAIT);
                } else {
                    assert(full_draws - before == 1); /* frozen outgoing surface */
                    assert(content_motion.phase == CONTENT_OUT);
                }
                assert(draws-total_before == 1 + (MIX_MOTION_MEDIUM-1)/MOTION_FRAME_MS + (card<2?1:0));
                assert(presented_pixels-pixels_before < (unsigned long long)(draws-total_before)*W*H/2);
                assert(motion_stats.last_frames == 1+(MIX_MOTION_MEDIUM-1)/MOTION_FRAME_MS);
                assert(page == PAGE_APP);
                assert(current_app == (card == 0 ? MIX_APP_TRANSLATE : card == 1 ? MIX_APP_NOTES : MIX_APP_AGENT));
                assert(allocations == 1);
            }
    char perf[512],tiny[2];
    size_t perf_len=mix_ui_performance(perf,sizeof(perf));
    assert(perf_len==strlen(perf)&&perf_len>0&&strstr(perf,"\"schema\":1"));
    assert(!mix_ui_performance(NULL,sizeof(perf))&&!mix_ui_performance(tiny,sizeof(tiny)));
    theme = 0; language = 0;

    /* Arrival of terminal data during the motion is retained but not painted
     * over the launch surface. Readiness must not wait for the 1 Hz snapshot. */
    navigate(PAGE_HOME); settle(v); drain(); mix_terminal_init();
    mix_terminal_resize(geom()->cols, geom()->rows);
    launch(1); drain();
    mix_ui_tick(v, step(50));
    const uint8_t output[] = "READY";
    mix_terminal_feed(output, sizeof(output) - 1);
    v->terminal_open = true; v->running_app = MIX_APP_NOTES;
    unsigned before = draws;
    mix_ui_tick(v, step(10));
    assert(view.terminal_open && draws == before && mix_terminal_dirty(0));
    assert(!mix_ui_terminal_visible());
    /* No touch-through or keyboard activation of a hidden destination. */
    tap(BODY_X + 40, 570); mix_ui_key((const uint8_t *)"\r", 1); no_action();
    mix_ui_tick(v, step(290));
    assert(!launch_motion.active && content_motion.phase == CONTENT_OUT);
    assert(!mix_ui_terminal_visible());
    /* The outgoing fade completes before the target is rendered. The target
     * then stays frozen through the incoming fade while the terminal model is
     * free to receive bytes. */
    mix_ui_tick(v, step(100));
    assert(content_motion.phase == CONTENT_PREPARE);
    mix_ui_tick(v, step(1));
    assert(content_motion.phase == CONTENT_IN && !mix_ui_terminal_visible());
    mix_ui_tick(v, step(175));
    assert(content_motion.phase == CONTENT_IDLE && mix_ui_terminal_visible());
    assert(mix_terminal_row(0)[0].codepoint == 'R' && !mix_terminal_dirty(0));

    /* Home and Escape interrupt immediately; no delayed callback reopens it. */
    navigate(PAGE_HOME); v->terminal_open = false; settle(v); drain();
    launch(0); drain();
    v->terminal_open = true; v->running_app = MIX_APP_TRANSLATE;
    mix_ui_tick(v, step(20));
    mix_ui_home_toggle();
    assert(page == PAGE_HOME && !launch_motion.active);
    assert(mix_ui_take_action(&a) && a.kind == MIX_ACTION_TERMINAL_CLOSE); no_action();
    v->terminal_open = false; mix_ui_tick(v, step(400)); no_action();
    /* Even before OPENED, cancelling queues CLOSE after the single OPEN. */
    launch(1); mix_ui_key((const uint8_t *)"\033", 1);
    assert(page == PAGE_HOME && !launch_motion.active);
    assert(mix_ui_take_action(&a) && a.kind == MIX_ACTION_APP_OPEN && a.value == MIX_APP_NOTES);
    assert(mix_ui_take_action(&a) && a.kind == MIX_ACTION_TERMINAL_CLOSE); no_action();
    mix_ui_tick(v, step(400)); no_action();
    launch(3); assert(page == PAGE_SETTINGS && settings_menu && !launch_motion.active); no_action();
    navigation_back(); assert(page == PAGE_HOME);
    settle(v);

    /* A stale input tick and a slow first full present cannot consume motion.
     * This reproduces the original one-frame failure with injected wall time. */
    launch(0); drain(); before = draws; fake_present_ms=280;
    mix_ui_tick(v, step(2000));
    assert(launch_motion.active && !launch_motion.pending && draws == before + 1);
    assert(launch_motion.start_ms==clk && motion_stats.first_frame_us==280000);
    fake_present_ms=20;
    for(int i=0;i<12&&launch_motion.active;i++)mix_ui_tick(v,step(20));
    assert(!launch_motion.active && motion_stats.last_frames>=5);
    fake_present_ms=0;
    navigate(PAGE_HOME); settle(v); drain();
    clk = UINT32_MAX - 100; ui_ms = view_ms = clk;
    launch(1); drain(); mix_ui_tick(v, step(50));
    assert(launch_motion.active);
    mix_ui_tick(v, step(300)); assert(!launch_motion.active && content_motion.phase == CONTENT_WAIT);
    assert(content_motion.waiting_surface);

    /* Waiting is an indeterminate pulse, only a 32-row band at <=20 Hz. */
    before = full_draws; unsigned n = draws;
    mix_ui_tick(v, step(MOTION_FRAME_MS-1)); assert(draws == n);
    mix_ui_tick(v, step(1));
    assert(draws == n + 1 && full_draws == before);
    assert(last_y0 == WAIT_DOTS_Y && last_y1 == WAIT_DOTS_Y + WAIT_DOTS_H);
    /* Disconnect changes the caption immediately and stops idle animation. */
    v->linux_online = false; mix_ui_tick(v, step(1));
    assert(!view.linux_online && full_draws == before + 1);
    n = draws; mix_ui_tick(v, step(100)); assert(draws == n);
    /* A ready session replaces waiting through the same outgoing/incoming
     * handoff, even inside 1 second. */
    v->linux_online = true; v->terminal_open = true; v->running_app = MIX_APP_NOTES;
    mix_terminal_feed((const uint8_t *)"READY", 5);
    mix_ui_tick(v, step(1));
    assert(view.terminal_open && content_motion.phase == CONTENT_OUT);
    mix_ui_tick(v, step(100)); assert(content_motion.phase == CONTENT_PREPARE);
    mix_ui_tick(v, step(1)); assert(content_motion.phase == CONTENT_IN);
    mix_ui_tick(v, step(175));
    assert(content_motion.phase == CONTENT_IDLE && mix_ui_terminal_visible());

    /* OTA/maintenance takes precedence, without generating update actions. */
    for (int busy = 0; busy < 2; busy++) {
        v->terminal_open = false; navigate(PAGE_HOME); settle(v); drain();
        launch(0); drain();
        if (busy) v->maintenance_busy = true; else v->ota_state = 1;
        mix_ui_tick(v, step(1));
        assert(!launch_motion.active); no_action();
        v->maintenance_busy = false; v->ota_state = 0;
    }
    /* A draw failure keeps the normal unhealthy signal and retries the actual
     * page, rather than prolonging an animation or masking a display fault. */
    navigate(PAGE_HOME); settle(v); drain(); launch(0); drain();
    next_draw_error = ESP_ERR_INVALID_STATE;
    mix_ui_tick(v, step(1)); assert(!mix_ui_draw_healthy() && repaint);
    mix_ui_tick(v, step(1));
    assert(!launch_motion.active && mix_ui_draw_healthy() && !repaint);

    /* Missing fonts retain the same bounded, allocation-free fallback path. */
    fake_font = false; navigate(PAGE_HOME); settle(v); drain();
    launch(1); drain(); mix_ui_tick(v, step(50)); mix_ui_tick(v, step(300));
    assert(!launch_motion.active && allocations == 1);
    fake_font = true; v->linux_online = false;
    navigate(PAGE_HOME); settle(v); drain();
}

/* Exercise the frozen-canvas lifetime rather than just final state names. */
static void test_content_handoff(mix_view_t *v) {
    for(int th=0;th<4;th++)for(int preset=0;preset<GEOMETRY_COUNT;preset++) {
        navigate(PAGE_HOME);v->terminal_open=false;v->linux_online=true;settle(v);drain();
        theme=(uint8_t)th;set_geometry(preset);note_geometry();drain();
        mix_terminal_init();mix_terminal_resize(geom()->cols,geom()->rows);
        launch(1);drain();mix_ui_tick(v,step(1));
        v->terminal_open=true;v->running_app=MIX_APP_NOTES;
        mix_terminal_feed((const uint8_t*)"READY",5);
        mix_ui_tick(v,step(300));assert(content_motion.phase==CONTENT_OUT);
        unsigned full=full_draws,previous=fade_steps;
        for(int i=0;i<3;i++)mix_ui_tick(v,step(35));
        assert(content_motion.phase==CONTENT_PREPARE&&fade_steps==previous+3);
        for(int i=0;i<CONTENT_H*W;i++)assert(screen[i]==P.bg);
        assert(!memcmp(screen+STATUS_Y*W,fb+STATUS_Y*W,STATUS_H*W*2));
        mix_ui_tick(v,step(1));assert(content_motion.phase==CONTENT_IN);
        assert(full_draws==full); /* handoff performs NO extra full present */
        assert(!mix_terminal_dirty(0));
        mix_terminal_feed((const uint8_t*)"\rLATEST",7);
        assert(mix_terminal_dirty(0));
        /* Force a telemetry snapshot while freezing both body AND status. */
        view_ms=clk-1000;v->host_time_s++;mix_ui_notice("deferred notice");
        tap(400,600);mix_ui_key((const uint8_t*)"\r",1);no_action();
        previous=fade_steps;
        for(int i=0;i<4;i++) {
            mix_ui_tick(v,step(35));assert(content_motion.phase==CONTENT_IN);
            assert(mix_terminal_dirty(0)&&!mix_ui_terminal_visible());
        }
        mix_ui_tick(v,step(35));assert(content_motion.phase==CONTENT_IDLE);
        assert(fade_steps==previous+5&&full_draws==full);
        assert(mix_terminal_dirty(0)&&mix_ui_terminal_visible());
        mix_ui_tick(v,step(50));assert(!mix_terminal_dirty(0));
        assert(!memcmp(screen,fb,sizeof(screen)));assert(allocations==1);
    }
    /* OPENED with an empty terminal remains a waiting animation. A bounded
     * grace time then reveals a deliberately empty shell instead of deadlock. */
    navigate(PAGE_HOME);v->terminal_open=false;settle(v);drain();
    mix_terminal_init();mix_terminal_resize(geom()->cols,geom()->rows);
    launch(0);drain();mix_ui_tick(v,step(1));mix_ui_tick(v,step(300));
    assert(content_motion.phase==CONTENT_WAIT);
    v->terminal_open=true;v->running_app=MIX_APP_TRANSLATE;mix_ui_tick(v,step(1));
    assert(content_motion.phase==CONTENT_WAIT&&!mix_ui_terminal_visible());
    mix_ui_tick(v,step(FIRST_CONTENT_GRACE_MS-1));assert(content_motion.phase==CONTENT_WAIT);
    mix_ui_tick(v,step(1));assert(content_motion.phase==CONTENT_OUT);
    mix_ui_key((const uint8_t*)"\033",1);
    assert(page==PAGE_HOME&&!mix_ui_motion_active()&&!fake_fade);drain();settle(v);

    /* Each frozen phase can be cancelled, loses its borrowed reference, and
     * never allows a delayed callback to reopen the page or approve an action. */
    const content_phase_t phases[]={CONTENT_OUT,CONTENT_PREPARE,CONTENT_IN};
    for(unsigned p=0;p<3;p++)for(int reason=0;reason<6;reason++) {
        navigate(PAGE_HOME);v->terminal_open=false;v->linux_online=true;
        v->maintenance_busy=false;v->ota_state=0;settle(v);drain();
        launch(1);drain();mix_ui_tick(v,step(1));
        v->terminal_open=true;v->running_app=MIX_APP_NOTES;
        mix_terminal_feed((const uint8_t*)"R",1);mix_ui_tick(v,step(300));
        if(p>=1)mix_ui_tick(v,step(100));
        if(p>=2)mix_ui_tick(v,step(1));
        assert(content_motion.phase==phases[p]);
        if(reason==0)mix_ui_home_toggle();
        if(reason==1)mix_ui_key((const uint8_t*)"\033",1);
        if(reason==2)v->maintenance_busy=true;
        if(reason==3)v->ota_state=1;
        if(reason==4){v->terminal_open=false;v->linux_online=false;}
        if(reason==5)modal=MIX_ACTION_INPUT_RESET;
        mix_ui_tick(v,step(35));
        assert(!fake_fade&&content_motion.phase!=CONTENT_IN&&content_motion.phase!=CONTENT_OUT);
        if(reason<2){assert(page==PAGE_HOME);drain();}else no_action();
        modal=0;v->maintenance_busy=false;v->ota_state=0;
    }
    /* A failed fade uses the existing draw-health signal and does not clean
     * unrendered terminal rows or let a later status present mask the error. */
    navigate(PAGE_HOME);v->terminal_open=false;v->linux_online=true;settle(v);drain();
    launch(1);drain();mix_ui_tick(v,step(1));
    v->terminal_open=true;v->running_app=MIX_APP_NOTES;
    mix_terminal_feed((const uint8_t*)"R",1);mix_ui_tick(v,step(300));
    mix_ui_tick(v,step(100));mix_ui_tick(v,step(1));assert(content_motion.phase==CONTENT_IN);
    mix_terminal_feed((const uint8_t*)"N",1);next_draw_error=ESP_ERR_INVALID_STATE;
    mix_ui_tick(v,step(35));assert(!mix_ui_draw_healthy()&&repaint&&!fake_fade&&mix_terminal_dirty(0));
    v->terminal_open = false;v->linux_online = false;
    navigate(PAGE_HOME);settle(v);drain();theme=0;
}

/* These scenarios run in separate processes so one failing new contract cannot
 * prevent the existing exhaustive animation tests or the other scenarios. */
static void expect_action(mix_action_kind_t kind) {
    mix_action_t a;
    assert(mix_ui_take_action(&a) && a.kind == kind);
    no_action();
}
static void footer_tap(int target) { tap(target * 120 + 60, 708); }
static void swipe(int x0, int y0, int x1, int y1) {
    mix_ui_touch(x0, y0, true);
    mix_ui_touch(x1, y1, true);
    /* A successful release from the parent retains the last down coordinates. */
    mix_ui_touch(x1, y1, false);
}
static void assert_region_equal(const uint16_t *a, const uint16_t *b,
                                int x0, int y0, int x1, int y1) {
    for (int y = y0; y < y1; y++)
        assert(!memcmp(a + y * W + x0, b + y * W + x0, (size_t)(x1 - x0) * 2));
}
static void prepare_home(mix_view_t *v) {
    assert(!mix_ui_locked());
    v->terminal_open = false; v->linux_online = true;
    v->maintenance_busy = false; v->ota_state = 0;
    modal = 0; touch_test = false;
    navigate(PAGE_HOME); settle(v); drain();
}
static void prepare_terminal(mix_view_t *v) {
    prepare_home(v);
    set_geometry(1); note_geometry(); drain();
    mix_terminal_init(); mix_terminal_resize(geom()->cols, geom()->rows);
    launch(1); expect_action(MIX_ACTION_APP_OPEN);
    v->terminal_open = true; v->running_app = MIX_APP_NOTES;
    mix_terminal_feed((const uint8_t *)"READY", 5);
    settle(v);
    assert(mix_ui_terminal_visible() && !mix_ui_motion_active());
    assert(!mix_terminal_dirty(0) && !memcmp(screen, fb, sizeof(screen)));
}
static void prepare_network(mix_view_t *v) {
    prepare_home(v);
    navigate(PAGE_SETTINGS); go_section(SEC_NETWORK);
    network_count = 1;
    snprintf(networks[0].ssid, sizeof(networks[0].ssid), "%s", "private-test-network");
    networks[0].secured = true; networks[0].known = true;
    settle(v); drain();
    settings_tap(96, 240);
    assert(net_view == 1);
    type("local-secret");
    assert(net_pass_len == 12 && !strcmp(net_pass, "local-secret"));
}
/* stage 0 is launch, 1 outgoing fade, 2 prepare, 3 incoming fade. */
static void prepare_transition(mix_view_t *v, int stage) {
    prepare_home(v);
    mix_terminal_init(); mix_terminal_resize(geom()->cols, geom()->rows);
    launch(1); expect_action(MIX_ACTION_APP_OPEN);
    mix_ui_tick(v, step(1));
    if (!stage) { assert(launch_motion.active); return; }
    v->terminal_open = true; v->running_app = MIX_APP_NOTES;
    mix_terminal_feed((const uint8_t *)"READY", 5);
    mix_ui_tick(v, step(MIX_MOTION_MEDIUM));
    if (stage >= 2) mix_ui_tick(v, step(FADE_OUT_MS));
    if (stage >= 3) mix_ui_tick(v, step(1));
    assert(content_motion.phase == (stage == 1 ? CONTENT_OUT : stage == 2 ? CONTENT_PREPARE : CONTENT_IN));
}

#ifdef MIX_TEST_TOUCH_PIPELINE
/* The real main.c poll block calls the real UI, not a stubbed touch sink.
 * Hardware frame arrival alone is stubbed; coordinates on zero-contact frames
 * are deliberately garbage to prove main preserves the last real contact. */
#include "gt911.h"
#define ESP_ERR_NOT_FOUND 5
#define ESP_ERR_TIMEOUT 6
#define ESP_LOGI(...) ((void)0)
#define ESP_LOGW(...) ((void)0)
static i2c_master_dev_handle_t tp = (void *)1;
static gt911_touch_t pipeline_frame;
static esp_err_t pipeline_result;
static esp_err_t recover_touch(void) { return ESP_OK; }
esp_err_t gt911_read(i2c_master_dev_handle_t dev, gt911_touch_t *out) {
    assert(dev == tp); *out = pipeline_frame; return pipeline_result;
}
esp_err_t gt911_try_read(i2c_master_dev_handle_t dev, gt911_touch_t *out) {
    return gt911_read(dev,out);
}
static uint32_t clock_ms(void) { return ui_clock_ms(); }
#include "main_touch_sampling.h"
static void pipeline_poll(uint32_t ms) {
#include "main_touch_poll.h"
}
static void pipeline_contact(mix_view_t *v, uint32_t elapsed, esp_err_t error, int count, int x, int y) {
    pipeline_result = error;
    pipeline_frame = (gt911_touch_t){.count=count,.x=x,.y=y};
    pipeline_poll(step(elapsed)); mix_ui_tick(v, clk);
}
static void pipeline_wait(int contacts,int x,int y) {
    pipeline_result=ESP_OK;pipeline_frame=(gt911_touch_t){.count=contacts,.x=x,.y=y};
    step(16);touch_wait_sample(NULL);
}
static void test_wait_input(mix_view_t *v) {
    prepare_home(v); launch(3); assert(page==PAGE_SETTINGS&&settings_menu);
    mix_ui_tick(v,step(1)); unsigned n=draws;
    int x=SET_TILE_X0+20,y=SET_TILE_Y0+20;
    pipeline_wait(1,x,y);pipeline_wait(1,x+6,y+5);pipeline_wait(0,999,999);
    assert(draws==n&&settings_menu&&touch_count==3);
    dispatch_touch();assert(!settings_menu&&section==SEC_APPEARANCE&&!touch_count);no_action();
    mix_ui_tick(v,step(1));assert(!memcmp(screen,fb,sizeof(screen)));
    uint8_t saved=theme;
    pipeline_wait(1,100,110);pipeline_wait(1,100,150);
    pipeline_wait(1,100,110);pipeline_wait(0,999,999);
    dispatch_touch();assert(settings_scroll==0&&theme==saved);no_action();
    /* The following contact is independent, even when both releases were
     * queued while the panel waited. No drag may turn into a theme tap. */
    pipeline_wait(1,400,110);pipeline_wait(0,999,999);
    dispatch_touch();assert(theme==1&&settings_scroll==0);no_action();
    puts("wait input passed: queued real UI fixed-page taps, ordered drag/release, no scroll or false action");
}
static void test_touch_pipeline(mix_view_t *v) {
    prepare_home(v);launch(3);go_section(SEC_DISPLAY);settle(v);
    /* A stationary hold without a new controller frame is not a release. */
    pipeline_contact(v,16,ESP_OK,1,100,200);no_action();
    pipeline_contact(v,1200,ESP_ERR_NOT_FOUND,0,0,0);no_action();
    assert(!touch_block_until_up);
    pipeline_contact(v,16,ESP_OK,0,999,999);
    expect_action(MIX_ACTION_BRIGHT_DOWN);
    /* Failed reads quarantine the held contact through a validated up frame. */
    pipeline_contact(v,16,ESP_OK,1,420,200);
    pipeline_contact(v,16,ESP_ERR_TIMEOUT,0,0,0);
    assert(touch_block_until_up);no_action();
    pipeline_contact(v,16,ESP_OK,1,420,200);
    pipeline_contact(v,16,ESP_OK,0,999,999);
    assert(!touch_block_until_up);no_action();
    pipeline_contact(v,16,ESP_OK,1,420,200);
    pipeline_contact(v,16,ESP_OK,0,999,999);
    expect_action(MIX_ACTION_BRIGHT_UP);
    for(int i=0;i<12;i++) {
        pipeline_contact(v,16,ESP_OK,1,100,200);
        pipeline_contact(v,16,ESP_OK,1,100,213);
        pipeline_contact(v,16,ESP_OK,1,100,200);
        pipeline_contact(v,16,ESP_OK,0,999,999);no_action();
    }
    assert(settings_scroll==0&&!mix_ui_locked()&&mix_ui_display_awake());
    puts("touch pipeline passed: retained release coordinates, stationary hold, error quarantine, cancelled drags and next-gesture recovery");
}

#endif

static void test_unlocked_runtime(mix_view_t *v) {
    assert(page==PAGE_HOME&&!mix_ui_locked()&&mix_ui_display_awake());
    prepare_home(v);memcpy(checkpoint,fb,sizeof(checkpoint));unsigned n=draws;
    for(int i=0;i<4;i++)mix_ui_lock_key();
    mix_ui_tick(v,step(15001));
    assert(page==PAGE_HOME&&!mix_ui_locked()&&mix_ui_display_awake());
    assert(draws==n&&!memcmp(checkpoint,fb,sizeof(checkpoint)));no_action();
    prepare_terminal(v);
    mix_terminal_feed((const uint8_t *)"\rSTILL LIVE",11);
    unsigned cells=cell_draws;
    mix_ui_lock_key();assert(mix_ui_terminal_visible());
    mix_ui_tick(v,step(50));
    assert(!mix_ui_locked()&&mix_ui_display_awake()&&!mix_terminal_dirty(0)&&cell_draws>cells);
    no_action();
    for(int stage=0;stage<4;stage++) {
        prepare_transition(v,stage);
        content_phase_t phase=content_motion.phase;bool active=launch_motion.active;
        memcpy(checkpoint,fb,sizeof(checkpoint));
        mix_ui_lock_key();
        assert(page==PAGE_APP&&phase==content_motion.phase&&active==launch_motion.active);
        assert(!mix_ui_locked()&&mix_ui_display_awake()&&!memcmp(checkpoint,fb,sizeof(checkpoint)));
        no_action();footer_tap(1);expect_action(MIX_ACTION_TERMINAL_CLOSE);
    }
    /* The inert key neither approves nor destroys local input or consent. */
    prepare_network(v);settings_tap(420,470);assert(modal==MIX_ACTION_NET_FORGET);
    mix_ui_lock_key();assert(modal==MIX_ACTION_NET_FORGET&&net_view&&net_pass_len==12);
    no_action();footer_tap(1);assert(page==PAGE_HOME&&!modal&&!net_pass_len);
    for(unsigned i=0;i<sizeof(net_pass);i++)assert(!net_pass[i]);
    prepare_home(v);
    puts("unlocked runtime passed: Home startup, inert lock key, always awake, live terminal and frozen fades, Home secret erasure");
}

static void test_navigation_runtime(mix_view_t *v) {
    assert(STATUS_Y == 648 && H - STATUS_Y == 120);
    /* Exercise every corner of all three 120x120 targets, not just icons. */
    for (int target = 0; target < 3; target++)
        for (int dx = 0; dx <= 119; dx += 119)
            for (int dy = 0; dy <= 119; dy += 119) {
                prepare_home(v); navigate(PAGE_SETTINGS); go_section(SEC_APPEARANCE); settle(v);
                int x = target * 120 + dx, y = 648 + dy;
                mix_ui_touch(x, y, true);
                assert(nav_pressed == target && page == PAGE_SETTINGS); no_action();
                mix_ui_touch(x, y, false);
                assert(page == (target == 1 ? PAGE_HOME : PAGE_SETTINGS));
                if(target==0)assert(settings_menu);
                if(target==2)assert(!settings_menu&&section==SEC_APPEARANCE);
                assert(nav_pressed == -1); no_action();
            }
    prepare_home(v); navigate(PAGE_SETTINGS); go_section(SEC_APPEARANCE); settle(v);
    const int outside[][2] = {{360,648},{1023,767},{119,647},{120,768},{-1,700}};
    for (unsigned i = 0; i < sizeof(outside) / sizeof(outside[0]); i++) {
        tap(outside[i][0], outside[i][1]);
        assert(page == PAGE_SETTINGS && nav_pressed == -1); no_action();
    }
    mix_ui_touch(60, 708, true); mix_ui_touch(180, 708, true); mix_ui_touch(60, 708, false);
    assert(page == PAGE_SETTINGS); no_action(); /* cancellation survives re-entry */
    mix_ui_touch(180, 708, true); mix_ui_touch(0, 0, false);
    assert(page == PAGE_SETTINGS); no_action();
    mix_ui_touch(60, 708, true); mix_ui_touch(180, 708, false);
    assert(page == PAGE_SETTINGS); no_action();

    prepare_network(v);
    settings_tap(420, 470); assert(modal == MIX_ACTION_NET_FORGET);
    footer_tap(0); assert(!modal && net_view == 1 && net_pass_len == 12); no_action();
    footer_tap(0); assert(page == PAGE_SETTINGS && section == SEC_NETWORK && !net_view && !net_pass_len);
    for (unsigned i = 0; i < sizeof(net_pass); i++) assert(!net_pass[i]);
    footer_tap(0); assert(page == PAGE_SETTINGS && settings_menu);
    footer_tap(0); assert(page == PAGE_HOME); no_action();
    footer_tap(0); footer_tap(1); assert(page == PAGE_HOME); no_action();
    prepare_network(v);
    settings_tap(420, 470);
    footer_tap(1); assert(page == PAGE_HOME && !modal); no_action();
    footer_tap(1); assert(page == PAGE_HOME); no_action();

    prepare_home(v);
    memcpy(checkpoint, fb, sizeof(checkpoint));
    footer_tap(2); assert(page == PAGE_HOME && !toast.active); no_action();
    mix_ui_tick(v, step(1));
    assert(!memcmp(checkpoint,fb,sizeof(checkpoint)));
    assert(!memcmp(checkpoint,screen,sizeof(checkpoint)));
    footer_tap(2); assert(page == PAGE_HOME); no_action();

    for (int target = 0; target < 3; target++) {
        prepare_terminal(v);
        footer_tap(target);
        if (target == 0) {
            assert(page == PAGE_APP && mix_ui_terminal_visible());
            expect_action(MIX_ACTION_APP_BACK);
            footer_tap(0); expect_action(MIX_ACTION_APP_BACK);
            mix_ui_tick(v, step(500)); no_action();
            /* Offline/stale sessions do not redirect Back to a hidden app. */
            v->linux_online = false; mix_ui_tick(v, step(1001));
            footer_tap(0); assert(page == PAGE_APP); no_action();
            footer_tap(1); assert(page == PAGE_HOME);
            expect_action(MIX_ACTION_TERMINAL_CLOSE);
        } else if(target==2) {
            assert(page==PAGE_APP&&mix_ui_terminal_visible());no_action();
            footer_tap(1);expect_action(MIX_ACTION_TERMINAL_CLOSE);
        } else {
            assert(page == PAGE_HOME);
            assert(!mix_ui_terminal_visible() && !toast.active);
            expect_action(MIX_ACTION_TERMINAL_CLOSE);
            /* The host may still report OPENED while CLOSE is in flight. */
            footer_tap(target); no_action();
            mix_ui_tick(v, step(500)); no_action();
        }
    }
    /* Home interrupts each borrowed-canvas phase; Menu is inert and Back
     * preserves an application whose first content is not ready yet. */
    for (int target = 0; target < 3; target++) for (int stage = 0; stage < 4; stage++) {
        prepare_transition(v, stage);
        memcpy(checkpoint, fb, sizeof(checkpoint));
        mix_ui_touch(target * 120 + 60, 708, true);
        assert(page == PAGE_APP && mix_ui_motion_active());
        assert(!memcmp(checkpoint, fb, sizeof(checkpoint))); no_action();
        mix_ui_touch(target * 120 + 60, 708, false);
        if(target!=1) {
            assert(page==PAGE_APP&&mix_ui_motion_active()); no_action();
            assert(!memcmp(checkpoint,fb,sizeof(checkpoint)));
            footer_tap(1); expect_action(MIX_ACTION_TERMINAL_CLOSE);
            continue;
        }
        page_t destination = PAGE_HOME;
        assert(page == destination && !mix_ui_motion_active() && !fake_fade);
        expect_action(MIX_ACTION_TERMINAL_CLOSE);
        footer_tap(target); no_action();
        mix_ui_tick(v, step(500));
        assert(page == destination && !mix_ui_motion_active()); no_action();
    }
    /* Include input before the first launch frame and the indefinite wait for
     * host readiness: neither may reopen an app after navigating away. */
    for (int target = 0; target < 3; target++) for (int waiting = 0; waiting < 2; waiting++) {
        prepare_home(v); launch(1); expect_action(MIX_ACTION_APP_OPEN);
        if (waiting) {
            mix_ui_tick(v, step(1)); mix_ui_tick(v, step(MIX_MOTION_MEDIUM));
            assert(content_motion.phase == CONTENT_WAIT);
        } else assert(launch_motion.active && launch_motion.pending);
        footer_tap(target);
        if(target!=1) {
            assert(page==PAGE_APP&&mix_ui_motion_active()); no_action();
            footer_tap(1); expect_action(MIX_ACTION_TERMINAL_CLOSE);
            continue;
        }
        page_t destination = PAGE_HOME;
        assert(page == destination && !mix_ui_motion_active() && !fake_fade);
        expect_action(MIX_ACTION_TERMINAL_CLOSE);
        footer_tap(target); mix_ui_tick(v, step(500));
        assert(page == destination && !mix_ui_motion_active()); no_action();
    }
    /* The local Agent page never owns or closes a stale host session. */
    for (int card = 2; card < 3; card++) for (int stage = 0; stage < 4; stage++) {
        prepare_home(v);
        v->terminal_open = true; v->running_app = MIX_APP_SHELL;
        mix_ui_tick(v, step(1)); launch(card); no_action();
        mix_ui_tick(v, step(1));
        if (stage >= 1) mix_ui_tick(v, step(MIX_MOTION_MEDIUM));
        if (stage >= 2) mix_ui_tick(v, step(FADE_OUT_MS));
        if (stage >= 3) mix_ui_tick(v, step(1));
        assert(stage == 0 ? launch_motion.active : content_motion.phase ==
               (stage == 1 ? CONTENT_OUT : stage == 2 ? CONTENT_PREPARE : CONTENT_IN));
        footer_tap(2);assert(page==PAGE_APP&&mix_ui_motion_active());no_action();
        footer_tap(1);
        assert(page == PAGE_HOME && !mix_ui_motion_active() && !fake_fade); no_action();
        mix_ui_tick(v, step(500));
        assert(page == PAGE_HOME && !mix_ui_motion_active()); no_action();
    }
    /* Active navigation remains interlocked during maintenance and OTA. */
    for (int source = 0; source < 3; source++) for (int guard = 1; guard < 3; guard++) {
        if (source == 1) prepare_terminal(v);
        else {
            prepare_home(v);
            if (source == 2) { launch(3); settle(v); no_action(); }
        }
        page_t previous = page;
        if (guard == 1) v->maintenance_busy = true;
        if (guard == 2) v->ota_state = 1;
        /* Maintenance is part of the one-second telemetry snapshot. */
        mix_ui_tick(v, step(1001));
        for(int target=0;target<3;target++)footer_tap(target);
        mix_ui_key((const uint8_t *)"\033",1);
        assert(page == previous && !toast.active); no_action();
        v->maintenance_busy = false; v->ota_state = 0;
    }
    /* Menu preserves pending consent, password and the current subsection. */
    prepare_network(v);
    settings_tap(420, 470); assert(modal == MIX_ACTION_NET_FORGET);
    memcpy(checkpoint, fb, sizeof(checkpoint));
    footer_tap(2);
    assert(page == PAGE_SETTINGS && section == SEC_NETWORK && modal == MIX_ACTION_NET_FORGET);
    assert(net_view == 1 && net_pass_len == 12 && !strcmp(net_pass, "local-secret"));
    assert(!memcmp(checkpoint, fb, sizeof(checkpoint))); no_action();
    mix_ui_tick(v, step(1)); no_action();
    assert(page == PAGE_SETTINGS && section == SEC_NETWORK && modal == MIX_ACTION_NET_FORGET);
    assert(net_view == 1 && net_pass_len == 12 && !strcmp(net_pass, "local-secret"));
    footer_tap(0); no_action();
    prepare_home(v);
    puts("navigation runtime passed: three 120x120 targets, release validation, Back hierarchy, Home, direct Settings, launch and fade interruption, empty pages, modal and busy guards, single CLOSE");
}

static void test_notes_geometry(mix_view_t *v) {
    for(int preset=0;preset<GEOMETRY_COUNT;preset++) {
        prepare_home(v);set_geometry(preset);note_geometry();drain();
        launch(1);expect_action(MIX_ACTION_APP_OPEN);
        assert(geometry==preset&&mix_terminal_cols()==48&&mix_terminal_rows()==16);
        assert(geom()->font==34&&geom()->cw==21&&geom()->ch==40);
        v->terminal_open=true;v->running_app=MIX_APP_NOTES;
        mix_terminal_feed((const uint8_t *)"READY",5);settle(v);
        footer_tap(0);expect_action(MIX_ACTION_APP_BACK);
        assert(page==PAGE_APP&&mix_terminal_cols()==48&&geometry==preset);
        footer_tap(1);expect_action(MIX_ACTION_TERMINAL_CLOSE);
        assert(page==PAGE_HOME&&geometry==preset);
        assert(mix_terminal_cols()==geometries[preset].cols);
        assert(mix_terminal_rows()==geometries[preset].rows);
        v->terminal_open=false;settle(v);
        launch(0);expect_action(MIX_ACTION_APP_OPEN);
        assert(mix_terminal_cols()==geometries[preset].cols&&geom()->font==geometries[preset].font);
        v->terminal_open=true;v->running_app=MIX_APP_TRANSLATE;
        mix_terminal_feed((const uint8_t *)"READY",5);settle(v);
        footer_tap(0);expect_action(MIX_ACTION_APP_BACK);assert(page==PAGE_APP);
        footer_tap(1);expect_action(MIX_ACTION_TERMINAL_CLOSE);
    }
    /* The matching normal host EXIT returns to Home once, without guessing
     * from rendered text. Stale events and exits for another app do nothing. */
    for(int which=0;which<2;which++) {
        prepare_home(v);launch(which);expect_action(MIX_ACTION_APP_OPEN);
        v->terminal_open=true;v->running_app=which?MIX_APP_NOTES:MIX_APP_TRANSLATE;
        mix_terminal_feed((const uint8_t *)"READY",5);settle(v);
        footer_tap(0);expect_action(MIX_ACTION_APP_BACK);
        v->terminal_exit_app=MIX_APP_SHELL;v->terminal_exit_serial++;
        mix_ui_tick(v,step(1));assert(page==PAGE_APP);no_action();
        v->terminal_open=false;v->terminal_exit_app=current_app;v->terminal_exit_serial++;
        mix_ui_tick(v,step(1));assert(page==PAGE_HOME);
        expect_action(MIX_ACTION_TERMINAL_CLOSE);
        mix_ui_tick(v,step(1));no_action();
        launch(which);expect_action(MIX_ACTION_APP_OPEN);
        mix_ui_tick(v,step(1));assert(page==PAGE_APP);no_action();
        footer_tap(1);expect_action(MIX_ACTION_TERMINAL_CLOSE);
    }
    prepare_home(v);
    puts("notes geometry passed: 48x16, 34px, per-app override, restored preferences, root EXIT to Home");
}
static void assert_empty_body(void) {
    assert(page == PAGE_APP && !mix_ui_terminal_visible() && !mix_ui_motion_active());
    for (int i = 0; i < CONTENT_H * W; i++) {
        assert(fb[i] == P.bg);
        assert(screen[i] == P.bg);
    }
    /* Empty means only the body: the existing bottom navigation stays drawn. */
    bool footer_drawn = false;
    for (int i = STATUS_Y * W; i < H * W; i++) if (screen[i] != P.bg) footer_drawn = true;
    assert(footer_drawn);
}
static void assert_no_body_text(void) {
    for (unsigned i = 0; i < text_count; i++) {
        assert(text_calls[i].y >= CONTENT_H);
        assert(!strstr(text_calls[i].value, "placeholder"));
        assert(!strstr(text_calls[i].value, "Placeholder"));
        assert(!strstr(text_calls[i].value, "占位"));
        assert(!strstr(text_calls[i].value, "backend"));
        assert(!strstr(text_calls[i].value, "后端"));
    }
}
static void test_empty_pages_runtime(mix_view_t *v) {
    assert(MIX_APP_TRANSLATE == 0 && MIX_APP_NOTES == 1 && MIX_APP_AGENT == 2 &&
           MIX_APP_SHELL == 3 && MIX_APP_COUNT == 4);
    uint8_t saved_app = current_app;
    for (unsigned id = 0; id <= 255; id++) {
        current_app = (uint8_t)id;
        assert(app_has_session() == (id == MIX_APP_TRANSLATE || id == MIX_APP_NOTES || id == MIX_APP_SHELL));
    }
    current_app = saved_app;
    const uint8_t running[] = {MIX_APP_TRANSLATE, MIX_APP_NOTES, MIX_APP_SHELL, MIX_APP_AGENT, 255};
    unsigned combinations = 0;
    for (int th = 0; th < 4; th++) for (int lang = 0; lang < 2; lang++)
        for (int card = 2; card < 3; card++) for (int online = 0; online < 2; online++)
            for (int opened = 0; opened < 2; opened++) {
                prepare_home(v); theme = (uint8_t)th; language = (uint8_t)lang;
                v->linux_online = online; v->terminal_open = opened; v->running_app = MIX_APP_SHELL;
                settle(v);
                /* Record one isolated card, including its pressed state. There
                 * must be one icon and exactly title + subtitle, no state line. */
                for (int pressed = 0; pressed < 2; pressed++) {
                    text_count = glyph_count = 0; record_text = true;
                    launcher_card(card, pressed); record_text = false;
                    assert(glyph_count == 1 && text_count == 2);
                    assert(!strcmp(text_calls[0].value,lang?"智能体":"Agent"));
                    assert(!strcmp(text_calls[1].value,lang?"编程与工具":"Code & tools"));
                    int x, y; card_rect(card, &x, &y);
                    int ix = x + MIX_SPACE_4 + (pressed ? 6 : 0);
                    int iy = y + MIX_SPACE_2 + (pressed ? 6 : 0);
                    bool icon_drawn = false;
                    for (int yy = iy; yy < iy + MIX_DP(48); yy++)
                        for (int xx = ix; xx < ix + MIX_DP(48); xx++)
                            if (fb[yy * W + xx] == P.accent) icon_drawn = true;
                    assert(icon_drawn);
                    for (unsigned i = 0; i < text_count; i++) {
                        assert(text_calls[i].x >= x && text_calls[i].x < x + CARD_W);
                        assert(text_calls[i].y >= y && text_calls[i].y < y + CARD_H);
                    }
                }
                settle(v);
                mix_terminal_init(); mix_terminal_resize(geom()->cols, geom()->rows);
                mix_terminal_feed((const uint8_t *)"STALE TERMINAL", 14);
                assert(mix_terminal_dirty(0));
                unsigned cells = cell_draws, cleans = terminal_cleans;
                launch(card);
                assert(page == PAGE_APP && current_app == MIX_APP_AGENT);
                assert(launch_motion.active && !content_motion.waiting && !mix_ui_terminal_visible()); no_action();
                mix_ui_tick(v, step(1)); mix_ui_tick(v, step(MIX_MOTION_MEDIUM));
                assert(content_motion.phase == CONTENT_OUT);
                mix_ui_tick(v, step(FADE_OUT_MS)); assert(content_motion.phase == CONTENT_PREPARE);
                text_count = 0; record_text = true;
                mix_ui_tick(v, step(1)); record_text = false;
                assert(content_motion.phase == CONTENT_IN); assert_no_body_text();
                mix_terminal_feed((const uint8_t *)"\rDURING FADE", 12);
                mix_ui_tick(v, step(35)); mix_ui_tick(v, step(140));
                assert_empty_body(); no_action();
                assert(cell_draws == cells && terminal_cleans == cleans && mix_terminal_dirty(0));
                for (unsigned r = 0; r < sizeof(running) / sizeof(running[0]); r++) {
                    v->running_app = running[r];
                    v->linux_online = !v->linux_online; v->terminal_open = !v->terminal_open;
                    mix_terminal_feed((const uint8_t *)"\rLATEST TERMINAL", 16);
                    text_count = 0; record_text = true; repaint = true;
                    mix_ui_tick(v, step(1001)); record_text = false;
                    assert_empty_body(); assert_no_body_text();
                    assert(cell_draws == cells && terminal_cleans == cleans && mix_terminal_dirty(0));
                    assert(mix_terminal_row(0)[0].codepoint == 'L'); no_action();
                }
                mix_ui_key((const uint8_t *)"x\r", 2); tap(512, 400); no_action();
                mix_ui_lock_key(); assert(!mix_ui_locked()&&mix_ui_display_awake());
                mix_terminal_feed((const uint8_t *)"\rLOCAL UPDATE", 13);
                mix_ui_tick(v, step(60));
                assert(cell_draws == cells && terminal_cleans == cleans && mix_terminal_dirty(0));
                footer_tap(2);assert(page==PAGE_APP);no_action();
                text_count = 0; record_text = true;
                mix_ui_tick(v, step(60)); record_text = false;
                assert_empty_body(); assert_no_body_text();
                assert(cell_draws == cells && terminal_cleans == cleans && mix_terminal_dirty(0)); no_action();
                /* Menu is inert; Back, Home and Escape leave without owning
                 * or closing an unrelated host terminal session. */
                footer_tap(2);assert(page==PAGE_APP);no_action();
                switch (combinations++ % 3) {
                    case 0: footer_tap(0); break;
                    case 1: footer_tap(1); break;
                    default: mix_ui_key((const uint8_t *)"\033", 1); break;
                }
                assert(page == PAGE_HOME);
                no_action();
                assert(cell_draws == cells && terminal_cleans == cleans && mix_terminal_dirty(0));
            }
    assert(combinations == 32);
    puts("empty pages runtime passed: 32 Agent theme/language/link/session combinations, exact card icon/title/subtitle, blank body, retained footer, no host requests or terminal clean/render, inert lock and menu");
}

static bool recorded_text_has(const char *value) {
    for(unsigned i=0;i<text_count;i++)if(!strcmp(text_calls[i].value,value))return true;
    return false;
}
static void assert_secret_erased(void) {
    assert(net_pass_len==0&&!net_reveal);
    for(unsigned i=0;i<sizeof(net_pass);i++)assert(net_pass[i]==0);
}
static void assert_network_rows(void) {
    text_count=0;record_text=true;settings_draw();record_text=false;
    int rows=network_count-net_scroll;if(rows>3)rows=3;
    assert(net_scroll%3==0&&net_scroll>=0);
    assert(network_count?rows>0:net_scroll==0);
    for(int i=0;i<network_count;i++)
        assert(recorded_text_has(networks[i].ssid)==(i>=net_scroll&&i<net_scroll+rows));
}
static void test_fixed_settings_runtime(mix_view_t *v) {
    prepare_home(v);
    assert(page==PAGE_HOME&&!mix_ui_locked()&&mix_ui_display_awake());
    /* Home has four cards; the fourth opens Settings directly and must not
     * create an application request or launch animation. */
    launch(3);assert(page==PAGE_SETTINGS&&settings_menu&&!mix_ui_motion_active());no_action();
    const char *categories[8]={"Theme","Display","Sound & keyboard","Network","Power","Device","System","About"};
    text_count=0;record_text=true;settings_draw();record_text=false;
    for(int i=0;i<8;i++)assert(recorded_text_has(categories[i]));
    assert(SEC_COUNT==8&&SEC_ABOUT!=SEC_SYSTEM&&SET_THEME_COLS==3&&MIX_THEME_COUNT==12);
    const section_t expected[8]={SEC_APPEARANCE,SEC_DISPLAY,SEC_INPUT,SEC_NETWORK,
                                  SEC_POWER,SEC_DEVICE,SEC_SYSTEM,SEC_ABOUT};
    for(int i=0;i<8;i++) {
        navigate(PAGE_SETTINGS);settings_menu_open();
        int col=i%2,row=i/2;
        settings_tap(SET_TILE_X0+col*(SET_TILE_W+SET_TILE_GAP_X)+SET_TILE_W/2,
                     SET_TILE_Y0+row*(SET_TILE_H+SET_TILE_GAP_Y)+SET_TILE_H/2);
        assert(!settings_menu&&section==expected[i]&&settings_scroll==0);no_action();
        settle(v);assert(settings_scroll==0&&draw_offset_y==0&&draw_limit_y==CONTENT_H);
        if(i==0) {
            for(int t=0;t<MIX_THEME_COUNT;t++) {
                theme_tap(t);assert(theme==t&&settings_scroll==0);no_action();
                uint8_t stored=255;assert(nvs_get_u8(1,"theme",&stored)==ESP_OK&&stored==t);
            }
            theme_tap(3);assert(theme==3);go_section(SEC_APPEARANCE);settle(v);
            /* A >12px move, an out-of-body move and cancel each reject the
             * original target; release coordinates are never a new hit test. */
            uint8_t before=theme;
            mix_ui_touch(SET_THEME_X0+SET_THEME_W/2,SET_THEME_Y0+SET_THEME_H/2,true);
            mix_ui_touch(SET_THEME_X0+SET_THEME_W+SET_THEME_GAP_X/2,SET_THEME_Y0+SET_THEME_H/2+20,true);
            mix_ui_touch(SET_THEME_X0+SET_THEME_W+SET_THEME_GAP_X/2,SET_THEME_Y0+SET_THEME_H/2+20,false);
            assert(theme==before&&settings_scroll==0);no_action();
            mix_ui_touch(SET_THEME_X0+SET_THEME_W/2,SET_THEME_Y0+SET_THEME_H/2,true);
            mix_ui_touch_cancel();mix_ui_touch(SET_THEME_X0+SET_THEME_W/2,SET_THEME_Y0+SET_THEME_H/2,false);
            assert(theme==before&&settings_scroll==0);no_action();
        } else if(i==1) {
            tap(64,174);expect_action(MIX_ACTION_BRIGHT_DOWN);tap(388,174);expect_action(MIX_ACTION_BRIGHT_UP);
            tap(388,382);no_action();assert(language==1);tap(64,382);no_action();assert(language==0);
        } else if(i==2) {
            tap(64,164);expect_action(MIX_ACTION_VOLUME_DOWN);tap(388,164);expect_action(MIX_ACTION_VOLUME_UP);
            tap(64,368);expect_action(MIX_ACTION_KBD_BACKLIGHT);
            tap(64,510);expect_action(MIX_ACTION_TERM_GEOMETRY);
            assert(geometry==0&&mix_terminal_cols()==80&&mix_terminal_rows()==27);note_geometry();
            tap(64,510);no_action();
            tap(388,510);expect_action(MIX_ACTION_TERM_GEOMETRY);
            assert(geometry==1&&mix_terminal_cols()==64&&mix_terminal_rows()==20);note_geometry();
            uint8_t stored=255;assert(nvs_get_u8(1,"term",&stored)==ESP_OK&&stored==1);
        } else if(i==3) {
            for(int n=0;n<MIX_NET_MAX;n++) {
                snprintf(networks[n].ssid,sizeof(networks[n].ssid),"net-%02d",n);
                networks[n].secured=(n%2)==0;networks[n].known=true;
            }
            /* Include empty lists, exact multiples of three, a partial last
             * page, the maximum list and shrinking scan results. */
            for(int count=0;count<=MIX_NET_MAX;count++) {
                network_count=count;go_section(SEC_NETWORK);
                int last=count?((count-1)/3)*3:0;
                for(int start=0;start<=last;start+=3) {
                    assert(net_scroll==start);assert_network_rows();
                    tap(420,530);no_action();
                }
                assert(net_scroll==last);tap(420,530);assert(net_scroll==last);no_action();
                for(int start=last;start>0;start-=3){tap(100,530);assert(net_scroll==start-3);no_action();}
                tap(100,530);assert(net_scroll==0);no_action();
            }
            network_count=10;go_section(SEC_NETWORK);settle(v);
            tap(800,140);expect_action(MIX_ACTION_NET_SCAN);
            net_busy=true;tap(800,140);no_action();net_busy=false;
            tap(420,530);tap(420,530);tap(420,530);assert(net_scroll==9);
            network_count=4;assert_network_rows();assert(net_scroll==3);
            network_count=10;tap(420,530);tap(420,530);assert(net_scroll==9);
            tap(100,240);assert(net_view&&!strcmp(net_ssid,"net-09"));type("fixed-secret");
            assert(net_pass_len==12);tap(850,340);assert(net_reveal);
            footer_tap(2);assert(net_view&&net_pass_len==12&&net_reveal&&net_scroll==9);no_action();
            footer_tap(0);assert(!net_view&&!settings_menu&&section==SEC_NETWORK&&net_scroll==9);
            assert_secret_erased();no_action();
            tap(100,240);type("fixed-secret");tap(850,340);assert(net_reveal);
            mix_ui_key((const uint8_t *)"\033",1);
            assert(!net_view&&net_scroll==9);assert_secret_erased();no_action();
            tap(100,240);type("fixed-secret");net_busy=true;
            tap(100,470);mix_ui_key((const uint8_t *)"\r",1);no_action();net_busy=false;
            tap(100,470);expect_action(MIX_ACTION_NET_CONNECT);
            assert(!strcmp(mix_ui_net_ssid(),"net-09")&&!strcmp(mix_ui_net_passphrase(),"fixed-secret"));
            mix_ui_tick(v,step(20));assert(net_pass_len==0);
            for(unsigned b=0;b<sizeof(net_pass);b++)assert(net_pass[b]==0);
            /* Both rejection and acceptance still require local consent. */
            type("forget-me");tap(420,470);assert(modal==MIX_ACTION_NET_FORGET);no_action();
            mix_ui_notice("YES");mix_terminal_feed((const uint8_t *)"YES\r\n",5);no_action();
            mix_ui_key((const uint8_t *)"\033",1);assert(!modal&&net_view);no_action();
            tap(420,470);assert(modal==MIX_ACTION_NET_FORGET);
            mix_ui_key((const uint8_t *)"\r",1);expect_action(MIX_ACTION_NET_FORGET);
            assert(!strcmp(mix_ui_net_ssid(),"net-09"));mix_ui_tick(v,step(20));assert(net_pass_len==0);
            /* Escape and Back share password -> list -> menu -> Home. */
            mix_ui_key((const uint8_t *)"\033",1);assert(!net_view&&net_scroll==9);
            mix_ui_key((const uint8_t *)"\033",1);assert(settings_menu);
            mix_ui_key((const uint8_t *)"\033",1);assert(page==PAGE_HOME);no_action();
            launch(3);go_section(SEC_NETWORK);tap(100,240);type("home-secret");
            footer_tap(1);assert(page==PAGE_HOME);assert_secret_erased();no_action();
            launch(3);go_section(SEC_NETWORK);network_count=0;net_scroll=0;notice_visible=false;
        } else if(i==5) {
            tap(100,530);assert(touch_test);settle(v);memcpy(checkpoint,fb,sizeof(checkpoint));
            text_count=0;record_text=true;
            mix_ui_touch(120,300,true);mix_ui_tick(v,step(60));record_text=false;
            assert(last_x0>=0&&last_x1<=W&&last_y0>=0&&last_y1<=CONTENT_H);
            assert(recorded_text_has("X 120   Y 300   DOWN"));
            for(unsigned t=0;t<text_count;t++)assert(text_calls[t].y>=0&&text_calls[t].y+text_calls[t].size<=CONTENT_H);
            assert_region_equal(checkpoint,fb,0,STATUS_Y,W,H);
            assert(!memcmp(screen,fb,sizeof(screen)));
            mix_ui_touch(120,300,false);tap(420,530);assert(modal==MIX_ACTION_INPUT_RESET);no_action();
            footer_tap(0);assert(!modal&&!settings_menu&&section==SEC_DEVICE);no_action();
            tap(420,530);assert(modal==MIX_ACTION_INPUT_RESET);
            mix_ui_key((const uint8_t *)"\r",1);expect_action(MIX_ACTION_INPUT_RESET);
            tap(100,530);assert(!touch_test);
        } else if(i==6) {
            for(int running=0;running<2;running++) {
                v->job_running=running;settle(v);
                mix_action_kind_t kind=running?MIX_ACTION_JOB_CANCEL:MIX_ACTION_JOB_START;
                tap(100,550);assert(modal==kind);no_action();
                mix_ui_key((const uint8_t *)"\033",1);assert(!modal);no_action();
                tap(100,550);assert(modal==kind);
                mix_ui_key((const uint8_t *)"\r",1);expect_action(kind);
            }
            v->job_running=false;settle(v);
            tap(420,550);assert(page==PAGE_APP&&current_app==MIX_APP_SHELL);
            expect_action(MIX_ACTION_APP_OPEN);footer_tap(1);expect_action(MIX_ACTION_TERMINAL_CLOSE);
            launch(3);go_section(SEC_SYSTEM);
        } else if(i==7) {
            text_count=0;record_text=true;settings_draw();record_text=false;
            assert(recorded_text_has("About")&&!recorded_text_has("Run task"));
            tap(100,550);tap(420,550);assert(!modal&&page==PAGE_SETTINGS&&section==SEC_ABOUT);no_action();
        }
        /* Every fixed page rejects a scroll-sized contact and keeps the body
         * coordinate system at zero. */
        if(i!=3||!net_view) {
            mix_ui_touch(500,300,true);mix_ui_touch(500,420,true);mix_ui_touch(500,420,false);
            assert(settings_scroll==0);no_action();
        }
    }
    /* Both touch and Esc unwind one level without silently opening apps. */
    for(int i=0;i<8;i++) {
        settings_menu_open();uint8_t key=(uint8_t)('1'+i);mix_ui_key(&key,1);
        assert(!settings_menu&&section==expected[i]);
        footer_tap(2);assert(!settings_menu&&section==expected[i]);no_action();
        if(i%2)mix_ui_key((const uint8_t *)"\033",1);else footer_tap(0);
        assert(settings_menu&&page==PAGE_SETTINGS);no_action();
    }
    /* Bottom menu is intentionally inert; Back unwinds detail -> menu -> Home. */
    settings_menu_open();settle(v);unsigned long long pixels=presented_pixels;
    footer_tap(2);assert(page==PAGE_SETTINGS&&settings_menu);no_action();
    assert(presented_pixels==pixels);
    footer_tap(0);assert(page==PAGE_HOME);footer_tap(0);assert(page==PAGE_HOME);no_action();
    /* Maintenance and OTA interlock navigation, adjustments, secrets and
     * local confirmation, including Enter while a modal is already open. */
    for(int guard=0;guard<2;guard++) {
        prepare_home(v);launch(3);go_section(SEC_SYSTEM);settle(v);
        tap(100,550);assert(modal==MIX_ACTION_JOB_START);
        v->maintenance_busy=guard==0;v->ota_state=guard==1;mix_ui_tick(v,step(1001));
        mix_ui_key((const uint8_t *)"\r",1);tap(650,470);no_action();
        v->maintenance_busy=false;v->ota_state=0;mix_ui_tick(v,step(1001));
        footer_tap(0);assert(!modal);no_action();
        go_section(SEC_DISPLAY);settle(v);
        v->maintenance_busy=guard==0;v->ota_state=guard==1;mix_ui_tick(v,step(1001));
        tap(100,220);tap(420,220);for(int target=0;target<3;target++)footer_tap(target);
        mix_ui_key((const uint8_t *)"\033",1);
        assert(page==PAGE_SETTINGS&&!settings_menu&&section==SEC_DISPLAY);no_action();
        v->maintenance_busy=false;v->ota_state=0;mix_ui_tick(v,step(1001));
        prepare_network(v);
        v->maintenance_busy=guard==0;v->ota_state=guard==1;mix_ui_tick(v,step(1001));
        type("x");mix_ui_key((const uint8_t *)"\r",1);tap(100,470);tap(420,470);
        assert(net_pass_len==12&&!modal&&net_view);no_action();
        v->maintenance_busy=false;v->ota_state=0;mix_ui_tick(v,step(1001));
        footer_tap(1);assert_secret_erased();
    }
    prepare_home(v);
    puts("fixed-settings runtime passed: startup awake, four-card Settings, eight categories, fixed pages, theme persistence, controls, network paging/password, device/system confirmations, inert menu, hierarchical Back, maintenance/OTA guards");
}
static void test_settings_cancel_runtime(mix_view_t *v) {
    prepare_home(v);launch(3);go_section(SEC_APPEARANCE);settle(v);
    theme_tap(5);settle(v);uint8_t original=theme;
    int x=SET_THEME_X0+SET_THEME_W/2,y=SET_THEME_Y0+SET_THEME_H/2;
    /* A rejected contact stays rejected after reversing through its origin. */
    const int moves[][2]={{13,0},{-13,0},{0,13},{0,-13},{120,120}};
    for(unsigned i=0;i<sizeof(moves)/sizeof(moves[0]);i++) {
        mix_ui_touch(x,y,true);mix_ui_touch(x+moves[i][0],y+moves[i][1],true);
        mix_ui_touch(x,y,true);mix_ui_touch(x,y,false);
        assert(theme==original&&settings_scroll==0);no_action();
        /* Controller may omit all intermediate motion frames. */
        mix_ui_touch(x,y,true);mix_ui_touch(x+moves[i][0],y+moves[i][1],false);
        assert(theme==original);no_action();
    }
    const int outside[][2]={{-1,100},{W,100},{100,-1},{100,CONTENT_H},{100,H}};
    for(unsigned i=0;i<sizeof(outside)/sizeof(outside[0]);i++) {
        mix_ui_touch(x,y,true);mix_ui_touch(outside[i][0],outside[i][1],true);
        mix_ui_touch(x,y,true);mix_ui_touch(x,y,false);
        assert(theme==original);no_action();
        mix_ui_touch(outside[i][0],outside[i][1],true);mix_ui_touch(x,y,false);
        assert(theme==original);no_action();
    }
    mix_ui_touch(x,y,true);mix_ui_touch_cancel();
    assert(touch_block_until_up);mix_ui_touch(x,y,true);mix_ui_touch(x,y,false);
    assert(theme==original&&!touch_block_until_up);no_action();
    /* A <=12px jitter uses the press coordinate, even across a tile edge. */
    int gap=SET_THEME_X0+SET_THEME_W+SET_THEME_GAP_X/2;
    mix_ui_touch(gap,y,true);mix_ui_touch(gap+12,y,false);
    assert(theme==original);no_action();
    mix_ui_touch(SET_THEME_X0+SET_THEME_W-1,y,true);
    mix_ui_touch(SET_THEME_X0+SET_THEME_W+11,y,false);
    assert(theme==0);no_action();
    theme_tap(5);mix_ui_touch(x,y,true);mix_ui_touch(x+12,y+12,false);
    assert(theme==0);no_action();
    /* All settings pages, including the menu and password page, have no
     * scroll movement or hidden-control activation in either direction. */
    for(int s=-1;s<SEC_COUNT;s++) {
        if(s<0)settings_menu_open();else go_section((section_t)s);
        settle(v);memcpy(checkpoint,fb,sizeof(checkpoint));
        uint8_t saved_theme=theme,saved_language=language,saved_geometry=geometry;
        for(int reverse=0;reverse<2;reverse++) {
            swipe(500,reverse?100:580,500,reverse?580:100);
            mix_ui_tick(v,step(1));
            assert(settings_scroll==0&&!modal&&!net_view&&theme==saved_theme);
            assert(language==saved_language&&geometry==saved_geometry);no_action();
            assert(!memcmp(checkpoint,fb,sizeof(checkpoint)));
        }
    }
    prepare_network(v);
    swipe(100,500,100,200);assert(net_view&&net_pass_len==12&&!modal);no_action();
    footer_tap(1);assert(!net_pass_len);prepare_home(v);
    puts("settings cancel passed: 12px threshold, press-coordinate jitter, reversal, body exits, error quarantine, all fixed pages without scroll");
}

static void test_nav_icons_runtime(mix_view_t *v) {
    prepare_home(v);
    const uint8_t *masks[]={mix_nav_arrow_back_alpha,mix_nav_home_alpha,mix_nav_settings_alpha};
    const int widths[]={MIX_NAV_ARROW_BACK_WIDTH,MIX_NAV_HOME_WIDTH,MIX_NAV_SETTINGS_WIDTH};
    const int heights[]={MIX_NAV_ARROW_BACK_HEIGHT,MIX_NAV_HOME_HEIGHT,MIX_NAV_SETTINGS_HEIGHT};
    unsigned allocated=allocations;
    for(int th=0;th<MIX_THEME_COUNT;th++)for(int state=0;state<2;state++) {
        theme=(uint8_t)th;page=state==0?PAGE_HOME:PAGE_SETTINGS;
        status_bar();
        assert(P.muted!=P.card&&P.muted!=P.bg);
        for(int y=STATUS_Y;y<STATUS_Y+3;y++)for(int x=0;x<W;x++)
            assert(fb[y*W+x]==P.muted);
        for(int n=0;n<2;n++) {
            uint16_t fg=(n==0&&page==PAGE_HOME)?P.muted:P.text;
            int left=n*120+60-widths[n]/2,top=STATUS_Y+60-heights[n]/2;
            unsigned ink=0;
            for(int y=STATUS_Y+3;y<H;y++)for(int x=n*120;x<(n+1)*120;x++) {
                uint16_t expected=P.card;
                if(inside(x,y,left,top,widths[n],heights[n])) {
                    uint8_t a=masks[n][(y-top)*widths[n]+x-left];
                    expected=blend565(P.card,fg,a);if(a)ink++;
                }
                assert(fb[y*W+x]==expected);
            }
            assert(ink>400);
        }
        /* The inactive Menu has three visible lines, not a Settings gear. */
        for(int dy=-14;dy<=14;dy+=14)assert(fb[(STATUS_Y+60+dy+2)*W+300]==P.text);
    }
    assert(allocations==allocated);prepare_home(v);
    puts("material navigation passed: exact official masks, 12 themes, Home/Back colors, visible inactive Menu, no allocation");
}

static void test_settings_render_budget(mix_view_t *v) {
    prepare_home(v);navigate(PAGE_SETTINGS);go_section(SEC_APPEARANCE);settle(v);
    notice_visible=false;repaint=true;mix_ui_tick(v,step(1));
    unsigned full=full_draws,n=draws;
    memcpy(checkpoint,fb,sizeof(checkpoint));
    unsigned before_drag=draws;
    mix_ui_touch(700,600,true);
    for(int i=1;i<=34;i++) {
        mix_ui_touch(700,600-i*3,true);mix_ui_tick(v,step(1));
        assert(draws==before_drag&&settings_scroll==0&&!mix_ui_needs_fast_tick());
    }
    mix_ui_touch(700,498,false);no_action();
    assert(!memcmp(checkpoint,fb,sizeof(checkpoint)));
    assert(draw_clip_y0==0&&draw_limit_y==CONTENT_H);
    /* Entirely offscreen text is rejected before any width/glyph lookup. */
    text_count=width_calls=glyph_count=0;record_text=true;
    text(300,2000,40,P.text,"offscreen");text_mid(500,-200,40,P.text,"offscreen");
    text_fit(300,2000,400,40,P.text,"offscreen");text_mid_fit(500,-200,400,40,P.text,"offscreen");
    icon(400,2000,60,P.text,ICON_AGENT);record_text=false;
    assert(text_count==0&&width_calls==0&&glyph_count==0);
    /* A changing clock/rate does not repaint static settings content. */
    memcpy(checkpoint,fb,sizeof(checkpoint));n=draws;v->host_time_s++;
    mix_ui_tick(v,step(1001));
    assert(draws==n+1&&full_draws==full&&last_y0==STATUS_Y);
    assert_region_equal(checkpoint,fb,0,0,W,CONTENT_H);
    /* Link changes invalidate network content even before the next telemetry
     * snapshot, rather than being hidden by early session adoption. */
    go_section(SEC_NETWORK);settle(v);full=full_draws;n=draws;
    v->linux_online=!v->linux_online;mix_ui_tick(v,step(35));
    assert(draws==n+1&&full_draws==full&&last_x0==0&&last_y1==H);
    assert(!memcmp(screen,fb,sizeof(screen)));
    go_section(SEC_DEVICE);
    settle(v);full=full_draws;
    mix_ui_notice("Preferences not saved");
    text_count=0;record_text=true;mix_ui_tick(v,step(35));record_text=false;
    bool shown=false;
    for(unsigned i=0;i<text_count;i++)if(!strcmp(text_calls[i].value,"Preferences not saved"))shown=true;
    assert(shown&&notice_visible&&full_draws==full);
    /* Dismissal consumes the whole contact, including movement into a
     * covered control; no network/theme/maintenance action can leak. */
    mix_ui_touch(BODY_X+400,50,true);mix_ui_touch(BODY_X+400,50,false);
    assert(!notice_visible&&!touch_block_until_up);no_action();
    mix_ui_tick(v,step(35));
    mix_ui_notice("Notice expiry");mix_ui_tick(v,step(35));
    mix_ui_tick(v,step(NOTICE_MS));assert(!notice_visible&&full_draws==full);
    prepare_home(v);
    puts("settings render budget passed: no scroll presents, skipped invisible text, footer-only telemetry, link changes, readable notices");
}

static void test_card_style_contract(mix_view_t *v) {
    prepare_home(v);layout_metrics=true;v->linux_online=true;v->terminal_open=false;view=*v;
    for(int th=0;th<MIX_THEME_COUNT;th++)for(int lang=0;lang<2;lang++)for(int pressed=0;pressed<2;pressed++) {
        theme=(uint8_t)th;language=(uint8_t)lang;
        for(int card=0;card<4;card++) {
            int x,y;card_rect(card,&x,&y);int inset=pressed?6:0;
            text_count=glyph_count=0;record_text=true;launcher_card(card,pressed);record_text=false;
            assert(text_count==2);
            assert(text_calls[0].size==CARD_TITLE_SIZE&&text_calls[1].size==CARD_SUBTITLE_SIZE);
            assert(text_calls[0].x==x+MIX_SPACE_4+inset&&text_calls[1].x==text_calls[0].x);
            assert(text_calls[0].y==y+CARD_TITLE_Y+inset&&text_calls[1].y==y+CARD_SUBTITLE_Y+inset);
            assert(text_calls[0].color==P.text&&text_calls[1].color==P.muted);
            for(unsigned i=0;i<2;i++)assert(text_calls[i].x+text_calls[i].width<=x+CARD_W-MIX_SPACE_4-inset);
            if(card==3) {
                assert(!strcmp(text_calls[0].value,lang?"设置":"Settings"));
                assert(glyph_count==1);
            }
            if(card==2) {
                assert(!strcmp(text_calls[0].value,lang?"智能体":"Agent"));
                assert(!strcmp(text_calls[1].value,lang?"编程与工具":"Code & tools"));
            }
        }
    }
    language=0;theme=0;layout_metrics=false;prepare_home(v);
    puts("card style passed: all four cards share type, baseline, spacing, colors and pressed offsets; programming subtitle");
}

static void assert_layout_text(bool footer) {
    for(unsigned i=0;i<text_count;i++) {
        int left=text_calls[i].x, right=left+text_calls[i].width;
        if(left<0||right>W) {
            fprintf(stderr,"overflow sec=%d lang=%u scroll=%d x=%d..%d size=%d text=%s\n",
                    section,language,settings_scroll,left,right,text_calls[i].size,text_calls[i].value);
            assert(0);
        }
        if(!footer) {
            assert(text_calls[i].size>=SET_SMALL);
            assert(text_calls[i].y>=0&&text_calls[i].y+text_calls[i].size<=CONTENT_H);
        }
        for(unsigned j=0;j<i;j++) {
            bool overlap=left<text_calls[j].x+text_calls[j].width&&right>text_calls[j].x&&
                text_calls[i].y<text_calls[j].y+text_calls[j].size&&
                text_calls[i].y+text_calls[i].size>text_calls[j].y;
            if(overlap) {
                fprintf(stderr,"overlap sec=%d lang=%u scroll=%d: %s / %s\n",section,language,
                        settings_scroll,text_calls[i].value,text_calls[j].value);
                assert(0);
            }
        }
    }
}
static void test_layout_runtime(mix_view_t *v) {
    prepare_home(v); layout_metrics=true; language=0; notice[0]=0;
    v->linux_online=true;v->wifi_reported=true;v->wifi_connected=true;
    v->wifi_speed_valid=true;v->wifi_rx_bps=99999;v->soc_valid=true;v->soc=100;
    v->host_time_s=951782400;v->host_tz_offset_min=0; /* 2000-02-29 UTC */
    snprintf(v->wifi_ssid,sizeof(v->wifi_ssid),"WWWWWWWWWWWWWWWWWWWWWWWWWWWWWWWW");
    snprintf(v->host_ip,sizeof(v->host_ip),"255.255.255.255");
    network_count=12;
    for(int i=0;i<network_count;i++) {
        snprintf(networks[i].ssid,sizeof(networks[i].ssid),"WWWWWWWWWWWWWWWWWWWWWWWWWWWWW%02d",i);
        networks[i].secured=networks[i].known=true;networks[i].signal=100;
    }
    view=*v;page=PAGE_SETTINGS;
    for(int th=0;th<MIX_THEME_COUNT;th++)for(int lang=0;lang<2;lang++) {
        theme=(uint8_t)th;language=(uint8_t)lang;
        settings_menu_open();text_count=0;record_text=true;settings_draw();record_text=false;
        assert_layout_text(false);
        for(int sec=0;sec<SEC_COUNT;sec++) {
            go_section((section_t)sec);
            text_count=0;record_text=true;settings_draw();record_text=false;
            assert(settings_scroll==0);assert_layout_text(false);
        }
        text_count=0;record_text=true;status_bar();record_text=false;
        assert_layout_text(true);
        go_section(SEC_NETWORK);net_view=1;
        snprintf(net_ssid,sizeof(net_ssid),"WWWWWWWWWWWWWWWWWWWWWWWWWWWWWWWW");
        memset(net_pass,'W',63);net_pass[63]=0;net_pass_len=63;
        for(int reveal=0;reveal<2;reveal++) {
            net_reveal=reveal!=0;
            text_count=0;record_text=true;settings_draw();record_text=false;
            assert_layout_text(false);
        }
    }
    net_view=0;memset(net_pass,0,sizeof(net_pass));net_pass_len=0;
    go_section(SEC_NETWORK);
    settings_tap(420,530);assert(net_scroll==3);no_action();
    settings_tap(100,240);assert(net_view==1&&!strcmp(net_ssid,networks[3].ssid));
    /* Clipping preserves UTF-8 code-point boundaries and does not mutate input. */
    char clipped[32];
    const char *fitted=fit_text(35,110,"中文中文",clipped,sizeof(clipped));
    assert(!strcmp(fitted,"中..."));
    fitted=fit_text(35,4,"中文",clipped,sizeof(clipped));assert(!fitted[0]);
    /* Every preset has a reachable, persisted hit target; include last row. */
    go_section(SEC_APPEARANCE);
    for(int i=0;i<MIX_THEME_COUNT;i++) {
        theme_tap(i);
        assert(theme==i);no_action();
        uint8_t saved=255;assert(nvs_get_u8(1,"theme",&saved)==ESP_OK&&saved==i);
    }
    char b[64];view.host_time_s=0;date_string(b,sizeof(b));assert(!strcmp(b,"--/--/--"));
    view.host_time_s=951782400;view.host_tz_offset_min=0;date_string(b,sizeof(b));assert(!strcmp(b,"2000-02-29"));
    view.host_tz_offset_min=-60;date_string(b,sizeof(b));assert(!strcmp(b,"2000-02-28"));
    view.host_time_s=1;clock_string(b,sizeof(b));assert(!strcmp(b,"23:00"));
    date_string(b,sizeof(b));assert(!strcmp(b,"1969-12-31"));
    view.host_time_s=1789884000;view.host_tz_offset_min=480;
    clock_string(b,sizeof(b));assert(!strcmp(b,"14:00"));
    date_string(b,sizeof(b));assert(!strcmp(b,"2026-09-20"));
    const float rates[]={0,999,1000,99950,1000000,1.0e12f,1.0e30f,NAN,-1};
    for(unsigned i=0;i<sizeof(rates)/sizeof(rates[0]);i++) {
        view.wifi_rx_bps=rates[i];speed_string(b,sizeof(b));assert(text_width(28,b)<=132);
        if(i>=7)assert(!strcmp(b,"--"));
    }
    view.wifi_speed_valid=false;speed_string(b,sizeof(b));assert(!strcmp(b,"--"));
    layout_metrics=false;theme=0;language=0;network_count=0;
    puts("layout runtime passed: 12 themes, bilingual size-aware bounds, non-overlapping text, UTF-8 clipping, date rollover and measured speed formatting");
}

static void assert_toast_present(void) {
    assert(last_x0 == 192 && last_x1 == 832);
    assert(last_y0 == TOAST_Y && last_y1 == TOAST_Y + TOAST_H);
    assert(last_y0 >= 0 && last_y1 <= CONTENT_H);
}
static void test_toast_runtime(mix_view_t *v) {
    prepare_home(v);
    memcpy(checkpoint, fb, sizeof(checkpoint));
    unsigned full = full_draws, n = draws;
    unsigned long long pixels = presented_pixels;
    mix_ui_feedback(MIX_UI_VOLUME, 75);
    assert(draws == n && !memcmp(checkpoint, fb, sizeof(checkpoint))); no_action();
    mix_ui_tick(v, step(1));
    assert(toast.active && toast.shown && !toast.dirty && volume_percent == 75);
    assert(draws == n + 1 && full_draws == full);
    assert(presented_pixels - pixels == 640u * 104u); assert_toast_present();
    assert_region_equal(checkpoint, fb, 0, 0, W, TOAST_Y);
    assert_region_equal(checkpoint, fb, 0, TOAST_Y + TOAST_H, W, H);
    assert_region_equal(checkpoint, fb, 0, TOAST_Y, 192, TOAST_Y + TOAST_H);
    assert_region_equal(checkpoint, fb, 832, TOAST_Y, W, TOAST_Y + TOAST_H);
    assert(fb[(TOAST_Y + 75) * W + 600] == P.accent);
    /* Each new value replaces the previous overlay and restarts its lifetime;
     * all three controls share the same single bounded present. */
    const mix_ui_feedback_t kinds[] = {MIX_UI_VOLUME, MIX_UI_BRIGHTNESS, MIX_UI_KEYBOARD_LIGHT, MIX_UI_BRIGHTNESS};
    const int values[] = {20, 90, 8, -1};
    for (unsigned i = 0; i < sizeof(values) / sizeof(values[0]); i++) {
        mix_ui_tick(v, step(1000)); assert(toast.active && full_draws == full);
        n = draws; pixels = presented_pixels;
        mix_ui_feedback(kinds[i], values[i]);
        assert(draws == n); no_action();
        mix_ui_tick(v, step(1));
        assert(toast.kind == kinds[i] && toast.value == values[i] && toast.shown && !toast.dirty);
        assert(draws == n + 1 && full_draws == full);
        assert(presented_pixels - pixels == 640u * 104u); assert_toast_present();
        assert(fb[(TOAST_Y + 75) * W + 600] == ((i == 0 || i == 3) ? P.card : P.accent));
    }
    uint32_t remaining = 1499u - (uint32_t)(clk - toast.at);
    n = draws; mix_ui_tick(v, step(remaining));
    assert(toast.active && draws == n);
    mix_ui_tick(v, step(1));
    assert(!toast.active && !toast.shown && !toast.dirty && full_draws == full + 1);
    assert(!memcmp(checkpoint, fb, sizeof(checkpoint)) && !memcmp(screen, fb, sizeof(screen)));
    no_action();

    prepare_terminal(v);
    mix_ui_feedback(MIX_UI_VOLUME, 70); mix_ui_tick(v, step(1));
    assert_toast_present(); memcpy(checkpoint, fb, sizeof(checkpoint)); full = full_draws;
    const char *updates[] = {"\033[14;1H\033[41mUPDATED DURING TOAST", "\033[16;1H\033[42mLATEST BOTTOM ROW"};
    for (unsigned i = 0; i < sizeof(updates) / sizeof(updates[0]); i++) {
        int row = i ? 15 : 13;
        mix_terminal_feed((const uint8_t *)updates[i], strlen(updates[i]));
        assert(mix_terminal_dirty(row));
        mix_ui_tick(v, step(60));
        assert(toast.active && full_draws == full && !mix_terminal_dirty(row));
        /* Ignore the rounded transparent corners; the complete opaque center
         * must remain identical even when dirty terminal rows cross it. */
        assert_region_equal(checkpoint, fb, 216, TOAST_Y + 24, 808, TOAST_Y + 80);
        assert_region_equal(checkpoint, screen, 216, TOAST_Y + 24, 808, TOAST_Y + 80);
        assert(!memcmp(screen, fb, sizeof(screen)));
    }
    mix_ui_tick(v, step(1500u - (uint32_t)(clk - toast.at)));
    assert(!toast.active && full_draws == full + 1);
    assert(mix_terminal_row(13)[0].codepoint == 'U' && mix_terminal_row(15)[0].codepoint == 'L');
    assert(memcmp(checkpoint, fb, sizeof(checkpoint)) && !memcmp(screen, fb, sizeof(screen)));
    assert(!mix_terminal_dirty(13) && !mix_terminal_dirty(15)); no_action();

    prepare_transition(v, 3);
    assert(fake_fade && content_motion.phase == CONTENT_IN);
    full = full_draws; memcpy(checkpoint, fb, sizeof(checkpoint));
    mix_terminal_feed((const uint8_t *)"\rFROZEN UPDATE", 13);
    for (int i = 0; i < 4; i++) {
        n = draws;
        mix_ui_feedback(i & 1 ? MIX_UI_BRIGHTNESS : MIX_UI_VOLUME, 30 + i);
        assert(draws == n && !memcmp(checkpoint, fb, sizeof(checkpoint)));
        mix_ui_tick(v, step(35));
        assert(content_motion.phase == CONTENT_IN && fake_fade);
        assert(toast.active && toast.dirty && !toast.shown && mix_terminal_dirty(0));
        assert(full_draws == full && !memcmp(checkpoint, fb, sizeof(checkpoint))); no_action();
    }
    mix_ui_tick(v, step(35));
    assert(content_motion.phase == CONTENT_IDLE && !fake_fade);
    assert(toast.active && toast.shown && !toast.dirty && toast.at == clk);
    assert(toast.kind == MIX_UI_BRIGHTNESS && toast.value == 33);
    assert(full_draws == full && mix_terminal_dirty(0)); assert_toast_present();
    mix_ui_tick(v, step(50)); assert(!mix_terminal_dirty(0));
    mix_ui_tick(v, step(1449)); assert(toast.active);
    mix_ui_tick(v, step(1)); assert(!toast.active);
    assert(!memcmp(screen, fb, sizeof(screen))); no_action();
    prepare_home(v);
    puts("toast runtime passed: partial present, consecutive updates, 1500ms expiry, terminal overlay retention, immutable incoming fade");
}

static void terminal_feed_text(const char *s) {
    mix_terminal_feed((const uint8_t *)s,strlen(s));
}
static void test_terminal_budget(mix_view_t *v) {
    for(int preset=0;preset<GEOMETRY_COUNT;preset++) {
        prepare_home(v);set_geometry(preset);note_geometry();drain();
        current_app=MIX_APP_SHELL;page=PAGE_APP;
        v->terminal_open=true;v->running_app=MIX_APP_SHELL;v->linux_online=true;
        mix_terminal_init();mix_terminal_resize(geom()->cols,geom()->rows);settle(v);
        terminal_feed_text("\033[?25l");mix_ui_tick(v,step(20));
        mix_ui_tick(v,step(TERM_FAST_MS));
        assert(!mix_ui_needs_fast_tick());
        unsigned n=draws,full=full_draws,d=deferred_draws;
        unsigned long long pixels=presented_pixels;
        terminal_feed_text("\033[1;1HA\033[11;1HZ\033[1;1H");
        mix_ui_tick(v,step(1));
        assert(draws==n+1&&deferred_draws==d+1&&full_draws==full);
        assert(last_rect_count==2&&presented_pixels-pixels==(unsigned long long)W*geom()->ch*2);
        assert(!memcmp(screen,fb,sizeof(screen))&&mix_ui_needs_fast_tick());
        assert(!mix_terminal_dirty(0)&&!mix_terminal_dirty(10));

        /* A burst is admitted at 16ms, not 50ms. No data means no present,
         * and idle iterations must not move the admission timestamp. */
        n=draws;terminal_feed_text("\033[1;1HB");
        mix_ui_tick(v,step(15));assert(draws==n&&mix_terminal_dirty(0));
        mix_ui_tick(v,step(1));assert(draws==n+1&&!mix_terminal_dirty(0));
        n=draws;uint32_t submitted=term_ms;
        mix_ui_tick(v,step(99));assert(draws==n&&term_ms==submitted&&mix_ui_needs_fast_tick());
        mix_ui_tick(v,step(1));assert(draws==n&&!mix_ui_needs_fast_tick());
        terminal_feed_text("\033[1;1HC");mix_ui_tick(v,step(1));assert(draws==n+1);

        /* Bottom-bar telemetry joins the same batch as terminal output. */
        n=draws;pixels=presented_pixels;v->host_time_s++;
        terminal_feed_text("\033[1;1HD");mix_ui_tick(v,step(1000));
        assert(draws==n+1&&last_rect_count==2&&last_rects[1].y0==STATUS_Y);
        assert(presented_pixels-pixels==(unsigned long long)W*(geom()->ch+STATUS_H));
        assert(!term_footer_dirty&&!memcmp(screen,fb,sizeof(screen)));

        /* Erase the previous underline when the cursor moves or hides. */
        terminal_feed_text("\033[1;1H\033[?25h");mix_ui_tick(v,step(16));
        int underline=term_oy()+geom()->ch-2;
        assert(screen[underline*W+term_ox()]==P.accent);
        terminal_feed_text("\033[11;1H");n=draws;mix_ui_tick(v,step(16));
        assert(draws==n+1&&screen[underline*W+term_ox()]==P.bg);
        assert(screen[(underline+10*geom()->ch)*W+term_ox()]==P.accent);
        terminal_feed_text("\033[?25l");mix_ui_tick(v,step(16));
        assert(screen[(underline+10*geom()->ch)*W+term_ox()]==P.bg);

        /* More sparse rows than descriptors, plus a changed toast/footer.
         * Every changed pixel must still be submitted, with a bounded batch. */
        for(int row=1;row<=geom()->rows;row+=2) {
            char b[32];snprintf(b,sizeof(b),"\033[%d;1HQ",row);terminal_feed_text(b);
        }
        terminal_feed_text("\033[11;1H");
        mix_ui_feedback(MIX_UI_VOLUME,51);v->host_time_s++;
        n=draws;mix_ui_tick(v,step(1000));
        assert(draws==n+1&&last_rect_count==MIX_PRESENT_MAX_RECTS);
        assert(!toast.dirty&&toast.shown&&!memcmp(screen,fb,sizeof(screen)));
        for(int row=0;row<geom()->rows;row++)assert(!mix_terminal_dirty(row));
        mix_ui_tick(v,step(1500));assert(!toast.active);

        /* Clock wrap preserves the admission and finite fast-tick interval. */
        clk=UINT32_MAX-8;view_ms=clk;term_ms=clk-TERM_FRAME_MS;
        terminal_feed_text("\033[1;1HR");mix_ui_tick(v,clk);
        n=draws;terminal_feed_text("\033[1;1HS");
        mix_ui_tick(v,step(15));assert(draws==n);
        mix_ui_tick(v,step(1));assert(draws==n+1&&mix_ui_needs_fast_tick());
        mix_ui_tick(v,step(TERM_FAST_MS));assert(!mix_ui_needs_fast_tick());

        /* Failed handoff keeps model dirtiness. The full-repaint recovery
         * clears deferred work before a later content fade can borrow it. */
        terminal_feed_text("\033[1;1HT");n=terminal_cleans;
        next_draw_error=ESP_ERR_INVALID_STATE;mix_ui_tick(v,step(16));
        assert(repaint&&!mix_ui_draw_healthy()&&mix_terminal_dirty(0)&&terminal_cleans==n);
        mix_ui_tick(v,step(20));assert(mix_ui_draw_healthy()&&!repaint);
        assert(!fake_deferred_pending&&!mix_terminal_dirty(0));
        assert(!memcmp(screen,fb,sizeof(screen)));
        terminal_feed_text("\033[1;1HU");mix_ui_tick(v,step(16));
        assert(fake_deferred_pending);
        navigate(PAGE_HOME);settle(v);drain();assert(!fake_deferred_pending);
        launch(2);for(int i=0;i<40;i++)mix_ui_tick(v,step(35));
        assert(!mix_ui_motion_active()&&mix_ui_draw_healthy());
        drain();
    }
    puts("terminal budget passed: sparse batches, one footer latch, cursor erasure, bounded rectangles, 16ms admission, idle sleep, clock wrap, retained failed rows");
}

int main(int argc, char **argv) {
    mix_terminal_init();
    assert(mix_ui_init((void *)1) == ESP_OK);
    assert(allocations == 1);
    assert(page==PAGE_HOME&&!mix_ui_locked()&&mix_ui_display_awake());
    note_geometry();
    /* The readable preset is the default on a 3.2 inch panel, and the parser
     * must already agree with it before a single frame is drawn. A stored
     * preference of zero is a real value, so only a missing key may fall back. */
    assert(geometry == 1 && mix_terminal_cols() == 64 && mix_terminal_rows() == 20);
    assert(geometries[0].cols == 80 && geometries[0].rows == 27);
    assert(geometries[1].cols == 64 && geometries[1].rows == 20);

    mix_view_t v = {0};
    mix_ui_tick(&v, step(1));
    assert(full_draws == 1);
    /* Nothing changed, so nothing is redrawn. */
    unsigned n = draws;
    mix_ui_tick(&v, step(50)); mix_ui_tick(&v, step(1000));
    assert(draws == n);

    if (argc > 1) {
        assert(argc == 2);
        if (!strcmp(argv[1], "unlocked")) test_unlocked_runtime(&v);
#ifdef MIX_TEST_TOUCH_PIPELINE
        else if (!strcmp(argv[1], "wait-input")) test_wait_input(&v);
        else if (!strcmp(argv[1], "touch_pipeline")) test_touch_pipeline(&v);
#endif
        else if (!strcmp(argv[1], "fixed-settings")) test_fixed_settings_runtime(&v);
        else if (!strcmp(argv[1], "settings-cancel")) test_settings_cancel_runtime(&v);
        else if (!strcmp(argv[1], "settings-budget")) test_settings_render_budget(&v);
        else if (!strcmp(argv[1], "empty")) test_empty_pages_runtime(&v);
        else if (!strcmp(argv[1], "notes-geometry")) test_notes_geometry(&v);
        else if (!strcmp(argv[1], "navigation")) test_navigation_runtime(&v);
        else if (!strcmp(argv[1], "layout")) test_layout_runtime(&v);
        else if (!strcmp(argv[1], "nav-icons")) test_nav_icons_runtime(&v);
        else if (!strcmp(argv[1], "card-style")) test_card_style_contract(&v);
        else if (!strcmp(argv[1], "terminal-budget")) test_terminal_budget(&v);
        else if (!strcmp(argv[1], "toast")) test_toast_runtime(&v);
        else assert(!"unknown host test scenario");
        assert(allocations == 1);
        assert(guarded[0] == 0xa55a && guarded[786433] == 0x5aa5);
        free(guarded);
        return 0;
    }

    /* Every page, section, theme and language has to draw inside the buffer. */
    for (int th = 0; th < 4; th++)
        for (int lang = 0; lang < 2; lang++)
            for (int p = 0; p < 3; p++)
                for (int s = 0; s < SEC_COUNT; s++) {
                    theme = (uint8_t)th; language = (uint8_t)lang;
                    page = (page_t)p; section = (section_t)s;settings_menu=false;
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

    /* Keyboard focus moves between all four cards; Settings starts no app. */
    page = PAGE_HOME; home_focus = 0; settle(&v); drain();
    mix_ui_key((const uint8_t *)"\033[C", 3); assert(home_focus == 1);
    mix_ui_key((const uint8_t *)"\033[B", 3); assert(home_focus == 3);
    mix_ui_key((const uint8_t *)"\r", 1);
    assert(page == PAGE_SETTINGS && settings_menu && !mix_ui_motion_active()); no_action();
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
        v.terminal_open = true; v.running_app = MIX_APP_SHELL; v.linux_online = true;
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
    settings_tap(96, 240);                       /* the first listed network */
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
    settings_tap(96, 240); type("secret");
    mix_ui_key((const uint8_t *)"\033", 1);
    assert(net_view == 0 && net_pass_len == 0 && net_pass[0] == 0);
    no_action();
    /* Forgetting a saved network asks for local consent first. */
    settle(&v);
    settings_tap(96, 240);
    settings_tap(420, 470);
    assert(modal == MIX_ACTION_NET_FORGET); no_action();
    settle(&v);
    mix_ui_key((const uint8_t *)"\033", 1);      /* rejected */
    assert(!modal); no_action();
    net_view = 0; network_count = 0;

    /* ---- maintenance still needs local consent ---- */
    go_section(SEC_SYSTEM); settle(&v); drain();
    settings_tap(96,540); assert(modal == MIX_ACTION_JOB_START); no_action();
    settle(&v);
    mix_ui_key((const uint8_t *)"\r", 1);
    assert(mix_ui_take_action(&a) && a.kind == MIX_ACTION_JOB_START);

    /* ---- the touch test updates its fixed page, never old scroll offsets ---- */
    go_section(SEC_DEVICE); settle(&v); drain();
    settings_tap(100,530); assert(touch_test);
    settle(&v);memcpy(checkpoint,fb,sizeof(checkpoint));
    mix_ui_touch(120,300,true);
    mix_ui_tick(&v,step(60));
    assert(last_y0>=0&&last_y1<=CONTENT_H);
    assert_region_equal(checkpoint,fb,0,STATUS_Y,W,H);
    assert(!memcmp(screen,fb,sizeof(screen)));
    mix_ui_touch(120,300,false);
    settings_tap(100,530);assert(!touch_test);

    test_launch_motion(&v);
    test_content_handoff(&v);

    /* ---- a missing font partition must still draw every page ---- */
    fake_font = false;
    for (int p = 0; p < 3; p++)
        for (int s = 0; s < SEC_COUNT; s++) {
            page = (page_t)p; section = (section_t)s;settings_menu=false;settle(&v);
        }

    assert(allocations == 1);
    assert(guarded[0] == 0xa55a && guarded[786433] == 0x5aa5);
    free(guarded);
    puts("UI host integration passed: all fixed page combinations, 24 launch motion combinations, "
         "bounded frames, wraparound, cancellation, readiness, draw recovery, present budget, launcher press/cancel, "
         "both terminal geometries, Wi-Fi passphrase lifetime, local consent, framebuffer guards");
    return 0;
}
