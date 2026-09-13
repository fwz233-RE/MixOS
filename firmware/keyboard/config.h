// SPDX-License-Identifier: GPL-2.0-or-later
#pragma once
// Scan and service I2C/backlight even if USB is absent or suspended.
#define NO_USB_STARTUP_CHECK
#define NO_SUSPEND_POWER_DOWN
#define NO_ACTION_TAPPING
#define NO_ACTION_ONESHOT
#define DEBOUNCE 5
#ifdef USB_WAIT_FOR_ENUMERATION
#    error MixOS must not wait for USB enumeration
#endif
#ifdef WAIT_FOR_USB
#    error MixOS must not wait for USB enumeration
#endif
