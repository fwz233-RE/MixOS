/* SPDX-License-Identifier: MIT
 * MixOS local interface: a status bar, a four-button launcher, a full-screen
 * application view and fixed settings pages for local controls/diagnostics.
 *
 * Drawing rules that keep this affordable on the 26 MHz RGB panel:
 *  - Exactly one 1.5 MiB PSRAM framebuffer. The panel's scanout buffer is the
 *    parent's and is never taken over.
 *  - Launcher motion moves only an icon/title region, at panel-paced intervals.
 *    The 300 ms clock starts AFTER the first complete frame is submitted.
 *  - Changed rectangles are copied to the idle RGB buffer, then latched at a
 *    frame boundary. No expanding, repeatedly cleared full-screen surface.
 *  - A touched card repaints its own band; waiting dots repaint only 32 rows.
 *    Application requests remain independent of the visual transition.
 */
#include "mix_ui.h"
#include "mix_link.h"
#include "mix_md3.h"
#include "mix_game_icon.h"
#include "mix_nav_icons.h"
#include "mix_terminal.h"
#include "mix_present.h"
#include "esp_timer.h"
#include "ttf_font.h"
#include "batt_log.h"
#include "esp_app_desc.h"
#include "esp_heap_caps.h"
#include "nvs.h"
#include <time.h>
#include <math.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#define W 1024
#define H 768
#define STATUS_H 120
#define STATUS_Y (H-STATUS_H)
#define CONTENT_H STATUS_Y
_Static_assert(CONTENT_H == MIX_PRESENT_BODY_END, "UI/presenter content bounds disagree");
/* Settings are a set of fixed pages. There is deliberately no content drag or
 * scroll offset: every control is placed inside the 648-pixel body viewport. */
static bool settings_menu = true;
/* A fixed-page tap is accepted only at its original, validated contact. */
static bool settings_tap_active;
static int settings_tap_x, settings_tap_y;
static int draw_offset_y, draw_limit_y = CONTENT_H, draw_clip_y0;
static int settings_scroll, settings_drag_y = -1;
static int settings_painted_scroll;
static bool settings_dragged, settings_body_dirty, settings_scroll_dirty;
static bool settings_scrolling __attribute__((unused));
static uint32_t settings_frame_ms;
/* Lock-screen state is retained in the ABI for old callers, but the product
 * no longer enters this state. The physical lock key is intentionally inert. */
static bool locked = false, display_awake = true;
static bool touch_block_until_up;
static uint32_t locked_at, lock_awake_at;
static int unlock_x = -1, unlock_y, unlock_last_x, unlock_last_y;
static bool unlock_tracking, unlock_ready;
static bool lock_touch_active, lock_feedback_visible, lock_feedback_dirty, lock_returning;
static int lock_touch_x, lock_touch_y, lock_offset, lock_return_from;
static uint32_t lock_feedback_at, lock_release_at, lock_frame_ms;
static mix_present_rect_t lock_previous[3];
static int lock_word_width;
static bool lock_footer_dirty, settings_footer_dirty;
#define TOAST_Y (CONTENT_H-116)
#define TOAST_H 104
static struct { bool active, dirty, shown; mix_ui_feedback_t kind; int value; uint32_t at; } toast;
static void toast_draw(void);
static __attribute__((unused)) void lock_draw(void);
static __attribute__((unused)) void lock_feedback_clear(void);
static void navigation_back(void);
static void navigation_settings(void);
static void settings_menu_open(void);
#define RGB(r,g,b) ((uint16_t)((((r)>>3)<<11)|(((g)>>2)<<5)|((b)>>3)))
_Static_assert(W * H * sizeof(uint16_t) == 1572864, "UI framebuffer must be 1.5 MiB");

typedef enum { PAGE_HOME, PAGE_APP, PAGE_SETTINGS } page_t;
typedef enum {
 SEC_APPEARANCE, /* Theme */
 SEC_DISPLAY,    /* Brightness and language */
 SEC_INPUT,      /* Volume, keyboard light and terminal size */
 SEC_NETWORK,
 SEC_POWER,
 SEC_DEVICE,
 SEC_SYSTEM,
 SEC_ABOUT,
 SEC_COUNT
} section_t;
typedef struct { uint16_t bg, card, raised, text, muted, accent, warning; } palette_t;
static uint8_t theme, language, geometry;
static const palette_t palettes[MIX_THEME_COUNT] = {
 {RGB(19,23,26),RGB(29,35,39),RGB(43,51,56),RGB(240,245,243),RGB(163,178,176),RGB(135,235,194),RGB(244,190,111)},
 {RGB(241,244,240),RGB(255,255,251),RGB(222,231,221),RGB(29,46,38),RGB(78,101,90),RGB(24,116,81),RGB(151,80,14)},
 {RGB(17,25,42),RGB(26,38,59),RGB(39,55,80),RGB(235,242,255),RGB(165,187,215),RGB(136,193,255),RGB(255,202,132)},
 {RGB(29,24,22),RGB(42,34,29),RGB(60,48,36),RGB(255,242,221),RGB(198,176,146),RGB(244,188,106),RGB(255,154,128)},
 {RGB(14,25,36),RGB(22,40,58),RGB(36,62,84),RGB(235,247,255),RGB(168,202,219),RGB(110,201,255),RGB(255,183,137)},
 {RGB(246,242,251),RGB(255,250,255),RGB(230,221,240),RGB(45,35,57),RGB(94,78,110),RGB(100,78,170),RGB(178,66,80)},
 {RGB(39,18,26),RGB(56,25,37),RGB(80,36,53),RGB(255,236,239),RGB(220,165,181),RGB(255,143,177),RGB(255,183,129)},
 {RGB(255,248,238),RGB(255,253,245),RGB(244,228,198),RGB(60,44,20),RGB(111,88,54),RGB(149,91,0),RGB(186,58,35)},
 {RGB(11,31,30),RGB(16,48,46),RGB(24,69,65),RGB(226,250,246),RGB(157,201,194),RGB(83,218,200),RGB(255,185,128)},
 {RGB(241,243,255),RGB(251,252,255),RGB(219,225,250),RGB(30,39,79),RGB(77,91,133),RGB(70,83,170),RGB(181,69,65)},
 {RGB(40,22,20),RGB(60,30,28),RGB(84,43,39),RGB(255,237,234),RGB(224,166,156),RGB(255,146,122),RGB(255,193,96)},
 {RGB(246,248,237),RGB(254,255,248),RGB(224,233,197),RGB(38,48,28),RGB(83,99,67),RGB(83,117,24),RGB(179,65,17)}
};
#define P (palettes[theme<MIX_THEME_COUNT?theme:0])

/* Both presets fit inside the parser's 80x28 buffer, so switching costs no
 * extra memory. The 3.2 inch panel is why the larger cell exists at all. */
typedef struct { uint8_t cols, rows, cw, ch, font; } geometry_t;
static const geometry_t geometries[2] = {
 {80, 27, 12, 24, 20},   /* compact: 960x648 above the bottom bar */
 {64, 20, 16, 32, 26},   /* large:   1024x640 */
};
#define GEOMETRY_COUNT 2
/* Document text is deliberately larger than the general terminal. This is a
 * per-app override, not a change to the saved user geometry preference. */
static const geometry_t notes_geometry = {48, 16, 21, 40, 34};

static uint16_t *fb;
static bool first_frame_presented;
static esp_err_t last_draw_error=ESP_ERR_INVALID_STATE;
static mix_view_t view;
static page_t page = PAGE_HOME;
static section_t section;
static int volume_percent = -1;
static bool repaint = true, have_view, touch_down;
static int tx, ty;
static int pressed_card = -1, home_focus, nav_pressed = -1;
static bool pressed_inside;
/* Terminal scroll drag: the y the last movement was accounted at, and the
 * sub-row remainder carried forward so a slow drag still moves the view. -1
 * means no drag is in progress. */
static int drag_y = -1, drag_rest;
/* The launcher decides which application the view is showing. The link's own
 * session state answers whether it is running, which is a different question. */
static uint8_t current_app = MIX_APP_SHELL;
static uint32_t seen_terminal_exit;
/* UI-only destination: never serialize it as a host application ID. */
enum { UI_APP_GAME = 0x80 };
static bool app_has_session(void) {
 return current_app==MIX_APP_TRANSLATE||current_app==MIX_APP_NOTES||current_app==MIX_APP_SHELL;
}
static bool touch_test, test_dirty;
static uint32_t view_ms, term_ms, ui_ms, wait_ms, app_enter_ms;
static uint32_t touch_ms __attribute__((unused));
/* This is an admission interval, NOT the LCD refresh rate. The synchronous
 * presenter still paces actual frames at the unchanged 26 MHz panel clock. */
#define TERM_FRAME_MS 16u
#define TERM_FAST_MS 100u
static bool term_recent, term_footer_dirty;
static uint32_t term_present_ms;
/* Admission only: the presenter already waits for the LCD frame boundary.
 * Another whole 35 ms AFTER that wait made fades miss every other scan. */
#define MOTION_FRAME_MS 16u
#define MOTION_SCALE 1024u
#define WAIT_DOTS_Y 532
#define WAIT_DOTS_H 32
typedef struct { int x0,y0,x1,y1; } ui_rect_t;
static struct {
 bool active, pending;
 uint8_t card;
 uint16_t progress;
 uint32_t start_ms, frame_ms;
 ui_rect_t previous;
} launch_motion;
#define FADE_OUT_MS 100u
#define FADE_IN_MS 175u
#define FIRST_CONTENT_GRACE_MS 750u
typedef enum { CONTENT_IDLE, CONTENT_OUT, CONTENT_PREPARE, CONTENT_WAIT, CONTENT_IN } content_phase_t;
static struct {
 content_phase_t phase;
 bool waiting, open_seen, render_target, waiting_surface;
 uint32_t start_ms, frame_ms, opened_ms;
 uint32_t transitions, out_frames, in_frames, render_us, max_present_us;
} content_motion;
static void content_cancel(void);
static struct {
 uint32_t animations, frames, last_frames, last_duration_ms, max_gap_ms;
 uint32_t draw_max_us, present_max_us, first_frame_us, last_present_ms;
 uint32_t cancelled;
} motion_stats;
static uint32_t ui_clock_ms(void){return (uint32_t)(esp_timer_get_time()/1000);}
typedef struct {
 uint32_t frames,draw_max_us,present_max_us,max_gap_ms,last_ms,pixels,rows;
 bool timed;
} gesture_stats_t;
static gesture_stats_t lock_stats,settings_stats;
static void gesture_presented(gesture_stats_t *stats,uint32_t draw_us,uint32_t present_us,uint32_t pixels) {
 if(last_draw_error!=ESP_OK)return;
 uint32_t stamp=ui_clock_ms();
 if(stats->timed&&(uint32_t)(stamp-stats->last_ms)>stats->max_gap_ms)
  stats->max_gap_ms=stamp-stats->last_ms;
 if(draw_us>stats->draw_max_us)stats->draw_max_us=draw_us;
 if(present_us>stats->present_max_us)stats->present_max_us=present_us;
 stats->last_ms=stamp;stats->timed=true;stats->frames++;stats->pixels=pixels;
}
static uint32_t elapsed_us(int64_t start){return (uint32_t)(esp_timer_get_time()-start);}
static void launch_motion_end(bool cancelled);
static int last_cursor_x = -1, last_cursor_y = -1, last_scroll_offset = -1;
static bool last_cursor_visible;
static int modal; /* 0 none, otherwise mix_action_kind_t; only local input accepts */
static mix_action_t actions[8];
static unsigned action_read, action_write;
static char notice[96];
static bool notice_visible;
static uint32_t notice_at;
#define NOTICE_H 104
#define NOTICE_MS 5000u
static batt_sample_t history[BATT_LOG_CAP]; /* 4.3 KiB RAM snapshot, never samples */
/* Network sub-view state. The passphrase never leaves this buffer except as a
 * single NET_CONNECT action, and is wiped on the tick after that action is
 * taken, once the parent has had its chance to read it. */
static int net_view, net_scroll;
static bool net_reveal, net_pass_expire;
static char net_ssid[MIX_SSID_MAX + 1];
static char net_pass[64];
static uint8_t net_pass_len;
static mix_net_entry_t net_painted_list[MIX_NET_MAX];
static int net_painted_count = -1;
static bool net_painted_busy;
static char net_painted_message[96];
static bool network_content_changed(void) {
 const mix_net_entry_t *list=NULL;int count=mix_link_net_list(&list);
 const char *message=mix_link_net_message();
 return count!=net_painted_count||net_painted_busy!=mix_link_net_busy()||
        strcmp(net_painted_message,message?message:"")||
        (count>0&&memcmp(net_painted_list,list,(size_t)count*sizeof(*list)));
}
static void network_content_record(void) {
 const mix_net_entry_t *list=NULL;int count=mix_link_net_list(&list);
 const char *message=mix_link_net_message();
 net_painted_count=count;net_painted_busy=mix_link_net_busy();
 snprintf(net_painted_message,sizeof(net_painted_message),"%s",message?message:"");
 if(count>0)memcpy(net_painted_list,list,(size_t)count*sizeof(*list));
}

static const char *tr(const char *en, const char *zh) { return language ? zh : en; }
static int clamp(int a, int lo, int hi) { return a < lo ? lo : a > hi ? hi : a; }
static bool inside(int x,int y,int rx,int ry,int rw,int rh){return x>=rx&&x<rx+rw&&y>=ry&&y<ry+rh;}
static int ratio_width(float value,int width){if(!isfinite(value)||value<=0)return 0;if(value>=1)return width;return (int)(value*width);}
static const geometry_t *geom(void){
 if(page==PAGE_APP&&current_app==MIX_APP_NOTES)return &notes_geometry;
 return &geometries[geometry<GEOMETRY_COUNT?geometry:0];
}
static int term_ox(void){return (W-geom()->cols*geom()->cw)/2;}
static int term_oy(void){return (CONTENT_H-geom()->rows*geom()->ch)/2;}

/* ---------- primitives ---------- */
static void rect(int x,int y,int w,int h,uint16_t c) {
 y+=draw_offset_y;
 int left=clamp(x,0,W),top=clamp(y,draw_clip_y0,draw_limit_y);
 int right=clamp(x+w,0,W),bottom=clamp(y+h,0,draw_limit_y);
 if(left>=right||top>=bottom)return;
 uint16_t *row=fb+(size_t)top*W+left;
 for(int i=0;i<right-left;i++)row[i]=c;
 /* Only generate one scanline; optimized memcpy fills the remaining rows. */
 for(int j=top+1;j<bottom;j++)memcpy(fb+(size_t)j*W+left,row,(size_t)(right-left)*2);
}
static void roundbox(int x,int y,int w,int h,int r,uint16_t c) {
 int top=y+draw_offset_y;
 if(w<=0||h<=0||top+h<=draw_clip_y0||top>=draw_limit_y)return;
 if(r<0)r=0;
 if(r*2>w)r=w/2;
 if(r*2>h)r=h/2;
 /* Disjoint spans: the old two large rectangles painted their overlap twice. */
 rect(x,y+r,w,h-2*r,c);
 for(int dy=0;dy<r;dy++) {
  int inset=0;
  while(inset<r&&(r-inset)*(r-inset)+(r-dy)*(r-dy)>r*r)inset++;
  rect(x+inset,y+dy,w-2*inset,1,c);
  rect(x+inset,y+h-1-dy,w-2*inset,1,c);
 }
}
static void frame(int x,int y,int w,int h,int r,int thickness,uint16_t c) {
 roundbox(x,y,w,h,r,c);
 roundbox(x+thickness,y+thickness,w-2*thickness,h-2*thickness,r>thickness?r-thickness:0,P.card);
}
/* Emergency font: independent 5x7 alphabet/digits, for a missing font
 * partition. Normal text and terminal cells use the TTF renderer. */
static const uint8_t fallback[][5] = {
 {0x7e,0x11,0x11,0x11,0x7e},{0x7f,0x49,0x49,0x49,0x36},{0x3e,0x41,0x41,0x41,0x22},
 {0x7f,0x41,0x41,0x22,0x1c},{0x7f,0x49,0x49,0x49,0x41},{0x7f,9,9,9,1},
 {0x3e,0x41,0x49,0x49,0x7a},{0x7f,8,8,8,0x7f},{0,0x41,0x7f,0x41,0},
 {0x20,0x40,0x41,0x3f,1},{0x7f,8,0x14,0x22,0x41},{0x7f,0x40,0x40,0x40,0x40},
 {0x7f,2,0x0c,2,0x7f},{0x7f,4,8,0x10,0x7f},{0x3e,0x41,0x41,0x41,0x3e},
 {0x7f,9,9,9,6},{0x3e,0x41,0x51,0x21,0x5e},{0x7f,9,0x19,0x29,0x46},
 {0x46,0x49,0x49,0x49,0x31},{1,1,0x7f,1,1},{0x3f,0x40,0x40,0x40,0x3f},
 {0x1f,0x20,0x40,0x20,0x1f},{0x3f,0x40,0x38,0x40,0x3f},{0x63,0x14,8,0x14,0x63},
 {7,8,0x70,8,7},{0x61,0x51,0x49,0x45,0x43},
 {0x3e,0x51,0x49,0x45,0x3e},{0,0x42,0x7f,0x40,0},{0x42,0x61,0x51,0x49,0x46},
 {0x21,0x41,0x45,0x4b,0x31},{0x18,0x14,0x12,0x7f,0x10},{0x27,0x45,0x45,0x45,0x39},
 {0x3c,0x4a,0x49,0x49,0x30},{1,0x71,9,5,3},{0x36,0x49,0x49,0x49,0x36},{6,0x49,0x49,0x29,0x1e}
};
static void fallback_char(int x,int y,int scale,uint32_t ch,uint16_t color) {
 if(ch==' ') return;
 if(ch>='a'&&ch<='z') ch-=32;
 int n=ch>='A'&&ch<='Z'?(int)ch-'A':ch>='0'&&ch<='9'?(int)ch-'0'+26:-1;
 if(n>=0) { for(int i=0;i<5;i++) for(int j=0;j<7;j++) if(fallback[n][i]&(1<<j)) rect(x+i*scale,y+j*scale,scale,scale,color); }
 else if(ch=='.') rect(x+2*scale,y+6*scale,scale,scale,color);
 else if(ch=='-'||ch=='_') rect(x,y+(ch=='-'?3:6)*scale,5*scale,scale,color);
 else if(ch==':') {rect(x+2*scale,y+2*scale,scale,scale,color);rect(x+2*scale,y+5*scale,scale,scale,color);}
 else if(ch=='%') {rect(x,y,scale,2*scale,color);rect(x+4*scale,y+5*scale,scale,2*scale,color);rect(x+2*scale,y+3*scale,scale,scale,color);}
 else {rect(x,y,5*scale,scale,color);rect(x,y,scale,7*scale,color);rect(x+4*scale,y,scale,7*scale,color);rect(x,y+6*scale,5*scale,scale,color);}
}
static bool text_visible(int y,int size) {
 int py=y+draw_offset_y,margin=size/3+4;
 return py+size+margin>draw_clip_y0&&py-margin<draw_limit_y;
}
static void text(int x,int y,int size,uint16_t color,const char *s) {
 if(!s)return;
 int py=y+draw_offset_y;
 /* Settings scrolling used to rasterize every off-screen label before the
  * destination clip rejected it. Skip invisible text before invoking TTF;
  * this keeps scroll cost proportional to the visible viewport. */
 if(!text_visible(y,size))return;
 if(ttf_font_ready()) {
  if(draw_clip_y0==0)ttf_draw_text(fb,W,draw_limit_y,x,py,size,color,s);
  else ttf_draw_text_clipped(fb,W,H,draw_clip_y0,draw_limit_y,x,py,size,color,s);
  return;
 }
 int scale=size>=26?3:2;
 for(;*s;s++) { if(((uint8_t)*s&0xc0)==0x80) continue; fallback_char(x,y,scale,(uint8_t)*s,color);x+=6*scale; }
}
static int text_width(int size,const char *s) {
 if(!s)return 0;
 if(ttf_font_ready())return ttf_text_width(size,s);
 int scale=size>=26?3:2,n=0;
 for(const char *p=s;*p;p++) if(((uint8_t)*p&0xc0)!=0x80) n++;
 return n*6*scale;
}
static void text_mid(int cx,int y,int size,uint16_t color,const char *s){if(text_visible(y,size))text(cx-text_width(size,s)/2,y,size,color,s);}

