idf_component_register(
    SRCS "main.c" "mix_i2c.c" "aw9523.c" "gt911.c" "lcd_spi_init.c" "audio.c"
         "sensors.c" "batt_log.c" "ttf_font.c"
         "mix_md3.c" "mix_lv_font.c"
         "mix_ui.c" "mix_present.c" "mix_input.c" "mix_keyboard.c"
         "mix_protocol.c" "mix_link.c" "mix_terminal.c" "mix_ota.c" "mix_ota_tx.c" "mix_health.c" "mix_restart.c"
    INCLUDE_DIRS "."
    REQUIRES driver esp_lcd esp_timer esp_driver_i2s esp_hw_support usb_device_uac
             espressif__tinyusb esp_partition nvs_flash json app_update mbedtls
             lvgl__lvgl espressif__esp_lvgl_port
)
# Only components/usb_device_uac/tusb supplies USB descriptors.
target_compile_options(${COMPONENT_LIB} PRIVATE -Wno-error=misleading-indentation)
