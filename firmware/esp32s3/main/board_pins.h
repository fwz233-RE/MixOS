// TypixDeck 0720 板 — ESP32-S3-PICO-1 引脚分配
// 依据 Netlist_PCB_TypixNode_1_2026-07-20.tel + ESP32-S3-PICO-1 Datasheet v1.2
// （模组 LGA56 引脚号 → GPIO 对照已逐一核对，36/37 号脚 = GPIO48/47，
//   与 N8R8 八线 PSRAM 占用的 GPIO33-37（模组 38-42 号脚）无冲突）
#pragma once

// ---- I2C 总线（AW9523 / IMU / 电量计共用）----
#define PIN_I2C_SDA        6   // U57.11 S3_I2C_SDA
#define PIN_I2C_SCL        7   // U57.12 S3_I2C_SCL

// ---- AW9523B IO 扩展器（U16，AD0=AD1=3V3）----
#define AW9523_I2C_ADDR    0x5B
// 引脚→功能以 aw9523.h 为准（2026-08-06 网表逐脚核实）：
//   P0_0 = MUX_SEL（R202 → TPG4899 ×7 SEL，R105 下拉=Pi 侧 / 1=ESP 侧）
//   P1_1 = IOE_LCD_RST、P1_4 = S3_TP_RST、P1_6 = ESP_TP_INT（走 MUX）
//   P0_7 = Pi GPIO2 = DPI VSYNC（R83 0Ω tap，vsync_mon 探测 Pi 刷屏用）
// ⚠️ INTN 脚经 R82 连到 ESP_LCD_CS(GPIO5)：LCD SPI init 期间必须全屏蔽中断；
//    init 完成后 vsync_mon 只解开 P0_7 一位（GPIO5 复用为中断输入）。

// ---- JD9168S SPI 初始化（走 R183/R184/R186，与 CH32 的 R179-182 并联）----
#define PIN_LCD_CS         5   // U57.10 ESP_LCD_CS
#define PIN_LCD_SCLK       47  // U57.37 ESP_LCD_SCLK（SPICLK_P）
#define PIN_LCD_MOSI       48  // U57.36 ESP_LCD_MOSI（SPICLK_N）

// ---- RGB DPI 控制信号 ----
#define PIN_LCD_DE         1   // U57.6
#define PIN_LCD_PCLK       2   // U57.7
#define PIN_LCD_HSYNC      3   // U57.8（JTAG strap 脚，启动后可安全复用）
#define PIN_LCD_VSYNC      4   // U57.9

// ---- RGB565 数据线（板上 R29/R30 已把 ESP_R2/ESP_B2 接地补 LSB）----
// data0..4  = B3..B7
#define PIN_LCD_B3         38
#define PIN_LCD_B4         39
#define PIN_LCD_B5         40
#define PIN_LCD_B6         41
#define PIN_LCD_B7         42
// data5..10 = G2..G7
#define PIN_LCD_G2         13
#define PIN_LCD_G3         14
#define PIN_LCD_G4         15
#define PIN_LCD_G5         16
#define PIN_LCD_G6         17
#define PIN_LCD_G7         18
// data11..15 = R3..R7
#define PIN_LCD_R3         8
#define PIN_LCD_R4         9
#define PIN_LCD_R5         10
#define PIN_LCD_R6         11
#define PIN_LCD_R7         12

// ---- 背光（经 MUX → SY7201 EN，R102 上拉默认亮）----
#define PIN_LCD_BL         21  // U57.27

// ---- SW3 = ESP32 BOOT 按钮（S3_BOOT：R1 上拉 3V3，SW3 按下接 GND）----
// 启动后 GPIO0 不再是 strap，可当普通按钮用
#define PIN_BOOT_BTN       0   // U57.5

// ---- INA219 电流/电压监控（网表 U4/U20，采样电阻 10mΩ）----
#define INA219_VBAT_ADDR   0x40  // U4: IN+=VBAT, IN-=VBAT_LOAD
#define INA219_VBUS_ADDR   0x41  // U20: IN+=VBUS_RAW, IN-=VBUS_LOAD

