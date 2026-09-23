#include "mix_keyboard.h"
#include "mix_kbd_transport.h"
#include "mix_i2c.h"
#include <assert.h>
#include <stdio.h>
#include <string.h>
static mix_kbd_transport_t stm;
static unsigned transactions, reads, emitted, backlight_writes, actions[7];
static uint8_t output[4096], applied_backlight;
static int saved_lock_level;
static bool fail_read, fail_ack, fail_backlight, inject, corrupt_seq, bad_version, corrupt_crc, reset_on_text;
static bool in_callback, lock_on_key, defer_apply;
static void apply_pending(void) {
    if (stm.backlight_pending) {
        applied_backlight=stm.pending_backlight;
        stm.backlight_pending=false;
    }
}
/* The driver reaches the bus through mix_i2c, which owns the bus handle and
 * serialises every transfer, so the fakes live at that boundary now. They keep
 * the same assertions the previous IDF-level fakes made: the slave address,
 * the 400 kHz rate, the single-register read of a whole frame, and the 5 ms
 * deadline are all still checked here. No transfer may run inside a callback. */
i2c_master_dev_handle_t mix_i2c_add_device(uint8_t address, uint32_t scl_hz) {
    assert(!in_callback && address==0x1f && scl_hz==400000); return (void *)2;
}
esp_err_t mix_i2c_rm_device(i2c_master_dev_handle_t d) { assert(!in_callback && d); return ESP_OK; }
esp_err_t mix_i2c_transmit(i2c_master_dev_handle_t d,const uint8_t *p,size_t n,int timeout) {
    assert(!in_callback && d && timeout==5); ++transactions;
    if (fail_ack && p[0]==0x11) return ESP_FAIL;
    if (p[0]==KBD_CMD_BACKLIGHT) {
        assert(n==2 && p[1]<=8); ++backlight_writes;
        if (fail_backlight) return ESP_FAIL;
    }
    mix_kbd_transport_write(&stm,p,n);
    // Production publishes requested feedback before its housekeeping task
    // applies PWM. Keep that distinction visible rather than faking an ACK.
    if (!defer_apply) apply_pending();
    return ESP_OK;
}
esp_err_t mix_i2c_read(i2c_master_dev_handle_t d,uint8_t reg,uint8_t *out,size_t len,int timeout) {
    assert(!in_callback && d && reg==0 && len==KBD_V1_FRAME && timeout==5); ++transactions; ++reads;
    if (fail_read) return ESP_FAIL;
    mix_kbd_transport_frame(&stm,out);
    if (bad_version) out[1]=0;
    if (corrupt_seq && out[3]) out[28]^=1;
    if (bad_version || corrupt_seq) {
        uint16_t crc=mix_kbd_crc16(out,len-2);
        out[len-2]=(uint8_t)crc; out[len-1]=(uint8_t)(crc>>8);
    }
    if (corrupt_crc) out[len-1]^=0x80;
    if (inject) { mix_kbd_transport_event(&stm,2,1,true); inject=false; }
    return ESP_OK;
}
static void cb(int a,const uint8_t *p,size_t n,void *ctx) {
    (void)ctx;
    assert(!in_callback && a>=0 && a<7);
    in_callback=true;
    unsigned before=transactions;
    int reported=mix_keyboard_backlight_level();
    assert(reported>=0 && reported<=8);
    ++actions[a];
    if (a==MIX_KEY_BACKLIGHT) mix_keyboard_backlight_step();
    if (a==MIX_KEY_LOCK && lock_on_key) {
        saved_lock_level=reported;
        mix_keyboard_backlight_set(0);
        mix_keyboard_reset_input();
    }
    if (a==MIX_KEY_TEXT) {
        assert(emitted+n<=sizeof(output)); memcpy(output+emitted,p,n); emitted+=(unsigned)n;
        if (reset_on_text) mix_keyboard_reset_input();
    }
    assert(transactions==before && mix_keyboard_backlight_level()==reported);
    in_callback=false;
}
static void tick(uint32_t now) {
    transactions=0; mix_keyboard_tick(now); assert(transactions<=3 && !in_callback);
    int level=mix_keyboard_backlight_level();
    assert(mix_keyboard_online() ? (level>=0 && level<=8) : level==-1);
}
static void edge(unsigned r,unsigned c,bool down) { mix_kbd_transport_event(&stm,(uint8_t)r,(uint8_t)c,down); }
static void tap(unsigned r,unsigned c) { edge(r,c,true); edge(r,c,false); }
static void init_driver(void) {
    mix_kbd_transport_init(&stm); emitted=reads=backlight_writes=transactions=0;
    memset(actions,0,sizeof(actions));
    fail_read=fail_ack=fail_backlight=inject=corrupt_seq=bad_version=corrupt_crc=reset_on_text=false;
    in_callback=lock_on_key=defer_apply=false;
    applied_backlight=3; saved_lock_level=-1;
    assert(mix_keyboard_init(cb,NULL)==ESP_OK);
    assert(!mix_keyboard_online() && mix_keyboard_backlight_level()==-1);
}
static void setup(void) {
    init_driver();
    tick(0); assert(!mix_keyboard_online()); tick(20); assert(mix_keyboard_online());
    assert(mix_keyboard_backlight_level()==3);
}
static void test_screen_keys(void) {
    const uint8_t columns[]={1,2,3,7,8,9};
    const int expected[]={MIX_KEY_LOCK,MIX_KEY_VOLUME_DOWN,MIX_KEY_VOLUME_UP,MIX_KEY_BRIGHT_DOWN,MIX_KEY_BRIGHT_UP,MIX_KEY_BACKLIGHT};
    assert(MIX_KEY_HOME==MIX_KEY_LOCK);
    for (unsigned i=0;i<sizeof(columns);++i) {
        setup(); edge(0,columns[i],true); edge(0,columns[i],true); tick(40); tick(500);
        assert(!emitted && actions[expected[i]]==1);
        for (int a=1;a<7;++a) assert(actions[a]==(unsigned)(a==expected[i]));
        edge(0,columns[i],false); edge(0,columns[i],false); tick(520);
        tap(0,columns[i]); tick(540); assert(actions[expected[i]]==2);
    }
    setup(); edge(3,0,true); tap(0,9); tick(40); tick(500);
    edge(3,0,false); edge(5,0,true); tap(0,9); tick(520); tick(1000);
    assert(!actions[MIX_KEY_BACKLIGHT] && !backlight_writes && !emitted);
    setup(); edge(3,0,true);
    for (unsigned c=3;c<=7;++c) edge(5,c,true);
    tick(40); tick(500); assert(actions[MIX_KEY_BACKLIGHT]==1 && backlight_writes==1 && !emitted);
    for (unsigned c=3;c<=7;++c) edge(5,c,false);
    tick(520); tap(5,7); tick(540); assert(actions[MIX_KEY_BACKLIGHT]==2 && !emitted);
}
static void test_backlight_queue(void) {
    // Offline absolute sets queue, offline steps do not, and no API does I2C.
    init_driver(); mix_keyboard_backlight_set(7); mix_keyboard_backlight_step();
    assert(!transactions && mix_keyboard_backlight_level()==-1);
    tick(0); assert(!mix_keyboard_online() && !backlight_writes);
    tick(20); assert(stm.backlight==7 && backlight_writes==1 && mix_keyboard_backlight_level()==3);
    tick(40); assert(mix_keyboard_backlight_level()==7);
    tick(60); assert(backlight_writes==1);
    for (uint8_t level=0;level<=8;++level) {
        setup(); unsigned before=transactions; mix_keyboard_backlight_set(level);
        assert(transactions==before && mix_keyboard_backlight_level()==3);
        tick(40); assert(stm.backlight==level && backlight_writes==1 && mix_keyboard_backlight_level()==3);
        tick(60); assert(mix_keyboard_backlight_level()==level);
        tick(80); assert(backlight_writes==1);
    }
    setup(); mix_keyboard_backlight_set(9); mix_keyboard_backlight_set(255);
    tick(40); assert(!backlight_writes && mix_keyboard_backlight_level()==3);
    mix_keyboard_backlight_set(6); mix_keyboard_backlight_set(9); mix_keyboard_backlight_step();
    tick(60); assert(stm.backlight==7 && backlight_writes==1);
    tick(80); assert(mix_keyboard_backlight_level()==7);
    setup(); unsigned before=transactions;
    for (unsigned i=0;i<9;++i) mix_keyboard_backlight_step();
    assert(transactions==before); tick(40); assert(!backlight_writes);
    for (unsigned i=0;i<20;++i) mix_keyboard_backlight_step();
    tick(60); assert(stm.backlight==5 && backlight_writes==1);
    tick(80); assert(mix_keyboard_backlight_level()==5);
    // Steps use the next report, rather than a possibly stale cached level.
    setup(); stm.backlight=8; mix_keyboard_backlight_step();
    tick(40); assert(stm.backlight==0 && mix_keyboard_backlight_level()==8);
    tick(60); assert(mix_keyboard_backlight_level()==0);
    mix_keyboard_backlight_step(); tick(80); assert(stm.backlight==1);
    // A lock set cancels every older queued step; none can replay later.
    setup(); mix_keyboard_backlight_step(); mix_keyboard_backlight_step(); mix_keyboard_backlight_set(0);
    tick(40); assert(stm.backlight==0 && backlight_writes==1);
    tick(60); tick(80); assert(mix_keyboard_backlight_level()==0 && backlight_writes==1);
    // Intentionally newer steps add to the pending set, with modulo wrap.
    setup(); mix_keyboard_backlight_set(8); mix_keyboard_backlight_step();
    tick(40); assert(stm.backlight==0 && backlight_writes==1);
    setup(); mix_keyboard_backlight_set(2); mix_keyboard_backlight_step();
    mix_keyboard_backlight_set(6); mix_keyboard_backlight_step();
    tick(40); assert(stm.backlight==7 && backlight_writes==1);
    setup(); mix_keyboard_backlight_set(0); mix_keyboard_backlight_set(6);
    tick(40); assert(stm.backlight==6 && backlight_writes==1);
    // Resetting input must not throw away the lock's queued absolute command.
    setup(); mix_keyboard_backlight_set(0); mix_keyboard_reset_input();
    tick(40); assert(stm.backlight==0); tick(60); assert(mix_keyboard_backlight_level()==0);
    // Driver reinitialisation deliberately clears all pending work.
    setup(); mix_keyboard_backlight_set(0); mix_keyboard_backlight_step();
    assert(mix_keyboard_init(cb,NULL)==ESP_OK && mix_keyboard_backlight_level()==-1);
    tick(40); tick(60); assert(stm.backlight==3 && !backlight_writes);
    // The STM32 protocol exposes requested, NOT hardware-applied, brightness.
    setup(); defer_apply=true; mix_keyboard_backlight_set(8);
    tick(40); assert(stm.backlight==8 && stm.backlight_pending && applied_backlight==3);
    assert(mix_keyboard_backlight_level()==3); // no optimistic update on write
    tick(60); assert(mix_keyboard_backlight_level()==8 && applied_backlight==3);
    apply_pending(); assert(applied_backlight==8);
}
static void test_lock_callback(void) {
    setup(); lock_on_key=true;
    mix_keyboard_backlight_step(); tap(0,9); edge(0,1,true); tap(0,9); tap(2,1);
    tick(40);
    assert(actions[MIX_KEY_LOCK]==1 && actions[MIX_KEY_BACKLIGHT]==1 && !emitted);
    assert(saved_lock_level==3 && stm.backlight==0 && backlight_writes==1 && transactions==3);
    assert(mix_keyboard_backlight_level()==3);
    tick(60); assert(mix_keyboard_backlight_level()==0 && backlight_writes==1);
    edge(2,1,true); tick(80); assert(!emitted); // still waiting for LOCK release
    edge(0,1,false); edge(2,1,false); tick(100); assert(!emitted);
    tap(3,1); tick(120); assert(emitted==1 && output[0]=='a');
    mix_keyboard_backlight_set((uint8_t)saved_lock_level);
    tick(140); assert(stm.backlight==3 && mix_keyboard_backlight_level()==0);
    tick(160); assert(mix_keyboard_backlight_level()==3 && backlight_writes==2);
}
static void test_backlight_failures(void) {
    // Absolute intent survives read errors; old steps do not survive reconnect.
    setup(); mix_keyboard_backlight_set(0); mix_keyboard_backlight_step(); fail_read=true;
    tick(40); assert(!mix_keyboard_online() && !backlight_writes);
    fail_read=false; mix_keyboard_backlight_step();
    mix_keyboard_backlight_set(6); mix_keyboard_backlight_set(0);
    unsigned before=reads; tick(1039); assert(reads==before);
    tick(1040); tick(1060); assert(stm.backlight==0 && backlight_writes==1 && mix_keyboard_backlight_level()==3);
    tick(1080); assert(mix_keyboard_backlight_level()==0);
    // A failed input ACK does not lose a pending lock set or emit input.
    setup(); mix_keyboard_backlight_set(0); mix_keyboard_backlight_step(); edge(2,1,true); fail_ack=true;
    tick(40); assert(!mix_keyboard_online() && !emitted && !backlight_writes);
    fail_ack=false; tick(1040); tick(1060);
    assert(stm.backlight==0 && backlight_writes==1 && !emitted);
    tick(1080); assert(mix_keyboard_backlight_level()==0 && !emitted);
    // A failed set write is retried only after a new session, without its steps.
    setup(); mix_keyboard_backlight_set(0); mix_keyboard_backlight_step(); fail_backlight=true;
    tick(40); assert(!mix_keyboard_online() && stm.backlight==3 && backlight_writes==1);
    fail_backlight=false; tick(1040); tick(1060);
    assert(stm.backlight==0 && backlight_writes==2 && mix_keyboard_backlight_level()==3);
    tick(1080); assert(mix_keyboard_backlight_level()==0);
    // Failed step-only writes are not replayed after reconnect.
    setup(); mix_keyboard_backlight_step(); fail_backlight=true;
    tick(40); assert(!mix_keyboard_online() && backlight_writes==1);
    fail_backlight=false; tick(1040); tick(1060);
    assert(stm.backlight==3 && backlight_writes==1 && mix_keyboard_backlight_level()==3);
    // MCU reset also invalidates feedback and steps, but preserves a queued set.
    setup(); mix_keyboard_backlight_set(0); mix_keyboard_backlight_step(); mix_kbd_transport_init(&stm);
    tick(40); assert(!mix_keyboard_online() && !backlight_writes);
    tick(60); assert(stm.backlight==0 && backlight_writes==1 && mix_keyboard_backlight_level()==3);
    tick(80); assert(mix_keyboard_backlight_level()==0);
    setup(); mix_keyboard_backlight_step(); mix_kbd_transport_init(&stm);
    tick(40); tick(60); assert(stm.backlight==3 && !backlight_writes);
    // A CRC-valid out-of-range report is rejected, never exposed or used.
    setup(); mix_keyboard_backlight_set(0); mix_keyboard_backlight_step(); stm.backlight=9;
    tick(40); assert(!mix_keyboard_online() && !backlight_writes);
    mix_kbd_transport_init(&stm); tick(1040); tick(1060);
    assert(stm.backlight==0 && backlight_writes==1 && mix_keyboard_backlight_level()==3);
    tick(1080); assert(mix_keyboard_backlight_level()==0);
}
int main(void) {
    setup(); edge(2,1,true); edge(2,1,false); tick(40); assert(emitted==1 && output[0]=='q' && !stm.count);
    unsigned read_count=reads; tick(41); assert(reads==read_count);
    edge(0,9,true); edge(0,9,false); tick(60); assert(stm.backlight==4 && transactions==3);
    setup(); edge(4,0,true); edge(2,1,true); tick(40); assert(emitted==1 && output[0]=='Q');
    mix_keyboard_reset_input(); edge(3,1,true); edge(3,1,false); tick(60); assert(emitted==1);
    edge(4,0,false); edge(2,1,false); tick(80); assert(emitted==1);
    edge(3,1,true); edge(3,1,false); tick(100); assert(emitted==2 && output[1]=='a');
    setup(); edge(2,1,true); fail_ack=true; tick(40); assert(!emitted && !mix_keyboard_online() && stm.count==1);
    fail_ack=false; read_count=reads; tick(60); tick(1039); assert(reads==read_count);
    tick(1040); tick(1060); assert(mix_keyboard_online() && !emitted);
    edge(2,1,false); tick(1080); edge(3,1,true); edge(3,1,false); tick(1100); assert(emitted==1 && output[0]=='a');
    setup(); fail_read=true; tick(40); for(unsigned t=60;t<1040;t+=20) tick(t); assert(reads==3);
    fail_read=false; tick(1040); tick(1060); assert(mix_keyboard_online());
    setup(); edge(2,1,true); corrupt_seq=true; tick(40); assert(!emitted); corrupt_seq=false;
    edge(2,1,false); tick(60); edge(3,1,true); edge(3,1,false); tick(80); assert(emitted==1);
    setup(); for(unsigned i=0;i<140;++i) edge(2,1,!(i&1));
    tick(40); assert(mix_keyboard_overflows()==12 && stm.count==96 && !emitted);
    tick(60); tick(80); tick(100); assert(!stm.count && !emitted);
    edge(3,1,true); edge(3,1,false); tick(120); assert(emitted==1);
    setup(); edge(2,1,true); tick(40); assert(emitted==1);
    mix_kbd_transport_init(&stm); tick(60); assert(!mix_keyboard_online());
    edge(4,0,true); tick(80); edge(3,1,true); edge(3,1,false); tick(100); assert(emitted==1);
    edge(4,0,false); tick(120); edge(3,1,true); edge(3,1,false); tick(140); assert(emitted==2 && output[1]=='a');
    setup(); mix_keyboard_reset_input(); inject=true; tick(40); assert(stm.count==1 && !emitted);
    tick(60); assert(emitted==1 && output[0]=='q'); // edge after all-up snapshot retained
    setup(); stm.next_seq=65535; mix_keyboard_reset_input(); tick(40); edge(2,1,true); edge(2,1,false); tick(60); assert(emitted==1);
    setup(); reset_on_text=true; edge(2,1,true); edge(2,1,false); edge(3,1,true); edge(3,1,false); tick(40); assert(emitted==1);
    reset_on_text=false; tick(60); edge(3,1,true); edge(3,1,false); tick(80); assert(emitted==2);
    setup(); bad_version=true; tick(40); assert(!mix_keyboard_online() && !emitted);
    setup(); edge(2,1,true); corrupt_crc=true; tick(40); assert(!mix_keyboard_online() && !emitted && stm.count==1);
    test_screen_keys();
    test_backlight_queue();
    test_lock_callback();
    test_backlight_failures();
    puts("driver: lock layout, callback-safe backlight queue/feedback, offline/reconnect, rescue, ACK/release/repeat protection passed");
    return 0;
}
