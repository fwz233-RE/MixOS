#include "mix_protocol.h"
#include "mix_terminal.h"
#include "mix_power_math.h"
#include <assert.h>
#include <stdio.h>
#include <string.h>
#include <math.h>
static void feed(const char *s){mix_terminal_feed((const uint8_t*)s,strlen(s));}
int main(void){
    assert(mix_crc32((const uint8_t*)"123456789",9)==0xcbf43926);
    uint8_t wire[MIX_MAX_WIRE];mix_frame_t f={.channel=1,.type=MIX_DATA,.epoch=42,.session=9,.sequence=1,.length=512},g;
    for(int i=0;i<512;i++)f.payload[i]=(uint8_t)i;
    size_t n=mix_frame_encode(&f,wire,sizeof(wire));assert(n>0&&n<=sizeof(wire));
    assert(mix_frame_decode(wire,n-1,&g)&&g.length==512&&!memcmp(g.payload,f.payload,512));
    mix_decoder_t d={0};for(size_t i=0;i<n;i++)assert(mix_decoder_push(&d,wire[i],&g)==(i==n-1));
    wire[3]^=1;assert(!mix_frame_decode(wire,n-1,&g));wire[3]^=1;
    for(int i=0;i<1000;i++)assert(!mix_decoder_push(&d,1,&g));assert(!mix_decoder_push(&d,0,&g));
    for(size_t i=0;i<n;i++)mix_decoder_push(&d,wire[i],&g);assert(g.epoch==42);
    mix_terminal_init();feed("abc\rZ");assert(mix_terminal_row(0)[0].codepoint=='Z');assert(mix_terminal_row(0)[1].codepoint=='b');
    feed("\033[2J\033[H\033[31mred");assert(mix_terminal_row(0)[0].fg==1);assert(mix_terminal_cursor_x()==3);
    mix_terminal_init();uint8_t zh[]={0xe4,0xb8,0xad};for(int i=0;i<3;i++)mix_terminal_feed(&zh[i],1);
    assert(mix_terminal_row(0)[0].codepoint==0x4e2d&&mix_terminal_row(0)[0].width==2&&mix_terminal_row(0)[1].width==0);
    feed("\r\033[K");assert(mix_terminal_row(0)[0].codepoint==' '&&mix_terminal_row(0)[1].width==1);
    feed("\033]title REBOOT_TO_BOOT_MODE\007OK");assert(mix_terminal_row(0)[0].codepoint=='O');
    mix_terminal_init();for(int i=0;i<150;i++)feed("line\r\n");mix_terminal_scroll(9999);assert(mix_terminal_scroll_offset()==100);
    for(int r=0;r<28;r++)assert(mix_terminal_row(r));mix_terminal_scroll(-9999);assert(!mix_terminal_scroll_offset());
    mix_terminal_init();for(int i=0;i<80;i++)feed("x");assert(mix_terminal_cursor_y()==0);feed("y");assert(mix_terminal_cursor_y()==1);
    assert(fabsf(mix_integrate_mah(1000,1000,5000000)-1.3888889f)<0.0001f);
    assert(mix_integrate_mah(1000,1000,61000000)==0);
    puts("MixOS protocol, terminal and power math tests passed");return 0;
}
