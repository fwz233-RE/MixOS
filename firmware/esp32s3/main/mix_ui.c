#include "mix_ui.h"
#include "mix_terminal.h"
#include "ttf_font.h"
#include "batt_log.h"
#include "esp_heap_caps.h"
#include "nvs.h"
#include <math.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#define W 1024
#define H 768
#define RGB(r,g,b) ((uint16_t)((((r)>>3)<<11)|(((g)>>2)<<5)|((b)>>3)))
_Static_assert(W * H * sizeof(uint16_t) == 1572864, "UI framebuffer must be 1.5 MiB");
_Static_assert(MIX_TERM_COLS == 80 && MIX_TERM_ROWS == 28, "fixed terminal geometry");
typedef enum { HOME, POWER, TERMINAL, DEVICE, SETTINGS } page_t;
typedef struct { uint16_t bg, card, raised, text, muted, accent, warning; } palette_t;
static const palette_t palettes[4] = {
 {RGB(19,23,26),RGB(29,35,39),RGB(43,51,56),RGB(240,245,243),RGB(163,178,176),RGB(135,235,194),RGB(244,190,111)},
 {RGB(241,244,240),RGB(255,255,251),RGB(222,231,221),RGB(29,46,38),RGB(78,101,90),RGB(24,116,81),RGB(151,80,14)},
 {RGB(17,25,42),RGB(26,38,59),RGB(39,55,80),RGB(235,242,255),RGB(165,187,215),RGB(136,193,255),RGB(255,202,132)},
 {RGB(29,24,22),RGB(42,34,29),RGB(60,48,36),RGB(255,242,221),RGB(198,176,146),RGB(244,188,106),RGB(255,154,128)}
};
static esp_lcd_panel_handle_t panel;
static uint16_t *fb;
static mix_view_t view;
static page_t page, last_page = TERMINAL;
static uint8_t theme, language;
static bool repaint = true, touch_down, touch_test, test_dirty, have_view;
static int tx, ty, last_cursor_x = -1, last_cursor_y = -1;
static bool last_cursor_visible;
static uint32_t view_ms, term_ms, touch_ms;
static int last_scroll_offset = -1;
static int modal; /* 0 none, otherwise mix_action_kind_t; only local input accepts */
static mix_action_t actions[8];
static unsigned action_read, action_write;
static char notice[96];
static batt_sample_t history[BATT_LOG_CAP]; /* 4.3 KiB RAM snapshot, never samples */
#define P (palettes[theme])
static const char *tr(const char *en, const char *zh) { return language ? zh : en; }
static int clamp(int a, int lo, int hi) { return a < lo ? lo : a > hi ? hi : a; }
static int ratio_width(float value,int width) { if(!isfinite(value)||value<=0)return 0;if(value>=1)return width;return (int)(value*width); }
static void rect(int x,int y,int w,int h,uint16_t c) {
 int right=clamp(x+w,0,W), bottom=clamp(y+h,0,H);
 for(int j=clamp(y,0,H);j<bottom;j++) for(int i=clamp(x,0,W);i<right;i++) fb[j*W+i]=c;
}
static void roundbox(int x,int y,int w,int h,int r,uint16_t c) {
 rect(x+r,y,w-2*r,h,c); rect(x,y+r,w,h-2*r,c);
 for(int dy=0;dy<r;dy++) for(int dx=0;dx<r;dx++)
  if((r-dx)*(r-dx)+(r-dy)*(r-dy)<=r*r) {
   rect(x+dx,y+dy,1,1,c); rect(x+w-1-dx,y+dy,1,1,c);
   rect(x+dx,y+h-1-dy,1,1,c); rect(x+w-1-dx,y+h-1-dy,1,1,c);
  }
}
/* Emergency font: independent 5x7 alphabet/digits, for missing font partition.
 * Normal terminal uses TTF in a bounded cell scratch, including UTF-8 CJK.
 */
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
 else {rect(x,y,5*scale,scale,color);rect(x,y,scale,7*scale,color);rect(x+4*scale,y,scale,7*scale,color);rect(x,y+6*scale,5*scale,scale,color);}
}
static void text(int x,int y,int size,uint16_t color,const char *s) {
 if(ttf_font_ready()) {ttf_draw_text(fb,W,H,x,y,size,color,s);return;}
 int scale=size>=26?3:2;
 for(;*s;s++) { if(((uint8_t)*s&0xc0)==0x80) continue; fallback_char(x,y,scale,(uint8_t)*s,color);x+=6*scale; }
}
static void label(int x,int y,const char *en,const char *zh) {text(x,y,20,P.muted,tr(en,zh));}
static void button(int x,int y,int w,const char *en,const char *zh,bool active) {
 roundbox(x,y,w,44,10,active?P.accent:P.raised);
 text(x+16,y+10,20,active?P.bg:P.text,tr(en,zh));
}
static void line(int x0,int y0,int x1,int y1,uint16_t c) {
 int dx=abs(x1-x0),sx=x0<x1?1:-1,dy=-abs(y1-y0),sy=y0<y1?1:-1,err=dx+dy;
 for(;;) {rect(x0,y0,2,2,c);if(x0==x1&&y0==y1)break;int e=2*err;if(e>=dy){err+=dy;x0+=sx;}if(e<=dx){err+=dx;y0+=sy;}}
}
static void save_prefs(void) {
 nvs_handle_t h;if(nvs_open("mixui",NVS_READWRITE,&h)==ESP_OK) {
  esp_err_t e=nvs_set_u8(h,"theme",theme);if(e==ESP_OK)e=nvs_set_u8(h,"lang",language);
  if(e==ESP_OK)e=nvs_commit(h);nvs_close(h);if(e!=ESP_OK)mix_ui_notice("Preferences not saved");
 } else mix_ui_notice("Preferences not saved");
}
static void queue(mix_action_kind_t kind) {
 unsigned next=(action_write+1)%8;
 if(next==action_read) {mix_ui_notice("Action queue full; retry");return;}
 actions[action_write]=(mix_action_t){.kind=kind,.value=0};action_write=next;
}
static void navigate(page_t p) {if(page!=p){page=p;repaint=true;if(p==TERMINAL)mix_terminal_invalidate();}}
static void metric(int x,int y,int width,const char *en,const char *zh,bool valid,float value,const char *unit) {
 char b[64];roundbox(x,y,width,108,16,P.card);label(x+20,y+16,en,zh);
 if(valid&&isfinite(value))snprintf(b,sizeof(b),"%.2f %s",(double)value,unit);else snprintf(b,sizeof(b),"%s",tr("Unavailable","不可用"));
 text(x+20,y+50,30,valid?P.text:P.muted,b);
}
static void navbar(void) {
 static const char *en[]={"Home","Power","Terminal","Device","Settings"};
 static const char *zh[]={"首页","电源","终端","设备","设置"};
 rect(0,704,W,64,P.card);for(int i=0;i<5;i++)button(16+i*201,714,188,en[i],zh[i],page==(page_t)i);
}
static void header(const char *en,const char *zh) {
 text(32,26,32,P.text,tr(en,zh));text(660,34,18,P.muted,view.linux_online?tr("LINUX / Connected","LINUX / 已连接"):tr("LINUX / Offline","LINUX / 离线"));
 if(notice[0])text(32,78,18,P.warning,notice);
}
static void home_draw(void) {
 header("Your device, at a glance","设备概览");
 roundbox(32,120,600,260,20,P.card);label(56,144,"BATTERY / LOCAL TELEMETRY","电池 / 本地状态");
 char b[128];if(view.soc_valid&&isfinite(view.soc))snprintf(b,sizeof(b),"%.0f %%",(double)view.soc);else snprintf(b,sizeof(b),"%s",tr("Unknown","未知"));
 text(56,190,64,P.text,b);
 label(56,292,view.usb_valid?(view.usb_v>4?"USB power present":"Battery power"):"Power source unknown",view.usb_valid?(view.usb_v>4?"USB 供电":"电池供电"):"供电来源未知");
 roundbox(56,336,552,12,6,P.raised);if(view.soc_valid&&isfinite(view.soc))roundbox(56,336,clamp(ratio_width(view.soc/100.0f,552),12,552),12,6,P.accent);
 roundbox(656,120,336,260,20,P.card);label(680,144,"TERMINAL","终端");text(680,190,30,P.text,"80 x 28");
 label(680,244,"USB CDC / fixed cells","USB CDC / 固定字符格");button(680,304,288,"Open terminal","打开终端",true);
 metric(32,404,304,"Battery voltage","电池电压",view.battery_valid,view.battery_v,"V");
 metric(360,404,304,"Load power","负载功率",view.battery_valid,view.battery_v*view.battery_a,"W");
 metric(688,404,304,"Display brightness","屏幕亮度",true,(float)view.brightness,"%");
 roundbox(32,536,960,140,20,P.card);label(56,560,"LOCAL UI / REMOTE SHELL","本地界面 / 远程命令行");
 text(56,592,20,P.text,tr("Home switches pages; your terminal stays connected.","切换页面不影响终端连接。"));
 if(view.linux_online) {
  snprintf(b,sizeof(b),"Linux CPU %d%% / %d C / RAM %u/%u MiB / up %u min",(int)view.linux_cpu,(int)view.linux_temp,(unsigned int)(view.linux_mem_used_kib/1024),(unsigned int)(view.linux_mem_total_kib/1024),(unsigned int)(view.linux_uptime_s/60));
  text(56,632,18,P.muted,b);
 } else label(56,632,"Linux CPU / temperature / memory / uptime: unavailable","Linux 处理器 / 温度 / 内存 / 运行时间：不可用");
}
static void graph_draw(void) {
 roundbox(32,372,960,220,16,P.card);label(52,388,"LAST HOUR / voltage · current · charge","最近一小时 / 电压 · 电流 · 电量");
 int n=batt_log_get(history,BATT_LOG_CAP);
 for(int k=0;k<4;k++)rect(56,430+k*40,904,1,P.raised);
 if(n<2){label(72,478,"Waiting for real samples — no invented data","等待真实采样，不生成模拟数据");return;}
 for(int i=1;i<n;i++) {
  int x0=56+(BATT_LOG_CAP-n+i-1)*904/(BATT_LOG_CAP-1), x1=56+(BATT_LOG_CAP-n+i)*904/(BATT_LOG_CAP-1);
  batt_sample_t a=history[i-1],b=history[i];
  if(a.mv&&b.mv)line(x0,550-clamp((a.mv-3000)*120/1400,0,120),x1,550-clamp((b.mv-3000)*120/1400,0,120),P.accent);
  if(a.mv&&b.mv)line(x0,550-clamp((a.ma+2000)*120/4000,0,120),x1,550-clamp((b.ma+2000)*120/4000,0,120),P.warning);
  if(a.soc>=0&&b.soc>=0)line(x0,550-clamp(a.soc*120/100,0,120),x1,550-clamp(b.soc*120/100,0,120),P.text);
 }
 text(56,564,14,P.muted,"-60m    V:3.0..4.4 / A:-2..2 / SOC:0..100%                                  now");
}
static void power_draw(void) {
 header("Power, measured locally","本地电源测量");
 metric(32,120,304,"Battery voltage","电池电压",view.battery_valid,view.battery_v,"V");
 metric(360,120,304,"Battery current","电池电流",view.battery_valid,view.battery_a,"A");
 metric(688,120,304,"Battery power","电池功率",view.battery_valid,view.battery_v*view.battery_a,"W");
 metric(32,248,304,"USB voltage","USB 电压",view.usb_valid,view.usb_v,"V");
 metric(360,248,304,"USB current","USB 电流",view.usb_valid,view.usb_a,"A");
 metric(688,248,304,"USB power","USB 功率",view.usb_valid,view.usb_v*view.usb_a,"W");
 graph_draw();
 char b[128];roundbox(32,608,960,72,16,P.card);
 if(view.calibration_verified&&view.capacity_mah>0&&isfinite(view.capacity_mah))snprintf(b,sizeof(b),"%s %.0f mAh",tr("Capacity","容量"),(double)view.capacity_mah);else snprintf(b,sizeof(b),"%s",tr("Capacity unavailable / calibration unverified","容量不可用 / 校准未验证"));
 text(52,623,18,P.warning,b);
 if(view.calibration_verified&&view.battery_valid&&view.runtime_hours>0&&isfinite(view.runtime_hours))snprintf(b,sizeof(b),"%s %.1f h",tr("Runtime estimate","续航估算"),(double)view.runtime_hours);else snprintf(b,sizeof(b),"%s",tr("Runtime unavailable / no verified estimate","续航不可用 / 无有效估算"));
 text(530,623,18,P.warning,b);
}
static void touch_area(void) {
 roundbox(32,416,620,264,16,P.card);label(52,432,"SINGLE-FINGER TOUCH TEST","单指触摸测试");
 button(464,428,164,touch_test?"Stop test":"Start test",touch_test?"结束测试":"开始测试",touch_test);
 label(52,486,touch_test?"Move one finger in this area":"Start to inspect local coordinates",touch_test?"在区域内移动单指":"开始后显示本地触摸坐标");
 if(touch_test) {char b[72];snprintf(b,sizeof(b),"X %d   Y %d   %s",tx,ty,touch_down?"DOWN":"UP");text(52,530,26,P.accent,b);
  if(tx>=52&&tx<632&&ty>=572&&ty<660){line(tx-12,ty,tx+12,ty,P.accent);line(tx,ty-12,tx,ty+12,P.accent);}}
}
static void device_draw(void) {
 header("Device diagnostics","设备诊断");
 roundbox(32,120,620,272,16,P.card);label(52,140,"SENSOR PRESENCE / CHECKED BY DEVICE SERVICE","传感器在位 / 由设备服务检测");
 /* Bit indices deliberately match the shared mask without inventing sensor mapping. */
 for(int i=0;i<16;i++) {char b[48];bool checked=(view.sensors_checked&(1u<<i))!=0,present=(view.sensors_present&(1u<<i))!=0;
  snprintf(b,sizeof(b),"%02d  %s",i,tr(checked?(present?"Present":"Absent"):"Unknown",checked?(present?"在位":"未检测到"):"未知"));
  text(52+(i%4)*146,188+(i/4)*44,18,checked?(present?P.accent:P.warning):P.muted,b);}
 roundbox(676,120,316,272,16,P.card);label(696,140,"AUDIO","音频");
 text(696,186,22,P.text,tr(view.headphone_valid?(view.headphone_inserted?"Headphones in":"Headphones out"):"Jack unknown",view.headphone_valid?(view.headphone_inserted?"耳机已插入":"耳机未插入"):"耳机状态未知"));
 label(696,234,view.audio_ready?"Microphone levels":"Microphone unavailable",view.audio_ready?"麦克风电平":"麦克风不可用");
 for(int i=0;i<2;i++){float f=i?view.mic_r:view.mic_l;rect(728,282+i*42,236,12,P.raised);text(696,274+i*42,18,P.muted,i?"R":"L");if(view.audio_ready&&isfinite(f))rect(728,282+i*42,ratio_width(f,236),12,P.accent);}
 touch_area();roundbox(676,416,316,264,16,P.card);label(696,436,"KEYBOARD / MEMORY","键盘 / 内存");
 text(696,480,22,P.text,tr(view.keyboard_online?"Keyboard connected":"Keyboard offline",view.keyboard_online?"键盘已连接":"键盘离线"));
 char b[64];snprintf(b,sizeof(b),"%s %lu",tr("Resyncs","重新同步"),(unsigned long)view.keyboard_overflows);text(696,524,18,P.muted,b);
 snprintf(b,sizeof(b),"PSRAM %.2f MiB",view.free_psram/1048576.0);text(696,568,20,P.muted,b);
 button(696,616,276,"Reset local input","重置本地输入",false);
}
static void settings_draw(void) {
 header("Make it yours","偏好设置");
 roundbox(32,120,600,262,16,P.card);label(52,140,"APPEARANCE / SAVED LOCALLY","外观 / 本地保存");
 static const char *names[]={"Graphite","Paper","Midnight","Ember"};
 static const char *zh[]={"石墨薄荷","纸白森林","午夜蓝","暖琥珀"};
 for(int i=0;i<4;i++)button(52+(i%2)*282,184+(i/2)*60,266,names[i],zh[i],theme==i);
 button(52,314,266,"English","English",!language);button(334,314,266,"中文","中文",language);
 roundbox(656,120,336,262,16,P.card);label(676,140,"DISPLAY & KEYBOARD","屏幕与键盘");
 char b[64];snprintf(b,sizeof(b),"%s %d%%",tr("Brightness","亮度"),view.brightness);text(676,186,24,P.text,b);
 button(676,230,140,"- 10%","- 10%",false);button(836,230,136,"+ 10%","+ 10%",false);button(676,310,296,"Keyboard backlight","键盘背光",false);
 roundbox(32,406,600,274,16,P.card);label(52,426,"VERSIONS & CONNECTION","版本与连接");
 text(52,470,24,P.text,"MixOS " MIX_VERSION);text(52,514,20,P.muted,"ESP32-S3 / USB protocol v1 / 80x28");
 label(52,558,"Linux / keyboard firmware: not reported","Linux / 键盘固件版本：未报告");
 text(52,610,18,P.muted,tr("Fixed 12x24 cells; CJK uses two columns.","固定 12x24 字符格，中文占两列。"));
 roundbox(656,406,336,274,16,P.card);label(676,426,"MAINTENANCE / FIRMWARE","维护 / 固件");
 button(676,474,296,view.job_running?"Cancel task":"Run maintenance",view.job_running?"取消任务":"运行维护",false);
 if(view.ota_state==1){
  snprintf(b,sizeof(b),"%s %d%%",tr("Receiving update","接收固件更新"),clamp(view.ota_percent,0,100));
  text(676,540,20,P.accent,b);
  roundbox(676,572,296,12,6,P.raised);
  roundbox(676,572,clamp(296*clamp(view.ota_percent,0,100)/100,12,296),12,6,P.accent);
 }else if(view.ota_state==2){
  text(676,540,20,P.accent,tr("Update verified; restarting","更新已校验，正在重启"));
 }else if(view.firmware_on_trial){
  text(676,540,20,P.warning,tr("New build on trial","新固件试运行中"));
  text(676,572,18,P.muted,tr("Confirms automatically when healthy","健康检查通过后自动确认"));
 }else{
  text(676,540,20,P.muted,tr("A/B update over USB","通过 USB 进行 A/B 升级"));
  text(676,572,18,P.muted,tr("Run tools/ota_esp.py on Linux","在 Linux 上运行 tools/ota_esp.py"));
 }
 snprintf(b,sizeof(b),"%s %d%%",tr(view.job_running?"Running":"Idle",view.job_running?"运行中":"空闲"),clamp(view.job_percent,0,100));text(676,620,20,P.muted,b);
}
static uint16_t ansi(uint8_t c,bool foreground) {
 static const uint16_t basic[]={0x0000,0xc986,0x4d4c,0xe5e9,0x541c,0xb2b8,0x4df9,0xce79,0x73ae,0xf9ac,0x87b3,0xff50,0x8d7f,0xed1f,0x87ff,0xffff};
 if(c<16)return basic[c];
 if(c<232){int n=c-16,r=n/36,g=n/6%6,b=n%6;return RGB(r?55+r*40:0,g?55+g*40:0,b?55+b*40:0);}
 if(c<255){int n=8+(c-232)*10;return RGB(n,n,n);}
 return foreground?P.text:P.bg;
}
static void cell_glyph(int x,int y,int width,uint32_t cp,uint16_t fg,bool bold) {
 if(cp==0||cp==' ')return;
 if(!ttf_font_ready()){fallback_char(x+1,y+4,2,cp,fg);return;}
 ttf_draw_cell(fb,W,H,x,y,width,24,20,fg,cp,bold);
}
static void terminal_row_draw(int row) {
 const mix_cell_t *cells=mix_terminal_row(row);int y=32+row*24;rect(0,y,W,24,P.bg);if(!cells)return;
 for(int col=0;col<80;col++) {mix_cell_t c=cells[col];if(!c.width)continue;int width=c.width==2&&col<79?24:12;
  uint16_t fg=ansi(c.fg,true),bg=ansi(c.bg,false);if(c.flags&2){uint16_t t=fg;fg=bg;bg=t;}
  rect(32+col*12,y,width,24,bg);cell_glyph(32+col*12,y,width,c.codepoint,fg,(c.flags&1)!=0);
 }
 if(mix_terminal_cursor_visible()&&mix_terminal_scroll_offset()==0&&row==mix_terminal_cursor_y())rect(32+clamp(mix_terminal_cursor_x(),0,79)*12,y+22,12,2,P.accent);
}
static void terminal_header(void) {
 char b[112];rect(0,0,W,32,P.card);
 snprintf(b,sizeof(b),"%s / %s / 80x28 / %s %d",tr("Terminal","终端"),tr(view.terminal_open?"session open":"session closed",view.terminal_open?"会话已打开":"会话已关闭"),tr("history","历史"),mix_terminal_scroll_offset());
 text(12,5,17,P.text,b);
 for(int i=0;i<4;i++)roundbox(660+i*88,3,84,26,5,P.raised);
 text(668,7,14,P.accent,tr("Older","更早"));text(756,7,14,P.accent,tr("Newer","更新"));
 text(844,7,14,P.accent,tr("Live","实时"));
 text(932,7,14,P.accent,tr(view.terminal_open?"Close":"Open",view.terminal_open?"关闭":"打开"));
}
static void modal_draw(void) {
 roundbox(184,206,656,332,24,P.raised);text(216,238,30,P.text,tr("Confirm on this device","请在本机确认"));
 const char *en="Reset local input state?",*zh="重置本地输入状态？";
 if(modal==MIX_ACTION_JOB_START){en="Run the configured maintenance task?";zh="运行预设维护任务？";}
 if(modal==MIX_ACTION_JOB_CANCEL){en="Cancel the running maintenance task?";zh="取消正在运行的维护任务？";}
 text(216,302,23,P.text,tr(en,zh));label(216,354,"Local touch / Enter accepts. Escape rejects.","本地触摸或回车确认，Escape 拒绝。");
 label(216,392,"Terminal output cannot approve this request.","终端输出无法批准此请求。");
 button(216,454,272,"Reject / Escape","拒绝 / Escape",false);button(520,454,288,"Confirm / Enter","确认 / Enter",true);
}
static void resolve_modal(bool accept) {
 int a=modal;modal=0;repaint=true;
 if(accept)queue((mix_action_kind_t)a);
}
static void present(int y0,int y1) {esp_lcd_panel_draw_bitmap(panel,0,y0,W,y1,fb+y0*W);}
esp_err_t mix_ui_init(esp_lcd_panel_handle_t p) {
 if(!p)return ESP_ERR_INVALID_ARG;
 if(fb)return ESP_ERR_INVALID_STATE;
 fb=heap_caps_malloc(W*H*sizeof(uint16_t),MALLOC_CAP_SPIRAM|MALLOC_CAP_8BIT);if(!fb)return ESP_ERR_NO_MEM;
 panel=p;nvs_handle_t h;if(nvs_open("mixui",NVS_READONLY,&h)==ESP_OK){nvs_get_u8(h,"theme",&theme);nvs_get_u8(h,"lang",&language);nvs_close(h);}
 if(theme>=4)theme=0;if(language>1)language=0;
 if(!ttf_font_ready()){language=0;snprintf(notice,sizeof(notice),"Font unavailable: reduced ASCII fallback");}
 repaint=true;return ESP_OK;
}
void mix_ui_tick(const mix_view_t *v,uint32_t now) {
 if(!fb||!v)return;
 /* A running A/B transfer repaints on its own cadence so the progress bar
  * moves without waiting for the 1 Hz telemetry snapshot. */
 if(v->ota_state!=view.ota_state||(v->ota_state==1&&v->ota_percent!=view.ota_percent)){
  view.ota_state=v->ota_state;view.ota_percent=v->ota_percent;
  if(page==SETTINGS&&!modal)repaint=true;
 }
 if(!have_view||(uint32_t)(now-view_ms)>=1000){
  bool changed=!have_view||memcmp(&view,v,sizeof(view))!=0;view=*v;view_ms=now;have_view=true;
  if((changed||page==POWER)&&page!=TERMINAL)repaint=true;
  else if(changed&&page==TERMINAL&&!modal){terminal_header();present(0,32);}
 }
 if(repaint){rect(0,0,W,H,P.bg);switch(page){case HOME:home_draw();break;case POWER:power_draw();break;case TERMINAL:terminal_header();for(int i=0;i<28;i++)terminal_row_draw(i);mix_terminal_clean();break;case DEVICE:device_draw();break;case SETTINGS:settings_draw();break;}
  navbar();if(modal)modal_draw();present(0,H);repaint=false;test_dirty=false;return;}
 if(page==DEVICE&&test_dirty&&!modal&&(uint32_t)(now-touch_ms)>=50){touch_ms=now;touch_area();present(416,680);test_dirty=false;}
 if(page==TERMINAL&&!modal&&(uint32_t)(now-term_ms)>=50){
  term_ms=now;
  int offset=mix_terminal_scroll_offset();if(offset!=last_scroll_offset){terminal_header();present(0,32);last_scroll_offset=offset;}
  int cx=mix_terminal_cursor_x(),cy=mix_terminal_cursor_y();bool cv=mix_terminal_cursor_visible();
  bool moved=cx!=last_cursor_x||cy!=last_cursor_y||cv!=last_cursor_visible;
  int start=-1;for(int r=0;r<28;r++) {bool dirty=mix_terminal_dirty(r)||(moved&&(r==cy||r==last_cursor_y));
   if(dirty){terminal_row_draw(r);if(start<0)start=r;}else if(start>=0){present(32+start*24,32+r*24);start=-1;}}
  if(start>=0)present(32+start*24,704);mix_terminal_clean();last_cursor_x=cx;last_cursor_y=cy;last_cursor_visible=cv;
 }
}
void mix_ui_touch(int x,int y,bool down) {
 bool press=down&&!touch_down;touch_down=down;tx=x;ty=y;
 if(x<0||x>=W||y<0||y>=H)return;
 if(modal){if(press&&y>=454&&y<498){if(x>=216&&x<488)resolve_modal(false);else if(x>=520&&x<808)resolve_modal(true);}return;}
 if(page==DEVICE&&touch_test)test_dirty=true;
 if(!press)return;
 if(y>=714&&y<758){int n=(x-16)/201;if(x>=16&&n<5&&x<16+n*201+188)navigate((page_t)n);return;}
 if(page==HOME&&x>=680&&x<968&&y>=304&&y<348){queue(MIX_ACTION_TERMINAL_OPEN);navigate(TERMINAL);}
 else if(page==TERMINAL&&y<32){if(x>=924)queue(view.terminal_open?MIX_ACTION_TERMINAL_CLOSE:MIX_ACTION_TERMINAL_OPEN);else if(x>=836)mix_terminal_scroll(-MIX_TERM_HISTORY);else if(x>=748)mix_terminal_scroll(-14);else if(x>=660)mix_terminal_scroll(14);repaint=true;}
 else if(page==DEVICE){if(x>=464&&x<628&&y>=428&&y<472){touch_test=!touch_test;test_dirty=true;}else if(x>=696&&x<972&&y>=616&&y<660){modal=MIX_ACTION_INPUT_RESET;repaint=true;}}
 else if(page==SETTINGS){
  if(x>=52&&x<600&&y>=184&&y<288){int c=x>=334, r=y>=244;if(x<(c?600:318)&&y<(r?288:228)){theme=(uint8_t)(r*2+c);save_prefs();repaint=true;}}
  else if(x>=52&&x<600&&y>=314&&y<358){if(x<318||x>=334){language=x>=334;if(language&&!ttf_font_ready()){language=0;mix_ui_notice("Chinese needs a valid font partition");}save_prefs();repaint=true;}}
  else if(x>=676&&x<972&&y>=230&&y<274)queue(x<816?MIX_ACTION_BRIGHT_DOWN:MIX_ACTION_BRIGHT_UP);
  else if(x>=676&&x<972&&y>=310&&y<354)queue(MIX_ACTION_KBD_BACKLIGHT);
  else if(x>=676&&x<972&&y>=474&&y<518){modal=view.job_running?MIX_ACTION_JOB_CANCEL:MIX_ACTION_JOB_START;repaint=true;}
 }
}
void mix_ui_key(const uint8_t *bytes,size_t len) {
 if(!bytes||!len)return;
 if(modal){if(len==1&&(bytes[0]=='\r'||bytes[0]=='\n'))resolve_modal(true);else if(len==1&&bytes[0]==27)resolve_modal(false);return;}
 /* Remote input belongs to parent. No remote escape sequence becomes a UI command. */
 if(page!=TERMINAL&&len==1&&bytes[0]>='1'&&bytes[0]<='5')navigate((page_t)(bytes[0]-'1'));
}
void mix_ui_home_toggle(void) {if(modal)return;if(page==HOME)navigate(last_page);else{last_page=page;navigate(HOME);}}
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
 notice[out]=0;repaint=true;
}
bool mix_ui_take_action(mix_action_t *out) {if(!out||action_read==action_write)return false;*out=actions[action_read];action_read=(action_read+1)%8;return true;}
bool mix_ui_terminal_visible(void) {return page==TERMINAL&&!modal;}
