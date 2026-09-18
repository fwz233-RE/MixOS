/* SPDX-License-Identifier: MIT */
#include "mix_health.h"
#include <stddef.h>
#include <stdio.h>
#include <string.h>
#include "esp_app_desc.h"
#include "esp_attr.h"
#include "esp_idf_version.h"
#include "esp_log.h"
#include "esp_private/startup_internal.h"
#include "esp_system.h"
#include "esp_task_wdt.h"
#include "esp_timer.h"
#include "hal/wdt_hal.h"
#include "soc/rtc.h"

/* Private startup/HAL contract audited against the local ESP-IDF 5.4.2 S3
 * sources. Fail the build rather than silently lose pre-scheduler coverage. */
#if !CONFIG_IDF_TARGET_ESP32S3 || ESP_IDF_VERSION != ESP_IDF_VERSION_VAL(5, 4, 2)
#error "Re-audit early RTC watchdog startup ordering for this target/IDF version"
#endif
#if !CONFIG_BOOTLOADER_WDT_ENABLE || !CONFIG_BOOTLOADER_WDT_DISABLE_IN_USER_CODE
#error "RTC startup watchdog requires BOOTLOADER_WDT_ENABLE and BOOTLOADER_WDT_DISABLE_IN_USER_CODE"
#endif

static const char *TAG = "mix_health";
#define RECORD_MAGIC 0x4d484c32u
#define FAILURE_LIMIT 2u
#define STABLE_MS 60000u

typedef struct {
    uint32_t magic;
    uint8_t image[32];
    uint32_t failures, boot_open;
    esp_err_t last_error;
    char stage[32];
    uint32_t check;
} boot_record_t;
static RTC_NOINIT_ATTR boot_record_t retained;
static bool startup_armed;
static bool initialized, handed_off, failure_noted, stable_started;
static uint32_t stable_since;
static portMUX_TYPE lock = portMUX_INITIALIZER_UNLOCKED;
typedef struct { TaskHandle_t owner; uint32_t at; bool progressed; } task_progress_t;
static task_progress_t tasks[MIX_HEALTH_TASK_COUNT];
static bool usb_finished;
static esp_err_t usb_error = ESP_ERR_INVALID_STATE;

static uint32_t now_ms(void) { return (uint32_t)(esp_timer_get_time()/1000); }
static uint32_t checksum(void) {
    const uint8_t *p = (const uint8_t *)&retained;
    uint32_t sum = 2166136261u;
    for (size_t i=0; i<offsetof(boot_record_t, check); ++i) sum=(sum^p[i])*16777619u;
    return sum;
}
static void store_record(void) { retained.check=checksum(); }
static bool fault_reset(esp_reset_reason_t reason) {
    return reason==ESP_RST_PANIC || reason==ESP_RST_INT_WDT ||
           reason==ESP_RST_TASK_WDT || reason==ESP_RST_WDT;
}

/* IDF 5.4.2/S3: cpu_start.c clears BSS, sets up PSRAM/cache and calls
 * esp_clk_init() before SYS_STARTUP_FN -> start_cpu0_default -> CORE hooks.
 * Thus the slow clock and memory are ready at CORE priority 0; only register
 * HAL calls and stack/BSS are used here (no heap, logging, locks or RTOS).
 * Priority 0 precedes the first IDF CORE hook (1), all constructors, SECONDARY
 * hooks and scheduler startup. CONFIG_BOOTLOADER_WDT_DISABLE_IN_USER_CODE
 * removes IDF's SECONDARY/999 disable hook, keeping this watchdog running.
 *
 * Keep this hook in mix_health_init's translation unit: app_main's live call
 * pulls the object from the archive; ESP_SYSTEM_INIT_FN marks the descriptor
 * used and S3 sections.ld.in KEEP/SORT_BY_INIT_PRIORITY retains it and its fn.
 *
 * This cannot cover old-bootloader/image loading or port init before the hook.
 * In particular esp_clk_init already feeds/reconfigures stage 0 using the APP
 * CONFIG_BOOTLOADER_WDT_TIME_MS after changing the slow clock; it is earlier
 * than this hook, but still too late to extend the old bootloader's own time.
 */
