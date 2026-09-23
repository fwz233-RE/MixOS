#include "aw9523.h"

#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "esp_log.h"
#include "board_pins.h"
#include "mix_i2c.h"
#include "freertos/semphr.h"

static SemaphoreHandle_t s_lock;
static uint8_t s_expected_out1 = AW9523_P1_LCD_RST | AW9523_P1_TP_RST;
static uint8_t s_expected_cfg1 = 0xED;
static bool s_rebuilding;
static void aw_lock(void) { if (s_lock) xSemaphoreTakeRecursive(s_lock, portMAX_DELAY); }
static void aw_unlock(void) { if (s_lock) xSemaphoreGiveRecursive(s_lock); }

static const char *TAG = "AW9523";

/* Why this file no longer uses ESP_ERROR_CHECK
 * --------------------------------------------
 * ESP_ERROR_CHECK aborts and reboots the device. Every caller of this driver
 * already handles failure: main.c checks aw9523_init()'s return value and
 * degrades to a printed recovery hint, and aw9523_gt911_reset()'s result
 * decides whether touch is available. Aborting inside meant a single I2C NAK
 * rebooted the board before any of that code could run, so the fault tolerance
 * that had been written was unreachable.
 *
 * AW_TRY propagates the error instead. Failure here is recoverable: the health
 * check in the device task rebuilds the expander, and the UI stays usable
 * without touch.
 */
#define AW_TRY(expr)                                                        \
    do {                                                                    \
        esp_err_t aw_try_err_ = (expr);                                     \
        if (aw_try_err_ != ESP_OK) {                                        \
            ESP_LOGE(TAG, "%s failed at %s:%d: %s", #expr, __func__,        \
                     __LINE__, esp_err_to_name(aw_try_err_));               \
            return aw_try_err_;                                             \
        }                                                                   \
    } while (0)


// ---- 开机取证快照（见 aw9523.h 注释）----
const uint8_t aw9523_snap_regs[AW9523_SNAP_COUNT] = {
    AW9523_REG_INPUT_P0,  AW9523_REG_INPUT_P1,
    AW9523_REG_OUTPUT_P0, AW9523_REG_OUTPUT_P1,
    AW9523_REG_CONFIG_P0, AW9523_REG_CONFIG_P1,
    AW9523_REG_INT_P0,    AW9523_REG_INT_P1,
    AW9523_REG_GCR,       AW9523_REG_LEDMODE_P0, AW9523_REG_LEDMODE_P1,
};
static uint8_t s_snap[AW9523_SNAP_COUNT];
static bool    s_snap_valid = false;

bool aw9523_boot_snapshot(uint8_t out[AW9523_SNAP_COUNT])
{
    if (!s_snap_valid) return false;
    for (int i = 0; i < AW9523_SNAP_COUNT; i++) out[i] = s_snap[i];
    return true;
}

// INTN 与 LCD CS 共线（R82）：vsync 探测锁定后 INT 必须全屏蔽，
// 任何"重开 P0_7"的路径（reinit/健康检查）都以此为唯一事实来源。
uint8_t aw9523_int_p0_expected(void)
{
    return 0xFF; // MixOS never enables AW interrupts: INTN shares LCD CS.
}

esp_err_t aw9523_read_reg(i2c_master_dev_handle_t dev, uint8_t reg, uint8_t *val)
{
    /* Both locks are taken: the AW lock protects this driver's expected-state
     * bookkeeping, the bus lock protects the transfer against a bus reset. */
    aw_lock();
    esp_err_t err = mix_i2c_read_u8(dev, reg, val);
    aw_unlock();
    return err;
}

esp_err_t aw9523_write_reg(i2c_master_dev_handle_t dev, uint8_t reg, uint8_t val)
{
    aw_lock();
    esp_err_t err = mix_i2c_write_u8(dev, reg, val);
    if (!s_rebuilding) { // Remember intent even on failure; health task retries it.
        if (reg == AW9523_REG_OUTPUT_P1) s_expected_out1 = val;
        if (reg == AW9523_REG_CONFIG_P1) s_expected_cfg1 = val;
    }
    aw_unlock();
    return err;
}

esp_err_t aw9523_update_bits(i2c_master_dev_handle_t dev, uint8_t reg, uint8_t mask, uint8_t val)
{
    aw_lock();
    uint8_t cur;
    esp_err_t err = aw9523_read_reg(dev, reg, &cur);
    if (err == ESP_OK) {
        uint8_t next = (cur & ~mask) | (val & mask);
        if (next != cur) err = aw9523_write_reg(dev, reg, next);
    }
    aw_unlock();
    return err;
}

