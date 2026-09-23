#pragma once
#include <stdbool.h>
#include <stdint.h>
#include "esp_err.h"
#include "mix_input.h"
esp_err_t mix_keyboard_init(mix_key_cb cb, void *ctx);
void mix_keyboard_tick(uint32_t now_ms);
void mix_keyboard_reset_input(void);
bool mix_keyboard_online(void);
uint32_t mix_keyboard_overflows(void);
// Last validated frame[22] from the current online session (0..8), or -1.
// A queued command or successful bus write never updates this value. STM32
// currently reports the REQUESTED level immediately on command receipt, before
// its task applies PWM; this is device feedback, NOT proof of applied light.
int mix_keyboard_backlight_level(void);
// Queue an absolute 0..8 level; invalid values are ignored. Latest set wins and
// discards all older queued steps. A set may be queued offline and survives
// errors/reconnect until a successful write; init clears all pending commands.
// Later steps add to this target modulo 9. This is a one-shot command, not a
// persistent lock policy, so callers must suppress new steps while locked.
void mix_keyboard_backlight_set(uint8_t level);
// Queue a step modulo 9 while online (ignored offline). Without a pending set,
// use the next validated device report as the base. Errors/session changes
// discard queued steps, but retain the pending absolute set, if any.
// Both mutators are callback-safe: no I2C until tick finishes input dispatch.
// Call all driver APIs from the same task as tick; they are not thread-safe.
void mix_keyboard_backlight_step(void);
