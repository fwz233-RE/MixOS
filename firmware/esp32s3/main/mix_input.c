#include "mix_input.h"
#include <stdio.h>
#include <string.h>
static mix_key_cb callback;
static void *context;
static uint16_t held[6];
static bool blocked, repeating;
static uint8_t repeat_row, repeat_col;
static uint32_t repeat_at;
static bool real(uint8_t r, uint8_t c) {
    return r < 6 && c < 11 && (r != 0 || ((0x038eU >> c) & 1U));
}
static bool down(unsigned r, unsigned c) { return !!(held[r] & (1U << c)); }
static bool all_up(void) {
    for (unsigned r = 0; r < 6; ++r) if (held[r]) return false;
    return true;
}
static bool space(uint8_t r, uint8_t c) { return r == 5 && c >= 3 && c <= 7; }
static bool modifier(uint8_t r, uint8_t c) {
    return (r == 3 && c == 0) || (r == 4 && (c == 0 || c == 10)) || (r == 5 && c <= 2);
}
static void action(int a) { if (callback) callback(a, NULL, 0, context); }
static bool emit(uint8_t r, uint8_t c, bool repeat) {
    bool fn = down(3, 0), sym = down(5, 0);
    bool shift = down(4, 0) || down(4, 10), ctrl = down(5, 1), alt = down(5, 2);
    if (r == 0) {
        if (repeat) return false;
        static const uint8_t actions[11] = {0,MIX_KEY_HOME,MIX_KEY_VOLUME_UP,MIX_KEY_VOLUME_DOWN,0,0,0,MIX_KEY_BRIGHT_UP,MIX_KEY_BRIGHT_DOWN,MIX_KEY_BACKLIGHT,0};
        if (c == 9 && (fn || sym)) return false; // STM32-local 3-second rescue
        action(actions[c]); return false;
    }
    if (space(r, c) && fn) {
        if (!repeat) action(MIX_KEY_BACKLIGHT);
        return false;
    }
    uint8_t bytes[24];
    size_t n = 0;
    char ch = 0, final = 0;
    unsigned tilde = 0;
    bool ss3 = false;
    if (fn && r == 1 && c < 10) {
        if (c < 4) { final = (char)('P' + c); ss3 = true; }
        else { static const uint8_t f[] = {15,17,18,19,20,21}; tilde = f[c - 4]; }
    } else if (fn && r == 2 && c >= 9) tilde = c == 9 ? 23 : 24;
    else if (r == 1 && c == 10) { if (fn || sym) tilde = 3; else ch = 0x7f; }
    else if (r == 2 && c == 0) ch = fn ? 0x1b : '\t';
    else if (r == 3 && c == 10) ch = '\r';
    else if (r == 4 && c == 9) { if (fn) tilde = 5; else final = 'A'; }
    else if (r == 5 && c >= 8) {
        if (c == 8) final = fn ? 'H' : 'D';
        if (c == 9) { if (fn) tilde = 6; else final = 'B'; }
        if (c == 10) final = fn ? 'F' : 'C';
    } else if (space(r, c)) ch = ' ';
    else {
        static const char base[5][12] = {"", "1234567890", "\tqwertyuiop", "\0asdfghjkl", "\0zxcvbnm;"};
        if (r < 5) ch = base[r][c];
        if (sym && !fn) {
            static const char symbols[5][12] = {"", "", "\0/?`~-_=+", "\0\0,.\\|[]{}", "\0\0\0\0<>\'\":"};
            if (r < 5 && symbols[r][c]) ch = symbols[r][c];
        }
        if (shift) {
            if (ch >= 'a' && ch <= 'z') ch = (char)(ch - 'a' + 'A');
            else {
                const char *plain = "1234567890-=[]\\;',./`";
                const char *upper = "!@#$%^&*()_+{}|:\"<>?~";
                const char *p = ch ? strchr(plain, ch) : NULL;
                if (p) ch = upper[p - plain];
            }
        }
    }
    unsigned mods = 1U + shift + 2U * alt + 4U * ctrl;
    if (final || tilde) {
        if (tilde) n = (size_t)(mods == 1 ? snprintf((char *)bytes, sizeof(bytes), "\033[%u~", tilde) : snprintf((char *)bytes, sizeof(bytes), "\033[%u;%u~", tilde, mods));
        else if (mods != 1) n = (size_t)snprintf((char *)bytes, sizeof(bytes), "\033[1;%u%c", mods, final);
        else { bytes[0] = 0x1b; bytes[1] = ss3 ? 'O' : '['; bytes[2] = (uint8_t)final; n = 3; }
    } else if (ch) {
        uint8_t b = (uint8_t)ch;
        if (ctrl) {
            if (b >= 'a' && b <= 'z') b = (uint8_t)(b - 'a' + 1);
            else if (b >= '@' && b <= '_') b &= 0x1f;
            else if (b == ' ' || b == '2') b = 0;
            else if (b == '?') b = 0x7f;
        }
        if (alt) bytes[n++] = 0x1b;
        bytes[n++] = b;
    }
    if (n && n < sizeof(bytes) && callback) callback(MIX_KEY_TEXT, bytes, n, context);
    return n != 0;
}
void mix_input_init(mix_key_cb cb, void *ctx) {
    callback = cb; context = ctx;
    memset(held, 0, sizeof(held)); blocked = repeating = false;
}
void mix_input_reset(void) { blocked = true; repeating = false; }
void mix_input_event(uint8_t r, uint8_t c, bool pressed, uint32_t now_ms) {
    if (!real(r, c)) return;
    bool was = down(r, c), had_space = !!(held[5] & 0x00f8);
    if (pressed) held[r] |= (uint16_t)(1U << c); else held[r] &= (uint16_t)~(1U << c);
    if (blocked) { if (!pressed && all_up()) blocked = false; return; }
    if (was == pressed) return;
    if (!pressed) {
        if (space(r, c)) { if (!(held[5] & 0x00f8) && repeat_row == 5 && repeat_col == 3) repeating = false; }
        else if (r == repeat_row && c == repeat_col) repeating = false;
        return;
    }
    if (modifier(r, c) || (space(r, c) && had_space)) return;
    if (space(r, c)) c = 3;
    // Set repeat state before calling user code, so a callback reset wins.
    repeat_row = r; repeat_col = c; repeat_at = now_ms + 450;
    repeating = true;
    if (!emit(r, c, false)) repeating = false;
}
void mix_input_tick(uint32_t now_ms) {
    if (blocked || !repeating || (int32_t)(now_ms - repeat_at) < 0) return;
    repeat_at = now_ms + 50; // One repeat maximum per tick; never catch-up burst.
    if (!emit(repeat_row, repeat_col, true)) repeating = false;
}
