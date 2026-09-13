// SPDX-License-Identifier: GPL-2.0-or-later
#pragma once
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#define KBD_V1_ADDR 0x1f
#define KBD_V1_ID 0x6b
#define KBD_V1_VERSION 1
#define KBD_V1_BATCH 32
#define KBD_V1_HEADER 28
#define KBD_V1_FRAME (KBD_V1_HEADER + 3 * KBD_V1_BATCH + 2)
#define KBD_V1_FIFO 128
#define KBD_CMD_ACK 0x11
#define KBD_CMD_SESSION 0x13
#define KBD_CMD_BACKLIGHT 0x20
// No packed structs on the wire. All multi-byte integers little endian.
typedef struct {
    uint8_t fifo[KBD_V1_FIFO][3];
    uint16_t rows[6], next_seq;
    uint32_t overflow, session;
    uint8_t head, tail, count, backlight, pending_backlight;
    bool backlight_pending;
} mix_kbd_transport_t;
// Caller serializes all methods (STM32 main uses chSysLock, ISR exclusive).
uint16_t mix_kbd_crc16(const uint8_t *data, size_t len);
void mix_kbd_transport_init(mix_kbd_transport_t *s);
void mix_kbd_transport_event(mix_kbd_transport_t *s, uint8_t r, uint8_t c, bool down);
void mix_kbd_transport_frame(const mix_kbd_transport_t *s, uint8_t out[KBD_V1_FRAME]);
void mix_kbd_transport_write(mix_kbd_transport_t *s, const uint8_t *data, size_t len);
