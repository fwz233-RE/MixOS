/* SPDX-License-Identifier: MIT */
#include "mix_ota.h"
#include <stdatomic.h>
#include <stdio.h>
#include <string.h>
#include "esp_app_desc.h"
#include "esp_log.h"
#include "esp_ota_ops.h"
#include "esp_partition.h"
#include "esp_system.h"
#include "mix_health.h"
#include "mbedtls/sha256.h"
#include "sdkconfig.h"

static const char *TAG = "MixOTA";
/* Only the OTA worker mutates the flash handle and hash context. Atomic small
 * status fields are read by the UI; transaction snapshots are separately locked. */
static const esp_partition_t *s_running, *s_target;
static esp_ota_handle_t s_handle;
static bool s_open;
static _Atomic mix_ota_state_t s_state;
static _Atomic uint32_t s_total, s_received;
static uint32_t s_stall_mark, s_stall_ms;
static uint8_t s_expected[32];
static mbedtls_sha256_context s_sha;
static char s_error[128];
static _Atomic bool s_pending_verify, s_confirmed, s_protect = true;
static _Atomic uint8_t s_image_state = 0xff;
static _Atomic uint32_t s_health_sample, s_local_sample;
static _Atomic bool s_health_input, s_local_input;
static uint32_t s_healthy_since, s_health_base, s_confirm_attempt;
static bool s_health_started, s_health_holding;
static unsigned s_confirm_failures;
#define HEALTH_HOLD_MS 20000u
#define HEALTH_LOCAL_DEADLINE_MS 120000u
#define HEALTH_SAMPLE_MAX_MS 2000u

