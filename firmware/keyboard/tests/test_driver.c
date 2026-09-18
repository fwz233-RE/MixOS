#include "mix_keyboard.h"
#include "mix_kbd_transport.h"
#include "mix_i2c.h"
#include <assert.h>
#include <stdio.h>
#include <string.h>
static mix_kbd_transport_t stm;
static unsigned transactions, reads, emitted;
static uint8_t output[4096];
static bool fail_read, fail_ack, inject, corrupt_seq, bad_version, corrupt_crc, reset_on_text;
/* The driver reaches the bus through mix_i2c, which owns the bus handle and
 * serialises every transfer, so the fakes live at that boundary now. They keep
 * the same assertions the previous IDF-level fakes made: the slave address,
 * the 400 kHz rate, the single-register read of a whole frame, and the 5 ms
 * deadline are all still checked here. */
i2c_master_dev_handle_t mix_i2c_add_device(uint8_t address, uint32_t scl_hz) {
    assert(address==0x1f && scl_hz==400000); return (void *)2;
}
esp_err_t mix_i2c_rm_device(i2c_master_dev_handle_t d) { assert(d); return ESP_OK; }
esp_err_t mix_i2c_transmit(i2c_master_dev_handle_t d,const uint8_t *p,size_t n,int timeout) {
    assert(d && timeout==5); ++transactions;
    if (fail_ack && p[0]==0x11) return ESP_FAIL;
    mix_kbd_transport_write(&stm,p,n);
    if(stm.backlight_pending) { stm.backlight=stm.pending_backlight; stm.backlight_pending=false; }
    return ESP_OK;
}
esp_err_t mix_i2c_read(i2c_master_dev_handle_t d,uint8_t reg,uint8_t *out,size_t len,int timeout) {
    assert(d && reg==0 && len==KBD_V1_FRAME && timeout==5); ++transactions; ++reads;
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
    if (a==MIX_KEY_BACKLIGHT) mix_keyboard_backlight_step();
    if (a!=MIX_KEY_TEXT) return;
    assert(emitted+n<=sizeof(output)); memcpy(output+emitted,p,n); emitted+=(unsigned)n;
    if (reset_on_text) mix_keyboard_reset_input();
}
static void tick(uint32_t now) { transactions=0; mix_keyboard_tick(now); assert(transactions<=3); }
static void edge(unsigned r,unsigned c,bool down) { mix_kbd_transport_event(&stm,(uint8_t)r,(uint8_t)c,down); }
static void setup(void) {
    mix_kbd_transport_init(&stm); emitted=reads=0;
    fail_read=fail_ack=inject=corrupt_seq=bad_version=corrupt_crc=reset_on_text=false;
    assert(mix_keyboard_init(cb,NULL)==ESP_OK);
    tick(0); assert(!mix_keyboard_online()); tick(20); assert(mix_keyboard_online());
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
    puts("driver: bounded polling, offline throttle, ACK failure, overflow, reset, all-up race, session, sequence wrap passed");
    return 0;
}
