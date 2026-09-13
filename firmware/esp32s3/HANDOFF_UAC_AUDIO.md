# 交接文档：ESP32-S3 USB UAC+CDC 声卡固件（卡在 UAC 音频 EP 不收数据）

> ✅ **已解决（2026-08-12）**：根因不在固件，是 **CM4 主机侧 dwc2 驱动的
> split-ISO OUT 调度问题**。macOS 走同一颗 hub 出声完美 → 锁定主机；
> `[cm4]` 段改 `otg_mode=1`（BCM2711 内置 XHCI）后 Pi 稳定出声。
> 详见上游项目（本仓库之外）的 `cm4_dwc2_uac_no_audio_xhci_fix_2026-08` 记录和 CLAUDE.md 踩坑 #17。
> 固件现为纯 UAC（non-AS_PART，commit `bb87ded`），CDC 魔串待加回。
> 以下为历史排查记录，其中"hub TT 嫌疑"的方向已被证伪。

> 接手者：Fable5。这份文档汇总了**当前状态 + 全部历史踩坑 + 未解问题 + 关键文件/命令**，可直接上手无需重新摸索。

---

## TL;DR（当前卡点）

TypixDeck 0720 板上 **ESP32-S3-PICO-1 ↔ ES8389 codec** 的 USB UAC 声卡固件：
- ✅ UAC2+CDC composite **枚举成功**，主机认成声卡 + CDC 串口。
- ✅ CDC 魔串 `REBOOT_TO_BOOT_MODE` → 软件重启进下载模式（**plan B 闭环验证过**）。
- ✅ ES8389 codec + I2S 通路**确认能出声**（用 MP3 固件 `firmware/typixdeck_esp32s3_lcd_mp3/` 验证过）。
- ❌ **UAC 放音没声音**：主机 `stream0 Status: Running`（在发数据），但 ESP 的 `uac_output_cb` **不触发**（收不到 PCM）。怀疑是 **ESP32-S3（Full-Speed）经板上 USB hub（HS, FE2.1）的 TT 翻译器 + AS_PART 自定义描述符** 导致 audio EP 没正确 arm。

---

## 项目上下文

- **板子**：TypixDeck 0720 打样板（CM5 Lite + ESP32-S3-PICO-1 协处理器）。网表权威来源：`hardware/TypixDeck_0720/Netlist_PCB_TypixNode_1_2026-07-20.tel`。
- **目标固件**：`firmware/typixdeck_esp32s3_lcd_init_touch_gui_uac_cdc/`
  - LCD（JD9168S SPI 初始化 + RGB DPI + GT911 触摸 + SW3 BOOT 键切换显示源 + INA219 遥测页）
  - ES8389 codec 初始化（**无 MP3**，音频源是 USB UAC）
  - USB **UAC2 speaker + CDC ACM** composite
  - CDC 收 `REBOOT_TO_BOOT_MODE` 魔串 → 写 RTC `FORCE_DOWNLOAD_BOOT` + `esp_restart()` → 进下载模式
- **编译**：ESP-IDF **v5.4.2**（`~/esp/esp-idf`），目标 esp32s3，Flash 8MB，PSRAM 八线 120MHz。
- **刷机**：macOS 编译，scp 到 **Pi（`pi@192.168.3.84`，`/dev/ttyACM0`）**，用 `~/.venv/esptool/bin/esptool`（v5.3.1，Pi 系统的 esptool 坏的——缺 stub json）。Mac 走 USB hub 刷会中途掉，必须走 Pi。

---

## ES8389 音频通路（已验证能出声）

这部分**没问题**，MP3 固件验证过耳机能响。UAC 固件复用同一套：

| 信号 | ESP32-S3 GPIO | ES8389 脚 | 备注 |
|---|---|---|---|
| I2C SDA/SCL | 6 / 7 | — | 主总线，AW9523/INA219/IMU 等共用 |
| I2S BCLK | 45 | 8 | U57.51 |
| I2S LRCK | 46 | 10 | U57.52 |
| I2S DOUT（放音）| 47 | 9 (DACDAT) | **与 LCD SPI SCLK 共脚**（R50 0Ω 复用），LCD init 后切 I2S |
| I2S DIN（录音）| 48 | 11 (ADCDAT) | 与 LCD MOSI 共脚 |
| I2S MCLK | **未接** | 4（只到测试点 TP6）| `use_mclk=false`，codec 从 BCLK 派生 |

