#pragma once
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include "esp_err.h"
#include "mix_view.h"
/* Call init before USB task startup; start_io only after TinyUSB init.
 * All remaining functions called by UI/main task only. */
esp_err_t mix_link_init(void);
esp_err_t mix_link_start_io(void);
void mix_link_tick(uint32_t now_ms,mix_view_t *view);
bool mix_link_open_terminal(void);
void mix_link_close_terminal(void);
bool mix_link_input(const uint8_t *bytes,size_t len);
void mix_link_job(bool start);
/* True once the USB device is enumerated, regardless of whether a host program
 * has the CDC port open. Used as the A/B update health signal. */
bool mix_link_usb_mounted(void);
/* Legacy ROM download-mode entry, kept for full-flash recovery. */
bool mix_link_take_boot_request(void);
/* An A/B image was verified and selected; main restarts into it. */
bool mix_link_take_restart_request(void);
bool mix_link_take_input_reset(void);
const char *mix_link_notice(void);
