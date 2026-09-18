// ES8389 codec 初始化（USB UAC 放音 + 录音全双工路径）
// 关键：GPIO47/48 先在 LCD SPI init 阶段当 SPI 用，这里重配成 I2S DOUT/DIN。
// 放音：UAC 收到的 PCM 由 main.c 的 uac_output_cb 经 audio_write 写进 codec；
// 录音：main.c 的 uac_input_cb 经 audio_read 从 codec 读 PCM
//       （双 MEMS 麦 → ES8389 ADC → I2S rx）。
//
// See audio.h for the concurrency contract this file implements.
#include <stdio.h>
#include <string.h>
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "driver/i2s_std.h"
#include "esp_codec_dev.h"
#include "esp_codec_dev_defaults.h"
#include "esp_log.h"
#include "esp_check.h"
#include "esp_timer.h"
#include "board_pins.h"
#include "audio.h"
#include "mix_i2c.h"

static const char *TAG = "AUDIO";
static i2c_master_bus_handle_t s_bus;
static i2s_chan_handle_t      s_tx;
static i2s_chan_handle_t      s_rx;
static esp_codec_dev_handle_t s_codec_out;
static esp_codec_dev_handle_t s_codec_in;
static const audio_codec_ctrl_if_t *s_ctrl_if;   // 直写寄存器用（DAC 声道互换）
/* The other three interfaces es8389_setup() builds. Kept because they have to
 * be released on a rebuild: the first version of audio_recover() dropped them
 * on the floor, which leaked them and left the replacement codec holding a
 * data interface bound to I2S channels that had just been deleted. */
static const audio_codec_data_if_t *s_data_if;
static const audio_codec_gpio_if_t *s_gpio_if;
static const audio_codec_if_t      *s_codec_if;
static SemaphoreHandle_t      s_lock;

/* Capture health. See the block comment above these functions in audio.h.
 *
 * s_dead_since_ms is the moment the current unbroken run of dead-line captures
 * began, or 0 when the last capture carried data. Storing the start of the run
 * rather than a count of buffers keeps the two-second judgement independent of
 * how fast the host happens to be recording.
 *
 * s_attempt_ms rate-limits rebuilds. Without it a rebuild that fails leaves no
 * codec, every read then fails, and the device spends every second tearing
 * down and rebuilding nothing.
 */
#define AUDIO_DEAD_SPAN     2      /* == DEAD_SPAN in linux/apps/audio.py */
#define AUDIO_DEAD_HOLD_MS  2000
#define AUDIO_RETRY_MS      30000

static volatile uint32_t s_dead_since_ms;
static volatile uint32_t s_attempt_ms;
static volatile uint32_t s_recoveries;

/* True while some task is building or tearing down the codec.
 *
 * s_lock alone is not enough. It guards the published handles, which are only
 * assigned at the very end of a successful bring-up, so for the whole duration
 * of audio_start the codec looks absent to everyone else. audio_maintain reads
 * that as "no codec at all" and calls audio_recover, which tears down the I2S
 * channels audio_start is still initialising - on real hardware that landed as
 * "i2s_channel_disable: the channel has not been enabled yet" in the middle of
 * the receive channel's own init log, followed by an assert in
 * spinlock_acquire once the freed channel was used again.
 *
 * This flag marks the whole build/teardown sequence, not just the handle
 * publication, so the two paths can never overlap. Read and written under
 * s_lock; see audio_claim_transition.
 */
static bool s_transition;

static uint32_t now_ms(void)
{
    return (uint32_t)(esp_timer_get_time() / 1000);
}

/* Why not ESP_ERROR_CHECK
 * -----------------------
 * It aborts and reboots. Audio is optional on this device: main.c logs
 * "local UI remains available" when the codec does not come up, and that code
 * could never run while a failed I2S channel allocation rebooted the board.
 * Every step now propagates instead.
 */
#define AUDIO_TRY(expr)                                                     \
    do {                                                                    \
        esp_err_t audio_try_err_ = (expr);                                  \
        if (audio_try_err_ != ESP_OK) {                                     \
            ESP_LOGE(TAG, "%s failed at %s:%d: %s", #expr, __func__,        \
                     __LINE__, esp_err_to_name(audio_try_err_));            \
            return audio_try_err_;                                          \
        }                                                                   \
    } while (0)

