#!/bin/bash
# No legacy 20260818 default/diag artifact is safe for the MixOS USB-silent policy.
# This file intentionally performs no flash operation until target validation.
set -eu
printf '%s\n' 'MixOS has no validated STM32 release artifact in this directory.' >&2
printf '%s\n' 'Build the pinned QMK commit and complete docs/keyboard.md checks first.' >&2
printf '%s\n' 'Legacy original-board binaries can emit USB HID input; refusing batch flash.' >&2
exit 1