/* ---------- interface icons ----------
 * An icon is a glyph in the Private Use Area of the same font partition MiSans
 * lives in. tools/build_font.py reads the ICON_* literals below straight out of
 * this file and merges exactly those glyphs in from Material Symbols, so the
 * set of icons the device can draw is defined here and nowhere else.
 *
 * The same caveat as any new character applies: the font partition is not
 * rewritten by an OTA, so an icon added here stays blank until the font is
 * flashed over USB.
 *
 * Icons are placed by their own ink box rather than by the text baseline. A
 * launcher icon has to sit in the optical centre of its container, and the
 * font's line metrics describe a line of text, not a 70 px pictogram. */
#define ICON_TRANSLATE      "\ue8e2"  /* translate */
#define ICON_NOTES          "\ue745"  /* edit_note */
#define ICON_AGENT          "\uf06c"  /* smart_toy */
#define ICON_SETTINGS       "\ue8b8"  /* settings; retain installed font coverage */
#define ICON_APPEARANCE     "\ue40a"  /* palette */
#define ICON_NETWORK        "\ue63e"  /* wifi */
#define ICON_POWER          "\uea0b"  /* bolt */
#define ICON_DEVICE         "\ue322"  /* memory */
#define ICON_SYSTEM         "\ue88e"  /* info */
#define ICON_WIFI_UNKNOWN   "\uf067"  /* signal_wifi_statusbar_null */
#define ICON_WIFI_OFF       "\ue1da"  /* signal_wifi_off */
#define ICON_WIFI_0         "\uf0b0"  /* signal_wifi_0_bar */
#define ICON_WIFI_1         "\uebe4"  /* network_wifi_1_bar */
#define ICON_WIFI_2         "\uebd6"  /* network_wifi_2_bar */
#define ICON_WIFI_3         "\uebe1"  /* network_wifi_3_bar */
#define ICON_WIFI_4         "\ue1ba"  /* network_wifi */
#define ICON_BATT_0         "\uebdc"  /* battery_0_bar */
#define ICON_BATT_1         "\uf09c"  /* battery_1_bar */
#define ICON_BATT_2         "\uf09d"  /* battery_2_bar */
#define ICON_BATT_3         "\uf09e"  /* battery_3_bar */
#define ICON_BATT_4         "\uf09f"  /* battery_4_bar */
#define ICON_BATT_5         "\uf0a0"  /* battery_5_bar */
#define ICON_BATT_6         "\uf0a1"  /* battery_6_bar */
#define ICON_BATT_FULL      "\ue1a5"  /* battery_full */
#define ICON_BATT_CHARGING  "\ue1a3"  /* battery_charging_full */
#define ICON_BATT_ALERT     "\ue19c"  /* battery_alert */
#define ICON_BATT_UNKNOWN   "\ue1a6"  /* battery_unknown */

/* RGB565 alpha blend, the same one ttf_font.c uses: a glyph bitmap is an
 * antialiased coverage mask, so it needs a per-channel mix, not a threshold. */
static inline uint16_t blend565(uint16_t dst,uint16_t fg,uint8_t a) {
 if(a>=250)return fg;
 uint32_t dr=(dst>>11)&0x1F,dg=(dst>>5)&0x3F,db=dst&0x1F;
 uint32_t fr=(fg>>11)&0x1F,fgg=(fg>>5)&0x3F,fbb=fg&0x1F;
 uint32_t r=(dr*(255u-a)+fr*a+127u)/255u;
 uint32_t g=(dg*(255u-a)+fgg*a+127u)/255u;
 uint32_t b=(db*(255u-a)+fbb*a+127u)/255u;
 return (uint16_t)((r<<11)|(g<<5)|b);
}
/* Every ICON_* literal is one Private Use Area codepoint, which UTF-8 always
 * encodes in three bytes; ASCII is accepted so a caller can pass a plain
 * character while an icon is being chosen. */
static uint32_t icon_codepoint(const char *s) {
 const uint8_t *p=(const uint8_t*)s;
 if(!p||!p[0])return 0;
 if(p[0]<0x80)return p[0];
 if((p[0]&0xF0)==0xE0&&p[1]&&p[2])
  return ((uint32_t)(p[0]&0x0F)<<12)|((uint32_t)(p[1]&0x3F)<<6)|(uint32_t)(p[2]&0x3F);
 return 0;
}
/* Shared, ink-centred alpha compositing for live font glyphs and the official
 * pre-rasterized game glyph. Both use identical RGB565 blending and clipping. */
static void icon_alpha(int cx,int cy,uint16_t color,const uint8_t *bitmap,int w,int h) {
 if(!bitmap||w<=0||h<=0)return;
 int x0=cx-w/2,y0=cy-h/2+draw_offset_y;
 for(int row=0;row<h;row++) {
  int fy=y0+row;
  if(fy<draw_clip_y0||fy>=draw_limit_y)continue;
  const uint8_t *src=bitmap+(size_t)row*w;
  uint16_t *dst=fb+(size_t)fy*W;
  for(int col=0;col<w;col++) {
   int fx=x0+col;
   if(fx<0||fx>=W)continue;
   uint8_t a=src[col];
   if(a)dst[fx]=blend565(dst[fx],color,a);
  }
 }
}
/* Draws `glyph` centred on (cx,cy). Silently draws nothing when the font
 * partition is missing, which is the same degradation the text path takes. */
static void icon(int cx,int cy,int size,uint16_t color,const char *glyph) {
 if(!ttf_font_ready())return;
 int py=cy+draw_offset_y;
 if(py+size/2<=draw_clip_y0||py-size/2>=draw_limit_y)return;
 uint32_t cp=icon_codepoint(glyph);
 ttf_glyph_t g;
 if(!cp||!ttf_font_glyph(cp,size,&g))return;
 icon_alpha(cx,cy,color,g.bitmap,g.w,g.h);
}

/* Fixed-size text is ellipsized at UTF-8 boundaries rather than shrunk into
 * unreadability. The width comes from the same renderer that draws the text. */
static const char *fit_text(int size,int width,const char *s,char *out,size_t cap) {
 if(text_width(size,s)<=width)return s;
 size_t n=strlen(s);if(n>cap-4)n=cap-4;
 while(n&&((uint8_t)s[n]&0xc0)==0x80)n--;
 memcpy(out,s,n);
 for(;;) {
  memcpy(out+n,"...",4);
  if(text_width(size,out)<=width||!n)return text_width(size,out)<=width?out:"";
  do {n--;} while(n&&((uint8_t)out[n]&0xc0)==0x80);
 }
}
static void text_fit(int x,int y,int width,int size,uint16_t color,const char *s) {
 if(!text_visible(y,size))return;
 char clipped[192];text(x,y,size,color,fit_text(size,width,s,clipped,sizeof(clipped)));
}
static void text_mid_fit(int cx,int y,int width,int size,uint16_t color,const char *s) {
 if(!text_visible(y,size))return;
 char clipped[192];text_mid(cx,y,size,color,fit_text(size,width,s,clipped,sizeof(clipped)));
}
static void label(int x,int y,const char *en,const char *zh) {text(x,y,20,P.muted,tr(en,zh));}
#define UI_BUTTON_H MIX_TOUCH_MIN
static void button(int x,int y,int w,const char *en,const char *zh,bool active) {
 int h=modal?44:UI_BUTTON_H,size=modal?MIX_TYPE_LABEL_M:MIX_TYPE_BODY_L;
 roundbox(x,y,w,h,14,active?P.accent:P.raised);
 text_mid_fit(x+w/2,y+(h-size)/2,w-24,size,active?P.bg:P.text,tr(en,zh));
}
static void line(int x0,int y0,int x1,int y1,uint16_t c) {
 int dx=abs(x1-x0),sx=x0<x1?1:-1,dy=-abs(y1-y0),sy=y0<y1?1:-1,err=dx+dy;
 for(;;) {rect(x0,y0,2,2,c);if(x0==x1&&y0==y1)break;int e=2*err;if(e>=dy){err+=dy;x0+=sx;}if(e<=dx){err+=dx;y0+=sy;}}
}
static void present_rect(int x0,int y0,int x1,int y1) {
 x0=clamp(x0,0,W);x1=clamp(x1,0,W);y0=clamp(y0,0,H);y1=clamp(y1,0,H);
 if(y1<=y0||x1<=x0)return;
 int64_t began=esp_timer_get_time();
 esp_err_t e=mix_present_rect(x0,y0,x1,y1,fb);
 uint32_t spent=elapsed_us(began);
 if(launch_motion.active&&spent>motion_stats.present_max_us)motion_stats.present_max_us=spent;
 if(e!=ESP_OK) {
  last_draw_error=e;repaint=true;
 } else if(x0==0&&x1==W&&y0==0&&y1==H) {
  first_frame_presented=true;last_draw_error=ESP_OK;
 }
}
static void present_batch(const mix_present_rect_t *regions,unsigned count) {
 if(!count)return;
 esp_err_t e=mix_present_rects(regions,count,fb);
 if(e!=ESP_OK){last_draw_error=e;repaint=true;}
}
static void present_deferred_batch(const mix_present_rect_t *regions,unsigned count) {
 if(!count)return;
 esp_err_t e=mix_present_rects_deferred(regions,count,fb);
 if(e!=ESP_OK){last_draw_error=e;repaint=true;}
}
static void present(int y0,int y1){present_rect(0,y0,W,y1);}
bool mix_ui_draw_healthy(void) {return fb&&first_frame_presented&&last_draw_error==ESP_OK;}
esp_err_t mix_ui_last_draw_error(void) {return last_draw_error;}

/* ---------- preferences ---------- */
static void save_prefs(void) {
 nvs_handle_t h;
 if(nvs_open("mixui",NVS_READWRITE,&h)==ESP_OK) {
  esp_err_t e=nvs_set_u8(h,"theme",theme);
  if(e==ESP_OK)e=nvs_set_u8(h,"lang",language);
  if(e==ESP_OK)e=nvs_set_u8(h,"term",geometry);
  if(e==ESP_OK)e=nvs_commit(h);
  nvs_close(h);
  if(e!=ESP_OK)mix_ui_notice("Preferences not saved");
 } else mix_ui_notice("Preferences not saved");
}
static void queue_value(mix_action_kind_t kind,int value) {
 unsigned next=(action_write+1)%8;
 if(next==action_read) {mix_ui_notice("Action queue full; retry");return;}
 actions[action_write]=(mix_action_t){.kind=kind,.value=value};action_write=next;
}
static void queue(mix_action_kind_t kind){queue_value(kind,0);}
static void navigate(page_t p) {
 if(page==p)return;
 /* Leaving an application ends it. The alternative - leaving it running in the
  * background - reads as a bug from the outside: the next application opened
  * finds the link busy, and a recogniser or a language model keeps its share of
  * a 4 GiB machine for an application nobody is looking at. The editor saves on
  * the way out, so nothing typed is lost. */
 /* Close an in-flight OPEN as well as an established session. Otherwise a
  * quick Home/Escape during the launch animation can leave a hidden app alive. */
 if(page==PAGE_APP&&app_has_session())queue(MIX_ACTION_TERMINAL_CLOSE);
 if(launch_motion.active)launch_motion_end(true);
 content_cancel();content_motion.waiting=false;content_motion.open_seen=false;
 page=p;repaint=true;pressed_card=-1;pressed_inside=false;
 settings_tap_active=false;nav_pressed=-1;
 /* Leaving text entry must not leave a password or reveal state behind. */
 if(p!=PAGE_SETTINGS) {
  net_view=0;net_reveal=false;net_pass_len=0;
  memset(net_pass,0,sizeof(net_pass));
 }
 if(p==PAGE_SETTINGS)settings_menu_open();
 /* OPEN carries the parser dimensions. Apply the app override before queuing
  * it, and restore the preference when leaving, without writing NVS. */
 const geometry_t *g=geom();
 if(mix_terminal_cols()!=g->cols||mix_terminal_rows()!=g->rows)
  mix_terminal_resize(g->cols,g->rows);
 last_cursor_x=last_cursor_y=-1;last_scroll_offset=-1;
 term_recent=term_footer_dirty=false;
 toast.active=toast.shown=toast.dirty=false;
 settings_drag_y=-1;
 drag_y=-1;drag_rest=0;
 app_enter_ms=ui_ms;wait_ms=ui_ms;
 /* Entering an application opens its session outright. There used to be a
  * floating button to open and close it by hand, which meant an application
  * could be on screen doing nothing until the user found and pressed it. The
  * page being open is the intent; local placeholders have no session. */
 if(p==PAGE_APP) {
  content_motion.waiting=app_has_session();
  mix_terminal_invalidate();
  if(app_has_session())queue_value(MIX_ACTION_APP_OPEN,current_app);
 }
}
static void go_section(section_t s) {
 if(s>=SEC_COUNT)return;
 section=s;settings_menu=false;settings_tap_active=false;net_view=0;net_scroll=0;settings_scroll=0;
 memset(net_pass,0,sizeof(net_pass));net_pass_len=0;net_reveal=false;
 repaint=true;
}
static void settings_menu_open(void) {
 settings_menu=true;settings_tap_active=false;section=SEC_APPEARANCE;net_view=0;net_scroll=0;settings_scroll=0;
 memset(net_pass,0,sizeof(net_pass));net_pass_len=0;net_reveal=false;
 repaint=true;
}
static section_t settings_category(int index) {
 static const section_t map[8]={SEC_APPEARANCE,SEC_DISPLAY,SEC_INPUT,SEC_NETWORK,SEC_POWER,SEC_DEVICE,SEC_SYSTEM,SEC_ABOUT};
 return index>=0&&index<8?map[index]:SEC_APPEARANCE;
}

/* ---------- status bar ---------- */
/* Battery and radio come from the icon font. The charge level picks one of the
 * eight discrete Material fill steps instead of the proportional bar this used
 * to draw: the gauge's own accuracy does not justify a continuous bar, and a
 * stepped icon matches the rest of the system's iconography. Colour still
 * carries the state that matters - unknown, low, charging. */
static void battery_icon(int cx,int cy,int size) {
 if(!view.soc_valid||!isfinite(view.soc)){icon(cx,cy,size,P.muted,ICON_BATT_UNKNOWN);return;}
 if(view.usb_valid&&isfinite(view.usb_v)&&view.usb_v>4.0f){icon(cx,cy,size,P.accent,ICON_BATT_CHARGING);return;}
 if(view.soc<15.0f){icon(cx,cy,size,P.warning,ICON_BATT_ALERT);return;}
 static const char *const steps[8]={ICON_BATT_0,ICON_BATT_1,ICON_BATT_2,ICON_BATT_3,
                                    ICON_BATT_4,ICON_BATT_5,ICON_BATT_6,ICON_BATT_FULL};
 icon(cx,cy,size,P.text,steps[clamp((int)view.soc*8/100,0,7)]);
}
/* `bars` is 0..4. Unknown means the host has not reported the radio at all,
 * which is a different statement from being disconnected, and the two use
 * different glyphs so neither is mistaken for the other. */
static void wifi_icon(int cx,int cy,int bars,bool unknown,bool connected,int size) {
 if(unknown){icon(cx,cy,size,P.muted,ICON_WIFI_UNKNOWN);return;}
 if(!connected){icon(cx,cy,size,P.muted,ICON_WIFI_OFF);return;}
 static const char *const steps[5]={ICON_WIFI_0,ICON_WIFI_1,ICON_WIFI_2,ICON_WIFI_3,ICON_WIFI_4};
 icon(cx,cy,size,P.text,steps[clamp(bars,0,4)]);
}
static void clock_string(char *out,size_t cap) {
 if(!view.host_time_s) {snprintf(out,cap,"--:--");return;}
 int64_t local=(int64_t)view.host_time_s+(int64_t)view.host_tz_offset_min*60;
 unsigned day_seconds=(unsigned)((local%86400+86400)%86400);
 snprintf(out,cap,"%02u:%02u",day_seconds/3600,(day_seconds/60)%60);
}
static void date_string(char *out,size_t cap) {
 if(!view.host_time_s) {snprintf(out,cap,"--/--/--");return;}
 /* Civil-date conversion keeps the firmware preview portable: newlib and the
  * Windows host harness expose different reentrant time APIs. */
 int64_t seconds=(int64_t)view.host_time_s+(int64_t)view.host_tz_offset_min*60;
 int64_t days=seconds/86400; if(seconds<0&&seconds%86400)days--;
 int64_t z=days+719468,era=(z>=0?z:z-146096)/146097;
 unsigned doe=(unsigned)(z-era*146097);
 unsigned yoe=(doe-doe/1460+doe/36524-doe/146096)/365;
 int year=(int)(yoe+era*400);
 unsigned doy=doe-(365*yoe+yoe/4-yoe/100);
 unsigned month_index=(5*doy+2)/153;
 unsigned day=doy-(153*month_index+2)/5+1;
 int month=(int)month_index+(month_index<10?3:-9);
 year+=(month<=2);
 snprintf(out,cap,"%04d-%02d-%02u",year,month,day);
}
static void speed_string(char *out,size_t cap) {
 if(!view.wifi_speed_valid||!isfinite(view.wifi_rx_bps)||view.wifi_rx_bps<0) {
  snprintf(out,cap,"--");return;
 }
 double rate=view.wifi_rx_bps;
 static const char *const units[]={"B/s","KB/s","MB/s","GB/s","TB/s"};
 int unit=0;
 while(rate>=999.5&&unit<4){rate/=1000.0;unit++;}
 if(rate>=999.5)snprintf(out,cap,"--");
 else snprintf(out,cap,rate>=9.95?"%.0f%s":"%.1f%s",rate,units[unit]);
}
static const char *app_title_en(void) {
 switch(current_app) {
 case MIX_APP_TRANSLATE: return "Live translation";
 case MIX_APP_NOTES: return "Notes";
 case MIX_APP_AGENT: return "Agent";
 case UI_APP_GAME: return "Games";
 default: return "Terminal";
 }
}
static const char *app_title_zh(void) {
 switch(current_app) {
 case MIX_APP_TRANSLATE: return "实时翻译";
 case MIX_APP_NOTES: return "记笔记";
 case MIX_APP_AGENT: return "智能体";
 case UI_APP_GAME: return "游戏";
 default: return "终端";
 }
}
/* The application session follows the page: navigate() opens it on the way in
 * and closes it on the way out.
 *
 * There was a floating action button here to open and close the session by
 * hand. It made the common case worse: opening an application put a page on
 * screen that did nothing until the button was found and pressed, and the
 * button's own label had to be read to know which state the session was in.
 * Being on the page is the intent, so the session now follows it.
 */

