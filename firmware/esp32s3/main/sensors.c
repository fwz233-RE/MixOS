#include "sensors.h"

#include <math.h>

#include "esp_check.h"
#include "esp_log.h"
#include "mix_i2c.h"

static const char *TAG = "SENSORS";

// ---------------------------------------------------------------------------
// INA219（寄存器级，POR 默认配置 0x399F：32V 量程 / 12bit 连续采样）
// bus voltage LSB=4mV（寄存器右移 3 位），shunt voltage LSB=10µV，I=Vshunt/10mΩ
// ---------------------------------------------------------------------------
#define INA219_REG_SHUNT_V 0x01
#define INA219_REG_BUS_V   0x02
#define INA219_SHUNT_OHM   0.010f

esp_err_t ina219_read(i2c_master_dev_handle_t dev, float *bus_v, float *cur_a)
{
    uint16_t raw;
    ESP_RETURN_ON_ERROR(mix_i2c_read_u16be(dev, INA219_REG_BUS_V, &raw), TAG, "bus_v");
    *bus_v = (float)(raw >> 3) * 0.004f;
    ESP_RETURN_ON_ERROR(mix_i2c_read_u16be(dev, INA219_REG_SHUNT_V, &raw), TAG, "shunt_v");
    *cur_a = (int16_t)raw * 0.00001f / INA219_SHUNT_OHM;
    return ESP_OK;
}

// CW2015：VCELL 14bit LSB 305µV，SOC 整数 %
esp_err_t cw2015_read(i2c_master_dev_handle_t dev, float *v, int *soc)
{
    uint8_t b[2];
    ESP_RETURN_ON_ERROR(mix_i2c_read(dev, 0x02, b, 2, I2C_XFER_TIMEOUT_MS), TAG, "cw vcell");
    *v = (float)((((uint16_t)b[0] & 0x3F) << 8) | b[1]) * 305e-6f;
    ESP_RETURN_ON_ERROR(mix_i2c_read(dev, 0x04, b, 2, I2C_XFER_TIMEOUT_MS), TAG, "cw soc");
    *soc = b[0];
    return ESP_OK;
}

// CW2015 POR 后可能在 sleep，写 MODE(0x0A)=0x00 唤醒
esp_err_t cw2015_wake(i2c_master_dev_handle_t dev)
{
    return mix_i2c_write_u8(dev, 0x0A, 0x00);
}

// ---------------------------------------------------------------------------
// STC3117：V LSB 2.20mV，SOC LSB 1/512%
//
// STC3117 在 POR/BATFAIL 后可能处于 standby，读数会停在第一次转换值。
// 参考实现的启动顺序是：停止 GG_RUN、保留已验证的 CC_CNF/VM_CNF，清除
// PORDET/BATFAIL，再启动混合模式。这里不擅自写入容量或电阻参数：如果
// 配置寄存器为零，只报告“未校准”，并返回错误让上层安全回退。
// ---------------------------------------------------------------------------
#define STC3117_REG_MODE       0x00
#define STC3117_REG_CTRL       0x01
#define STC3117_REG_SOC        0x02
#define STC3117_REG_VOLTAGE    0x08
#define STC3117_REG_CC_CNF     0x0F
#define STC3117_REG_VM_CNF     0x11
#define STC3117_GG_RUN         (1 << 4)   // 1=运行
#define STC3117_FORCE_CC       (1 << 5)
#define STC3117_FORCE_VM       (1 << 6)
#define STC3117_ZERO_SOC_VOLTAGE 3.25f   // 高于此值时原始 0% 通常是冻结/无效值

static bool s_stc_config_warning;
static bool s_stc_first_sample_logged;

/* The STC3117 transmits register words high byte first, like the other gauges
 * on this bus. A helper here used to be named reg16_read_le while its own
 * comment admitted it decoded big endian; mix_i2c_read_u16be now says so in
 * its name and is shared with the INA219 and CW2015 paths. */