esp_err_t aw9523_init(i2c_master_dev_handle_t *out_dev)
{
    /* The bus handle is no longer a parameter: mix_i2c owns it, and passing a
     * second handle around only invited a driver to bypass the bus lock. */
    if (!s_lock) s_lock = xSemaphoreCreateRecursiveMutex();
    if (!s_lock) return ESP_ERR_NO_MEM;
    i2c_master_dev_handle_t dev = mix_i2c_add_device(AW9523_I2C_ADDR, I2C_STANDARD_HZ);
    if (!dev) return ESP_ERR_NOT_FOUND;

    /* From here on every early return must remove the device handle again;
     * leaking it kept the 0x5B address claimed on the bus after a failed
     * probe, so a later retry could not add it back. */
    esp_err_t err;
    #define AW_TRY_DEV(expr)                                                \
        do {                                                                \
            esp_err_t aw_dev_err_ = (expr);                                 \
            if (aw_dev_err_ != ESP_OK) {                                    \
                ESP_LOGE(TAG, "%s failed at line %d: %s", #expr, __LINE__,  \
                         esp_err_to_name(aw_dev_err_));                     \
                mix_i2c_rm_device(dev);                                     \
                return aw_dev_err_;                                         \
            }                                                               \
        } while (0)

    // ---- 自检：读芯片 ID ----
    uint8_t id = 0;
    err = aw9523_read_reg(dev, AW9523_REG_ID, &id);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "0x5B 无应答: %s（查 R72/R87、U16 供电/RSTN 上拉）", esp_err_to_name(err));
        mix_i2c_rm_device(dev);
        return err;
    }
    if (id != AW9523_CHIP_ID) {
        ESP_LOGE(TAG, "ID 寄存器 = 0x%02X，期望 0x23，芯片异常", id);
        mix_i2c_rm_device(dev);
        return ESP_ERR_INVALID_RESPONSE;
    }
    ESP_LOGI(TAG, "AW9523B OK (ID=0x23 @0x5B)");

    // ---- 取证快照：任何写入之前抢拍现场（寄存器保留上一轮运行态）----
    s_snap_valid = true;
    for (int i = 0; i < AW9523_SNAP_COUNT; i++) {
        if (aw9523_read_reg(dev, aw9523_snap_regs[i], &s_snap[i]) != ESP_OK) {
            s_snap_valid = false;
            break;
        }
    }
    if (s_snap_valid) {
        ESP_LOGI(TAG, "开机快照: IN %02X/%02X OUT %02X/%02X CFG %02X/%02X INT %02X/%02X "
                      "GCR %02X LED %02X/%02X",
                 s_snap[0], s_snap[1], s_snap[2], s_snap[3], s_snap[4], s_snap[5],
                 s_snap[6], s_snap[7], s_snap[8], s_snap[9], s_snap[10]);
    }

    // ---- 配置（不做软复位，避免其它已在用的输出瞬间跳变）----
    // 1. 屏蔽全部中断：INTN 经 R82 搭在 ESP_LCD_CS(GPIO5) 上，绝不能让它拉低
    AW_TRY_DEV(aw9523_write_reg(dev, AW9523_REG_INT_P0, 0xFF));
    AW_TRY_DEV(aw9523_write_reg(dev, AW9523_REG_INT_P1, 0xFF));
    // 2. 全部端口 GPIO 模式（非 LED 电流源）
    AW_TRY_DEV(aw9523_write_reg(dev, AW9523_REG_LEDMODE_P0, 0xFF));
    AW_TRY_DEV(aw9523_write_reg(dev, AW9523_REG_LEDMODE_P1, 0xFF));
    // 3. 绝对写入（不用 update_bits）——AW9523 不随 ESP 复位，寄存器会残留
    //    上一版固件的状态，必须强制写成确定状态。
    //    ⚠️ 只驱动真正要控的 3 根线，其余 13 脚一律输入 Hi-Z（网表逐脚核实，
    //    见 aw9523.h / CLAUDE.md 踩坑 #15）。
    //    CM_PMIC_EN(P0_2) 完全不驱动（输入 Hi-Z）：R79 上拉 = CM 上电自启，
    //    ESP 与 CM 并行启动，互不干预电源。
    //
    //    ⚠️⚠️ 写入顺序是电源安全的关键：必须【先 CONFIG 后 OUTPUT】！
    //    AW9523 上电默认全 16 脚输出高、ESP 热复位后残留上一次的输出状态。
    //    若先写 OUTPUT_P0=0x01，P0_2 此刻还是输出态，会被瞬间驱成 0 →
    //    GLOBAL_EN 打出一个低脉冲 → 刚开始启动的 CM 被掐死且 PMIC 不再自启
    //    （实测：无论怎么重新上电 Pi 都起不来，因为每次 ESP 启动都补一刀）。
    //    先写 CONFIG 把不要的脚全部转输入释放（此时输出寄存器仍是高/残留值，
    //    输入态不驱动，无任何毛刺），再写 OUTPUT 只影响留下的输出脚。
    //    （"先 OUTPUT 后 CONFIG"仅适用于上一版故意抑制 CM 上电的固件。）
    AW_TRY_DEV(aw9523_write_reg(dev, AW9523_REG_CONFIG_P0,
                                0xFF & ~AW9523_P0_MUX_SEL));                     // 0xFE
    AW_TRY_DEV(aw9523_write_reg(dev, AW9523_REG_CONFIG_P1,
                                0xFF & ~(AW9523_P1_LCD_RST | AW9523_P1_TP_RST)));// 0xED
    // 4. P0 推挽输出（默认开漏推不高 MUX_SEL）。放在 CONFIG 之后：此刻 P0 只剩
    //    P0_0 一个输出，切推挽不会波及其它脚。
    AW_TRY_DEV(aw9523_update_bits(dev, AW9523_REG_GCR, 1 << 4, 1 << 4));
    // 5. 输出电平：MUX_SEL(P0_0)=1 先切 ESP 侧做 LCD SPI 初始化 + GT911 复位；
    //    LCD_RST(P1_1)=1 / TP_RST(P1_4)=1 复位线保持释放
    AW_TRY_DEV(aw9523_write_reg(dev, AW9523_REG_OUTPUT_P0, AW9523_P0_MUX_SEL));  // 0x01
    AW_TRY_DEV(aw9523_write_reg(dev, AW9523_REG_OUTPUT_P1,
                                AW9523_P1_LCD_RST | AW9523_P1_TP_RST));          // 0x12

    // 回读全部关键寄存器，验证配置真的写进去了
    struct { uint8_t reg; const char *name; } dump[] = {
        { AW9523_REG_INPUT_P0,  "INPUT_P0 " }, { AW9523_REG_INPUT_P1,  "INPUT_P1 " },
        { AW9523_REG_OUTPUT_P0, "OUTPUT_P0" }, { AW9523_REG_OUTPUT_P1, "OUTPUT_P1" },
        { AW9523_REG_CONFIG_P0, "CONFIG_P0" }, { AW9523_REG_CONFIG_P1, "CONFIG_P1" },
        { AW9523_REG_INT_P0,    "INT_P0   " }, { AW9523_REG_INT_P1,    "INT_P1   " },
        { AW9523_REG_GCR,       "GCR      " },
    };
    for (int i = 0; i < sizeof(dump) / sizeof(dump[0]); i++) {
        uint8_t v = 0;
        esp_err_t dump_err = aw9523_read_reg(dev, dump[i].reg, &v);
        if (dump_err != ESP_OK) {
            // Printing an uninitialised v here used to make a failed read look
            // like a register that reads back as 0x00.
            ESP_LOGW(TAG, "  [0x%02X] %s = <read failed: %s>", dump[i].reg,
                     dump[i].name, esp_err_to_name(dump_err));
            continue;
        }
        ESP_LOGI(TAG, "  [0x%02X] %s = 0x%02X", dump[i].reg, dump[i].name, v);
    }
    ESP_LOGI(TAG, "MUX_SEL(P0_0)=1 已切 ESP 侧, CM_PMIC_EN(P0_2)=输入Hi-Z(CM 自启不干预), "
                  "LCD_RST(P1_1)=1, TP_RST(P1_4)=1, 其余 13 脚全输入 Hi-Z");

    #undef AW_TRY_DEV
    *out_dev = dev;
    return ESP_OK;
}

