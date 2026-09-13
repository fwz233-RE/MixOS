#include "mix_kbd_transport.h"
#include <assert.h>
#include <stdio.h>
#include <string.h>
static uint16_t u16(const uint8_t *p) { return (uint16_t)(p[0] | (p[1]<<8)); }
int main(void) {
    mix_kbd_transport_t s;
    uint8_t f[KBD_V1_FRAME], again[KBD_V1_FRAME];
    assert(mix_kbd_crc16((const uint8_t *)"123456789",9)==0x29b1);
    mix_kbd_transport_init(&s);
    mix_kbd_transport_event(&s,0,0,true); mix_kbd_transport_event(&s,6,0,true); assert(!s.count);
    mix_kbd_transport_event(&s,2,1,true); mix_kbd_transport_event(&s,2,1,true); assert(s.count==1 && s.next_seq==1);
    mix_kbd_transport_frame(&s,f); mix_kbd_transport_frame(&s,again); assert(!memcmp(f,again,sizeof(f)));
    assert(f[0]==0x6b && f[1]==1 && f[3]==1 && f[28]==0 && f[30]==0xa1 && u16(f+14)==2);
    mix_kbd_transport_event(&s,2,1,false); assert(u16(f+14)==2); // previous snapshot immutable
    uint8_t ack[]={0x11,0,0}; mix_kbd_transport_write(&s,ack,3); assert(s.count==1); mix_kbd_transport_write(&s,ack,3); assert(s.count==1);
    ack[1]=1; mix_kbd_transport_write(&s,ack,3); assert(!s.count);
    for(unsigned i=0;i<140;++i) mix_kbd_transport_event(&s,3,1,!(i&1));
    assert(s.count==128 && s.overflow==12 && s.next_seq==142 && !s.rows[3]);
    mix_kbd_transport_frame(&s,f); assert(f[2]==1 && f[3]==128 && u16(f+4)==142);
    ack[1]=f[28+31*3]; ack[2]=f[29+31*3]; mix_kbd_transport_write(&s,ack,3); assert(s.count==96);
    uint8_t cmd[]={0x20,8}; mix_kbd_transport_write(&s,cmd,2); assert(s.backlight_pending && s.pending_backlight==8);
    cmd[1]=9; mix_kbd_transport_write(&s,cmd,2); assert(s.pending_backlight==8);
    uint8_t session[]={0x13,0x78,0x56,0x34,0x12}; mix_kbd_transport_write(&s,session,5); assert(s.session==0x12345678);
    mix_kbd_transport_init(&s); s.next_seq=65535;
    mix_kbd_transport_event(&s,2,1,true); mix_kbd_transport_event(&s,2,1,false);
    mix_kbd_transport_frame(&s,f); assert(u16(f+28)==65535 && u16(f+31)==0 && u16(f+4)==1);
    ack[1]=0; ack[2]=0; mix_kbd_transport_write(&s,ack,3); assert(!s.count);
    printf("transport: FIFO 128, overflow, ACK retry, wrap, atomic snapshot, commands passed; state=%zu bytes\n",sizeof(s));
    return 0;
}