**ES8389 关键坑（已解决，固件里都处理了）**：
1. **I2C 地址 0x20 不是 0x10**：CE(pin1)→R147→AGND，但 **AD1 脚悬空**，地址在 7-bit **0x10/0x12 间漂**（受总线噪声）。固件运行时探测 `{0x10,0x11,0x12,0x13}` 用真实应答的那个。
2. **esp_codec_dev 把 `.addr` 右移一位当 7-bit**（`audio_codec_ctrl_i2c.c:52`：`.device_address = (i2c_cfg->addr >> 1)`）。所以给 codec 的 `addr = 实际7-bit << 1`。
3. **`use_mclk=false` 模式采样率限死** `{8000,16000,44100,48000,96000}`（驱动 coeff 表 Ratio=64 路径只覆盖这五档；32k/24k 不行）。
4. **DAC_3V3_EN（AW9523 P1_0）必须显式推挽驱高**——R84 100kΩ 上拉对 U31 负载开关 EN 不够稳 → ES8389 欠压不响应 I2C。
5. AW9523 IO 扩展器**只能动音频电源脚**，P0 的 CM 电源/触摸/键盘脚碰了会背馈/掐死 CM（CLAUDE.md 踩坑 #15）。
6. `audio_start + uac_device_init` **必须放独立 16KB 任务**（app_main 8KB 栈会溢出崩溃重启循环）。

---

## USB UAC+CDC composite 架构

用 `espressif/usb_device_uac` 组件（vendored 在 `components/usb_device_uac/`，版本 1.3.1）的 **AS_PART 模式**：

- `CONFIG_USB_DEVICE_UAC_AS_PART=y` → 组件只出 UAC 驱动逻辑，**app 自带 `main/tusb_config.h` + `main/usb_descriptors.c`**。
- `main/CMakeLists.txt` 把 main/ + 组件的 `tusb_uac/` 喂给 tinyusb lib（mirror 组件 non-AS_PART 的逻辑），`usb_descriptors.c` 编进 tinyusb。
- 描述符布局：`[0] Audio Control + [1] Audio Streaming + [2] CDC Comm + [3] CDC Data`。CDC 接口号跟在 `NUM_INTERFACES`（uac_descriptors.h，spk-only=2）后，避免和 UAC AS 接口撞车。
- `uac_device_config_t` 在 AS_PART 模式**必须填 `.spk_itf_num=1`**（AS 接口号）+ `.skip_tinyusb_init=false`。
- VID/PID/字符串在 `usb_descriptors.c` 里**硬编码**（AS_PART 模式 `CONFIG_UAC_TUSB_*` Kconfig 不暴露）。

**ES8389 由 UAC output_cb 喂数据**：
```c
static esp_err_t uac_output_cb(uint8_t *buf, size_t len, void *ctx) {
    esp_codec_dev_write(audio_codec_handle(), buf, len);  // 主机 PCM → I2S → ES8389
    return ESP_OK;
}
```

**CDC plan B（验证过）**：
```c
// tud_cdc_rx_cb 扫描行，命中 "REBOOT_TO_BOOT_MODE" 就：
REG_WRITE(RTC_CNTL_OPTION1_REG, RTC_CNTL_FORCE_DOWNLOAD_BOOT);
esp_restart();   // ROM 复位后进下载模式，esptool 无按钮可刷
```
（借鉴上游项目 cm3_usb_wifi_dongle 的 `CLI_Commands.c` download 命令，该文件不在本仓库内。S3 的 USB PHY 被 tinyusb 占了 → USB-Serial-JTAG 没了 → esptool 自动复位不了 → 这个魔串是唯一的软件刷机口。）

---

