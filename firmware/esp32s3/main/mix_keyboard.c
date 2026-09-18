#include "mix_keyboard.h"
#include <string.h>
#include "board_pins.h"
#include "mix_i2c.h"
// Wire constants intentionally local: ESP build has no QMK dependency.
#define FRAME_SIZE 126
#define HEADER_SIZE 28
#define BATCH_SIZE 32
// The slave answers from a prepared buffer, so a healthy read never needs
// longer than this; a longer wait would stall the UI task behind a dead MCU.
#define IO_TIMEOUT_MS 5
#define POLL_MS 20
#define OFFLINE_MS 1000
static i2c_master_dev_handle_t device;
static mix_key_cb callback;
static void *context;
static bool online, waiting_release, scheduled;
static uint32_t next_poll, session, serial, remote_overflow, overflows;
static uint16_t expected, rows[6];
static uint8_t backlight_steps;
static uint16_t u16(const uint8_t *p) { return (uint16_t)(p[0] | ((uint16_t)p[1] << 8)); }
static uint32_t u32(const uint8_t *p) { return (uint32_t)p[0] | ((uint32_t)p[1] << 8) | ((uint32_t)p[2] << 16) | ((uint32_t)p[3] << 24); }
static uint16_t crc16(const uint8_t *data, size_t len) {
    uint16_t crc = 0xffff;
    while (len--) {
        crc ^= (uint16_t)((uint16_t)*data++ << 8);
        for (unsigned bit = 0; bit < 8; ++bit)
            crc = (uint16_t)((crc & 0x8000) ? (crc << 1) ^ 0x1021 : crc << 1);
    }
    return crc;
}
static bool real(uint8_t r, uint8_t c) { return r < 6 && c < 11 && (r != 0 || ((0x038eU >> c) & 1U)); }
static esp_err_t write_bytes(const uint8_t *p, size_t n) { return mix_i2c_transmit(device, p, n, IO_TIMEOUT_MS); }
void mix_keyboard_reset_input(void) {
    waiting_release = true;
    mix_input_reset();
}
static void failed(uint32_t now) {
    online = false;
    session = 0;
    backlight_steps = 0;
    mix_keyboard_reset_input();
    next_poll = now + OFFLINE_MS;
}
esp_err_t mix_keyboard_init(mix_key_cb cb, void *ctx) {
    // The bus belongs to mix_i2c; main.c initialises it before any driver runs.
    // Idempotent re-init without leaking another IDF device handle.
    if (device) {
        esp_err_t e = mix_i2c_rm_device(device);
        if (e != ESP_OK) return e;
        device = NULL;
    }
    callback = cb; context = ctx;
    online = scheduled = false; session = 0;
    remote_overflow = overflows = 0; backlight_steps = 0;
    memset(rows, 0, sizeof(rows));
    mix_input_init(cb, ctx); mix_keyboard_reset_input();
    device = mix_i2c_add_device(MIX_KEYBOARD_I2C_ADDR, I2C_FAST_HZ);
    return device ? ESP_OK : ESP_ERR_NOT_FOUND;
}
bool mix_keyboard_online(void) { return online; }
uint32_t mix_keyboard_overflows(void) { return overflows; }
void mix_keyboard_backlight_step(void) {
    // Callback-safe: enqueue only; no nested bus access or EEPROM writes.
    if (online) backlight_steps = (uint8_t)((backlight_steps + 1) % 9);
}
void mix_keyboard_tick(uint32_t now) {
    if (!device || (scheduled && (int32_t)(now - next_poll) < 0)) return;
    scheduled = true; next_poll = now + POLL_MS;
    uint8_t frame[FRAME_SIZE];
    if (mix_i2c_read(device, 0, frame, sizeof(frame), IO_TIMEOUT_MS) != ESP_OK) { failed(now); return; }
    if (u16(frame + FRAME_SIZE - 2) != crc16(frame, FRAME_SIZE - 2)) { failed(now); return; }
    if (frame[0] != 0x6b || frame[1] != 1 || (frame[2] & ~1U) || frame[3] > 128 || frame[22] > 8 || frame[23]) { failed(now); return; }
    uint16_t snapshot[6];
    bool all_up = true;
    for (unsigned r = 0; r < 6; ++r) {
        snapshot[r] = u16(frame + 10 + 2 * r);
        if (snapshot[r] & (uint16_t)~(r ? 0x07ffU : 0x038eU)) { failed(now); return; }
        if (snapshot[r]) all_up = false;
    }
    if (!session || u32(frame + 24) != session) {
        // Fresh host cookie detects MCU reset even when sequence wraps to zero.
        session = ++serial;
        if (!session) session = ++serial;
        uint8_t claim[5] = {0x13, (uint8_t)session, (uint8_t)(session >> 8), (uint8_t)(session >> 16), (uint8_t)(session >> 24)};
        online = false; remote_overflow = 0;
        mix_keyboard_reset_input();
        if (write_bytes(claim, sizeof(claim)) != ESP_OK) failed(now);
        return;
    }
    online = true;
    uint32_t overflow = u32(frame + 6);
    if (overflow != remote_overflow) {
        overflows += overflow - remote_overflow;
        remote_overflow = overflow;
        mix_keyboard_reset_input();
    }
    unsigned count = frame[3] < BATCH_SIZE ? frame[3] : BATCH_SIZE;
    uint16_t predicted[6];
    memcpy(predicted, rows, sizeof(rows));
    bool valid = true;
    for (unsigned i = 0; i < count; ++i) {
        const uint8_t *e = frame + HEADER_SIZE + 3 * i;
        uint8_t r = (e[2] >> 4) & 7, c = e[2] & 15;
        bool pressed = !!(e[2] & 0x80);
        if (!real(r, c)) { failed(now); return; }
        if (!waiting_release) {
            if (u16(e) != (uint16_t)(expected + i) || !!(predicted[r] & (1U << c)) == pressed) valid = false;
            if (pressed) predicted[r] |= (uint16_t)(1U << c); else predicted[r] &= (uint16_t)~(1U << c);
        }
    }
    if (!waiting_release && frame[3] <= BATCH_SIZE &&
        (u16(frame + 4) != (uint16_t)(expected + count) || memcmp(predicted, snapshot, sizeof(snapshot)))) valid = false;
    if (!valid) mix_keyboard_reset_input();
    // ACK BEFORE callbacks: a failed ACK never duplicates emitted text.
    if (count) {
        const uint8_t *last = frame + HEADER_SIZE + 3 * (count - 1);
        uint8_t ack[3] = {0x11, last[0], last[1]};
        if (write_bytes(ack, sizeof(ack)) != ESP_OK) { failed(now); return; }
    }
    if (waiting_release) {
        // Drain/discard bounded batches; arm ONLY at an atomic all-up boundary.
        // Events arriving after this snapshot survive the prefix ACK.
        if (all_up && frame[3] <= BATCH_SIZE) {
            memset(rows, 0, sizeof(rows));
            expected = u16(frame + 4);
            mix_input_init(callback, context);
            waiting_release = false;
        }
    } else {
        expected = (uint16_t)(expected + count);
        memcpy(rows, predicted, sizeof(rows));
        for (unsigned i = 0; i < count && !waiting_release; ++i) {
            uint8_t e = frame[HEADER_SIZE + 3 * i + 2];
            mix_input_event((e >> 4) & 7, e & 15, !!(e & 0x80), now);
        }
        // Suppress repeats while a release could still be queued behind batch.
        if (frame[3] <= BATCH_SIZE && !waiting_release) mix_input_tick(now);
    }
    if (backlight_steps) {
        uint8_t cmd[2] = {0x20, (uint8_t)((frame[22] + backlight_steps) % 9)};
        backlight_steps = 0;
        if (write_bytes(cmd, sizeof(cmd)) != ESP_OK) failed(now);
    }
}
