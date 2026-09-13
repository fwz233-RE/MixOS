/* SPDX-License-Identifier: MIT */
#include "mix_protocol.h"
#include <string.h>
uint16_t mix_get16(const uint8_t *p) { return (uint16_t)(p[0]|((uint16_t)p[1]<<8)); }
uint32_t mix_get32(const uint8_t *p) { return (uint32_t)p[0]|((uint32_t)p[1]<<8)|((uint32_t)p[2]<<16)|((uint32_t)p[3]<<24); }
void mix_put16(uint8_t *p,uint16_t v) { p[0]=(uint8_t)v;p[1]=(uint8_t)(v>>8); }
void mix_put32(uint8_t *p,uint32_t v) { for(int i=0;i<4;i++)p[i]=(uint8_t)(v>>(8*i)); }
uint32_t mix_crc32(const uint8_t *p,size_t n) {
    uint32_t c=~0u;
    for(size_t i=0;i<n;i++){c^=p[i];for(int k=0;k<8;k++)c=(c>>1)^(0xedb88320u & (0u-(c&1)));}
    return ~c;
}
size_t mix_frame_encode(const mix_frame_t *f,uint8_t *wire,size_t cap) {
    if(!f || !wire || f->length>MIX_MAX_PAYLOAD || cap<MIX_MAX_WIRE)return 0;
    uint8_t raw[MIX_HEADER_SIZE+MIX_MAX_PAYLOAD+4];
    raw[0]=1;raw[1]=f->channel;raw[2]=f->type;raw[3]=0;
    mix_put32(raw+4,f->epoch);mix_put32(raw+8,f->session);mix_put32(raw+12,f->sequence);
    mix_put16(raw+16,f->length);memcpy(raw+18,f->payload,f->length);
    size_t n=18+f->length;mix_put32(raw+n,mix_crc32(raw,n));n+=4;
    size_t code_at=0,w=1;uint8_t code=1;
    for(size_t r=0;r<n;r++) {
        if(!raw[r]){wire[code_at]=code;code_at=w++;code=1;}
        else {wire[w++]=raw[r];if(++code==255){wire[code_at]=code;code_at=w++;code=1;}}
    }
    wire[code_at]=code;wire[w++]=0;return w;
}
bool mix_frame_decode(const uint8_t *wire,size_t n,mix_frame_t *f) {
    uint8_t raw[MIX_HEADER_SIZE+MIX_MAX_PAYLOAD+4];size_t r=0,w=0;
    if(!wire||!f||!n||n>=MIX_MAX_WIRE)return false;
    while(r<n) {
        uint8_t c=wire[r++];if(!c || (size_t)(c-1)>n-r)return false;
        for(unsigned i=1;i<c;i++){if(w>=sizeof(raw)||!wire[r])return false;raw[w++]=wire[r++];}
        if(c!=255&&r<n){if(w>=sizeof(raw))return false;raw[w++]=0;}
    }
    if(w<22||raw[0]!=1||raw[1]>MIX_CH_LOG||raw[3]!=0)return false;
    uint16_t len=mix_get16(raw+16);
    if(len>MIX_MAX_PAYLOAD||w!=(size_t)len+22||mix_get32(raw+w-4)!=mix_crc32(raw,w-4))return false;
    f->channel=raw[1];f->type=raw[2];f->epoch=mix_get32(raw+4);f->session=mix_get32(raw+8);
    f->sequence=mix_get32(raw+12);f->length=len;memcpy(f->payload,raw+18,len);return true;
}
bool mix_decoder_push(mix_decoder_t *d,uint8_t b,mix_frame_t *out) {
    if(!b){bool ok=!d->discard&&d->used&&mix_frame_decode(d->bytes,d->used,out);
        if(!ok&&(d->used||d->discard))d->errors++;
        d->used=0;d->discard=false;return ok;}
    if(d->discard)return false;
    if(d->used>=sizeof(d->bytes)){d->discard=true;d->used=0;return false;}
    d->bytes[d->used++]=b;return false;
}