ESP_SYSTEM_INIT_FN(mix_health_early_rtc, CORE, BIT(0), 0)
{
    if (startup_armed) return ESP_ERR_INVALID_STATE; /* Never renew the budget. */
    uint64_t ticks=(uint64_t)MIX_HEALTH_STARTUP_MS*rtc_clk_slow_freq_get_hz()/1000u;
    if (ticks<2 || ticks>UINT32_MAX) return ESP_ERR_INVALID_STATE;
    wdt_hal_context_t rtc=RWDT_HAL_CONTEXT_DEFAULT();
    wdt_hal_write_protect_disable(&rtc);
    wdt_hal_config_stage(&rtc,WDT_STAGE0,(uint32_t)ticks,WDT_STAGE_ACTION_RESET_SYSTEM);
    wdt_hal_config_stage(&rtc,WDT_STAGE1,(uint32_t)ticks,WDT_STAGE_ACTION_RESET_RTC);
    /* enable also feeds once in IDF's HAL. No disable/re-init gap and no later
     * app_main rearm: one nominal 30 s interval covers all startup until task
     * handoff. Stage 1 is the last-resort RTC reset after another interval. */
    wdt_hal_enable(&rtc);
    wdt_hal_write_protect_enable(&rtc);
    startup_armed=true;
    return ESP_OK;
}

esp_err_t mix_health_init(void) {
    if (!startup_armed || initialized) return ESP_ERR_INVALID_STATE;
    const esp_app_desc_t *app=esp_app_get_description();
    esp_reset_reason_t reason=esp_reset_reason();
    /* Discard random/cold RTC memory and another image's failure count. */
    if (reason==ESP_RST_POWERON || reason==ESP_RST_BROWNOUT ||
        retained.magic!=RECORD_MAGIC || retained.check!=checksum() ||
        memcmp(retained.image,app->app_elf_sha256,sizeof(retained.image))) {
        memset(&retained,0,sizeof(retained));
        retained.magic=RECORD_MAGIC;
        memcpy(retained.image,app->app_elf_sha256,sizeof(retained.image));
    } else if ((retained.boot_open || fault_reset(reason)) && retained.failures<FAILURE_LIMIT) {
        retained.failures++;
        if(!retained.stage[0]){
            retained.last_error=ESP_ERR_TIMEOUT;
            snprintf(retained.stage,sizeof(retained.stage),"unfinished boot / reset %d",(int)reason);
        }
    }
    if (retained.failures)
        ESP_LOGW(TAG,"Prior local failures=%lu stage=%.31s error=%s reset=%d",
                 (unsigned long)retained.failures,retained.stage,
                 esp_err_to_name(retained.last_error),(int)reason);
    retained.boot_open=1;
    store_record();

    /* The early RTC interval remains active during task-WDT setup, including
     * failures. Only explicit progress-based handoff may disable it. */

    esp_task_wdt_config_t config={.timeout_ms=MIX_HEALTH_WATCHDOG_MS,
        .idle_core_mask=(1u<<portNUM_PROCESSORS)-1u,.trigger_panic=true};
    esp_err_t e=esp_task_wdt_reconfigure(&config);
    if (e==ESP_ERR_INVALID_STATE) e=esp_task_wdt_init(&config);
    if (e!=ESP_OK) return e;
    initialized=true;
    return mix_watchdog_task_begin(MIX_HEALTH_MAIN);
}

static bool valid_role(mix_health_task_t role) { return (unsigned)role<MIX_HEALTH_TASK_COUNT; }
esp_err_t mix_watchdog_task_begin(mix_health_task_t role) {
    if (!initialized || !valid_role(role)) return ESP_ERR_INVALID_ARG;
    TaskHandle_t self=xTaskGetCurrentTaskHandle();
    portENTER_CRITICAL(&lock);
    bool busy=tasks[role].owner!=NULL;
    for (unsigned i=0;i<MIX_HEALTH_TASK_COUNT;i++) busy|=tasks[i].owner==self;
    if (!busy) tasks[role].owner=self; /* Reserve before the allocating API. */
    portEXIT_CRITICAL(&lock);
    if (busy) return ESP_ERR_INVALID_STATE;
    esp_err_t e=esp_task_wdt_add(NULL);
    portENTER_CRITICAL(&lock);
    if (e!=ESP_OK) memset(&tasks[role],0,sizeof(tasks[role]));
    portEXIT_CRITICAL(&lock);
    return e;
}
esp_err_t mix_watchdog_task_reset(mix_health_task_t role) {
    if (!valid_role(role)) return ESP_ERR_INVALID_ARG;
    TaskHandle_t self=xTaskGetCurrentTaskHandle();
    portENTER_CRITICAL(&lock);
    bool ours=tasks[role].owner==self;
    portEXIT_CRITICAL(&lock);
    if (!ours) return ESP_ERR_INVALID_STATE;
    esp_err_t e=esp_task_wdt_reset();
    if (e==ESP_OK) {
        uint32_t at=now_ms();
        portENTER_CRITICAL(&lock);
        tasks[role].at=at;tasks[role].progressed=true;
        portEXIT_CRITICAL(&lock);
    }
    return e;
}
esp_err_t mix_watchdog_task_end(mix_health_task_t role) {
    if (!valid_role(role)) return ESP_ERR_INVALID_ARG;
    TaskHandle_t self=xTaskGetCurrentTaskHandle();
    portENTER_CRITICAL(&lock);
    bool ours=tasks[role].owner==self;
    portEXIT_CRITICAL(&lock);
    if (!ours) return ESP_ERR_INVALID_STATE;
    esp_err_t e=esp_task_wdt_delete(NULL);
    if (e==ESP_OK) {
        portENTER_CRITICAL(&lock);
        memset(&tasks[role],0,sizeof(tasks[role]));
        portEXIT_CRITICAL(&lock);
    }
    return e;
}