## ⚠️ 当前未解问题：UAC audio EP 不收数据

### 症状
- 主机（Pi Linux）`/proc/asound/card2/stream0`：
  ```
  Status: Running              ← 主机在发数据
  Interface = 1, Altset = 1, Format: S16_LE, Channels: 2, Rates: 48000
  Endpoint: 0x01 (1 OUT) (ASYNC), Sync Endpoint: 0x81 (1 IN)
  Momentary freq = 48013 Hz
  Feedback Format = 18.14      ← ⚠️ 异常（FS 应该是 10.14）
  ```
- 但 ESP 的 `uac_output_cb` **不触发**（CDC 里加的 `FIRST PCM` 日志不出现，`esp_codec_dev_write` 没被调）→ 没数据进 codec → 没声音。
- 耳机偶尔"咔哒/咚"一声 = codec 对流启动有反应（模拟通路活的），但没持续数据。

### 关键线索
1. **UAC 经 USB hub**（见下节），FS iso 通过 HS hub 的 TT，时序敏感、偶发。
2. **`cdc_printf` 在 set_itf 回调里"意外"能让 audio 工作**：最初调试时在 `tud_audio_set_itf_cb`（tinyusb 任务里）加了 `cdc_printf`（调 `tud_cdc_write_flush`），那一次 VLC 能连续出声（CDC 日志显示 `output_cb` 跑了 13600 次）。删掉它（managed 干净版）→ 不出声。**但**那行 `cdc_printf` 又会引发 **tinyusb 重入死锁**（在 tinyusb 任务里调 CDC API）→ 控制传输全 STALL（`usb_set_interface failed -32`, `cannot set freq 48000 err -32`）→ 固件卡死（CDC `/dev/ttyACM0` 报 I/O error）。
   - 即：**那行 cdc_printf 既是 audio EP 的"启动药"、又是死锁源**。推测它的副作用（`tud_cdc_write_flush` 触发 tinyusb 处理端点）顺带把 audio OUT EP arm 上了；没了它 tinyusb 不 arm audio EP → 收不到 iso 数据。
3. **`Feedback Format = 18.14` 异常**：FS 异步反馈应是 10.14（3 字节）。已开 `CONFIG_UAC_SUPPORT_MACOS=y`（做 FS 反馈格式转换），但 ALSA 报 18.14，存疑。
4. **hw:2,0 直推（绕开 PipeWire）从来不出 `FIRST PCM`**——但这条路径可能本身就不代表 PipeWire 路径，不能作为判据。

### USB hub 拓扑（重点怀疑对象）
```
Pi root_hub (480M, HS)
  └─ Dev 002 Hub (480M, 6-port)        ← 板上 + Pi 侧的 hub
      └─ Dev 011 ESP32-S3 (12M, FS)    ← UAC+CDC，全速设备
```
**ESP32-S3 是 Full-Speed（12M），挂在一个 High-Speed（480M）hub 下面**。UAC 用的是**等时传输（iso）+ 异步反馈端点**——USB 里对时序最敏感的传输类型。**FS iso 通过 HS hub 的事务翻译器（TT）** 是出了名的脆，小到几微秒的时序差异就能让 iso 包丢失/错位。这能解释：
- **偶发性**（"之前好的"是某个时序窗口运气好）。
- **cdc_printf 改变时序就能翻盘**（加了处理延迟，恰好踩进工作窗口）。
- 板上 hub（U58，FE2.1）是廉价 USB 2.0 hub，TT 处理 iso 可能尤其差。

板上 hub **没法绕过**（ESP USB 物理上就接在 hub 下游，没直连主机的口）。

---

## 历史踩坑全记录（已解决）

