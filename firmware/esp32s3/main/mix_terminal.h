#pragma once
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
/* Buffer maxima. The active grid is chosen with mix_terminal_resize and is
 * never larger than these, so the geometry setting costs no extra memory. */
#define MIX_TERM_COLS 80
#define MIX_TERM_ROWS 28
#define MIX_TERM_HISTORY 100
/* A cell colour is exactly one of:
 *   0..255                   xterm palette index
 *   MIX_COLOR_RGB(r,g,b)     direct 24-bit colour
 *   MIX_COLOR_DEFAULT_FG/BG  the renderer's own theme colour
 * The parser never invents pixels; the renderer owns the palette. */
typedef uint32_t mix_color_t;
#define MIX_COLOR_DEFAULT_FG 0xFFFFFFFFu
#define MIX_COLOR_DEFAULT_BG 0xFFFFFFFEu
#define MIX_COLOR_RGB_FLAG   0x01000000u
#define MIX_COLOR_RGB(r,g,b) (MIX_COLOR_RGB_FLAG|((uint32_t)(r)<<16)|((uint32_t)(g)<<8)|(uint32_t)(b))
#define MIX_COLOR_IS_RGB(c)  (((c)&0xFF000000u)==MIX_COLOR_RGB_FLAG)
#define MIX_COLOR_IS_INDEX(c) ((c)<256u)
#define MIX_COLOR_R(c) ((uint8_t)(((c)>>16)&0xFFu))
#define MIX_COLOR_G(c) ((uint8_t)(((c)>>8)&0xFFu))
#define MIX_COLOR_B(c) ((uint8_t)((c)&0xFFu))
#define MIX_ATTR_BOLD      1u
#define MIX_ATTR_INVERSE   2u
#define MIX_ATTR_UNDERLINE 4u
#define MIX_ATTR_DIM       8u
/* width=0 wide continuation / 1 narrow / 2 wide */
typedef struct { uint32_t codepoint; mix_color_t fg,bg; uint8_t flags,width; } mix_cell_t;
/* Cursor-position and device-attribute queries need an answer. Without one a
 * TUI that probes the terminal at startup waits forever, so replies travel
 * back to the host as ordinary terminal input. The callback runs inside
 * mix_terminal_feed and must not re-enter the parser. */
typedef void (*mix_terminal_reply_cb)(const uint8_t *bytes,size_t len,void *ctx);
void mix_terminal_set_reply(mix_terminal_reply_cb cb,void *ctx);
void mix_terminal_init(void);
/* Clears the grid and scrollback: a geometry change invalidates every stored
 * coordinate, and a blank redraw is honest where a reflow guess is not. */
bool mix_terminal_resize(int cols,int rows);
int mix_terminal_cols(void);
int mix_terminal_rows(void);
void mix_terminal_feed(const uint8_t *data,size_t len);
const mix_cell_t *mix_terminal_row(int row); /* visible row; incorporates scrollback offset */
bool mix_terminal_dirty(int row);
void mix_terminal_clean(void);
void mix_terminal_invalidate(void);
int mix_terminal_cursor_x(void);
int mix_terminal_cursor_y(void);
bool mix_terminal_cursor_visible(void);
void mix_terminal_scroll(int lines); /* positive=older, negative=newer */
int mix_terminal_scroll_offset(void);
/* True while the alternate screen owns the grid. Scrollback is frozen then,
 * so the older/newer controls have nothing to show. */
bool mix_terminal_alternate(void);
