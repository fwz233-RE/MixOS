#include "batt_log.h"

#include <string.h>
#include <stdlib.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "esp_log.h"
#include "nvs.h"
#include "nvs_flash.h"

#include "sensors.h"
#include "board_pins.h"
#include "esp_timer.h"
#include "sdkconfig.h"
#include "mix_power_math.h"

bool batt_log_calibration_verified(void) {
#ifdef CONFIG_MIXOS_STC_CALIBRATION_VERIFIED
    return true;
#else
    return false;
#endif
}
static int64_t s_prev_us;

static const char *TAG = "BATTLOG";

static i2c_master_dev_handle_t s_ina, s_ina_bus, s_stc, s_cw;
static batt_sample_t s_ring[BATT_LOG_CAP];
static int s_head = 0;    // 下一个写入位置
static int s_count = 0;
static portMUX_TYPE s_mux = portMUX_INITIALIZER_UNLOCKED;

// ---- 容量自学习（库仑计数法，TypixDeck 电源固件同款思路）----
// 只在**放电段**学习：拔电时 U4(VBAT INA219) 电流≈电池放电电流；
// 插电时 U4 测的是整机输出功率，电池真实充电电流没有直接测点
// （≈P_in−P_out，还含充电效率不确定），不参与积分。
// SOC 端点只认 STC3117（带 10mΩ 采样电阻的库仑计，2026-08-21 用户定的主源；
// CW2015 纯电压估计、后续板子可能不贴，不参与学习）。
// ⚠️ STC3117 走 MUX，Pi 持屏时 ESP 读不到 → SOC 不可读期间**继续积分**
// （INA219 常连总线），锚定/结算只在 SOC 可读的样本上做。
// 算法：锚定一个 SOC，放电梯形积分累计 mAh，ΔSOC≥门槛 时结算：
//   容量 = 累计放电mAh / ΔSOC × 100
// 合理范围 100~20000mAh，EMA（70%旧 + 30%新）平滑后写 NVS("batt"/"cap_mah")。
// 段作废条件：插电、INA219 读失败、SOC 跳变>10%、电压跳变>0.3V（热插拔）。
#define CAP_NVS_NS       "batt"
#define CAP_NVS_KEY      "cap_mah"
#define CAP_MIN_MAH      100.0f
#define CAP_MAX_MAH      20000.0f
// 结算门槛：段越长对大电流下的 SOC 电压凹陷越不敏感
// （1.35A 放电时 SOC 掉得比真实电荷快 → 段太短会系统性低估容量）
#define CAP_SETTLE_DSOC  5.0f    // 每放掉 5% SOC 结算一次

static float s_cap_mah = BOARD_BATT_CAPACITY_MAH;
static bool  s_seg_active = false;   // 放电学习段是否有效
static float s_anchor_soc = -1;      // 段起点 SOC（STC3117，0.1% 分辨率）
static float s_last_soc = -1;        // 段内上一个可读 SOC（跳变检测）
static float s_acc_mah = 0;          // 段内累计放电电荷 mAh
static batt_sample_t s_prev;         // 段内上一个样本（梯形积分 + 跳变检测）

static void cap_load(void)
{
    // ui_init 的 lang_load 已做过 nvs_flash_init（含坏页擦除重试），
    // 这里再调一次是幂等的，仅兜底启动顺序变化。
    if (nvs_flash_init() != ESP_OK) return;
    nvs_handle_t h;
    if (nvs_open(CAP_NVS_NS, NVS_READONLY, &h) != ESP_OK) return;
    uint16_t v = 0;
    if (nvs_get_u16(h, CAP_NVS_KEY, &v) == ESP_OK &&
        v >= CAP_MIN_MAH && v <= CAP_MAX_MAH) {
        s_cap_mah = (float)v;
        ESP_LOGI(TAG, "载入自学习容量 %u mAh", v);
    }
    nvs_close(h);
}