/* Why not assert()
 * ----------------
 * assert() compiles away under CONFIG_COMPILER_OPTIMIZATION_ASSERTIONS_DISABLE.
 * The previous code asserted each interface pointer and then stored it, so in a
 * build with assertions disabled a NULL control interface was saved into
 * s_ctrl_if and dereferenced later in audio_set_dac_lr_swap.
 */
#define AUDIO_REQUIRE(ptr, what)                                            \
    do {                                                                    \
        if (!(ptr)) {                                                       \
            ESP_LOGE(TAG, "%s unavailable", (what));                        \
            return ESP_ERR_NO_MEM;                                          \
        }                                                                   \
    } while (0)

static bool audio_lock(void)
{
    return s_lock && xSemaphoreTake(s_lock, portMAX_DELAY) == pdTRUE;
}

static void audio_unlock(void)
{
    if (s_lock) xSemaphoreGive(s_lock);
}

/* Take exclusive ownership of the codec's construction state.
 *
 * Returns false when another task is already building or tearing the codec
 * down. A caller that gets false must not touch s_tx, s_rx or any of the
 * codec interface pointers: they belong to the task that holds the claim and
 * may be freed at any moment.
 */
static bool audio_claim_transition(void)
{
    if (!audio_lock()) return false;
    if (s_transition) { audio_unlock(); return false; }
    s_transition = true;
    audio_unlock();
    return true;
}

static void audio_release_transition(void)
{
    if (audio_lock()) { s_transition = false; audio_unlock(); }
}

static bool audio_transition_in_flight(void)
{
    bool busy;
    if (!audio_lock()) return true;   /* no lock yet: assume unsafe */
    busy = s_transition;
    audio_unlock();
    return busy;
}

static esp_err_t i2s_setup(int hz)
{
    // 全双工：tx/rx 同一 port（时钟共享，同 48k/16bit/立体声）
    i2s_chan_config_t chan = I2S_CHANNEL_DEFAULT_CONFIG(I2S_NUM_0, I2S_ROLE_MASTER);
    chan.auto_clear = true;
    AUDIO_TRY(i2s_new_channel(&chan, &s_tx, &s_rx));
    i2s_std_config_t std = {
        .clk_cfg  = I2S_STD_CLK_DEFAULT_CONFIG(hz),
        .slot_cfg = I2S_STD_PHILIPS_SLOT_DEFAULT_CONFIG(I2S_DATA_BIT_WIDTH_16BIT, I2S_SLOT_MODE_STEREO),
        .gpio_cfg = {
            .mclk = PIN_I2S_MCLK, .bclk = PIN_I2S_BCLK, .ws = PIN_I2S_LRCK,
            .dout = PIN_I2S_DOUT, .din  = PIN_I2S_DIN,
            .invert_flags = { .mclk_inv = false, .bclk_inv = false, .ws_inv = false },
        },
    };
    std.clk_cfg.mclk_multiple = I2S_MCLK_MULTIPLE_256;
    AUDIO_TRY(i2s_channel_init_std_mode(s_tx, &std));
    AUDIO_TRY(i2s_channel_init_std_mode(s_rx, &std));
    AUDIO_TRY(i2s_channel_enable(s_tx));
    AUDIO_TRY(i2s_channel_enable(s_rx));
    return ESP_OK;
}

static void i2s_teardown(void)
{
    if (s_tx) { i2s_channel_disable(s_tx); i2s_del_channel(s_tx); s_tx = NULL; }
    if (s_rx) { i2s_channel_disable(s_rx); i2s_del_channel(s_rx); s_rx = NULL; }
}