static void status_bar(void) {
 char b[64];
 int saved_offset=draw_offset_y,saved_limit=draw_limit_y,saved_clip=draw_clip_y0;
 draw_offset_y=0;draw_limit_y=H;draw_clip_y0=0;
 rect(0,STATUS_Y,W,STATUS_H,P.card);
 /* A separate high-contrast boundary, not another near-background surface. */
 rect(0,STATUS_Y,W,3,P.muted);
 /* Three 120x120 physical-pixel touch targets (48dp at 400 PPI):
  * Back, Home and an inactive Menu. */
 int cy=STATUS_Y+60;
 /* The third target is deliberately a visual menu affordance for a future
  * command palette. It has no action until that feature is designed. */
 uint16_t back=(page!=PAGE_HOME||modal)?P.text:P.muted;
 uint16_t nav=P.text;
 /* Back and Home use bundled Material Symbols Rounded coverage masks;
  * neither needs a font-partition update. */
 icon_alpha(60,cy,back,mix_nav_arrow_back_alpha,MIX_NAV_ARROW_BACK_WIDTH,MIX_NAV_ARROW_BACK_HEIGHT);
 icon_alpha(180,cy,nav,mix_nav_home_alpha,MIX_NAV_HOME_WIDTH,MIX_NAV_HOME_HEIGHT);
 /* Menu is intentionally inactive. Use a simple three-line glyph rather than
  * reusing the settings icon, so the visual affordance matches its meaning. */
 for(int i=-1;i<=1;i++)roundbox(280,cy+i*14,40,5,2,nav);
 const char *title=page==PAGE_APP?tr(app_title_en(),app_title_zh()):
                   page==PAGE_SETTINGS?tr("Settings","设置"):"MixOS";
 text_fit(384,STATUS_Y+14,220,32,P.text,title);
 const esp_app_desc_t *built=esp_app_get_description();
 if(built) {
  snprintf(b,sizeof(b),"%02x%02x%02x%02x",built->app_elf_sha256[0],built->app_elf_sha256[1],
           built->app_elf_sha256[2],built->app_elf_sha256[3]);
  text(390,STATUS_Y+72,20,P.muted,b);
 }
 clock_string(b,sizeof(b));text_mid(700,STATUS_Y+16,35,view.host_time_s?P.text:P.muted,b);
 date_string(b,sizeof(b));text_mid_fit(700,STATUS_Y+75,184,28,P.muted,b);
 int bars=view.wifi_reported&&view.wifi_connected&&view.wifi_signal>=0?1+view.wifi_signal*3/100:0;
 wifi_icon(858,STATUS_Y+39,bars,!view.wifi_reported,view.wifi_connected,44);
 speed_string(b,sizeof(b));text_mid_fit(858,STATUS_Y+75,132,28,P.muted,b);
 battery_icon(974,STATUS_Y+39,44);
 if(view.soc_valid&&isfinite(view.soc))snprintf(b,sizeof(b),"%d%%",clamp((int)view.soc,0,100));
 else snprintf(b,sizeof(b),"--");
 text_mid_fit(974,STATUS_Y+75,84,28,P.muted,b);
 if(!view.linux_online)text(390,STATUS_Y+96,16,P.warning,tr("Offline","离线"));
 else if(page==PAGE_APP&&app_has_session()&&mix_terminal_scroll_offset()) {
  snprintf(b,sizeof(b),"-%d",mix_terminal_scroll_offset());text(390,STATUS_Y+96,16,P.warning,b);
 }
 draw_offset_y=saved_offset;draw_limit_y=saved_limit;draw_clip_y0=saved_clip;
}

/* ---------- launcher ---------- */
#define CARD_W 468
#define CARD_H 288
#define CARD_ICON_SIZE MIX_DP(28)
#define CARD_TITLE_SIZE MIX_SP(20)
#define CARD_SUBTITLE_SIZE MIX_SP(13)
#define CARD_TITLE_Y MIX_DP(56)
#define CARD_SUBTITLE_Y MIX_DP(78)
static void card_rect(int index,int *x,int *y) {
 *x=32+(index%2)*(CARD_W+24);
 *y=24+(index/2)*(CARD_H+24);
}
static const char *const app_icons[4]={ICON_TRANSLATE,ICON_NOTES,ICON_AGENT,ICON_SETTINGS};
static const char *const app_names_en[4]={"Live translation","Notes","Agent","Settings"};
static const char *const app_names_zh[4]={"实时翻译","记笔记","智能体","设置"};
static void launcher_icon(int index,int cx,int cy,int size,uint16_t color) {
 icon(cx,cy,size,color,app_icons[index]);
}
static void launcher_card(int index,bool pressed) {
 int x,y;card_rect(index,&x,&y);
 /* Erase the old edge too: otherwise the inset leaves the unpressed outline. */
 rect(x,y,CARD_W,CARD_H,P.bg);
 static const char *sub_en[4]={"Speak, hear it back","Write and dictate","Code & tools","Configure the device"};
 static const char *sub_zh[4]={"说出来，听译文","书写与语音听写","编程与工具","配置设备"};
 int inset=pressed?6:0;
 roundbox(x+inset,y+inset,CARD_W-2*inset,CARD_H-2*inset,MIX_RADIUS_L,pressed?P.raised:P.card);
 if(home_focus==index&&!pressed)frame(x,y,CARD_W,CARD_H,MIX_RADIUS_L,3,P.accent);
 /* The icon sits in a tonal container rather than floating loose on the card.
  * This is what Material 3 uses to give a card one focal point, and it is the
  * difference between a card that reads as a surface with a hierarchy and one
  * that reads as four things scattered on a rectangle.
  *
  * Positions and type sizes are dp and sp through mix_md3.h rather than raw
  * pixels. The previous 34/20/18 px were 13.6/8/7.2 dp on this 400 PPI panel,
  * so the body text sat below the 11 sp floor of the Material scale - about
  * 1.3 mm of glass. That is the whole reason the page looked sparse and was
  * hard to read at arm's length.
  *
  * The four y positions are stated outright, in dp, because they have to be
  * checked against CARD_H by hand: 288 px is about 115 dp; the container,
  * title, subtitle and state line must fit without touching. */
 uint16_t holder=pressed?P.card:P.raised;
 int cs=MIX_DP(48);                        /* 120 px icon container */
 int cx=x+MIX_SPACE_4+inset,cy=y+MIX_SPACE_2+inset;
 roundbox(cx,cy,cs,cs,MIX_RADIUS_L,holder);
 /* 70 px glyph in a 120 px container, centred: the Material 3 ratio for an
  * icon inside a tonal holder. */
 launcher_icon(index,cx+cs/2,cy+cs/2,CARD_ICON_SIZE,P.accent);
 int tx0=x+MIX_SPACE_4+inset;
 /* Identical type, baseline offsets, width and colors for every card. An
  * entry changes content only; Agent has no alternate text renderer/style. */
 text_fit(tx0,y+CARD_TITLE_Y+inset,CARD_W-2*MIX_SPACE_4-2*inset,CARD_TITLE_SIZE,P.text,
          tr(app_names_en[index],app_names_zh[index]));
 text_fit(tx0,y+CARD_SUBTITLE_Y+inset,CARD_W-2*MIX_SPACE_4-2*inset,CARD_SUBTITLE_SIZE,P.muted,
          tr(sub_en[index],sub_zh[index]));
 /* A card says what it will actually do right now, so nothing looks ready
  * when it is not. A session belongs to one application, so "running" is
  * claimed only by the card whose application the host actually opened. */
 static const uint8_t card_app[2]={MIX_APP_TRANSLATE,MIX_APP_NOTES};
 const char *state_en=NULL,*state_zh=NULL;
 uint16_t state_colour=P.muted;
 if(index<2&&!view.linux_online){state_en="Linux offline";state_zh="Linux 离线";state_colour=P.warning;}
 else if(index<2&&view.terminal_open&&view.running_app==card_app[index]) {
  state_en="Session running";state_zh="会话运行中";state_colour=P.accent;
 }
 if(state_en)text(tx0,y+MIX_DP(96)+inset,MIX_SP(11),state_colour,tr(state_en,state_zh));
 /* Layout check against CARD_H = 288:
  *   container 20..140, title 140..190, subtitle 195..228, state 240..268.
  * The last line retains 20 px below it (14 px while pressed). */
 _Static_assert(MIX_DP(96) + MIX_SP(11) < CARD_H, "launcher card content overflows the card");
}
static void home_draw(void) {
 rect(0,0,W,CONTENT_H,P.bg);
 for(int i=0;i<4;i++)launcher_card(i,pressed_card==i);
}

/* ---------- settings: shared chrome ---------- */
#define RAIL_X 24
#define RAIL_W 240
#define BODY_X 280
#define BODY_W 712
#define SET_HEAD MIX_TYPE_TITLE_M
#define SET_BODY MIX_TYPE_BODY_L
#define SET_RAIL MIX_TYPE_BODY_M
#define SET_VALUE MIX_SP(20)
#define SET_SMALL MIX_SP(13)
#define RAIL_Y 56
#define RAIL_ITEM_H MIX_TOUCH_MIN
#define RAIL_STEP 128
#define RAIL_ICON_X (RAIL_X+MIX_DP(20))
#define RAIL_TEXT_X (RAIL_X+MIX_DP(40))
#define RAIL_TEXT_W (RAIL_W-MIX_DP(52))
#define THEME_COLS 2
#define THEME_Y 144
#define THEME_H 144
#define THEME_GAP 20
#define AP_LANGUAGE_Y 1208
#define AP_BRIGHT_Y 1464
#define AP_VOLUME_Y 1696
#define AP_GEOMETRY_Y 1952
#define AP_BACKLIGHT_Y 2144
#define NET_SCAN_Y 88
#define NET_LIST_Y 252
#define NET_ROW_H 136
#define NET_ROW_STEP 156
#define NET_PAGE_ROWS 4
#define NET_PAGER_Y 952
#define NET_SHOW_Y 480
#define NET_CONNECT_Y 736
#define NET_BACK_Y 876
#define DEVICE_TEST_Y 1264
#define DEVICE_TEST_BUTTON_Y 1288
#define DEVICE_TOUCH_Y 1488
#define DEVICE_RESET_Y 1712
#define SYSTEM_ACTION_Y 1064
static const int theme_chip_x0=BODY_X+20,theme_chip_w=326,theme_chip_gap=20;
/* Measured content extents, with a fixed 648-pixel viewport and no artificial
 * scrolling of short pages. Large diagnostic pages intentionally show fewer
 * readable rows rather than shrinking everything to fit one screen. */
static int settings_scroll_max(void) { return 0; }
static __attribute__((unused)) void section_rail(void) {
 static const char *en[SEC_COUNT]={"Theme","Display","Input","Network","Power","Device","System"};
 static const char *zh[SEC_COUNT]={"主题","屏幕","输入","网络","电源","设备","系统"};
 static const char *const glyphs[SEC_COUNT]={ICON_APPEARANCE,ICON_DEVICE,ICON_POWER,ICON_NETWORK,ICON_BATT_FULL,ICON_DEVICE,ICON_SYSTEM};
 for(int i=0;i<SEC_COUNT;i++) {
  int y=RAIL_Y+i*RAIL_STEP;
  bool on=section==(section_t)i;
  uint16_t fg=on?P.bg:P.text;
  roundbox(RAIL_X,y,RAIL_W,RAIL_ITEM_H,18,on?P.accent:P.card);
  /* Material list/navigation items use a leading icon and a trailing label on
   * one axis. Keeping the label beside the icon makes the enlarged rail read
   * as a spacious list instead of stacking two large elements vertically. */
  icon(RAIL_ICON_X,y+RAIL_ITEM_H/2,MIX_SP(18),fg,glyphs[i]);
  text_fit(RAIL_TEXT_X,y+MIX_DP(16),RAIL_TEXT_W,SET_RAIL,fg,tr(en[i],zh[i]));
 }
}
static void metric(int x,int y,int width,const char *en,const char *zh,bool valid,float value,const char *unit) {
 char b[64];roundbox(x,y,width,168,24,P.card);
 text_fit(x+24,y+20,width-48,SET_SMALL,P.muted,tr(en,zh));
 if(valid&&isfinite(value))snprintf(b,sizeof(b),"%.2f %s",(double)value,unit);
 else snprintf(b,sizeof(b),"-- %s",unit);
 text_fit(x+24,y+88,width-48,SET_VALUE,valid?P.text:P.muted,b);
}

/* ---------- settings: appearance ---------- */
static const char *const theme_names_en[MIX_THEME_COUNT]={
 "Graphite","Paper","Midnight","Ember","Ocean","Lavender",
 "Rose","Amber","Teal","Indigo","Coral","Lime"};
static const char *const theme_names_zh[MIX_THEME_COUNT]={
 "石墨薄荷","纸白森林","午夜蓝","暖琥珀","海洋蓝","薰衣草",
 "玫瑰红","琥珀金","深青绿","靛青","珊瑚橙","青柠绿"};
static void theme_chip(int index) {
 int col=index%THEME_COLS,row=index/THEME_COLS;
 int x=theme_chip_x0+col*(theme_chip_w+theme_chip_gap),y=THEME_Y+row*(THEME_H+THEME_GAP);
 bool active=theme==index;
 roundbox(x,y,theme_chip_w,THEME_H,16,active?P.accent:P.raised);
 /* Keep the swatch distinct even when it matches the selected accent fill. */
 roundbox(x+theme_chip_w/2-24,y+20,48,48,24,active?P.bg:P.card);
 roundbox(x+theme_chip_w/2-19,y+25,38,38,19,palettes[index].accent);
 text_mid_fit(x+theme_chip_w/2,y+88,theme_chip_w-24,SET_BODY,active?P.bg:P.text,
              tr(theme_names_en[index],theme_names_zh[index]));
}
static __attribute__((unused)) void appearance_draw(void) {
 char b[64];
 roundbox(BODY_X,64,BODY_W,1064,24,P.card);
 text(BODY_X+20,84,SET_HEAD,P.muted,tr("Theme","主题配色"));
 for(int i=0;i<MIX_THEME_COUNT;i++)theme_chip(i);
 text(BODY_X+20,1148,SET_HEAD,P.muted,tr("Language","界面语言"));
 button(BODY_X+20,AP_LANGUAGE_Y,326,"English","English",!language);
 button(BODY_X+366,AP_LANGUAGE_Y,326,"中文","中文",language);
 roundbox(BODY_X,1352,BODY_W,488,24,P.card);
 snprintf(b,sizeof(b),"%s %d%%",tr("Brightness","亮度"),view.brightness*10);
 text(BODY_X+20,1396,SET_BODY,P.text,b);
 button(BODY_X+20,AP_BRIGHT_Y,326,"Dimmer","调暗",false);
 button(BODY_X+366,AP_BRIGHT_Y,326,"Brighter","调亮",false);
 if(volume_percent>=0)snprintf(b,sizeof(b),"%s %d%%",tr("Volume","音量"),volume_percent);
 else snprintf(b,sizeof(b),"%s",tr("Volume","音量"));
 text(BODY_X+20,1628,SET_BODY,P.text,b);
 button(BODY_X+20,AP_VOLUME_Y,326,"Quieter","调小",false);
 button(BODY_X+366,AP_VOLUME_Y,326,"Louder","调大",false);
 roundbox(BODY_X,1864,BODY_W,256,24,P.card);
 text(BODY_X+20,1888,SET_HEAD,P.muted,tr("Terminal text","终端文字大小"));
 for(int i=0;i<GEOMETRY_COUNT;i++) {
  snprintf(b,sizeof(b),"%dx%d",geometries[i].cols,geometries[i].rows);
  int x=BODY_X+20+i*346;
  roundbox(x,AP_GEOMETRY_Y,326,144,20,geometry==i?P.accent:P.raised);
  text_mid(x+163,AP_GEOMETRY_Y+20,SET_BODY,geometry==i?P.bg:P.text,b);
  text_mid_fit(x+163,AP_GEOMETRY_Y+84,302,SET_SMALL,geometry==i?P.bg:P.muted,
           tr(i?"Large (default)":"Compact",i?"大字（默认）":"紧凑"));
 }
 button(BODY_X+20,AP_BACKLIGHT_Y,BODY_W-40,"Keyboard backlight","键盘背光",false);
}

/* ---------- settings: network ---------- */
static void network_list_draw(void) {
 char b[96];
 roundbox(BODY_X,64,BODY_W,160,24,P.card);
 const char *state=!view.linux_online?tr("Linux offline","Linux 离线"):
  !view.wifi_reported?tr("Wi-Fi unknown","无线状态未知"):
  view.wifi_connected?view.wifi_ssid:tr("Not connected","未连接");
 text_fit(BODY_X+20,92,440,SET_BODY,view.wifi_connected?P.accent:P.text,state);
 if(view.wifi_connected&&view.host_ip[0])text_fit(BODY_X+20,160,440,SET_SMALL,P.muted,view.host_ip);
 bool busy=mix_link_net_busy();
 button(BODY_X+492,NET_SCAN_Y,200,busy?"Scanning":"Scan",busy?"扫描中":"扫描",busy);
 const mix_net_entry_t *list=NULL;
 int count=mix_link_net_list(&list),rows=count-net_scroll;
 if(rows>NET_PAGE_ROWS)rows=NET_PAGE_ROWS;
 if(!count)text_fit(BODY_X+20,NET_LIST_Y+40,BODY_W-40,SET_BODY,P.muted,
       busy?tr("Scanning...","正在扫描…"):tr("No networks found","尚无扫描结果"));
 for(int i=0;i<rows;i++) {
  const mix_net_entry_t *e=&list[net_scroll+i];int y=NET_LIST_Y+i*NET_ROW_STEP;
  bool current=view.wifi_connected&&!strcmp(e->ssid,view.wifi_ssid);
  roundbox(BODY_X,y,BODY_W,NET_ROW_H,20,current?P.raised:P.card);
  text_fit(BODY_X+20,y+20,BODY_W-100,SET_BODY,P.text,e->ssid);
  int bars=1+e->signal*3/100;
  for(int k=0;k<4;k++)rect(BODY_X+BODY_W-64+k*10,y+56-(10+k*6),7,10+k*6,k<bars?P.accent:P.raised);
  snprintf(b,sizeof(b),"%s%s%s",e->secured?tr("Secured","加密"):tr("Open","开放"),
           e->known?" / ":"",e->known?tr("Saved","已保存"):"");
  text(BODY_X+20,y+82,SET_SMALL,P.muted,b);
 }
 const char *message=mix_link_net_message();
 if(message&&message[0])text_fit(BODY_X+20,880,BODY_W-40,SET_SMALL,P.muted,message);
 if(count>NET_PAGE_ROWS) {
  button(BODY_X,NET_PAGER_Y,336,"Previous","上一页",false);
  button(BODY_X+376,NET_PAGER_Y,336,"Next","下一页",false);
 }
}
static void network_connect_draw(void) {
 char b[96];
 roundbox(BODY_X,64,BODY_W,176,24,P.card);
 text(BODY_X+20,88,SET_SMALL,P.muted,tr("Network","网络"));
 text_fit(BODY_X+20,152,BODY_W-40,SET_VALUE,P.text,net_ssid);
 roundbox(BODY_X,264,BODY_W,356,24,P.card);
 text(BODY_X+20,288,SET_HEAD,P.muted,tr("Passphrase","密码"));
 roundbox(BODY_X+20,352,BODY_W-40,104,20,P.raised);
 if(net_pass_len) {
  if(net_reveal) {
   const char *visible=net_pass;
   while(*visible&&text_width(SET_BODY,visible)>BODY_W-80)visible++;
   text(BODY_X+40,384,SET_BODY,P.text,visible);
  } else {
   static const char bullet[3]={'\xe2','\x80','\xa2'};
   char dots[3*sizeof(net_pass)+1];size_t at=0;
   for(int i=0;i<net_pass_len&&at+3<sizeof(dots);i++){memcpy(dots+at,bullet,3);at+=3;}
   dots[at]=0;text_fit(BODY_X+40,384,BODY_W-80,SET_BODY,P.text,dots);
  }
 } else text(BODY_X+40,384,SET_SMALL,P.muted,tr("Type on keyboard","用键盘输入"));
 button(BODY_X+BODY_W-220,NET_SHOW_Y,200,net_reveal?"Hide":"Show",net_reveal?"隐藏":"显示",net_reveal);
 snprintf(b,sizeof(b),"%d / 63",net_pass_len);text(BODY_X+20,520,SET_SMALL,P.muted,b);
 text_fit(BODY_X+20,648,BODY_W-40,SET_SMALL,P.muted,tr("Enter: connect / Escape: back","回车连接 / Escape 返回"));
 bool busy=mix_link_net_busy();
 button(BODY_X,NET_CONNECT_Y,336,busy?"Working...":"Connect",busy?"处理中…":"连接",!busy);
 button(BODY_X+376,NET_CONNECT_Y,336,"Forget","忘记网络",false);
 button(BODY_X,NET_BACK_Y,336,"Back","返回",false);
 const char *message=mix_link_net_message();
 if(message&&message[0])text_fit(BODY_X+356,NET_BACK_Y+40,BODY_W-356,SET_SMALL,P.warning,message);
}
static __attribute__((unused)) void network_draw(void){if(net_view)network_connect_draw();else network_list_draw();}

