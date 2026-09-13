// 电池历史采样（后台任务，无论谁持屏都在跑）：
// 每 BATT_LOG_INTERVAL_S 秒采一次 INA219(VBAT/VBUS，常连 ESP 总线) +
// STC3117（主 SOC，⚠️ 走 MUX：Pi 持屏时读不到）+ CW2015（仅参考/回退，
// 后续板子可能不贴），环形缓冲存最近 1 小时，供电池曲线页绘图。
#pragma once

#include <stdbool.h>
#include <stdint.h>
#include "driver/i2c_master.h"

#define BATT_LOG_INTERVAL_S  5
#define BATT_LOG_CAP         720   // 720 × 5s = 1 小时

typedef struct {
    uint16_t mv;      // 电池电压 mV（INA219 VBAT bus voltage）
    int16_t  ma;      // 电流 mA（正=放电）
    int8_t   soc;     // SOC %（主 STC3117，读不到回退 CW2015），-1=都失败
    uint8_t  plugged; // 采样时 VBUS > 4.0V（INA219 VBUS 读失败按 1 处理，宁缺勿错）
} batt_sample_t;

void batt_log_start(i2c_master_dev_handle_t ina_vbat,
                    i2c_master_dev_handle_t ina_vbus,
                    i2c_master_dev_handle_t stc3117,
                    i2c_master_dev_handle_t cw2015);

// 按时间顺序（旧→新）拷出最近 max 个样本，返回实际数量
int batt_log_get(batt_sample_t *out, int max);

// 最近 window_s 秒内输出功率（U4 VBAT INA219，>30mA 的样本）的平均值 [W]。
// 插电与否都统计——U4 测的是输出到整机的功率，插电时它就是整机功耗，
// 拔线瞬间即可用作放电功率预测（2026-08-21 用户口径：5 秒出首个估算值，
// 随样本增多收敛，窗口最近 1 分钟）。返回参与平均的有效样本数。
int batt_log_avg_discharge_w(int window_s, float *avg_w);

// 当前电池容量 mAh：库仑计数自学习值（NVS 持久化），
// 没有学习值时返回 BOARD_BATT_CAPACITY_MAH 默认种子。
bool batt_log_calibration_verified(void);
float batt_log_capacity_mah(void);