static esp_err_t es8389_setup(int hz, int ch, uint8_t es7bit)
{
    // esp_codec_dev 把 addr 右移一位当 7-bit（audio_codec_ctrl_i2c.c:52），故 addr = 7bit<<1
    audio_codec_i2c_cfg_t ctrl = { .port = 0, .addr = es7bit << 1, .bus_handle = s_bus };
    const audio_codec_ctrl_if_t *ctrl_if = audio_codec_new_i2c_ctrl(&ctrl);
    AUDIO_REQUIRE(ctrl_if, "codec I2C control interface");
    audio_codec_i2s_cfg_t data = { .port = 0, .tx_handle = s_tx, .rx_handle = s_rx };
    const audio_codec_data_if_t *data_if = audio_codec_new_i2s_data(&data);
    AUDIO_REQUIRE(data_if, "codec I2S data interface");
    const audio_codec_gpio_if_t *gpio_if = audio_codec_new_gpio();
    AUDIO_REQUIRE(gpio_if, "codec GPIO interface");

    es8389_codec_cfg_t cfg = {
        .ctrl_if = ctrl_if, .gpio_if = gpio_if,
        .codec_mode = ESP_CODEC_DEV_WORK_MODE_BOTH,   // DAC 放音 + ADC 录音（双 MEMS 麦）
        .pa_pin = -1, .pa_reverted = false,
        .master_mode = false,        // ES8389 作 I2S 从
        .use_mclk = false,           // ★ MCLK 未接：codec 从 BCLK 派生内部时钟
        .digital_mic = false, .invert_mclk = false, .invert_sclk = false,
        .hw_gain = { .pa_voltage = 0.0, .codec_dac_voltage = 3.3 },
        // ★ no_dac_ref 必须为 true：false 时驱动开 AEC 参考模式，ADC 数字输出
        //   右 slot 被替换成 DAC 回采参考，MIC2 信号进不了 I2S。
        //   完整分析见 docs/AUDIO_HARDWARE_NOTES.md。
        .no_dac_ref = true,
        // ★ 板 #1 专用 workaround（右声道模拟前端个体故障）。板 #2 必须为 false。
        .adc2_copy_left = BOARD1_ADC2_DEAD_WORKAROUND,
        .mclk_div = 256,
    };
    const audio_codec_if_t *codec_if = es8389_codec_new(&cfg);
    AUDIO_REQUIRE(codec_if, "ES8389 codec instance");

    // ★ 单个 IN_OUT 设备：拆成 OUT/IN 双设备后实测 ADC 读出全零。
    esp_codec_dev_cfg_t dev = { .dev_type = ESP_CODEC_DEV_TYPE_IN_OUT, .codec_if = codec_if, .data_if = data_if };
    esp_codec_dev_handle_t handle = esp_codec_dev_new(&dev);
    AUDIO_REQUIRE(handle, "codec device");

    esp_codec_dev_sample_info_t s = {
        .bits_per_sample = 16, .channel = ch, .channel_mask = 0x03,
        .sample_rate = hz, .mclk_multiple = 256,
    };
    if (esp_codec_dev_open(handle, &s) != ESP_CODEC_DEV_OK) {
        ESP_LOGE(TAG, "esp_codec_dev_open failed at %dHz %dch", hz, ch);
        esp_codec_dev_delete(handle);
        return ESP_FAIL;
    }

    /* Publish the handle only once it is fully open, so another task can never
     * observe a half-initialised device through audio_ready(). */
    s_ctrl_if = ctrl_if;
    s_data_if = data_if;
    s_gpio_if = gpio_if;
    s_codec_if = codec_if;
    s_codec_out = handle;
    s_codec_in = handle;   // 同一句柄，读写共用

    esp_codec_dev_set_out_vol(s_codec_out, 60);
    /* 麦克风 PGA 增益（dB）。ES8389 支持到 36.5 dB（es8389_reg.h:
     * ES8389_MIC_GAIN_36_5DB），驱动把请求值映射到最近的档位，>=36 落到最高档。
     *
     * 曾长期设为 24.0（→ 24.5 dB 档），理由是把设备贴在自己扬声器上会削波。
     * 2026-09-14 在 typixdeck 上按真实语音重新测量，结论相反：语音录音峰值只有
     * 0.06~0.08 满量程，约 22 dB 余量白白浪费，而 Moonshine 对过轻的音频返回空
     * 字符串而不是报错——屏幕上看不出区别，正是"语音转文字完全没反应"的成因。
     *
     * 提到 36.5 dB 后峰值约 0.25~0.33，仍远离削波，而信噪比直接改善 12 dB。
     * 这是模拟增益，改善的是 ADC 之前的信噪比，Linux 侧的软件归一化
     * （linux/apps/audio.py for_recognition）只能补幅度补不了信噪比，两者互补。 */
    esp_codec_dev_set_in_gain(s_codec_in, 36.0);
    ESP_LOGI(TAG, "ES8389 @7bit 0x%02X: %dHz %dch (use_mclk=false, slave, IN_OUT single dev, no_dac_ref=1)", es7bit, hz, ch);
    return ESP_OK;
}

