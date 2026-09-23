/* MixOS — local interaction on ESP32, Linux is a headless USB peer.
 * Board wiring and codec implementation inherited from TypixDeck (MIT).
 */
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <math.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "driver/gpio.h"
#include "driver/ledc.h"
#include "driver/i2c_master.h"
#include "esp_lcd_panel_ops.h"
#include "esp_lcd_panel_rgb.h"
#include "esp_timer.h"
#include "esp_heap_caps.h"
#include "esp_log.h"
#include "esp_system.h"
#include "nvs_flash.h"
#include "soc/rtc_cntl_reg.h"
#include "board_pins.h"
#include "aw9523.h"
#include "gt911.h"
#include "mix_i2c.h"
#include "lcd_spi_init.h"
#include "sensors.h"
#include "batt_log.h"
#include "audio.h"
#include "usb_device_uac.h"
#include "mix_view.h"
#include "mix_ui.h"
#include "mix_present.h"
#include "mix_terminal.h"
#include "ttf_font.h"
#include "mix_keyboard.h"
#include "mix_input.h"
#include "mix_link.h"
#include "mix_ota.h"
#include "mix_health.h"
#include "mix_restart.h"

static const char *TAG="MixOS";
static i2c_master_bus_handle_t bus;
static i2c_master_dev_handle_t aw,tp,ina_bat,ina_usb,stc,cw;
static esp_lcd_panel_handle_t panel;
static portMUX_TYPE data_lock=portMUX_INITIALIZER_UNLOCKED;
static mix_view_t sampled,view;
static uint32_t sample_ms;
static bool codec_ready,usb_audio_allowed,device_service_enabled;
static esp_err_t audio_boot_error;
static bool link_initialized,usb_requested,ota_worker_ready;
static bool maintenance_mode;
static DRAM_ATTR portMUX_TYPE rgb_lock=portMUX_INITIALIZER_UNLOCKED;
static DRAM_ATTR uint32_t rgb_vsyncs,rgb_frames;
/* ISR counters establish scanout activity, not a physical LCD self-test. */
static bool rgb_vsync(esp_lcd_panel_handle_t p,const esp_lcd_rgb_panel_event_data_t *e,void *ctx){
    (void)p;(void)e;(void)ctx;
    portENTER_CRITICAL_ISR(&rgb_lock);rgb_vsyncs++;portEXIT_CRITICAL_ISR(&rgb_lock);
    return false;
}
static bool rgb_frame_complete(esp_lcd_panel_handle_t p,const esp_lcd_rgb_panel_event_data_t *e,void *ctx){
    (void)p;(void)e;(void)ctx;
    portENTER_CRITICAL_ISR(&rgb_lock);rgb_frames++;portEXIT_CRITICAL_ISR(&rgb_lock);
    return mix_present_frame_complete();
}
static bool rgb_progress_healthy(uint32_t now){
    static uint32_t seen_vsync,seen_frame,vsync_at,frame_at;
    static bool observed,have_vsync,have_frame;
    portENTER_CRITICAL(&rgb_lock);uint32_t vs=rgb_vsyncs,fr=rgb_frames;portEXIT_CRITICAL(&rgb_lock);
    /* Establish the baseline after first draw; pre-render interrupts alone
     * must never pass the trial's display-health check. */
    if(!observed){observed=true;seen_vsync=vs;seen_frame=fr;return false;}
    if(vs!=seen_vsync){seen_vsync=vs;vsync_at=now;have_vsync=true;}
    if(fr!=seen_frame){seen_frame=fr;frame_at=now;have_frame=true;}
    return have_vsync&&have_frame&&(uint32_t)(now-vsync_at)<=1000&&(uint32_t)(now-frame_at)<=1000;
}
static int brightness=10,volume=60;
static uint64_t mic_sums[2];static uint32_t mic_frames;
static int amp_wanted=-1;
static uint32_t clock_ms(void){return (uint32_t)(esp_timer_get_time()/1000);}

static i2c_master_dev_handle_t add_sensor(uint8_t address){
    esp_err_t probe=mix_i2c_probe(address,I2C_PROBE_TIMEOUT_MS);
    if(probe!=ESP_OK){ESP_LOGW(TAG,"I2C 设备 0x%02X 无应答: %s",address,esp_err_to_name(probe));return NULL;}
    return mix_i2c_add_device(address,I2C_STANDARD_HZ);
}
/* Main task is the only owner of tp. Never discard a handle if removal
 * fails, and never touch LCD SPI or audio pins during touch recovery. */
static esp_err_t recover_touch(void){
    esp_err_t e;
    if(tp){
        if((e=mix_i2c_rm_device(tp))!=ESP_OK)return e;
        tp=NULL;
    }
    if((e=aw9523_gt911_reset(aw))!=ESP_OK)return e;
    return gt911_init(&tp); /* probes both addresses and reloads volatile config */
}
/* Touch sampling state: main task only, including the present wait hook.
 * Keep every edge and movement in order. A single latest-point mailbox would
 * lose a short swipe's down/up pair, or turn a drag into a click. */
