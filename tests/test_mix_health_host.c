/* Host-only fault injection for mix_health.c; real IDF APIs are stubbed. */
#include <assert.h>
#include <stdio.h>
#include <string.h>
#include "mix_health.c"

static TaskHandle_t current=(void *)1;
static uint32_t tick;
static esp_reset_reason_t reason=ESP_RST_POWERON;
static esp_app_desc_t image={.app_elf_sha256={1,2,3}};
static bool subscribed[16],rtc_on=true,wdt_exists=true,unlocked,early_running;
static unsigned disables,enables,feeds,reset_calls,config_calls,rtc_age;
static uint32_t slow_hz=32768;
static unsigned stages;
static esp_err_t next_add=ESP_OK,next_reset=ESP_OK,next_end=ESP_OK;
static esp_err_t next_config=ESP_OK,next_init=ESP_OK;

const char *esp_err_to_name(esp_err_t e){assert(!early_running);(void)e;return "stub";}
const esp_app_desc_t *esp_app_get_description(void){assert(!early_running);return &image;}
esp_reset_reason_t esp_reset_reason(void){assert(!early_running);return reason;}
int64_t esp_timer_get_time(void){assert(!early_running);return (int64_t)tick*1000;}
TaskHandle_t xTaskGetCurrentTaskHandle(void){assert(!early_running);return current;}
static unsigned task_index(void){return (unsigned)(uintptr_t)current;}
esp_err_t esp_task_wdt_reconfigure(const esp_task_wdt_config_t *c){
    assert(!early_running);config_calls++;
    assert(c->trigger_panic&&c->timeout_ms==15000&&c->idle_core_mask==3);
    if(next_config!=ESP_OK)return next_config;
    return wdt_exists?ESP_OK:ESP_ERR_INVALID_STATE;
}
esp_err_t esp_task_wdt_init(const esp_task_wdt_config_t *c){
    assert(!early_running);
    assert(c->trigger_panic);if(next_init!=ESP_OK)return next_init;
    wdt_exists=true;return ESP_OK;
}
esp_err_t esp_task_wdt_add(TaskHandle_t h){
    assert(!h);if(next_add!=ESP_OK)return next_add;
    assert(!subscribed[task_index()]);subscribed[task_index()]=true;return ESP_OK;
}
esp_err_t esp_task_wdt_reset(void){
    assert(subscribed[task_index()]);reset_calls++;return next_reset;
}
esp_err_t esp_task_wdt_delete(TaskHandle_t h){
    assert(!h);if(next_end!=ESP_OK)return next_end;
    assert(subscribed[task_index()]);subscribed[task_index()]=false;return ESP_OK;
}
uint32_t rtc_clk_slow_freq_get_hz(void){assert(early_running);return slow_hz;}
void wdt_hal_write_protect_disable(wdt_hal_context_t *c){(void)c;assert(!unlocked);unlocked=true;}
void wdt_hal_write_protect_enable(wdt_hal_context_t *c){(void)c;assert(unlocked);unlocked=false;}
void wdt_hal_config_stage(wdt_hal_context_t *c,int stage,uint32_t ticks,int action){
    (void)c;assert(early_running&&unlocked&&rtc_on&&ticks==30u*slow_hz);
    assert((stage==WDT_STAGE0&&action==WDT_STAGE_ACTION_RESET_SYSTEM)||
           (stage==WDT_STAGE1&&action==WDT_STAGE_ACTION_RESET_RTC));
    assert(!(stages&(1u<<stage)));stages|=1u<<stage;
}
void wdt_hal_enable(wdt_hal_context_t *c){
    (void)c;assert(early_running&&unlocked&&rtc_on&&stages==3);
    /* Real IDF HAL enable implicitly feeds; model that instead of claiming 0 feeds. */
    rtc_on=true;enables++;feeds++;rtc_age=0;
}
void wdt_hal_disable(wdt_hal_context_t *c){(void)c;assert(!early_running&&unlocked);rtc_on=false;disables++;}
void wdt_hal_feed(wdt_hal_context_t *c){(void)c;assert(0&&"no explicit RTC feed allowed");}

