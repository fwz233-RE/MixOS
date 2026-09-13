// SPDX-License-Identifier: GPL-2.0-or-later
#include "hal.h"
#include "quantum.h"
#include "i2c_slave_kbd.h"
#include "mix_matrix.h"
#if defined(COMMAND_ENABLE) || defined(CONSOLE_ENABLE) || defined(EXTRAKEY_ENABLE) || defined(MOUSEKEY_ENABLE) || defined(NKRO_ENABLE) || defined(COMBO_ENABLE) || defined(VIA_ENABLE) || defined(DYNAMIC_MACRO_ENABLE)
#    error Unsupported processor can bypass MixOS USB isolation
#endif
void board_init(void) {
    // ROM DFU can leave system memory mapped at address zero. Restore the
    // application vector table before USB interrupts are enabled.
#if defined(SYSCFG_CFGR1_MEM_MODE)
    SYSCFG->CFGR1 = (SYSCFG->CFGR1 & ~SYSCFG_CFGR1_MEM_MODE) |
                     SYSCFG_CFGR1_PA11_PA12_RMP;
#else
    SYSCFG->CFGR1 |= SYSCFG_CFGR1_PA11_PA12_RMP;
#endif
}
void keyboard_post_init_kb(void) { kbd_i2c_slave_init(); }
// Verified on QMK 0.28.0: matrix_scan/debounce -> has_ghost_in_row ->
// action_exec -> pre_process_record_quantum -> this hook -> [STOP].
// It precedes tapping, process_record_user, process_backlight and HID actions.
// IS_KEYEVENT and physical mask reject tick events and ghosted blank locations.
bool pre_process_record_kb(uint16_t keycode, keyrecord_t *record) {
    (void)keycode;
    if (IS_KEYEVENT(record->event) && mix_matrix_real(record->event.key.row, record->event.key.col))
        kbd_i2c_push_event(record->event.key.row, record->event.key.col, record->event.pressed);
    return false;
}
// Defense in depth: no user callback (in particular no old direct-space code).
bool process_record_kb(uint16_t keycode, keyrecord_t *record) {
    (void)keycode; (void)record;
    return false;
}
static bool rescue_chord(void) {
    bool fn = kbd_i2c_key_down(3, 0), sym = kbd_i2c_key_down(5, 0);
    if (!kbd_i2c_key_down(0, 9) || fn == sym) return false;
    // Require EXACTLY two real keys, in both debounced raw and accepted state.
    // A suppressed ghost release must not complete a stale rescue timer.
    for (uint8_t r = 0; r < 6; ++r) {
        matrix_row_t raw = matrix_get_row(r);
        for (uint8_t c = 0; c < 11; ++c) {
            if (!mix_matrix_real(r, c)) continue;
            bool wanted = (r == 0 && c == 9) || (r == (fn ? 3 : 5) && c == 0);
            if (!!(raw & (1U << c)) != wanted || kbd_i2c_key_down(r, c) != wanted) return false;
        }
    }
    return true;
}
void housekeeping_task_kb(void) {
    static bool timing;
    static uint32_t started;
    kbd_i2c_task();
    if (!rescue_chord()) { timing = false; return; }
    if (!timing) { started = timer_read32(); timing = true; }
    if (timer_elapsed32(started) >= 3000) reset_keyboard();
}
