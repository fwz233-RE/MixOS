/* Host-only fault injection: actual gt911.c plus the actual AW reset body. */
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "gt911.h"
#include "aw9523.h"
#include "mix_i2c.h"
#include "freertos/task.h"

static int bus_depth, aw_depth, status_reads, point_reads, acks;
static uint8_t status;
static int px=500, py=600;
static esp_err_t status_error, point_error, ack_error;
static bool init_mode, blank_config, primary_fails, config_sent, soft_reset;
static int operations, fail_at, removes, adds;
static uint8_t address;
const char *esp_err_to_name(esp_err_t e) { (void)e; return "injected"; }
void vTaskDelay(unsigned ticks) { (void)ticks; }
static bool bus_busy;
bool mix_i2c_try_lock(void) { if(bus_busy)return false; ++bus_depth; return true; }
void mix_i2c_lock(void) { ++bus_depth; }
void mix_i2c_unlock(void) { assert(bus_depth>0); --bus_depth; }
i2c_master_dev_handle_t mix_i2c_add_device(uint8_t addr, uint32_t hz) {
    assert(hz==I2C_STANDARD_HZ); address=addr; ++adds; return (void *)1;
}
esp_err_t mix_i2c_rm_device(i2c_master_dev_handle_t dev) {
    assert(dev==(void *)1); ++removes; return ESP_OK;
}
esp_err_t mix_i2c_transmit(i2c_master_dev_handle_t dev, const uint8_t *data, size_t len, int timeout) {
    if(!init_mode) {
        assert(dev==(void *)1 && bus_depth==1 && len==3 && timeout==4);
        assert(data[0]==0x81 && data[1]==0x4e && !data[2]);
        ++acks; return ack_error;
    }
    assert(dev==(void *)1 && data && len==188 && timeout==I2C_BULK_TIMEOUT_MS);
    if(++operations==fail_at)return ESP_FAIL;
    config_sent=true;return ESP_OK;
}
esp_err_t mix_i2c_read_wide(i2c_master_dev_handle_t dev, uint16_t reg, uint8_t *out, size_t len, int timeout) {
    assert(dev==(void *)1 && out && (timeout==I2C_XFER_TIMEOUT_MS || (!init_mode && timeout==4)));
    memset(out,0,len);
    if(init_mode) {
        if(++operations==fail_at)return ESP_FAIL;
        if(primary_fails && address==GT911_I2C_ADDR_PRIMARY)return ESP_FAIL;
        if(reg==0x8140) {
            assert(len==4 || len==11); memcpy(out,"911",3);
            if(len==11 && (!blank_config || soft_reset)) {out[7]=4;out[9]=3;}
        } else {
            assert(reg==0x8047 && len==186);
            if(!blank_config)out[0]=0x41;
        }
        return ESP_OK;
    }
    assert(bus_depth==1);
    if(reg==0x814e) {
        assert(len==1);++status_reads;*out=status;return status_error;
    }
    assert(reg==0x814f && len==8);++point_reads;
    out[1]=(uint8_t)px;out[2]=(uint8_t)(px>>8);out[3]=(uint8_t)py;out[4]=(uint8_t)(py>>8);
    return point_error;
}
esp_err_t mix_i2c_write_wide_u8(i2c_master_dev_handle_t dev, uint16_t reg, uint8_t value) {
    assert(dev==(void *)1);
    if(init_mode) {
        assert(reg==0x8040 && (value==0 || value==2));
        if(++operations==fail_at)return ESP_FAIL;
        soft_reset=true;return ESP_OK;
    }
    assert(bus_depth==1 && reg==0x814e && !value);++acks;return ack_error;
}

/* Preserve real reset's locking, ordering and cleanup rather than model it. */
static uint8_t s_expected_out1=AW9523_P1_LCD_RST|AW9523_P1_TP_RST;
static uint8_t s_expected_cfg1=0xed;
static int reset_calls, reset_fail, delays;
static uint8_t reset_reg[8], reset_mask[8], reset_value[8];
static void aw_lock(void) { ++aw_depth; }
static void aw_unlock(void) { assert(aw_depth==1); --aw_depth; }
esp_err_t aw9523_update_bits(i2c_master_dev_handle_t dev, uint8_t reg, uint8_t mask, uint8_t value) {
    assert(dev==(void *)2 && aw_depth==1 && reset_calls<8);
    reset_reg[reset_calls]=reg;reset_mask[reset_calls]=mask;reset_value[reset_calls++]=value;
    return reset_calls==reset_fail?ESP_FAIL:ESP_OK;
}
#include "aw_touch_reset.h"