static void cap_save(void)
{
    nvs_handle_t h;
    if (nvs_open(CAP_NVS_NS, NVS_READWRITE, &h) != ESP_OK) return;
    nvs_set_u16(h, CAP_NVS_KEY, (uint16_t)(s_cap_mah + 0.5f));
    nvs_commit(h);
    nvs_close(h);
}

// stc_soc < 0 表示本次 STC3117 不可读（MUX 在 Pi 侧）——继续积分不结算
static void cap_learn(const batt_sample_t *s, float stc_soc)
{
    int64_t now_us = esp_timer_get_time();
    if (!batt_log_calibration_verified()) { s_seg_active=false; return; }
    // 插电 / INA219 读失败：段作废，等下一个干净的放电样本重新锚定
    if (s->plugged || s->mv == 0) {
        s_seg_active = false;
        return;
    }
    if (!s_seg_active) {
        // 新段必须以可读的 STC SOC 锚定
        if (stc_soc < 0) return;
        s_seg_active = true;
        s_anchor_soc = stc_soc;
        s_last_soc = stc_soc;
        s_acc_mah = 0;
        s_prev = *s;
        s_prev_us = now_us;
        return;
    }
    // 电池热插拔判据：电压突变即作废本段（电压 INA219 常连，始终可查）
    if (abs((int)s->mv - (int)s_prev.mv) > 300) {
        s_seg_active = false;
        return;
    }
    // 梯形积分（ma 正=放电；充电/零漂样本按 0 计，不倒扣）。
    // STC SOC 读不到也照常积——电荷不会因为 MUX 在 Pi 侧就蒸发
    int64_t dt = now_us - s_prev_us;
    if (dt <= 0 || dt > 60000000) { s_seg_active=false; return; }
    s_acc_mah += mix_integrate_mah(s_prev.ma, s->ma, dt);
    s_prev = *s;
    s_prev_us = now_us;

    if (stc_soc < 0) return;            // 等下次能读到 SOC 再判结算
    if (s_last_soc >= 0 && (stc_soc - s_last_soc > 10.0f ||
                            s_last_soc - stc_soc > 10.0f)) {
        s_seg_active = false;           // SOC 突变（热插拔/芯片复位）
        return;
    }
    s_last_soc = stc_soc;

    float dsoc = s_anchor_soc - stc_soc;   // 正=SOC 在下降
    if (dsoc >= CAP_SETTLE_DSOC) {
        float new_cap = s_acc_mah / dsoc * 100.0f;
        if (new_cap >= CAP_MIN_MAH && new_cap <= CAP_MAX_MAH) {
            portENTER_CRITICAL(&s_mux);
            s_cap_mah = 0.7f * s_cap_mah + 0.3f * new_cap;
            portEXIT_CRITICAL(&s_mux);
            cap_save();
            ESP_LOGI(TAG, "容量结算：ΔSOC=%.1f%% Δq=%.1fmAh → 本次 %.0f，EMA=%.0f mAh",
                     dsoc, s_acc_mah, new_cap, s_cap_mah);
        } else {
            ESP_LOGW(TAG, "容量结算值 %.0f mAh 越界，丢弃", new_cap);
        }
        // 无论有效与否都以当前点重新锚定，开始下一段
        s_anchor_soc = stc_soc;
        s_acc_mah = 0;
    }
}

float batt_log_capacity_mah(void)
{
    portENTER_CRITICAL(&s_mux);
    float result = s_cap_mah;
    portEXIT_CRITICAL(&s_mux);
    return result;
}