// ---- 电池容量（放电续航估算用）----
// 这里只是**默认种子**：batt_log 的库仑计数自学习会在放电中估出真实容量并
// 存 NVS（"batt"/"cap_mah"），之后以学习值为准（见 batt_log.c）。
// STC3117 的容量是要写进 CC_CNF 的输入参数，不是测量结果。
#define BOARD_BATT_CAPACITY_MAH  3000
#define BOARD_BATT_NOMINAL_V     3.7f

// ---- 面板时序（与 Pi 端 DPI overlay 完全一致：1144×803 总幅面）----
#define LCD_H_RES          1024
#define LCD_V_RES          768
#define LCD_HFP            46
#define LCD_HSYNC_W        30
#define LCD_HBP            44
#define LCD_VFP            15
#define LCD_VSYNC_W        5
#define LCD_VBP            15
// Pi 端 53MHz@60Hz；ESP32-S3 走 PSRAM framebuffer 带宽有限，先 26MHz（约 28Hz）。
// 若稳定可尝试 32/40MHz 提刷新率；若花屏/断流则降。
#define LCD_PCLK_HZ        (26 * 1000 * 1000)

// ===== ES8389 音频 codec（U12）— LCD SPI 初始化完成后才启用 I2S =====
// GPIO47/48 与 ESP_LCD_SCLK/MOSI 共脚（R50/R51 0Ω 复用）：LCD init 阶段当 SPI，
// 之后 audio.c 把它们重配成 I2S DOUT/DIN。音频源是 USB UAC（主机 PCM → codec）。
#define ES8389_I2C_ADDR    0x20    // CE(pin1)→R147→AGND，AD1=AD0=0（实测 AD1 悬空会漂，需探测）
#define PIN_I2S_BCLK       45      // U57.51 → ES8389.8
#define PIN_I2S_LRCK       46      // U57.52 → ES8389.10
#define PIN_I2S_DOUT       47      // U57.37 → ES8389.9  (与 LCD_SCLK 共脚)
#define PIN_I2S_DIN        48      // U57.36 → ES8389.11 (与 LCD_MOSI 共脚)
#define PIN_I2S_MCLK       (-1)    // 未接 → use_mclk=false

// ===== 麦克风输入（UAC IN 方向）=====
// 双模拟 MEMS 麦克风 MIC3(左)/MIC4(右)（ZTS6056）→ ES8389 模拟输入：
//   MIC3 → MIC1（pin24/23，伪差分）→ ADC 左声道（PGA InputSel=DIFF_MIC1P1N）
//   MIC4 → MIC2（pin22/21，伪差分）→ ADC 右声道（PGA InputSel=DIFF_MIC2N2P）
// 供电 AUDIO_3V3 由 AW9523 P1_0(DAC_3V3_EN) 控制，main.c 已显式驱高，
// 麦克风路径无需任何额外 IO 操作。
// I2S 全双工共用同一 port：DOUT=GPIO47（放音）、DIN=GPIO48（录音），
// DIN 与 LCD_MOSI 共脚——LCD SPI init 先跑完再起 I2S（时序已在 main.c 保证）。
#define UAC_MIC_SAMPLE_RATE  48000
#define UAC_MIC_CHANNELS     2
#define UAC_MIC_BITS         16

// 板 #1（ESP MAC 70:04:1D:D7:E3:40）ES8389 ADC2 模拟前端个体故障：
// 置 1 使右声道输出左声道数字拷贝（REG0x23 bit4）。
// 板 #2（70:04:1D:D8:52:70）及正常板必须置 0（真立体声）。
// 诊断证据链：docs/es8389_adc2_right_channel_dead_2026-08.md
#define BOARD1_ADC2_DEAD_WORKAROUND  0

// USB UAC + CDC composite（non-AS_PART：描述符在 components/usb_device_uac/tusb/）
#define UAC_SAMPLE_RATE    48000
#define UAC_CHANNELS       2
#define UAC_BITS           16