/* ---------- settings: power ---------- */
static void graph_draw(void) {
 if(392+draw_offset_y>=draw_limit_y||696+draw_offset_y<=draw_clip_y0)return;
 roundbox(BODY_X,392,BODY_W,304,24,P.card);
 text_fit(BODY_X+20,416,BODY_W-40,SET_HEAD,P.muted,tr("History: last hour","最近一小时"));
 int n=batt_log_get(history,BATT_LOG_CAP);
 for(int k=0;k<4;k++)rect(BODY_X+24,482+k*40,BODY_W-48,1,P.raised);
 if(n<2) {
  text(BODY_X+40,510,SET_BODY,P.muted,tr("Waiting for real samples","等待真实采样"));
  return;
 }
 int span=BODY_W-48;
 for(int i=1;i<n;i++) {
  int x0=BODY_X+24+(BATT_LOG_CAP-n+i-1)*span/(BATT_LOG_CAP-1);
  int x1=BODY_X+24+(BATT_LOG_CAP-n+i)*span/(BATT_LOG_CAP-1);
  batt_sample_t a=history[i-1],b=history[i];
  if(a.mv&&b.mv) {
   line(x0,602-clamp((a.mv-3000)*120/1400,0,120),x1,602-clamp((b.mv-3000)*120/1400,0,120),P.accent);
   line(x0,602-clamp((a.ma+2000)*120/4000,0,120),x1,602-clamp((b.ma+2000)*120/4000,0,120),P.warning);
  }
  if(a.soc>=0&&b.soc>=0)line(x0,602-clamp(a.soc*120/100,0,120),x1,602-clamp(b.soc*120/100,0,120),P.text);
 }
 text_fit(BODY_X+24,632,BODY_W-48,SET_SMALL,P.muted,"-60m   V 3.0-4.4 / A -2..2 / SOC 0-100%");
}
static __attribute__((unused)) void power_draw(void) {
 char b[96];
 metric(BODY_X,64,344,"Battery voltage","电池电压",view.battery_valid,view.battery_v,"V");
 metric(BODY_X+368,64,344,"USB voltage","USB 电压",view.usb_valid,view.usb_v,"V");
 metric(BODY_X,256,344,"Battery current","电池电流",view.battery_valid,view.battery_a,"A");
 metric(BODY_X+368,256,344,"USB current","USB 电流",view.usb_valid,view.usb_a,"A");
 metric(BODY_X,448,344,"Battery power","电池功率",view.battery_valid,view.battery_v*view.battery_a,"W");
 metric(BODY_X+368,448,344,"USB power","USB 功率",view.usb_valid,view.usb_v*view.usb_a,"W");
 roundbox(BODY_X,640,BODY_W,200,24,P.card);
 if(view.calibration_verified&&view.capacity_mah>0&&isfinite(view.capacity_mah))
  snprintf(b,sizeof(b),"%s %.0f mAh",tr("Capacity","容量"),(double)view.capacity_mah);
 else snprintf(b,sizeof(b),"%s",tr("Capacity: not calibrated","容量：未校准"));
 text_fit(BODY_X+24,676,BODY_W-48,SET_BODY,P.warning,b);
 if(view.calibration_verified&&view.battery_valid&&view.runtime_hours>0&&isfinite(view.runtime_hours))
  snprintf(b,sizeof(b),"%s %.1f h",tr("Runtime","续航估算"),(double)view.runtime_hours);
 else snprintf(b,sizeof(b),"%s",tr("Runtime: unavailable","续航：暂无估算"));
 text_fit(BODY_X+24,756,BODY_W-48,SET_BODY,P.warning,b);
 int saved=draw_offset_y;draw_offset_y+=472;graph_draw();draw_offset_y=saved;
}

/* ---------- settings: device ---------- */
static void touch_area(void) {
 roundbox(BODY_X,DEVICE_TEST_Y,BODY_W,424,24,P.card);
 text(BODY_X+24,DEVICE_TEST_Y+48,SET_HEAD,P.muted,tr("Touch test","触摸测试"));
 button(BODY_X+BODY_W-224,DEVICE_TEST_BUTTON_Y,200,touch_test?"Stop":"Start",touch_test?"结束":"开始",touch_test);
 if(!touch_test) {
  text_fit(BODY_X+24,DEVICE_TOUCH_Y+40,BODY_W-48,SET_BODY,P.muted,tr("Start to test touch","点击开始，测试触摸"));
  return;
 }
 char b[72];snprintf(b,sizeof(b),"X %d   Y %d   %s",tx,ty,touch_down?"DOWN":"UP");
 text_fit(BODY_X+24,1428,BODY_W-48,SET_BODY,P.accent,b);
 if(tx>=BODY_X+24&&tx<BODY_X+BODY_W-24&&ty>=DEVICE_TOUCH_Y&&ty<1680) {
  line(tx-14,ty,tx+14,ty,P.accent);line(tx,ty-14,tx,ty+14,P.accent);
 }
}
static __attribute__((unused)) void device_draw(void) {
 char b[64];
 roundbox(BODY_X,64,BODY_W,648,24,P.card);
 text(BODY_X+24,88,SET_HEAD,P.muted,tr("Sensor status","传感器状态"));
 for(int i=0;i<16;i++) {
  bool checked=(view.sensors_checked&(1u<<i))!=0,present=(view.sensors_present&(1u<<i))!=0;
  snprintf(b,sizeof(b),"%02d  %s",i,tr(checked?(present?"Present":"Absent"):"Unknown",checked?(present?"在位":"未连接"):"未知"));
  text_fit(BODY_X+24+(i%2)*344,160+(i/2)*68,300,SET_BODY,checked?(present?P.text:P.muted):P.muted,b);
 }
 roundbox(BODY_X,736,BODY_W,224,24,P.card);
 text_fit(BODY_X+24,764,BODY_W-48,SET_BODY,P.text,
      tr(view.headphone_valid?(view.headphone_inserted?"Headphones connected":"Headphones disconnected"):"Headphones unknown",
         view.headphone_valid?(view.headphone_inserted?"耳机已连接":"耳机未连接"):"耳机状态未知"));
 for(int i=0;i<2;i++) {
  float f=i?view.mic_r:view.mic_l;
  text(BODY_X+24,832+i*60,SET_SMALL,P.muted,i?"R":"L");
  rect(BODY_X+80,844+i*60,BODY_W-112,16,P.raised);
  if(view.audio_ready&&isfinite(f))rect(BODY_X+80,844+i*60,ratio_width(f,BODY_W-112),16,P.accent);
 }
 roundbox(BODY_X,984,BODY_W,256,24,P.card);
 text(BODY_X+24,1012,SET_BODY,P.text,tr(view.keyboard_online?"Keyboard connected":"Keyboard offline",view.keyboard_online?"键盘已连接":"键盘离线"));
 snprintf(b,sizeof(b),"%s %lu",tr("Resyncs","重新同步"),(unsigned long)view.keyboard_overflows);
 text_fit(BODY_X+24,1088,BODY_W-48,SET_SMALL,P.muted,b);
 snprintf(b,sizeof(b),"PSRAM %.2f MiB",view.free_psram/1048576.0);
 text_fit(BODY_X+24,1160,BODY_W-48,SET_SMALL,P.muted,b);
 button(BODY_X,DEVICE_RESET_Y,BODY_W,"Reset local input","重置本地输入",false);
 touch_area();
}

/* ---------- settings: system ---------- */
static __attribute__((unused)) void system_draw(void) {
 char b[96];
 roundbox(BODY_X,64,BODY_W,440,24,P.card);
 text(BODY_X+24,88,SET_VALUE,P.text,"MixOS " MIX_VERSION);
 text(BODY_X+24,176,SET_BODY,P.muted,tr("ESP32-S3 / USB","ESP32-S3 / USB"));
 if(view.linux_online) {
  if(isfinite(view.linux_cpu))snprintf(b,sizeof(b),"Linux CPU %.0f%%",(double)view.linux_cpu);
  else snprintf(b,sizeof(b),"Linux CPU --");
  text(BODY_X+24,256,SET_BODY,P.text,b);
  snprintf(b,sizeof(b),"RAM %u / %u MiB",(unsigned)(view.linux_mem_used_kib/1024),(unsigned)(view.linux_mem_total_kib/1024));
  text(BODY_X+24,336,SET_BODY,P.text,b);
  snprintf(b,sizeof(b),"IP %s",view.host_ip[0]?view.host_ip:"--");
  text_fit(BODY_X+24,416,BODY_W-48,SET_SMALL,P.muted,b);
 } else text(BODY_X+24,256,SET_BODY,P.muted,tr("Linux offline","Linux 离线"));
 roundbox(BODY_X,528,BODY_W,360,24,P.card);
 text(BODY_X+24,552,SET_HEAD,P.muted,tr("Firmware update","固件更新"));
 if(view.ota_state==1) {
  snprintf(b,sizeof(b),"%s %d%%",tr("Receiving","正在接收"),clamp(view.ota_percent,0,100));
  text(BODY_X+24,632,SET_BODY,P.accent,b);
  roundbox(BODY_X+24,716,BODY_W-48,16,8,P.raised);
  roundbox(BODY_X+24,716,clamp((BODY_W-48)*clamp(view.ota_percent,0,100)/100,16,BODY_W-48),16,8,P.accent);
 } else {
  text_fit(BODY_X+24,632,BODY_W-48,SET_BODY,view.firmware_on_trial?P.warning:P.text,
    tr(view.ota_state==2?"Verified; restarting":view.firmware_on_trial?"New build on trial":"A/B update over USB",
       view.ota_state==2?"已校验，正在重启":view.firmware_on_trial?"新固件试运行中":"通过 USB 进行 A/B 升级"));
  text_fit(BODY_X+24,712,BODY_W-48,SET_SMALL,P.muted,tr("Host tools manage installation","由主机更新工具执行安装"));
 }
 text_fit(BODY_X+24,804,BODY_W-48,SET_SMALL,P.muted,tr("Local approval for maintenance","维护操作需在本机确认"));
 roundbox(BODY_X,912,BODY_W,312,24,P.card);
 snprintf(b,sizeof(b),"%s %d%%",tr(view.job_running?"Task running":"Maintenance",view.job_running?"任务运行中":"维护操作"),clamp(view.job_percent,0,100));
 text_fit(BODY_X+24,944,BODY_W-48,SET_HEAD,P.muted,b);
 button(BODY_X+20,SYSTEM_ACTION_Y,326,view.job_running?"Cancel task":"Run task",view.job_running?"取消任务":"运行维护",false);
 button(BODY_X+366,SYSTEM_ACTION_Y,326,"Terminal","命令行",false);
}
/* ---------- fixed settings pages ---------- */
#define SET_PAGE_X 32
#define SET_PAGE_W 960
#define SET_TITLE_Y 24
#define SET_TILE_X0 32
#define SET_TILE_W 464
#define SET_TILE_H 112
#define SET_TILE_GAP_X 32
#define SET_TILE_Y0 82
#define SET_TILE_GAP_Y 24
#define SET_ACTION_Y 480
#define SET_ACTION_W 300
#define SET_ACTION_GAP 24
#define SET_THEME_COLS 3
#define SET_THEME_W 296
#define SET_THEME_H 112
#define SET_THEME_GAP_X 20
#define SET_THEME_GAP_Y 14
#define SET_THEME_X0 32
#define SET_THEME_Y0 76
#define SET_NETWORK_ROWS 3
static int network_page_limit(int count) {
 return count>0?((count-1)/SET_NETWORK_ROWS)*SET_NETWORK_ROWS:0;
}
static void settings_title(const char *en,const char *zh) {
 text(SET_PAGE_X,SET_TITLE_Y,SET_HEAD,P.text,tr(en,zh));
}
static void settings_menu_tile(int index,const char *en,const char *zh,const char *glyph) {
 int col=index%2,row=index/2;
 int x=SET_TILE_X0+col*(SET_TILE_W+SET_TILE_GAP_X);
 int y=SET_TILE_Y0+row*(SET_TILE_H+SET_TILE_GAP_Y);
 roundbox(x,y,SET_TILE_W,SET_TILE_H,20,P.card);
 icon(x+48,y+SET_TILE_H/2,MIX_SP(18),P.accent,glyph);
 text_fit(x+92,y+34,SET_TILE_W-112,SET_BODY,P.text,tr(en,zh));
}
static void settings_menu_draw(void) {
 static const char *en[8]={"Theme","Display","Sound & keyboard","Network","Power","Device","System","About"};
 static const char *zh[8]={"主题","屏幕","声音与键盘","网络","电源","设备","系统","关于"};
 static const char *glyphs[8]={ICON_APPEARANCE,ICON_DEVICE,ICON_POWER,ICON_NETWORK,
                               ICON_BATT_FULL,ICON_DEVICE,ICON_SYSTEM,ICON_SYSTEM};
 settings_title("Settings","设置");
 for(int i=0;i<8;i++)settings_menu_tile(i,en[i],zh[i],glyphs[i]);
}
static void fixed_theme_draw(void) {
 settings_title("Theme","主题");
 static const char *en[MIX_THEME_COUNT]={"Graphite","Paper","Midnight","Ember","Ocean","Lavender","Rose","Amber","Teal","Indigo","Coral","Lime"};
 static const char *zh[MIX_THEME_COUNT]={"石墨薄荷","纸白森林","午夜蓝","暖琥珀","海洋蓝","薰衣草","玫瑰红","琥珀金","深青绿","靛青","珊瑚橙","青柠绿"};
 for(int i=0;i<MIX_THEME_COUNT;i++) {
  int col=i%SET_THEME_COLS,row=i/SET_THEME_COLS;
  int x=SET_THEME_X0+col*(SET_THEME_W+SET_THEME_GAP_X);
  int y=SET_THEME_Y0+row*(SET_THEME_H+SET_THEME_GAP_Y);
  bool active=theme==i;
  roundbox(x,y,SET_THEME_W,SET_THEME_H,16,active?P.accent:P.card);
  roundbox(x+20,y+24,64,64,32,active?P.bg:P.raised);
  roundbox(x+32,y+36,40,40,20,palettes[i].accent);
  text_fit(x+108,y+36,SET_THEME_W-124,SET_BODY,active?P.bg:P.text,tr(en[i],zh[i]));
 }
}
static void fixed_display_draw(void) {
 char b[64];settings_title("Display","屏幕");
 roundbox(SET_PAGE_X,76,SET_PAGE_W,224,24,P.card);
 snprintf(b,sizeof(b),"%s  %d%%",tr("Brightness","亮度"),view.brightness*10);
 text(64,106,SET_BODY,P.text,b);
 button(64,174,300,"Dimmer","调暗",false);button(388,174,300,"Brighter","调亮",false);
 roundbox(SET_PAGE_X,310,SET_PAGE_W,200,24,P.card);
 text(64,322,SET_BODY,P.text,tr("Interface language","界面语言"));
 button(64,382,300,"English","English",!language);
 button(388,382,300,"中文","中文",language);
}
static void fixed_input_draw(void) {
 char b[64];settings_title("Sound & keyboard","声音与键盘");
 roundbox(SET_PAGE_X,76,SET_PAGE_W,216,24,P.card);
 if(volume_percent>=0)snprintf(b,sizeof(b),"%s  %d%%",tr("Volume","音量"),volume_percent);
 else snprintf(b,sizeof(b),"%s  --",tr("Volume","音量"));
 text(64,106,SET_BODY,P.text,b);
 button(64,164,300,"Quieter","调小",false);button(388,164,300,"Louder","调大",false);
 text(64,308,SET_BODY,P.text,tr("Keyboard backlight","键盘背光"));
 button(64,368,624,"Adjust","调节",false);
 text(724,408,SET_SMALL,P.muted,tr("Terminal size","终端字号"));
 button(64,510,300,"Compact 80x27","紧凑 80x27",geometry==0);
 button(388,510,300,"Large 64x20","大字 64x20",geometry==1);
}
static void fixed_network_draw(void) {
 const char *state=!view.linux_online?tr("Linux offline","Linux 离线"):
  !view.wifi_reported?tr("Wi-Fi unknown","无线状态未知"):
  view.wifi_connected?view.wifi_ssid:tr("Not connected","未连接");
 settings_title("Network","网络");
 roundbox(SET_PAGE_X,76,SET_PAGE_W,132,24,P.card);
 text_fit(64,106,620,SET_BODY,view.wifi_connected?P.accent:P.text,state);
 text_fit(64,162,620,SET_SMALL,P.muted,view.host_ip[0]?view.host_ip:"IP --");
 button(716,88,240,"Scan","扫描",mix_link_net_busy());
 const mix_net_entry_t *list=NULL;int count=mix_link_net_list(&list);
 net_scroll=clamp(net_scroll,0,network_page_limit(count));
 int rows=count-net_scroll;if(rows>SET_NETWORK_ROWS)rows=SET_NETWORK_ROWS;if(rows<0)rows=0;
 for(int i=0;i<rows;i++) {
  int y=212+i*84;bool current=view.wifi_connected&&!strcmp(list[net_scroll+i].ssid,view.wifi_ssid);
  roundbox(SET_PAGE_X,y,SET_PAGE_W,68,16,current?P.raised:P.card);
  text_fit(64,y+16,600,SET_BODY,P.text,list[net_scroll+i].ssid);
  text(760,y+20,SET_SMALL,P.muted,list[net_scroll+i].secured?tr("Secured","加密"):tr("Open","开放"));
 }
 if(!count)text(64,238,SET_BODY,P.muted,tr("No networks found","尚无扫描结果"));
 const char *message=mix_link_net_message();
 if(message&&message[0])text_fit(64,450,880,SET_SMALL,P.muted,message);
 button(64,486,300,"Previous","上一页",false);button(388,486,300,"Next","下一页",false);
}
static void fixed_network_connect_draw(void) {
 settings_title("Connect","连接网络");
 roundbox(SET_PAGE_X,76,SET_PAGE_W,148,24,P.card);
 text(64,106,SET_SMALL,P.muted,tr("Network","网络"));text_fit(64,148,880,SET_VALUE,P.text,net_ssid);
 roundbox(SET_PAGE_X,248,SET_PAGE_W,180,24,P.card);
 text(64,278,SET_HEAD,P.muted,tr("Passphrase","密码"));
 if(net_pass_len&&net_reveal) {
  const char *visible=net_pass;
  while(*visible&&text_width(SET_BODY,visible)>704)visible++;
  text(64,338,SET_BODY,P.text,visible);
 } else text_fit(64,338,704,SET_BODY,P.text,net_pass_len?"••••••":tr("Type on keyboard","用键盘输入"));
 button(800,300,160,net_reveal?"Hide":"Show",net_reveal?"隐藏":"显示",net_reveal);
 button(64,434,300,"Connect","连接",false);button(388,434,300,"Forget","忘记网络",false);
 const char *message=mix_link_net_message();
 if(message&&message[0])text_fit(64,574,880,SET_SMALL,P.warning,message);
}
static void fixed_power_draw(void) {
 settings_title("Power","电源");
 metric(SET_PAGE_X,76,300,"Battery voltage","电池电压",view.battery_valid,view.battery_v,"V");
 metric(356,76,300,"USB voltage","USB 电压",view.usb_valid,view.usb_v,"V");
 metric(680,76,312,"Battery current","电池电流",view.battery_valid,view.battery_a,"A");
 metric(SET_PAGE_X,268,300,"USB current","USB 电流",view.usb_valid,view.usb_a,"A");
 metric(356,268,300,"Battery power","电池功率",view.battery_valid,view.battery_v*view.battery_a,"W");
 metric(680,268,312,"USB power","USB 功率",view.usb_valid,view.usb_v*view.usb_a,"W");
 roundbox(SET_PAGE_X,460,SET_PAGE_W,150,24,P.card);
 text_fit(64,492,880,SET_BODY,P.warning,view.calibration_verified?tr("Battery capacity calibrated","电池容量已校准"):tr("Battery capacity not calibrated","电池容量未校准"));
}
static void fixed_device_draw(void) {
 char b[96];settings_title("Device","设备");
 roundbox(SET_PAGE_X,76,SET_PAGE_W,180,24,P.card);
 text(64,106,SET_BODY,P.text,tr(view.keyboard_online?"Keyboard connected":"Keyboard offline",view.keyboard_online?"键盘已连接":"键盘离线"));
 snprintf(b,sizeof(b),"PSRAM %.2f MiB",view.free_psram/1048576.0);text(64,164,SET_SMALL,P.muted,b);
 roundbox(SET_PAGE_X,280,SET_PAGE_W,180,24,P.card);
 text(64,310,SET_BODY,P.text,tr(view.headphone_valid?(view.headphone_inserted?"Headphones connected":"Headphones disconnected"):"Headphones unknown",view.headphone_valid?(view.headphone_inserted?"耳机已连接":"耳机未连接"):"耳机状态未知"));
 snprintf(b,sizeof(b),"%s %02X / %02X",tr("Sensors found/checked","传感器发现/检测"),
          (unsigned)view.sensors_present,(unsigned)view.sensors_checked);
 text_fit(64,368,880,SET_SMALL,P.muted,b);
 button(64,486,300,touch_test?"Stop":"Touch test",touch_test?"结束":"触摸测试",touch_test);
 button(388,486,300,"Reset input","重置输入",false);
 if(touch_test) {
  snprintf(b,sizeof(b),"X %d   Y %d   %s",tx,ty,touch_down?"DOWN":"UP");
  text_fit(64,420,640,SET_SMALL,P.accent,b);
 }
}
static void fixed_system_draw(void) {
 char b[96];settings_title("System","系统");
 roundbox(SET_PAGE_X,76,SET_PAGE_W,190,24,P.card);
 text(64,106,SET_VALUE,P.text,"MixOS " MIX_VERSION);
 text(64,172,SET_BODY,P.muted,tr("ESP32-S3 / USB","ESP32-S3 / USB"));
 roundbox(SET_PAGE_X,292,SET_PAGE_W,180,24,P.card);
 if(view.linux_online) {
  if(isfinite(view.linux_cpu))snprintf(b,sizeof(b),"Linux CPU %.0f%%",(double)view.linux_cpu);
  else snprintf(b,sizeof(b),"Linux CPU --");
  text(64,322,SET_BODY,P.text,b);text_fit(64,382,880,SET_SMALL,P.muted,view.host_ip[0]?view.host_ip:"IP --");
 } else text(64,342,SET_BODY,P.muted,tr("Linux offline","Linux 离线"));
 if(view.ota_state)snprintf(b,sizeof(b),"OTA %d%%",clamp(view.ota_percent,0,100));
 else snprintf(b,sizeof(b),"%s %d%%",tr(view.job_running?"Task running":"Maintenance",view.job_running?"任务运行中":"维护操作"),clamp(view.job_percent,0,100));
 text_fit(64,426,880,SET_SMALL,P.muted,b);
 button(64,500,300,view.job_running?"Cancel task":"Run task",view.job_running?"取消任务":"运行维护",false);
 button(388,500,300,"Terminal","命令行",false);
}
static void fixed_about_draw(void) {
 char b[96];settings_title("About","关于");
 roundbox(SET_PAGE_X,76,SET_PAGE_W,190,24,P.card);
 text(64,106,SET_VALUE,P.text,"MixOS " MIX_VERSION);
 text(64,180,SET_BODY,P.muted,"ESP32-S3 / RGB565");
 const esp_app_desc_t *built=esp_app_get_description();
 if(built) {
  snprintf(b,sizeof(b),"%s %s",built->date,built->time);
  text(64,302,SET_BODY,P.text,b);
  snprintf(b,sizeof(b),"ELF %02x%02x%02x%02x",built->app_elf_sha256[0],built->app_elf_sha256[1],
           built->app_elf_sha256[2],built->app_elf_sha256[3]);
  text(64,378,SET_BODY,P.muted,b);
 }
 text(64,474,SET_BODY,P.text,tr(view.firmware_on_trial?"Trial firmware":"Validated firmware",
                             view.firmware_on_trial?"固件试运行中":"已验证固件"));
}
static void fixed_settings_content_draw(void) {
 if(settings_menu){settings_menu_draw();return;}
 switch(section) {
 case SEC_APPEARANCE:fixed_theme_draw();break;
 case SEC_DISPLAY:fixed_display_draw();break;
 case SEC_INPUT:fixed_input_draw();break;
 case SEC_NETWORK:
  if(net_view)fixed_network_connect_draw();else fixed_network_draw();
  network_content_record();break;
 case SEC_POWER:fixed_power_draw();break;
 case SEC_DEVICE:fixed_device_draw();break;
 case SEC_ABOUT:fixed_about_draw();break;
 default:fixed_system_draw();break;
 }
}
static void settings_body_content_draw(void) {
 rect(0,0,W,CONTENT_H,P.bg);
 draw_offset_y=0;draw_limit_y=CONTENT_H;draw_clip_y0=0;
 settings_scroll=0;
 fixed_settings_content_draw();
}
static void settings_scrollbar_draw(void) {
 int saved_offset=draw_offset_y,saved_limit=draw_limit_y,saved_clip=draw_clip_y0;
 draw_offset_y=0;draw_limit_y=CONTENT_H;draw_clip_y0=0;
 rect(W-8,0,8,CONTENT_H,P.bg);
 int track=CONTENT_H-40,range=settings_scroll_max();
 if(range>0) {
  int thumb=track*CONTENT_H/(CONTENT_H+range);
  rect(W-7,20,3,track,P.raised);
  rect(W-7,20+settings_scroll*(track-thumb)/range,3,thumb,P.accent);
 }
 draw_offset_y=saved_offset;draw_limit_y=saved_limit;draw_clip_y0=saved_clip;
}
static void settings_notice_draw(void) {
 if(!notice_visible)return;
 roundbox(SET_PAGE_X,0,SET_PAGE_W,NOTICE_H,20,P.raised);
 text_mid_fit(W/2,32,SET_PAGE_W-48,SET_SMALL,P.warning,notice);
}
static void settings_body_draw(void) {
 draw_clip_y0=0;draw_limit_y=CONTENT_H;
 settings_body_content_draw();
 settings_scrollbar_draw();
 settings_notice_draw();
 settings_painted_scroll=settings_scroll;
 settings_body_dirty=false;settings_scroll_dirty=false;settings_frame_ms=ui_ms;
}
/* Reuse the already-rendered body for a drag. Only the newly exposed strip is
 * rasterized; the fixed rail and footer never participate in this present. */
