#include "gt911.h"

#include <string.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "esp_log.h"

static const char *TAG = "GT911";

// GT911 寄存器地址是 16 位大端
#define REG_COMMAND     0x8040  // 命令：0x00 读坐标 0x01 rawdata 0x02 软复位 0x05 休眠
#define REG_CONFIG_VER  0x8047  // 配置区（RAM 副本）：写入即可读回，不代表已生效
#define REG_PRODUCT_ID  0x8140
#define REG_FW_VER      0x8144
#define REG_RT_XRES     0x8146  // 运行时分辨率：只有配置校验通过被采纳后才非 0
#define REG_SENSOR_ID   0x814A
#define REG_STATUS      0x814E
#define REG_POINT0      0x814F

static esp_err_t reg_read(i2c_master_dev_handle_t dev, uint16_t reg, uint8_t *buf, size_t len)
{
    uint8_t addr[2] = { reg >> 8, reg & 0xFF };
    return i2c_master_transmit_receive(dev, addr, 2, buf, len, 100);
}

static esp_err_t reg_write_u8(i2c_master_dev_handle_t dev, uint16_t reg, uint8_t val)
{
    uint8_t buf[3] = { reg >> 8, reg & 0xFF, val };
    return i2c_master_transmit(dev, buf, 3, 100);
}

// ---- 通用配置表（0x8047..0x80FE，184 字节）----
// 本模组 GT911 出厂配置区全 0（NVM 未烧录），芯片不会扫描/报点，必须由主机
// 每次复位后下发。以下按 Goodix 参考模板改：1024x768、5 点、INT 下降沿，
// 通道映射用顺序映射（非本传感器实测值，先验证能报点，精度后续再调）。
static const uint8_t k_cfg[184] = {
    0x41, 0x00, 0x04, 0x00, 0x03, 0x05, 0x05, 0x00, 0x01, 0x08,  // ver, 1024, 768, 5点
    0x28, 0x05, 0x50, 0x32, 0x03, 0x05, 0x00, 0x00, 0x00, 0x00,
    0x00, 0x00, 0x00, 0x18, 0x1A, 0x1E, 0x14, 0x8B, 0x2B, 0x0D,
    0x2D, 0x2B, 0x0F, 0x0A, 0x00, 0x00, 0x00, 0x9A, 0x03, 0x25,
    0x00, 0x00, 0x00, 0x00, 0x00, 0x03, 0x64, 0x32, 0x00, 0x00,
    0x00, 0x0F, 0x94, 0x94, 0xC5, 0x02, 0x07, 0x00, 0x00, 0x04,
    0x8D, 0x13, 0x00, 0x5C, 0x1E, 0x00, 0x3C, 0x30, 0x00, 0x29,
    0x4C, 0x00, 0x1E, 0x78, 0x00, 0x1E, 0x00, 0x00, 0x00, 0x00,
    0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
    0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
    0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
    0x00, 0x00,
    // Sensor_CH0..13 (0x80B7)
    0x00, 0x02, 0x04, 0x06, 0x08, 0x0A, 0x0C, 0x0E, 0x10, 0x12,
    0x14, 0x16, 0x18, 0x1A,
    // 0x80C5..0x80D4 保留
    0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF,
    0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF,
    // Driver_CH0..25 (0x80D5)
    0x00, 0x02, 0x04, 0x06, 0x08, 0x0A, 0x0C, 0x0F, 0x10, 0x12,
    0x13, 0x16, 0x18, 0x1C, 0x1D, 0x1E, 0x1F, 0x20, 0x21, 0x22,
    0x24, 0x26, 0xFF, 0xFF, 0xFF, 0xFF,
    // 0x80EF..0x80FE 保留
    0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
    0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
};

