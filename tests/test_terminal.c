/* SPDX-License-Identifier: MIT
 * Terminal parser coverage for the sequences a full-screen TUI relies on.
 * Pure host test: no ESP headers, no hardware, no timing assumptions.
 */
#include "mix_terminal.h"
#include <assert.h>
#include <stdio.h>
#include <string.h>

static char replies[256];
static size_t reply_len;
static void capture(const uint8_t *bytes, size_t len, void *ctx) {
    (void)ctx;
    assert(reply_len + len < sizeof(replies));
    memcpy(replies + reply_len, bytes, len);
    reply_len += len;
    replies[reply_len] = 0;
}
static void feed(const char *s) { mix_terminal_feed((const uint8_t *)s, strlen(s)); }
static void reset(void) { mix_terminal_init(); reply_len = 0; replies[0] = 0; }
static uint32_t at(int row, int col) { return mix_terminal_row(row)[col].codepoint; }
static void expect_row(int row, const char *ascii) {
    const mix_cell_t *cells = mix_terminal_row(row);
    assert(cells);
    for (int i = 0; ascii[i]; i++) assert(cells[i].codepoint == (uint32_t)ascii[i]);
}

static void test_alternate_screen(void) {
    reset();
    feed("primary\r\n");
    for (int i = 0; i < 40; i++) feed("scrollback\r\n");
    assert(!mix_terminal_alternate());
    int history_before = (mix_terminal_scroll(9999), mix_terminal_scroll_offset());
    assert(history_before > 0);
    mix_terminal_scroll(-9999);

    feed("\033[?1049h");
    assert(mix_terminal_alternate());
    /* Entering clears the alternate grid and homes the cursor. */
    assert(mix_terminal_cursor_x() == 0 && mix_terminal_cursor_y() == 0);
    assert(at(0, 0) == ' ');
    feed("ALT");
    expect_row(0, "ALT");

    /* Scrollback is frozen while the alternate screen owns the grid. */
    mix_terminal_scroll(10);
    assert(mix_terminal_scroll_offset() == 0);

    feed("\033[?1049l");
    assert(!mix_terminal_alternate());
    /* The primary screen and its scrollback survived untouched. */
    mix_terminal_scroll(9999);
    assert(mix_terminal_scroll_offset() == history_before);
    mix_terminal_scroll(-9999);

    /* A second entry must not show the previous session's text. */
    feed("\033[?1049h");
    assert(at(0, 0) == ' ');
    feed("\033[?1049l");

    /* The older 47 and 1047 spellings select the same buffer. */
    feed("\033[?47h");
    assert(mix_terminal_alternate());
    feed("\033[?47l");
    assert(!mix_terminal_alternate());
    feed("\033[?1047h");
    assert(mix_terminal_alternate());
    feed("\033[?1047l");
    assert(!mix_terminal_alternate());
}

static void test_line_and_character_editing(void) {
    reset();
    feed("one\r\ntwo\r\nthree\r\n");
    feed("\033[2;1H\033[2L");            /* insert two lines above "two" */
    expect_row(0, "one");
    assert(at(1, 0) == ' ' && at(2, 0) == ' ');
    expect_row(3, "two");
    expect_row(4, "three");

    feed("\033[2;1H\033[2M");            /* delete them again */
    expect_row(0, "one");
    expect_row(1, "two");
    expect_row(2, "three");

    reset();
    feed("abcdef\033[1;1H\033[2@");      /* insert two blanks at the start */
    expect_row(0, "  abcdef");
    feed("\033[1;1H\033[2P");            /* delete them */
    expect_row(0, "abcdef");
    feed("\033[1;2H\033[3X");            /* erase three characters in place */
    assert(at(0, 0) == 'a');
    assert(at(0, 1) == ' ' && at(0, 2) == ' ' && at(0, 3) == ' ');
    assert(at(0, 4) == 'e');
    assert(mix_terminal_cursor_x() == 1); /* ECH never moves the cursor */

    /* Insert and delete stay inside the row: nothing may spill past the edge. */
    reset();
    for (int i = 0; i < MIX_TERM_COLS; i++) feed("z");
    feed("\033[1;1H\033[200@");
    for (int i = 0; i < MIX_TERM_COLS; i++) assert(at(0, i) == ' ');
}