static __attribute__((unused)) void settings_body_scroll_draw(void) {
 int old=settings_painted_scroll,new_scroll=clamp(settings_scroll,0,settings_scroll_max());
 int delta=new_scroll-old;
 if(!delta||abs(delta)>=CONTENT_H){settings_body_draw();return;}
 size_t row_bytes=(size_t)BODY_W*sizeof(uint16_t);
 /* Each row is shorter than the W-pixel stride and |delta| >= 1, so each
  * source/destination pair is DISJOINT. Preserve the across-row order for
  * overlap, but use the aligned ROM memcpy, not ROM memmove's byte loop.
  * The row origins/lengths preserve the allocator's word alignment. */
 _Static_assert(BODY_W<=W&&(BODY_X*2)%4==0&&(BODY_W*2)%4==0&&(W*2)%4==0,
                "scroll row copy must stay aligned and non-overlapping");
 if(delta>0) {
  for(int y=0;y<CONTENT_H-delta;y++)
   memcpy(fb+(size_t)y*W+BODY_X,fb+(size_t)(y+delta)*W+BODY_X,row_bytes);
 } else {
  int move=-delta;
  for(int y=CONTENT_H-1;y>=move;y--)
   memcpy(fb+(size_t)y*W+BODY_X,fb+(size_t)(y-move)*W+BODY_X,row_bytes);
 }
 int clip0=delta>0?CONTENT_H-delta:0,clip1=delta>0?CONTENT_H:-delta;
 settings_scroll=new_scroll;draw_clip_y0=clip0;draw_limit_y=clip1;
 settings_body_content_draw();
 draw_clip_y0=0;draw_limit_y=CONTENT_H;
 settings_scrollbar_draw();
 settings_painted_scroll=new_scroll;settings_body_dirty=false;settings_scroll_dirty=false;
 /* Moving the existing pixels changes the WHOLE body. Its single present is
  * owned by the caller, never just the exposed strip or a second scrollbar
  * present (each present waits for a hardware frame). */
}
static void settings_draw(void) {
 rect(0,0,W,CONTENT_H,P.bg);
 settings_body_draw();settings_footer_dirty=false;
}
static bool settings_metrics_changed(const mix_view_t *a,const mix_view_t *b) {
 if(settings_menu)return false;
 switch(section) {
 case SEC_ABOUT:return a->firmware_on_trial!=b->firmware_on_trial;
 case SEC_APPEARANCE:return false;
 case SEC_DISPLAY:return a->brightness!=b->brightness;
 case SEC_INPUT:return false;
 case SEC_NETWORK:return a->linux_online!=b->linux_online||a->wifi_reported!=b->wifi_reported||
  a->wifi_connected!=b->wifi_connected||strcmp(a->wifi_ssid,b->wifi_ssid)||strcmp(a->host_ip,b->host_ip);
 case SEC_POWER:return true; /* sensor/history refresh, at most once a second */
 case SEC_DEVICE:return a->sensors_checked!=b->sensors_checked||a->sensors_present!=b->sensors_present||
  a->headphone_valid!=b->headphone_valid||a->headphone_inserted!=b->headphone_inserted||
  a->keyboard_online!=b->keyboard_online||a->keyboard_overflows!=b->keyboard_overflows||
  a->free_psram!=b->free_psram||a->audio_ready!=b->audio_ready||a->mic_l!=b->mic_l||a->mic_r!=b->mic_r;
 default:return a->linux_online!=b->linux_online||a->linux_cpu!=b->linux_cpu||
  a->linux_mem_used_kib!=b->linux_mem_used_kib||a->linux_mem_total_kib!=b->linux_mem_total_kib||
  a->job_running!=b->job_running||a->job_percent!=b->job_percent||a->firmware_on_trial!=b->firmware_on_trial||
  strcmp(a->host_ip,b->host_ip);
 }
}

/* ---------- application view ---------- */
static uint16_t ansi(mix_color_t c,bool foreground) {
 static const uint16_t basic[]={0x0000,0xc986,0x4d4c,0xe5e9,0x541c,0xb2b8,0x4df9,0xce79,0x73ae,0xf9ac,0x87b3,0xff50,0x8d7f,0xed1f,0x87ff,0xffff};
 if(MIX_COLOR_IS_RGB(c))return RGB(MIX_COLOR_R(c),MIX_COLOR_G(c),MIX_COLOR_B(c));
 if(!MIX_COLOR_IS_INDEX(c))return foreground?P.text:P.bg;
 if(c<16)return basic[c];
 if(c<232){int n=(int)c-16,r=n/36,g=n/6%6,b=n%6;return RGB(r?55+r*40:0,g?55+g*40:0,b?55+b*40:0);}
 if(c<255){int n=8+((int)c-232)*10;return RGB(n,n,n);}
 return foreground?P.text:P.bg;
}
static void cell_glyph(int x,int y,int w,int h,int size,uint32_t cp,uint16_t fg,bool bold) {
 if(cp==0||cp==' ')return;
 if(!ttf_font_ready()){fallback_char(x+1,y+(h-14)/2,2,cp,fg);return;}
 ttf_draw_cell(fb,W,H,x,y,w,h,size,fg,cp,bold);
}
static void terminal_row_draw(int row) {
 const geometry_t *g=geom();
 int ox=term_ox(),y=term_oy()+row*g->ch;
 rect(0,y,W,g->ch,P.bg);
 const mix_cell_t *cells=mix_terminal_row(row);
 if(!cells)return;
 for(int col=0;col<g->cols;col++) {
  mix_cell_t c=cells[col];
  if(!c.width)continue;
  int w=(c.width==2&&col<g->cols-1)?g->cw*2:g->cw;
  uint16_t fg=ansi(c.fg,true),bg=ansi(c.bg,false);
  if(c.flags&MIX_ATTR_INVERSE){uint16_t t=fg;fg=bg;bg=t;}
  /* SGR DIM is a visual attribute, not just parser state. Blend the
   * foreground toward this cell's background while keeping antialiasing. */
  if(c.flags&MIX_ATTR_DIM)fg=blend565(bg,fg,160);
  rect(ox+col*g->cw,y,w,g->ch,bg);
  cell_glyph(ox+col*g->cw,y,w,g->ch,g->font,c.codepoint,fg,(c.flags&MIX_ATTR_BOLD)!=0);
  if(c.flags&MIX_ATTR_UNDERLINE)rect(ox+col*g->cw,y+g->ch-3,w,1,fg);
 }
 if(mix_terminal_cursor_visible()&&!mix_terminal_scroll_offset()&&row==mix_terminal_cursor_y())
  rect(ox+clamp(mix_terminal_cursor_x(),0,g->cols-1)*g->cw,y+g->ch-2,g->cw,2,P.accent);
}
/* Motion uses integer easing, no per-pixel screen blend or extra framebuffer.
 * Even after a slow tick, elapsed time skips straight to the appropriate frame. */
static unsigned motion_ease(unsigned t) {
 if(t>=MOTION_SCALE)return MOTION_SCALE;
 unsigned r=t;
 return r*r*(3u*MOTION_SCALE-2u*r)/(MOTION_SCALE*MOTION_SCALE);
}
static int motion_lerp(int from,int to,unsigned t) {
 return from+(to-from)*(int)t/(int)MOTION_SCALE;
}
static const char *waiting_caption(void) {
 return tr(view.linux_online?"Starting the application":"Linux is offline",
           view.linux_online?"正在启动应用":"Linux 离线");
}
static void waiting_dots(void) {
 rect(0,WAIT_DOTS_Y,W,WAIT_DOTS_H,P.bg);
 for(int i=0;i<3;i++) {
  unsigned phase=((uint32_t)(ui_ms-app_enter_ms)+(unsigned)(2-i)*180u)%1200u;
  unsigned triangle=phase<600u?phase:1200u-phase;
  unsigned pulse=motion_ease(triangle*MOTION_SCALE/600u);
  int size=motion_lerp(10,18,pulse);
  uint16_t color=blend565(P.raised,P.accent,(uint8_t)motion_lerp(40,255,pulse));
  roundbox(W/2+(i-1)*32-size/2,WAIT_DOTS_Y+(WAIT_DOTS_H-size)/2,size,size,size/2,color);
 }
}
static void waiting_draw(void) {
 rect(0,0,W,CONTENT_H,P.bg);
 const char *glyph=current_app==MIX_APP_TRANSLATE?ICON_TRANSLATE:
                   current_app==MIX_APP_NOTES?ICON_NOTES:ICON_SYSTEM;
 roundbox(W/2-68,232,136,136,MIX_RADIUS_L,P.raised);
 icon(W/2,300,MIX_DP(28),P.accent,glyph);
 text_mid(W/2,402,MIX_SP(20),P.text,tr(app_title_en(),app_title_zh()));
 text_mid(W/2,476,20,view.linux_online?P.muted:P.warning,waiting_caption());
 if(view.linux_online)waiting_dots();
 else text_mid(W/2,WAIT_DOTS_Y,20,P.muted,tr("Check the USB link, then try again","请检查 USB 连接后重试"));
}
static ui_rect_t launch_bounds(int *cx_out,int *cy_out,int *title_x,int *title_y) {
 int x,y;card_rect(launch_motion.card,&x,&y);
 unsigned e=motion_ease(launch_motion.progress);
 int cx=motion_lerp(x+MIX_SPACE_4+6+MIX_DP(48)/2,W/2,e);
 int cy=motion_lerp(y+MIX_SPACE_2+6+MIX_DP(48)/2,300,e);
 const char *title=tr(app_names_en[launch_motion.card],app_names_zh[launch_motion.card]);
 int tw=text_width(MIX_SP(20),title);
 int tx0=motion_lerp(x+MIX_SPACE_4+6,(W-tw)/2,e);
 int ty0=motion_lerp(y+MIX_DP(56)+6,402,e);
 *cx_out=cx;*cy_out=cy;*title_x=tx0;*title_y=ty0;
 /* Text ink can extend past advance and line ascent. A bounded extra margin
  * also covers the emergency font, without clearing an entire screen. */
 return (ui_rect_t){clamp((cx-68<tx0-8?cx-68:tx0-8),0,W),
  clamp(cy-68,0,CONTENT_H),clamp((cx+68>tx0+tw+12?cx+68:tx0+tw+12),0,W),
  clamp((cy+68>ty0+88?cy+68:ty0+88),0,CONTENT_H)};
}
static void launch_motion_draw(void) {
 int cx,cy,tx0,ty0;
 launch_motion.previous=launch_bounds(&cx,&cy,&tx0,&ty0);
 roundbox(cx-68,cy-68,136,136,MIX_RADIUS_L,P.raised);
 launcher_icon(launch_motion.card,cx,cy,CARD_ICON_SIZE,P.accent);
 text(tx0,ty0,CARD_TITLE_SIZE,P.text,tr(app_names_en[launch_motion.card],app_names_zh[launch_motion.card]));
}
static void launch_motion_step(void) {
 int cx,cy,tx0,ty0;
 ui_rect_t old=launch_motion.previous,next=launch_bounds(&cx,&cy,&tx0,&ty0);
 ui_rect_t dirty={old.x0<next.x0?old.x0:next.x0,old.y0<next.y0?old.y0:next.y0,
                  old.x1>next.x1?old.x1:next.x1,old.y1>next.y1?old.y1:next.y1};
 int64_t began=esp_timer_get_time();
 rect(dirty.x0,dirty.y0,dirty.x1-dirty.x0,dirty.y1-dirty.y0,P.bg);
 launch_motion_draw();
 uint32_t cost=elapsed_us(began);
 if(cost>motion_stats.draw_max_us)motion_stats.draw_max_us=cost;
 present_rect(dirty.x0,dirty.y0,dirty.x1,dirty.y1);
 if(last_draw_error==ESP_OK){
  uint32_t stamp=ui_clock_ms(),gap=(uint32_t)(stamp-motion_stats.last_present_ms);
  if(gap>motion_stats.max_gap_ms)motion_stats.max_gap_ms=gap;
  motion_stats.last_present_ms=stamp;motion_stats.frames++;
 }
}
static void launch_motion_end(bool cancelled) {
 motion_stats.last_frames=motion_stats.frames;
 motion_stats.last_duration_ms=launch_motion.pending?0:(uint32_t)(ui_clock_ms()-launch_motion.start_ms);
 if(cancelled)motion_stats.cancelled++;
 launch_motion.active=false;
}
bool mix_ui_motion_active(void){return launch_motion.active||content_motion.phase!=CONTENT_IDLE;}
bool mix_ui_needs_fast_tick(void) {
 if(mix_ui_motion_active())return true;
 if(page==PAGE_APP&&app_has_session()&&view.terminal_open&&!content_motion.waiting&&!modal)
  return term_recent&&(uint32_t)(ui_clock_ms()-term_present_ms)<TERM_FAST_MS;
 return page==PAGE_SETTINGS&&!modal&&(settings_body_dirty||settings_scroll_dirty);
}
/* Scan callbacks and successful submissions are different counters. A host
 * can sample twice and compute rates using the device's monotonic clock;
 * neither counter claims that pixels on the physical glass were measured. */
