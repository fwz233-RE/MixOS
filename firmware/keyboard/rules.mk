# Supported with the exact QMK revision in QMK_PIN.json only.
# Standard feature settings live in keyboard.json (avoid duplicate QMK metadata).
LTO_ENABLE = yes
# Retain debug symbols and a linker map; QMK keeps size optimization enabled.
DEBUG_ENABLE = yes
MAGIC_ENABLE = no
SPACE_CADET_ENABLE = no
GRAVE_ESC_ENABLE = no
DEBOUNCE_TYPE = sym_defer_g
BOOTLOADER = custom
STM32_BOOTLOADER_ADDRESS = 0x1FFFC400
SRC += i2c_slave_kbd.c mix_kbd_transport.c stm32_bootloader.c
# Keep the two final 1 KiB flash pages outside ld/STM32F042x6.ld.
EEPROM_DRIVER = legacy_stm32_flash