| # | 问题 | 根因 | 解法 |
|---|---|---|---|
| 1 | Mac 走 hub 刷 ESP32 失败（stub checksum / chip stopped responding）| Mac→板上 hub 链路不稳 | 改走 Pi 的 `/dev/ttyACM0` + venv esptool |
| 2 | ES8389 工厂测试一直 FAIL | 地址写错 0x10（实际漂 0x10/0x12，AD1 悬空）| 运行时探 {0x10..0x13} |
| 3 | ES8389 初始化 assert 失败 | esp_codec_dev `addr>>1` + 我传错地址 | `addr = 7bit<<1` |
| 4 | UAC+CDC composite Linux 拒绝（`Interface #2 referenced by multiple IADs`, `can't set config #1 error -32`）| sdkconfig.defaults 行尾中文注释让 `CONFIG_UAC_MIC_CHANNEL_NUM=0` 没生效（kconfiglib 不认带注释的 int）→ MIC=1 → UAC 占 3 接口，CDC comm 也占接口 2 → 撞车 | 去掉行尾注释 + CDC 接口号跟在 `NUM_INTERFACES` 后（robust）|
| 5 | AS_PART 模式 `tusb_config.h`/`usb_descriptors.c` 找不到 | 组件名引用错 + main 没 REQUIRE | main/CMakeLists.txt 用 `espressif__usb_device_uac` + REQUIRES 加 tinyusb/usb_device_uac |
| 6 | app_main 8KB 栈崩溃重启循环 | audio+uac init 调用栈深 | 挪到独立 16KB 任务 |
| 7 | UAC "咚一声"然后静音 | FS 反馈格式 16.16（应 10.14）→ 主机算错发流节奏 → 第一帧后 underrun | `CONFIG_UAC_SUPPORT_MACOS=y` |
| 8 | CDC 收不到主机数据 | PipeWire 缓存了重新枚举前的设备 profile（误标 IEC958）| 重启 Pi / 清 WirePlubber state |
| 9 | managed 干净版不出声；vendored（带 cdc_printf）出声但死锁 | 见上面"当前未解问题" | **未解** |

---

## 关键文件

```
firmware/typixdeck_esp32s3_lcd_init_touch_gui_uac_cdc/
├── main/
│   ├── main.c              # lcd + audio + uac + cdc + reboot-to-boot，audio_usb_task(16KB)
│   ├── audio.c/h           # ES8389 + I2S 初始化（探地址、use_mclk=false）
│   ├── usb_descriptors.c   # UAC2 speaker + CDC composite 描述符（AS_PART）
│   ├── tusb_config.h       # CFG_TUD_AUDIO=1 + CFG_TUD_CDC=1 + FS 模式
│   ├── board_pins.h        # I2S/I2C/ES8389 引脚表
│   ├── aw9523.c/h, gt911.c/h, lcd_spi_init.c/h  # lcd 那套（从 lcd 固件继承）
│   ├── CMakeLists.txt      # AS_PART wiring（tusb_config/usb_descriptors 喂给 tinyusb）
│   └── idf_component.yml   # esp_codec_dev（usb_device_uac 现在用 vendored）
├── components/usb_device_uac/   # vendored 1.3.1（main 旁边，本地组件）
├── sdkconfig.defaults      # 含 CONFIG_UAC_SUPPORT_MACOS=y（FS 反馈修复）
└── build/                  # 编译产物（gitignored）
```

参考工程（macOS 上）：
- `~/esp/s32_uac1_speaker/` — **UAC-only**（non-AS_PART，ESP32-S31，HS），出声的参考。用的是同一个 usb_device_uac 组件 non-AS_PART 模式。
- `~/esp/s31_i2s_es8311_test/` — I2S + ES8311 example（IDF 官方）。
- `~/esp/s31_es8311_mp3/` — minimp3 + ES8311。
- `esp-iot-solution/examples/usb/device/usb_uac/`（clone 在 TypixDeck 仓里）— **官方 UAC example，UAC-only，BSP codec，默认 48k/1ch**。没有 UAC+CDC composite 的官方例子。

---

## 编译 / 刷机 / 调试命令

