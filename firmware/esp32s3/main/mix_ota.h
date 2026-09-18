/* SPDX-License-Identifier: MIT
 * A/B firmware update over the existing USB CDC protocol.
 *
 * The image is streamed into the inactive OTA slot, checked against a host
 * SHA-256 and the ESP-IDF image format, and only then made bootable. The new
 * build starts in PENDING_VERIFY: confirmation requires sustained local
 * rendering/scanning/task progress plus fresh host protocol exchange and a
 * read-back of VALID. Local failure triggers rollback/reset; host absence alone
 * leaves confirmation pending. Normal updates avoid ROM and physical buttons;
 * recovery from arbitrary hardware/firmware hangs is not guaranteed by USB.
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
    MIX_OTA_VALIDATING,
    MIX_OTA_VALIDATED,
} mix_ota_state_t;

/* Called after NVS init, before the link and worker start. No flash state is
 * interpreted as confirmed unless the running image is actually VALID. */
void mix_ota_init(void);
esp_err_t mix_ota_init_worker(void);

/* Conservative: also true if running state cannot be read. */
bool mix_ota_pending_verify(void);

/* Main reports progress only. The single OTA worker owns flash confirmation.
 * The first signal includes a real host exchange, the second is local health
 * independent of whether Linux is connected. Both samples expire. */
void mix_ota_health_tick(uint32_t now_ms, bool healthy);
void mix_ota_local_health_tick(uint32_t now_ms, bool healthy);
/* Worker supplies a current boot/link/session-bound exact-measurement ACK.
 * Heartbeat/identity traffic alone can never satisfy this third condition. */
void mix_ota_process_health(uint32_t now_ms, bool maintenance_confirmed);
void mix_ota_set_baseline_protection(bool protect);
bool mix_ota_baseline_protected(void);
uint8_t mix_ota_running_state(void);
uint8_t mix_ota_running_index(void);
uint8_t mix_ota_boot_index(void);
/* Worker-only flash operations. Validation and boot selection are separated so
 * the durable SELECT_INTENT is recorded before changing otadata. */
esp_err_t mix_ota_validate(uint8_t stored_sha[32]);
esp_err_t mix_ota_select(void);
esp_err_t mix_ota_hash_running(uint32_t bytes, uint8_t sha[32]);

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

/* Serialised answer to "which build is actually running right now": the slot,
 * its rollback state, the reset reason, and the image's own application
 * descriptor as the build system wrote it into the header.
 *
 * Nothing else on this board can answer that question. The ESP32 log goes to
 * UART0, which this hardware does not bring out, and - the reason this exists -
 * a build the bootloader has rolled back answers the USB handshake exactly like
 * the build that was just installed. Without this the host cannot tell a
 * successful update from one that was silently reverted, so `ota_esp.py` would
 * go on reporting "installed" either way.
 *
 * Layout, little-endian, MIX_OTA_IDENTITY_BYTES total:
 *   0   u8    format, 1
 *   1   u8    OTA slot index, 0 = ota_0, 1 = ota_1, 0xFF = not an OTA slot
 *   2   u8    esp_ota_img_states_t, 0xFF when unreadable
 *   3   u8    esp_reset_reason_t of the boot that is running
 *   4   u32   flash address of the running partition
 *   8   [32]  app_elf_sha256, the field that identifies the build exactly
 *   40  [16]  build date, NUL padded
 *   56  [16]  build time, NUL padded
 *   72  [32]  version, NUL padded
 *   104 [32]  project name, NUL padded
 *
 * Returns the bytes written, or 0 when `cap` is too small. */
#define MIX_OTA_IDENTITY_BYTES 136
size_t mix_ota_identity(uint8_t *out, size_t cap);

/* Bounded ASCII reason for the last failure; empty when there is none. */
const char *mix_ota_error(void);