// 下发配置到 RAM（掉电/复位丢失，不烧 NVM）：0x8047 起连写 184 字节 +
// checksum(0x80FF) + fresh flag(0x8100)=1
static esp_err_t send_config(i2c_master_dev_handle_t dev)
{
    uint8_t buf[2 + sizeof(k_cfg) + 2];
    buf[0] = 0x80;
    buf[1] = 0x47;
    memcpy(&buf[2], k_cfg, sizeof(k_cfg));
    uint8_t sum = 0;
    for (size_t i = 0; i < sizeof(k_cfg); i++) {
        sum += k_cfg[i];
    }
    buf[2 + sizeof(k_cfg)] = (uint8_t)(~sum + 1);  // checksum
    buf[3 + sizeof(k_cfg)] = 0x01;                 // config fresh
    return i2c_master_transmit(dev, buf, sizeof(buf), 200);
}

static esp_err_t probe_addr(i2c_master_bus_handle_t bus, uint8_t addr,
                            i2c_master_dev_handle_t *out_dev, uint8_t id[4])
{
    i2c_device_config_t cfg = {
        .dev_addr_length = I2C_ADDR_BIT_LEN_7,
        .device_address = addr,
        .scl_speed_hz = 100 * 1000,
    };
    i2c_master_dev_handle_t dev;
    esp_err_t err = i2c_master_bus_add_device(bus, &cfg, &dev);
    if (err != ESP_OK) {
        return err;
    }
    err = reg_read(dev, REG_PRODUCT_ID, id, 4);
    if (err != ESP_OK || id[0] != '9') {
        i2c_master_bus_rm_device(dev);
        return (err != ESP_OK) ? err : ESP_ERR_INVALID_RESPONSE;
    }
    *out_dev = dev;
    return ESP_OK;
}

esp_err_t gt911_init(i2c_master_bus_handle_t bus, i2c_master_dev_handle_t *out_dev)
{
    // 复位时序 INT 拉低贯穿 → 地址 0x5D；万一时序被打断退回 0x14
    uint8_t id[4] = { 0 };
    uint8_t addr = 0x5D;
    i2c_master_dev_handle_t dev = NULL;
    esp_err_t err = probe_addr(bus, addr, &dev, id);
    if (err != ESP_OK) {
        addr = 0x14;
        err = probe_addr(bus, addr, &dev, id);
        if (err != ESP_OK) {
            ESP_LOGE(TAG, "0x5D/0x14 均无应答: %s（查 MUX_SEL / TP_RST 时序）",
                     esp_err_to_name(err));
            return err;
        }
    }

    // ---- 深度诊断：运行时信息块 0x8140..0x814A ----
    // 0x8146 运行时分辨率是"配置是否真被芯片采纳"的判据（配置区 0x8047 只是
    // RAM 副本，写进去就能读回，不代表校验通过）
    uint8_t info[11] = { 0 };
    reg_read(dev, REG_PRODUCT_ID, info, 11);
    int rt_x = info[6] | (info[7] << 8);
    int rt_y = info[8] | (info[9] << 8);
    ESP_LOGI(TAG, "GT911 @0x%02X ID=\"%c%c%c\" fw=0x%02X%02X 运行时分辨率=%dx%d Sensor_ID=0x%02X",
             addr, info[0], info[1], info[2], info[5], info[4], rt_x, rt_y, info[10]);

    // ---- 完整配置区 dump（0x8047..0x8100 共 186 字节）----
    uint8_t cfg[186] = { 0 };
    reg_read(dev, 0x8047, cfg, sizeof(cfg));
    for (int i = 0; i < 186; i += 31) {
        char line[3 * 31 + 1] = { 0 };
        int n = (186 - i < 31) ? (186 - i) : 31;
        for (int j = 0; j < n; j++) {
            snprintf(line + j * 3, 4, "%02X ", cfg[i + j]);
        }
        ESP_LOGI(TAG, "cfg[%3d..%3d]: %s", i, i + n - 1, line);
    }
    uint8_t sum = 0;
    for (int i = 0; i < 184; i++) {
        sum += cfg[i];
    }
    uint8_t calc = (uint8_t)(~sum + 1);
    ESP_LOGI(TAG, "cfg_ver=0x%02X 配置区分辨率=%dx%d 校验和: 存储=0x%02X 计算=0x%02X %s",
             cfg[0], cfg[1] | (cfg[2] << 8), cfg[3] | (cfg[4] << 8),
             cfg[184], calc, (cfg[184] == calc) ? "✓" : "✗ 不匹配");

    bool cfg_blank = true;
    for (int i = 0; i < 184; i++) {
        if (cfg[i] != 0) {
            cfg_blank = false;
            break;
        }
    }

    if (cfg_blank || rt_x == 0) {
        ESP_LOGW(TAG, "%s，下发通用 1024x768 配置...",
                 cfg_blank ? "配置区全 0" : "配置未被采纳（运行时分辨率=0）");
        err = send_config(dev);
        if (err != ESP_OK) {
            ESP_LOGE(TAG, "配置下发失败: %s", esp_err_to_name(err));
            i2c_master_bus_rm_device(dev);
            return err;
        }
        vTaskDelay(pdMS_TO_TICKS(200));
        reg_read(dev, REG_PRODUCT_ID, info, 11);
        rt_x = info[6] | (info[7] << 8);
        rt_y = info[8] | (info[9] << 8);
        ESP_LOGI(TAG, "下发后运行时分辨率=%dx%d %s", rt_x, rt_y,
                 (rt_x == 1024) ? "✓ 配置被采纳" : "✗ 仍未采纳");
        if (rt_x == 0) {
            // 不走 RST 引脚的固件重启：命令寄存器 0x8040 = 0x02（软复位）。
            // 复位线（R78）不通时这是唯一能重启触摸固件的手段。
            ESP_LOGW(TAG, "尝试 GT911 软复位命令（0x8040=0x02）...");
            reg_write_u8(dev, REG_COMMAND, 0x02);
            vTaskDelay(pdMS_TO_TICKS(300));
            reg_write_u8(dev, REG_COMMAND, 0x00);   // 回到读坐标模式
            vTaskDelay(pdMS_TO_TICKS(100));
            reg_read(dev, REG_PRODUCT_ID, info, 11);
            rt_x = info[6] | (info[7] << 8);
            rt_y = info[8] | (info[9] << 8);
            ESP_LOGI(TAG, "软复位后运行时分辨率=%dx%d Sensor_ID=0x%02X %s",
                     rt_x, rt_y, info[10],
                     (rt_x == 1024) ? "✓ 恢复" : "✗ 无效");
        }
        if (rt_x == 0) {
            // 固件异常态（常伴 Sensor_ID=0xFF）：返回错误，由上层做 recovery reset
            i2c_master_bus_rm_device(dev);
            return ESP_ERR_INVALID_STATE;
        }
    }

    *out_dev = dev;
    return ESP_OK;
}