```bash
# 编译（Mac）
export IDF_PATH=/Users/eggfly/esp/esp-idf && source $IDF_PATH/export.sh
cd ~/github/eggfly/TypixDeck/firmware/typixdeck_esp32s3_lcd_init_touch_gui_uac_cdc
idf.py build
scp build/typixdeck_esp32s3_lcd_init_touch_gui_uac_cdc.bin pi@192.168.3.84:~/tdflash/

# 刷机（Pi）—— ESP 在跑 UAC 固件时，用 CDC 魔串进下载模式（plan B）：
ssh pi@192.168.3.84 'python3 -c "import serial,time; s=serial.Serial(\"/dev/ttyACM0\",115200); s.write(b\"REBOOT_TO_BOOT_MODE\r\n\"); time.sleep(0.5)"'
sleep 3
ssh pi@192.168.3.84 '~/.venvs/esptool/bin/esptool --chip esp32s3 -p /dev/ttyACM0 -b 460800 --before default_reset --after hard_reset write_flash 0x10000 ~/tdflash/typixdeck_esp32s3_lcd_init_touch_gui_uac_cdc.bin'
# （ESP 死锁卡死、CDC 不通时，手动按 BOOT+RESET 进下载模式再刷）

# 看主机 UAC 流状态
ssh pi@192.168.3.84 'cat /proc/asound/card2/stream0'
# 看 USB 拓扑
ssh pi@192.168.3.84 'lsusb -t'
# 看 UAC 控制有没有 STALL
ssh pi@192.168.3.84 'dmesg | grep -iE "1-1.2|set_interface|clock|freq|cannot" | tail'

# 抓 CDC 日志（ESP 侧 cdc_printf 出来的，要主机开着 /dev/ttyACM0 才 tud_cdc_connected）
ssh pi@192.168.3.84 '~/.venvs/esptool/bin/python -c "import serial,time,sys; s=serial.Serial(\"/dev/ttyACM0\",115200,timeout=0.5); [sys.stdout.write(s.read(4096).decode(\"utf-8\",\"replace\")) or sys.stdout.flush() for _ in iter(lambda: time.time()<time.time()+15, False)]"'
```

---

## 给接手者的建议方向（按怀疑度排序）

1. **USB hub / TT iso 问题**（最怀疑）：FS UAC 经 HS hub 的 TT。可验证：找办法把 ESP 直连主机（绕过 hub）——但这块板 ESP USB 物理接在 hub 下，可能得飞线。或换个更高质量的 hub。或**改用 UAC1.0**（FS 设备的标准，反馈更简单，比 UAC2.0 over FS 稳）——但 `usb_device_uac` 组件只支持 UAC2.0，得换实现。
2. **`Feedback Format = 18.14` 异常**：查 `CONFIG_UAC_SUPPORT_MACOS` 的转换逻辑是不是产出了错误格式。试关掉 MACOS 看反馈格式变不变、audio 行为变不变。
3. **tinyusb 为什么不 arm audio EP**（除非 set_itf 里调 CDC API）：深挖 tinyusb audio 驱动在 AS_PART + 自定义描述符下的 EP arm 路径。对比官方 non-AS_PART example（能稳出声）的差异。
4. **退路：UAC-only**：丢掉 CDC composite，用组件 non-AS_PART 自带描述符（跟 `~/esp/s32_uac1_speaker` / 官方 example 一样）。**音频应该立刻能稳出声**（官方验证配置）。代价：失去 CDC 魔串 plan B（刷机改回 BOOT+RESET）。用户在意 plan B，但稳定出声优先。

---

## 已 commit 的状态（git）

- `main` 分支。两个相关 commit：
  - `ff8b378` feat(esp32s3): UAC2 speaker + CDC composite 固件（plan B）
  - `a55fe78` fix(esp32s3): UAC 出声 + 切回 managed（这版其实是 managed 干净版，**不出声**——别作为参考）
- **当前能出声的版本**（vendored + audio_usb_task + MACOS）**没单独 commit**，工作树里就是。如果要稳的，参考 `firmware/typixdeck_esp32s3_lcd_mp3/`（MP3 版，ES8389 出声验证过）。
- 用户工作流：**小步直接 commit/push 到 main**（不开 feature branch）。
- LFS：仓库用 Git LFS 跟踪大文件（之前 140MB mp4 的事）。
</content>
</invoke>