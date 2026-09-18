// One owner for the shared I2C bus.
//
// Two problems made this module necessary.
//
// 1. A bus reset raced live transactions.
//    The device task resets the bus when the IO expander stops matching its
//    expected register state. The IDF bus lock serialises individual
//    transactions, but it does not stop a reset of the whole bus from landing
//    in the middle of another task's transfer -- and the main task (touch,
//    keyboard) and the battery logger both drive this same bus.
//    mix_i2c_reset_bus() takes the same lock every transaction takes, so a
//    reset can only happen between transfers.
//
// 2. Register access was implemented four times.
//    sensors.c, aw9523.c, gt911.c and mix_keyboard.c each carried their own
//    wrapper around i2c_master_transmit_receive, with four different timeout
//    values and, in one case, a function named "_le" that decoded big endian.
//
// The lock is recursive so that a driver holding it for a read-modify-write
// can call the helpers below without deadlocking itself.
#pragma once
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include "driver/i2c_master.h"
#include "esp_err.h"
/* The timeout arguments below are meant to be one of the three tiers in
 * board_pins.h (probe / transfer / bulk), so every user of this API gets them
 * without a separate include. */
#include "board_pins.h"

/* Call once, right after the bus is created, before any task is started. */
esp_err_t mix_i2c_init(i2c_master_bus_handle_t bus);

/* Hold the bus across several transfers, for read-modify-write sequences. */
void mix_i2c_lock(void);
void mix_i2c_unlock(void);

/* Reset the controller and the bus. Serialised against every helper here. */
esp_err_t mix_i2c_reset_bus(void);

/* Probe an address. Uses the short probe timeout: an absent device must fail
 * fast, because the presence scan walks ten addresses. */
esp_err_t mix_i2c_probe(uint8_t address, int timeout_ms);

/* Add a device to the bus, or return NULL after logging why. */
i2c_master_dev_handle_t mix_i2c_add_device(uint8_t address, uint32_t scl_hz);

/* Release a device handle. Serialised for the same reason as everything else:
 * a failed probe used to remove its handle while another task was mid-transfer
 * on the same bus. Also frees the address for a later retry. */
esp_err_t mix_i2c_rm_device(i2c_master_dev_handle_t dev);

/* ---- Register access, 8-bit register address ---- */
esp_err_t mix_i2c_read(i2c_master_dev_handle_t dev, uint8_t reg,
                       uint8_t *out, size_t len, int timeout_ms);
esp_err_t mix_i2c_read_u8(i2c_master_dev_handle_t dev, uint8_t reg, uint8_t *out);
esp_err_t mix_i2c_write_u8(i2c_master_dev_handle_t dev, uint8_t reg, uint8_t value);

/* Big-endian 16-bit register word, which is what the INA219, the CW2015 and
 * the STC3117 all return. */
esp_err_t mix_i2c_read_u16be(i2c_master_dev_handle_t dev, uint8_t reg, uint16_t *out);
esp_err_t mix_i2c_write_u16be(i2c_master_dev_handle_t dev, uint8_t reg, uint16_t value);

/* ---- Register access, 16-bit big-endian register address (GT911) ---- */
esp_err_t mix_i2c_read_wide(i2c_master_dev_handle_t dev, uint16_t reg,
                            uint8_t *out, size_t len, int timeout_ms);
esp_err_t mix_i2c_write_wide_u8(i2c_master_dev_handle_t dev, uint16_t reg, uint8_t value);

/* Raw transfers, for drivers that build their own frames. Still serialised. */
esp_err_t mix_i2c_transmit(i2c_master_dev_handle_t dev, const uint8_t *data,
                           size_t len, int timeout_ms);
esp_err_t mix_i2c_transmit_receive(i2c_master_dev_handle_t dev,
                                   const uint8_t *tx, size_t tx_len,
                                   uint8_t *rx, size_t rx_len, int timeout_ms);
