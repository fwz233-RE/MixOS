/* Actual reboot executor + audited TinyUSB disconnect implementations. */
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdatomic.h>
#include "mix_restart.h"
#include "freertos/FreeRTOS.h"

/* Register doubles exercise source control flow, not USB electrical timing. */
typedef struct { unsigned pad_pull_override,dp_pullup,dp_pulldown,dm_pullup,dm_pulldown; } usb_wrap_otg_conf_reg_t;
static struct { usb_wrap_otg_conf_reg_t otg_conf; } USB_WRAP;
typedef struct { unsigned dctl; } dwc2_regs_t;
static dwc2_regs_t regs;
static unsigned register_accesses,delays,restarts,elapsed_min;
static bool expect_detach;
#define TUP_USBIP_DWC2_ESP32 1
#define TU_CHECK_MCU(...) 0
#define DCTL_SDIS 2u
static dwc2_regs_t *get_regs(unsigned port){assert(port==0);register_accesses++;return &regs;}
#define DWC2_REG(port) get_regs(port)
static const uint8_t _usbd_rhport=0;
#include "usb_restart_functions.h"

void vTaskDelay(unsigned ticks)
{
    assert(!restarts);
    assert(register_accesses==(expect_detach?1u:0u));
    if(expect_detach){
        assert(USB_WRAP.otg_conf.pad_pull_override==1);
        assert(!USB_WRAP.otg_conf.dp_pullup&&!USB_WRAP.otg_conf.dm_pullup);
        assert(USB_WRAP.otg_conf.dp_pulldown&&USB_WRAP.otg_conf.dm_pulldown);
        assert(regs.dctl&DCTL_SDIS);
    }
    assert(++delays==1);
    elapsed_min=(ticks-1)*portTICK_PERIOD_MS; /* call just before next tick */
    assert(elapsed_min>=300&&elapsed_min<500);
    mix_restart(); /* A repeated/reentrant request cannot extend the wait. */
    assert(delays==1&&!restarts);
}
void esp_restart(void){assert(delays==1&&elapsed_min>=300);restarts++;}
#include "mix_restart.c"

static void reset_case(bool initialized)
{
    atomic_store(&s_tinyusb_initialized,initialized);
    atomic_store(&s_restart_detached,false);
    atomic_store(&restart_claimed,false);
    USB_WRAP.otg_conf=(usb_wrap_otg_conf_reg_t){.dp_pullup=1};
    regs.dctl=0;register_accesses=delays=restarts=elapsed_min=0;
    expect_detach=initialized;
}
int main(void)
{
    reset_case(true);
    mix_restart();assert(restarts==1&&register_accesses==1&&delays==1);
    for(unsigned i=0;i<100;i++)mix_restart();
    assert(restarts==1&&register_accesses==1&&delays==1);
    assert(uac_device_disconnect_for_restart());assert(register_accesses==1);
    /* Simulate a concurrent DCTL RMW losing SDIS: physical pad override is
     * independent and remains asserted (the audited S3 disconnect mechanism). */
    regs.dctl=0;assert(USB_WRAP.otg_conf.pad_pull_override==1);
    assert(USB_WRAP.otg_conf.dp_pulldown&&!USB_WRAP.otg_conf.dp_pullup);
    reset_case(false);
    assert(!uac_device_disconnect_for_restart());assert(!register_accesses);
    mix_restart();mix_restart();assert(restarts==1&&delays==1&&!register_accesses);
    puts("bounded USB detach before one-shot restart passed");return 0;
}