esp_err_t stc3117_ensure_running(i2c_master_dev_handle_t dev)
{
    if (!dev) return ESP_ERR_INVALID_ARG;

    /* The whole start-up sequence is a read-modify-write across many
     * registers. Holding the bus for its duration stops the presence scan in
     * the device task from interleaving transfers into the middle of it. */
    mix_i2c_lock();
    esp_err_t result = ESP_OK;

    uint8_t mode = 0;
    result = mix_i2c_read_u8(dev, STC3117_REG_MODE, &mode);
    if (result != ESP_OK) { mix_i2c_unlock(); ESP_LOGE(TAG, "stc mode: %s", esp_err_to_name(result)); return result; }

    // Read the calibration registers even when the gauge is already running.
    // A running but unconfigured gauge must not be promoted to the primary SOC
    // source: the caller needs ESP_ERR_INVALID_STATE to select a fallback.
    uint16_t cc_cnf = 0, vm_cnf = 0;
    #define STC_TRY(expr, what)                                              \
        do {                                                                 \
            esp_err_t stc_err_ = (expr);                                     \
            if (stc_err_ != ESP_OK) {                                        \
                ESP_LOGE(TAG, "%s: %s", (what), esp_err_to_name(stc_err_));  \
                mix_i2c_unlock();                                            \
                return stc_err_;                                             \
            }                                                                \
        } while (0)

    STC_TRY(mix_i2c_read_u16be(dev, STC3117_REG_CC_CNF, &cc_cnf), "stc cc_cnf");
    STC_TRY(mix_i2c_read_u16be(dev, STC3117_REG_VM_CNF, &vm_cnf), "stc vm_cnf");
    bool calibrated = cc_cnf != 0 && vm_cnf != 0;
    if (!calibrated && !s_stc_config_warning) {
        ESP_LOGW(TAG, "STC3117 校准寄存器未完整配置，不写入猜测值；上层将回退 SOC 来源");
        s_stc_config_warning = true;
    }

    if (mode & STC3117_GG_RUN) {
        mix_i2c_unlock();
        return calibrated ? ESP_OK : ESP_ERR_INVALID_STATE;
    }

    uint16_t soc = 0;
    STC_TRY(mix_i2c_read_u16be(dev, STC3117_REG_SOC, &soc), "stc soc init");
    ESP_LOGI(TAG, "STC3117 standby: MODE=0x%02X SOC_RAW=0x%04X CC_CNF=0x%04X VM_CNF=0x%04X",
             mode, soc, cc_cnf, vm_cnf);

    // Stop the gauge before changing algorithm parameters, as required by the
    // ST reference driver. Preserve all mode bits except forced modes/GG_RUN.
    uint8_t stopped_mode = (uint8_t)(mode & ~(STC3117_GG_RUN | STC3117_FORCE_CC | STC3117_FORCE_VM));
    STC_TRY(mix_i2c_write_u8(dev, STC3117_REG_MODE, stopped_mode), "stc stop");

    // Never invent calibration. Writing back non-zero values also preserves a
    // valid board configuration that may have been programmed externally.
    if (vm_cnf) STC_TRY(mix_i2c_write_u16be(dev, STC3117_REG_VM_CNF, vm_cnf), "stc vm_cnf write");
    STC_TRY(mix_i2c_write_u16be(dev, STC3117_REG_SOC, soc), "stc soc preserve");
    if (cc_cnf) STC_TRY(mix_i2c_write_u16be(dev, STC3117_REG_CC_CNF, cc_cnf), "stc cc_cnf write");

    // CTRL=0x03 clears PORDET/BATFAIL and resets the conversion counter.
    STC_TRY(mix_i2c_write_u8(dev, STC3117_REG_CTRL, 0x03), "stc ctrl");
    STC_TRY(mix_i2c_write_u8(dev, STC3117_REG_MODE, (uint8_t)(stopped_mode | STC3117_GG_RUN)), "stc run");

    uint8_t verify = 0;
    STC_TRY(mix_i2c_read_u8(dev, STC3117_REG_MODE, &verify), "stc mode verify");
    #undef STC_TRY
    mix_i2c_unlock();

    if (!(verify & STC3117_GG_RUN)) {
        ESP_LOGE(TAG, "STC3117 启动后 MODE=0x%02X，GG_RUN 未置位", verify);
        return ESP_ERR_INVALID_STATE;
    }
    ESP_LOGI(TAG, "STC3117 已按参考顺序清除 POR/BATFAIL 并启动: MODE=0x%02X", verify);
    return calibrated ? ESP_OK : ESP_ERR_INVALID_STATE;
}

esp_err_t stc3117_read(i2c_master_dev_handle_t dev, float *v, float *soc)
{
    if (!dev || !v || !soc) return ESP_ERR_INVALID_ARG;
    uint16_t raw_v = 0, raw_soc = 0;
    ESP_RETURN_ON_ERROR(mix_i2c_read_u16be(dev, STC3117_REG_VOLTAGE, &raw_v), TAG, "stc v");
    ESP_RETURN_ON_ERROR(mix_i2c_read_u16be(dev, STC3117_REG_SOC, &raw_soc), TAG, "stc soc");
    *v = (float)(int16_t)raw_v * 2.20e-3f;
    *soc = (float)raw_soc / 512.0f;
    if (!s_stc_first_sample_logged) {
        ESP_LOGI(TAG, "STC3117 sample: V_RAW=0x%04X V=%.3fV SOC_RAW=0x%04X SOC=%.3f%%",
                 raw_v, *v, raw_soc, *soc);
        s_stc_first_sample_logged = true;
    }
    return ESP_OK;
}

bool stc3117_soc_is_plausible(float gauge_voltage, float soc)
{
    return isfinite(gauge_voltage) && isfinite(soc) &&
           gauge_voltage >= 2.50f && gauge_voltage <= 4.50f &&
           soc >= 0.0f && soc <= 100.0f &&
           !(soc <= 0.0f && gauge_voltage >= STC3117_ZERO_SOC_VOLTAGE);
}

int battery_soc_from_voltage(float voltage)
{
    // Conservative unloaded Li-ion estimate; this is deliberately marked
    // estimated by the UI and is only a fallback when both gauges fail.
    static const float v[] = {3.20f, 3.35f, 3.50f, 3.60f, 3.70f, 3.78f, 3.86f, 3.95f, 4.05f, 4.15f, 4.20f};
    static const int p[] = {0, 5, 12, 20, 35, 50, 65, 78, 88, 96, 100};
    if (!isfinite(voltage) || voltage < v[0] - 0.05f || voltage > 4.25f) return -1;
    if (voltage <= v[0]) return 0;
    for (size_t i = 1; i < sizeof(v) / sizeof(v[0]); ++i) {
        if (voltage <= v[i]) {
            float f = (voltage - v[i - 1]) / (v[i] - v[i - 1]);
            return (int)lroundf(p[i - 1] + f * (p[i] - p[i - 1]));
        }
    }
    return 100;
}
