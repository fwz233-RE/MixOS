// SPDX-License-Identifier: GPL-2.0-or-later
#include "mix_kbd_transport.h"
#include "mix_matrix.h"
#include <string.h>
static void put16(uint8_t *p, uint16_t v) { p[0] = (uint8_t)v; p[1] = (uint8_t)(v >> 8); }
static void put32(uint8_t *p, uint32_t v) {
    for (unsigned i = 0; i < 4; ++i) p[i] = (uint8_t)(v >> (8 * i));
}
uint16_t mix_kbd_crc16(const uint8_t *data, size_t len) {
    uint16_t crc = 0xffff;
    while (len--) {
        crc ^= (uint16_t)((uint16_t)*data++ << 8);
        for (unsigned bit = 0; bit < 8; ++bit)
            crc = (uint16_t)((crc & 0x8000) ? (crc << 1) ^ 0x1021 : crc << 1);
    }
    return crc;
}
void mix_kbd_transport_init(mix_kbd_transport_t *s) {
    memset(s, 0, sizeof(*s));
    s->backlight = 3;
}
void mix_kbd_transport_event(mix_kbd_transport_t *s, uint8_t r, uint8_t c, bool down) {
    if (!mix_matrix_real(r, c)) return;
    uint16_t bit = (uint16_t)(1U << c);
    if (!!(s->rows[r] & bit) == down) return;
    if (down) s->rows[r] |= bit; else s->rows[r] &= (uint16_t)~bit;
    uint16_t seq = s->next_seq++;
    if (s->count == KBD_V1_FIFO) { ++s->overflow; return; }
    put16(s->fifo[s->head], seq);
    s->fifo[s->head][2] = (uint8_t)((down ? 0x80 : 0) | (r << 4) | c);
    s->head = (uint8_t)((s->head + 1) % KBD_V1_FIFO);
    ++s->count;
}
void mix_kbd_transport_frame(const mix_kbd_transport_t *s, uint8_t out[KBD_V1_FRAME]) {
    memset(out, 0, KBD_V1_FRAME);
    out[0] = KBD_V1_ID; out[1] = KBD_V1_VERSION;
    out[2] = s->overflow ? 1 : 0; out[3] = s->count;
    put16(out + 4, s->next_seq); put32(out + 6, s->overflow);
    for (unsigned r = 0; r < 6; ++r) put16(out + 10 + 2 * r, s->rows[r]);
    out[22] = s->backlight;
    put32(out + 24, s->session);
    unsigned n = s->count < KBD_V1_BATCH ? s->count : KBD_V1_BATCH;
    for (unsigned i = 0; i < n; ++i)
        memcpy(out + KBD_V1_HEADER + i * 3, s->fifo[(s->tail + i) % KBD_V1_FIFO], 3);
    put16(out + KBD_V1_FRAME - 2, mix_kbd_crc16(out, KBD_V1_FRAME - 2));
}
void mix_kbd_transport_write(mix_kbd_transport_t *s, const uint8_t *data, size_t len) {
    if (len == 3 && data[0] == KBD_CMD_ACK) {
        uint16_t seq = (uint16_t)(data[1] | ((uint16_t)data[2] << 8));
        // Only acknowledge the first batch. Repeated ACK is harmless.
        unsigned n = s->count < KBD_V1_BATCH ? s->count : KBD_V1_BATCH;
        for (unsigned i = 0; i < n; ++i) {
            uint8_t *ev = s->fifo[(s->tail + i) % KBD_V1_FIFO];
            if (seq != (uint16_t)(ev[0] | ((uint16_t)ev[1] << 8))) continue;
            s->tail = (uint8_t)((s->tail + i + 1) % KBD_V1_FIFO);
            s->count = (uint8_t)(s->count - i - 1);
            break;
        }
    } else if (len == 5 && data[0] == KBD_CMD_SESSION) {
        s->session = (uint32_t)data[1] | ((uint32_t)data[2] << 8) |
                     ((uint32_t)data[3] << 16) | ((uint32_t)data[4] << 24);
    } else if (len == 2 && data[0] == KBD_CMD_BACKLIGHT && data[1] <= 8) {
        s->backlight = data[1]; // requested level, visible immediately to next step
        s->pending_backlight = data[1]; s->backlight_pending = true;
    }
}
_Static_assert(sizeof(mix_kbd_transport_t) + KBD_V1_FRAME + 10 <= 576,
               "Transport budget must remain below 576 bytes on F042");