static void test_scrolling_and_regions(void) {
    reset();
    feed("a\r\nb\r\nc\r\nd\r\n");
    feed("\033[2S");                     /* scroll the whole screen up twice */
    expect_row(0, "c");
    expect_row(1, "d");

    feed("\033[2T");                     /* and back down */
    assert(at(0, 0) == ' ' && at(1, 0) == ' ');
    expect_row(2, "c");

    /* Reverse index at the top of a region scrolls that region only. */
    reset();
    feed("\033[1;3r");                   /* region rows 1..3 */
    feed("\033[4;1Hkeep");               /* outside the region */
    feed("\033[1;1Htop");
    feed("\033M");
    assert(at(0, 0) == ' ');
    expect_row(1, "top");
    expect_row(3, "keep");

    /* Origin mode makes row 1 the top of the region. */
    reset();
    feed("\033[5;10r\033[?6h\033[1;1Hx");
    assert(at(4, 0) == 'x');
    feed("\033[?6l\033[1;1Hy");
    assert(at(0, 0) == 'y');
}

static void test_colors(void) {
    reset();
    feed("\033[31mA");
    assert(mix_terminal_row(0)[0].fg == 1);
    feed("\033[91mB");
    assert(mix_terminal_row(0)[1].fg == 9);
    feed("\033[38;5;196mC");
    assert(mix_terminal_row(0)[2].fg == 196);
    feed("\033[48;5;21mD");
    assert(mix_terminal_row(0)[3].bg == 21);
    feed("\033[38;2;10;20;30mE");
    assert(mix_terminal_row(0)[4].fg == MIX_COLOR_RGB(10, 20, 30));
    /* Both ITU colon spellings, with and without the colourspace field. */
    feed("\033[38:2:40:50:60mF");
    assert(mix_terminal_row(0)[5].fg == MIX_COLOR_RGB(40, 50, 60));
    feed("\033[38:2::70:80:90mG");
    assert(mix_terminal_row(0)[6].fg == MIX_COLOR_RGB(70, 80, 90));
    feed("\033[39;49mH");
    assert(mix_terminal_row(0)[7].fg == MIX_COLOR_DEFAULT_FG);
    assert(mix_terminal_row(0)[7].bg == MIX_COLOR_DEFAULT_BG);
    /* An underline colour is parsed and discarded without eating the rest. */
    feed("\033[58;2;1;2;3;31mI");
    assert(mix_terminal_row(0)[8].fg == 1);
    /* Out-of-range channels clamp rather than wrap. */
    feed("\033[38;2;999;0;0mJ");
    assert(mix_terminal_row(0)[9].fg == MIX_COLOR_RGB(255, 0, 0));

    reset();
    feed("\033[1;4;2mK");
    assert(mix_terminal_row(0)[0].flags == (MIX_ATTR_BOLD | MIX_ATTR_UNDERLINE | MIX_ATTR_DIM));
    feed("\033[24mL");
    assert(!(mix_terminal_row(0)[1].flags & MIX_ATTR_UNDERLINE));
    feed("\033[mM");
    assert(mix_terminal_row(0)[2].flags == 0);

    /* Background-colour erase keeps the background but drops attributes. */
    reset();
    feed("\033[1;4;41m\033[2J");
    assert(mix_terminal_row(0)[0].bg == 1);
    assert(mix_terminal_row(0)[0].flags == 0);
    assert(mix_terminal_row(0)[0].fg == MIX_COLOR_DEFAULT_FG);
}

static void test_replies(void) {
    reset();
    mix_terminal_set_reply(capture, NULL);
    feed("\033[6n");
    assert(!strcmp(replies, "\033[1;1R"));

    reply_len = 0; replies[0] = 0;
    feed("\033[10;20H\033[6n");
    assert(!strcmp(replies, "\033[10;20R"));

    /* Origin mode reports the row relative to the scrolling region. */
    reply_len = 0; replies[0] = 0;
    feed("\033[5;20r\033[?6h\033[1;1H\033[6n");
    assert(!strcmp(replies, "\033[1;1R"));

    reply_len = 0; replies[0] = 0;
    feed("\033[5n");
    assert(!strcmp(replies, "\033[0n"));

    reply_len = 0; replies[0] = 0;
    feed("\033[c");
    assert(!strcmp(replies, "\033[?62;22c"));

    /* Secondary device attributes share the final byte and must stay silent
     * rather than answer as if they were the primary query. */
    reply_len = 0; replies[0] = 0;
    feed("\033[>c");
    assert(reply_len == 0);

    mix_terminal_set_reply(NULL, NULL);
    reply_len = 0; replies[0] = 0;
    feed("\033[6n");
    assert(reply_len == 0); /* no callback means no reply, never a crash */
}

