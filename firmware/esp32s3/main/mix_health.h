/* SPDX-License-Identifier: MIT
 * Local progress/watchdog evidence. This never confirms an OTA image.
 */
#pragma once
#include <stdbool.h>
#include <stdint.h>
#include "esp_err.h"

#define MIX_HEALTH_PROGRESS_MS 3000u
#define MIX_HEALTH_STARTUP_MS 30000u
#define MIX_HEALTH_WATCHDOG_MS 15000u

typedef enum {
    MIX_HEALTH_MAIN = 0,
    MIX_HEALTH_IO,
    MIX_HEALTH_OTA,
    MIX_HEALTH_USB_START,
    MIX_HEALTH_TASK_COUNT
} mix_health_task_t;

/* First app_main call, before NVS/board/font/USB setup. Requires the CPU0 CORE
 * startup hook to have armed the RTC watchdog before constructors/scheduler.
 * The single nominal MIX_HEALTH_STARTUP_MS budget starts at that hook, NOT at
 * app_main: this call never feeds/rearms it. RTC coverage continues until an
 * explicit progress-based handoff to subscribed task watchdogs. The hook cannot
 * extend the old bootloader's budget or protect time before it executes. */
esp_err_t mix_health_init(void);
/* Same-task-only, checked wrappers. Begin does not count as progress. Reset
 * after completed work or a bounded queue poll, even without a connected host.
 * Long-lived IO and OTA workers must use bounded waits (at most 1000 ms),
 * subscribe for their lifetime and reset after each completed iteration.
 * The 3000 ms freshness window is stricter than the 15000 ms panic timeout.
 * Never feed another task from a timer/idle task. End before task deletion. */
esp_err_t mix_watchdog_task_begin(mix_health_task_t role);
esp_err_t mix_watchdog_task_reset(mix_health_task_t role);
esp_err_t mix_watchdog_task_end(mix_health_task_t role);

/* Main-task-only boot-failure policy, backed by a per-image RTC retained
 * record (not an OTA confirmed-state journal and not persistent on power loss).
 * A trial is always restarted on initialization failure, for bootloader rollback.
 * Repeated failures in an already valid image select USB-only maintenance. */
bool mix_health_maintenance_required(void);
void mix_health_record_failure(const char *stage, esp_err_t error);
void mix_health_note_stable(uint32_t now_ms, bool local_healthy);

/* USB starter writes its result; main may read it concurrently. ESP_OK means
 * uac_device_init succeeded, not that the physical link or peer is healthy. */
void mix_health_usb_result(esp_err_t result);
bool mix_health_usb_ready(void);
bool mix_health_usb_finished(esp_err_t *result);

/* Handoff succeeds only after main, IO and OTA tasks have subscribed AND made
 * recent progress, and USB initialization succeeded. There is no RTC feed loop.
 * The main loop calls this after render (or a maintenance service iteration). */
bool mix_health_handoff(uint32_t now_ms);
bool mix_health_progress_ok(uint32_t now_ms);
/* Explicit terminal initialization failures in VALID maintenance mode can
 * hand off to the subscribed main task alone to avoid a reset storm when the
 * USB stack itself cannot be allocated. This does not make local health true. */
bool mix_health_degraded_handoff(uint32_t now_ms);
