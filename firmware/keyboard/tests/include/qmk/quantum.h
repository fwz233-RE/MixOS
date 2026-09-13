#pragma once
#include <stdint.h>
#include <stdbool.h>
typedef uint16_t matrix_row_t;
typedef struct { struct { uint8_t row, col; } key; bool pressed; uint8_t type; } keyevent_t;
typedef struct { keyevent_t event; } keyrecord_t;
#define IS_KEYEVENT(e) ((e).type == 1)
uint32_t timer_read32(void);
uint32_t timer_elapsed32(uint32_t);
matrix_row_t matrix_get_row(uint8_t);
void reset_keyboard(void);
void backlight_level_noeeprom(uint8_t);
uint8_t get_backlight_level(void);
