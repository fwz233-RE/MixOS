// See mix_i2c.h for why this module exists.
#include "mix_i2c.h"

#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "esp_log.h"
#include "board_pins.h"

static const char *TAG = "I2C";

static i2c_master_bus_handle_t s_bus;
static SemaphoreHandle_t       s_lock;

esp_err_t mix_i2c_init(i2c_master_bus_handle_t bus)
{
    if (!bus) return ESP_ERR_INVALID_ARG;
    if (!s_lock) s_lock = xSemaphoreCreateRecursiveMutex();
    if (!s_lock) return ESP_ERR_NO_MEM;
    s_bus = bus;
    return ESP_OK;
}

void mix_i2c_lock(void)
{
    if (s_lock) xSemaphoreTakeRecursive(s_lock, portMAX_DELAY);
}

bool mix_i2c_try_lock(void)
{
    return s_lock && xSemaphoreTakeRecursive(s_lock, 0) == pdTRUE;
}

void mix_i2c_unlock(void)
{
    if (s_lock) xSemaphoreGiveRecursive(s_lock);
}

esp_err_t mix_i2c_reset_bus(void)
{
    if (!s_bus) return ESP_ERR_INVALID_STATE;
    mix_i2c_lock();
    esp_err_t err = i2c_master_bus_reset(s_bus);
    mix_i2c_unlock();
    if (err != ESP_OK) ESP_LOGE(TAG, "bus reset failed: %s", esp_err_to_name(err));
    else ESP_LOGW(TAG, "bus reset performed between transfers");
    return err;
}

esp_err_t mix_i2c_probe(uint8_t address, int timeout_ms)
{
    if (!s_bus) return ESP_ERR_INVALID_STATE;
    mix_i2c_lock();
    esp_err_t err = i2c_master_probe(s_bus, address, timeout_ms);
    mix_i2c_unlock();
    return err;
}

i2c_master_dev_handle_t mix_i2c_add_device(uint8_t address, uint32_t scl_hz)
{
    if (!s_bus) return NULL;
    i2c_device_config_t cfg = {
        .dev_addr_length = I2C_ADDR_BIT_LEN_7,
        .device_address = address,
        .scl_speed_hz = scl_hz,
    };
    i2c_master_dev_handle_t dev = NULL;
    mix_i2c_lock();
    esp_err_t err = i2c_master_bus_add_device(s_bus, &cfg, &dev);
    mix_i2c_unlock();
    if (err != ESP_OK) {
        ESP_LOGW(TAG, "add device 0x%02X failed: %s", address, esp_err_to_name(err));
        return NULL;
    }
    return dev;
}

esp_err_t mix_i2c_rm_device(i2c_master_dev_handle_t dev)
{
    if (!dev) return ESP_ERR_INVALID_ARG;
    mix_i2c_lock();
    esp_err_t err = i2c_master_bus_rm_device(dev);
    mix_i2c_unlock();
    return err;
}

esp_err_t mix_i2c_transmit(i2c_master_dev_handle_t dev, const uint8_t *data,
                           size_t len, int timeout_ms)
{
    if (!dev) return ESP_ERR_INVALID_ARG;
    mix_i2c_lock();
    esp_err_t err = i2c_master_transmit(dev, data, len, timeout_ms);
    mix_i2c_unlock();
    return err;
}

esp_err_t mix_i2c_transmit_receive(i2c_master_dev_handle_t dev,
                                   const uint8_t *tx, size_t tx_len,
                                   uint8_t *rx, size_t rx_len, int timeout_ms)
{
    if (!dev) return ESP_ERR_INVALID_ARG;
    mix_i2c_lock();
    esp_err_t err = i2c_master_transmit_receive(dev, tx, tx_len, rx, rx_len, timeout_ms);
    mix_i2c_unlock();
    return err;
}

esp_err_t mix_i2c_read(i2c_master_dev_handle_t dev, uint8_t reg,
                       uint8_t *out, size_t len, int timeout_ms)
{
    return mix_i2c_transmit_receive(dev, &reg, 1, out, len, timeout_ms);
}

esp_err_t mix_i2c_read_u8(i2c_master_dev_handle_t dev, uint8_t reg, uint8_t *out)
{
    return mix_i2c_read(dev, reg, out, 1, I2C_XFER_TIMEOUT_MS);
}

esp_err_t mix_i2c_write_u8(i2c_master_dev_handle_t dev, uint8_t reg, uint8_t value)
{
    const uint8_t frame[2] = { reg, value };
    return mix_i2c_transmit(dev, frame, sizeof(frame), I2C_XFER_TIMEOUT_MS);
}

esp_err_t mix_i2c_read_u16be(i2c_master_dev_handle_t dev, uint8_t reg, uint16_t *out)
{
    uint8_t b[2];
    esp_err_t err = mix_i2c_read(dev, reg, b, sizeof(b), I2C_XFER_TIMEOUT_MS);
    if (err != ESP_OK) return err;
    *out = (uint16_t)(((uint16_t)b[0] << 8) | b[1]);
    return ESP_OK;
}

esp_err_t mix_i2c_write_u16be(i2c_master_dev_handle_t dev, uint8_t reg, uint16_t value)
{
    const uint8_t frame[3] = { reg, (uint8_t)(value >> 8), (uint8_t)value };
    return mix_i2c_transmit(dev, frame, sizeof(frame), I2C_XFER_TIMEOUT_MS);
}

esp_err_t mix_i2c_read_wide(i2c_master_dev_handle_t dev, uint16_t reg,
                            uint8_t *out, size_t len, int timeout_ms)
{
    const uint8_t address[2] = { (uint8_t)(reg >> 8), (uint8_t)reg };
    return mix_i2c_transmit_receive(dev, address, sizeof(address), out, len, timeout_ms);
}

esp_err_t mix_i2c_write_wide_u8(i2c_master_dev_handle_t dev, uint16_t reg, uint8_t value)
{
    const uint8_t frame[3] = { (uint8_t)(reg >> 8), (uint8_t)reg, value };
    return mix_i2c_transmit(dev, frame, sizeof(frame), I2C_XFER_TIMEOUT_MS);
}
