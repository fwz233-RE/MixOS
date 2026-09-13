// 电源/电量传感器迷你驱动（从 main.c 抽出）：
//   INA219 ×2（U4 VBAT @0x40 / U20 VBUS @0x41，10mΩ 采样电阻）
//   CW2015 电量计（U27 @0x62，常连 ESP 总线）
//   STC3117 电量计（U53 @0x70，⚠️ 在 MUX U71 后面，仅 MUX=ESP 侧可达）
#pragma once

#include <stdbool.h>
#include "esp_err.h"
#include "driver/i2c_master.h"

esp_err_t ina219_read(i2c_master_dev_handle_t dev, float *bus_v, float *cur_a);
esp_err_t cw2015_read(i2c_master_dev_handle_t dev, float *v, int *soc);
esp_err_t cw2015_wake(i2c_master_dev_handle_t dev);
esp_err_t stc3117_read(i2c_master_dev_handle_t dev, float *v, float *soc);
// 当电量计 SOC 不可读时，按单节锂电池电压给出明确的估算值（非校准值）。
int battery_soc_from_voltage(float voltage);
// 按参考驱动顺序启动 STC3117；只保留已有非零校准值，不猜测容量/电阻参数。
// 若 CC_CNF/VM_CNF 缺失，仍尝试让芯片运行，但返回 ESP_ERR_INVALID_STATE，
// 调用方必须改用其他 SOC 来源。
esp_err_t stc3117_ensure_running(i2c_master_dev_handle_t dev);
// 用电池电压拒绝“正常电压下冻结的原始 0%”，允许安全回退。
bool stc3117_soc_is_plausible(float gauge_voltage, float soc);
