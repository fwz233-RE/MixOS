#pragma once

#include "esp_err.h"

// 发送 JD9168S (HD317001C40) 全部 SPI 初始化命令。
// 调用前必须先通过 AW9523 P1_1 完成硬复位（低 20ms / 高 120ms）。
esp_err_t lcd_jd9168s_spi_init(void);