static void fail(const char *reason)
{
    if (s_open) { esp_ota_abort(s_handle); s_open = false; }
    mbedtls_sha256_free(&s_sha);
    s_state = MIX_OTA_FAILED;
    /* Keep acknowledged bytes and the candidate binding for diagnosis. */
    snprintf(s_error, sizeof(s_error), "%s", reason);
    ESP_LOGE(TAG, "update failed: %s", reason);
}
static esp_err_t refuse(const char *reason, esp_err_t err)
{
    snprintf(s_error, sizeof(s_error), "%s", reason);
    ESP_LOGW(TAG, "update refused: %s", reason);
    return err;
}
static uint8_t slot_index(const esp_partition_t *p)
{
    return p && p->subtype >= ESP_PARTITION_SUBTYPE_APP_OTA_0 &&
           p->subtype <= ESP_PARTITION_SUBTYPE_APP_OTA_15
               ? (uint8_t)(p->subtype - ESP_PARTITION_SUBTYPE_APP_OTA_0) : 0xff;
}
void mix_ota_init(void)
{
    /* Startup-only, before creating the worker. Flash handles cannot survive
     * reset; all transient transfer state must be reconstructed from scratch. */
    s_target = NULL; s_handle = 0; s_open = false; s_state = MIX_OTA_IDLE;
    s_total = s_received = 0; s_stall_mark = s_stall_ms = 0;
    memset(s_expected, 0, sizeof(s_expected)); s_error[0] = 0;
    mbedtls_sha256_free(&s_sha); mbedtls_sha256_init(&s_sha);
    s_protect = true;
    s_health_sample = s_local_sample = 0;
    s_healthy_since = s_health_base = s_confirm_attempt = 0;
    s_running = esp_ota_get_running_partition();
    esp_ota_img_states_t state;
    s_image_state = 0xff; s_confirmed = false; s_pending_verify = false;
    s_health_started = s_health_holding = false; s_confirm_failures = 0;
    s_health_input = s_local_input = false;
    if (s_running && esp_ota_get_state_partition(s_running, &state) == ESP_OK) {
        s_image_state = (uint8_t)state;
        s_pending_verify = state == ESP_OTA_IMG_PENDING_VERIFY;
        s_confirmed = state == ESP_OTA_IMG_VALID;
    }
    ESP_LOGI(TAG, "slot %s state %u", s_running ? s_running->label : "?", (unsigned)s_image_state);
}
bool mix_ota_pending_verify(void) { return !s_confirmed; }
void mix_ota_set_baseline_protection(bool protect) { s_protect = protect; }
bool mix_ota_baseline_protected(void) { return s_protect; }
uint8_t mix_ota_running_state(void) { return s_image_state; }
uint8_t mix_ota_running_index(void) { return slot_index(s_running); }
uint8_t mix_ota_boot_index(void) { return slot_index(esp_ota_get_boot_partition()); }
void mix_ota_health_tick(uint32_t now_ms, bool healthy)
{
    s_health_input = healthy; s_health_sample = now_ms;
}
void mix_ota_local_health_tick(uint32_t now_ms, bool healthy)
{
    s_local_input = healthy; s_local_sample = now_ms;
}
void mix_ota_process_health(uint32_t now_ms, bool maintenance_confirmed)
{
    if (!s_health_started) { s_health_started = true; s_health_base = now_ms; }
    if (!s_pending_verify || s_confirmed) return;
    bool local = s_local_input && (uint32_t)(now_ms - s_local_sample) <= HEALTH_SAMPLE_MAX_MS;
    bool healthy = local && maintenance_confirmed && s_health_input && (uint32_t)(now_ms - s_health_sample) <= HEALTH_SAMPLE_MAX_MS;
    if (!healthy) s_health_holding = false;
    if (!local && (uint32_t)(now_ms - s_health_base) >= HEALTH_LOCAL_DEADLINE_MS) {
        snprintf(s_error, sizeof(s_error), "trial local health deadline expired");
        /* A failed explicit rollback still needs a reset while PENDING_VERIFY. */
        esp_ota_mark_app_invalid_rollback_and_reboot(); esp_restart(); return;
    }
    if (!healthy) return; /* Linux absence is not a local firmware fault. */
    if (!s_health_holding) { s_health_holding = true; s_healthy_since = now_ms; }
    if ((uint32_t)(now_ms - s_healthy_since) < HEALTH_HOLD_MS) return;
    if (s_confirm_failures && (uint32_t)(now_ms - s_confirm_attempt) < 1000u) return;
    s_confirm_attempt = now_ms;
    esp_err_t err = esp_ota_mark_app_valid_cancel_rollback();
    esp_ota_img_states_t state;
    if (err == ESP_OK && esp_ota_get_state_partition(s_running, &state) == ESP_OK && state == ESP_OTA_IMG_VALID) {
        s_image_state = ESP_OTA_IMG_VALID; s_confirmed = true; s_pending_verify = false;
        ESP_LOGI(TAG, "trial confirmed in flash after local and protocol health");
        return;
    }
    snprintf(s_error, sizeof(s_error), "confirmation not verified: %s", esp_err_to_name(err));
    /* Never let a failed mark disarm the software guard. A subsequent boot is
     * authoritative if a mark succeeded but its read-back was unavailable. */
    if (++s_confirm_failures >= 3) esp_restart();
}

