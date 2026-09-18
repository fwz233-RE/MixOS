// ES8389 codec 初始化（USB UAC 放音 + 录音全双工路径）
// LCD 初始化完成后调用；配好 I2S（ESP 主、ES8389 从、无 MCLK）+ ES8389 codec。
//
// Concurrency contract
// --------------------
// The codec is touched from three tasks at once: the TinyUSB task (playback
// write, capture read, host mute and host volume), the main task (local volume
// keys) and the device task (speaker/headphone channel swap, which is an I2C
// read-modify-write on codec register 0x44). esp_codec_dev is not documented as
// thread safe, and a read-modify-write racing a volume write can publish a
// register value computed from stale contents.
//
// Therefore this module owns a mutex and exposes operations rather than the
// handle. The raw esp_codec_dev_handle_t is deliberately no longer public:
// callers cannot reintroduce an unsynchronised access path.
#pragma once
#include <stdbool.h>
#include <stddef.h>
#include "driver/i2c_master.h"
#include "esp_err.h"

/* Probe and bring up I2S and the ES8389. Returns an error instead of aborting:
 * main.c keeps the local UI usable on a board whose audio path is dead. */
esp_err_t audio_start(i2c_master_bus_handle_t bus);

/* True once a codec was found and opened. */
bool audio_ready(void);

/* Playback. Returns ESP_ERR_INVALID_STATE when no codec came up. */
esp_err_t audio_write(const void *samples, size_t bytes);

/* Capture. On any failure `samples` is zero-filled so the caller always has a
 * defined buffer to hand back to the USB stack. */
esp_err_t audio_read(void *samples, size_t bytes);

/* Output volume, 0..100. Values outside the range are clamped. */
esp_err_t audio_set_volume(int percent);

esp_err_t audio_set_mute(bool muted);

/* 外放左右反接补偿（REG0x44 bit5:4）。Idempotent: a read-modify-write that
 * issues no I2C write when the register already holds the wanted value. */
esp_err_t audio_set_dac_lr_swap(bool swap);

// Capture health and recovery
// ---------------------------
// Measured on typixdeck on 2026-09-14: after about nineteen hours of running,
// every sample arriving from the codec was exactly -1, on both channels, while
// the speaker still played and the codec still answered on I2C. Every sample
// identical is not something a microphone produces; it is GPIO48's pull-up
// holding an I2S data line that the ES8389 has stopped driving.
//
// Nothing on the device noticed. audio_ready() answers "is there an open
// handle", the handle stayed open, and so the screen went on reporting the
// audio path as ready while recording was dead until the next reboot.
//
// What this must not do, learned the hard way on the same day
// ----------------------------------------------------------
// The first version of this made the device far worse than the fault it was
// written for, and both mistakes are easy to make again:
//
//   * It rebuilt the codec without releasing the four interface objects
//     es8389_setup() creates, so every rebuild leaked them and handed the new
//     codec a data interface still pointing at deleted I2S channels. Rebuilds
//     could only fail.
//   * A failed rebuild leaves no codec, audio_read() then zero-fills, and
//     zeros are a dead line by any honest test - so the failure fed the
//     detector that triggered it, and the device rebuilt itself into the same
//     hole every second. Only captures that were actually read may be
//     accounted, and a rebuild that fails must back off rather than retry
//     immediately.

/* Account one capture buffer against the dead-line test. Call this ONLY for a
 * buffer audio_read() returned ESP_OK for: on failure it contains zeros this
 * module wrote, and feeding those back in is the positive feedback loop
 * described above. */
void audio_note_capture(const void *samples, size_t bytes);

/* True when every capture accounted for over the last couple of seconds was a
 * dead line. Deliberately not "the last one": a single buffer is short enough
 * that a genuine silence could span very few counts. */
bool audio_capture_dead(void);

/* Housekeeping, to be called about once a second from a task that can afford
 * to block for the length of a codec bring-up. Decides for itself whether the
 * codec needs rebuilding, rate-limits its own attempts, and does nothing at
 * all in the ordinary case. */
void audio_maintain(void);

/* Close the codec, release every interface it was built from, and bring it up
 * again. Safe to call from a task other than the one recording: captures taken
 * while the codec is being rebuilt are zero-filled, like any other failure. */
esp_err_t audio_recover(void);

/* How many times audio_recover() has succeeded. Worth reporting rather than
 * hiding: a codec that has to be rebuilt repeatedly is a different fault from
 * one that never does, and both look identical once the repair works. */
uint32_t audio_recovery_count(void);