static void test_tabs_and_reset(void) {
    reset();
    feed("\033[1;5H\033H");              /* set a stop at column 5 */
    feed("\033[1;1H\t");
    assert(mix_terminal_cursor_x() == 4);
    feed("\t");
    assert(mix_terminal_cursor_x() == 8);
    feed("\033[Z");
    assert(mix_terminal_cursor_x() == 4);
    feed("\033[2I");
    assert(mix_terminal_cursor_x() == 16);
    feed("\033[3g\033[1;1H\t");          /* clear every stop */
    assert(mix_terminal_cursor_x() == MIX_TERM_COLS - 1);

    /* Soft reset restores modes and attributes but leaves the text alone. */
    reset();
    feed("visible\033[4;38;5;9m\033[2;5r\033[?7l\033[?25l");
    assert(!mix_terminal_cursor_visible());
    feed("\033[!p");
    assert(mix_terminal_cursor_visible());
    feed("\033[1;1HX");
    assert(mix_terminal_row(0)[0].flags == 0);
    assert(mix_terminal_row(0)[0].fg == MIX_COLOR_DEFAULT_FG);
}

static void test_repeat_and_geometry(void) {
    reset();
    feed("A\033[3b");
    expect_row(0, "AAAA");
    assert(mix_terminal_cursor_x() == 4);

    /* A repeat count larger than the grid is bounded, not unbounded work. */
    feed("\033[2J\033[1;1HB\033[99999b");
    assert(mix_terminal_cursor_x() < MIX_TERM_COLS);

    assert(mix_terminal_cols() == MIX_TERM_COLS);
    assert(mix_terminal_rows() == MIX_TERM_ROWS);
    assert(mix_terminal_resize(64, 22));
    assert(mix_terminal_cols() == 64 && mix_terminal_rows() == 22);
    assert(!mix_terminal_row(22));
    assert(mix_terminal_row(21));
    /* Resizing clears: a stale coordinate is worse than a blank redraw. */
    assert(at(0, 0) == ' ');
    feed("edge");
    expect_row(0, "edge");
    /* Wrapping now happens at the narrower width. */
    feed("\033[2J\033[1;1H");
    for (int i = 0; i < 64; i++) feed("w");
    assert(mix_terminal_cursor_y() == 0);
    feed("w");
    assert(mix_terminal_cursor_y() == 1);

    assert(!mix_terminal_resize(4, 10));      /* below the floor */
    assert(!mix_terminal_resize(200, 10));    /* above the buffer */
    assert(mix_terminal_cols() == 64);
    assert(mix_terminal_resize(MIX_TERM_COLS, MIX_TERM_ROWS));
}

static void test_wide_characters(void) {
    reset();
    feed("\xe4\xb8\xad\xe6\x96\x87");     /* 中文 */
    assert(at(0, 0) == 0x4e2d && mix_terminal_row(0)[0].width == 2);
    assert(mix_terminal_row(0)[1].width == 0);
    assert(at(0, 2) == 0x6587 && mix_terminal_row(0)[2].width == 2);

    /* Erasing half a wide glyph must take the whole glyph with it. */
    feed("\033[1;2H\033[1X");
    assert(at(0, 0) == ' ' && mix_terminal_row(0)[0].width == 1);
    assert(at(0, 1) == ' ' && mix_terminal_row(0)[1].width == 1);

    /* Deleting characters cannot leave an orphaned continuation cell. */
    reset();
    feed("\xe4\xb8\xadx");
    feed("\033[1;1H\033[1P");
    assert(mix_terminal_row(0)[0].width != 0);
}

static void test_untrusted_output_is_inert(void) {
    reset();
    /* Window, mouse, paste and title controls are accepted and discarded; a
     * stream can change what is shown but never what the device does. */
    feed("\033[?1000h\033[?1002h\033[?1006h\033[?2004h\033]0;REBOOT_TO_BOOT_MODE\007");
    feed("\033[8;50;200t\033[ q");
    feed("OK");
    expect_row(0, "OK");
    assert(mix_terminal_cols() == MIX_TERM_COLS);
    assert(mix_terminal_rows() == MIX_TERM_ROWS);

    /* An over-long parameter run is dropped through its final byte instead of
     * overflowing the fixed parameter array. */
    reset();
    feed("\033[");
    for (int i = 0; i < 200; i++) feed("1;");
    feed("m");
    feed("Z");
    assert(at(0, 0) == 'Z');
}

int main(void) {
    test_alternate_screen();
    test_line_and_character_editing();
    test_scrolling_and_regions();
    test_colors();
    test_replies();
    test_tabs_and_reset();
    test_repeat_and_geometry();
    test_wide_characters();
    test_untrusted_output_is_inert();
    puts("MixOS terminal sequence tests passed");
    return 0;
}
