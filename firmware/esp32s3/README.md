# TypixNode ESP32-S3 Firmware

ESP32-S3 coprocessor firmware for the TypixNode / TypixDeck handheld
(Raspberry Pi CM4/CM5 based cyberdeck). This is the main production firmware
running on the on-board ESP32-S3-PICO-1.

## Features

- **USB UAC audio**: full-speed USB sound card (48 kHz, ES8389 codec,
  speaker / headphone with HP detect, dual microphones)
- **USB CDC console**: debug commands and remote maintenance. The former
  `EGGFLY_*` magic strings are gone; maintenance now travels as framed
  messages on the protocol's maintenance channel. A framebuffer dump is
  available there (`MIX_SCREEN_REQUEST`); see `tools/esp_screenshot.py`.
- **LCD GUI**: 1024x768 RGB (DPI) panel dashboard with four themes,
  Chinese/English UI (FreeType + font partition), battery / power monitoring
- **Touch**: GT911 reset handling behind the CM/ESP display MUX
- **System management**: AW9523B IO expander (power sequencing, safe
  shutdown chain to the CM), INA219 / STC3117 / CW2015 gauges, RX8130 RTC,
  QMI8658 IMU

## Build

ESP-IDF v5.4.x, target `esp32s3`:

```bash
. $IDF_PATH/export.sh
idf.py set-target esp32s3
idf.py build
```

Note: flash size must be set manually to 8 MB
(`CONFIG_ESPTOOLPY_FLASHSIZE="8MB"`); auto-detection misreads it as 2 MB.

## Flash

- Normal (firmware alive): write `EGGFLY_REBOOT_TO_BOOT_MODE\n` to the CDC
  port, then flash with esptool (auto hard-reset applies).
- Bricked firmware: hold the side ESP32 BOOT button (SW3), tap RESET (SW1),
  flash, then press RESET again to leave download mode.

Prebuilt images are under `release/`.

## License

MIT — see [LICENSE](LICENSE). Third-party components under `components/` and
`managed_components/` keep their own licenses.
