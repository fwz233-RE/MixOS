idf_component_register(
    SRCS "main.c" "aw9523.c" "gt911.c" "lcd_spi_init.c" "audio.c"
         "sensors.c" "batt_log.c" "ttf_font.c"
         "mix_ui.c" "mix_input.c" "mix_keyboard.c"
         "mix_protocol.c" "mix_link.c" "mix_terminal.c" "mix_ota.c"
    INCLUDE_DIRS "."
    REQUIRES driver esp_lcd esp_timer esp_driver_i2s esp_hw_support usb_device_uac
             espressif__tinyusb esp_partition nvs_flash json app_update mbedtls
)
# Only components/usb_device_uac/tusb supplies USB descriptors.
target_compile_options(${COMPONENT_LIB} PRIVATE -Wno-error=misleading-indentation)
