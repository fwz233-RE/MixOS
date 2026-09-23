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
// 无新数据返回 ESP_ERR_NOT_FOUND（不清状态）；仅成功读帧且清状态成功时填充 t。
// 任何读/清状态错误都会传播给调用方；错误或无数据不表示真实抬手。
esp_err_t gt911_read(i2c_master_dev_handle_t dev, gt911_touch_t *t);
// 显示等待期间使用：总线忙则跳过，最多三次 4ms 事务，不执行复位。
// ESP_ERR_NOT_FOUND 也可表示总线忙，不能当作抬手。
esp_err_t gt911_try_read(i2c_master_dev_handle_t dev, gt911_touch_t *t);
