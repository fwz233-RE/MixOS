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
static portMUX_TYPE rgb_lock=portMUX_INITIALIZER_UNLOCKED;
static uint32_t rgb_vsyncs,rgb_frames;
/* ISR counters establish scanout activity, not a physical LCD self-test. */
static bool rgb_vsync(esp_lcd_panel_handle_t p,const esp_lcd_rgb_panel_event_data_t *e,void *ctx){
    (void)p;(void)e;(void)ctx;
    portENTER_CRITICAL_ISR(&rgb_lock);rgb_vsyncs++;portEXIT_CRITICAL_ISR(&rgb_lock);
    return false;
}
static bool rgb_frame_complete(esp_lcd_panel_handle_t p,const esp_lcd_rgb_panel_event_data_t *e,void *ctx){
    (void)p;(void)e;(void)ctx;
    portENTER_CRITICAL_ISR(&rgb_lock);rgb_frames++;portEXIT_CRITICAL_ISR(&rgb_lock);
    return false;
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
    esp_lcd_rgb_panel_config_t rgb={
        .clk_src=LCD_CLK_SRC_PLL240M,
        .timings={.pclk_hz=LCD_PCLK_HZ,.h_res=LCD_H_RES,.v_res=LCD_V_RES,
            .hsync_front_porch=LCD_HFP,.hsync_pulse_width=LCD_HSYNC_W,.hsync_back_porch=LCD_HBP,
            .vsync_front_porch=LCD_VFP,.vsync_pulse_width=LCD_VSYNC_W,.vsync_back_porch=LCD_VBP},
        /* The display driver keeps two scanout buffers so the LCD controller
         * never scans from the same memory that the UI is repainting. The UI
         * owns its separate canvas and presents changed rows through the driver. */
        .data_width=16,.bits_per_pixel=16,.num_fbs=2,.bounce_buffer_size_px=LCD_H_RES*16,
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
    brightness+=step;if(brightness<1)brightness=1;if(brightness>10)brightness=10;
    esp_err_t e=ledc_set_duty(LEDC_LOW_SPEED_MODE,LEDC_CHANNEL_0,(uint32_t)brightness*1023/10);
    if(e==ESP_OK)e=ledc_update_duty(LEDC_LOW_SPEED_MODE,LEDC_CHANNEL_0);
    /* Dropping these used to make the brightness keys look dead with no clue why. */
    if(e!=ESP_OK)ESP_LOGW(TAG,"Backlight duty update failed: %s",esp_err_to_name(e));
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
static void key_event(int action,const uint8_t *bytes,size_t len,void *ctx){
    (void)ctx;
    switch(action){
    case MIX_KEY_TEXT:
        if(view.maintenance_busy)mix_ui_key(bytes,len);
        else if(mix_ui_terminal_visible()&&view.terminal_open){if(!mix_link_input(bytes,len))mix_ui_notice("Terminal input unavailable");}
        else mix_ui_key(bytes,len);break;
    case MIX_KEY_HOME:if(!view.maintenance_busy){mix_ui_home_toggle();mix_keyboard_reset_input();}break;
    case MIX_KEY_BRIGHT_UP:apply_brightness(1);break;
    case MIX_KEY_BRIGHT_DOWN:apply_brightness(-1);break;
    case MIX_KEY_BACKLIGHT:mix_keyboard_backlight_step();break;
    case MIX_KEY_VOLUME_UP:case MIX_KEY_VOLUME_DOWN:
        volume+=action==MIX_KEY_VOLUME_UP?5:-5;if(volume<0)volume=0;if(volume>100)volume=100;
        audio_set_volume(volume);
        mix_ui_volume(volume);break;
    }
}
static void actions(void){mix_action_t a;
    while(mix_ui_take_action(&a))switch(a.kind){
    case MIX_ACTION_APP_OPEN:
        if(!mix_link_open_app((mix_app_t)a.value))mix_ui_notice("Linux is offline");break;
    case MIX_ACTION_TERMINAL_OPEN:if(!mix_link_open_app(MIX_APP_SHELL))mix_ui_notice("Linux is offline");break;
    case MIX_ACTION_TERMINAL_CLOSE:mix_link_close_terminal();break;
    case MIX_ACTION_TERM_GEOMETRY:mix_link_resize(mix_terminal_cols(),mix_terminal_rows());break;
    case MIX_ACTION_NET_SCAN:if(!mix_link_net_scan())mix_ui_notice("Network request unavailable");break;
    case MIX_ACTION_NET_CONNECT:
        if(!mix_link_net_connect(mix_ui_net_ssid(),mix_ui_net_passphrase()))mix_ui_notice("Network request unavailable");
        break;
    case MIX_ACTION_NET_FORGET:
        if(!mix_link_net_forget(mix_ui_net_ssid()))mix_ui_notice("Network request unavailable");break;
    case MIX_ACTION_BRIGHT_UP:apply_brightness(1);break;
    case MIX_ACTION_BRIGHT_DOWN:apply_brightness(-1);break;
    case MIX_ACTION_KBD_BACKLIGHT:mix_keyboard_backlight_step();break;
    case MIX_ACTION_VOLUME_UP:case MIX_ACTION_VOLUME_DOWN:
        volume+=a.kind==MIX_ACTION_VOLUME_UP?5:-5;if(volume<0)volume=0;if(volume>100)volume=100;
        audio_set_volume(volume);break;
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
    int prev=1,candidate=1;uint32_t edge=0,last_tp_retry=0,last_tp=0;char old_notice[96]={0};
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
        mix_keyboard_tick(ms);view.keyboard_online=mix_keyboard_online();view.keyboard_overflows=mix_keyboard_overflows();
        if(!tp&&(uint32_t)(ms-last_tp_retry)>5000){last_tp_retry=ms;gt911_init(&tp);}
        if(tp){gt911_touch_t t={0};esp_err_t e=gt911_read(tp,&t);
            if(e==ESP_OK){mix_ui_touch(t.x,t.y,t.count>0);last_tp=ms;}
            else if(e!=ESP_ERR_NOT_FOUND||(uint32_t)(ms-last_tp)>500)mix_ui_touch(0,0,false);}
        int level=gpio_get_level(PIN_BOOT_BTN);
        if(level!=candidate){candidate=level;edge=ms;}
        if(level!=prev&&(uint32_t)(ms-edge)>=40){prev=level;if(!level&&!view.maintenance_busy){mix_ui_home_toggle();mix_keyboard_reset_input();}}
        actions();
        const char *msg=mix_link_notice();if(strcmp(msg,old_notice)){snprintf(old_notice,sizeof(old_notice),"%s",msg);mix_ui_notice(msg);}
        view.firmware_on_trial=mix_ota_pending_verify();
        mix_ui_tick(&view,ms);
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
        /* A firmware transfer drains the RX queue far faster at 5 ms. */
        vTaskDelay(pdMS_TO_TICKS(view.ota_state==MIX_OTA_RECEIVING?5:20));
    }
}