// 运行期自愈重建（2026-08-16 实翻车：碰外壳静电把 AW9523 打回默认态/挂总线，
// MUX 卡 ESP 侧回不去 Pi）。序列与 aw9523_init 相同：先 CONFIG 释放 13 个
// 输入脚（复位后全输出高，输入态释放无毛刺），再 GCR 推挽，最后 OUTPUT。
// INT_P0：vsync_mon 未锁定时保留 P0_7 使能（探测还在跑）；已锁定（Pi 出图
// 确认、探测永久关闭）则全屏蔽——INTN 与 LCD CS 共线，重开 P0_7 等于把
// I2S 音频码流当 SPI 灌进面板（2026-08-16 实翻车：运行中随机反色）。
esp_err_t aw9523_reinit(i2c_master_dev_handle_t dev, bool mux_esp_side)
{
    (void)mux_esp_side; // Permanent ESP ownership; caller cannot hand screen to Pi.
    aw_lock();
    uint8_t out1 = s_expected_out1, cfg1 = s_expected_cfg1;
    s_rebuilding = true;
    esp_err_t err = ESP_OK;
    const uint8_t seq[][2] = {
        {AW9523_REG_INT_P0, 0xFF}, {AW9523_REG_INT_P1, 0xFF},
        {AW9523_REG_LEDMODE_P0, 0xFF}, {AW9523_REG_LEDMODE_P1, 0xFF},
        {AW9523_REG_CONFIG_P0, 0xFE}, {AW9523_REG_CONFIG_P1, 0xED},
        {AW9523_REG_OUTPUT_P0, AW9523_P0_MUX_SEL},
        {AW9523_REG_OUTPUT_P1, out1}, {AW9523_REG_CONFIG_P1, cfg1},
    };
    for (unsigned i = 0; i < sizeof(seq)/sizeof(seq[0]); i++) {
        err = aw9523_write_reg(dev, seq[i][0], seq[i][1]);
        if (err != ESP_OK) break;
    }
    if (err == ESP_OK) err = aw9523_update_bits(dev, AW9523_REG_GCR, 1 << 4, 1 << 4);
    s_rebuilding = false;
    aw_unlock();
    return err;
}

