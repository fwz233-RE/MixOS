#include "mix_input.h"
#include <assert.h>
#include <stdio.h>
#include <string.h>
static uint8_t text[2048];
static size_t length;
static int actions[MIX_KEY_IME_TOGGLE + 1];
static bool reset_on_lock, reset_on_ime;
_Static_assert(MIX_KEY_TEXT == 0 && MIX_KEY_LOCK == 1 &&
               MIX_KEY_BRIGHT_UP == 2 && MIX_KEY_BRIGHT_DOWN == 3 &&
               MIX_KEY_VOLUME_UP == 4 && MIX_KEY_VOLUME_DOWN == 5 &&
               MIX_KEY_BACKLIGHT == 6 && MIX_KEY_IME_TOGGLE == 7,
               "new local actions must not renumber existing actions");
_Static_assert(MIX_KEY_HOME == MIX_KEY_LOCK, "legacy HOME must remain a LOCK alias");
static void receive(int action, const uint8_t *bytes, size_t len, void *ctx) {
    assert(ctx == (void *)123);
    assert(action >= 0 && action <= MIX_KEY_IME_TOGGLE);
    ++actions[action];
    if (action == MIX_KEY_TEXT) {
        assert(length + len <= sizeof(text));
        memcpy(text + length, bytes, len); length += len;
    } else assert(bytes == NULL && len == 0);
    if ((reset_on_lock && action == MIX_KEY_LOCK) ||
        (reset_on_ime && action == MIX_KEY_IME_TOGGLE)) mix_input_reset();
}
static void init(void) { length = 0; reset_on_lock = reset_on_ime = false; memset(actions, 0, sizeof(actions)); mix_input_init(receive, (void *)123); }
static void ev(uint8_t r, uint8_t c, bool down) { mix_input_event(r, c, down, 100); }
static void tap(uint8_t r, uint8_t c) { ev(r,c,true); ev(r,c,false); }
static void expect(const char *s) { assert(length == strlen(s)); assert(!memcmp(text,s,length)); }
int main(void) {
    init(); tap(2,1); tap(3,1); tap(4,1); tap(1,0); expect("qaz1");
    init(); ev(4,0,true); ev(4,10,true); tap(2,1); ev(4,0,false); tap(3,1); ev(4,10,false); tap(4,1); expect("QAz");
    init(); ev(5,0,true); for (uint8_t c=1;c<=8;++c) tap(2,c); for (uint8_t c=2;c<=9;++c) tap(3,c); for (uint8_t c=4;c<=8;++c) tap(4,c); expect("/?`~-_=+,.\\|[]{}<>'\":");
    init(); ev(5,1,true); tap(2,3); tap(5,3); tap(3,3); assert(length==3 && text[0]==5 && text[1]==0 && text[2]==4);
    init(); ev(5,2,true); tap(2,1); expect("\033q");
    init(); ev(5,2,true); ev(5,1,true); tap(4,3); expect("\033\003");
    /* Both Shifts and all five physical contacts form one logical shortcut.
     * It is local-only, one-shot, and can be rearmed only by releasing Space. */
    for (uint8_t shift_col = 0; shift_col <= 10; shift_col += 10) {
        for (uint8_t contact = 3; contact <= 7; ++contact) {
            init(); ev(4,shift_col,true); ev(5,contact,true); ev(5,contact,true);
            mix_input_tick(550); mix_input_tick(1000);
            assert(!length && !actions[MIX_KEY_TEXT] && actions[MIX_KEY_IME_TOGGLE]==1);
            ev(4,shift_col,false); mix_input_tick(2000);
            ev(4,shift_col,true); mix_input_tick(3000);
            assert(!length && actions[MIX_KEY_IME_TOGGLE]==1);
            ev(5,contact,false); tap(5,contact);
            assert(!length && actions[MIX_KEY_IME_TOGGLE]==2);
        }
    }
    init(); ev(4,0,true); ev(4,10,true);
    for (uint8_t c=3;c<=7;++c) ev(5,c,true);
    for (uint8_t c=3;c<7;++c) ev(5,c,false);
    ev(5,3,true); ev(5,7,false); mix_input_tick(5000);
    assert(!length && actions[MIX_KEY_IME_TOGGLE]==1);
    ev(5,3,false); tap(5,6);
    assert(!length && actions[MIX_KEY_IME_TOGGLE]==2);
    /* Adding Shift during an ordinary held/repeating Space cannot toggle. */
    init(); ev(5,3,true); mix_input_tick(550); ev(4,0,true);
    ev(5,4,true); mix_input_tick(600); mix_input_tick(5000);
    assert(actions[MIX_KEY_IME_TOGGLE]==0); expect("  ");
    ev(4,0,false); mix_input_tick(6000); expect("  ");
    ev(5,3,false); ev(5,4,false); ev(4,0,true); tap(5,7);
    assert(actions[MIX_KEY_IME_TOGGLE]==1); expect("  ");
    /* Fn keeps its local backlight shortcut; Ctrl/Alt/Sym are not IME. */
    init(); ev(4,0,true); ev(3,0,true); tap(5,3);
    assert(!length && actions[MIX_KEY_BACKLIGHT]==1 && !actions[MIX_KEY_IME_TOGGLE]);
    init(); ev(4,0,true); ev(5,1,true); tap(5,3);
    assert(length==1 && text[0]==0 && !actions[MIX_KEY_IME_TOGGLE]);
    init(); ev(4,0,true); ev(5,2,true); tap(5,3); expect("\033 ");
    assert(!actions[MIX_KEY_IME_TOGGLE]);
    init(); ev(4,0,true); ev(5,0,true); tap(5,3); expect(" ");
    assert(!actions[MIX_KEY_IME_TOGGLE]);
    /* A reset inside the action callback wins over repeat and held contacts. */
    init(); reset_on_ime=true; ev(4,0,true); ev(5,3,true);
    ev(5,4,true); ev(5,3,false); tap(2,1); mix_input_tick(1000);
    ev(4,0,false); ev(5,4,false); tap(2,1); expect("q");
    assert(actions[MIX_KEY_IME_TOGGLE]==1);
    init(); ev(4,0,true); mix_input_reset(); tap(5,3); mix_input_tick(1000);
    assert(!length && !actions[MIX_KEY_IME_TOGGLE]);
    ev(4,0,false); ev(4,0,true); tap(5,3);
    assert(!length && actions[MIX_KEY_IME_TOGGLE]==1);
    assert(strcmp(MIX_IME_TOGGLE_SEQUENCE, "\033[32;2u")==0);
    init(); tap(2,0); tap(1,10); tap(3,10); tap(4,9); tap(5,8); tap(5,9); tap(5,10); expect("\t\177\r\033[A\033[D\033[B\033[C");
    init(); ev(3,0,true); tap(2,0); tap(1,10); tap(4,9); tap(5,8); tap(5,9); tap(5,10); expect("\033\033[3~\033[5~\033[H\033[6~\033[F");
    init(); ev(3,0,true); for (uint8_t c=0;c<10;++c) tap(1,c); tap(2,9); tap(2,10); expect("\033OP\033OQ\033OR\033OS\033[15~\033[17~\033[18~\033[19~\033[20~\033[21~\033[23~\033[24~");
    init(); ev(4,0,true); ev(5,1,true); tap(4,9); expect("\033[1;6A");
    init(); for (uint8_t c=3;c<=7;++c) ev(5,c,true); ev(5,3,true); expect(" ");
    ev(5,3,false); mix_input_tick(550); expect("  ");
    for (uint8_t c=4;c<=7;++c) { ev(5,c,false); } ev(5,7,false); mix_input_tick(9999); expect("  ");
    tap(5,6); expect("   ");
    init(); ev(3,0,true); for (uint8_t c=3;c<=7;++c) ev(5,c,true); ev(5,3,true); mix_input_tick(1000); assert(actions[MIX_KEY_BACKLIGHT]==1 && length==0);
    for (uint8_t c=3;c<=7;++c) ev(5,c,false);
    tap(5,7); assert(actions[MIX_KEY_BACKLIGHT]==2 && length==0);
    const uint8_t columns[] = {1,2,3,7,8,9};
    const int expected[] = {MIX_KEY_LOCK,MIX_KEY_VOLUME_DOWN,MIX_KEY_VOLUME_UP,MIX_KEY_BRIGHT_DOWN,MIX_KEY_BRIGHT_UP,MIX_KEY_BACKLIGHT};
    for (unsigned i=0;i<sizeof(columns);++i) {
        init(); ev(0,columns[i],true); ev(0,columns[i],true);
        mix_input_tick(550); mix_input_tick(100000);
        assert(!length && actions[expected[i]]==1);
        for (int a=1;a<=MIX_KEY_IME_TOGGLE;++a) assert(actions[a]==(a==expected[i]));
        ev(0,columns[i],false); ev(0,columns[i],false); mix_input_tick(100100);
        assert(actions[expected[i]]==1);
        tap(0,columns[i]); assert(actions[expected[i]]==2);
    }
    init(); tap(0,9);
    ev(3,0,true); tap(0,9); mix_input_tick(1000); ev(3,0,false);
    ev(5,0,true); tap(0,9); mix_input_tick(2000); assert(actions[MIX_KEY_BACKLIGHT]==1 && !length);
    // A LOCK callback may reset while modifiers/keys are still physically held.
    init(); reset_on_lock=true; ev(4,0,true); ev(2,1,true); ev(0,1,true);
    ev(0,1,true); mix_input_tick(1000); tap(3,1); ev(0,1,false); tap(3,1);
    ev(4,0,false); ev(2,1,false); tap(3,1); expect("Qa");
    assert(actions[MIX_KEY_LOCK]==1);
    tap(0,1); assert(actions[MIX_KEY_LOCK]==2);
    init(); ev(2,1,true); mix_input_tick(549); expect("q"); mix_input_tick(550); expect("qq"); mix_input_tick(100000); expect("qqq"); mix_input_tick(100001); expect("qqq"); ev(2,1,false); mix_input_tick(100100); expect("qqq");
    init(); mix_input_event(2,1,true,UINT32_MAX-100); mix_input_tick(348); expect("q"); mix_input_tick(349); expect("qq");
    init(); ev(4,0,true); ev(2,1,true); mix_input_reset(); mix_input_tick(999); tap(3,1); ev(4,0,false); tap(4,1); ev(2,1,false); tap(3,1); expect("Qa");
    init(); mix_input_reset(); tap(2,1); tap(3,1); expect("a");
    init(); for (unsigned r=0;r<256;++r) for(unsigned c=0;c<256;++c) if (r>=6 || c>=11 || (r==0 && !(0x038eU & (1U << c)))) { ev((uint8_t)r,(uint8_t)c,true); ev((uint8_t)r,(uint8_t)c,false); } assert(!length);
    puts("input: layout, modifiers, repeats, local IME toggle, space, reset, bounds passed");
    return 0;
}
