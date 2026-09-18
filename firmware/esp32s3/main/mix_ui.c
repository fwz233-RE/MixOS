/* SPDX-License-Identifier: MIT
 * MixOS local interface: a status bar, a four-button launcher, a full-screen
 * application view and one settings page that carries every diagnostic the
 * device can actually measure.
 *
 * Drawing rules that keep this affordable on a 28 Hz RGB panel:
 *  - Exactly one 1.5 MiB PSRAM framebuffer. The panel's scanout buffer is the
 *    parent's and is never taken over.
 *  - A page change is presented in one go. It used to be revealed as a
 *    top-to-bottom wipe in six bands, one per tick. On a 28 Hz panel that does
 *    not read as an animation: the page arrives in visible horizontal chunks,
 *    which looks like a tearing or vsync fault rather than a transition. A
 *    single full present is both cheaper and honest.
 *  - A touched card repaints and presents only its own rectangle.
 */
#include "mix_ui.h"
#include "mix_link.h"
#include "mix_md3.h"
#include "mix_terminal.h"
#include "ttf_font.h"
#include "batt_log.h"
#include "esp_app_desc.h"
#include "esp_heap_caps.h"
#include "nvs.h"
#include <math.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#define W 1024
#define H 768
#define STATUS_H 48
#define RGB(r,g,b) ((uint16_t)((((r)>>3)<<11)|(((g)>>2)<<5)|((b)>>3)))
_Static_assert(W * H * sizeof(uint16_t) == 1572864, "UI framebuffer must be 1.5 MiB");

typedef enum { PAGE_HOME, PAGE_APP, PAGE_SETTINGS } page_t;
typedef enum { SEC_APPEARANCE, SEC_NETWORK, SEC_POWER, SEC_DEVICE, SEC_SYSTEM, SEC_COUNT } section_t;
typedef struct { uint16_t bg, card, raised, text, muted, accent, warning; } palette_t;
static const palette_t palettes[4] = {
 {RGB(19,23,26),RGB(29,35,39),RGB(43,51,56),RGB(240,245,243),RGB(163,178,176),RGB(135,235,194),RGB(244,190,111)},
 {RGB(241,244,240),RGB(255,255,251),RGB(222,231,221),RGB(29,46,38),RGB(78,101,90),RGB(24,116,81),RGB(151,80,14)},
 {RGB(17,25,42),RGB(26,38,59),RGB(39,55,80),RGB(235,242,255),RGB(165,187,215),RGB(136,193,255),RGB(255,202,132)},
 {RGB(29,24,22),RGB(42,34,29),RGB(60,48,36),RGB(255,242,221),RGB(198,176,146),RGB(244,188,106),RGB(255,154,128)}
};
#define P (palettes[theme])

/* Both presets fit inside the parser's 80x28 buffer, so switching costs no
 * extra memory. The 3.2 inch panel is why the larger cell exists at all. */
typedef struct { uint8_t cols, rows, cw, ch, font; } geometry_t;
static const geometry_t geometries[2] = {
 {80, 28, 12, 24, 20},   /* compact: 960x672 */
 {64, 22, 16, 32, 26},   /* large:   1024x704 */
};
#define GEOMETRY_COUNT 2

static esp_lcd_panel_handle_t panel;
static uint16_t *fb;
static bool first_frame_presented;
static esp_err_t last_draw_error=ESP_ERR_INVALID_STATE;
static mix_view_t view;
static page_t page;
static section_t section;
static uint8_t theme, language, geometry;
static int volume_percent = -1;
static bool repaint = true, have_view, touch_down;
static int tx, ty;
static int pressed_card = -1, home_focus;
static bool pressed_inside;
/* Terminal scroll drag: the y the last movement was accounted at, and the
 * sub-row remainder carried forward so a slow drag still moves the view. -1
 * means no drag is in progress. */
static int drag_y = -1, drag_rest;
/* The launcher decides which application the view is showing. The link's own
 * session state answers whether it is running, which is a different question. */
static uint8_t current_app = MIX_APP_SHELL;
static bool touch_test, test_dirty;
static uint32_t view_ms, term_ms, touch_ms, spin_ms;
static uint8_t spin_phase;
static int last_cursor_x = -1, last_cursor_y = -1, last_scroll_offset = -1;
static bool last_cursor_visible;
static int modal; /* 0 none, otherwise mix_action_kind_t; only local input accepts */
static mix_action_t actions[8];
static unsigned action_read, action_write;
static char notice[96];
static batt_sample_t history[BATT_LOG_CAP]; /* 4.3 KiB RAM snapshot, never samples */
/* Network sub-view state. The passphrase never leaves this buffer except as a
 * single NET_CONNECT action, and is wiped on the tick after that action is
 * taken, once the parent has had its chance to read it. */
static int net_view, net_scroll;
static bool net_reveal, net_pass_expire;
static char net_ssid[MIX_SSID_MAX + 1];
static char net_pass[64];
static uint8_t net_pass_len;

static const char *tr(const char *en, const char *zh) { return language ? zh : en; }
static int clamp(int a, int lo, int hi) { return a < lo ? lo : a > hi ? hi : a; }
static bool inside(int x,int y,int rx,int ry,int rw,int rh){return x>=rx&&x<rx+rw&&y>=ry&&y<ry+rh;}
static int ratio_width(float value,int width){if(!isfinite(value)||value<=0)return 0;if(value>=1)return width;return (int)(value*width);}
static const geometry_t *geom(void){return &geometries[geometry<GEOMETRY_COUNT?geometry:0];}
static int term_ox(void){return (W-geom()->cols*geom()->cw)/2;}
static int term_oy(void){return STATUS_H+(H-STATUS_H-geom()->rows*geom()->ch)/2;}