bool aw9523_state_matches(i2c_master_dev_handle_t dev)
{
    aw_lock();
    uint8_t v; bool ok = true;
    const uint8_t expect[][2] = {
        {AW9523_REG_CONFIG_P0, 0xFE}, {AW9523_REG_CONFIG_P1, s_expected_cfg1},
        {AW9523_REG_OUTPUT_P0, AW9523_P0_MUX_SEL}, {AW9523_REG_OUTPUT_P1, s_expected_out1},
        {AW9523_REG_INT_P0, 0xFF}, {AW9523_REG_INT_P1, 0xFF},
        {AW9523_REG_LEDMODE_P0, 0xFF}, {AW9523_REG_LEDMODE_P1, 0xFF}
    };
    for (unsigned i=0;i<sizeof(expect)/sizeof(expect[0]);i++) {
        if (aw9523_read_reg(dev,expect[i][0],&v)!=ESP_OK || v!=expect[i][1]) {ok=false;break;}
    }
    if(ok && (aw9523_read_reg(dev,AW9523_REG_GCR,&v)!=ESP_OK || !(v & (1<<4))))ok=false;
    aw_unlock();return ok;
}

// GT911 复位（INT 拉低贯穿 → 地址 0x5D）。
// 实测：GT911 固件启动阶段 INT 必须为低；INT 悬空被 R20 拉高时固件进异常态
// （Sensor_ID=0xFF、配置区全 0、拒绝采纳配置）。INT 只在复位期间短暂驱动，
// 结束立即还回输入交还 GT911。
esp_err_t aw9523_gt911_reset(i2c_master_dev_handle_t dev)
{
    /* Serialize the full pin sequence against expander health recovery.
     * Always restore RST high and INT input, even after an early I2C error. */
    aw_lock();
    esp_err_t err = aw9523_update_bits(dev, AW9523_REG_OUTPUT_P1,
                                      AW9523_P1_TP_RST | AW9523_P1_TP_INT, 0);
    if (err == ESP_OK)
        err = aw9523_update_bits(dev, AW9523_REG_CONFIG_P1, AW9523_P1_TP_INT, 0);
    if (err == ESP_OK) vTaskDelay(pdMS_TO_TICKS(20));
    esp_err_t rst = aw9523_update_bits(dev, AW9523_REG_OUTPUT_P1,
                                      AW9523_P1_TP_RST, AW9523_P1_TP_RST);
    if (err == ESP_OK) err = rst;
    if (err == ESP_OK) vTaskDelay(pdMS_TO_TICKS(50));
    esp_err_t release = aw9523_update_bits(dev, AW9523_REG_CONFIG_P1,
                                          AW9523_P1_TP_INT, AW9523_P1_TP_INT);
    /* A failed read-modify-write still leaves a safe desired state for the
     * expander health task to restore, rather than keeping reset asserted. */
    s_expected_out1 |= AW9523_P1_TP_RST;
    s_expected_cfg1 |= AW9523_P1_TP_INT;
    if (err == ESP_OK) err = release;
    vTaskDelay(pdMS_TO_TICKS(120));
    aw_unlock();
    return err;
}

