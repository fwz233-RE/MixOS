/* SPDX-License-Identifier: MIT */
#include "mix_ota.h"

#include <stdio.h>
#include <string.h>

#include "esp_app_desc.h"
#include "esp_log.h"
#include "esp_ota_ops.h"
#include "esp_partition.h"
#include "mbedtls/sha256.h"
#include "sdkconfig.h"

static const char *TAG = "MixOTA";

static const esp_partition_t *s_running, *s_target;
static esp_ota_handle_t s_handle;
static bool s_open;
static mix_ota_state_t s_state;
static uint32_t s_total, s_received;
/* Stall detection works off observed progress, so the write path stays
 * timestamp-free and callable from anywhere the link runs. */
static uint32_t s_stall_mark, s_stall_ms;
static uint8_t s_expected[32];
static mbedtls_sha256_context s_sha;
static char s_error[128];

/* Rollback bookkeeping for the build that is currently running. */
static bool s_pending_verify, s_confirmed;
static uint32_t s_healthy_since, s_health_base;
static bool s_health_started;

/* A new build confirms itself once it has run this long with the USB host
 * mounted, which is the normal path and costs about twenty seconds. */
#define HEALTH_HOLD_MS 20000
/* It also confirms after this long of continuous running even with no USB host
 * at all, so a perfectly good build is never discarded just because the CM5 is
 * off or the USB route was switched. The genuinely dangerous cases - a crash,
 * a watchdog reset, a boot loop - reset the chip while the slot is still
 * PENDING_VERIFY, and the bootloader then rolls back by itself without the
 * application having to detect anything. */
#define HEALTH_CONFIRM_MS 90000

static void fail(const char *reason)
{
    if (s_open) {
        esp_ota_abort(s_handle);
        s_open = false;
    }
    mbedtls_sha256_free(&s_sha);
    s_state = MIX_OTA_FAILED;
    s_total = s_received = 0;
    snprintf(s_error, sizeof(s_error), "%s", reason);
    ESP_LOGE(TAG, "update failed: %s", reason);
}

/* Refuse before anything was opened. The reason still has to reach the host:
 * an empty ERROR payload is what turns a precise refusal such as a missing
 * OTA slot into an operator guessing at the serial cable. */
static esp_err_t refuse(const char *reason, esp_err_t err)
{
    snprintf(s_error, sizeof(s_error), "%s", reason);
    ESP_LOGW(TAG, "update refused: %s", reason);
    return err;
}

void mix_ota_init(void)
{
    s_running = esp_ota_get_running_partition();
    esp_ota_img_states_t state = ESP_OTA_IMG_UNDEFINED;
    if (s_running && esp_ota_get_state_partition(s_running, &state) == ESP_OK &&
        state == ESP_OTA_IMG_PENDING_VERIFY) {
        s_pending_verify = true;
        ESP_LOGW(TAG, "running %s on trial; rollback armed",
                 s_running->label);
    } else {
        s_confirmed = true;
    }
    const esp_app_desc_t *desc = esp_app_get_description();
    ESP_LOGI(TAG, "slot %s, app %s, built %s",
             s_running ? s_running->label : "?",
             desc ? desc->version : "?", desc ? desc->date : "?");
}

bool mix_ota_pending_verify(void) { return s_pending_verify && !s_confirmed; }

void mix_ota_health_tick(uint32_t now_ms, bool healthy)
{
    if (!s_health_started) {
        s_health_started = true;
        s_health_base = s_healthy_since = now_ms;
    }
    if (s_confirmed) return;
    if (!healthy) s_healthy_since = now_ms;
    if ((uint32_t)(now_ms - s_healthy_since) < HEALTH_HOLD_MS &&
        (uint32_t)(now_ms - s_health_base) < HEALTH_CONFIRM_MS)
        return;
    if (esp_ota_mark_app_valid_cancel_rollback() == ESP_OK) {
        ESP_LOGI(TAG, "trial build confirmed after %u ms; rollback disarmed",
                 (unsigned)(now_ms - s_health_base));
    } else {
        ESP_LOGW(TAG, "could not record confirmation");
    }
    s_confirmed = true;
}

esp_err_t mix_ota_begin(uint32_t image_size, const uint8_t sha256[32])
{
    if (!sha256 || image_size < 1024)
        return refuse("image size implausible", ESP_ERR_INVALID_ARG);
    if (s_state == MIX_OTA_RECEIVING || s_state == MIX_OTA_READY_TO_BOOT)
        return refuse(s_state == MIX_OTA_RECEIVING ? "a transfer is already running"
                                                   : "an update is already staged; reboot first",
                      ESP_ERR_INVALID_STATE);
    if (mix_ota_pending_verify()) {
        /* Chaining updates would discard the only known-good slot. */
        return refuse("new build still on trial; retry in a minute", ESP_ERR_INVALID_STATE);
    }
    s_target = esp_ota_get_next_update_partition(NULL);
    if (!s_target) {
        /* The historic single-application layout has no ota_0/ota_1/otadata,
         * so no in-protocol update can ever start on it. Say so exactly: the
         * one-time migration is a partition-table flash, not a retry. */
        return refuse("no OTA slot: device still has the factory-only partition "
                      "table; run the one-time A/B migration", ESP_ERR_NOT_FOUND);
    }
    if (image_size > s_target->size)
        return refuse("image exceeds slot", ESP_ERR_INVALID_SIZE);
    /* Sequential writes erase one sector at a time instead of blanking the
     * whole 1984 KiB slot up front, which would stall the UI for seconds. */
    esp_err_t err = esp_ota_begin(s_target, OTA_WITH_SEQUENTIAL_WRITES, &s_handle);
    if (err != ESP_OK) {
        char reason[64];
        snprintf(reason, sizeof(reason), "esp_ota_begin: %s", esp_err_to_name(err));
        return refuse(reason, err);
    }
    s_open = true;
    s_total = image_size;
    s_received = 0;
    s_state = MIX_OTA_RECEIVING;
    s_error[0] = 0;
    memcpy(s_expected, sha256, sizeof(s_expected));
    mbedtls_sha256_init(&s_sha);
    if (mbedtls_sha256_starts(&s_sha, 0) != 0) {
        fail("sha256 init");
        return ESP_FAIL;
    }
    ESP_LOGI(TAG, "receiving %u bytes into %s", (unsigned)image_size, s_target->label);
    return ESP_OK;
}