esp_err_t mix_ota_begin(uint32_t image_size, const uint8_t sha256[32])
{
    if (!sha256 || image_size < 1024) return refuse("image size implausible", ESP_ERR_INVALID_ARG);
    if (s_state == MIX_OTA_RECEIVING || s_state == MIX_OTA_VALIDATING ||
        s_state == MIX_OTA_VALIDATED || s_state == MIX_OTA_READY_TO_BOOT)
        return refuse("a transfer or boot selection is already active", ESP_ERR_INVALID_STATE);
    if (!s_confirmed || s_image_state != ESP_OTA_IMG_VALID)
        return refuse("running build is unconfirmed or state unknown", ESP_ERR_INVALID_STATE);
    const esp_partition_t *boot = esp_ota_get_boot_partition();
    if (!boot || !s_running || boot->address != s_running->address)
        return refuse("boot selection differs from running slot; reconcile first", ESP_ERR_INVALID_STATE);
    s_target = esp_ota_get_next_update_partition(NULL);
    if (!s_target) return refuse("no OTA slot: verified A/B layout required", ESP_ERR_NOT_FOUND);
    if (s_target->address == s_running->address)
        return refuse("refusing to overwrite running slot", ESP_ERR_INVALID_STATE);
    if (s_protect && slot_index(s_target) == 0)
        return refuse("ota_0 recovery baseline is protected", ESP_ERR_INVALID_STATE);
    if (image_size > s_target->size) return refuse("image exceeds slot", ESP_ERR_INVALID_SIZE);
    esp_err_t err = esp_ota_begin(s_target, OTA_WITH_SEQUENTIAL_WRITES, &s_handle);
    if (err != ESP_OK) return refuse(esp_err_to_name(err), err);
    s_open = true; s_total = image_size; s_received = 0; s_stall_mark = 0;
    s_state = MIX_OTA_RECEIVING; s_error[0] = 0;
    memcpy(s_expected, sha256, sizeof(s_expected));
    mbedtls_sha256_init(&s_sha);
    if (mbedtls_sha256_starts(&s_sha, 0) != 0) { fail("sha256 init"); return ESP_FAIL; }
    return ESP_OK;
}
esp_err_t mix_ota_write(uint32_t offset, const uint8_t *data, size_t len)
{
    if (s_state != MIX_OTA_RECEIVING || !s_open) return refuse("no transfer is open", ESP_ERR_INVALID_STATE);
    if (!data || !len || len > MIX_OTA_CHUNK) return refuse("chunk length out of range", ESP_ERR_INVALID_ARG);
    if (offset != s_received) return ESP_ERR_INVALID_STATE;
    if (len > s_total - s_received) { fail("image longer than announced"); return ESP_ERR_INVALID_SIZE; }
    esp_err_t err = esp_ota_write(s_handle, data, len);
    if (err != ESP_OK) { fail(esp_err_to_name(err)); return err; }
    if (mbedtls_sha256_update(&s_sha, data, len) != 0) { fail("sha256 update"); return ESP_FAIL; }
    s_received += (uint32_t)len;
    return ESP_OK;
}
static esp_err_t hash_partition(const esp_partition_t *p, uint32_t bytes, uint8_t out[32])
{
    if (!p || !out || !bytes || bytes > p->size) return ESP_ERR_INVALID_SIZE;
    uint8_t data[1024]; mbedtls_sha256_context ctx; mbedtls_sha256_init(&ctx);
    esp_err_t err = mbedtls_sha256_starts(&ctx, 0) == 0 ? ESP_OK : ESP_FAIL;
    for (uint32_t offset = 0; err == ESP_OK && offset < bytes;) {
        uint32_t n = bytes - offset; if (n > sizeof(data)) n = sizeof(data);
        err = esp_partition_read(p, offset, data, n);
        if (err == ESP_OK && mbedtls_sha256_update(&ctx, data, n) != 0) err = ESP_FAIL;
        offset += n;
        /* The worker is making measured progress, not feeding for another task. */
        if (mix_watchdog_task_reset(MIX_HEALTH_OTA) != ESP_OK) err = ESP_FAIL;
    }
    if (err == ESP_OK && mbedtls_sha256_finish(&ctx, out) != 0) err = ESP_FAIL;
    mbedtls_sha256_free(&ctx); return err;
}
esp_err_t mix_ota_hash_running(uint32_t bytes, uint8_t sha[32]) { return hash_partition(s_running, bytes, sha); }
esp_err_t mix_ota_validate(uint8_t stored_sha[32])
{
    if (s_state != MIX_OTA_RECEIVING || !s_open) return refuse("no transfer is open", ESP_ERR_INVALID_STATE);
    if (s_received != s_total) { fail("incomplete image"); return ESP_ERR_INVALID_SIZE; }
    s_state = MIX_OTA_VALIDATING;
    uint8_t digest[32];
    if (mbedtls_sha256_finish(&s_sha, digest) != 0) { fail("sha256 finish"); return ESP_FAIL; }
    mbedtls_sha256_free(&s_sha);
    if (memcmp(digest, s_expected, sizeof(digest))) { fail("sha256 mismatch"); return ESP_ERR_INVALID_CRC; }
    /* esp_ota_end consumes its handle on success AND failure. */
    esp_err_t err = esp_ota_end(s_handle); s_open = false;
    if (err != ESP_OK) { fail(esp_err_to_name(err)); return err; }
    err = hash_partition(s_target, s_total, digest);
    if (err != ESP_OK || memcmp(digest, s_expected, 32)) {
        fail("flash readback SHA256 mismatch or read failure"); return err != ESP_OK ? err : ESP_ERR_INVALID_CRC;
    }
    if (stored_sha) memcpy(stored_sha, digest, 32);
    s_state = MIX_OTA_VALIDATED; return ESP_OK;
}
esp_err_t mix_ota_select(void)
{
    if (s_state != MIX_OTA_VALIDATED) return refuse("image is not validated", ESP_ERR_INVALID_STATE);
    esp_err_t err = esp_ota_set_boot_partition(s_target);
    const esp_partition_t *boot = esp_ota_get_boot_partition();
    if (boot && boot->address == s_target->address) {
        s_state = MIX_OTA_READY_TO_BOOT;
        if (err != ESP_OK) snprintf(s_error, sizeof(s_error), "boot selected despite API error: %s", esp_err_to_name(err));
        return ESP_OK;
    }
    fail("boot selection not verified; inspect metadata before retry");
    return err != ESP_OK ? err : ESP_FAIL;
}
esp_err_t mix_ota_finish(void)
{
    esp_err_t err = mix_ota_validate(NULL); return err == ESP_OK ? mix_ota_select() : err;
}
void mix_ota_abort(void)
{
    if (s_state == MIX_OTA_RECEIVING || s_state == MIX_OTA_VALIDATED) fail("aborted by host");
}
void mix_ota_reset(void)
{
    if (s_state == MIX_OTA_FAILED) { s_state = MIX_OTA_IDLE; s_total = s_received = 0; }
}
void mix_ota_tick(uint32_t now_ms)
{
    if (s_state != MIX_OTA_RECEIVING || s_received != s_stall_mark) {
        s_stall_mark = s_received; s_stall_ms = now_ms; return;
    }
    if ((uint32_t)(now_ms - s_stall_ms) >= MIX_OTA_STALL_MS) fail("transfer stalled");
}
mix_ota_state_t mix_ota_state(void) { return s_state; }
uint32_t mix_ota_received(void) { return s_received; }
uint32_t mix_ota_total(void) { return s_total; }
int mix_ota_percent(void) { uint32_t total = s_total; return total ? (int)((uint64_t)s_received * 100 / total) : 0; }
const char *mix_ota_running_slot(void) { return s_running ? s_running->label : "?"; }
const char *mix_ota_target_slot(void) { return s_target ? s_target->label : "?"; }
static void identity_text(uint8_t *out, const char *src, size_t width)
{
    memset(out, 0, width); if (src) memcpy(out, src, strnlen(src, width));
}
size_t mix_ota_identity(uint8_t *out, size_t cap)
{
    if (!out || cap < MIX_OTA_IDENTITY_BYTES) return 0;
    memset(out, 0, MIX_OTA_IDENTITY_BYTES); out[0] = 1; out[1] = slot_index(s_running);
    /* Atomic cached state: querying identity no longer mmaps otadata from the
     * main/UI task. Only the worker updates confirmed state. */
    out[2] = s_image_state; out[3] = (uint8_t)esp_reset_reason();
    uint32_t addr = s_running ? s_running->address : 0;
    for (unsigned i = 0; i < 4; ++i) out[4+i] = (uint8_t)(addr >> (i*8));
    const esp_app_desc_t *desc = esp_app_get_description();
    if (desc) {
        memcpy(out+8, desc->app_elf_sha256, 32);
        identity_text(out+40, desc->date, 16); identity_text(out+56, desc->time, 16);
        identity_text(out+72, desc->version, 32); identity_text(out+104, desc->project_name, 32);
    }
    return MIX_OTA_IDENTITY_BYTES;
}
const char *mix_ota_error(void) { return s_error[0] ? s_error : "update failed for an unrecorded reason"; }
