/* SPDX-License-Identifier: MIT
 * The four Material Design 3 colour schemes.
 *
 * These keep the identities the device already had — graphite mint, paper
 * forest, midnight blue, warm ember — and extend each from seven colours to
 * the Material 3 roles. The three additions that matter are:
 *
 *  - surface_container_highest, so a pressed control has somewhere to go that
 *    is not the same tone as a resting raised one;
 *  - outline, so a divider or a focus ring is not borrowed from body text;
 *  - on_primary / primary_container, so an accent-filled button can state its
 *    own label colour instead of assuming the page background is readable on
 *    the accent, which is false for the light scheme.
 *
 * Contrast was checked against the Material 3 requirement that body text reach
 * 4.5:1 against the surface it sits on, and that on_primary reach it against
 * primary. The light scheme is where this bit: its accent is a mid-tone green,
 * so on_primary is white rather than the page background.
 */
#include "mix_md3.h"

const mix_md3_scheme_t mix_md3_schemes[MIX_THEME_COUNT] = {
    {   /* Graphite mint — the default dark scheme. */
        .surface                   = 0x13171A,
        .surface_container         = 0x1D2327,
        .surface_container_high    = 0x2B3338,
        .surface_container_highest = 0x363F45,
        .on_surface                = 0xF0F5F3,
        .on_surface_variant        = 0xA3B2B0,
        .outline                   = 0x5A6A68,
        .primary                   = 0x87EBC2,
        .on_primary                = 0x00382A,
        .primary_container         = 0x00513C,
        .on_primary_container      = 0xA3F7D8,
        .error                     = 0xF4BE6F,
        .name_en = "Graphite", .name_zh = "石墨薄荷",
    },
    {   /* Paper forest — the one light scheme. */
        .surface                   = 0xF1F4F0,
        .surface_container         = 0xFFFFFB,
        .surface_container_high    = 0xDEE7DD,
        .surface_container_highest = 0xD0DACF,
        .on_surface                = 0x1D2E26,
        .on_surface_variant        = 0x4E655A,
        .outline                   = 0x7E938A,
        .primary                   = 0x187451,
        /* White, not the page background: the page is near-white itself, so
         * using it here would put a pale label on a mid-green fill. */
        .on_primary                = 0xFFFFFF,
        .primary_container         = 0xA6F2CE,
        .on_primary_container      = 0x00210F,
        .error                     = 0x97500E,
        .name_en = "Paper", .name_zh = "纸白森林",
    },
    {   /* Midnight blue. */
        .surface                   = 0x11192A,
        .surface_container         = 0x1A263B,
        .surface_container_high    = 0x273750,
        .surface_container_highest = 0x32445F,
        .on_surface                = 0xEBF2FF,
        .on_surface_variant        = 0xA5BBD7,
        .outline                   = 0x637A94,
        .primary                   = 0x88C1FF,
        .on_primary                = 0x00325A,
        .primary_container         = 0x004880,
        .on_primary_container      = 0xD0E4FF,
        .error                     = 0xFFCA84,
        .name_en = "Midnight", .name_zh = "午夜蓝",
    },
    {   /* Warm ember. */
        .surface                   = 0x1D1816,
        .surface_container         = 0x2A221D,
        .surface_container_high    = 0x3C3024,
        .surface_container_highest = 0x4A3C2E,
        .on_surface                = 0xFFF2DD,
        .on_surface_variant        = 0xC6B092,
        .outline                   = 0x8A7A63,
        .primary                   = 0xF4BC6A,
        .on_primary                = 0x422C00,
        .primary_container         = 0x5E4100,
        .on_primary_container      = 0xFFDEA8,
        .error                     = 0xFF9A80,
        .name_en = "Ember", .name_zh = "暖琥珀",
    },
};