/* Probe the codec's address and bring I2S and the ES8389 up.
 *
 * Split out of audio_start so that audio_recover can repeat exactly the
 * start-up sequence rather than an abbreviated version of it that would drift
 * away from it over time. Callers must hold the transition claim; that is what
 * keeps a concurrent audio_maintain from tearing these channels down while
 * they are still being initialised.
 */
static esp_err_t audio_bring_up(void)
{
    // ES8389 AD1 脚悬空 → 地址在 7-bit 0x10/0x12 间漂，逐个探
    // 探测走 mix_i2c，与其它任务的传输和总线复位串行。
    // 注意：之后 esp_codec_dev 直接拿 bus_handle 自己发起传输，不经过本项目的
    // 总线锁；它只在这里集中写寄存器，与健康检查的复位窗口错开。
    static const uint8_t cand[] = ES8389_I2C_ADDR_CANDIDATES;
    uint8_t es7 = 0;
    for (int i = 0; i < (int)(sizeof(cand)/sizeof(cand[0])); i++) {
        if (mix_i2c_probe(cand[i], I2C_PROBE_TIMEOUT_MS) == ESP_OK) { es7 = cand[i]; break; }
    }
    if (!es7) {
        ESP_LOGE(TAG, "ES8389 0x10-0x13 全无应答——查 DAC_3V3 电源");
        return ESP_ERR_NOT_FOUND;
    }
    ESP_LOGI(TAG, "ES8389 真实 7-bit 地址 = 0x%02X", es7);

    esp_err_t err = i2s_setup(UAC_SAMPLE_RATE);
    if (err != ESP_OK) { i2s_teardown(); return err; }

    err = es8389_setup(UAC_SAMPLE_RATE, UAC_CHANNELS, es7);
    if (err != ESP_OK) i2s_teardown();
    return err;
}

esp_err_t audio_start(i2c_master_bus_handle_t bus)
{
    s_bus = bus;
    if (!s_lock) s_lock = xSemaphoreCreateMutex();
    if (!s_lock) {
        ESP_LOGE(TAG, "codec mutex allocation failed");
        return ESP_ERR_NO_MEM;
    }
    /* Hold the claim across the whole bring-up. audio_maintain runs from a
     * different task and would otherwise see the not-yet-published handles as
     * a missing codec and rebuild on top of this one. */
    if (!audio_claim_transition()) {
        ESP_LOGW(TAG, "codec bring-up already in flight");
        return ESP_ERR_INVALID_STATE;
    }
    esp_err_t err = audio_bring_up();
    audio_release_transition();
    return err;
}

bool audio_ready(void)
{
    return s_codec_out != NULL;
}

esp_err_t audio_write(const void *samples, size_t bytes)
{
    if (!audio_lock()) return ESP_ERR_INVALID_STATE;
    /* Re-read the handle under the lock. It used to be read outside, which was
     * safe only while nothing ever closed the codec; audio_recover does, and a
     * write that had already passed the outside check would have carried a
     * freed handle into esp_codec_dev_write. */
    esp_codec_dev_handle_t handle = s_codec_out;
    if (!handle) { audio_unlock(); return ESP_ERR_INVALID_STATE; }
    int rc = esp_codec_dev_write(handle, (void *)samples, bytes);
    audio_unlock();
    return rc == ESP_CODEC_DEV_OK ? ESP_OK : ESP_FAIL;
}

