/* Compiles actual maintenance/RGB/startup functions extracted from main.c. */
#include <assert.h>
#include <setjmp.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include "esp_err.h"
#include "mix_health.h"
#include "mix_view.h"
#include "freertos/FreeRTOS.h"

typedef void *esp_lcd_panel_handle_t;
typedef struct { int unused; } esp_lcd_rgb_panel_event_data_t;
static const char *TAG="main_test";
static portMUX_TYPE rgb_lock,data_lock;
static uint32_t rgb_vsyncs,rgb_frames;
static bool maintenance_mode,usb_audio_allowed,device_service_enabled;
static bool link_initialized,ota_worker_ready;
static mix_view_t view;
static bool pending,usb_finished=true,restart_requested,boot_requested;
static esp_err_t usb_error=ESP_OK;
static jmp_buf escape;
static unsigned now,iterations,link_ticks,resets,local_ticks,handoffs,degraded,recorded,starts;
static unsigned forced_download;
#define RTC_CNTL_OPTION1_REG 1
#define RTC_CNTL_FORCE_DOWNLOAD_BOOT 8
#define REG_WRITE(reg,value) do {(void)(reg);forced_download=(value);}while(0)
#define ESP_LOGW(tag,...) do{(void)(tag);}while(0)
static uint32_t clock_ms(void){return now;}
static bool mix_ota_pending_verify(void){return pending;}
void mix_health_record_failure(const char *s,esp_err_t e){assert(s&&e!=ESP_OK);recorded++;}
static void esp_restart(void){longjmp(escape,1);}
/* The reboot executor is compiled independently by test_mix_restart.py. */
static void mix_restart(void){now+=300;esp_restart();}
static void vTaskDelay(unsigned ticks){now+=ticks;if(ticks==20&&++iterations==3)longjmp(escape,2);}
static void start_usb(bool with_audio){assert(!with_audio);starts++;}
static void mix_link_tick(uint32_t ms,mix_view_t *v){assert(ms==now&&v==&view);link_ticks++;}
static bool mix_link_take_restart_request(void){return restart_requested;}
static bool mix_link_take_boot_request(void){return boot_requested;}
bool mix_health_usb_finished(esp_err_t *e){*e=usb_error;return usb_finished;}
esp_err_t mix_watchdog_task_reset(mix_health_task_t role){assert(role==MIX_HEALTH_MAIN);resets++;return ESP_OK;}
static void mix_ota_local_health_tick(uint32_t ms,bool healthy){assert(ms==now&&!healthy);local_ticks++;}
bool mix_health_handoff(uint32_t ms){assert(ms==now);handoffs++;return true;}
bool mix_health_degraded_handoff(uint32_t ms){assert(ms==now);degraded++;return true;}
#include "main_health_functions.h"

static void reset_case(void){
    pending=maintenance_mode=restart_requested=boot_requested=false;
    usb_audio_allowed=device_service_enabled=link_initialized=ota_worker_ready=true;
    usb_finished=true;usb_error=ESP_OK;
    now=iterations=link_ticks=resets=local_ticks=handoffs=degraded=recorded=starts=forced_download=0;
}
int main(void){
    /* Initial callback counts before the first submitted frame are insufficient. */
    rgb_vsyncs=rgb_frames=10;assert(!rgb_progress_healthy(0));
    rgb_vsync(NULL,NULL,NULL);assert(!rgb_progress_healthy(10));
    rgb_frame_complete(NULL,NULL,NULL);assert(rgb_progress_healthy(20));
    assert(!rgb_progress_healthy(1021));
    rgb_vsync(NULL,NULL,NULL);assert(!rgb_progress_healthy(1022));
    rgb_frame_complete(NULL,NULL,NULL);assert(rgb_progress_healthy(1023));
    rgb_vsyncs=rgb_frames=UINT32_MAX;assert(rgb_progress_healthy(UINT32_MAX-10));
    rgb_vsync(NULL,NULL,NULL);rgb_frame_complete(NULL,NULL,NULL);
    assert(rgb_progress_healthy(20));assert(!rgb_progress_healthy(1021));

    reset_case();pending=true;
    int exit=setjmp(escape);
    if(!exit){startup_failure("LCD",ESP_FAIL);assert(0);}
    assert(exit==1&&recorded==1&&now==200); /* trial is recorded before restart */
    reset_case();startup_failure("LCD",ESP_FAIL);
    assert(maintenance_mode&&recorded==1&&now==0); /* VALID never loops resets */
    reset_case();exit=setjmp(escape);
    if(!exit)maintenance_loop();
    assert(exit==2&&starts==1&&link_ticks==3&&local_ticks==3&&resets==3&&handoffs==3);
    assert(!degraded&&!recorded&&!usb_audio_allowed&&!device_service_enabled);
    /* No host traffic was required, and no display/font/audio setup ran. */
    reset_case();usb_error=ESP_FAIL;exit=setjmp(escape);
    if(!exit)maintenance_loop();
    assert(exit==2&&recorded==1&&degraded==3&&!handoffs);
    reset_case();link_initialized=false;ota_worker_ready=false;exit=setjmp(escape);
    if(!exit)maintenance_loop();
    assert(exit==2&&!starts&&!link_ticks&&degraded==3);
    reset_case();restart_requested=true;exit=setjmp(escape);
    if(!exit)maintenance_loop();
    assert(exit==1&&now==300);
    reset_case();boot_requested=true;exit=setjmp(escape);
    if(!exit)maintenance_loop();
    assert(exit==1&&now==100&&forced_download==RTC_CNTL_FORCE_DOWNLOAD_BOOT);
    puts("main maintenance and scanout fault injection passed");return 0;
}
