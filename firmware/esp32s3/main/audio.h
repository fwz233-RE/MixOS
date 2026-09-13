// ES8389 codec 初始化（给 USB UAC 放音用，无 MP3）
// LCD 初始化完成后调用；配好 I2S（ESP 主、ES8389 从、无 MCLK）+ ES8389 codec，
// 返回 codec handle 供 UAC output_cb 把主机 PCM 写进去。
#pragma once
#include "driver/i2c_master.h"
#include "esp_codec_dev.h"

void audio_start(i2c_master_bus_handle_t bus);
esp_codec_dev_handle_t audio_codec_handle(void);      // 放音（OUT）设备：write / 音量 / mute
esp_codec_dev_handle_t audio_codec_in_handle(void);   // 录音（IN）设备：read / 麦克风增益
int audio_set_dac_lr_swap(bool swap);                  // 外放左右反接补偿（REG0x44 bit5:4）