esp_err_t audio_read(void *samples, size_t bytes)
{
    if (!audio_lock()) {
        memset(samples, 0, bytes);
        return ESP_ERR_INVALID_STATE;
    }
    esp_codec_dev_handle_t handle = s_codec_in;   /* under the lock; see audio_write */
    if (!handle) {
        audio_unlock();
        memset(samples, 0, bytes);
        return ESP_ERR_INVALID_STATE;
    }
    int rc = esp_codec_dev_read(handle, samples, bytes);
    audio_unlock();
    if (rc != ESP_CODEC_DEV_OK) {
        memset(samples, 0, bytes);
        return ESP_FAIL;
    }
    return ESP_OK;
}

/* Does this buffer span so few counts that nothing can be driving the line.
 *
 * A live capture carries the room even in silence: measured on this board a
 * quiet room spans some hundreds of counts. Two counts is the allowance for a
 * line that is genuinely not moving, and it is the same number
 * linux/apps/audio.py uses, so the device and the interfaces cannot disagree
 * about what a dead microphone is.
 */
static bool buffer_is_dead(const int16_t *samples, size_t count)
{
    if (count == 0) return false;
    int16_t lo = samples[0], hi = samples[0];
    for (size_t i = 1; i < count; i++) {
        if (samples[i] < lo) lo = samples[i];
        if (samples[i] > hi) hi = samples[i];
    }
    return (int32_t)hi - (int32_t)lo <= AUDIO_DEAD_SPAN;
}

static void account_capture(const void *samples, size_t bytes)
{
    size_t count = bytes / sizeof(int16_t);
    if (count == 0) return;
    if (!buffer_is_dead((const int16_t *)samples, count)) {
        s_dead_since_ms = 0;
        return;
    }
    if (s_dead_since_ms == 0) {
        uint32_t now = now_ms();
        s_dead_since_ms = now ? now : 1;   /* 0 is the "not dead" value */
    }
}

void audio_note_capture(const void *samples, size_t bytes)
{
    account_capture(samples, bytes);
}

bool audio_capture_dead(void)
{
    uint32_t since = s_dead_since_ms;
    if (!since) return false;
    return (uint32_t)(now_ms() - since) >= AUDIO_DEAD_HOLD_MS;
}

uint32_t audio_recovery_count(void)
{
    return s_recoveries;
}

/* Release everything es8389_setup() built, in the reverse order it built it.
 * Called with no other task inside the codec; see audio_recover. */
static void codec_teardown(esp_codec_dev_handle_t handle)
{
    if (handle) {
        esp_codec_dev_close(handle);
        esp_codec_dev_delete(handle);
    }
    /* Read into locals first: these are the objects the codec was made of, and
     * releasing them out of order or twice is worse than leaking them. */
    const audio_codec_if_t      *codec_if = s_codec_if;
    const audio_codec_data_if_t *data_if  = s_data_if;
    const audio_codec_ctrl_if_t *ctrl_if  = s_ctrl_if;
    const audio_codec_gpio_if_t *gpio_if  = s_gpio_if;
    s_codec_if = NULL; s_data_if = NULL; s_ctrl_if = NULL; s_gpio_if = NULL;

    if (codec_if) audio_codec_delete_codec_if(codec_if);
    if (data_if)  audio_codec_delete_data_if(data_if);
    if (ctrl_if)  audio_codec_delete_ctrl_if(ctrl_if);
    if (gpio_if)  audio_codec_delete_gpio_if(gpio_if);
}