static void clear(void) {
    bus_depth=aw_depth=status_reads=point_reads=acks=0;
    status=0;px=500;py=600;status_error=point_error=ack_error=ESP_OK;
    init_mode=blank_config=primary_fails=config_sent=soft_reset=false;
    operations=fail_at=removes=adds=0;reset_calls=reset_fail=delays=0;
}
static gt911_touch_t read_frame(esp_err_t expected) {
    gt911_touch_t t={7,999,888};
    assert(gt911_read((void *)1,&t)==expected && !bus_depth);
    if(expected!=ESP_OK)assert(t.count==0 && t.x==0 && t.y==0);
    return t;
}
static void idle(void) {
    read_frame(ESP_ERR_NOT_FOUND);assert(status_reads==1 && !point_reads && !acks);
    status=1;read_frame(ESP_ERR_NOT_FOUND);assert(!acks);
}
static void frames(void) {
    status=0x81;gt911_touch_t t=read_frame(ESP_OK);
    assert(t.count==1 && t.x==px && t.y==py && acks==1 && point_reads==1);
    status=0x80;t=read_frame(ESP_OK);assert(!t.count && !t.x && !t.y && acks==2 && point_reads==1);
    status=0x85;t=read_frame(ESP_OK);assert(t.count==5 && acks==3);
}
static void read_errors(void) {
    status_error=ESP_ERR_TIMEOUT;read_frame(ESP_ERR_TIMEOUT);assert(!acks && !point_reads);
    status_error=ESP_OK;status=0x81;point_error=ESP_FAIL;read_frame(ESP_FAIL);assert(acks==1);
    point_error=ESP_OK;ack_error=ESP_ERR_TIMEOUT;read_frame(ESP_ERR_TIMEOUT);assert(acks==2);
    status=0x80;read_frame(ESP_ERR_TIMEOUT);assert(acks==3);
}
static void invalid(void) {
    status=0x86;read_frame(ESP_ERR_INVALID_RESPONSE);assert(!point_reads && acks==1);
    status=0x81;px=1024;read_frame(ESP_ERR_INVALID_RESPONSE);
    px=0;py=768;read_frame(ESP_ERR_INVALID_RESPONSE);
    py=0;gt911_touch_t t=read_frame(ESP_OK);assert(t.count==1 && !t.x && !t.y);
    assert(gt911_read(NULL,&t)==ESP_ERR_INVALID_ARG);
    assert(gt911_read((void *)1,NULL)==ESP_ERR_INVALID_ARG);
    assert(gt911_init(NULL)==ESP_ERR_INVALID_ARG);
}
static void quick_read(void) {
    gt911_touch_t t={7,999,888};status=0x81;bus_busy=true;
    assert(gt911_try_read((void *)1,&t)==ESP_ERR_NOT_FOUND);
    assert(!t.count && !bus_depth && !status_reads && !acks);
    bus_busy=false;
    assert(gt911_try_read((void *)1,&t)==ESP_OK && t.count==1 && t.x==px && t.y==py);
    assert(!bus_depth && acks==1);
    status=0x80;ack_error=ESP_ERR_TIMEOUT;
    assert(gt911_try_read((void *)1,&t)==ESP_ERR_TIMEOUT && !t.count && !bus_depth);
    ack_error=ESP_OK;
    assert(gt911_try_read((void *)1,&t)==ESP_OK && !t.count && !bus_depth);
    status=0;
    assert(gt911_try_read((void *)1,&t)==ESP_ERR_NOT_FOUND && !bus_depth);
    status=0x81;point_error=ESP_ERR_TIMEOUT;
    assert(gt911_try_read((void *)1,&t)==ESP_ERR_TIMEOUT && !t.count && !bus_depth);
    status_error=ESP_ERR_TIMEOUT;int previous=acks;
    assert(gt911_try_read((void *)1,&t)==ESP_ERR_TIMEOUT && !bus_depth && acks==previous);
}
static void init(void) {
    init_mode=true;i2c_master_dev_handle_t dev=NULL;
    assert(gt911_init(&dev)==ESP_OK && dev && adds==1 && !removes);
    clear();init_mode=true;primary_fails=true;
    assert(gt911_init(&dev)==ESP_OK && dev && adds==2 && removes==1 && address==GT911_I2C_ADDR_ALT);
    clear();init_mode=blank_config=true;
    assert(gt911_init(&dev)==ESP_OK && dev && config_sent && soft_reset && !removes);
}
static void init_errors(void) {
    /* First probe failure can legitimately fall back to the alternate address.
     * Every later init I/O failure must release its handle, never claim ready. */
    for(int nth=2;nth<=8;nth++) {
        clear();init_mode=blank_config=true;fail_at=nth;i2c_master_dev_handle_t dev=(void *)99;
        assert(gt911_init(&dev)==ESP_FAIL && !dev && removes==1);
    }
}
static void reset(void) {
    for(int nth=0;nth<=4;nth++) {
        clear();reset_fail=nth;
        assert(aw9523_gt911_reset((void *)2)==(nth?ESP_FAIL:ESP_OK));
        assert(!aw_depth && reset_calls>=3);
        int end=reset_calls-1;
        assert(reset_reg[end]==AW9523_REG_CONFIG_P1 && reset_mask[end]==AW9523_P1_TP_INT && reset_value[end]==AW9523_P1_TP_INT);
        assert(reset_reg[end-1]==AW9523_REG_OUTPUT_P1 && reset_mask[end-1]==AW9523_P1_TP_RST && reset_value[end-1]==AW9523_P1_TP_RST);
        assert((s_expected_out1&AW9523_P1_TP_RST) && (s_expected_cfg1&AW9523_P1_TP_INT));
    }
}
int main(int argc,char **argv) {
    assert(argc==2);clear();
    struct {const char *name;void (*run)(void);} cases[]={
        {"quick",quick_read},{"idle",idle},{"frames",frames},{"read_errors",read_errors},{"invalid",invalid},
        {"init",init},{"init_errors",init_errors},{"reset",reset}};
    for(unsigned i=0;i<sizeof(cases)/sizeof(cases[0]);i++)if(!strcmp(argv[1],cases[i].name)) {
        cases[i].run();printf("PASS %s\n",argv[1]);return 0;
    }
    return 2;
}
