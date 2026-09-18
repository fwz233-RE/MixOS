/* SPDX-License-Identifier: MIT */
#include "mix_restart.h"
#include <stdatomic.h>
#include <stdbool.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "esp_system.h"
#include "usb_device_uac.h"

#define USB_DISCONNECT_MS 300u
static atomic_bool restart_claimed;

void mix_restart(void)
{
    /* Claim before any side effect; concurrent/repeated requests cannot renew
     * the delay or detach/restart twice (also testable with a returning stub). */
    if (atomic_exchange(&restart_claimed, true)) return;
    /* A not-yet-initialized controller has nothing to detach. Do not acquire
     * a logging lock on this terminal path, even in that fallback case. */
    (void)uac_device_disconnect_for_restart();
    /* FreeRTOS delays round down and may start just before the next tick.
     * Round UP plus one tick so the physical detach lasts at least 300 ms.
     * Keep MAIN/IO/OTA and idle watchdog subscriptions intact through restart.
     * No UI redraw, audio lock, NVS write, or unbounded USB queue wait here. */
    vTaskDelay((USB_DISCONNECT_MS + portTICK_PERIOD_MS - 1u) /
               portTICK_PERIOD_MS + 1u);
    esp_restart();
}