esp_err_t mix_ota_write(uint32_t offset, const uint8_t *data, size_t len)
{
    if (s_state != MIX_OTA_RECEIVING || !s_open)
        return refuse("no transfer is open", ESP_ERR_INVALID_STATE);
    if (!data || !len || len > MIX_OTA_CHUNK)
        return refuse("chunk length out of range", ESP_ERR_INVALID_ARG);
    /* A mismatch is not fatal: the host resynchronises from mix_ota_received().
     * Leave s_error alone so a genuine earlier reason is not overwritten by
     * what is really ordinary retransmission. */
    if (offset != s_received) return ESP_ERR_INVALID_STATE;
    if (s_received + len > s_total) {
        fail("image longer than announced");
        return ESP_ERR_INVALID_SIZE;
    }
    esp_err_t err = esp_ota_write(s_handle, data, len);
    if (err != ESP_OK) {
        fail(esp_err_to_name(err));
        return err;
    }
    if (mbedtls_sha256_update(&s_sha, data, len) != 0) {
        fail("sha256 update");
        return ESP_FAIL;
    }
    s_received += (uint32_t)len;
    return ESP_OK;
}

esp_err_t mix_ota_finish(void)
{
    if (s_state != MIX_OTA_RECEIVING || !s_open)
        return refuse("no transfer is open", ESP_ERR_INVALID_STATE);
    if (s_received != s_total) {
        fail("incomplete image");
        return ESP_ERR_INVALID_SIZE;
    }
    uint8_t digest[32];
    if (mbedtls_sha256_finish(&s_sha, digest) != 0) {
        fail("sha256 finish");
        return ESP_FAIL;
    }
    mbedtls_sha256_free(&s_sha);
    /* Constant-time-ish compare; this is integrity, not authentication. */
    uint8_t diff = 0;
    for (size_t i = 0; i < sizeof(digest); i++) diff |= digest[i] ^ s_expected[i];
    if (diff) {
        fail("sha256 mismatch");
        return ESP_ERR_INVALID_CRC;
    }
    /* esp_ota_end also validates the ESP-IDF image header and its own hash. */
    esp_err_t err = esp_ota_end(s_handle);
    s_open = false;
    if (err != ESP_OK) {
        fail(err == ESP_ERR_OTA_VALIDATE_FAILED ? "not a valid ESP32-S3 image"
                                                : esp_err_to_name(err));
        return err;
    }
    err = esp_ota_set_boot_partition(s_target);
    if (err != ESP_OK) {
        fail(esp_err_to_name(err));
        return err;
    }
    s_state = MIX_OTA_READY_TO_BOOT;
    ESP_LOGI(TAG, "%s verified and selected for next boot", s_target->label);
    return ESP_OK;
}

void mix_ota_abort(void)
{
    if (s_state == MIX_OTA_RECEIVING) fail("aborted by host");
}

void mix_ota_reset(void)
{
    /* Clear a finished failure so the local UI stops showing it forever. A
     * transfer in flight and an image already staged for the next boot are
     * both left alone. */
    if (s_state == MIX_OTA_FAILED) {
        s_state = MIX_OTA_IDLE;
        s_total = s_received = 0;
    }
}

void mix_ota_tick(uint32_t now_ms)
{
    if (s_state != MIX_OTA_RECEIVING) {
        s_stall_mark = 0;
        s_stall_ms = now_ms;
        return;
    }
    if (s_received != s_stall_mark) {
        s_stall_mark = s_received;
        s_stall_ms = now_ms;
        return;
    }
    if ((uint32_t)(now_ms - s_stall_ms) >= MIX_OTA_STALL_MS) fail("transfer stalled");
}

mix_ota_state_t mix_ota_state(void) { return s_state; }
uint32_t mix_ota_received(void) { return s_received; }
uint32_t mix_ota_total(void) { return s_total; }

int mix_ota_percent(void)
{
    if (!s_total) return s_state == MIX_OTA_READY_TO_BOOT ? 100 : 0;
    return (int)((uint64_t)s_received * 100 / s_total);
}

const char *mix_ota_running_slot(void) { return s_running ? s_running->label : "?"; }
const char *mix_ota_target_slot(void) { return s_target ? s_target->label : "?"; }
const char *mix_ota_error(void) { return s_error[0] ? s_error : "update failed for an unrecorded reason"; }