static size_t performance_finish(char *out,size_t capacity,int n) {
 if(n<=0||(size_t)n>=capacity)return 0;
 mix_present_stats_t stats;
 if(mix_present_get_stats(&stats)!=ESP_OK)return 0;
 int extra=snprintf(out+n-1,capacity-(size_t)n+1,
  ",\"scan_frames\":%lu,\"scan_ms\":%lu,\"presents\":%lu}",
  (unsigned long)stats.frame_count,(unsigned long)ui_clock_ms(),(unsigned long)stats.present_count);
 return extra>0&&(size_t)extra<capacity-(size_t)n+1?(size_t)n-1+(size_t)extra:0;
}
size_t mix_ui_performance(char *out,size_t capacity) {
 if(!out||!capacity)return 0;
 if(locked||page==PAGE_SETTINGS) {
  const gesture_stats_t *s=locked?&lock_stats:&settings_stats;
  int n=snprintf(out,capacity,"{\"schema\":1,\"scene\":\"%s\",\"frames\":%lu,\"draw_max_us\":%lu,"
   "\"present_max_us\":%lu,\"max_gap_ms\":%lu,\"last_pixels\":%lu,\"raster_rows\":%lu,\"fast_tick\":%s,\"draw_healthy\":%s}",
   locked?"lock":"settings",(unsigned long)s->frames,(unsigned long)s->draw_max_us,
   (unsigned long)s->present_max_us,(unsigned long)s->max_gap_ms,(unsigned long)s->pixels,
   (unsigned long)s->rows,mix_ui_needs_fast_tick()?"true":"false",mix_ui_draw_healthy()?"true":"false");
  return performance_finish(out,capacity,n);
 }
 int n=snprintf(out,capacity,"{\"schema\":1,\"animations\":%lu,\"active\":%s,\"frames\":%lu,"
  "\"last_frames\":%lu,\"duration_ms\":%lu,\"max_gap_ms\":%lu,\"draw_max_us\":%lu,"
  "\"present_max_us\":%lu,\"first_frame_us\":%lu,\"cancelled\":%lu,\"draw_healthy\":%s,"
  "\"fade_phase\":%u,\"fade_count\":%lu,\"fade_in_frames\":%lu,\"fade_out_frames\":%lu,"
  "\"fade_render_us\":%lu,\"fade_present_max_us\":%lu}",
  (unsigned long)motion_stats.animations,mix_ui_motion_active()?"true":"false",(unsigned long)motion_stats.frames,
  (unsigned long)motion_stats.last_frames,(unsigned long)motion_stats.last_duration_ms,
  (unsigned long)motion_stats.max_gap_ms,(unsigned long)motion_stats.draw_max_us,
  (unsigned long)motion_stats.present_max_us,(unsigned long)motion_stats.first_frame_us,
  (unsigned long)motion_stats.cancelled,mix_ui_draw_healthy()?"true":"false",
  (unsigned)content_motion.phase,(unsigned long)content_motion.transitions,
  (unsigned long)content_motion.in_frames,(unsigned long)content_motion.out_frames,
  (unsigned long)content_motion.render_us,(unsigned long)content_motion.max_present_us);
 return performance_finish(out,capacity,n);
}
static bool first_content_ready(uint32_t now) {
 if(page!=PAGE_APP||!app_has_session())return true;
 if(!view.terminal_open||!view.linux_online||view.running_app!=current_app)return false;
 /* OPENED is not a painted TUI. Prefer actual glyphs/backgrounds, but a
  * deliberately empty shell must become usable after a bounded grace time. */
 for(int r=0;r<geom()->rows;r++) {
  const mix_cell_t *cells=mix_terminal_row(r);
  if(!cells)continue;
  for(int c=0;c<geom()->cols;c++)
   if((cells[c].codepoint&&cells[c].codepoint!=' ')||
      cells[c].bg!=MIX_COLOR_DEFAULT_BG||(cells[c].flags&MIX_ATTR_INVERSE))return true;
 }
 return content_motion.open_seen&&(uint32_t)(now-content_motion.opened_ms)>=FIRST_CONTENT_GRACE_MS;
}
static void app_draw(void) {
 if(!app_has_session()){rect(0,0,W,CONTENT_H,P.bg);return;}
 if(!view.terminal_open||(content_motion.waiting&&!content_motion.render_target)){waiting_draw();return;}
 rect(0,0,W,CONTENT_H,P.bg);
 for(int r=0;r<geom()->rows;r++)terminal_row_draw(r);
 mix_terminal_clean();
 last_cursor_x=mix_terminal_cursor_x();last_cursor_y=mix_terminal_cursor_y();
 last_cursor_visible=mix_terminal_cursor_visible();last_scroll_offset=mix_terminal_scroll_offset();
}

/* ---------- modal ---------- */
static void modal_draw(void) {
 roundbox(184,206,656,332,24,P.raised);
 text(216,238,30,P.text,tr("Confirm on this device","请在本机确认"));
 const char *en="Reset local input state?",*zh="重置本地输入状态？";
 if(modal==MIX_ACTION_JOB_START){en="Run the configured maintenance task?";zh="运行预设维护任务？";}
 if(modal==MIX_ACTION_JOB_CANCEL){en="Cancel the running maintenance task?";zh="取消正在运行的维护任务？";}
 if(modal==MIX_ACTION_NET_FORGET){en="Forget this saved network?";zh="忘记这个已保存的网络？";}
 text(216,302,23,P.text,tr(en,zh));
 label(216,354,"Local touch or Enter accepts. Escape rejects.","本地触摸或回车确认，Escape 拒绝。");
 label(216,392,"Terminal output cannot approve this request.","终端输出无法批准此请求。");
 button(216,454,272,"Reject / Escape","拒绝 / Escape",false);
 button(520,454,288,"Confirm / Enter","确认 / Enter",true);
}
static void resolve_modal(bool accept) {
 int a=modal;modal=0;repaint=true;
 if(accept)queue((mix_action_kind_t)a);
}

/* ---------- local lock and nonblocking adjustment feedback ---------- */
bool mix_ui_locked(void){return locked;}
bool mix_ui_display_awake(void){return display_awake;}
uint32_t mix_ui_locked_since(void){return locked_at;}
void mix_ui_lock_key(void) {
 /* Lock screen was removed from the product. Keep this ABI entry as a safe
  * no-op for older main-loop callers and recovery builds. */
 locked=false;display_awake=true;
}
static __attribute__((unused)) void lock_feedback_clear(void) {
 lock_touch_active=lock_feedback_visible=lock_feedback_dirty=lock_returning=false;
 lock_offset=lock_return_from=0;
}
static void lock_feedback_return(void) {
 lock_touch_active=false;lock_returning=lock_offset!=0;
 lock_return_from=lock_offset;lock_release_at=ui_clock_ms();lock_feedback_dirty=true;
}
static void lock_bounds(mix_present_rect_t out[3]) {
 int y=CONTENT_H/2-36-lock_offset/2;
 int width=180+lock_offset/2,pill_y=CONTENT_H-30-lock_offset/8;
 out[0]=(mix_present_rect_t){W/2-lock_word_width/2,y-8,W/2+(lock_word_width+1)/2,y+96};
 out[1]=(mix_present_rect_t){W/2-width/2-2,pill_y-2,W/2+(width+1)/2+2,pill_y+8};
 out[2]=lock_feedback_visible?(mix_present_rect_t){lock_touch_x-52,lock_touch_y-52,lock_touch_x+53,lock_touch_y+53}:
                            (mix_present_rect_t){0,0,0,0};
 for(int i=0;i<3;i++) {
  out[i].x0=clamp(out[i].x0,0,W);out[i].x1=clamp(out[i].x1,0,W);
  out[i].y0=clamp(out[i].y0,0,CONTENT_H);out[i].y1=clamp(out[i].y1,0,CONTENT_H);
 }
}
static void lock_artwork(void) {
 /* Default wallpaper is the MixOS wordmark, not duplicate status text.
  * It follows the swipe, then returns smoothly when the gesture is cancelled. */
 text_mid(W/2,CONTENT_H/2-36-lock_offset/2,64,P.text,"MixOS");
 int width=180+lock_offset/2;
 uint16_t color=blend565(P.muted,P.accent,(uint8_t)(lock_offset*255/192));
 roundbox(W/2-width/2,CONTENT_H-30-lock_offset/8,width,6,3,color);
 if(lock_feedback_visible) {
  uint32_t age=(uint32_t)(ui_ms-lock_feedback_at);
  unsigned phase=age>=240?MOTION_SCALE:age*MOTION_SCALE/240;
  int radius=motion_lerp(12,46,motion_ease(phase));
  uint32_t fade=lock_touch_active?0:(uint32_t)(ui_ms-lock_release_at);
  unsigned opacity=fade>=180?0:180-fade;
  /* Blend a ring directly over existing artwork. Clearing its centre would
   * cut a hole in the logo when the finger passes over it. */
  int x0=clamp(lock_touch_x-radius-1,0,W),x1=clamp(lock_touch_x+radius+2,0,W);
  int y0=clamp(lock_touch_y-radius-1,0,CONTENT_H),y1=clamp(lock_touch_y+radius+2,0,CONTENT_H);
  for(int y=y0;y<y1;y++)for(int x=x0;x<x1;x++) {
   int dx=x-lock_touch_x,dy=y-lock_touch_y,d=dx*dx+dy*dy;
   if(d<=radius*radius&&d>=(radius-4)*(radius-4))
    fb[y*W+x]=blend565(fb[y*W+x],P.accent,(uint8_t)opacity);
  }
 }
}
static __attribute__((unused)) void lock_draw(void) {
 lock_word_width=text_width(64,"MixOS")+24;
 rect(0,0,W,CONTENT_H,P.bg);lock_artwork();
 lock_bounds(lock_previous);
 lock_feedback_dirty=lock_footer_dirty=false;lock_frame_ms=ui_ms;
}
static bool lock_feedback_animating(uint32_t now) {
 return lock_feedback_dirty||lock_returning||(lock_feedback_visible&&
   (!lock_touch_active||(uint32_t)(now-lock_feedback_at)<240u||
    (uint32_t)(lock_frame_ms-lock_feedback_at)<240u));
}
static __attribute__((unused)) void lock_feedback_tick(uint32_t now) {
 if(!lock_feedback_animating(now)) {
  if(lock_footer_dirty){status_bar();present(STATUS_Y,H);lock_footer_dirty=false;}
  return;
 }
 /* Live finger movement is already frame-paced by the presenter. Only the
  * autonomous ripple/return animation needs the software interval. */
 if(!(lock_touch_active&&lock_feedback_dirty)&&
    (uint32_t)(now-lock_frame_ms)<MOTION_FRAME_MS)return;
 int64_t began=esp_timer_get_time();
 if(lock_returning) {
  uint32_t age=(uint32_t)(now-lock_release_at);
  lock_offset=motion_lerp(lock_return_from,0,motion_ease(age>=180?MOTION_SCALE:age*MOTION_SCALE/180));
  if(age>=180)lock_returning=false;
 }
 if(!lock_touch_active&&(uint32_t)(now-lock_release_at)>=180u)lock_feedback_visible=false;
 mix_present_rect_t next[3],regions[4];unsigned count=0;uint32_t pixels=0;
 lock_bounds(next);
 for(int i=0;i<3;i++) {
  mix_present_rect_t a=lock_previous[i],b=next[i];
  bool old_valid=a.x1>a.x0&&a.y1>a.y0,new_valid=b.x1>b.x0&&b.y1>b.y0;
  if(!old_valid&&!new_valid)continue;
  mix_present_rect_t r=!old_valid?b:!new_valid?a:(mix_present_rect_t){
   a.x0<b.x0?a.x0:b.x0,a.y0<b.y0?a.y0:b.y0,a.x1>b.x1?a.x1:b.x1,a.y1>b.y1?a.y1:b.y1};
  regions[count++]=r;
  rect(r.x0,r.y0,r.x1-r.x0,r.y1-r.y0,P.bg);
  pixels+=(uint32_t)(r.x1-r.x0)*(r.y1-r.y0);
 }
 /* Clear all regions BEFORE repainting artwork: a ripple overlapping the
  * wordmark must never erase text drawn by another region. One batch, one
  * future-frame wait, and no empty logo-to-indicator gap is transferred. */
 lock_artwork();
 if(lock_footer_dirty) {
  status_bar();regions[count++]=(mix_present_rect_t){0,STATUS_Y,W,H};
  pixels+=(uint32_t)W*STATUS_H;
 }
 uint32_t draw_us=elapsed_us(began);began=esp_timer_get_time();
 present_batch(regions,count);
 gesture_presented(&lock_stats,draw_us,elapsed_us(began),pixels);
 memcpy(lock_previous,next,sizeof(next));
 lock_frame_ms=now;lock_feedback_dirty=lock_footer_dirty=false;
}

static __attribute__((unused)) void lock_touch(int x,int y,bool press,bool release,bool down) {
 /* A touch is also the wake gesture. The old early return discarded every
  * contact while the backlight was off, leaving only the physical lock key
  * able to wake the lock surface. Start the swipe from this same contact. */
 if(!display_awake) {
  if(!press)return;
  display_awake=true;lock_awake_at=ui_clock_ms();repaint=true;
 }
 if(press) {
  lock_stats=(gesture_stats_t){0};
  /* The disabled footer is part of the lock gesture surface, not a
   * navigation dead zone. Any on-screen start may form an upward swipe. */
  unlock_tracking=x>=0&&x<W&&y>=0&&y<H;
  unlock_ready=false;unlock_x=x;unlock_y=y;
  lock_touch_x=clamp(x,0,W-1);lock_touch_y=clamp(y,0,CONTENT_H-12);
  lock_touch_active=lock_feedback_visible=unlock_tracking;lock_returning=false;
  lock_feedback_at=ui_clock_ms();lock_feedback_dirty=true;lock_offset=0;
 }
 if(down&&unlock_tracking) {
  lock_touch_x=clamp(x,0,W-1);lock_touch_y=clamp(y,0,CONTENT_H-12);
  lock_touch_active=true;lock_feedback_dirty=true;
  if(x<0||x>=W||y<0||y>=H||abs(x-unlock_x)>160) {
   unlock_tracking=unlock_ready=false;lock_feedback_return();
  }
  else {
   unlock_last_x=x;unlock_last_y=y;
   lock_awake_at=ui_clock_ms(); /* inactivity starts after the latest contact */
   lock_offset=clamp(unlock_y-y,0,192);
   unlock_ready=unlock_y-y>=160;
  }
 }
 if(release) {
  /* Releases retain the latest valid contact; cancellation has a separate
   * API and cannot complete a swipe. Reject unrelated release coordinates. */
  bool valid=x>=0&&y>=0&&x<W&&y<H&&
             abs(x-unlock_last_x)<80&&abs(y-unlock_last_y)<80;
  if(unlock_tracking&&unlock_ready&&valid) {
   locked=false;display_awake=true;repaint=true;mix_terminal_invalidate();
   if(page==PAGE_APP&&content_motion.waiting)content_motion.open_seen=false;
  }
  lock_feedback_return();
  unlock_tracking=unlock_ready=false;
 }
}
void mix_ui_feedback(mix_ui_feedback_t kind,int value) {
 if(locked||kind<MIX_UI_VOLUME||kind>MIX_UI_KEYBOARD_LIGHT)return;
 toast.kind=kind;toast.value=value;toast.active=true;toast.dirty=true;toast.at=ui_clock_ms();
 if(kind==MIX_UI_VOLUME&&value>=0)volume_percent=clamp(value,0,100);
}
static void toast_draw(void) {
 if(!toast.active||locked||launch_motion.active||
    (content_motion.phase!=CONTENT_IDLE&&content_motion.phase!=CONTENT_WAIT))return;
 if(!toast.shown){toast.at=ui_clock_ms();toast.shown=true;}
 char b[80];
 const char *name=toast.kind==MIX_UI_VOLUME?tr("Volume","音量"):
                  toast.kind==MIX_UI_BRIGHTNESS?tr("Brightness","亮度"):tr("Keyboard light","键盘背光");
 if(toast.value<0)snprintf(b,sizeof(b),"%s  %s",name,tr("Unavailable","不可用"));
 else if(toast.kind==MIX_UI_KEYBOARD_LIGHT)snprintf(b,sizeof(b),"%s  %d / 8",name,clamp(toast.value,0,8));
 else snprintf(b,sizeof(b),"%s  %d%%",name,clamp(toast.value,0,100));
 roundbox(192,TOAST_Y,640,TOAST_H,24,P.raised);
 /* Keep meaningful icons available with the already-installed font. */
 int cx=238,cy=TOAST_Y+48;
 if(toast.kind==MIX_UI_VOLUME) {
  roundbox(cx-22,cy-9,14,18,3,P.accent);
  for(int dx=0;dx<15;dx++)rect(cx-9+dx,cy-9-dx,1,18+2*dx,P.accent);
  for(int dy=-17;dy<=17;dy++) {
   int inset=abs(dy)/4;
   rect(cx+20-inset,cy+dy,3,1,P.accent);
  }
 } else if(toast.kind==MIX_UI_BRIGHTNESS) {
  roundbox(cx-12,cy-12,24,24,12,P.accent);
  rect(cx-2,cy-27,4,9,P.accent);rect(cx-2,cy+18,4,9,P.accent);
  rect(cx-27,cy-2,9,4,P.accent);rect(cx+18,cy-2,9,4,P.accent);
  for(int sx=-1;sx<=1;sx+=2)for(int sy=-1;sy<=1;sy+=2)
   line(cx+sx*15,cy+sy*15,cx+sx*21,cy+sy*21,P.accent);
 } else {
  roundbox(cx-27,cy-18,54,36,6,P.accent);
  roundbox(cx-23,cy-14,46,28,3,P.raised);
  for(int row=0;row<2;row++)for(int col=0;col<5;col++)rect(cx-19+col*8,cy-10+row*8,5,5,P.accent);
  rect(cx-14,cy+7,28,4,P.accent);
 }
 text(280,TOAST_Y+16,30,P.text,b);
 roundbox(280,TOAST_Y+72,512,8,4,P.card);
 if(toast.value>=0) {
  int max=toast.kind==MIX_UI_KEYBOARD_LIGHT?8:100;
  int length=clamp(toast.value,0,max)*512/max;
  if(length)roundbox(280,TOAST_Y+72,length,8,4,P.accent);
 }
}

