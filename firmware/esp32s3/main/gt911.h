// GT911 触摸驱动（TypixDeck 0720：MUX_SEL=1 后挂在 S3_I2C GPIO6/7 上）
#pragma once

#include "esp_err.h"
#include "driver/i2c_master.h"

// 从机地址见 board_pins.h 的 GT911_I2C_ADDR_PRIMARY / _ALT。此处曾另有一个
// GT911_I2C_ADDR 0x14 的重复定义，没有任何调用点，且与复位时序实际得到的
// 0x5D 矛盾。

typedef struct {
    int count;      // 触点数 0..5
    int x, y;       // 第一个触点原始坐标
} gt911_touch_t;

esp_err_t gt911_init(i2c_master_dev_handle_t *out_dev);
// 调试：读原始状态寄存器 0x814E（不清除）
esp_err_t gt911_raw_status(i2c_master_dev_handle_t dev, uint8_t *status);
// 无新数据时返回 ESP_ERR_NOT_FOUND；有数据时填充 t 并清状态寄存器
esp_err_t gt911_read(i2c_master_dev_handle_t dev, gt911_touch_t *t);
