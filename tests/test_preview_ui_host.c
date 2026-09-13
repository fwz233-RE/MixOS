/* Host-only UI integration harness. Built by test_preview_ui.py with SDK stubs. */
#include <assert.h>
#include <stdlib.h>
#include <string.h>
#include "../firmware/esp32s3/main/mix_ui.c"

static unsigned draws, full_draws, allocations;
static int last_y0, last_y1;
static uint16_t *guarded;
static bool fake_font=true;
void *heap_caps_malloc(size_t n,unsigned caps) {
 (void)caps;allocations++;assert(n==1572864);
 guarded=calloc(1,n+4);assert(guarded);guarded[0]=0xa55a;guarded[n/2+1]=0x5aa5;return guarded+1;
}
esp_err_t esp_lcd_panel_draw_bitmap(esp_lcd_panel_handle_t p,int x0,int y0,int x1,int y1,const void *pixels) {
 assert(p);assert(x0==0&&x1==1024&&y0>=0&&y1<=768&&y1>y0);
 assert(pixels==fb+y0*1024);assert(guarded[0]==0xa55a&&guarded[786433]==0x5aa5);
 draws++;if(y0==0&&y1==768)full_draws++;last_y0=y0;last_y1=y1;return ESP_OK;
}
esp_err_t nvs_open(const char *ns,int mode,nvs_handle_t *h){assert(strcmp(ns,"mixui")==0);(void)mode;*h=1;return ESP_OK;}
esp_err_t nvs_get_u8(nvs_handle_t h,const char *key,uint8_t *v){(void)h;(void)key;*v=0;return ESP_OK;}
esp_err_t nvs_set_u8(nvs_handle_t h,const char *key,uint8_t v){(void)h;(void)key;(void)v;return ESP_OK;}
esp_err_t nvs_commit(nvs_handle_t h){(void)h;return ESP_OK;}
void nvs_close(nvs_handle_t h){(void)h;}
bool ttf_font_ready(void){return fake_font;}
int ttf_text_width(int size,const char *s){(void)size;return (int)strlen(s)*12;}
int ttf_draw_text(uint16_t *buf,int w,int h,int x,int y,int size,uint16_t color,const char *s){
 (void)size;(void)s;if(x>=0&&x<w&&y>=0&&y<h)buf[y*w+x]=color;return 12;
}
void ttf_draw_cell(uint16_t *buf,int w,int h,int x,int y,int cell_w,int cell_h,int size,uint16_t color,uint32_t cp,bool bold){
 (void)cp;(void)bold;assert(buf==fb&&w==1024&&h==768);
 assert((cell_w==12||cell_w==24)&&cell_h==24&&size==20);
 assert(x>=0&&x+cell_w<=w&&y>=0&&y+cell_h<=h);buf[y*w+x]=color;
}
int batt_log_get(batt_sample_t *out,int max){assert(max==720);for(int i=0;i<max;i++)out[i]=(batt_sample_t){.mv=(uint16_t)(3000+i),.ma=(int16_t)(i-360),.soc=(int8_t)(i%101),.plugged=1};return max;}
static void tap(int x,int y){mix_ui_touch(x,y,true);mix_ui_touch(x,y,false);}
static void no_action(void){mix_action_t a;assert(!mix_ui_take_action(&a));}
int main(void){
 mix_terminal_init();assert(mix_ui_init((void*)1)==ESP_OK);assert(allocations==1);
 mix_view_t v={0};mix_ui_tick(&v,0);assert(full_draws==1);
 unsigned n=draws;mix_ui_tick(&v,50);mix_ui_tick(&v,1000);assert(draws==n);
 for(int th=0;th<4;th++)for(int lang=0;lang<2;lang++)for(int p=0;p<5;p++){
  theme=(uint8_t)th;language=(uint8_t)lang;page=(page_t)p;repaint=true;mix_ui_tick(&v,1100);assert(!repaint);
 }
 page=TERMINAL;repaint=true;mix_ui_tick(&v,1200);mix_ui_tick(&v,1250);n=full_draws;
 const uint8_t sample[]="ASCII \xe4\xb8\xad\xe6\x96\x87 \033[31mred\033[0m";
 mix_terminal_feed(sample,sizeof(sample)-1);mix_ui_tick(&v,1300);
 assert(full_draws==n);assert(last_y0==32&&last_y1==56);
 assert(mix_terminal_row(0)[6].width==2&&mix_terminal_row(0)[7].width==0);
 assert(mix_ui_terminal_visible());
 /* A host-authorized A/B update asks for no local consent. Neither a firmware
  * notice nor terminal output may raise a modal or emit an action, so nothing
  * on the device has to be touched for an update to proceed. */
 mix_ui_notice("Host update ready; waiting for boot request");mix_ui_tick(&v,1325);
 assert(!modal&&mix_ui_terminal_visible());no_action();
 mix_ui_notice("Enter YES \033[confirm]");mix_terminal_feed((const uint8_t*)"YES\r\n",5);
 mix_ui_tick(&v,1350);mix_ui_key((const uint8_t*)"\033[A",3);mix_ui_key((const uint8_t*)"\r",1);
 assert(!modal&&mix_ui_terminal_visible());no_action();
 mix_action_t a;
 /* The transfer panel repaints once per changed percent while the image is
  * streaming, which is what keeps the progress bar moving between the
  * once-a-second telemetry snapshots. */
 page=SETTINGS;repaint=true;mix_ui_tick(&v,1400);n=full_draws;
 v.ota_state=1;v.ota_percent=7;mix_ui_tick(&v,1450);assert(full_draws==n+1);
 v.ota_percent=63;mix_ui_tick(&v,1500);assert(full_draws==n+2);
 mix_ui_tick(&v,1550);assert(full_draws==n+2); /* unchanged percent draws nothing */
 /* Progress on any other page must not steal a repaint. */
 page=HOME;repaint=true;mix_ui_tick(&v,1600);n=full_draws;
 v.ota_percent=88;mix_ui_tick(&v,1650);assert(full_draws==n);
 /* Trial and verified states ride the ordinary telemetry snapshot. */
 v.ota_state=0;v.ota_percent=0;v.firmware_on_trial=true;page=SETTINGS;repaint=true;
 mix_ui_tick(&v,2000);assert(!repaint&&view.firmware_on_trial&&!view.ota_state);
 v.firmware_on_trial=false;v.ota_state=2;mix_ui_tick(&v,3000);
 assert(view.ota_state==2&&!view.firmware_on_trial);
 v.ota_state=0;page=SETTINGS;repaint=true;mix_ui_tick(&v,3100);
 tap(720,492);assert(modal==MIX_ACTION_JOB_START);no_action();tap(600,476);assert(mix_ui_take_action(&a)&&a.kind==MIX_ACTION_JOB_START);
 page=DEVICE;repaint=true;mix_ui_tick(&v,3200);tap(500,450);assert(touch_test);mix_ui_touch(120,610,true);mix_ui_tick(&v,3250);assert(last_y0==416&&last_y1==680);mix_ui_touch(120,610,false);
 fake_font=false;for(int p=0;p<5;p++){page=(page_t)p;repaint=true;mix_ui_tick(&v,3300);}
 assert(allocations==1);assert(guarded[0]==0xa55a&&guarded[786433]==0x5aa5);free(guarded);
 puts("UI host integration passed: 40 combinations, dirty rows, CJK, A/B update panel, no local consent, touch, framebuffer guards");return 0;
}
