/* SPDX-License-Identifier: MIT
 * Material Design 3 tokens for a 400 PPI panel.
 *
 * Why this file exists. Every size in the previous interface was a raw pixel
 * literal, and the layout was built as if 1024x768 were a desktop window. It
 * is not: the hardware specification for this deck is "3.2 inch, 1024 x 768,
 * 400 PPI". Material Design expresses sizes in density-independent pixels
 * defined at 160 PPI, so on this panel
 *
 *     1 dp = 400 / 160 = 2.5 physical pixels
 *
 * and every pixel literal in the old layout was really a dp value 2.5x smaller
 * than it looked. The four terminal controls in the status bar were 74x30 px,
 * which is 30x12 dp, or about 4.7 x 1.9 mm of glass. Material Design's minimum
 * touch target is 48 dp; these were a quarter of it in one axis and a sixth in
 * the other, which is the whole explanation for controls that cannot be hit.
 *
 * So sizes here are written in dp and sp and converted once, and MIX_TOUCH_MIN
 * is the floor that every interactive element has to clear.
 *
 * The conversion rounds rather than truncates: MIX_DP(9) is 23 px, not 22.
 */
#pragma once

#include <stdint.h>

/* ---------- density ---------- */
/* Numerator and denominator are separate so the arithmetic stays integral and
 * the panel's own figure stays visible instead of becoming a magic 2.5. */
#define MIX_PPI          400
#define MIX_BASELINE_PPI 160
#define MIX_DP(v)  (((v) * MIX_PPI + MIX_BASELINE_PPI / 2) / MIX_BASELINE_PPI)
/* Text is scaled by the same factor; this device has no separate user text
 * scale, so sp and dp coincide. Kept distinct because they mean different
 * things and only one of them would change if a text-size setting appeared. */
#define MIX_SP(v)  MIX_DP(v)

/* Material Design 3 minimum touch target, and the gap that keeps two adjacent
 * targets from being one ambiguous region. */
#define MIX_TOUCH_MIN MIX_DP(48)
#define MIX_TOUCH_GAP MIX_DP(8)

/* ---------- spacing ---------- */
#define MIX_SPACE_1 MIX_DP(4)
#define MIX_SPACE_2 MIX_DP(8)
#define MIX_SPACE_3 MIX_DP(12)
#define MIX_SPACE_4 MIX_DP(16)
#define MIX_SPACE_6 MIX_DP(24)
#define MIX_SPACE_8 MIX_DP(32)

/* Material 3 shape scale. */
#define MIX_RADIUS_XS  MIX_DP(4)
#define MIX_RADIUS_S   MIX_DP(8)
#define MIX_RADIUS_M   MIX_DP(12)
#define MIX_RADIUS_L   MIX_DP(16)
#define MIX_RADIUS_XL  MIX_DP(28)

/* Material 3 top app bar is 64 dp; the old status bar was 48 px, i.e. 19 dp. */
#define MIX_APPBAR_H MIX_DP(64)

/* ---------- type scale ---------- */
/* Material 3 type scale, in sp. Only the steps this interface actually uses
 * are listed, so an unused size cannot drift out of step with the font cache
 * in mix_lv_font.c, which instantiates exactly these. */
#define MIX_TYPE_HEADLINE_S MIX_SP(24)
#define MIX_TYPE_TITLE_L    MIX_SP(22)
#define MIX_TYPE_TITLE_M    MIX_SP(16)
#define MIX_TYPE_BODY_L     MIX_SP(16)
#define MIX_TYPE_BODY_M     MIX_SP(14)
#define MIX_TYPE_LABEL_L    MIX_SP(14)
#define MIX_TYPE_LABEL_M    MIX_SP(12)
#define MIX_TYPE_LABEL_S    MIX_SP(11)

/* ---------- motion ---------- */
/* Material 3 "emphasized" durations. The previous page transition was a
 * six-band wipe driven one band per UI tick, which is neither a duration nor a
 * curve: it ran at whatever rate the loop happened to turn, and every band was
 * a separate unsynchronised copy into the live scanout buffer. */
#define MIX_MOTION_SHORT    150
#define MIX_MOTION_MEDIUM   300
#define MIX_MOTION_LONG     450

/* ---------- colour ---------- */
/* Material 3 colour roles. The previous seven-colour palette is a subset of
 * these; the additional roles are what make a surface hierarchy possible
 * instead of "card" and "raised". Stored as 24-bit RGB and handed to LVGL as
 * lv_color_hex, so the theme is independent of the framebuffer's depth. */
typedef struct {
    uint32_t surface;                  /* page background                     */
    uint32_t surface_container;        /* cards                               */
    uint32_t surface_container_high;   /* raised elements inside a card       */
    uint32_t surface_container_highest;/* pressed / dragged state             */
    uint32_t on_surface;               /* primary text                        */
    uint32_t on_surface_variant;       /* secondary text, icons               */
    uint32_t outline;                  /* borders, dividers                   */
    uint32_t primary;                  /* accent, selected state              */
    uint32_t on_primary;               /* text on an accent fill              */
    uint32_t primary_container;        /* tonal button fill                   */
    uint32_t on_primary_container;     /* text on a tonal fill                */
    uint32_t error;                    /* warnings and failures               */
    const char *name_en;
    const char *name_zh;
} mix_md3_scheme_t;

#define MIX_THEME_COUNT 4

/* The four schemes, keeping the established identities: graphite mint, paper
 * forest, midnight blue, warm ember. */
extern const mix_md3_scheme_t mix_md3_schemes[MIX_THEME_COUNT];
