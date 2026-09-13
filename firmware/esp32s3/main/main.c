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
#include "lcd_spi_init.h"
#include "sensors.h"
#include "batt_log.h"
#include "audio.h"
#include "usb_device_uac.h"
#include "mix_view.h"
#include "mix_ui.h"
#include "ttf_font.h"
#include "mix_keyboard.h"
#include "mix_input.h"
#include "mix_link.h"
#include "mix_ota.h"

static const char *TAG="MixOS";
static i2c_master_bus_handle_t bus;
static i2c_master_dev_handle_t aw,tp,ina_bat,ina_usb,stc,cw;
static esp_lcd_panel_handle_t panel;
static portMUX_TYPE data_lock=portMUX_INITIALIZER_UNLOCKED;
static mix_view_t sampled,view;
static uint32_t sample_ms;
static bool codec_ready;
static int brightness=10,volume=60;
static uint64_t mic_sums[2];static uint32_t mic_frames;
static int amp_wanted=-1;
static uint32_t clock_ms(void){return (uint32_t)(esp_timer_get_time()/1000);}

static i2c_master_dev_handle_t add_sensor(uint8_t address){
    i2c_master_dev_handle_t dev=NULL;
    esp_err_t probe=i2c_master_probe(bus,address,20);
    if(probe!=ESP_OK){ESP_LOGW(TAG,"I2C 设备 0x%02X 无应答: %s",address,esp_err_to_name(probe));return NULL;}
    i2c_device_config_t c={.dev_addr_length=I2C_ADDR_BIT_LEN_7,.device_address=address,.scl_speed_hz=100000};
    if(i2c_master_bus_add_device(bus,&c,&dev)!=ESP_OK)return NULL;return dev;
}
static esp_err_t start_board(void){
    i2c_master_bus_config_t c={.i2c_port=-1,.sda_io_num=PIN_I2C_SDA,.scl_io_num=PIN_I2C_SCL,
        .clk_source=I2C_CLK_SRC_DEFAULT,.glitch_ignore_cnt=7,.flags.enable_internal_pullup=true};
    esp_err_t e=i2c_new_master_bus(&c,&bus);if(e!=ESP_OK)return e;
    if((e=aw9523_init(bus,&aw))!=ESP_OK)return e;
    if((e=aw9523_update_bits(aw,AW9523_REG_OUTPUT_P1,AW9523_P1_LCD_RST,0))!=ESP_OK)return e;
    vTaskDelay(pdMS_TO_TICKS(20));
    if((e=aw9523_update_bits(aw,AW9523_REG_OUTPUT_P1,AW9523_P1_LCD_RST,AW9523_P1_LCD_RST))!=ESP_OK)return e;
    vTaskDelay(pdMS_TO_TICKS(120));
    if((e=lcd_jd9168s_spi_init())!=ESP_OK)return e;
    if(aw9523_gt911_reset(aw)==ESP_OK)gt911_init(bus,&tp);
    /* GPIO47/48 are now free for I2S. Never re-init panel SPI while audio runs. */
    esp_lcd_rgb_panel_config_t rgb={
        .clk_src=LCD_CLK_SRC_PLL240M,
        .timings={.pclk_hz=LCD_PCLK_HZ,.h_res=LCD_H_RES,.v_res=LCD_V_RES,
            .hsync_front_porch=LCD_HFP,.hsync_pulse_width=LCD_HSYNC_W,.hsync_back_porch=LCD_HBP,
            .vsync_front_porch=LCD_VFP,.vsync_pulse_width=LCD_VSYNC_W,.vsync_back_porch=LCD_VBP},
        .data_width=16,.bits_per_pixel=16,.num_fbs=1,.bounce_buffer_size_px=LCD_H_RES*16,
        .hsync_gpio_num=PIN_LCD_HSYNC,.vsync_gpio_num=PIN_LCD_VSYNC,.de_gpio_num=PIN_LCD_DE,
        .pclk_gpio_num=PIN_LCD_PCLK,.disp_gpio_num=-1,
        .data_gpio_nums={PIN_LCD_B3,PIN_LCD_B4,PIN_LCD_B5,PIN_LCD_B6,PIN_LCD_B7,
            PIN_LCD_G2,PIN_LCD_G3,PIN_LCD_G4,PIN_LCD_G5,PIN_LCD_G6,PIN_LCD_G7,
            PIN_LCD_R3,PIN_LCD_R4,PIN_LCD_R5,PIN_LCD_R6,PIN_LCD_R7},.flags.fb_in_psram=true};
    if((e=esp_lcd_new_rgb_panel(&rgb,&panel))!=ESP_OK)return e;
    if((e=esp_lcd_panel_reset(panel))!=ESP_OK)return e;
    return esp_lcd_panel_init(panel);
}
static void apply_brightness(int step){
    brightness+=step;if(brightness<1)brightness=1;if(brightness>10)brightness=10;
    ledc_set_duty(LEDC_LOW_SPEED_MODE,LEDC_CHANNEL_0,(uint32_t)brightness*1023/10);
    ledc_update_duty(LEDC_LOW_SPEED_MODE,LEDC_CHANNEL_0);
}
static void start_backlight(void){
    ledc_timer_config_t t={.speed_mode=LEDC_LOW_SPEED_MODE,.duty_resolution=LEDC_TIMER_10_BIT,
        .timer_num=LEDC_TIMER_0,.freq_hz=10000,.clk_cfg=LEDC_AUTO_CLK};
    ESP_ERROR_CHECK(ledc_timer_config(&t));
    ledc_channel_config_t c={.gpio_num=PIN_LCD_BL,.speed_mode=LEDC_LOW_SPEED_MODE,
        .channel=LEDC_CHANNEL_0,.timer_sel=LEDC_TIMER_0,.duty=0};
    ESP_ERROR_CHECK(ledc_channel_config(&c));apply_brightness(0);
}
static bool is_codec_ready(void){portENTER_CRITICAL(&data_lock);bool r=codec_ready;portEXIT_CRITICAL(&data_lock);return r;}
static void set_amp(bool on){
    esp_err_t e=aw9523_update_bits(aw,AW9523_REG_OUTPUT_P1,AW9523_P1_AMP_4V6_EN,on?AW9523_P1_AMP_4V6_EN:0);
    if(e==ESP_OK)e=aw9523_update_bits(aw,AW9523_REG_CONFIG_P1,AW9523_P1_AMP_4V6_EN,0);
    if(e==ESP_OK)amp_wanted=on;
}
static esp_err_t output(uint8_t *b,size_t n,void *ctx){
    (void)ctx;esp_codec_dev_handle_t h=audio_codec_handle();if(h)esp_codec_dev_write(h,b,n);return ESP_OK;
}
static esp_err_t input(uint8_t *b,size_t n,size_t *got,void *ctx){
    (void)ctx;esp_codec_dev_handle_t h=audio_codec_in_handle();
    if(!h||esp_codec_dev_read(h,b,n)!=ESP_CODEC_DEV_OK)memset(b,0,n);
    uint64_t sums[2]={0,0};const int16_t *p=(const int16_t*)b;
    for(size_t i=0;i<n/4;i++){int32_t l=p[i*2],r=p[i*2+1];sums[0]+=(uint64_t)(l*l);sums[1]+=(uint64_t)(r*r);}
    portENTER_CRITICAL(&data_lock);mic_sums[0]+=sums[0];mic_sums[1]+=sums[1];mic_frames+=(uint32_t)(n/4);portEXIT_CRITICAL(&data_lock);
    *got=n;return ESP_OK;
}
static void mute(uint32_t v,void *ctx){(void)ctx;if(audio_codec_handle())esp_codec_dev_set_out_mute(audio_codec_handle(),v!=0);}
static void host_volume(uint32_t v,void *ctx){(void)ctx;if(audio_codec_handle())esp_codec_dev_set_out_vol(audio_codec_handle(),v>100?100:(int)v);}
static void usb_start_task(void *arg){
    (void)arg;audio_start(bus);
    portENTER_CRITICAL(&data_lock);codec_ready=audio_codec_handle()!=NULL;portEXIT_CRITICAL(&data_lock);
    uac_device_config_t c={.skip_tinyusb_init=false,.output_cb=output,.input_cb=input,
        .set_mute_cb=mute,.set_volume_cb=host_volume};
    if(uac_device_init(&c)==ESP_OK)ESP_ERROR_CHECK(mix_link_start_io());
    else ESP_LOGE(TAG,"USB audio/CDC failed; local UI remains available");
    vTaskDelete(NULL);
}
static void device_task(void *arg){
    (void)arg;mix_view_t s={0};s.runtime_hours=NAN;
    const uint8_t addresses[]={0x5b,0x40,0x41,0x70,0x62,0x5d,0x1f,0x10,0x6a,0x32};
    uint32_t last=0;int hp=-1,hp_candidate=-1,stable=0,health_fail=0,probe=0;
    for(;;){
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
                if(aw9523_reinit(aw,true)!=ESP_OK&&++health_fail>=3){health_fail=0;i2c_master_bus_reset(bus);}
            }else health_fail=0;
            uint8_t a=addresses[probe];bool found=i2c_master_probe(bus,a,10)==ESP_OK;
            if(!found&&probe==5)found=i2c_master_probe(bus,0x14,10)==ESP_OK;
            if(!found&&probe==7)for(uint8_t alt=0x11;alt<=0x13&&!found;alt++)found=i2c_master_probe(bus,alt,10)==ESP_OK;
            if(!found&&probe==8)found=i2c_master_probe(bus,0x6b,10)==ESP_OK;
            s.sensors_checked|=(uint16_t)(1u<<probe);
            if(found)s.sensors_present|=(uint16_t)(1u<<probe);else s.sensors_present&=(uint16_t)~(1u<<probe);
            probe=(probe+1)%10;
            portENTER_CRITICAL(&data_lock);
            uint64_t l=mic_sums[0],r=mic_sums[1];uint32_t n=mic_frames;
            mic_sums[0]=mic_sums[1]=0;mic_frames=0;
            portEXIT_CRITICAL(&data_lock);
            s.mic_l=n?sqrtf((float)l/n):0;s.mic_r=n?sqrtf((float)r/n):0;
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
        if(is_codec_ready())esp_codec_dev_set_out_vol(audio_codec_handle(),volume);
        mix_ui_notice("Local audio output volume changed");break;
    }
}
static void actions(void){mix_action_t a;
    while(mix_ui_take_action(&a))switch(a.kind){
    case MIX_ACTION_TERMINAL_OPEN:if(!mix_link_open_terminal())mix_ui_notice("Linux is offline");break;
    case MIX_ACTION_TERMINAL_CLOSE:mix_link_close_terminal();break;
    case MIX_ACTION_BRIGHT_UP:apply_brightness(1);break;
    case MIX_ACTION_BRIGHT_DOWN:apply_brightness(-1);break;
    case MIX_ACTION_KBD_BACKLIGHT:mix_keyboard_backlight_step();break;
    case MIX_ACTION_JOB_START:mix_link_job(true);break;
    case MIX_ACTION_JOB_CANCEL:mix_link_job(false);break;
    case MIX_ACTION_INPUT_RESET:mix_keyboard_reset_input();break;
    }
}
void app_main(void){
    ESP_LOGI(TAG,"MixOS %s - ESP owns display; Linux uses USB protocol",MIX_VERSION);
    mix_ota_init();
    esp_err_t nvs=nvs_flash_init();
    /* Preserve original settings: never silently erase NVS on a migration error. */
    if(nvs!=ESP_OK)ESP_LOGW(TAG,"NVS unavailable: %s; preferences not persisted",esp_err_to_name(nvs));
    if(start_board()!=ESP_OK){ESP_LOGE(TAG,"Board init failed; use hardware BOOT+RESET recovery");return;}
    start_backlight();
    esp_err_t font_err=ttf_font_init();
    if(font_err!=ESP_OK)ESP_LOGW(TAG,"Font initialization failed: %s; using ASCII fallback",esp_err_to_name(font_err));
    if(mix_link_init()!=ESP_OK||mix_ui_init(panel)!=ESP_OK){ESP_LOGE(TAG,"MixOS allocation failed");return;}
    ina_bat=add_sensor(INA219_VBAT_ADDR);ina_usb=add_sensor(INA219_VBUS_ADDR);stc=add_sensor(0x70);cw=add_sensor(0x62);
    if(cw)cw2015_wake(cw);
    batt_log_start(ina_bat,ina_usb,stc,cw);
    aw9523_update_bits(aw,AW9523_REG_OUTPUT_P1,AW9523_P1_DAC_3V3_EN,AW9523_P1_DAC_3V3_EN);
    aw9523_update_bits(aw,AW9523_REG_CONFIG_P1,AW9523_P1_DAC_3V3_EN,0);
    set_amp(false);vTaskDelay(pdMS_TO_TICKS(200));
    if(xTaskCreate(usb_start_task,"audio_usb",16384,NULL,5,NULL)!=pdPASS)ESP_LOGE(TAG,"Audio task allocation failed");
    if(xTaskCreate(device_task,"devices",4096,NULL,3,NULL)!=pdPASS)ESP_LOGE(TAG,"Device task allocation failed");
    if(mix_keyboard_init(bus,key_event,NULL)!=ESP_OK)mix_ui_notice("Keyboard unavailable");
    gpio_config_t button={.pin_bit_mask=1ULL<<PIN_BOOT_BTN,.mode=GPIO_MODE_INPUT,.pull_up_en=GPIO_PULLUP_ENABLE};gpio_config(&button);
    int prev=1,candidate=1;uint32_t edge=0,last_tp_retry=0,last_tp=0;char old_notice[96]={0};
    while(true){
        uint32_t ms=clock_ms(),stamp;
        portENTER_CRITICAL(&data_lock);view=sampled;stamp=sample_ms;portEXIT_CRITICAL(&data_lock);
        if((uint32_t)(ms-stamp)>3000){view.battery_valid=view.usb_valid=view.soc_valid=view.headphone_valid=false;}
        view.brightness=brightness;view.free_psram=(uint32_t)heap_caps_get_free_size(MALLOC_CAP_SPIRAM);
        mix_link_tick(ms,&view);
        if(mix_link_take_input_reset())mix_keyboard_reset_input();
        mix_keyboard_tick(ms);view.keyboard_online=mix_keyboard_online();view.keyboard_overflows=mix_keyboard_overflows();
        if(!tp&&(uint32_t)(ms-last_tp_retry)>5000){last_tp_retry=ms;gt911_init(bus,&tp);}
        if(tp){gt911_touch_t t={0};esp_err_t e=gt911_read(tp,&t);
            if(e==ESP_OK){mix_ui_touch(t.x,t.y,t.count>0);last_tp=ms;}
            else if(e!=ESP_ERR_NOT_FOUND||(uint32_t)(ms-last_tp)>500)mix_ui_touch(0,0,false);}
        int level=gpio_get_level(PIN_BOOT_BTN);
        if(level!=candidate){candidate=level;edge=ms;}
        if(level!=prev&&(uint32_t)(ms-edge)>=40){prev=level;if(!level&&!view.maintenance_busy){mix_ui_home_toggle();mix_keyboard_reset_input();}}
        actions();
        const char *msg=mix_link_notice();if(strcmp(msg,old_notice)){snprintf(old_notice,sizeof(old_notice),"%s",msg);mix_ui_notice(msg);}
        /* Reaching this point every loop means the board, UI and USB came up,
         * which is what an A/B trial build has to prove before it is kept. */
        view.firmware_on_trial=mix_ota_pending_verify();
        mix_ota_health_tick(ms,mix_link_usb_mounted());
        mix_ui_tick(&view,ms);
        if(mix_link_take_restart_request()){
            mix_keyboard_reset_input();mix_ui_notice("Restarting into the new firmware");mix_ui_tick(&view,ms);
            vTaskDelay(pdMS_TO_TICKS(300));esp_restart();
        }
        if(mix_link_take_boot_request()){
            mix_keyboard_reset_input();mix_ui_notice("Entering ROM download mode");mix_ui_tick(&view,ms);
            vTaskDelay(pdMS_TO_TICKS(100));REG_WRITE(RTC_CNTL_OPTION1_REG,RTC_CNTL_FORCE_DOWNLOAD_BOOT);esp_restart();
        }
        /* A firmware transfer drains the RX queue far faster at 5 ms. */
        vTaskDelay(pdMS_TO_TICKS(view.ota_state==MIX_OTA_RECEIVING?5:20));
    }
}