/* ---------- frame assembly ---------- */
static void draw_all(void) {
 term_footer_dirty=false;
 draw_offset_y=0;draw_limit_y=CONTENT_H;draw_clip_y0=0;
 if(launch_motion.active){rect(0,0,W,CONTENT_H,P.bg);launch_motion_draw();}
 else switch(page) {
 case PAGE_HOME: home_draw();break;
 case PAGE_APP: app_draw();break;
 default: settings_draw();break;
 }
 status_bar();
 if(modal)modal_draw();
 toast_draw();
}
static void content_cancel(void) {
 mix_present_fade_end();content_motion.phase=CONTENT_IDLE;
 content_motion.render_target=false;content_motion.waiting_surface=false;
}
static bool content_result(esp_err_t e,int64_t began) {
 uint32_t cost=elapsed_us(began);
 if(cost>content_motion.max_present_us)content_motion.max_present_us=cost;
 if(e==ESP_OK)return true;
 last_draw_error=e;repaint=true;content_cancel();return false;
}
static void content_prepare(uint32_t now) {
 /* Do not fade a placeholder into view and then hard-cut it away when the
  * terminal finally speaks. Once the launcher has faded to the shared
  * background, keep a small waiting surface there until the first useful
  * terminal frame exists. */
 if(!first_content_ready(now)) {
  content_motion.phase=CONTENT_WAIT;
  content_motion.waiting=true;
  if(!content_motion.waiting_surface) {
   int64_t began=esp_timer_get_time();
   waiting_draw();status_bar();toast_draw();present(0,H);
   content_motion.render_us=elapsed_us(began);
   if(last_draw_error!=ESP_OK){content_cancel();return;}
   content_motion.waiting_surface=true;content_motion.frame_ms=now;repaint=false;
  }
  return;
 }
 /* OUT finished with opacity zero: both driver bodies are already exactly
  * P.bg. Render the target only into the existing canvas; no full-screen
  * background copy or extra framebuffer is needed for the handoff. */
 int64_t began=esp_timer_get_time();
 content_motion.render_target=true;
 if(page==PAGE_SETTINGS)settings_draw();else app_draw();
 content_motion.render_target=false;
 content_motion.render_us=elapsed_us(began);
 /* app_draw cleans only the snapshot rendered here. Future terminal data
  * continues parsing/marking dirty while this immutable canvas is fading. */
 if(!content_result(mix_present_fade_begin(fb,P.bg),began))return;
 content_motion.waiting_surface=false;
 content_motion.phase=CONTENT_IN;content_motion.start_ms=ui_clock_ms();
 content_motion.frame_ms=content_motion.start_ms;
 content_motion.transitions++;content_motion.in_frames=0;repaint=false;
}
static void content_start_out(uint32_t now) {
 int64_t began=esp_timer_get_time();
 /* At this point both driver buffers and fb contain the same launcher (or
  * waiting) surface. The presenter fades that borrowed image toward the
  * already shared theme background without a full-screen cross-blend. */
 if(!content_result(mix_present_fade_begin(fb,P.bg),began))return;
 content_motion.phase=CONTENT_OUT;content_motion.start_ms=ui_clock_ms();
 content_motion.frame_ms=content_motion.start_ms;
 content_motion.out_frames=0;repaint=false;
 (void)now;
}
static void content_tick(uint32_t now) {
 if(content_motion.phase==CONTENT_PREPARE){content_prepare(now);return;}
 if(content_motion.phase==CONTENT_WAIT) {
  if(first_content_ready(now)&&!toast.active) {content_start_out(now);return;}
  if(repaint) {
   int64_t began=esp_timer_get_time();
   waiting_draw();status_bar();toast_draw();present(0,H);
   content_motion.render_us=elapsed_us(began);
   if(last_draw_error!=ESP_OK){content_cancel();return;}
   repaint=false;content_motion.frame_ms=now;
  }
  if(view.linux_online&&(uint32_t)(now-content_motion.frame_ms)>=MOTION_FRAME_MS) {
   content_motion.frame_ms=now;waiting_dots();toast_draw();present(WAIT_DOTS_Y,WAIT_DOTS_Y+WAIT_DOTS_H);
  }
  return;
 }
 uint32_t elapsed=(uint32_t)(now-content_motion.start_ms);
 uint32_t duration=content_motion.phase==CONTENT_OUT?FADE_OUT_MS:FADE_IN_MS;
 if(elapsed<duration&&(uint32_t)(now-content_motion.frame_ms)<MOTION_FRAME_MS)return;
 content_motion.frame_ms=now;
 unsigned t=elapsed>=duration?MOTION_SCALE:elapsed*MOTION_SCALE/duration;
 uint8_t a=(uint8_t)(motion_ease(t)*255u/MOTION_SCALE);
 bool outgoing=content_motion.phase==CONTENT_OUT;
 int64_t began=esp_timer_get_time();
 if(!content_result(mix_present_fade_step(outgoing?(uint8_t)(255-a):a),began))return;
 if(outgoing)content_motion.out_frames++;else content_motion.in_frames++;
 if(elapsed>=duration) {
  mix_present_fade_end();
  if(outgoing)content_motion.phase=CONTENT_PREPARE;
  else {
   content_motion.phase=CONTENT_IDLE;content_motion.waiting=false;
   content_motion.waiting_surface=false;
   /* Refresh deferred telemetry once. Terminal bytes received during the
    * fade still carry dirty flags. A settings repaint requested after its
    * snapshot was rendered must also survive this handoff. */
   if(page==PAGE_APP)repaint=false;
   status_bar();present(STATUS_Y,H);
  }
 }
}
/* See mix_ui.h: deliberately unsynchronised with the drawing path. */
const uint16_t *mix_ui_framebuffer(size_t *bytes, uint16_t *width, uint16_t *height) {
 if(bytes)*bytes=(size_t)W*H*sizeof(uint16_t);
 if(width)*width=W;
 if(height)*height=H;
 return fb;
}
esp_err_t mix_ui_init(esp_lcd_panel_handle_t p) {
 if(!p)return ESP_ERR_INVALID_ARG;
 if(fb)return ESP_ERR_INVALID_STATE;
 fb=heap_caps_malloc((size_t)W*H*sizeof(uint16_t),MALLOC_CAP_SPIRAM|MALLOC_CAP_8BIT);
 if(!fb)return ESP_ERR_NO_MEM;
 esp_err_t ready=mix_present_init(p);
 if(ready!=ESP_OK){free(fb);fb=NULL;return ready;}
 nvs_handle_t h;
 if(nvs_open("mixui",NVS_READONLY,&h)==ESP_OK) {
  /* Zero is a valid stored value for every one of these, so a missing key can
   * only be told apart by the return code. Ignoring it made the readable
   * terminal default unreachable on a device that had never saved a
   * preference. */
  uint8_t stored;
  if(nvs_get_u8(h,"theme",&stored)==ESP_OK)theme=stored;
  if(nvs_get_u8(h,"lang",&stored)==ESP_OK)language=stored;
  if(nvs_get_u8(h,"term",&stored)==ESP_OK)geometry=stored;
  else geometry=1; /* the readable default on a 3.2 inch panel */
  nvs_close(h);
 } else geometry=1;
 if(theme>=MIX_THEME_COUNT)theme=0;
 if(language>1)language=0;
 if(geometry>=GEOMETRY_COUNT)geometry=1;
 if(!ttf_font_ready()){language=0;snprintf(notice,sizeof(notice),"Font unavailable: reduced ASCII fallback");}
 mix_terminal_resize(geom()->cols,geom()->rows);
 repaint=true;return ESP_OK;
}
void mix_ui_tick(const mix_view_t *v,uint32_t now) {
 if(!fb||!v)return;
 ui_ms=now;
 if(page==PAGE_SETTINGS&&!settings_menu&&section==SEC_NETWORK&&network_content_changed()) {
  /* Scan completion and connection errors are not in the telemetry struct. */
  settings_body_dirty=true;
  settings_tap_active=false;
 }
 if(page==PAGE_SETTINGS&&!settings_menu&&section==SEC_DEVICE&&test_dirty&&!modal) {
  test_dirty=false;settings_body_dirty=true;
 }
 /* Only an explicit successful EXIT from the matching host session means
  * that the app reached its root and closed. Disconnects never imply Back. */
 if(v->terminal_exit_serial!=seen_terminal_exit) {
  seen_terminal_exit=v->terminal_exit_serial;
  if(page==PAGE_APP&&app_has_session()&&v->terminal_exit_app==current_app&&
     v->linux_online&&!v->terminal_open&&!v->maintenance_busy&&!v->ota_state)
   navigate(PAGE_HOME);
 }
 if(term_recent&&(uint32_t)(now-term_present_ms)>=TERM_FAST_MS)term_recent=false;
 if(page==PAGE_SETTINGS&&!modal&&notice_visible&&
    (uint32_t)(now-notice_at)>=NOTICE_MS) {
  notice_visible=false;settings_body_dirty=true;
 }
 if(toast.active&&toast.shown&&(uint32_t)(now-toast.at)>=1500) {
  toast.active=toast.shown=toast.dirty=false;
  /* One repaint removes the overlay; never one full repaint per animation
   * frame. Latest terminal state is rendered, not a stale saved rectangle. */
  repaint=true;
 }
 /* Session changes are not 1 Hz telemetry: show readiness/offline immediately
  * instead of keeping the startup surface over an already open application. */
 if(view.terminal_open!=v->terminal_open||view.running_app!=v->running_app||
    view.linux_online!=v->linux_online) {
  if(page==PAGE_APP&&app_has_session()&&
     (!v->terminal_open||!v->linux_online||v->running_app!=current_app)) {
   /* A waiting scene has no borrowed canvas and remains live on disconnect.
    * Cancel only a frozen image whose session is no longer valid. */
   if(content_motion.phase!=CONTENT_WAIT)content_cancel();
   content_motion.waiting=true;
  }
  if(page==PAGE_SETTINGS&&view.linux_online!=v->linux_online) {
   settings_footer_dirty=true;
   if(section==SEC_NETWORK||section==SEC_SYSTEM)settings_body_dirty=true;
  }
  view.terminal_open=v->terminal_open;view.running_app=v->running_app;
  view.linux_online=v->linux_online;
  if(page==PAGE_APP||page==PAGE_HOME)repaint=true;
 }
 if(page==PAGE_APP&&view.terminal_open) {
  if(!content_motion.open_seen){content_motion.open_seen=true;content_motion.opened_ms=now;}
 } else content_motion.open_seen=false;
 bool transition_cancel=modal||v->maintenance_busy||v->ota_state||last_draw_error!=ESP_OK;
 if(transition_cancel&&content_motion.phase!=CONTENT_IDLE){content_cancel();repaint=true;}
 /* The parent has had its turn to read the passphrase; erase it now. */
 if(net_pass_expire) {
  net_pass_expire=false;
  memset(net_pass,0,sizeof(net_pass));net_pass_len=0;net_reveal=false;
  if(page==PAGE_SETTINGS&&section==SEC_NETWORK&&net_view&&!modal)repaint=true;
 }
 /* A running A/B transfer repaints on its own cadence so the progress bar
  * moves without waiting for the 1 Hz telemetry snapshot. */
 if(v->ota_state!=view.ota_state||(v->ota_state==1&&v->ota_percent!=view.ota_percent)) {
  view.ota_state=v->ota_state;view.ota_percent=v->ota_percent;
  if(page==PAGE_SETTINGS&&section==SEC_SYSTEM&&!modal)repaint=true;
 }
 if(!have_view||(uint32_t)(now-view_ms)>=1000) {
  bool changed=!have_view||memcmp(&view,v,sizeof(view))!=0;
  bool body_changed=page==PAGE_SETTINGS&&settings_metrics_changed(&view,v);
  view=*v;view_ms=now;have_view=true;
  if(changed||(page==PAGE_SETTINGS&&section==SEC_POWER)) {
   if(!modal&&!mix_ui_motion_active()&&!repaint&&
      (page==PAGE_SETTINGS||(page==PAGE_APP&&view.terminal_open))) {
    if(page==PAGE_SETTINGS)settings_footer_dirty=true;
    else if(app_has_session()&&!content_motion.waiting)term_footer_dirty=true;
    else {status_bar();present(STATUS_Y,H);}
    if(body_changed)settings_body_dirty=true;
   } else repaint=true;
  }
 }
 bool painted=false;
 if(launch_motion.active) {
  uint32_t elapsed=launch_motion.pending?0:(uint32_t)(now-launch_motion.start_ms);
  bool cancel=modal||v->maintenance_busy||v->ota_state||last_draw_error!=ESP_OK;
  if(cancel) {
   launch_motion_end(true);repaint=true;mix_terminal_invalidate();
  } else if(elapsed>=MIX_MOTION_MEDIUM) {
   launch_motion_end(false);mix_terminal_invalidate();
   /* The launcher surface is still the current framebuffer. Borrow it for a
    * short shared-background fade before preparing the immutable app snapshot;
    * never expose a half-rendered terminal canvas during this handoff. */
   if(first_content_ready(now)) {
    repaint=false;content_start_out(now);
    painted=content_motion.phase!=CONTENT_IDLE;
   } else {
    /* Keep the waiting icon/title visible until there is a useful first
     * frame, rather than fading them out just to immediately draw them back. */
    content_motion.phase=CONTENT_WAIT;content_motion.waiting_surface=true;
    repaint=true;draw_all();repaint=false;present(0,H);
    content_motion.frame_ms=ui_clock_ms();painted=true;
   }
  } else if(launch_motion.pending) {
   launch_motion.progress=0;repaint=true;
  } else {
   /* Adopt session state above without repainting the full scene. */
   repaint=false;painted=true;
   if((uint32_t)(now-launch_motion.frame_ms)>=MOTION_FRAME_MS) {
    launch_motion.frame_ms=now;
    launch_motion.progress=(uint16_t)(elapsed*MOTION_SCALE/MIX_MOTION_MEDIUM);
    launch_motion_step();
   }
  }
 }
 /* While content is fading, the framebuffer is a frozen handoff canvas. The
  * terminal model may continue receiving bytes and marking rows dirty, but no
  * row is drawn until the incoming fade has completed. */
 if(!painted&&content_motion.phase!=CONTENT_IDLE) {
  painted=true;
  content_tick(now);
 }
 if(repaint&&!painted) {
  bool first_motion=launch_motion.active&&launch_motion.pending;
  int64_t began=esp_timer_get_time();
  draw_all();repaint=false;test_dirty=false;wait_ms=now;
  uint32_t draw_us=elapsed_us(began);
  int64_t present_began=esp_timer_get_time();
  present(0,H);painted=true;
  if(page==PAGE_SETTINGS&&!modal) {
   settings_stats.rows=CONTENT_H;settings_stats.timed=false;
   gesture_presented(&settings_stats,draw_us,elapsed_us(present_began),W*H);
  }
  if(first_motion&&last_draw_error==ESP_OK) {
   /* Cold glyph generation and the initial full-screen copy are setup, not
    * elapsed animation. Both must finish before its visible clock starts. */
   motion_stats.first_frame_us=elapsed_us(began);motion_stats.present_max_us=0;
   launch_motion.pending=false;launch_motion.start_ms=ui_clock_ms();
   launch_motion.frame_ms=launch_motion.start_ms;
   motion_stats.last_present_ms=launch_motion.start_ms;motion_stats.frames=1;
  }
 }
 if(!painted&&page==PAGE_SETTINGS&&!modal&&!mix_ui_motion_active()) {
  bool body=settings_body_dirty;
  if(body&&(uint32_t)(now-settings_frame_ms)>=MOTION_FRAME_MS) {
   int64_t began=esp_timer_get_time();
   settings_body_draw();toast_draw();settings_stats.rows=CONTENT_H;
   mix_present_rect_t areas[2]={{0,0,W,CONTENT_H},{0,STATUS_Y,W,H}};
   unsigned count=1;uint32_t pixels=W*CONTENT_H;
   if(settings_footer_dirty){status_bar();count=2;pixels+=W*STATUS_H;}
   uint32_t draw_us=elapsed_us(began);began=esp_timer_get_time();
   present_batch(areas,count);
   gesture_presented(&settings_stats,draw_us,elapsed_us(began),pixels);
   settings_frame_ms=now;settings_footer_dirty=false;painted=true;
  } else if(!body&&settings_footer_dirty) {
   status_bar();present(STATUS_Y,H);settings_footer_dirty=false;painted=true;
  }
 }
 /* A nonanimated launch or an interrupted fade can also leave a waiting
  * page. Render it normally first, then use the same ready-content handoff. */
 if(!launch_motion.active&&content_motion.phase==CONTENT_IDLE&&
    page==PAGE_APP&&content_motion.waiting&&!transition_cancel) {
  content_motion.phase=CONTENT_WAIT;content_motion.waiting_surface=true;
  if(!painted) {
   draw_all();repaint=false;present(0,H);painted=true;
   content_motion.frame_ms=ui_clock_ms();
  }
 }
 /* The waiting pulse touches one small band, not a whole screen per frame. */
 if(!painted&&!mix_ui_motion_active()&&page==PAGE_APP&&!view.terminal_open&&
    app_has_session()&&view.linux_online&&!modal&&
    !v->maintenance_busy&&!v->ota_state&&(uint32_t)(now-wait_ms)>=MOTION_FRAME_MS) {
  wait_ms=now;waiting_dots();toast_draw();present(WAIT_DOTS_Y,WAIT_DOTS_Y+WAIT_DOTS_H);
 }
 if(!painted&&!mix_ui_motion_active()&&!content_motion.waiting&&page==PAGE_APP&&view.terminal_open&&app_has_session()&&!modal&&
    ((uint32_t)(now-term_ms)>=TERM_FRAME_MS||term_footer_dirty)) {
  const geometry_t *g=geom();
  int oy=term_oy(),offset=mix_terminal_scroll_offset();
  int cx=mix_terminal_cursor_x(),cy=mix_terminal_cursor_y();
  bool cv=mix_terminal_cursor_visible();
  bool moved=cx!=last_cursor_x||cy!=last_cursor_y||cv!=last_cursor_visible;
  bool footer=term_footer_dirty||offset!=last_scroll_offset;
  /* Reserve one descriptor each for footer and a newly changed toast. Keep
   * sparse row runs separate; if they exceed the bound only the final run
   * expands across gaps. All changes still share ONE frame acknowledgement. */
  mix_present_rect_t areas[MIX_PRESENT_MAX_RECTS];
  unsigned count=0;
  bool rows=false;
  for(int r=0;r<g->rows;r++) {
   if(mix_terminal_dirty(r)||(moved&&(r==cy||r==last_cursor_y))) {
    terminal_row_draw(r);rows=true;
    int y0=oy+r*g->ch,y1=y0+g->ch;
    if(count&&(areas[count-1].y1==y0||count==MIX_PRESENT_MAX_RECTS-2))areas[count-1].y1=y1;
    else areas[count++]=(mix_present_rect_t){0,y0,W,y1};
   }
  }
  bool toast_changed=toast.active&&toast.dirty;
  if(rows||toast_changed)toast_draw();
  if(toast_changed)areas[count++]=(mix_present_rect_t){192,TOAST_Y,832,TOAST_Y+TOAST_H};
  if(footer){status_bar();areas[count++]=(mix_present_rect_t){0,STATUS_Y,W,H};}
  if(count) {
   term_ms=now;
   /* Same deferred-mirror path as settings scrolling: repeated updates copy
    * a region once, rather than immediately writing both PSRAM buffers.
    * Ordinary presents on navigation/full repaint synchronize them for fades. */
   present_deferred_batch(areas,count);
   if(last_draw_error==ESP_OK) {
    mix_terminal_clean();term_footer_dirty=false;
    if(toast_changed)toast.dirty=false;
    last_cursor_x=cx;last_cursor_y=cy;last_cursor_visible=cv;last_scroll_offset=offset;
    if(rows){term_recent=true;term_present_ms=ui_clock_ms();}
   }
  }
 }
 if(toast.active&&toast.dirty&&!launch_motion.active&&
    (content_motion.phase==CONTENT_IDLE||content_motion.phase==CONTENT_WAIT)) {
  toast_draw();present_rect(192,TOAST_Y,832,TOAST_Y+TOAST_H);toast.dirty=false;
 }
}

