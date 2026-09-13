#pragma once
#include <stddef.h>
#include <stdint.h>
#include "esp_err.h"
typedef unsigned esp_partition_mmap_handle_t;
typedef struct {size_t size; uint32_t address;} esp_partition_t;
#define ESP_PARTITION_TYPE_DATA 1
#define ESP_PARTITION_SUBTYPE_ANY 0
#define ESP_PARTITION_MMAP_DATA 0
const esp_partition_t *esp_partition_find_first(int type, int subtype, const char *label);
esp_err_t esp_partition_mmap(const esp_partition_t *part, size_t offset, size_t size, int type, const void **ptr, esp_partition_mmap_handle_t *handle);
void esp_partition_munmap(esp_partition_mmap_handle_t handle);
