// SPDX-License-Identifier: GPL-2.0-or-later
// STM32F042 I2Cv2, PB6/PB7 AF1, 0x1f; HAL I2C1 must remain disabled.
#include "quantum.h"
#include "hal.h"
#include "i2c_slave_kbd.h"
#include "mix_kbd_transport.h"
#if STM32_I2C_USE_I2C1
#    error I2C1 is exclusively owned by the MixOS slave
#endif
static mix_kbd_transport_t state;
static uint8_t frame[KBD_V1_FRAME], rx[6], rx_len, tx_pos;
static bool read_valid, read_phase;
static void int_update(void) {
    if (state.count) {
        palClearLine(A13);
        palSetLineMode(A13, PAL_MODE_OUTPUT_OPENDRAIN);
    } else palSetLineMode(A13, PAL_MODE_INPUT);
}
void kbd_i2c_push_event(uint8_t row, uint8_t col, bool pressed) {
    chSysLock();
    mix_kbd_transport_event(&state, row, col, pressed);
    int_update();
    chSysUnlock();
}
bool kbd_i2c_key_down(uint8_t row, uint8_t col) {
    // Only main context writes rows; ISR only reads them.
    return row < 6 && col < 11 && !!(state.rows[row] & (1U << col));
}
void kbd_i2c_task(void) {
    chSysLock();
    bool pending = state.backlight_pending;
    uint8_t level = state.pending_backlight;
    state.backlight_pending = false;
    chSysUnlock();
    if (pending) backlight_level_noeeprom(level);
}
OSAL_IRQ_HANDLER(Vector9C) {
    OSAL_IRQ_PROLOGUE();
    uint32_t flags = I2C1->ISR;
    if (flags & (I2C_ISR_BERR | I2C_ISR_ARLO | I2C_ISR_OVR)) {
        I2C1->ICR = I2C_ICR_BERRCF | I2C_ICR_ARLOCF | I2C_ICR_OVRCF;
        rx_len = 0; read_valid = false;
    }
    if (flags & I2C_ISR_RXNE) {
        uint8_t b = (uint8_t)I2C1->RXDR;
        if (rx_len < sizeof(rx)) rx[rx_len++] = b;
        // Saturated length 6 rejects overlong writes (max command length 5).
    }
    if (flags & I2C_ISR_ADDR) {
        read_phase = !!(flags & I2C_ISR_DIR);
        if (read_phase) {
            read_valid = rx_len == 1 && rx[0] == 0;
            tx_pos = 0;
            if (read_valid) mix_kbd_transport_frame(&state, frame);
            I2C1->ISR = I2C_ISR_TXE; // discard any speculative stale TXDR
        } else {
            rx_len = 0;
            read_valid = false;
        }
        I2C1->ICR = I2C_ICR_ADDRCF;
    }
    if ((flags & I2C_ISR_TXIS) && !(flags & (I2C_ISR_NACKF | I2C_ISR_STOPF))) {
        I2C1->TXDR = read_valid && tx_pos < sizeof(frame) ? frame[tx_pos++] : 0xff;
    }
    if (flags & I2C_ISR_NACKF) I2C1->ICR = I2C_ICR_NACKCF;
    if (flags & I2C_ISR_STOPF) {
        // Commit only complete write commands, never speculative read bytes.
        if (!read_phase) mix_kbd_transport_write(&state, rx, rx_len);
        rx_len = 0; read_valid = false;
        int_update();
        I2C1->ICR = I2C_ICR_STOPCF;
    }
    OSAL_IRQ_EPILOGUE();
}
void kbd_i2c_slave_init(void) {
    mix_kbd_transport_init(&state);
    state.backlight = get_backlight_level();
    rccEnableAPB1(RCC_APB1ENR_I2C1EN, true);
    rccResetAPB1(RCC_APB1RSTR_I2C1RST);
    palSetLineMode(B6, PAL_MODE_ALTERNATE(1) | PAL_STM32_OTYPE_OPENDRAIN);
    palSetLineMode(B7, PAL_MODE_ALTERNATE(1) | PAL_STM32_OTYPE_OPENDRAIN);
    I2C1->CR1 = 0;
    I2C1->TIMINGR = 0x00310309;
    I2C1->OAR1 = I2C_OAR1_OA1EN | (KBD_V1_ADDR << 1);
    I2C1->CR1 = I2C_CR1_PE | I2C_CR1_ADDRIE | I2C_CR1_RXIE | I2C_CR1_TXIE |
                I2C_CR1_STOPIE | I2C_CR1_NACKIE | I2C_CR1_ERRIE;
    nvicEnableVector(I2C1_IRQn, 3);
    int_update();
}