// 调试：读原始状态寄存器 0x814E（不清除）
esp_err_t gt911_raw_status(i2c_master_dev_handle_t dev, uint8_t *status)
{
    return reg_read(dev, REG_STATUS, status, 1);
}

esp_err_t gt911_read(i2c_master_dev_handle_t dev, gt911_touch_t *t)
{
    uint8_t status = 0;
    esp_err_t err = reg_read(dev, REG_STATUS, &status, 1);
    if (err != ESP_OK) {
        return err;
    }
    if (!(status & 0x80)) {
        // 参考 esp_lcd_touch_gt911：即使无数据也清一次状态，保持与官方驱动一致
        reg_write_u8(dev, REG_STATUS, 0);
        return ESP_ERR_NOT_FOUND;  // 无新数据
    }

    t->count = status & 0x0F;
    if (t->count > 0) {
        // 每个触点 8 字节：track_id, xL, xH, yL, yH, sizeL, sizeH, rsv
        uint8_t p[8] = { 0 };
        err = reg_read(dev, REG_POINT0, p, 8);
        if (err != ESP_OK) { t->count = 0; reg_write_u8(dev, REG_STATUS, 0); return err; }
        t->x = p[1] | (p[2] << 8);
        t->y = p[3] | (p[4] << 8);
        if (t->count > 5 || t->x >= 1024 || t->y >= 768) {
            t->count = 0; reg_write_u8(dev, REG_STATUS, 0); return ESP_ERR_INVALID_RESPONSE;
        }
    }
    reg_write_u8(dev, REG_STATUS, 0);  // 清状态，准备下一帧
    return ESP_OK;
}