static esp_err_t early_hook(void){
    assert(esp_system_init_fn_mix_health_early_rtc.cores==BIT(0));
    assert(esp_system_init_fn_mix_health_early_rtc.stage==ESP_SYSTEM_INIT_STAGE_CORE);
    boot_record_t before=retained;
    early_running=true;
    esp_err_t e=esp_system_init_fn_mix_health_early_rtc.fn();
    early_running=false;
    assert(!memcmp(&before,&retained,sizeof(retained))); /* no retained-policy work before RTOS */
    assert(!unlocked);return e;
}
static void reset_boot(esp_reset_reason_t r){
    /* Simulate BSS reinitialization, retaining only RTC_NOINIT storage. */
    startup_armed=initialized=handed_off=failure_noted=stable_started=false;stable_since=0;
    memset(tasks,0,sizeof(tasks));usb_finished=false;usb_error=ESP_ERR_INVALID_STATE;
    memset(subscribed,0,sizeof(subscribed));current=(void *)1;tick=0;reason=r;
    next_add=next_reset=next_end=next_config=next_init=ESP_OK;
    disables=enables=feeds=reset_calls=config_calls=stages=0;
    rtc_on=true;unlocked=early_running=false;slow_hz=32768;rtc_age=8999;
}
static void boot(esp_reset_reason_t r){
    reset_boot(r);
    assert(early_hook()==ESP_OK&&startup_armed);
    assert(rtc_on&&rtc_age==0&&enables==1&&disables==0&&feeds==1&&!config_calls);
    /* Constructors, secondary init and scheduler consume the same interval. */
    rtc_age=7000;
    assert(mix_health_init()==ESP_OK);assert(subscribed[1]&&rtc_on);
    assert(rtc_age==7000&&enables==1&&disables==0&&feeds==1);
}
static void progress(mix_health_task_t role,unsigned task){
    current=(void *)(uintptr_t)task;
    assert(mix_watchdog_task_reset(role)==ESP_OK);
}
static void test_early_startup(void){
    reset_boot(ESP_RST_POWERON);
    assert(mix_health_init()==ESP_ERR_INVALID_STATE); /* missing startup descriptor is fatal */
    assert(!config_calls&&!enables&&!feeds&&rtc_on);
    assert(!mix_health_handoff(0)&&!mix_health_degraded_handoff(0));

    const uint32_t invalid_hz[]={0,UINT32_MAX};
    for(unsigned i=0;i<sizeof(invalid_hz)/sizeof(invalid_hz[0]);i++){
        reset_boot(ESP_RST_POWERON);slow_hz=invalid_hz[i];
        assert(early_hook()==ESP_ERR_INVALID_STATE);
        assert(!startup_armed&&!stages&&!feeds&&!enables&&!disables&&rtc_on);
        assert(mix_health_init()==ESP_ERR_INVALID_STATE);
    }
    const uint32_t valid_hz[]={32768,136000,17500000u/256u};
    for(unsigned i=0;i<sizeof(valid_hz)/sizeof(valid_hz[0]);i++){
        reset_boot(ESP_RST_POWERON);slow_hz=valid_hz[i];
        assert(early_hook()==ESP_OK&&rtc_on);
        rtc_age=MIX_HEALTH_STARTUP_MS-1;
        assert(early_hook()==ESP_ERR_INVALID_STATE); /* no second feed */
        assert(mix_health_init()==ESP_OK);
        assert(mix_health_init()==ESP_ERR_INVALID_STATE);
        assert(rtc_age==MIX_HEALTH_STARTUP_MS-1&&feeds==1&&enables==1&&!disables);
        /* Without progress/handoff the original deadline still expires; this
         * models elapsed budget only, not the physical reset signal. */
        rtc_age++;
        assert(rtc_on&&rtc_age>=MIX_HEALTH_STARTUP_MS);
        assert(!mix_health_handoff(0)&&!mix_health_degraded_handoff(0));
    }
    /* Every task watchdog setup failure leaves the original RTC budget intact. */
    for(unsigned i=0;i<3;i++){
        reset_boot(ESP_RST_POWERON);assert(early_hook()==ESP_OK);rtc_age=1234;
        if(i==0)next_config=ESP_FAIL;
        if(i==1){wdt_exists=false;next_init=ESP_FAIL;}
        if(i==2){wdt_exists=true;next_add=ESP_FAIL;}
        assert(mix_health_init()==ESP_FAIL);
        assert(rtc_age==1234&&rtc_on&&feeds==1&&enables==1&&!disables);
        mix_health_record_failure("task watchdog",ESP_FAIL);
        assert(!mix_health_handoff(0)&&!mix_health_degraded_handoff(0));
    }
    wdt_exists=true;
    puts("early RTC startup takeover passed");
}
int main(void){
    test_early_startup();
    boot(ESP_RST_POWERON);
    assert(!mix_health_maintenance_required());
    assert(!mix_health_handoff(0));
    mix_health_usb_result(ESP_OK);
    assert(!mix_health_progress_ok(0)); /* registration alone is insufficient */
    progress(MIX_HEALTH_MAIN,1);
    current=(void *)2;assert(mix_watchdog_task_begin(MIX_HEALTH_IO)==ESP_OK);
    current=(void *)3;assert(mix_watchdog_task_begin(MIX_HEALTH_OTA)==ESP_OK);
    assert(!mix_health_handoff(0));
    progress(MIX_HEALTH_IO,2);assert(!mix_health_handoff(0));
    progress(MIX_HEALTH_OTA,3);assert(mix_health_handoff(0)&&!rtc_on);
    assert(mix_health_handoff(0)&&disables==1&&feeds==1);
    assert(!mix_health_progress_ok(MIX_HEALTH_PROGRESS_MS+1));

    current=(void *)2;
    unsigned before=reset_calls;
    assert(mix_watchdog_task_reset(MIX_HEALTH_MAIN)==ESP_ERR_INVALID_STATE);
    assert(reset_calls==before); /* one task cannot mask another task's stall */
    assert(mix_watchdog_task_begin(MIX_HEALTH_MAIN)==ESP_ERR_INVALID_STATE);
    assert(mix_watchdog_task_begin(MIX_HEALTH_USB_START)==ESP_ERR_INVALID_STATE);
    assert(mix_watchdog_task_end(MIX_HEALTH_MAIN)==ESP_ERR_INVALID_STATE);
    next_reset=ESP_FAIL;tick=5000;
    assert(mix_watchdog_task_reset(MIX_HEALTH_IO)==ESP_FAIL);
    assert(tasks[MIX_HEALTH_IO].at==0);next_reset=ESP_OK;
    next_end=ESP_FAIL;assert(mix_watchdog_task_end(MIX_HEALTH_IO)==ESP_FAIL);
    assert(tasks[MIX_HEALTH_IO].owner==current);next_end=ESP_OK;
    assert(mix_watchdog_task_end(MIX_HEALTH_IO)==ESP_OK);
    assert(!mix_health_progress_ok(tick));
    next_add=ESP_ERR_NO_MEM;assert(mix_watchdog_task_begin(MIX_HEALTH_IO)==ESP_ERR_NO_MEM);
    assert(!tasks[MIX_HEALTH_IO].owner);next_add=ESP_OK;
    assert(mix_watchdog_task_begin(MIX_HEALTH_IO)==ESP_OK);
    assert(mix_watchdog_task_reset((mix_health_task_t)99)==ESP_ERR_INVALID_ARG);

    tick=UINT32_MAX-30;
    progress(MIX_HEALTH_MAIN,1);progress(MIX_HEALTH_IO,2);progress(MIX_HEALTH_OTA,3);
    assert(mix_health_progress_ok(15)); /* wrap-safe age */
    assert(!mix_health_progress_ok(MIX_HEALTH_PROGRESS_MS+20));
    /* Another CPU may publish after main sampled its clock. */
    tick=16;progress(MIX_HEALTH_MAIN,1);progress(MIX_HEALTH_IO,2);progress(MIX_HEALTH_OTA,3);
    assert(mix_health_progress_ok(15));

    boot(ESP_RST_TASK_WDT);assert(retained.failures==1);
    boot(ESP_RST_WDT);assert(mix_health_maintenance_required());
    image.app_elf_sha256[0]++;
    boot(ESP_RST_SW);assert(retained.failures==0); /* image-scoped record */
    boot(ESP_RST_SW);assert(retained.failures==1); /* failed uncompleted boot */
    mix_health_record_failure("LCD draw",ESP_FAIL);assert(retained.failures==2);
    mix_health_record_failure("USB",ESP_ERR_NO_MEM);assert(retained.failures==2);
    progress(MIX_HEALTH_MAIN,1);
    assert(mix_health_degraded_handoff(0));
    assert(!mix_health_usb_ready()&&!mix_health_progress_ok(0));
    boot(ESP_RST_SW);assert(retained.failures==2); /* explicit failure counted once */
    assert(!mix_health_degraded_handoff(0)); /* never silently bypass startup */

    boot(ESP_RST_POWERON);assert(retained.failures==0);
    boot(ESP_RST_PANIC);assert(retained.failures==1);
    mix_health_note_stable(0,true);mix_health_note_stable(STABLE_MS-1,true);
    assert(retained.failures==1);
    mix_health_note_stable(STABLE_MS,false);
    mix_health_note_stable(STABLE_MS+1,true);
    mix_health_note_stable(STABLE_MS*2,true);assert(retained.failures==1);
    mix_health_note_stable(STABLE_MS*2+1,true);assert(retained.failures==0&&!retained.boot_open);
    boot(ESP_RST_SW);assert(retained.failures==0);
    mix_health_note_stable(0,true);mix_health_note_stable(STABLE_MS,true);
    boot(ESP_RST_TASK_WDT);assert(retained.failures==1); /* post-stability panic */
    retained.check^=1;boot(ESP_RST_SW);assert(retained.failures==0);
    wdt_exists=false;boot(ESP_RST_POWERON);assert(wdt_exists);
    mix_health_usb_result(ESP_FAIL);esp_err_t e=ESP_OK;
    assert(mix_health_usb_finished(&e)&&e==ESP_FAIL&&!mix_health_usb_ready());
    assert(!mix_health_handoff(0));
    puts("health host fault injection passed");return 0;
}
