#pragma once
#include <stdint.h>
typedef struct { uint32_t CFGR1; } fake_syscfg_t;
extern fake_syscfg_t fake_syscfg;
#define SYSCFG (&fake_syscfg)
#define SYSCFG_CFGR1_PA11_PA12_RMP 1U
typedef struct { uint32_t ISR, ICR, RXDR, TXDR, CR1, TIMINGR, OAR1; } fake_i2c_t;
extern fake_i2c_t fake_i2c;
#define I2C1 (&fake_i2c)
#define I2C_ISR_BERR (1U<<0)
#define I2C_ISR_ARLO (1U<<1)
#define I2C_ISR_OVR (1U<<2)
#define I2C_ISR_RXNE (1U<<3)
#define I2C_ISR_ADDR (1U<<4)
#define I2C_ISR_DIR (1U<<5)
#define I2C_ISR_TXE (1U<<6)
#define I2C_ISR_TXIS (1U<<7)
#define I2C_ISR_NACKF (1U<<8)
#define I2C_ISR_STOPF (1U<<9)
#define I2C_ICR_BERRCF I2C_ISR_BERR
#define I2C_ICR_ARLOCF I2C_ISR_ARLO
#define I2C_ICR_OVRCF I2C_ISR_OVR
#define I2C_ICR_ADDRCF I2C_ISR_ADDR
#define I2C_ICR_NACKCF I2C_ISR_NACKF
#define I2C_ICR_STOPCF I2C_ISR_STOPF
#define I2C_OAR1_OA1EN 1U
#define I2C_CR1_PE 1U
#define I2C_CR1_ADDRIE 2U
#define I2C_CR1_RXIE 4U
#define I2C_CR1_TXIE 8U
#define I2C_CR1_STOPIE 16U
#define I2C_CR1_NACKIE 32U
#define I2C_CR1_ERRIE 64U
#define I2C1_IRQn 23
#define A13 13
#define B6 22
#define B7 23
#define PAL_MODE_OUTPUT_OPENDRAIN 1
#define PAL_MODE_INPUT 0
#define PAL_MODE_ALTERNATE(n) (n)
#define PAL_STM32_OTYPE_OPENDRAIN 2
#define RCC_APB1ENR_I2C1EN 1
#define RCC_APB1RSTR_I2C1RST 1
#define OSAL_IRQ_HANDLER(name) void name(void)
#define OSAL_IRQ_PROLOGUE() ((void)0)
#define OSAL_IRQ_EPILOGUE() ((void)0)
#define chSysLock() ((void)0)
#define chSysUnlock() ((void)0)
#define palClearLine(line) ((void)(line))
#define palSetLineMode(line, mode) ((void)(line), (void)(mode))
#define rccEnableAPB1(bit, enable) ((void)(bit), (void)(enable))
#define rccResetAPB1(bit) ((void)(bit))
#define nvicEnableVector(irq, priority) ((void)(irq), (void)(priority))
