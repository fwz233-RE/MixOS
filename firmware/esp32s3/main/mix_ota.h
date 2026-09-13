/* SPDX-License-Identifier: MIT
 * A/B firmware update over the existing USB CDC protocol.
 *
 * The image is streamed into the inactive OTA slot, checked against a host
 * SHA-256 and the ESP-IDF image format, and only then made bootable. The new
 * build starts in PENDING_VERIFY: it must prove it reached a working UI and a
 * mounted USB device, otherwise the bootloader rolls back to the slot that was
 * running before. No ROM download mode, esptool or physical button is needed.
 */
#pragma once
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include "esp_err.h"

/* MIX_MAX_PAYLOAD minus the 4-byte absolute offset carried by every OTA_DATA. */
#define MIX_OTA_CHUNK 508
/* Chunks the host may keep in flight, and how often the ESP acknowledges. */
#define MIX_OTA_WINDOW 8
#define MIX_OTA_ACK_EVERY 4
/* A transfer that stalls this long is abandoned so the slot is reusable. */
#define MIX_OTA_STALL_MS 15000

typedef enum {
    MIX_OTA_IDLE = 0,
    MIX_OTA_RECEIVING,
    MIX_OTA_READY_TO_BOOT,
    MIX_OTA_FAILED,
} mix_ota_state_t;

/* Call once from app_main before the link starts. Records rollback state. */
void mix_ota_init(void);

/* True while the running build still has to prove itself. */
bool mix_ota_pending_verify(void);

/* Call from the main loop. `healthy` must mean "this build reached a working
 * local UI and a mounted USB device". Reaching it for long enough confirms the
 * new build; a build that instead crashes or hangs is rolled back by the
 * bootloader on the next reset without any help from here. */
void mix_ota_health_tick(uint32_t now_ms, bool healthy);

esp_err_t mix_ota_begin(uint32_t image_size, const uint8_t sha256[32]);
esp_err_t mix_ota_write(uint32_t offset, const uint8_t *data, size_t len);
esp_err_t mix_ota_finish(void);
void mix_ota_abort(void);
/* Clears a finished failure back to idle. A running transfer and an image
 * already staged for the next boot are left untouched. */
void mix_ota_reset(void);
/* Drops a transfer that stopped mid-flight; call once per main-loop tick. */
void mix_ota_tick(uint32_t now_ms);

mix_ota_state_t mix_ota_state(void);
uint32_t mix_ota_received(void);
uint32_t mix_ota_total(void);
int mix_ota_percent(void);
const char *mix_ota_running_slot(void);
const char *mix_ota_target_slot(void);
/* Bounded ASCII reason for the last failure; empty when there is none. */
const char *mix_ota_error(void);