bool mix_health_maintenance_required(void) { return retained.failures>=FAILURE_LIMIT; }
void mix_health_record_failure(const char *stage,esp_err_t error) {
    if (!failure_noted && retained.failures<FAILURE_LIMIT) retained.failures++;
    failure_noted=true;retained.boot_open=0;retained.last_error=error;
    snprintf(retained.stage,sizeof(retained.stage),"%s",stage?stage:"unknown");
    store_record();
    ESP_LOGE(TAG,"Local failure: %s: %s",retained.stage,esp_err_to_name(error));
}
void mix_health_note_stable(uint32_t now,bool healthy) {
    if (!healthy) { stable_started=false;return; }
    if (!stable_started) { stable_started=true;stable_since=now; }
    if ((uint32_t)(now-stable_since)<STABLE_MS || failure_noted) return;
    if (retained.failures || retained.boot_open) {
        retained.failures=0;retained.boot_open=0;retained.last_error=ESP_OK;
        retained.stage[0]=0;store_record();
    }
}
void mix_health_usb_result(esp_err_t result) {
    portENTER_CRITICAL(&lock);
    usb_error=result;usb_finished=true;
    portEXIT_CRITICAL(&lock);
}
bool mix_health_usb_ready(void) {
    portENTER_CRITICAL(&lock);
    bool ready=usb_finished && usb_error==ESP_OK;
    portEXIT_CRITICAL(&lock);
    return ready;
}
bool mix_health_usb_finished(esp_err_t *result) {
    portENTER_CRITICAL(&lock);
    bool finished=usb_finished;
    if (result) *result=usb_error;
    portEXIT_CRITICAL(&lock);
    return finished;
}
static bool fresh(mix_health_task_t role,uint32_t now) {
    /* The caller samples `now` before taking this lock. A concurrent worker
     * can publish a slightly newer timestamp; that is progress, not staleness. */
    return tasks[role].owner && tasks[role].progressed &&
           ((uint32_t)(now-tasks[role].at)<=MIX_HEALTH_PROGRESS_MS ||
            (uint32_t)(tasks[role].at-now)<=MIX_HEALTH_PROGRESS_MS);
}
bool mix_health_progress_ok(uint32_t now) {
    portENTER_CRITICAL(&lock);
    bool ok=fresh(MIX_HEALTH_MAIN,now) && fresh(MIX_HEALTH_IO,now) && fresh(MIX_HEALTH_OTA,now);
    portEXIT_CRITICAL(&lock);
    return ok;
}
static void disable_startup_watchdog(void) {
    if (handed_off) return;
    wdt_hal_context_t rtc=RWDT_HAL_CONTEXT_DEFAULT();
    wdt_hal_write_protect_disable(&rtc);
    wdt_hal_disable(&rtc);
    wdt_hal_write_protect_enable(&rtc);
    handed_off=true;
    ESP_LOGI(TAG,"RTC startup watchdog handed off to subscribed task watchdogs");
}
bool mix_health_handoff(uint32_t now) {
    if (!mix_health_usb_ready() || !mix_health_progress_ok(now)) return false;
    disable_startup_watchdog();return true;
}
bool mix_health_degraded_handoff(uint32_t now) {
    portENTER_CRITICAL(&lock);
    bool ok=fresh(MIX_HEALTH_MAIN,now);
    portEXIT_CRITICAL(&lock);
    if (!failure_noted || !ok) return false;
    disable_startup_watchdog();return true;
}
