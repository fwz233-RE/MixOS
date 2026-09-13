#pragma once
#include <stdbool.h>
#include <stdint.h>
#define MIX_VERSION "0.2.0-ota"
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
} mix_view_t;
typedef enum {
    MIX_ACTION_TERMINAL_OPEN=1, MIX_ACTION_TERMINAL_CLOSE,
    MIX_ACTION_BRIGHT_UP, MIX_ACTION_BRIGHT_DOWN, MIX_ACTION_KBD_BACKLIGHT,
    MIX_ACTION_JOB_START, MIX_ACTION_JOB_CANCEL, MIX_ACTION_INPUT_RESET
} mix_action_kind_t;
typedef struct { mix_action_kind_t kind; int value; } mix_action_t;