esp_err_t audio_recover(void)
{
    if (!s_lock) return ESP_ERR_INVALID_STATE;
    /* Never rebuild on top of a bring-up that is still running. */
    if (!audio_claim_transition()) return ESP_ERR_INVALID_STATE;
    if (!audio_lock()) { audio_release_transition(); return ESP_ERR_INVALID_STATE; }
    /* Unpublish first, then release outside the lock. Every entry point takes
     * this lock and re-reads the handle under it, so once this critical
     * section ends no task can still be inside esp_codec_dev_*, and captures
     * taken meanwhile are zero-filled by audio_read's own failure path. */
    esp_codec_dev_handle_t handle = s_codec_out;
    s_codec_out = NULL;
    s_codec_in = NULL;
    audio_unlock();

    codec_teardown(handle);
    i2s_teardown();

    esp_err_t err = audio_bring_up();
    /* Cleared either way. On success the line is live again. On failure the run
     * must restart from the next capture that was actually read, because a
     * device with no codec cannot produce one and would otherwise be judged
     * dead forever by captures this module zero-filled itself. */
    s_dead_since_ms = 0;
    if (err == ESP_OK) {
        s_recoveries++;
        ESP_LOGW(TAG, "codec rebuilt after a dead capture line (recovery #%u)",
                 (unsigned)s_recoveries);
    } else {
        ESP_LOGE(TAG, "codec rebuild failed: %s", esp_err_to_name(err));
    }
    audio_release_transition();
    return err;
}

void audio_maintain(void)
{
    /* A bring-up or rebuild is already running in another task. The channels
     * and interface pointers belong to it; leave them alone. */
    if (audio_transition_in_flight()) return;

    uint32_t now = now_ms();
    uint32_t last = s_attempt_ms;
    bool due = !last || (uint32_t)(now - last) >= AUDIO_RETRY_MS;

    if (s_codec_in) {
        if (!audio_capture_dead()) return;
        if (!due) return;            /* already tried recently; let it settle */
    } else {
        /* No codec at all: either it never came up or a rebuild failed. Retry
         * on the slow timer only - this is the path that used to run every
         * second and keep the device permanently broken. */
        if (!due) return;
    }
    s_attempt_ms = now ? now : 1;
    audio_recover();
}

esp_err_t audio_set_volume(int percent)
{
    if (!s_codec_out) return ESP_ERR_INVALID_STATE;
    if (percent < 0) percent = 0;
    if (percent > 100) percent = 100;
    if (!audio_lock()) return ESP_ERR_INVALID_STATE;
    int rc = esp_codec_dev_set_out_vol(s_codec_out, percent);
    audio_unlock();
    return rc == ESP_CODEC_DEV_OK ? ESP_OK : ESP_FAIL;
}

esp_err_t audio_set_mute(bool muted)
{
    if (!s_codec_out) return ESP_ERR_INVALID_STATE;
    if (!audio_lock()) return ESP_ERR_INVALID_STATE;
    int rc = esp_codec_dev_set_out_mute(s_codec_out, muted);
    audio_unlock();
    return rc == ESP_CODEC_DEV_OK ? ESP_OK : ESP_FAIL;
}

// DAC 左右声道数字互换：REG0x44 (DAC MIX CONTROL) bit5=DAC2→DAC1、
// bit4=DAC1→DAC2，两位同置 0x30 即完整 L/R 互换（ES8389_DS Rev1.0）。
// 用途：本板喇叭链路左右反接，外放时互换、插耳机时恢复。
// 硬件分析见 docs/AUDIO_HARDWARE_NOTES.md。
esp_err_t audio_set_dac_lr_swap(bool swap)
{
    if (!s_ctrl_if) return ESP_ERR_INVALID_STATE;
    if (!audio_lock()) return ESP_ERR_INVALID_STATE;
    esp_err_t result = ESP_OK;
    uint8_t v = 0;
    /* Read-modify-write under the same lock as every other codec access: a
     * concurrent volume change used to be able to land between the read and
     * the write, and this function would then restore the stale register. */
    if (s_ctrl_if->read_reg(s_ctrl_if, 0x44, 1, &v, 1) != 0) {
        result = ESP_FAIL;
    } else {
        uint8_t nv = swap ? (uint8_t)(v | 0x30) : (uint8_t)(v & ~0x30);
        if (nv != v) {
            if (s_ctrl_if->write_reg(s_ctrl_if, 0x44, 1, &nv, 1) != 0) result = ESP_FAIL;
            else ESP_LOGI(TAG, "DAC L/R %s (REG0x44: 0x%02X -> 0x%02X)",
                          swap ? "互换" : "正常", v, nv);
        }
    }
    audio_unlock();
    return result;
}
