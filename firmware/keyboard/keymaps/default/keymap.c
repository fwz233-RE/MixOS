// SPDX-License-Identifier: GPL-2.0-or-later
#include QMK_KEYBOARD_H
// Every REAL switch, including ALL five space domes, has a non-HID custom
// code. KC_NO MUST remain on blanks: QMK ghost detection consults layer 0.
// No user processor, macros, register_code(), consumer or layer actions.
#define P QK_USER_0
#define X KC_NO
const uint16_t PROGMEM keymaps[][MATRIX_ROWS][MATRIX_COLS] = {
    LAYOUT_6x11(
        X,P,P,P,X,X,X,P,P,P,X,
        P,P,P,P,P,P,P,P,P,P,P,
        P,P,P,P,P,P,P,P,P,P,P,
        P,P,P,P,P,P,P,P,P,P,P,
        P,P,P,P,P,P,P,P,P,P,P,
        P,P,P,P,P,P,P,P,P,P,P
    )
};
