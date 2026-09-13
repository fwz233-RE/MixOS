/*
 * SPDX-FileCopyrightText: 2025 Espressif Systems (Shanghai) CO LTD
 *
 * SPDX-License-Identifier: Apache-2.0
 */

#ifndef _ES8389_REG_H_
#define _ES8389_REG_H_

#ifdef __cplusplus
extern "C" {
#endif

/*
 *   ES8389_REGISTER NAME_REG_REGISTER ADDRESS
 */
#define ES8389_RESET_REG0x00                0x00

/*
 *   ES8389 MISC CONTROL ADDRESS
 */
#define ES8389_MISC_CONTROL_REG0x01         0x01

/*
 * Clock Scheme Register definition
 */
#define ES8389_CLK_MANAGER_REG0x02          0x02
#define ES8389_CLK_MANAGER_REG0x03          0x03
#define ES8389_CLK_MANAGER_REG0x04          0x04
#define ES8389_CLK_MANAGER_REG0x05          0x05
#define ES8389_CLK_MANAGER_REG0x06          0x06
#define ES8389_CLK_MANAGER_REG0x07          0x07
#define ES8389_CLK_MANAGER_REG0x08          0x08
#define ES8389_CLK_MANAGER_REG0x09          0x09
#define ES8389_CLK_MANAGER_REG0x0A          0x0A
#define ES8389_CLK_MANAGER_REG0x0B          0x0B
#define ES8389_CLK_MANAGER_REG0x0C          0x0C
#define ES8389_CLK_MANAGER_REG0x0D          0x0D
#define ES8389_CLK_MANAGER_REG0x0E          0x0E
#define ES8389_CLK_MANAGER_REG0x0F          0x0F
#define ES8389_CLK_MANAGER_REG0x10          0x10
#define ES8389_CLK_MANAGER_REG0x11          0x11

/*
 * ADC SP CONTROL
 */
#define ES8389_ADC_SP_CONTROL_REG0x20       0x20
#define ES8389_ADC_SP_CONTROL_REG0x21       0x21
#define ES8389_ADC_SP_CONTROL_REG0x22       0x22
#define ES8389_ADC_SP_CONTROL_REG0x23       0x23
#define ES8389_ADC_SP_CONTROL_REG0x24       0x24
#define ES8389_ADC_SP_CONTROL_REG0x25       0x25
#define ES8389_ADC_SP_CONTROL_REG0x26       0x26
#define ES8389_ADC_SP_CONTROL_REG0x27       0x27
#define ES8389_ADC_SP_CONTROL_REG0x28       0x28
#define ES8389_ADC_SP_CONTROL_REG0x2A       0x2A
#define ES8389_ADC_SP_CONTROL_REG0x2F       0x2F
#define ES8389_ADC_SP_CONTROL_REG0x31       0x31

/*
 * ALC CONTROL
 */
#define ES8389_ALC_CONTROL_REG0x29          0x29
#define ES8389_ALC_CONTROL_REG0x2B          0x2B
#define ES8389_ALC_CONTROL_REG0x2C          0x2C
#define ES8389_ALC_CONTROL_REG0x2D          0x2D

/*
 * DAC SERIAL PORT CONTROL
 */
#define ES8389_DAC_CONTROL_REG0x40          0x40
#define ES8389_DAC_CONTROL_REG0x41          0x41
#define ES8389_DAC_CONTROL_REG0x42          0x42
#define ES8389_DAC_CONTROL_REG0x43          0x43
#define ES8389_DAC_CONTROL_REG0x44          0x44
#define ES8389_DAC_CONTROL_REG0x45          0x45
#define ES8389_DAC_CONTROL_REG0x46          0x46
#define ES8389_DAC_CONTROL_REG0x47          0x47
#define ES8389_DAC_CONTROL_REG0x48          0x48
#define ES8389_DAC_CONTROL_REG0x49          0x49
#define ES8389_DAC_CONTROL_REG0x4D          0x4D

/*
 * VMID CONTROL
 */

#define ES8389_VMID_CONTROL_REG0x60         0x60

/*
 * ANALOG ENABLE CONTROL
 */
#define ES8389_ANALOG_CONTROL_REG0x61       0x61
#define ES8389_ANALOG_CONTROL_REG0x62       0x62
#define ES8389_ANALOG_CONTROL_REG0x63       0x63
#define ES8389_ANALOG_CONTROL_REG0x64       0x64
#define ES8389_ANALOG_CONTROL_REG0x69       0x69
#define ES8389_ANALOG_CONTROL_REG0x6B       0x6B
#define ES8389_ANALOG_CONTROL_REG0x6C       0x6C
#define ES8389_ANALOG_CONTROL_REG0x6D       0x6D
#define ES8389_ANALOG_CONTROL_REG0x6E       0x6E
#define ES8389_ANALOG_CONTROL_REG0x6F       0x6F
#define ES8389_ANALOG_CONTROL_REG0x70       0x70
#define ES8389_ANALOG_CONTROL_REG0x71       0x71

/*
 *  PGA1 GAIN CONTROL
 */
#define ES8389_PGA1_GAIN_CONTROL_REG0x72    0x72
#define ES8389_PGA1_GAIN_CONTROL_REG0x73    0x73

/*
 *  CHIP MISC CONTROL
 */
#define ES8389_CHIP_MISC_CONTROL_REG0xF0    0xF0

/*
 *  CSM STATE REPORT
 */
#define ES8389_CSM_STATE_REG0xF1            0xF1

/*
 *  PULL DOWN CONTROL
 */
#define ES8389_PULL_DOWN_CONTROL_REG0xF2    0xF2

/*
 *   ISOLATION CONTROL
 */
#define ES8389_ISOLATION_CONTROL_REG0xF3    0xF3

/*
 *    CSM STATE CONTROL
 */
#define ES8389_CSM_STATE_CONTROL_REG0xF4    0xF4

/*
 *     CHIP ID1 ID2
 */
#define ES8389_CHIP_ID1_CONTROL_REG0xFD     0xFD
#define ES8389_CHIP_ID2_CONTROL_REG0xFE     0xFE

#define ES8389_MAX_REGISTER                 0xFF

/* ES8389 Data Format Definition */
#define ES8389_DAIFMT_I2S                   (0 << 2)
#define ES8389_DAIFMT_LEFT_J                (1 << 2)
#define ES8389_DAIFMT_DSP_A                 (1 << 3)
#define ES8389_DAIFMT_DSP_B                 (3 << 3)

/* ES8389 Data length Definition */
#define ES8389_S24_LE                       (0 << 5)
#define ES8389_S20_LE                       (1 << 5)
#define ES8389_S18_LE                       (2 << 5)
#define ES8389_S16_LE                       (3 << 5)
#define ES8389_S32_LE                       (4 << 5)

/* ALSA call function used */
#define ES8389_MASK_MSModeSel               (1 << 0)
#define ES8389_MASK_LRCKINV                 (1 << 4)
#define ES8389_MASK_SCLKINV                 (1 << 0)
#define ES8389_MASK_DATALEN                 (7 << 5)
#define ES8389_MASK_DAIFMT                  (7 << 2)

#define ES8389_DriveSel_Normal		        0
#define ES8389_DriveSel_LowPower	        1
#define ES8389_DriveSel_HeadPhone	        2

#define ES8389_Analog_DriveSel              ES8389_DriveSel_Normal  /* Normal;LowPower;HeadPhone */

/*
 * PGA1 gain registers REG0x72 (ADCL/LINSEL) and REG0x73 (ADCR/RINSEL):
 * bits [6:4] are the 3-bit analog input mux. Verified against ES8389_DS Rev 5.0
 * (Oct 2025), REGISTER 0X72/0X73 tables:
 *   LINSEL: 001 – MIC1P-MIC1N diff, 101 – MIC1P single, 110 – MIC2P single
 *   RINSEL: 001 – MIC2N-MIC2P diff, 101 – MIC2P single, 110 – MIC1P single
 * Bits [3:0] are PGA gain steps (es8389_mic_gain_t); bit7 is reserved.
 */
typedef enum {
    /* ADCL (LINSEL) */
    ES8389_ADC_INSEL_L_DIFF_MIC1P1N = 1,  /* Diff MIC1P-MIC1N */
    ES8389_ADC_INSEL_L_SINGLE_MIC1P = 5,  /* Single-ended MIC1P */
    ES8389_ADC_INSEL_L_SINGLE_MIC2P = 6,  /* Single-ended MIC2P (cross: right mic) */
    /* ADCR (RINSEL) */
    ES8389_ADC_INSEL_R_DIFF_MIC2N2P = 1,  /* Diff MIC2N-MIC2P */
    ES8389_ADC_INSEL_R_SINGLE_MIC2P = 5,  /* Single-ended MIC2P */
    ES8389_ADC_INSEL_R_SINGLE_MIC1P = 6,  /* Single-ended MIC1P (cross: left mic) */
} es8389_adc_insel_t;

/*
 * 本板（TypixDeck 0720 #1）诊断结论（2026-08-15，详见
 * docs/es8389_adc2_right_channel_dead_2026-08.md）：
 * U12 的 PGA2/MOD2 模拟块损坏（右声道模拟前端），芯片单元级故障：
 *   - 交叉探测：MIC2P 进 PGA1 有信号（右麦 MIC4 硬件好）；
 *     MIC1P 进 PGA2 仍死寂（与输入源无关）；
 *   - 数字注入（REG0x23 bit4）：右槽位 100% 复制 ADC1 → 数字链路全好。
 * Workaround：正常差分输入 + open 时 REG0x23=0x10（右声道=左声道拷贝）。
 * 换 U12 后应把 es8389.c 中 REG0x23 恢复写 0x00 以启用真立体声。
 */
#define ES8389_ADCL_InputSel  ES8389_ADC_INSEL_L_DIFF_MIC1P1N
#define ES8389_ADCR_InputSel  ES8389_ADC_INSEL_R_DIFF_MIC2N2P

typedef enum {
    ES8389_MIC_GAIN_MIN    = -1,
    ES8389_MIC_GAIN_0DB    = 0b0000,
    ES8389_MIC_GAIN_3_5DB  = 0b0001,
    ES8389_MIC_GAIN_6_5DB  = 0b0010,
    ES8389_MIC_GAIN_9_5DB  = 0b0011,
    ES8389_MIC_GAIN_12_5DB = 0b0110,
    ES8389_MIC_GAIN_15_5DB = 0b0111,
    ES8389_MIC_GAIN_18_5DB = 0b1000,
    ES8389_MIC_GAIN_21_5DB = 0b1001,
    ES8389_MIC_GAIN_24_5DB = 0b1010,
    ES8389_MIC_GAIN_27_5DB = 0b1011,
    ES8389_MIC_GAIN_30_5DB = 0b1100,
    ES8389_MIC_GAIN_33_5DB = 0b1101,
    ES8389_MIC_GAIN_36_5DB = 0b1110,
    ES8389_MIC_GAIN_MAX
} es8389_mic_gain_t;

#ifdef __cplusplus
}
#endif

#endif
