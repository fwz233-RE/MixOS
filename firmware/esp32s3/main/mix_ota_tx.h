/* SPDX-License-Identifier: MIT */
#pragma once
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include "esp_err.h"
#define MIX_OTA_CAP_BYTES 36
#define MIX_OTA_REQUEST_BYTES 72
#define MIX_OTA_RESPONSE_BYTES 192
#define MIX_OTA_V2 2
#define MIX_OTA_FEATURES 31
#define MIX_OTA_FEATURE_HEALTH_ACK 0x10u
#define MIX_OTA_FLAG_HEALTH_CHALLENGE 0x08u
#define MIX_OTA_FLAG_HEALTH_ACKED 0x10u

/* HEALTH_ACK (8), same 72-byte request / 192-byte response:
 * VERIFY_RUNNING OK returns flags HEALTH_CHALLENGE and ASCII message
 * "health-challenge:%08x". Only a successfully queued exact measurement can
 * offer this nonzero random challenge (not peer authentication).
 * Host must check its file SHA/length, ELF, running/boot slots and boot_id.
 * ACK keeps that measurement's transaction ID, size, boot_id and target, but
 * request [24:56] is the expected ELF SHA and [68:72] is the challenge (LE).
 * ACK request_id must be newer than the challenge-issuing VERIFY request_id.
 * ACK response uses the ORIGINAL file-SHA binding, not the request's ELF SHA;
 * host must check that binding explicitly rather than generic request equality.
 * OK + HEALTH_ACKED proves acceptance; repeated ACK/VERIFY is idempotent and
 * never extends/restarts the 20-second local+host health hold. Credentials are
 * volatile and scoped to boot, link generation and OTA request session.
 * Session changes in submitted v2/legacy OTA requests revoke credentials even
 * when switching away and back before the worker runs. CAPS/IDENTIFY are
 * read-only link snapshots, not proof or OTA-session transitions here.
 * Malformed VERIFY/ACK also revoke an accepted proof, including zero tokens.
 * [68:72] remains zero for every other operation. No wire lengths change. */

enum { MIX_TX_BEGIN=1, MIX_TX_END, MIX_TX_QUERY, MIX_TX_REBOOT, MIX_TX_ABORT,
       MIX_TX_VERIFY_RUNNING, MIX_TX_RELEASE_BASELINE, MIX_TX_HEALTH_ACK };
enum { MIX_TX_IDLE=0, MIX_TX_RECEIVING, MIX_TX_HASH_CHECK, MIX_TX_IMAGE_VALIDATE,
       MIX_TX_SELECT_INTENT, MIX_TX_BOOT_SELECTED, MIX_TX_REBOOT_REQUESTED,
       MIX_TX_RUNNING_PENDING, MIX_TX_CONFIRMED, MIX_TX_FAILED, MIX_TX_ABORTED,
       MIX_TX_VERIFYING_RUNNING };
enum { MIX_TX_OK=0, MIX_TX_BUSY, MIX_TX_CONFLICT, MIX_TX_REFUSED, MIX_TX_NOT_FOUND, MIX_TX_ERROR };
typedef struct {
    uint32_t generation, session, maintenance_revision;
    uint8_t type;
    uint16_t length;
    uint8_t payload[512];
} mix_ota_reply_t;
esp_err_t mix_ota_init_worker(void);
bool mix_ota_submit_legacy(uint8_t type, uint32_t session, const uint8_t *payload, size_t len);
bool mix_ota_submit_request(uint32_t session, const uint8_t *payload, size_t len);
bool mix_ota_poll_reply(mix_ota_reply_t *reply);
size_t mix_ota_capabilities(uint8_t *out, size_t cap);
void mix_ota_link_lost(void);
bool mix_ota_transaction_busy(void);
bool mix_ota_take_worker_restart(void);
#ifdef MIX_OTA_HOST_TEST
/* Execute the same production dispatch and periodic work deterministically. */
bool mix_ota_worker_step(void);
void mix_ota_test_reboot(void);
#endif
