/* Direct STM32F042 ROM handoff.
 *
 * The stock QMK stm32-dfu implementation stores a magic word in retained SRAM
 * and resets. The STM32 ROM leave path can reset without clearing that SRAM,
 * so the application can immediately interpret its own stale request and jump
 * back to ROM. Entering the ROM directly avoids that retained-marker loop.
 */
#include "bootloader.h"
#include "hal.h"

#define STM32_ROM_DFU_ADDRESS 0x1FFFC400U

typedef struct {
    uint32_t stack_top;
    void (*entrypoint)(void);
} stm32_rom_vector_t;

void enter_bootloader_mode_if_requested(void) {
    /* Deliberately ignore the legacy retained marker. */
}

void bootloader_jump(void) {
    const stm32_rom_vector_t *rom = (const stm32_rom_vector_t *)STM32_ROM_DFU_ADDRESS;

    __disable_irq();
    SysTick->CTRL = 0;
    SysTick->VAL = 0;
    SysTick->LOAD = 0;
    for (uint32_t i = 0; i < sizeof(NVIC->ICER) / sizeof(NVIC->ICER[0]); ++i) {
        NVIC->ICER[i] = 0xFFFFFFFFU;
        NVIC->ICPR[i] = 0xFFFFFFFFU;
    }
    __set_CONTROL(0);
    __set_MSP(rom->stack_top);
    __enable_irq();
    rom->entrypoint();
    while (true) {
    }
}

void mcu_reset(void) {
    NVIC_SystemReset();
}
