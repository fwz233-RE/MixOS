/* SPDX-License-Identifier: MIT */
#pragma once

/* Normal OTA/software restart, after the durable intent and reply grace period.
 * One-shot per boot. Does not confirm a trial, alter boot selection, feed or
 * unsubscribe watchdog tasks, or enter ROM download mode. */
void mix_restart(void);