/* ---------- input ---------- */
static void launch(int index) {
 if(index<0||index>=4||page!=PAGE_HOME)return;
 if(index==3){navigation_settings();return;}
 bool animate=mix_ui_draw_healthy()&&!view.maintenance_busy&&!view.ota_state;
 static const uint8_t apps[4]={MIX_APP_TRANSLATE,MIX_APP_NOTES,MIX_APP_AGENT,0};
 current_app=apps[index];
 /* Only session-backed applications enqueue a host request in navigate(). */
 navigate(PAGE_APP);
 if(animate) {
  motion_stats.animations++;motion_stats.frames=0;motion_stats.draw_max_us=0;
  motion_stats.present_max_us=0;motion_stats.max_gap_ms=0;
  launch_motion.active=true;launch_motion.pending=true;
  launch_motion.card=(uint8_t)index;launch_motion.progress=0;
  /* start_ms is assigned after the first completed present, never from the
   * previous UI tick's stale timestamp. */
  launch_motion.start_ms=0;launch_motion.frame_ms=0;
 }
}
static void set_geometry(int index) {
 if(index<0||index>=GEOMETRY_COUNT||index==geometry)return;
 geometry=(uint8_t)index;
 if(!mix_terminal_resize(geometries[index].cols,geometries[index].rows)) {
  mix_ui_notice("Terminal geometry rejected");return;
 }
 last_cursor_x=last_cursor_y=-1;last_scroll_offset=-1;
 save_prefs();queue(MIX_ACTION_TERM_GEOMETRY);repaint=true;
}
static void select_network(int index) {
 const mix_net_entry_t *list=NULL;
 int count=mix_link_net_list(&list);
 if(index<0||index>=count)return;
 snprintf(net_ssid,sizeof(net_ssid),"%s",list[index].ssid);
 net_view=1;net_reveal=false;settings_scroll=0;
 memset(net_pass,0,sizeof(net_pass));net_pass_len=0;
 repaint=true;
}
static __attribute__((unused)) void touch_appearance(int x,int y) {
 for(int i=0;i<MIX_THEME_COUNT;i++) {
  int col=i%THEME_COLS,row=i/THEME_COLS;
  int chip_x=theme_chip_x0+col*(theme_chip_w+theme_chip_gap),chip_y=THEME_Y+row*(THEME_H+THEME_GAP);
  if(inside(x,y,chip_x,chip_y,theme_chip_w,THEME_H)) {
   theme=(uint8_t)i;save_prefs();repaint=true;return;
  }
 }
 if(inside(x,y,BODY_X+20,AP_LANGUAGE_Y,326,UI_BUTTON_H)){language=0;save_prefs();repaint=true;return;}
 if(inside(x,y,BODY_X+366,AP_LANGUAGE_Y,326,UI_BUTTON_H)) {
  if(!ttf_font_ready()){mix_ui_notice("Chinese needs a valid font partition");return;}
  language=1;save_prefs();repaint=true;return;
 }
 if(inside(x,y,BODY_X+20,AP_BRIGHT_Y,326,UI_BUTTON_H)){queue(MIX_ACTION_BRIGHT_DOWN);return;}
 if(inside(x,y,BODY_X+366,AP_BRIGHT_Y,326,UI_BUTTON_H)){queue(MIX_ACTION_BRIGHT_UP);return;}
 if(inside(x,y,BODY_X+20,AP_VOLUME_Y,326,UI_BUTTON_H)){queue(MIX_ACTION_VOLUME_DOWN);return;}
 if(inside(x,y,BODY_X+366,AP_VOLUME_Y,326,UI_BUTTON_H)){queue(MIX_ACTION_VOLUME_UP);return;}
 for(int i=0;i<GEOMETRY_COUNT;i++)
  if(inside(x,y,BODY_X+20+i*346,AP_GEOMETRY_Y,326,144)){set_geometry(i);return;}
 if(inside(x,y,BODY_X+20,AP_BACKLIGHT_Y,BODY_W-40,UI_BUTTON_H))queue(MIX_ACTION_KBD_BACKLIGHT);
}
static __attribute__((unused)) void touch_network(int x,int y) {
 if(net_view) {
  if(inside(x,y,BODY_X+BODY_W-220,NET_SHOW_Y,200,UI_BUTTON_H)){net_reveal=!net_reveal;repaint=true;return;}
  if(inside(x,y,BODY_X,NET_CONNECT_Y,336,UI_BUTTON_H)) {
   if(!mix_link_net_busy())queue(MIX_ACTION_NET_CONNECT);
   return;
  }
  if(inside(x,y,BODY_X+376,NET_CONNECT_Y,336,UI_BUTTON_H)){modal=MIX_ACTION_NET_FORGET;repaint=true;return;}
  if(inside(x,y,BODY_X,NET_BACK_Y,336,UI_BUTTON_H)) {
   net_view=0;settings_scroll=0;memset(net_pass,0,sizeof(net_pass));net_pass_len=0;repaint=true;
  }
  return;
 }
 if(inside(x,y,BODY_X+492,NET_SCAN_Y,200,UI_BUTTON_H)) {
  if(mix_link_net_busy())return;
  queue(MIX_ACTION_NET_SCAN);mix_ui_notice("Scanning");return;
 }
 const mix_net_entry_t *list=NULL;
 int count=mix_link_net_list(&list),rows=count-net_scroll;
 if(rows>NET_PAGE_ROWS)rows=NET_PAGE_ROWS;
 for(int i=0;i<rows;i++)
  if(inside(x,y,BODY_X,NET_LIST_Y+i*NET_ROW_STEP,BODY_W,NET_ROW_H)){select_network(net_scroll+i);return;}
 if(count>NET_PAGE_ROWS) {
  if(inside(x,y,BODY_X,NET_PAGER_Y,336,UI_BUTTON_H)){net_scroll=clamp(net_scroll-NET_PAGE_ROWS,0,count-1);settings_scroll=0;repaint=true;}
  else if(inside(x,y,BODY_X+376,NET_PAGER_Y,336,UI_BUTTON_H)){net_scroll=clamp(net_scroll+NET_PAGE_ROWS,0,count-1);settings_scroll=0;repaint=true;}
 }
}
void mix_ui_touch_cancel(void) {
 /* Communication loss is not a physical release. Cancel every possible
  * target without dispatching actions, then require a real release frame. */
 /* Keep touch_down as an unknown physical state. A later validated
  * zero-contact frame is the only event that clears this quarantine. */
 touch_block_until_up=true;
 if(locked)lock_feedback_return();
 if(unlock_ready)lock_feedback_dirty=true;
 unlock_tracking=unlock_ready=false;unlock_x=-1;
 if(pressed_card>=0||nav_pressed!=-1)repaint=true;
 pressed_card=-1;pressed_inside=false;nav_pressed=-1;
 drag_y=settings_drag_y=-1;settings_dragged=false;settings_tap_active=false;
}
void mix_ui_touch(int x,int y,bool down) {
 bool press=down&&!touch_down;
 bool release=!down&&touch_down;
 touch_down=down;tx=x;ty=y;
 if(settings_tap_active&&(!inside(x,y,0,0,W,CONTENT_H)||
    abs(x-settings_tap_x)>12||abs(y-settings_tap_y)>12))settings_tap_active=false;
 if(touch_block_until_up) {
  if(!down)touch_block_until_up=false;
  return;
 }
 if(page==PAGE_SETTINGS&&!modal&&notice_visible&&press&&
    inside(x,y,0,0,W,NOTICE_H)) {
  notice_visible=false;settings_body_dirty=true;
  touch_block_until_up=true;settings_drag_y=-1;return;
 }
 if(nav_pressed!=-1) {
  if(down&&(x<0||x>=360||y<STATUS_Y||y>=H||x/120!=nav_pressed))nav_pressed=-2;
  if(release) {
   int chosen=nav_pressed;nav_pressed=-1;
   if(chosen>=0&&x>=chosen*120&&x<(chosen+1)*120&&y>=STATUS_Y&&y<H&&
      !view.maintenance_busy&&!view.ota_state) {
    if(chosen==0)navigation_back();
    else if(chosen==1)mix_ui_home_toggle();
    /* chosen==2 is the inactive menu button. */
   }
  }
  return;
 }
 if(y>=STATUS_Y) {
  if(pressed_card>=0){pressed_card=-1;pressed_inside=false;repaint=true;}
  if(release){drag_y=settings_drag_y=-1;}
  if(press&&x>=0&&x<360&&y<H)nav_pressed=x/120;
  return;
 }
 /* Navigation remains available to cancel an in-flight launch. */
 if(mix_ui_motion_active()){if(release)drag_y=-1;return;}
 if(modal) {
  if(view.maintenance_busy||view.ota_state)return;
  if(press&&y>=454&&y<498) {
   if(x>=216&&x<488)resolve_modal(false);
   else if(x>=520&&x<808)resolve_modal(true);
  }
  return;
 }
 if(page==PAGE_SETTINGS&&!settings_menu&&section==SEC_DEVICE&&touch_test&&
    (down||release)&&inside(x,y,0,0,W,CONTENT_H))test_dirty=true;
 if(page==PAGE_SETTINGS) {
  /* Fixed settings pages accept only a tap that started in this body.
   * Crossing the slop/viewport cancels it permanently, even after re-entry.
   * Release jitter never transfers a gap or another control into a hit. */
  if(view.maintenance_busy||view.ota_state||!inside(x,y,0,0,W,CONTENT_H)){settings_tap_active=false;return;}
  if(press) {
   settings_tap_active=true;settings_tap_x=x;settings_tap_y=y;
  }
  if(down)return;
  if(!release||!settings_tap_active)return;
  settings_tap_active=false;x=settings_tap_x;y=settings_tap_y;press=true;
 }
 /* A pressed card is drawn inset and presented on its own, so the feedback is
  * immediate without repainting the page. The driver reports a failed read as
  * (0, 0, up), so the release coordinates cannot be trusted; whether the
  * finger was still on the card is decided while it is still down. */
 if(page==PAGE_HOME&&pressed_card>=0) {
  int index=pressed_card,cx,cy;
  card_rect(index,&cx,&cy);
  if(down) {
   bool still=x>=0&&x<W&&y>=0&&y<H&&inside(x,y,cx,cy,CARD_W,CARD_H);
   if(still!=pressed_inside) {
    pressed_inside=still;launcher_card(index,still);toast_draw();present(cy,cy+CARD_H);
   }
   return;
  }
  if(release) {
   pressed_card=-1;
   if(pressed_inside)launch(index);
   pressed_inside=false;
   return;
  }
 }
 if(page==PAGE_APP&&release){drag_y=-1;}
 if(x<0||x>=W||y<0||(page!=PAGE_SETTINGS&&y>=H))return;
 /* Scrolling the terminal is a drag on the terminal itself. The three buttons
  * that used to do it were 30x12 dp and could not reliably be hit; a gesture
  * has no minimum size, and dragging the content is what a touch screen
  * affords anyway. Dragging down moves towards older output, so the text
  * follows the finger. */
 if(page==PAGE_APP&&app_has_session()&&down&&y>=0&&y<CONTENT_H) {
  if(drag_y<0){drag_y=y;drag_rest=0;}
  else {
   int cell=geom()->ch,travel=y-drag_y+drag_rest,lines=travel/cell;
   if(lines){mix_terminal_scroll(lines);repaint=true;}
   drag_rest=travel-lines*cell;
   drag_y=y;
  }
  return;
 }
 if(!press)return;
 if(page==PAGE_HOME) {
  for(int i=0;i<4;i++) {
   int cx,cy;card_rect(i,&cx,&cy);
   if(inside(x,y,cx,cy,CARD_W,CARD_H)) {
    pressed_card=i;pressed_inside=true;home_focus=i;
    launcher_card(i,true);toast_draw();present(cy,cy+CARD_H);return;
   }
  }
  return;
 }
 if(page==PAGE_APP)return;
 if(settings_menu) {
  for(int i=0;i<8;i++) {
   int col=i%2,row=i/2;
   int rx=SET_TILE_X0+col*(SET_TILE_W+SET_TILE_GAP_X);
   int ry=SET_TILE_Y0+row*(SET_TILE_H+SET_TILE_GAP_Y);
   if(inside(x,y,rx,ry,SET_TILE_W,SET_TILE_H)){go_section(settings_category(i));return;}
  }
  return;
 }
 switch(section) {
 case SEC_APPEARANCE:
  for(int i=0;i<MIX_THEME_COUNT;i++) {
   int col=i%SET_THEME_COLS,row=i/SET_THEME_COLS;
   int rx=SET_THEME_X0+col*(SET_THEME_W+SET_THEME_GAP_X),ry=SET_THEME_Y0+row*(SET_THEME_H+SET_THEME_GAP_Y);
   if(inside(x,y,rx,ry,SET_THEME_W,SET_THEME_H)){theme=(uint8_t)i;save_prefs();repaint=true;return;}
  }
  break;
 case SEC_DISPLAY:
  if(inside(x,y,64,174,300,UI_BUTTON_H)){queue(MIX_ACTION_BRIGHT_DOWN);return;}
  if(inside(x,y,388,174,300,UI_BUTTON_H)){queue(MIX_ACTION_BRIGHT_UP);return;}
  if(inside(x,y,64,382,300,UI_BUTTON_H)){language=0;save_prefs();repaint=true;return;}
  if(inside(x,y,388,382,300,UI_BUTTON_H)){
   if(!ttf_font_ready()){mix_ui_notice("Chinese needs a valid font partition");return;}
   language=1;save_prefs();repaint=true;return;
  }
  break;
 case SEC_INPUT:
  if(inside(x,y,64,164,300,UI_BUTTON_H)){queue(MIX_ACTION_VOLUME_DOWN);return;}
  if(inside(x,y,388,164,300,UI_BUTTON_H)){queue(MIX_ACTION_VOLUME_UP);return;}
  if(inside(x,y,64,368,624,UI_BUTTON_H)){queue(MIX_ACTION_KBD_BACKLIGHT);return;}
  if(inside(x,y,64,510,300,UI_BUTTON_H)){set_geometry(0);return;}
  if(inside(x,y,388,510,300,UI_BUTTON_H)){set_geometry(1);return;}
  break;
 case SEC_NETWORK:
  if(net_view){
   if(inside(x,y,800,300,160,UI_BUTTON_H)){net_reveal=!net_reveal;repaint=true;return;}
   if(inside(x,y,64,434,300,UI_BUTTON_H)){if(!mix_link_net_busy())queue(MIX_ACTION_NET_CONNECT);return;}
   if(inside(x,y,388,434,300,UI_BUTTON_H)){modal=MIX_ACTION_NET_FORGET;repaint=true;return;}
  } else {
   if(inside(x,y,716,88,240,UI_BUTTON_H)){if(!mix_link_net_busy())queue(MIX_ACTION_NET_SCAN);return;}
   const mix_net_entry_t *list=NULL;int count=mix_link_net_list(&list);
   int limit=network_page_limit(count);net_scroll=clamp(net_scroll,0,limit);
   int rows=count-net_scroll;if(rows>SET_NETWORK_ROWS)rows=SET_NETWORK_ROWS;if(rows<0)rows=0;
   for(int i=0;i<rows;i++)if(inside(x,y,SET_PAGE_X,212+i*84,SET_PAGE_W,68)){select_network(net_scroll+i);return;}
   if(inside(x,y,64,486,300,UI_BUTTON_H)){net_scroll=clamp(net_scroll-SET_NETWORK_ROWS,0,limit);repaint=true;return;}
   if(inside(x,y,388,486,300,UI_BUTTON_H)){net_scroll=clamp(net_scroll+SET_NETWORK_ROWS,0,limit);repaint=true;return;}
  }
  break;
 case SEC_DEVICE:
  if(inside(x,y,64,486,300,UI_BUTTON_H)){touch_test=!touch_test;test_dirty=true;repaint=true;return;}
  if(inside(x,y,388,486,300,UI_BUTTON_H)){modal=MIX_ACTION_INPUT_RESET;repaint=true;return;}
  break;
 case SEC_SYSTEM:
  if(inside(x,y,64,500,300,UI_BUTTON_H)){modal=view.job_running?MIX_ACTION_JOB_CANCEL:MIX_ACTION_JOB_START;repaint=true;return;}
  if(inside(x,y,388,500,300,UI_BUTTON_H)){current_app=MIX_APP_SHELL;navigate(PAGE_APP);return;}
  break;
 default:break;
 }

}
/* Passphrase editing. Only printable ASCII is accepted: a Wi-Fi passphrase is
 * defined over exactly that range, so anything else is a keyboard artefact. */
static void passphrase_key(const uint8_t *bytes,size_t len) {
 if(len==1&&(bytes[0]=='\r'||bytes[0]=='\n')) {
  if(!mix_link_net_busy())queue(MIX_ACTION_NET_CONNECT);
  return;
 }
 if(len==1&&bytes[0]==27) {navigation_back();return;}
 if(len==1&&(bytes[0]==0x7f||bytes[0]=='\b')) {
  if(net_pass_len){net_pass[--net_pass_len]=0;repaint=true;}
  return;
 }
 bool changed=false;
 for(size_t i=0;i<len;i++) {
  uint8_t b=bytes[i];
  if(b<0x20||b>0x7e)continue;
  if(net_pass_len>=sizeof(net_pass)-1){mix_ui_notice("Passphrase is full");break;}
  net_pass[net_pass_len++]=(char)b;net_pass[net_pass_len]=0;changed=true;
 }
 if(changed)repaint=true;
}
void mix_ui_key(const uint8_t *bytes,size_t len) {
 if(!bytes||!len||view.maintenance_busy||view.ota_state)return;
 if(modal) {
  if(len==1&&(bytes[0]=='\r'||bytes[0]=='\n'))resolve_modal(true);
  else if(len==1&&bytes[0]==27)resolve_modal(false);
  return;
 }
 if(mix_ui_motion_active()) {
  if(len==1&&bytes[0]==27)navigate(PAGE_HOME);
  return;
 }
 /* Remote input belongs to the parent. No terminal escape sequence reaches
  * any of the branches below. */
 if(page==PAGE_SETTINGS&&section==SEC_NETWORK&&net_view){passphrase_key(bytes,len);return;}
 if(page==PAGE_HOME) {
  if(len==1&&bytes[0]>='1'&&bytes[0]<='4'){home_focus=bytes[0]-'1';launch(home_focus);return;}
  if(len==1&&(bytes[0]=='\r'||bytes[0]=='\n')){launch(home_focus);return;}
  if(len==3&&bytes[0]==27&&bytes[1]=='[') {
   int previous=home_focus;
   if(bytes[2]=='C')home_focus=(home_focus+1)%4;
   else if(bytes[2]=='D')home_focus=(home_focus+3)%4;
   else if(bytes[2]=='B')home_focus=(home_focus+2)%4;
   else if(bytes[2]=='A')home_focus=(home_focus+2)%4;
   else return;
   if(previous!=home_focus) {
    int cx,cy;
    card_rect(previous,&cx,&cy);launcher_card(previous,false);toast_draw();present(cy,cy+CARD_H);
    card_rect(home_focus,&cx,&cy);launcher_card(home_focus,false);toast_draw();present(cy,cy+CARD_H);
   }
   return;
  }
  return;
 }
 if(page==PAGE_SETTINGS&&settings_menu&&len==1&&bytes[0]>='1'&&bytes[0]<='8')go_section(settings_category(bytes[0]-'1'));
 if(page==PAGE_SETTINGS&&len==1&&bytes[0]==27){navigation_back();return;}
 if(page==PAGE_APP&&!app_has_session()&&len==1&&bytes[0]==27)navigate(PAGE_HOME);
}
static void navigation_back(void) {
 if(locked||view.maintenance_busy||view.ota_state)return;
 if(modal){resolve_modal(false);return;}
 if(page==PAGE_SETTINGS&&net_view) {
  net_view=0;settings_tap_active=false;
  memset(net_pass,0,sizeof(net_pass));net_pass_len=0;net_reveal=false;repaint=true;return;
 }
 if(page==PAGE_SETTINGS&&!settings_menu) {settings_menu_open();return;}
 if(page==PAGE_SETTINGS&&settings_menu) {navigate(PAGE_HOME);return;}
 if(page==PAGE_APP&&app_has_session()) {
  /* The app handles one level. At its root it exits normally; the matching
   * host EXIT above then returns to the desktop. Never parse screen text. */
  if(mix_ui_terminal_visible()&&view.linux_online&&view.terminal_open&&view.running_app==current_app)
   queue_value(MIX_ACTION_APP_BACK,current_app);
  else if(!mix_ui_motion_active())navigate(PAGE_HOME);
  return;
 }
 navigate(PAGE_HOME);
}
static void navigation_settings(void) {
 if(locked||view.maintenance_busy||view.ota_state)return;
 if(modal)resolve_modal(false);
 navigate(PAGE_SETTINGS);
}
void mix_ui_home_toggle(void) {
 if(locked||view.maintenance_busy||view.ota_state)return;
 if(modal)resolve_modal(false);
 navigate(PAGE_HOME);
}
void mix_ui_volume(int percent) {
 volume_percent=clamp(percent,0,100);
 if(page==PAGE_SETTINGS&&section==SEC_INPUT&&!settings_menu&&!modal)settings_body_dirty=true;
}
/* Notices are firmware-authored UTF-8, not terminal data. Whole code points are
 * preserved so Chinese messages render; controls and malformed bytes become
 * spaces, and the buffer is never truncated in the middle of a sequence. */
void mix_ui_notice(const char *utf8) {
 if(!utf8)return;
 size_t out=0;
 for(size_t i=0;utf8[i];) {
  uint8_t b=(uint8_t)utf8[i];
  size_t len=b<0x80?1:(b>=0xc2&&b<=0xdf)?2:(b>=0xe0&&b<=0xef)?3:(b>=0xf0&&b<=0xf4)?4:0;
  for(size_t k=1;k<len;k++) if(((uint8_t)utf8[i+k]&0xc0)!=0x80) {len=0;break;}
  if(!len||(len==1&&(b<32||b==127))) {
   if(out+1>=sizeof(notice))break;
   notice[out++]=' ';i++;continue;
  }
  if(out+len>=sizeof(notice))break;
  memcpy(notice+out,utf8+i,len);out+=len;i+=len;
 }
 notice[out]=0;
 notice_visible=out!=0;notice_at=ui_clock_ms();
 if(page==PAGE_SETTINGS&&!modal)settings_body_dirty=true;
}
bool mix_ui_take_action(mix_action_t *out) {
 if(!out||action_read==action_write)return false;
 *out=actions[action_read];action_read=(action_read+1)%8;
 /* The passphrase has to survive exactly one round trip: the parent reads it
  * through mix_ui_net_passphrase() after this call returns, so it is marked
  * for erasure here and wiped at the start of the next tick. */
 if(out->kind==MIX_ACTION_NET_CONNECT||out->kind==MIX_ACTION_NET_FORGET)net_pass_expire=true;
 return true;
}
bool mix_ui_terminal_visible(void) {
 return page==PAGE_APP&&!modal&&!mix_ui_motion_active()&&content_motion.waiting==false&&app_has_session();
}
const char *mix_ui_net_ssid(void){return net_ssid;}
const char *mix_ui_net_passphrase(void){return net_pass;}