static void batt_log_task(void *arg)
{
    (void)arg;
    while (1) {
        batt_sample_t s = { .mv = 0, .ma = 0, .soc = -1, .plugged = 1 };
        float v = 0, a = 0;
        if (s_ina && ina219_read(s_ina, &v, &a) == ESP_OK) {
            s.mv = (uint16_t)(v * 1000.0f + 0.5f);
            s.ma = (int16_t)(a * 1000.0f);
        }
        float bus_v = 0, bus_a = 0;
        if (s_ina_bus && ina219_read(s_ina_bus, &bus_v, &bus_a) == ESP_OK) {
            s.plugged = bus_v > 4.0f;   // 与 UI 的"插电"阈值一致
        }
        // 主 SOC：STC3117（走 MUX，Pi 持屏时读失败是常态，静默回退）
        float stc_v = 0, stc_soc = -1;
        if (s_stc) {
            bool stc_running = stc3117_ensure_running(s_stc) == ESP_OK;
            bool stc_readable = stc3117_read(s_stc, &stc_v, &stc_soc) == ESP_OK;
            if (!(stc_running && stc_readable &&
                  stc3117_soc_is_plausible(stc_v, stc_soc) &&
                  stc_soc <= 100.0f)) {
                stc_soc = -1;
            }
        }
        if (stc_soc >= 0) {
            s.soc = (int8_t)(stc_soc + 0.5f);
        } else {
            // 曲线显示回退 CW2015（仅参考；不参与容量学习）
            float cw_v = 0;
            int cw_soc = -1;
            if (s_cw && cw2015_read(s_cw, &cw_v, &cw_soc) == ESP_OK) {
                s.soc = (int8_t)cw_soc;
            }
        }
        portENTER_CRITICAL(&s_mux);
        s_ring[s_head] = s;
        s_head = (s_head + 1) % BATT_LOG_CAP;
        if (s_count < BATT_LOG_CAP) s_count++;
        portEXIT_CRITICAL(&s_mux);

        cap_learn(&s, stc_soc);   // 容量自学习（SOC 端点只认 STC3117）

        vTaskDelay(pdMS_TO_TICKS(BATT_LOG_INTERVAL_S * 1000));
    }
}

void batt_log_start(i2c_master_dev_handle_t ina_vbat,
                    i2c_master_dev_handle_t ina_vbus,
                    i2c_master_dev_handle_t stc3117,
                    i2c_master_dev_handle_t cw2015)
{
    s_ina = ina_vbat;
    s_ina_bus = ina_vbus;
    s_stc = stc3117;
    s_cw = cw2015;
    cap_load();
    if (xTaskCreate(batt_log_task, "batt_log", 3072, NULL, 3, NULL) != pdPASS) {
        ESP_LOGE(TAG, "batt_log 任务创建失败");
    }
}

int batt_log_get(batt_sample_t *out, int max)
{
    portENTER_CRITICAL(&s_mux);
    int n = s_count < max ? s_count : max;
    // 取最新的 n 个，按旧→新排列
    int start = (s_head - n + BATT_LOG_CAP) % BATT_LOG_CAP;
    for (int i = 0; i < n; i++) {
        out[i] = s_ring[(start + i) % BATT_LOG_CAP];
    }
    portEXIT_CRITICAL(&s_mux);
    return n;
}

int batt_log_avg_discharge_w(int window_s, float *avg_w)
{
    int n_want = window_s / BATT_LOG_INTERVAL_S;
    if (n_want <= 0) return 0;
    if (n_want > BATT_LOG_CAP) n_want = BATT_LOG_CAP;

    float sum = 0;
    int cnt = 0;
    portENTER_CRITICAL(&s_mux);
    int n = s_count < n_want ? s_count : n_want;
    int start = (s_head - n + BATT_LOG_CAP) % BATT_LOG_CAP;
    for (int i = 0; i < n; i++) {
        const batt_sample_t *s = &s_ring[(start + i) % BATT_LOG_CAP];
        // 插电与否都算（U4 恒测输出侧功率），只滤掉读失败和零漂（≤30mA）
        if (s->mv == 0 || s->ma <= 30) continue;
        sum += (s->mv * 1e-3f) * (s->ma * 1e-3f);
        cnt++;
    }
    portEXIT_CRITICAL(&s_mux);

    if (cnt > 0 && avg_w) *avg_w = sum / cnt;
    return cnt;
}
