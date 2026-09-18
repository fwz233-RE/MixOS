#pragma once
#include <stdbool.h>
#include <stdint.h>
#define MIX_VERSION "0.3.0-deck"
#define MIX_SSID_MAX 32
/* The four things the launcher can start. AGENT is a deliberate placeholder:
 * it opens a screen that says so and starts nothing. */
typedef enum { MIX_APP_TRANSLATE=0, MIX_APP_NOTES, MIX_APP_AGENT, MIX_APP_SHELL, MIX_APP_COUNT } mix_app_t;
typedef struct {
    bool battery_valid, usb_valid, soc_valid, calibration_verified;
    float battery_v, battery_a, usb_v, usb_a, soc, capacity_mah, runtime_hours;
    bool linux_online, terminal_open, keyboard_online;
    bool headphone_valid, headphone_inserted, audio_ready;
    float mic_l, mic_r;
    float linux_cpu, linux_temp;
    uint32_t linux_mem_used_kib, linux_mem_total_kib, linux_uptime_s;
    uint32_t keyboard_overflows, free_psram;
    int brightness, job_percent;
    bool job_running;
    /* True while a host-authorized maintenance operation owns the link: either
     * the legacy ROM download grant or an A/B image transfer. Local input and
     * the terminal stay out of the way until it clears. */
    bool maintenance_busy;
    /* mix_ota_state_t as a plain byte, so the UI needs no OTA header. */
    uint8_t ota_state;
    int ota_percent;
    /* True while this build is on trial and can still be rolled back. */
    bool firmware_on_trial;
    uint16_t sensors_present, sensors_checked;
    /* Reported by the Linux host, not measured locally. wifi_signal is -1 when
     * the host did not report one; an unknown value is never shown as zero. */
    bool wifi_reported, wifi_connected;
    int wifi_signal;
    char wifi_ssid[MIX_SSID_MAX + 1];
    char host_ip[40];
    /* Wall clock from the host. The device has no running RTC of its own, so
     * zero means the status bar has no time to show. */
    uint32_t host_time_s;
    int16_t host_tz_offset_min;
    /* Which application owns the open session, valid only while terminal_open. */
    uint8_t running_app;
} mix_view_t;
typedef enum {
    MIX_ACTION_TERMINAL_OPEN=1, MIX_ACTION_TERMINAL_CLOSE,
    MIX_ACTION_BRIGHT_UP, MIX_ACTION_BRIGHT_DOWN, MIX_ACTION_KBD_BACKLIGHT,
    MIX_ACTION_JOB_START, MIX_ACTION_JOB_CANCEL, MIX_ACTION_INPUT_RESET,
    /* value carries a mix_app_t */
    MIX_ACTION_APP_OPEN,
    MIX_ACTION_NET_SCAN, MIX_ACTION_NET_CONNECT, MIX_ACTION_NET_FORGET,
    MIX_ACTION_VOLUME_UP, MIX_ACTION_VOLUME_DOWN,
    /* value carries the terminal geometry preset index */
    MIX_ACTION_TERM_GEOMETRY
} mix_action_kind_t;
typedef struct { mix_action_kind_t kind; int value; } mix_action_t;
