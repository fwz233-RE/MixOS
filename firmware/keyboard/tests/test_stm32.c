#include "hal.h"
#include "quantum.h"
#include "i2c_slave_kbd.h"
#include "mix_kbd_transport.h"
#include "mix_matrix.h"
#include <assert.h>
#include <stdio.h>
#include <string.h>
fake_syscfg_t fake_syscfg;
fake_i2c_t fake_i2c;
static uint32_t now, boots;
static uint8_t backlight=3;
static uint16_t raw[6];
void Vector9C(void);
void keyboard_post_init_kb(void);
void housekeeping_task_kb(void);
bool pre_process_record_kb(uint16_t,keyrecord_t *);
bool process_record_kb(uint16_t,keyrecord_t *);
uint32_t timer_read32(void) { return now; }
uint32_t timer_elapsed32(uint32_t start) { return now-start; }
matrix_row_t matrix_get_row(uint8_t r) { return raw[r]; }
void reset_keyboard(void) { ++boots; }
void backlight_level_noeeprom(uint8_t level) { backlight=level; }
uint8_t get_backlight_level(void) { return backlight; }
static void irq(uint32_t flags) { I2C1->ISR=flags; Vector9C(); }
static void byte(uint8_t b) { I2C1->RXDR=b; irq(I2C_ISR_RXNE); }
static void command(const uint8_t *p,unsigned n) { irq(I2C_ISR_ADDR); for(unsigned i=0;i<n;++i) byte(p[i]); irq(I2C_ISR_STOPF); }
static void start_read(void) { irq(I2C_ISR_ADDR); byte(0); irq(I2C_ISR_ADDR|I2C_ISR_DIR); }
static void frame(uint8_t *out,unsigned len) {
    start_read();
    for(unsigned i=0;i<len;++i) { irq(I2C_ISR_TXIS); out[i]=(uint8_t)I2C1->TXDR; }
    irq(I2C_ISR_NACKF|I2C_ISR_TXIS); irq(I2C_ISR_STOPF);
}
static void event(uint8_t r,uint8_t c,bool pressed) {
    keyrecord_t record={.event={.key={r,c},.pressed=pressed,.type=1}};
    assert(!pre_process_record_kb(0x7e00,&record));
    if (r<6 && c<11) { if(pressed) raw[r]|=(uint16_t)(1U<<c); else raw[r]&=(uint16_t)~(1U<<c); }
}
static void init(void) { memset(raw,0,sizeof(raw)); keyboard_post_init_kb(); housekeeping_task_kb(); }
int main(void) {
    uint8_t f[KBD_V1_FRAME], g[KBD_V1_FRAME];
    init(); event(0,0,true); event(0,4,true); frame(f,sizeof(f)); assert(f[3]==0);
    keyrecord_t tick={.event={.key={2,1},.pressed=true,.type=0}};
    assert(!pre_process_record_kb(0,&tick)); assert(!process_record_kb(0,&tick)); frame(f,sizeof(f)); assert(!f[3]);
    for(unsigned c=3;c<=7;++c) event(5,(uint8_t)c,true);
    frame(f,sizeof(f)); frame(g,sizeof(g)); assert(!memcmp(f,g,sizeof(f)) && f[3]==5);
    frame(g,1); frame(g,sizeof(g)); assert(g[3]==5); // aborted/short reads never pop
    uint8_t ack[]={0x11,4,0}; command(ack,3); frame(g,sizeof(g)); assert(!g[3]);
    start_read(); event(2,1,true); // latched BEFORE accepted edge
    for(unsigned i=0;i<sizeof(f);++i) { irq(I2C_ISR_TXIS); f[i]=(uint8_t)I2C1->TXDR; }
    irq(I2C_ISR_NACKF); irq(I2C_ISR_STOPF); assert(!f[3]); frame(g,sizeof(g)); assert(g[3]==1);
    uint8_t bl[]={0x20,8}; command(bl,2); assert(backlight==3); housekeeping_task_kb(); assert(backlight==8);
    uint8_t too_long[]={0x20,2,0,0,0,0,0}; command(too_long,sizeof(too_long)); housekeeping_task_kb(); assert(backlight==8);
    // A write followed by a read is not a committed command, even if invalid.
    irq(I2C_ISR_ADDR); byte(0x20); byte(1); irq(I2C_ISR_ADDR|I2C_ISR_DIR); irq(I2C_ISR_NACKF); irq(I2C_ISR_STOPF); housekeeping_task_kb(); assert(backlight==8);
    init(); now=100; event(3,0,true); event(0,9,true); housekeeping_task_kb(); now=3099; housekeeping_task_kb(); assert(!boots); now=3100; housekeeping_task_kb(); assert(boots==1);
    init(); now=4000; event(5,0,true); event(0,9,true); housekeeping_task_kb(); now=6999; housekeeping_task_kb(); assert(boots==1); now=7000; housekeeping_task_kb(); assert(boots==2);
    init(); now=8000; event(3,0,true); event(0,9,true); housekeeping_task_kb(); raw[0]=0; now=11001; housekeeping_task_kb(); assert(boots==2); // ghost-suppressed release cannot rescue
    init(); now=12000; event(3,0,true); event(0,9,true); event(2,1,true); housekeeping_task_kb(); now=16000; housekeeping_task_kb(); assert(boots==2);
    init(); now=UINT32_MAX-1000; event(5,0,true); event(0,9,true); housekeeping_task_kb(); now=1999; housekeeping_task_kb(); assert(boots==3);
    puts("STM32 host stubs: prehook, blank/tick rejection, five spaces, IRQ read/ACK/abort, backlight, local rescue passed (not hardware)");
    return 0;
}