#define TOUCH_QUEUE_CAP 32u
typedef struct { int x,y; bool down,cancel; } touch_event_t;
static touch_event_t touch_queue[TOUCH_QUEUE_CAP];
static unsigned touch_read_at,touch_count;
static int touch_x,touch_y,tp_errors;
static bool tp_down;
static uint32_t last_tp_retry,last_tp_poll;
static void touch_enqueue(touch_event_t event){
    if(touch_count==TOUCH_QUEUE_CAP){
        /* A stalled owner must cancel, not replay an incomplete gesture.
         * Retain the current real frame so a real release can end quarantine. */
        touch_read_at=0;touch_count=1;
        touch_queue[0]=(touch_event_t){.cancel=true};
    }
    touch_queue[(touch_read_at+touch_count)%TOUCH_QUEUE_CAP]=event;
    ++touch_count;
}
static void sample_touch(uint32_t ms,bool quick){
    if(!tp||tp_errors>=3||(uint32_t)(ms-last_tp_poll)<16)return;
    last_tp_poll=ms;
    gt911_touch_t t={0};
    esp_err_t e=quick?gt911_try_read(tp,&t):gt911_read(tp,&t);
    if(e==ESP_OK){
        tp_errors=0;
        if(t.count){touch_x=t.x;touch_y=t.y;}
        tp_down=t.count>0;
        touch_enqueue((touch_event_t){touch_x,touch_y,tp_down,false});
    }else if(e==ESP_ERR_NOT_FOUND){
        /* Idle/bus busy is not an up event. A held finger stays held. */
        tp_errors=0;
    }else{
        touch_x=touch_y=0;tp_down=false;
        touch_enqueue((touch_event_t){.cancel=true});
        ++tp_errors;
    }
}
static void touch_wait_sample(void *ctx){
    (void)ctx;
    /* No recovery, UI dispatch, logging or drawing in the wait hook. */
    sample_touch(clock_ms(),true);
}
static void dispatch_touch(void){
    /* Pop before dispatch: home-card feedback may itself present and sample.
     * Limit work to the entry count so input cannot starve link/OTA progress. */
    unsigned budget=touch_count;
    while(budget--&&touch_count){
        touch_event_t e=touch_queue[touch_read_at];
        touch_read_at=(touch_read_at+1)%TOUCH_QUEUE_CAP;--touch_count;
        if(e.cancel)mix_ui_touch_cancel();else mix_ui_touch(e.x,e.y,e.down);
    }
}
/* End touch sampling state. */