/* ---------- primitives ---------- */
static void rect(int x,int y,int w,int h,uint16_t c) {
 int right=clamp(x+w,0,W), bottom=clamp(y+h,0,H);
 for(int j=clamp(y,0,H);j<bottom;j++) for(int i=clamp(x,0,W);i<right;i++) fb[j*W+i]=c;
}
static void roundbox(int x,int y,int w,int h,int r,uint16_t c) {
 if(w<=0||h<=0)return;
 if(r*2>w)r=w/2;
 if(r*2>h)r=h/2;
 rect(x+r,y,w-2*r,h,c); rect(x,y+r,w,h-2*r,c);
 for(int dy=0;dy<r;dy++) for(int dx=0;dx<r;dx++)
  if((r-dx)*(r-dx)+(r-dy)*(r-dy)<=r*r) {
   rect(x+dx,y+dy,1,1,c); rect(x+w-1-dx,y+dy,1,1,c);
   rect(x+dx,y+h-1-dy,1,1,c); rect(x+w-1-dx,y+h-1-dy,1,1,c);
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
static void text(int x,int y,int size,uint16_t color,const char *s) {
 if(!s)return;
 if(ttf_font_ready()) {ttf_draw_text(fb,W,H,x,y,size,color,s);return;}
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
static void text_right(int right,int y,int size,uint16_t color,const char *s){text(right-text_width(size,s),y,size,color,s);}
static void text_mid(int cx,int y,int size,uint16_t color,const char *s){text(cx-text_width(size,s)/2,y,size,color,s);}

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
#define ICON_SETTINGS       "\ue8b8"  /* settings */
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
/* Draws `glyph` centred on (cx,cy). Silently draws nothing when the font
 * partition is missing, which is the same degradation the text path takes. */
static void icon(int cx,int cy,int size,uint16_t color,const char *glyph) {
 if(!ttf_font_ready())return;
 uint32_t cp=icon_codepoint(glyph);
 ttf_glyph_t g;
 if(!cp||!ttf_font_glyph(cp,size,&g)||!g.bitmap||g.w<=0||g.h<=0)return;
 int x0=cx-g.w/2,y0=cy-g.h/2;
 for(int row=0;row<g.h;row++) {
  int fy=y0+row;
  if(fy<0||fy>=H)continue;
  const uint8_t *src=g.bitmap+(size_t)row*g.w;
  uint16_t *dst=fb+(size_t)fy*W;
  for(int col=0;col<g.w;col++) {
   int fx=x0+col;
   if(fx<0||fx>=W)continue;
   uint8_t a=src[col];
   if(a)dst[fx]=blend565(dst[fx],color,a);
  }
 }
}

static void label(int x,int y,const char *en,const char *zh) {text(x,y,20,P.muted,tr(en,zh));}
static void button(int x,int y,int w,const char *en,const char *zh,bool active) {
 roundbox(x,y,w,44,10,active?P.accent:P.raised);
 const char *s=tr(en,zh);
 text_mid(x+w/2,y+10,20,active?P.bg:P.text,s);
}
static void line(int x0,int y0,int x1,int y1,uint16_t c) {
 int dx=abs(x1-x0),sx=x0<x1?1:-1,dy=-abs(y1-y0),sy=y0<y1?1:-1,err=dx+dy;
 for(;;) {rect(x0,y0,2,2,c);if(x0==x1&&y0==y1)break;int e=2*err;if(e>=dy){err+=dy;x0+=sx;}if(e<=dx){err+=dx;y0+=sy;}}
}
static void present(int y0,int y1) {
 y0=clamp(y0,0,H);y1=clamp(y1,0,H);
 if(y1<=y0)return;
 esp_err_t e=esp_lcd_panel_draw_bitmap(panel,0,y0,W,y1,fb+(size_t)y0*W);
 if(e!=ESP_OK) {
  last_draw_error=e;repaint=true; /* Retry the whole frame on the next tick. */
 } else if(y0==0&&y1==H) {
  first_frame_presented=true;last_draw_error=ESP_OK;
 }
}
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
 if(page==PAGE_APP&&view.terminal_open)queue(MIX_ACTION_TERMINAL_CLOSE);
 page=p;repaint=true;pressed_card=-1;pressed_inside=false;
 /* Entering an application opens its session outright. There used to be a
  * floating button to open and close it by hand, which meant an application
  * could be on screen doing nothing until the user found and pressed it. The
  * page being open is the intent; the Agent placeholder has no session. */
 if(p==PAGE_APP) {
  mix_terminal_invalidate();
  if(current_app!=MIX_APP_AGENT)queue_value(MIX_ACTION_APP_OPEN,current_app);
 }
}
static void go_section(section_t s) {
 if(section==s&&net_view==0)return;
 section=s;net_view=0;net_scroll=0;
 memset(net_pass,0,sizeof(net_pass));net_pass_len=0;net_reveal=false;
 repaint=true;
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
 uint32_t local=view.host_time_s+(uint32_t)((int32_t)view.host_tz_offset_min*60);
 snprintf(out,cap,"%02u:%02u",(unsigned)((local/3600)%24),(unsigned)((local/60)%60));
}
static const char *app_title_en(void) {
 switch(current_app) {
 case MIX_APP_TRANSLATE: return "Live translation";
 case MIX_APP_NOTES: return "Notes";
 case MIX_APP_AGENT: return "Agent";
 default: return "Terminal";
 }
}
static const char *app_title_zh(void) {
 switch(current_app) {
 case MIX_APP_TRANSLATE: return "实时翻译";
 case MIX_APP_NOTES: return "记笔记";
 case MIX_APP_AGENT: return "Agent";
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
 rect(0,0,W,STATUS_H,P.card);
 const char *title_en="MixOS",*title_zh="MixOS";
 if(page==PAGE_APP){title_en=app_title_en();title_zh=app_title_zh();}
 else if(page==PAGE_SETTINGS){title_en="Settings";title_zh="设置";}
 text(16,12,22,P.text,tr(title_en,title_zh));
 /* Which build this is, next to the title on every page.
  *
  * The ELF hash rather than the build date. ESP-IDF only regenerates the
  * descriptor's date and time when esp_app_desc.c is recompiled, so two builds
  * made minutes apart routinely carry the same timestamp - this very display
  * read "Sep 14 2026 20:42:03" for two images with different contents, which
  * made it useless for the one question it was added to answer. app_elf_sha256
  * is patched in after every link, so it is the only part of the descriptor
  * guaranteed to differ whenever the binary differs. tools/ota_esp.py prints
  * the same eight characters for the image it is about to install.
  *
  * This is needed because nothing else on this board can identify the running
  * build: the ESP32 log goes to UART0, which this hardware does not bring out.
  */
 const esp_app_desc_t *built=esp_app_get_description();
 if(built){
  snprintf(b,sizeof(b),"build %02x%02x%02x%02x",
           built->app_elf_sha256[0],built->app_elf_sha256[1],
           built->app_elf_sha256[2],built->app_elf_sha256[3]);
  text(150,16,18,P.accent,b);
 }
 /* The clock is centred on the panel, and the radio and the battery sit at the
  * right edge with the battery outermost. Each is either a real reading or an
  * explicit unknown; none of them is ever filled in with a guess.
  *
  * The battery percentage is right-aligned to the edge while its icon keeps a
  * fixed centre, so the icon does not shift between "--" and "100%". At size 20
  * the widest reading clears the icon, and the left-hand block ends well before
  * the centred clock starts. */
 clock_string(b,sizeof(b));
 text_mid(W/2,13,20,view.host_time_s?P.text:P.muted,b);
 int bars=view.wifi_reported&&view.wifi_connected&&view.wifi_signal>=0?1+view.wifi_signal*3/100:0;
 wifi_icon(890,STATUS_H/2,bars,!view.wifi_reported,view.wifi_connected,28);
 battery_icon(940,STATUS_H/2,28);
 if(view.soc_valid&&isfinite(view.soc))snprintf(b,sizeof(b),"%d%%",(int)view.soc);
 else snprintf(b,sizeof(b),"--");
 text_right(1008,13,20,view.soc_valid?P.text:P.muted,b);
 if(!view.linux_online)text(300,14,18,P.warning,tr("Linux offline","Linux 离线"));
 if(page==PAGE_APP) {
  int offset=mix_terminal_scroll_offset();
  /* Just the offset. A "swipe down to return" hint belongs here, but the font
   * is a subset built from this file and lives in its own partition that an
   * OTA does not rewrite, so introducing a character this device has never
   * rendered would show as blank until the next serial flash. */
  if(offset){snprintf(b,sizeof(b),"-%d",offset);text(404,16,16,P.warning,b);}
 }
}

/* ---------- launcher ---------- */
#define CARD_W 468
#define CARD_H 316
static void card_rect(int index,int *x,int *y) {
 *x=32+(index%2)*(CARD_W+24);
 *y=80+(index/2)*(CARD_H+24);
}
/* The four launcher glyphs, in card order. They replace a set of hand-drawn
 * shapes that had to cut holes in themselves to suggest depth, and so needed
 * to know the colour of the surface underneath; a real glyph is an alpha mask
 * and simply composites onto whatever is already there. */
static const char *const app_icons[4]={ICON_TRANSLATE,ICON_NOTES,ICON_AGENT,ICON_SETTINGS};
static void launcher_card(int index,bool pressed) {
 int x,y;card_rect(index,&x,&y);
 static const char *title_en[4]={"Live translation","Notes","Agent","Settings"};
 static const char *title_zh[4]={"实时翻译","记笔记","Agent","设置"};
 static const char *sub_en[4]={"Speak, hear it back","Write and dictate","Reserved","Device and network"};
 static const char *sub_zh[4]={"说出来，听译文","书写与语音听写","预留","设备与网络"};
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
  * checked against CARD_H by hand: 316 px is 126 dp, and the container, title,
  * subtitle and state line have to fit inside it without touching. */
 uint16_t holder=pressed?P.card:P.raised;
 int cs=MIX_DP(48);                        /* 120 px icon container */
 int cx=x+MIX_SPACE_4+inset,cy=y+MIX_SPACE_2+inset;
 roundbox(cx,cy,cs,cs,MIX_RADIUS_L,holder);
 /* 70 px glyph in a 120 px container, centred: the Material 3 ratio for an
  * icon inside a tonal holder. */
 icon(cx+cs/2,cy+cs/2,MIX_DP(28),P.accent,app_icons[index]);
 int tx0=x+MIX_SPACE_4+inset;
 text(tx0,y+MIX_DP(64)+inset,MIX_SP(20),P.text,tr(title_en[index],title_zh[index]));
 text(tx0,y+MIX_DP(89)+inset,MIX_SP(13),P.muted,tr(sub_en[index],sub_zh[index]));
 /* A card says what it will actually do right now, so nothing looks ready
  * when it is not. A session belongs to one application, so "running" is
  * claimed only by the card whose application the host actually opened. */
 static const uint8_t card_app[2]={MIX_APP_TRANSLATE,MIX_APP_NOTES};
 const char *state_en=NULL,*state_zh=NULL;
 uint16_t state_colour=P.muted;
 if(index==2){state_en="Placeholder: no backend yet";state_zh="占位：暂无后端";}
 else if(index<2&&!view.linux_online){state_en="Linux offline";state_zh="Linux 离线";state_colour=P.warning;}
 else if(index<2&&view.terminal_open&&view.running_app==card_app[index]) {
  state_en="Session running";state_zh="会话运行中";state_colour=P.accent;
 }
 if(state_en)text(tx0,y+MIX_DP(108)+inset,MIX_SP(11),state_colour,tr(state_en,state_zh));
 /* Layout check, in pixels, against CARD_H = 316:
  *   container  20..140    title 160..210    subtitle 223..255    state 270..298
  * The last line ends 18 px above the card edge. Any change to the four dp
  * constants above has to be re-checked here. */
 _Static_assert(MIX_DP(108) + MIX_SP(11) < CARD_H, "launcher card content overflows the card");
}
static void home_draw(void) {
 rect(0,STATUS_H,W,H-STATUS_H,P.bg);
 for(int i=0;i<4;i++)launcher_card(i,pressed_card==i);
}

/* ---------- settings: shared chrome ---------- */
#define RAIL_X 32
#define RAIL_W 224
#define BODY_X 280
#define BODY_W 712
static void section_rail(void) {
 static const char *en[SEC_COUNT]={"Appearance","Network","Power","Device","System"};
 static const char *zh[SEC_COUNT]={"外观","网络","电源","设备","系统"};
 static const char *const glyphs[SEC_COUNT]={ICON_APPEARANCE,ICON_NETWORK,ICON_POWER,ICON_DEVICE,ICON_SYSTEM};
 for(int i=0;i<SEC_COUNT;i++) {
  int y=72+i*76;
  bool on=section==(section_t)i;
  uint16_t fg=on?P.bg:P.text;
  roundbox(RAIL_X,y,RAIL_W,60,14,on?P.accent:P.card);
  icon(RAIL_X+30,y+30,26,fg,glyphs[i]);
  text(RAIL_X+58,y+18,22,fg,tr(en[i],zh[i]));
 }
}
static void metric(int x,int y,int width,const char *en,const char *zh,bool valid,float value,const char *unit) {
 char b[64];roundbox(x,y,width,104,16,P.card);text(x+18,y+14,18,P.muted,tr(en,zh));
 if(valid&&isfinite(value))snprintf(b,sizeof(b),"%.2f %s",(double)value,unit);
 else snprintf(b,sizeof(b),"%s",tr("Unavailable","不可用"));
 text(x+18,y+46,28,valid?P.text:P.muted,b);
}

/* ---------- settings: appearance ---------- */
static void appearance_draw(void) {
 char b[64];
 static const char *names[]={"Graphite","Paper","Midnight","Ember"};
 static const char *zh[]={"石墨薄荷","纸白森林","午夜蓝","暖琥珀"};
 roundbox(BODY_X,64,BODY_W,196,16,P.card);
 text(BODY_X+20,80,18,P.muted,tr("THEME AND LANGUAGE","主题与语言"));
 for(int i=0;i<4;i++)button(BODY_X+20+(i%2)*344,112+(i/2)*56,332,names[i],zh[i],theme==i);
 button(BODY_X+20,224,332,"English","English",!language);
 button(BODY_X+364,224,332,"中文","中文",language);
 roundbox(BODY_X,276,BODY_W,168,16,P.card);
 text(BODY_X+20,292,18,P.muted,tr("DISPLAY, KEYBOARD AND SOUND","屏幕、键盘与声音"));
 snprintf(b,sizeof(b),"%s %d%%",tr("Brightness","亮度"),view.brightness*10);
 text(BODY_X+20,322,22,P.text,b);
 button(BODY_X+300,318,180,"Dimmer","调暗",false);
 button(BODY_X+492,318,180,"Brighter","调亮",false);
 if(volume_percent>=0)snprintf(b,sizeof(b),"%s %d%%",tr("Volume","音量"),volume_percent);
 else snprintf(b,sizeof(b),"%s",tr("Volume","音量"));
 text(BODY_X+20,382,22,P.text,b);
 button(BODY_X+300,378,180,"Quieter","调小",false);
 button(BODY_X+492,378,180,"Louder","调大",false);
 roundbox(BODY_X,460,BODY_W,168,16,P.card);
 text(BODY_X+20,476,18,P.muted,tr("TERMINAL CELL SIZE","终端字符格"));
 for(int i=0;i<GEOMETRY_COUNT;i++) {
  snprintf(b,sizeof(b),"%dx%d",geometries[i].cols,geometries[i].rows);
  roundbox(BODY_X+20+i*344,506,332,58,12,geometry==i?P.accent:P.raised);
  text(BODY_X+40+i*344,514,24,geometry==i?P.bg:P.text,b);
  text_right(BODY_X+332+i*344,520,17,geometry==i?P.bg:P.muted,
             tr(i?"large":"compact",i?"大字":"紧凑"));
 }
 text(BODY_X+20,580,17,P.muted,
      tr("The panel is 3.2 inches; large cells are the readable default.",
         "屏幕只有 3.2 英寸，大字档更易读。"));
 button(BODY_X+20,652,332,"Keyboard backlight","键盘背光",false);
}

/* ---------- settings: network ---------- */
static void network_list_draw(void) {
 char b[96];
 roundbox(BODY_X,64,BODY_W,92,16,P.card);
 if(!view.linux_online)
  text(BODY_X+20,84,22,P.warning,tr("Linux offline: no radio to ask","Linux 离线：无法查询无线"));
 else if(!view.wifi_reported)
  text(BODY_X+20,84,22,P.muted,tr("Wi-Fi state unknown","无线状态未知"));
 else if(view.wifi_connected) {
  snprintf(b,sizeof(b),"%s  %s",tr("Connected","已连接"),view.wifi_ssid);
  text(BODY_X+20,78,22,P.accent,b);
  if(view.host_ip[0])text(BODY_X+20,108,17,P.muted,view.host_ip);
 } else text(BODY_X+20,84,22,P.muted,tr("Not connected","未连接"));
 bool busy=mix_link_net_busy();
 button(BODY_X+512,88,180,busy?"Scanning...":"Scan",busy?"扫描中…":"扫描",busy);
 const mix_net_entry_t *list=NULL;
 int count=mix_link_net_list(&list);
 const char *message=mix_link_net_message();
 if(!count) {
  roundbox(BODY_X,172,BODY_W,120,16,P.card);
  text(BODY_X+20,208,20,P.muted,
       busy?tr("Asking the host to scan","正在请求主机扫描"):
            tr("No scan results yet","尚无扫描结果"));
 }
 int rows=count-net_scroll;
 if(rows>9)rows=9;
 for(int i=0;i<rows;i++) {
  const mix_net_entry_t *e=&list[net_scroll+i];
  int y=172+i*58;
  bool current=view.wifi_connected&&!strcmp(e->ssid,view.wifi_ssid);
  roundbox(BODY_X,y,BODY_W,50,12,current?P.raised:P.card);
  text(BODY_X+16,y+12,21,P.text,e->ssid);
  int bars=1+e->signal*3/100;
  for(int k=0;k<4;k++)rect(BODY_X+BODY_W-40+k*7,y+30-(6+k*4),5,6+k*4,k<bars?P.accent:P.raised);
  if(e->secured)text_right(BODY_X+BODY_W-56,y+16,16,P.muted,tr("locked","加密"));
  if(e->known)text_right(BODY_X+BODY_W-116,y+16,16,P.accent,tr("saved","已保存"));
 }
 if(count>9) {
  button(BODY_X,700,220,"Earlier","上一页",false);
  button(BODY_X+240,700,220,"Later","下一页",false);
 }
 if(message&&message[0])text(BODY_X,668,17,P.muted,message);
}
static void network_connect_draw(void) {
 char b[96];
 roundbox(BODY_X,64,BODY_W,132,16,P.card);
 text(BODY_X+20,80,18,P.muted,tr("NETWORK","网络"));
 text(BODY_X+20,110,28,P.text,net_ssid);
 roundbox(BODY_X,216,BODY_W,196,16,P.card);
 text(BODY_X+20,232,18,P.muted,tr("PASSPHRASE","密码"));
 roundbox(BODY_X+20,264,BODY_W-40,56,10,P.raised);
 if(net_pass_len) {
  if(net_reveal)text(BODY_X+36,276,24,P.text,net_pass);
  else {
   /* U+2022 written as its UTF-8 bytes: the meaning must not depend on the
    * encoding this source file happens to be saved in. */
   static const char bullet[3]={'\xe2','\x80','\xa2'};
   char dots[3*sizeof(net_pass)+1];size_t at=0;
   for(int i=0;i<net_pass_len&&at+3<sizeof(dots);i++){memcpy(dots+at,bullet,3);at+=3;}
   dots[at]=0;text(BODY_X+36,272,24,P.text,dots);
  }
 } else text(BODY_X+36,276,20,P.muted,tr("Type on the keyboard","用键盘输入"));
 button(BODY_X+BODY_W-180,336,160,net_reveal?"Hide":"Show",net_reveal?"隐藏":"显示",net_reveal);
 snprintf(b,sizeof(b),"%d / 63",net_pass_len);
 text(BODY_X+20,348,18,P.muted,b);
 text(BODY_X+20,432,17,P.muted,
      tr("Enter connects. Escape goes back. The passphrase is sent once and cleared.",
         "回车连接，Escape 返回。密码只发送一次并随即清除。"));
 bool busy=mix_link_net_busy();
 button(BODY_X,480,340,busy?"Working...":"Connect",busy?"处理中…":"连接",!busy);
 button(BODY_X+372,480,340,"Forget this network","忘记此网络",false);
 button(BODY_X,552,340,"Back","返回",false);
 const char *message=mix_link_net_message();
 if(message&&message[0])text(BODY_X,624,18,P.warning,message);
}
static void network_draw(void){if(net_view)network_connect_draw();else network_list_draw();}

/* ---------- settings: power ---------- */
static void graph_draw(void) {
 roundbox(BODY_X,392,BODY_W,240,16,P.card);
 text(BODY_X+20,408,18,P.muted,tr("LAST HOUR / voltage, current, charge","最近一小时 / 电压、电流、电量"));
 int n=batt_log_get(history,BATT_LOG_CAP);
 for(int k=0;k<4;k++)rect(BODY_X+24,450+k*40,BODY_W-48,1,P.raised);
 if(n<2) {
  text(BODY_X+40,510,20,P.muted,tr("Waiting for real samples","等待真实采样"));
  return;
 }
 int span=BODY_W-48;
 for(int i=1;i<n;i++) {
  int x0=BODY_X+24+(BATT_LOG_CAP-n+i-1)*span/(BATT_LOG_CAP-1);
  int x1=BODY_X+24+(BATT_LOG_CAP-n+i)*span/(BATT_LOG_CAP-1);
  batt_sample_t a=history[i-1],b=history[i];
  if(a.mv&&b.mv) {
   line(x0,570-clamp((a.mv-3000)*120/1400,0,120),x1,570-clamp((b.mv-3000)*120/1400,0,120),P.accent);
   line(x0,570-clamp((a.ma+2000)*120/4000,0,120),x1,570-clamp((b.ma+2000)*120/4000,0,120),P.warning);
  }
  if(a.soc>=0&&b.soc>=0)line(x0,570-clamp(a.soc*120/100,0,120),x1,570-clamp(b.soc*120/100,0,120),P.text);
 }
 text(BODY_X+24,586,15,P.muted,"-60m   V 3.0-4.4 / A -2..2 / SOC 0-100%");
}
static void power_draw(void) {
 char b[96];
 metric(BODY_X,64,224,"Battery voltage","电池电压",view.battery_valid,view.battery_v,"V");
 metric(BODY_X+244,64,224,"Battery current","电池电流",view.battery_valid,view.battery_a,"A");
 metric(BODY_X+488,64,224,"Battery power","电池功率",view.battery_valid,view.battery_v*view.battery_a,"W");
 metric(BODY_X,184,224,"USB voltage","USB 电压",view.usb_valid,view.usb_v,"V");
 metric(BODY_X+244,184,224,"USB current","USB 电流",view.usb_valid,view.usb_a,"A");
 metric(BODY_X+488,184,224,"USB power","USB 功率",view.usb_valid,view.usb_v*view.usb_a,"W");
 roundbox(BODY_X,304,BODY_W,72,16,P.card);
 if(view.calibration_verified&&view.capacity_mah>0&&isfinite(view.capacity_mah))
  snprintf(b,sizeof(b),"%s %.0f mAh",tr("Capacity","容量"),(double)view.capacity_mah);
 else snprintf(b,sizeof(b),"%s",tr("Capacity unavailable / calibration unverified","容量不可用 / 校准未验证"));
 text(BODY_X+20,320,18,P.warning,b);
 if(view.calibration_verified&&view.battery_valid&&view.runtime_hours>0&&isfinite(view.runtime_hours))
  snprintf(b,sizeof(b),"%s %.1f h",tr("Runtime estimate","续航估算"),(double)view.runtime_hours);
 else snprintf(b,sizeof(b),"%s",tr("Runtime unavailable / no verified estimate","续航不可用 / 无有效估算"));
 text(BODY_X+20,346,18,P.warning,b);
 graph_draw();
}

/* ---------- settings: device ---------- */
static void touch_area(void) {
 roundbox(BODY_X,436,BODY_W,288,16,P.card);
 text(BODY_X+20,452,18,P.muted,tr("SINGLE-FINGER TOUCH TEST","单指触摸测试"));
 button(BODY_X+BODY_W-184,448,164,touch_test?"Stop":"Start",touch_test?"结束":"开始",touch_test);
 if(!touch_test) {
  text(BODY_X+20,540,20,P.muted,tr("Start to inspect local touch coordinates","开始后显示本地触摸坐标"));
  return;
 }
 char b[72];snprintf(b,sizeof(b),"X %d   Y %d   %s",tx,ty,touch_down?"DOWN":"UP");
 text(BODY_X+20,500,26,P.accent,b);
 if(tx>=BODY_X+20&&tx<BODY_X+BODY_W-20&&ty>=548&&ty<712) {
  line(tx-14,ty,tx+14,ty,P.accent);line(tx,ty-14,tx,ty+14,P.accent);
 }
}
static void device_draw(void) {
 char b[64];
 roundbox(BODY_X,64,BODY_W,196,16,P.card);
 text(BODY_X+20,80,18,P.muted,tr("SENSOR PRESENCE / CHECKED BY DEVICE SERVICE","传感器在位 / 由设备服务检测"));
 /* Bit indices deliberately match the shared mask; no sensor mapping is
  * invented here that mix_view.h does not actually define. */
 for(int i=0;i<16;i++) {
  bool checked=(view.sensors_checked&(1u<<i))!=0,present=(view.sensors_present&(1u<<i))!=0;
  snprintf(b,sizeof(b),"%02d %s",i,tr(checked?(present?"yes":"no"):"?",checked?(present?"在位":"无"):"?"));
  text(BODY_X+20+(i%4)*172,112+(i/4)*34,17,checked?(present?P.accent:P.warning):P.muted,b);
 }
 roundbox(BODY_X,276,344,144,16,P.card);
 text(BODY_X+20,292,18,P.muted,tr("AUDIO","音频"));
 text(BODY_X+20,320,20,P.text,
      tr(view.headphone_valid?(view.headphone_inserted?"Headphones in":"Headphones out"):"Jack unknown",
         view.headphone_valid?(view.headphone_inserted?"耳机已插入":"耳机未插入"):"耳机状态未知"));
 for(int i=0;i<2;i++) {
  float f=i?view.mic_r:view.mic_l;
  text(BODY_X+20,352+i*28,16,P.muted,i?"R":"L");
  rect(BODY_X+44,356+i*28,268,10,P.raised);
  if(view.audio_ready&&isfinite(f))rect(BODY_X+44,356+i*28,ratio_width(f,268),10,P.accent);
 }
 roundbox(BODY_X+368,276,344,144,16,P.card);
 text(BODY_X+388,292,18,P.muted,tr("KEYBOARD AND MEMORY","键盘与内存"));
 text(BODY_X+388,320,20,P.text,
      tr(view.keyboard_online?"Keyboard connected":"Keyboard offline",
         view.keyboard_online?"键盘已连接":"键盘离线"));
 snprintf(b,sizeof(b),"%s %lu",tr("Resyncs","重新同步"),(unsigned long)view.keyboard_overflows);
 text(BODY_X+388,352,17,P.muted,b);
 snprintf(b,sizeof(b),"PSRAM %.2f MiB",view.free_psram/1048576.0);
 text(BODY_X+388,380,17,P.muted,b);
 button(BODY_X+368,724,344,"Reset local input","重置本地输入",false);
 touch_area();
}

/* ---------- settings: system ---------- */
static void system_draw(void) {
 char b[96];
 roundbox(BODY_X,64,BODY_W,224,16,P.card);
 text(BODY_X+20,80,18,P.muted,tr("VERSIONS AND CONNECTION","版本与连接"));
 text(BODY_X+20,110,26,P.text,"MixOS " MIX_VERSION);
 snprintf(b,sizeof(b),"ESP32-S3 / USB protocol v1 / %dx%d",geom()->cols,geom()->rows);
 text(BODY_X+20,148,18,P.muted,b);
 text(BODY_X+20,178,18,P.muted,tr("Linux and keyboard firmware versions: not reported",
                                  "Linux 与键盘固件版本：未报告"));
 if(view.linux_online) {
  snprintf(b,sizeof(b),"Linux CPU %d%% / RAM %u/%u MiB / up %u min",
           (int)view.linux_cpu,(unsigned)(view.linux_mem_used_kib/1024),
           (unsigned)(view.linux_mem_total_kib/1024),(unsigned)(view.linux_uptime_s/60));
  text(BODY_X+20,212,18,P.muted,b);
  if(view.host_ip[0]){snprintf(b,sizeof(b),"IP %s",view.host_ip);text(BODY_X+20,244,18,P.muted,b);}
 } else text(BODY_X+20,212,18,P.muted,tr("Linux metrics unavailable","Linux 指标不可用"));
 roundbox(BODY_X,304,BODY_W,180,16,P.card);
 text(BODY_X+20,320,18,P.muted,tr("FIRMWARE","固件"));
 if(view.ota_state==1) {
  snprintf(b,sizeof(b),"%s %d%%",tr("Receiving update","接收固件更新"),clamp(view.ota_percent,0,100));
  text(BODY_X+20,352,20,P.accent,b);
  roundbox(BODY_X+20,388,BODY_W-40,12,6,P.raised);
  roundbox(BODY_X+20,388,clamp((BODY_W-40)*clamp(view.ota_percent,0,100)/100,12,BODY_W-40),12,6,P.accent);
 } else if(view.ota_state==2) {
  text(BODY_X+20,352,20,P.accent,tr("Update verified; restarting","更新已校验，正在重启"));
 } else if(view.firmware_on_trial) {
  text(BODY_X+20,352,20,P.warning,tr("New build on trial","新固件试运行中"));
  text(BODY_X+20,388,18,P.muted,tr("Confirms automatically when healthy","健康检查通过后自动确认"));
 } else {
  text(BODY_X+20,352,20,P.muted,tr("A/B update over USB","通过 USB 进行 A/B 升级"));
  text(BODY_X+20,388,18,P.muted,tr("Run tools/deploy_ota.py on Linux","在 Linux 上运行 tools/deploy_ota.py"));
 }
 text(BODY_X+20,428,17,P.muted,tr("Terminal output cannot start or approve an update.",
                                  "终端输出无法发起或批准更新。"));
 roundbox(BODY_X,500,BODY_W,224,16,P.card);
 text(BODY_X+20,516,18,P.muted,tr("MAINTENANCE AND SHELL","维护与命令行"));
 button(BODY_X+20,548,332,view.job_running?"Cancel task":"Run maintenance",
        view.job_running?"取消任务":"运行维护",false);
 button(BODY_X+364,548,328,"Open a shell","打开命令行",false);
 snprintf(b,sizeof(b),"%s %d%%",tr(view.job_running?"Running":"Idle",view.job_running?"运行中":"空闲"),
          clamp(view.job_percent,0,100));
 text(BODY_X+20,616,20,P.muted,b);
}
static void settings_draw(void) {
 rect(0,STATUS_H,W,H-STATUS_H,P.bg);
 section_rail();
 switch(section) {
 case SEC_APPEARANCE: appearance_draw();break;
 case SEC_NETWORK: network_draw();break;
 case SEC_POWER: power_draw();break;
 case SEC_DEVICE: device_draw();break;
 default: system_draw();break;
 }
 if(notice[0])text(RAIL_X,700,17,P.warning,notice);
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
  rect(ox+col*g->cw,y,w,g->ch,bg);
  cell_glyph(ox+col*g->cw,y,w,g->ch,g->font,c.codepoint,fg,(c.flags&MIX_ATTR_BOLD)!=0);
  if(c.flags&MIX_ATTR_UNDERLINE)rect(ox+col*g->cw,y+g->ch-3,w,1,fg);
 }
 if(mix_terminal_cursor_visible()&&!mix_terminal_scroll_offset()&&row==mix_terminal_cursor_y())
  rect(ox+clamp(mix_terminal_cursor_x(),0,g->cols-1)*g->cw,y+g->ch-2,g->cw,2,P.accent);
}
static void agent_placeholder(void) {
 rect(0,STATUS_H,W,H-STATUS_H,P.bg);
 roundbox(192,220,640,320,24,P.card);
 icon(512,312,96,P.raised,ICON_AGENT);
 text_mid(512,400,34,P.text,tr("Agent is a placeholder","Agent 目前是占位"));
 text_mid(512,452,20,P.muted,tr("No backend is wired to this button yet.","这个按钮尚未接任何后端。"));
 text_mid(512,484,18,P.muted,tr("Press the Home key to go back.","按 Home 键返回。"));
}
static void waiting_draw(void) {
 rect(0,STATUS_H,W,H-STATUS_H,P.bg);
 const char *en=view.linux_online?"Starting the application":"Linux is offline";
 const char *zh=view.linux_online?"正在启动应用":"Linux 离线";
 text_mid(512,352,30,view.linux_online?P.text:P.warning,tr(en,zh));
 if(view.linux_online) {
  for(int i=0;i<5;i++)
   roundbox(452+i*28,412,18,18,9,i==spin_phase%5?P.accent:P.raised);
 } else {
  text_mid(512,404,20,P.muted,tr("Check the USB link, then try again","请检查 USB 连接后重试"));
 }
}
static void app_draw(void) {
 if(current_app==MIX_APP_AGENT){agent_placeholder();return;}
 if(!view.terminal_open){waiting_draw();return;}
 rect(0,STATUS_H,W,H-STATUS_H,P.bg);
 for(int r=0;r<geom()->rows;r++)terminal_row_draw(r);
 mix_terminal_clean();
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

/* ---------- frame assembly ---------- */
static void draw_all(void) {
 switch(page) {
 case PAGE_HOME: home_draw();break;
 case PAGE_APP: app_draw();break;
 default: settings_draw();break;
 }
 status_bar();
 if(modal)modal_draw();
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
 panel=p;
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
 if(theme>=4)theme=0;
 if(language>1)language=0;
 if(geometry>=GEOMETRY_COUNT)geometry=1;
 if(!ttf_font_ready()){language=0;snprintf(notice,sizeof(notice),"Font unavailable: reduced ASCII fallback");}
 mix_terminal_resize(geom()->cols,geom()->rows);
 repaint=true;return ESP_OK;
}
void mix_ui_tick(const mix_view_t *v,uint32_t now) {
 if(!fb||!v)return;
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
  view=*v;view_ms=now;have_view=true;
  if(changed||(page==PAGE_SETTINGS&&section==SEC_POWER)) {
   if(page==PAGE_APP&&view.terminal_open&&!modal){status_bar();present(0,STATUS_H);}
   else repaint=true;
  }
 }
 /* The waiting indicator has to move while nothing else is changing. */
 if(page==PAGE_APP&&!view.terminal_open&&current_app!=MIX_APP_AGENT&&
    view.linux_online&&!modal&&(uint32_t)(now-spin_ms)>=180) {
  spin_ms=now;spin_phase++;repaint=true;
 }
 if(repaint) {
  draw_all();repaint=false;test_dirty=false;
  present(0,H);
 }
 if(page==PAGE_SETTINGS&&section==SEC_DEVICE&&test_dirty&&!modal&&(uint32_t)(now-touch_ms)>=50) {
  touch_ms=now;touch_area();present(436,724);test_dirty=false;
 }
 if(page==PAGE_APP&&view.terminal_open&&current_app!=MIX_APP_AGENT&&!modal&&
    (uint32_t)(now-term_ms)>=50) {
  term_ms=now;
  const geometry_t *g=geom();
  int oy=term_oy();
  int offset=mix_terminal_scroll_offset();
  if(offset!=last_scroll_offset){status_bar();present(0,STATUS_H);last_scroll_offset=offset;}
  int cx=mix_terminal_cursor_x(),cy=mix_terminal_cursor_y();
  bool cv=mix_terminal_cursor_visible();
  bool moved=cx!=last_cursor_x||cy!=last_cursor_y||cv!=last_cursor_visible;
  int start=-1;
  for(int r=0;r<g->rows;r++) {
   bool dirty=mix_terminal_dirty(r)||(moved&&(r==cy||r==last_cursor_y));
   if(dirty) {
    terminal_row_draw(r);
    if(start<0)start=r;
   }
   else if(start>=0){present(oy+start*g->ch,oy+r*g->ch);start=-1;}
  }
  if(start>=0)present(oy+start*g->ch,oy+g->rows*g->ch);
  mix_terminal_clean();
  last_cursor_x=cx;last_cursor_y=cy;last_cursor_visible=cv;
 }
}

/* ---------- input ---------- */
static void launch(int index) {
 if(index==3){navigate(PAGE_SETTINGS);return;}
 static const mix_app_t apps[3]={MIX_APP_TRANSLATE,MIX_APP_NOTES,MIX_APP_AGENT};
 current_app=(uint8_t)apps[index];
 /* navigate() opens the session for the page it enters, and already skips the
  * Agent placeholder. Queueing here as well made one card press produce two
  * MIX_ACTION_APP_OPEN actions. The second was harmless on the device -
  * mix_link_open_app() treats a repeat of the application already opening as
  * the answer to itself - but "one press, one action" is the contract the
  * parent and the tests are written against, so it is queued in one place. */
 navigate(PAGE_APP);
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
 net_view=1;net_reveal=false;
 memset(net_pass,0,sizeof(net_pass));net_pass_len=0;
 repaint=true;
}
static void touch_appearance(int x,int y) {
 if(inside(x,y,BODY_X+20,112,332,44)||inside(x,y,BODY_X+364,112,332,44)||
    inside(x,y,BODY_X+20,168,332,44)||inside(x,y,BODY_X+364,168,332,44)) {
  theme=(uint8_t)((y>=168?2:0)+(x>=BODY_X+364?1:0));save_prefs();repaint=true;return;
 }
 if(inside(x,y,BODY_X+20,224,332,44)){language=0;save_prefs();repaint=true;return;}
 if(inside(x,y,BODY_X+364,224,332,44)) {
  if(!ttf_font_ready()){mix_ui_notice("Chinese needs a valid font partition");return;}
  language=1;save_prefs();repaint=true;return;
 }
 if(inside(x,y,BODY_X+300,318,180,44)){queue(MIX_ACTION_BRIGHT_DOWN);return;}
 if(inside(x,y,BODY_X+492,318,180,44)){queue(MIX_ACTION_BRIGHT_UP);return;}
 if(inside(x,y,BODY_X+300,378,180,44)){queue(MIX_ACTION_VOLUME_DOWN);return;}
 if(inside(x,y,BODY_X+492,378,180,44)){queue(MIX_ACTION_VOLUME_UP);return;}
 for(int i=0;i<GEOMETRY_COUNT;i++)
  if(inside(x,y,BODY_X+20+i*344,506,332,58)){set_geometry(i);return;}
 if(inside(x,y,BODY_X+20,652,332,44))queue(MIX_ACTION_KBD_BACKLIGHT);
}
static void touch_network(int x,int y) {
 if(net_view) {
  if(inside(x,y,BODY_X+BODY_W-180,336,160,44)){net_reveal=!net_reveal;repaint=true;return;}
  if(inside(x,y,BODY_X,480,340,44)) {
   if(!mix_link_net_busy())queue(MIX_ACTION_NET_CONNECT);
   return;
  }
  if(inside(x,y,BODY_X+372,480,340,44)){modal=MIX_ACTION_NET_FORGET;repaint=true;return;}
  if(inside(x,y,BODY_X,552,340,44)){net_view=0;memset(net_pass,0,sizeof(net_pass));net_pass_len=0;repaint=true;}
  return;
 }
 if(inside(x,y,BODY_X+512,88,180,44)) {
  if(mix_link_net_busy())return;
  queue(MIX_ACTION_NET_SCAN);mix_ui_notice("Scanning");return;
 }
 const mix_net_entry_t *list=NULL;
 int count=mix_link_net_list(&list);
 int rows=count-net_scroll;
 if(rows>9)rows=9;
 for(int i=0;i<rows;i++)
  if(inside(x,y,BODY_X,172+i*58,BODY_W,50)){select_network(net_scroll+i);return;}
 if(count>9) {
  if(inside(x,y,BODY_X,700,220,44)){net_scroll=clamp(net_scroll-9,0,count-1);repaint=true;}
  else if(inside(x,y,BODY_X+240,700,220,44)){net_scroll=clamp(net_scroll+9,0,count-1);repaint=true;}
 }
}
void mix_ui_touch(int x,int y,bool down) {
 bool press=down&&!touch_down;
 bool release=!down&&touch_down;
 touch_down=down;tx=x;ty=y;
 if(modal) {
  if(press&&y>=454&&y<498) {
   if(x>=216&&x<488)resolve_modal(false);
   else if(x>=520&&x<808)resolve_modal(true);
  }
  return;
 }
 if(page==PAGE_SETTINGS&&section==SEC_DEVICE&&touch_test)test_dirty=true;
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
    pressed_inside=still;launcher_card(index,still);present(cy,cy+CARD_H);
   }
   return;
  }
  if(release) {
   pressed_card=-1;
   if(pressed_inside){launcher_card(index,false);present(cy,cy+CARD_H);launch(index);}
   pressed_inside=false;
   return;
  }
 }
 if(page==PAGE_APP&&release){drag_y=-1;}
 if(x<0||x>=W||y<0||y>=H)return;
 /* Scrolling the terminal is a drag on the terminal itself. The three buttons
  * that used to do it were 30x12 dp and could not reliably be hit; a gesture
  * has no minimum size, and dragging the content is what a touch screen
  * affords anyway. Dragging down moves towards older output, so the text
  * follows the finger. */
 if(page==PAGE_APP&&down&&y>=STATUS_H) {
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
    launcher_card(i,true);present(cy,cy+CARD_H);return;
   }
  }
  return;
 }
 if(page==PAGE_APP) {
  if(y<STATUS_H)return;
  return;
 }
 for(int i=0;i<SEC_COUNT;i++)
  if(inside(x,y,RAIL_X,72+i*76,RAIL_W,60)){go_section((section_t)i);return;}
 switch(section) {
 case SEC_APPEARANCE: touch_appearance(x,y);break;
 case SEC_NETWORK: touch_network(x,y);break;
 case SEC_DEVICE:
  if(inside(x,y,BODY_X+BODY_W-184,448,164,44)){touch_test=!touch_test;test_dirty=true;repaint=true;}
  else if(inside(x,y,BODY_X+368,724,344,44)){modal=MIX_ACTION_INPUT_RESET;repaint=true;}
  break;
 case SEC_SYSTEM:
  if(inside(x,y,BODY_X+20,548,332,44)) {
   modal=view.job_running?MIX_ACTION_JOB_CANCEL:MIX_ACTION_JOB_START;repaint=true;
  } else if(inside(x,y,BODY_X+364,548,328,44)) {
   current_app=MIX_APP_SHELL;navigate(PAGE_APP); /* navigate() queues the open */
  }
  break;
 default: break;
 }
}
/* Passphrase editing. Only printable ASCII is accepted: a Wi-Fi passphrase is
 * defined over exactly that range, so anything else is a keyboard artefact. */
