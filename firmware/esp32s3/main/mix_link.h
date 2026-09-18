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
/* Opens one session running the named application at the current terminal
 * geometry. The host maps the identifier through a fixed table; no command
 * string crosses the link. */
bool mix_link_open_app(mix_app_t app);
void mix_link_close_terminal(void);
bool mix_link_input(const uint8_t *bytes,size_t len);
/* Tells the host about a local geometry change. Only meaningful while a
 * session is open; the next open carries the new size by itself. */
void mix_link_resize(int cols,int rows);
void mix_link_job(bool start);

#define MIX_NET_MAX 12
typedef struct {
    char ssid[MIX_SSID_MAX+1];
    uint8_t signal;            /* 0..100 as reported by the host */
    bool secured, known;       /* known: the host already has a saved profile */
} mix_net_entry_t;
/* Ask the host to scan. The result arrives asynchronously; poll the list. */
bool mix_link_net_scan(void);
bool mix_link_net_connect(const char *ssid,const char *passphrase);
bool mix_link_net_forget(const char *ssid);
/* Returns the entry count and points out at the stored array. The array is
 * owned by the link and is only replaced on the UI/main task. */
int mix_link_net_list(const mix_net_entry_t **out);
bool mix_link_net_busy(void);
/* Empty until the host answers a scan or connect request. */
const char *mix_link_net_message(void);

/* Enumeration is diagnostic only, not a trial confirmation signal. */
bool mix_link_usb_mounted(void);
bool mix_link_io_healthy(uint32_t now_ms);
bool mix_link_host_healthy(uint32_t now_ms);
/* Legacy ROM download-mode entry, kept for full-flash recovery. */
bool mix_link_take_boot_request(void);
/* An A/B image was verified and selected; main restarts into it. */
bool mix_link_take_restart_request(void);
bool mix_link_take_input_reset(void);
const char *mix_link_notice(void);