static esp_err_t start_board(void){
    i2c_master_bus_config_t c={.i2c_port=-1,.sda_io_num=PIN_I2C_SDA,.scl_io_num=PIN_I2C_SCL,
        .clk_source=I2C_CLK_SRC_DEFAULT,.glitch_ignore_cnt=7,.flags.enable_internal_pullup=true};
    esp_err_t e=i2c_new_master_bus(&c,&bus);if(e!=ESP_OK)return e;
    /* Every driver reaches the bus through mix_i2c, so it must own the handle
     * before the first device is added. */
    if((e=mix_i2c_init(bus))!=ESP_OK)return e;
    if((e=aw9523_init(&aw))!=ESP_OK)return e;
    if((e=aw9523_update_bits(aw,AW9523_REG_OUTPUT_P1,AW9523_P1_LCD_RST,0))!=ESP_OK)return e;
    vTaskDelay(pdMS_TO_TICKS(20));
    if((e=aw9523_update_bits(aw,AW9523_REG_OUTPUT_P1,AW9523_P1_LCD_RST,AW9523_P1_LCD_RST))!=ESP_OK)return e;
    vTaskDelay(pdMS_TO_TICKS(120));
    if((e=lcd_jd9168s_spi_init())!=ESP_OK)return e;
    if(aw9523_gt911_reset(aw)==ESP_OK)gt911_init(&tp);
    /* GPIO47/48 are now free for I2S. Never re-init panel SPI while audio runs. */
    _Static_assert(LCD_V_RES % LCD_BOUNCE_LINES == 0,
                   "IDF requires whole bounce buffers per frame");
    esp_lcd_rgb_panel_config_t rgb={
        .clk_src=LCD_CLK_SRC_PLL240M,
        .timings={.pclk_hz=LCD_PCLK_HZ,.h_res=LCD_H_RES,.v_res=LCD_V_RES,
            .hsync_front_porch=LCD_HFP,.hsync_pulse_width=LCD_HSYNC_W,.hsync_back_porch=LCD_HBP,
            .vsync_front_porch=LCD_VFP,.vsync_pulse_width=LCD_VSYNC_W,.vsync_back_porch=LCD_VBP},
        /* mix_present explicitly fills the idle driver buffer and waits for
         * the bounce-frame callback before reusing the old one. Merely setting
         * num_fbs=2 does not synchronize copies from an external UI canvas. */
        .data_width=16,.bits_per_pixel=16,.num_fbs=2,.bounce_buffer_size_px=LCD_H_RES*LCD_BOUNCE_LINES,
        .hsync_gpio_num=PIN_LCD_HSYNC,.vsync_gpio_num=PIN_LCD_VSYNC,.de_gpio_num=PIN_LCD_DE,
        .pclk_gpio_num=PIN_LCD_PCLK,.disp_gpio_num=-1,
        .data_gpio_nums={PIN_LCD_B3,PIN_LCD_B4,PIN_LCD_B5,PIN_LCD_B6,PIN_LCD_B7,
            PIN_LCD_G2,PIN_LCD_G3,PIN_LCD_G4,PIN_LCD_G5,PIN_LCD_G6,PIN_LCD_G7,
            PIN_LCD_R3,PIN_LCD_R4,PIN_LCD_R5,PIN_LCD_R6,PIN_LCD_R7},.flags.fb_in_psram=true};
    if((e=esp_lcd_new_rgb_panel(&rgb,&panel))!=ESP_OK)return e;
    esp_lcd_rgb_panel_event_callbacks_t callbacks={.on_vsync=rgb_vsync,.on_frame_buf_complete=rgb_frame_complete};
    if((e=esp_lcd_rgb_panel_register_event_callbacks(panel,&callbacks,NULL))!=ESP_OK)return e;
    if((e=esp_lcd_panel_reset(panel))!=ESP_OK)return e;
    return esp_lcd_panel_init(panel);
}
static esp_err_t apply_brightness(int step){
    int wanted=brightness+step;if(wanted<1)wanted=1;if(wanted>10)wanted=10;
    uint32_t duty=mix_ui_display_awake()?(uint32_t)wanted*1023/10:0;
    esp_err_t e=ledc_set_duty(LEDC_LOW_SPEED_MODE,LEDC_CHANNEL_0,duty);
    if(e==ESP_OK)e=ledc_update_duty(LEDC_LOW_SPEED_MODE,LEDC_CHANNEL_0);
    if(e==ESP_OK)brightness=wanted;
    else ESP_LOGW(TAG,"Backlight duty update failed: %s",esp_err_to_name(e));
    return e;
}
static esp_err_t start_backlight(void){
    ledc_timer_config_t t={.speed_mode=LEDC_LOW_SPEED_MODE,.duty_resolution=LEDC_TIMER_10_BIT,
        .timer_num=LEDC_TIMER_0,.freq_hz=10000,.clk_cfg=LEDC_AUTO_CLK};
    esp_err_t e=ledc_timer_config(&t);
    if(e!=ESP_OK)return e;
    ledc_channel_config_t c={.gpio_num=PIN_LCD_BL,.speed_mode=LEDC_LOW_SPEED_MODE,
        .channel=LEDC_CHANNEL_0,.timer_sel=LEDC_TIMER_0,.duty=0};
    e=ledc_channel_config(&c);
    if(e!=ESP_OK)return e;
    return apply_brightness(0);
}
static bool is_codec_ready(void){portENTER_CRITICAL(&data_lock);bool r=codec_ready;portEXIT_CRITICAL(&data_lock);return r;}
static void set_amp(bool on){
    esp_err_t e=aw9523_update_bits(aw,AW9523_REG_OUTPUT_P1,AW9523_P1_AMP_4V6_EN,on?AW9523_P1_AMP_4V6_EN:0);
    if(e==ESP_OK)e=aw9523_update_bits(aw,AW9523_REG_CONFIG_P1,AW9523_P1_AMP_4V6_EN,0);
    if(e==ESP_OK)amp_wanted=on;
    else ESP_LOGW(TAG,"Amplifier enable failed: %s",esp_err_to_name(e));
}
static bool audio_callbacks_enabled(void){portENTER_CRITICAL(&data_lock);bool enabled=usb_audio_allowed;portEXIT_CRITICAL(&data_lock);return enabled;}
static esp_err_t output(uint8_t *b,size_t n,void *ctx){
    (void)ctx;if(audio_callbacks_enabled())audio_write(b,n);return ESP_OK;
}
static esp_err_t input(uint8_t *b,size_t n,size_t *got,void *ctx){
    (void)ctx;
    if(!audio_callbacks_enabled()){memset(b,0,n);*got=n;return ESP_OK;}
    /* Only a capture that succeeded may be judged. On failure audio_read
     * zero-fills, and zeros are a dead line by any honest test, so accounting
     * them would make a failed codec rebuild trigger the next one. */
    if(audio_read(b,n)==ESP_OK)audio_note_capture(b,n);
    uint64_t sums[2]={0,0};
    /* The USB stack hands over whole stereo 16-bit frames. Anything else would
     * silently drop the tail sample and misalign the channel split. */
    size_t frames=n/4;
    if(n%4)ESP_LOGW(TAG,"USB capture buffer %u is not a whole stereo frame",(unsigned)n);
    const int16_t *p=(const int16_t*)(const void*)b;
    for(size_t i=0;i<frames;i++){int32_t l=p[i*2],r=p[i*2+1];sums[0]+=(uint64_t)(l*l);sums[1]+=(uint64_t)(r*r);}
    portENTER_CRITICAL(&data_lock);mic_sums[0]+=sums[0];mic_sums[1]+=sums[1];mic_frames+=(uint32_t)frames;portEXIT_CRITICAL(&data_lock);
    *got=n;return ESP_OK;
}
static void mute(uint32_t v,void *ctx){(void)ctx;if(audio_callbacks_enabled())audio_set_mute(v!=0);}
static void host_volume(uint32_t v,void *ctx){(void)ctx;if(audio_callbacks_enabled())audio_set_volume((int)v);}
static void usb_start_task(void *arg){
    bool with_audio=arg!=NULL;
    esp_err_t e=mix_watchdog_task_begin(MIX_HEALTH_USB_START);
    if(e!=ESP_OK){mix_health_usb_result(e);vTaskDelete(NULL);return;}
    if(with_audio){
        esp_err_t audio_err=audio_start(bus);
        if(audio_err!=ESP_OK)ESP_LOGE(TAG,"Audio codec unavailable: %s",esp_err_to_name(audio_err));
        portENTER_CRITICAL(&data_lock);
        codec_ready=audio_ready();audio_boot_error=audio_err!=ESP_OK?audio_err:(codec_ready?ESP_OK:ESP_ERR_INVALID_STATE);
        usb_audio_allowed=codec_ready&&device_service_enabled;
        portEXIT_CRITICAL(&data_lock);
        e=mix_watchdog_task_reset(MIX_HEALTH_USB_START);
    }
    /* Maintenance keeps precisely the same UAC+CDC descriptors and framing.
     * Its silent/drop callbacks never probe the codec or touch I2S GPIO47/48. */
    uac_device_config_t c={.skip_tinyusb_init=false,.output_cb=output,.input_cb=input,
        .set_mute_cb=mute,.set_volume_cb=host_volume};
    if(e==ESP_OK)e=uac_device_init(&c);
    if(e==ESP_OK)e=mix_link_start_io();
    esp_err_t ended=mix_watchdog_task_end(MIX_HEALTH_USB_START);
    if(e==ESP_OK)e=ended;
    mix_health_usb_result(e);
    vTaskDelete(NULL);
}
static void start_usb(bool with_audio){
    if(usb_requested)return;
    usb_requested=true;
    if(xTaskCreate(usb_start_task,"audio_usb",4096,with_audio?(void *)1:NULL,5,NULL)!=pdPASS)
        mix_health_usb_result(ESP_ERR_NO_MEM);
}
static void startup_failure(const char *stage,esp_err_t error){
    mix_health_record_failure(stage,error);
    if(mix_ota_pending_verify()){
        /* PENDING_VERIFY is deliberately left unconfirmed. Restarting lets
         * the bootloader select the previous valid slot. */
        vTaskDelay(pdMS_TO_TICKS(200));esp_restart();
    }
    maintenance_mode=true;
}
static void maintenance_loop(void){
    maintenance_mode=true;
    portENTER_CRITICAL(&data_lock);usb_audio_allowed=false;device_service_enabled=false;portEXIT_CRITICAL(&data_lock);
    ESP_LOGW(TAG,"USB maintenance mode: display/font/audio hardware are not required");
    if(link_initialized)start_usb(false);
    bool terminal_failure=!link_initialized||!ota_worker_ready;
    bool reported_usb=false;
    for(;;){
        uint32_t ms=clock_ms();
        if(link_initialized){
            mix_link_tick(ms,&view);
            if(mix_link_take_restart_request())mix_restart();
            if(mix_link_take_boot_request()){
                vTaskDelay(pdMS_TO_TICKS(100));REG_WRITE(RTC_CNTL_OPTION1_REG,RTC_CNTL_FORCE_DOWNLOAD_BOOT);esp_restart();
            }
        }
        esp_err_t usb;
        if(mix_health_usb_finished(&usb)&&usb!=ESP_OK&&!reported_usb){
            reported_usb=true;terminal_failure=true;startup_failure("maintenance USB/link",usb);
        }
        esp_err_t wd=mix_watchdog_task_reset(MIX_HEALTH_MAIN);
        if(wd!=ESP_OK)startup_failure("maintenance watchdog",wd);
        ms=clock_ms();
        /* Maintenance is an update/recovery route, never evidence that this
         * trial's local display works. This budget is independent of a host. */
        mix_ota_local_health_tick(ms,false);
        if(terminal_failure)mix_health_degraded_handoff(ms);
        else mix_health_handoff(ms);
        vTaskDelay(pdMS_TO_TICKS(20));
    }
}
static void device_task(void *arg){
    (void)arg;mix_view_t s={0};s.runtime_hours=NAN;
    /* The rotating presence scan. Named constants replace the bare address
     * table that used to sit here alongside four hard-coded special cases. */
    static const uint8_t addresses[]={
        AW9523_I2C_ADDR, INA219_VBAT_ADDR, INA219_VBUS_ADDR, STC3117_I2C_ADDR,
        CW2015_I2C_ADDR, GT911_I2C_ADDR_PRIMARY, MIX_KEYBOARD_I2C_ADDR,
        0x10 /* ES8389, alternates probed below */, IMU_I2C_ADDR, AUX_I2C_ADDR};
    enum { PROBE_COUNT = sizeof(addresses)/sizeof(addresses[0]) };
    enum { PROBE_GT911 = 5, PROBE_CODEC = 7, PROBE_IMU = 8 };
    uint32_t last=0;int hp=-1,hp_candidate=-1,stable=0,health_fail=0,probe=0;
    for(;;){
        portENTER_CRITICAL(&data_lock);bool service_enabled=device_service_enabled;portEXIT_CRITICAL(&data_lock);
        if(!service_enabled){vTaskDelay(pdMS_TO_TICKS(100));continue;}
        uint8_t in=0;
        if(aw9523_read_reg(aw,AW9523_REG_INPUT_P1,&in)==ESP_OK){
            int raw=!!(in&AW9523_P1_HP_DET);s.headphone_valid=true;
            if(raw==hp_candidate)stable++;else {hp_candidate=raw;stable=0;}
            if(stable>=1){hp=raw;s.headphone_inserted=hp==0;}
            if(hp>=0&&amp_wanted!=(hp!=0))set_amp(hp!=0);
            if(hp>=0&&is_codec_ready())audio_set_dac_lr_swap(hp!=0);
        }else s.headphone_valid=false;
        uint32_t ms=clock_ms();
        if((uint32_t)(ms-last)>=1000){
            last=ms;s.battery_v=s.battery_a=s.usb_v=s.usb_a=s.soc=NAN;
            s.battery_valid=ina_bat&&ina219_read(ina_bat,&s.battery_v,&s.battery_a)==ESP_OK;
            s.usb_valid=ina_usb&&ina219_read(ina_usb,&s.usb_v,&s.usb_a)==ESP_OK;
            float voltage=NAN;
            bool stc_running=stc && stc3117_ensure_running(stc)==ESP_OK;
            bool stc_readable=stc && stc3117_read(stc,&voltage,&s.soc)==ESP_OK;
            bool stc_ok=stc_running && stc_readable;
            s.soc_valid=stc_ok && stc3117_soc_is_plausible(voltage,s.soc);
            if(stc_running && stc_readable && !s.soc_valid)
                ESP_LOGW(TAG,"STC3117 SOC %.3f%% 与电压 %.3fV 不一致，忽略并启用回退",s.soc,voltage);
            if(!s.soc_valid&&cw){int soc=-1;if(cw2015_read(cw,&voltage,&soc)==ESP_OK&&soc>=0&&soc<=100){s.soc=(float)soc;s.soc_valid=true;}}
            if(!s.soc_valid && s.battery_valid) {
                int estimated = battery_soc_from_voltage(s.battery_v);
                if (estimated >= 0) { s.soc = (float)estimated; s.soc_valid = true; }
            }
            s.capacity_mah=batt_log_capacity_mah();s.calibration_verified=batt_log_calibration_verified();s.runtime_hours=NAN;
            float watts=0;
            if(s.calibration_verified&&s.soc_valid&&s.usb_valid&&s.usb_v<=4&&batt_log_avg_discharge_w(60,&watts)>0&&watts>0.1f)
                s.runtime_hours=s.capacity_mah*0.001f*BOARD_BATT_NOMINAL_V*s.soc*0.01f/watts;
            if(!aw9523_state_matches(aw)){
                if(aw9523_reinit(aw,true)!=ESP_OK&&++health_fail>=3){health_fail=0;mix_i2c_reset_bus();}
            }else health_fail=0;
            uint8_t a=addresses[probe];bool found=mix_i2c_probe(a,I2C_PROBE_TIMEOUT_MS)==ESP_OK;
            if(!found&&probe==PROBE_GT911)found=mix_i2c_probe(GT911_I2C_ADDR_ALT,I2C_PROBE_TIMEOUT_MS)==ESP_OK;
            if(!found&&probe==PROBE_CODEC){static const uint8_t codec[]=ES8389_I2C_ADDR_CANDIDATES;
                for(unsigned i=1;i<sizeof(codec)/sizeof(codec[0])&&!found;i++)
                    found=mix_i2c_probe(codec[i],I2C_PROBE_TIMEOUT_MS)==ESP_OK;}
            if(!found&&probe==PROBE_IMU)found=mix_i2c_probe(IMU_I2C_ADDR_ALT,I2C_PROBE_TIMEOUT_MS)==ESP_OK;
            s.sensors_checked|=(uint16_t)(1u<<probe);
            if(found)s.sensors_present|=(uint16_t)(1u<<probe);else s.sensors_present&=(uint16_t)~(1u<<probe);
            probe=(probe+1)%PROBE_COUNT;
            portENTER_CRITICAL(&data_lock);
            uint64_t l=mic_sums[0],r=mic_sums[1];uint32_t n=mic_frames;
            mic_sums[0]=mic_sums[1]=0;mic_frames=0;
            portEXIT_CRITICAL(&data_lock);
            s.mic_l=n?sqrtf((float)l/n):0;s.mic_r=n?sqrtf((float)r/n):0;
            /* The ES8389 has been seen to stop driving its I2S data pin after
             * many hours, with the handle still open and playback unaffected,
             * so nothing else in this loop would notice. audio_maintain rate
             * limits itself and logs its own outcome; see audio.h. */
            audio_maintain();
            portENTER_CRITICAL(&data_lock);codec_ready=audio_ready();portEXIT_CRITICAL(&data_lock);
        }
        s.audio_ready=is_codec_ready();
        portENTER_CRITICAL(&data_lock);sampled=s;sample_ms=clock_ms();portEXIT_CRITICAL(&data_lock);
        vTaskDelay(pdMS_TO_TICKS(100));
    }
}
static bool light_feedback_pending;
static int light_feedback_target, saved_keyboard_light=-1;
static uint32_t light_feedback_at;
static void adjust_brightness(int step){
    esp_err_t e=apply_brightness(step);
    mix_ui_feedback(MIX_UI_BRIGHTNESS,e==ESP_OK?brightness*10:-1);
}
static void adjust_volume(int step){
    int wanted=volume+step;if(wanted<0)wanted=0;if(wanted>100)wanted=100;
    esp_err_t e=audio_set_volume(wanted);
    if(e==ESP_OK){volume=wanted;mix_ui_volume(volume);}
    mix_ui_feedback(MIX_UI_VOLUME,e==ESP_OK?volume:-1);
}
static void adjust_keyboard_light(void){
    int level=mix_keyboard_backlight_level();
    if(level<0){mix_ui_feedback(MIX_UI_KEYBOARD_LIGHT,-1);return;}
    light_feedback_target=((light_feedback_pending?light_feedback_target:level)+1)%9;
    /* Absolute targets preserve rapid presses even when the next device
     * report still describes the previous command. */
    mix_keyboard_backlight_set((uint8_t)light_feedback_target);
    light_feedback_pending=true;light_feedback_at=clock_ms();
}
static void sync_local_lock(void){
    static bool previous_lock,previous_awake=true;
    bool is_locked=mix_ui_locked(),awake=mix_ui_display_awake();
    if(is_locked!=previous_lock){
        mix_keyboard_reset_input();light_feedback_pending=false;
        if(is_locked){saved_keyboard_light=mix_keyboard_backlight_level();mix_keyboard_backlight_set(0);}
        else if(saved_keyboard_light>=0)mix_keyboard_backlight_set((uint8_t)saved_keyboard_light);
        previous_lock=is_locked;
    }
    /* Reconnecting a keyboard while locked must not light it back up. */
    if(is_locked&&mix_keyboard_backlight_level()>0)mix_keyboard_backlight_set(0);
    if(awake!=previous_awake&&(!awake||mix_ui_draw_healthy())){
        if(apply_brightness(0)==ESP_OK)previous_awake=awake;
    }
    if(light_feedback_pending&&!is_locked){
        int observed=mix_keyboard_backlight_level();
        if(observed==light_feedback_target){mix_ui_feedback(MIX_UI_KEYBOARD_LIGHT,observed);light_feedback_pending=false;}
        else if(observed<0||(uint32_t)(clock_ms()-light_feedback_at)>800){
            mix_ui_feedback(MIX_UI_KEYBOARD_LIGHT,-1);light_feedback_pending=false;
        }
    }
}
static void key_event(int action,const uint8_t *bytes,size_t len,void *ctx){
    (void)ctx;
    if(mix_ui_locked()&&action!=MIX_KEY_LOCK)return;
    switch(action){
    case MIX_KEY_TEXT:
        if(view.maintenance_busy)mix_ui_key(bytes,len);
        else if(mix_ui_terminal_visible()&&view.terminal_open){if(!mix_link_input(bytes,len))mix_ui_notice("Terminal input unavailable");}
        else mix_ui_key(bytes,len);break;
    case MIX_KEY_IME_TOGGLE:
        if(!view.maintenance_busy&&!view.ota_state&&view.linux_online&&
           mix_ui_terminal_visible()&&view.terminal_open&&view.running_app==MIX_APP_NOTES){
            static const uint8_t toggle[]=MIX_IME_TOGGLE_SEQUENCE;
            if(!mix_link_input(toggle,sizeof(toggle)-1))mix_ui_notice("Terminal input unavailable");
        }
        break;
    case MIX_KEY_LOCK:
        /* Lock screen removed: keep the physical key reserved and inert. */
        break;
    case MIX_KEY_BRIGHT_UP:adjust_brightness(1);break;
    case MIX_KEY_BRIGHT_DOWN:adjust_brightness(-1);break;
    case MIX_KEY_BACKLIGHT:adjust_keyboard_light();break;
    case MIX_KEY_VOLUME_UP:case MIX_KEY_VOLUME_DOWN:
        adjust_volume(action==MIX_KEY_VOLUME_UP?5:-5);break;
    }
}
static void app_back(int app){
    /* A delayed tap must never reach another app or a hidden session. */
    if(app!=MIX_APP_NOTES&&app!=MIX_APP_TRANSLATE&&app!=MIX_APP_SHELL)return;
    if(view.maintenance_busy||view.ota_state||mix_ui_locked()||
       !mix_ui_terminal_visible()||!view.linux_online||!view.terminal_open||
       view.running_app!=app)return;
    uint8_t back=app==MIX_APP_NOTES?0x11:0x1b;
    if(!mix_link_input(&back,1))mix_ui_notice("Terminal input unavailable");
}
static void actions(void){mix_action_t a;
    while(mix_ui_take_action(&a))switch(a.kind){
    case MIX_ACTION_APP_OPEN:
        if(!mix_link_open_app((mix_app_t)a.value))mix_ui_notice("Linux is offline");break;
    case MIX_ACTION_TERMINAL_OPEN:if(!mix_link_open_app(MIX_APP_SHELL))mix_ui_notice("Linux is offline");break;
    case MIX_ACTION_TERMINAL_CLOSE:mix_link_close_terminal();break;
    case MIX_ACTION_TERM_GEOMETRY:mix_link_resize(mix_terminal_cols(),mix_terminal_rows());break;
    case MIX_ACTION_APP_BACK:app_back(a.value);break;
    case MIX_ACTION_NET_SCAN:if(!mix_link_net_scan())mix_ui_notice("Network request unavailable");break;
    case MIX_ACTION_NET_CONNECT:
        if(!mix_link_net_connect(mix_ui_net_ssid(),mix_ui_net_passphrase()))mix_ui_notice("Network request unavailable");
        break;
    case MIX_ACTION_NET_FORGET:
        if(!mix_link_net_forget(mix_ui_net_ssid()))mix_ui_notice("Network request unavailable");break;
    case MIX_ACTION_BRIGHT_UP:adjust_brightness(1);break;
    case MIX_ACTION_BRIGHT_DOWN:adjust_brightness(-1);break;
    case MIX_ACTION_KBD_BACKLIGHT:adjust_keyboard_light();break;
    case MIX_ACTION_VOLUME_UP:case MIX_ACTION_VOLUME_DOWN:
        adjust_volume(a.kind==MIX_ACTION_VOLUME_UP?5:-5);break;
    case MIX_ACTION_JOB_START:mix_link_job(true);break;
    case MIX_ACTION_JOB_CANCEL:mix_link_job(false);break;
    case MIX_ACTION_INPUT_RESET:mix_keyboard_reset_input();break;
    }
}
void app_main(void){
    esp_err_t health=mix_health_init();
    ESP_LOGI(TAG,"MixOS %s - ESP owns display; Linux uses USB protocol",MIX_VERSION);
    esp_err_t nvs=nvs_flash_init();
    /* Preserve original settings: never silently erase NVS on a migration error.
     * OTA's confirmed-state journal must see initialized NVS. */
    mix_ota_init();
    if(health!=ESP_OK)startup_failure("watchdog initialization",health);
    if(nvs!=ESP_OK)startup_failure("NVS initialization",nvs);
    esp_err_t e=mix_ota_init_worker();
    ota_worker_ready=e==ESP_OK;
    if(e!=ESP_OK)startup_failure("OTA worker allocation",e);
    e=mix_link_init();link_initialized=e==ESP_OK;
    if(e!=ESP_OK)startup_failure("link allocation",e);
    if(maintenance_mode||mix_health_maintenance_required())maintenance_loop();
    if((e=start_board())!=ESP_OK){startup_failure("board/LCD initialization",e);maintenance_loop();}
    if((e=mix_watchdog_task_reset(MIX_HEALTH_MAIN))!=ESP_OK){startup_failure("main watchdog",e);maintenance_loop();}
    if((e=start_backlight())!=ESP_OK){startup_failure("backlight initialization",e);maintenance_loop();}
    if((e=ttf_font_init())!=ESP_OK){startup_failure("font initialization",e);maintenance_loop();}
    if((e=mix_ui_init(panel))!=ESP_OK){startup_failure("UI allocation",e);maintenance_loop();}
    if((e=mix_watchdog_task_reset(MIX_HEALTH_MAIN))!=ESP_OK){startup_failure("main watchdog",e);maintenance_loop();}
    ina_bat=add_sensor(INA219_VBAT_ADDR);ina_usb=add_sensor(INA219_VBUS_ADDR);
    stc=add_sensor(STC3117_I2C_ADDR);cw=add_sensor(CW2015_I2C_ADDR);
    /* A sleeping CW2015 returns a frozen reading forever, so a failed wake is
     * the difference between a fallback gauge and a gauge that silently lies. */
    if(cw){esp_err_t e=cw2015_wake(cw);
        if(e!=ESP_OK){ESP_LOGW(TAG,"CW2015 wake failed: %s; dropping the fallback gauge",esp_err_to_name(e));cw=NULL;}}
    batt_log_start(ina_bat,ina_usb,stc,cw);
    /* Without AUDIO_3V3 the codec probe below cannot possibly succeed, so a
     * failure here is worth naming rather than discovering as "no ES8389". */
    esp_err_t dac_power=aw9523_update_bits(aw,AW9523_REG_OUTPUT_P1,AW9523_P1_DAC_3V3_EN,AW9523_P1_DAC_3V3_EN);
    if(dac_power==ESP_OK)dac_power=aw9523_update_bits(aw,AW9523_REG_CONFIG_P1,AW9523_P1_DAC_3V3_EN,0);
    if(dac_power!=ESP_OK){startup_failure("audio power initialization",dac_power);maintenance_loop();}
    set_amp(false);vTaskDelay(pdMS_TO_TICKS(200));
    portENTER_CRITICAL(&data_lock);device_service_enabled=true;portEXIT_CRITICAL(&data_lock);
    start_usb(true);
    if(xTaskCreate(device_task,"devices",6144,NULL,3,NULL)!=pdPASS)ESP_LOGE(TAG,"Device task allocation failed");
    if(mix_keyboard_init(key_event,NULL)!=ESP_OK)mix_ui_notice("Keyboard unavailable");
    gpio_config_t button={.pin_bit_mask=1ULL<<PIN_BOOT_BTN,.mode=GPIO_MODE_INPUT,.pull_up_en=GPIO_PULLUP_ENABLE};
    esp_err_t btn=gpio_config(&button);
    if(btn!=ESP_OK)ESP_LOGW(TAG,"BOOT button unavailable: %s",esp_err_to_name(btn));
    mix_ui_volume(volume);
    int prev=1,candidate=1;
    uint32_t edge=0;char old_notice[96]={0};
    last_tp_retry=clock_ms()-5001u;
    mix_present_set_wait_hook(touch_wait_sample,NULL);
    bool unhealthy_started=false,draw_error_reported=false;uint32_t unhealthy_since=0;
    while(true){
        uint32_t ms=clock_ms(),stamp;
        portENTER_CRITICAL(&data_lock);view=sampled;stamp=sample_ms;portEXIT_CRITICAL(&data_lock);
        if((uint32_t)(ms-stamp)>3000){view.battery_valid=view.usb_valid=view.soc_valid=view.headphone_valid=false;}
        view.brightness=brightness;view.free_psram=(uint32_t)heap_caps_get_free_size(MALLOC_CAP_SPIRAM);
        mix_link_tick(ms,&view);
        /* Consume the durable OTA restart before input/audio/UI work. */
        if(mix_link_take_restart_request())mix_restart();
        if(mix_link_take_input_reset())mix_keyboard_reset_input();
        /* Previously sampled gestures precede a newly observed lock key. */
        dispatch_touch();
        mix_keyboard_tick(ms);view.keyboard_online=mix_keyboard_online();view.keyboard_overflows=mix_keyboard_overflows();
        /* Drain previous display-wait input before recovery or fresh polling,
         * then apply the newest sample before the next UI render. */
        dispatch_touch();
        if((!tp||tp_errors>=3)&&(uint32_t)(ms-last_tp_retry)>5000){
            last_tp_retry=ms;touch_x=touch_y=0;tp_down=false;
            touch_read_at=touch_count=0;mix_ui_touch_cancel();
            esp_err_t retry=recover_touch();
            if(retry==ESP_OK){tp_errors=0;ESP_LOGI(TAG,"GT911 touch recovered");}
            else ESP_LOGW(TAG,"GT911 recovery failed: %s",esp_err_to_name(retry));
        }
        sample_touch(clock_ms(),true);
        dispatch_touch();
        int level=gpio_get_level(PIN_BOOT_BTN);
        if(level!=candidate){candidate=level;edge=ms;}
        if(level!=prev&&(uint32_t)(ms-edge)>=40){prev=level;/* lock key intentionally unused */}
        actions();
        const char *msg=mix_link_notice();if(strcmp(msg,old_notice)){snprintf(old_notice,sizeof(old_notice),"%s",msg);mix_ui_notice(msg);}
        view.firmware_on_trial=mix_ota_pending_verify();
        mix_ui_tick(&view,clock_ms());
        /* Wake the backlight only after the lock surface has been presented. */
        sync_local_lock();
        /* Only a completed render/loop counts as main-task progress. Driver
         * success, continuing scanout, USB init and actual IO/OTA worker loops
         * are distinct evidence; USB mount alone is not a protocol health test. */
        e=mix_watchdog_task_reset(MIX_HEALTH_MAIN);
        if(e!=ESP_OK){startup_failure("main watchdog progress",e);maintenance_loop();}
        ms=clock_ms();
        esp_err_t usb;
        if(mix_health_usb_finished(&usb)){
            if(usb!=ESP_OK){startup_failure("USB/link initialization",usb);maintenance_loop();}
            portENTER_CRITICAL(&data_lock);esp_err_t audio_error=audio_boot_error;portEXIT_CRITICAL(&data_lock);
            if(audio_error!=ESP_OK){startup_failure("audio initialization",audio_error);maintenance_loop();}
        }
        bool draw=mix_ui_draw_healthy();
        if(!draw&&!draw_error_reported){
            ESP_LOGE(TAG,"UI draw submission failed: %s",esp_err_to_name(mix_ui_last_draw_error()));
            draw_error_reported=true;
        }else if(draw)draw_error_reported=false;
        bool local_healthy=draw&&rgb_progress_healthy(ms)&&mix_health_usb_ready()&&mix_health_progress_ok(ms);
        mix_health_handoff(ms);
        mix_ota_local_health_tick(ms,local_healthy);
        mix_ota_health_tick(ms,local_healthy&&mix_link_host_healthy(ms));
        mix_health_note_stable(ms,local_healthy);
        if(local_healthy)unhealthy_started=false;
        else {
            if(!unhealthy_started){unhealthy_started=true;unhealthy_since=ms;}
            /* Trial timeout/rollback belongs to mix_ota_local_health_tick.
             * A valid image stays reachable for repair instead of rebooting
             * repeatedly or attempting LCD SPI initialization over live I2S. */
            if(!mix_ota_pending_verify()&&(uint32_t)(ms-unhealthy_since)>=MIX_HEALTH_WATCHDOG_MS){
                startup_failure("local UI/USB progress lost",ESP_ERR_TIMEOUT);maintenance_loop();
            }
        }
        if(mix_link_take_boot_request()){
            mix_keyboard_reset_input();mix_ui_notice("Entering ROM download mode");mix_ui_tick(&view,ms);
            vTaskDelay(pdMS_TO_TICKS(100));REG_WRITE(RTC_CNTL_OPTION1_REG,RTC_CNTL_FORCE_DOWNLOAD_BOOT);esp_restart();
        }
        /* RGB presentation paces motion at frame boundaries; avoid adding a
         * fixed 20 ms sleep to every animation frame. Input polling is bounded
         * independently, and OTA retains its existing 5 ms cadence. */
        vTaskDelay(pdMS_TO_TICKS(view.ota_state==MIX_OTA_RECEIVING?5:mix_ui_needs_fast_tick()?2:20));
    }
}