static void passphrase_key(const uint8_t *bytes,size_t len) {
 if(len==1&&(bytes[0]=='\r'||bytes[0]=='\n')) {
  if(!mix_link_net_busy())queue(MIX_ACTION_NET_CONNECT);
  return;
 }
 if(len==1&&bytes[0]==27) {
  net_view=0;memset(net_pass,0,sizeof(net_pass));net_pass_len=0;repaint=true;return;
 }
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
 if(!bytes||!len)return;
 if(modal) {
  if(len==1&&(bytes[0]=='\r'||bytes[0]=='\n'))resolve_modal(true);
  else if(len==1&&bytes[0]==27)resolve_modal(false);
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
    card_rect(previous,&cx,&cy);launcher_card(previous,false);present(cy,cy+CARD_H);
    card_rect(home_focus,&cx,&cy);launcher_card(home_focus,false);present(cy,cy+CARD_H);
   }
   return;
  }
  return;
 }
 if(page==PAGE_SETTINGS&&len==1&&bytes[0]>='1'&&bytes[0]<='5')go_section((section_t)(bytes[0]-'1'));
 if(page==PAGE_APP&&current_app==MIX_APP_AGENT&&len==1&&bytes[0]==27)navigate(PAGE_HOME);
}
void mix_ui_home_toggle(void) {
 if(modal)return;
 navigate(page==PAGE_HOME?PAGE_SETTINGS:PAGE_HOME);
}
void mix_ui_volume(int percent) {
 volume_percent=clamp(percent,0,100);
 if(page==PAGE_SETTINGS&&section==SEC_APPEARANCE&&!modal)repaint=true;
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
 if(page==PAGE_SETTINGS&&!modal)repaint=true;
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
 return page==PAGE_APP&&!modal&&current_app!=MIX_APP_AGENT;
}
const char *mix_ui_net_ssid(void){return net_ssid;}
const char *mix_ui_net_passphrase(void){return net_pass;}
