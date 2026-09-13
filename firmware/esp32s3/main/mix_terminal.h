#pragma once
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#define MIX_TERM_COLS 80
#define MIX_TERM_ROWS 28
#define MIX_TERM_HISTORY 100
typedef struct { uint32_t codepoint; uint8_t fg,bg,flags,width; } mix_cell_t;
/* flags bit0=bold bit1=inverse, width=0 wide continuation/1 narrow/2 wide */
void mix_terminal_init(void);
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
