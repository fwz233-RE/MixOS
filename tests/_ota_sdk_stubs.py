"""SDK interfaces for compiling the real OTA owner/transaction code offline."""
HEADERS = {
    'sdkconfig.h': '#pragma once\n',
    'esp_err.h': '''#pragma once
    typedef int esp_err_t;
    #define ESP_OK 0
    #define ESP_FAIL -1
    #define ESP_ERR_NO_MEM 0x101
    #define ESP_ERR_INVALID_ARG 0x102
    #define ESP_ERR_INVALID_STATE 0x103
    #define ESP_ERR_INVALID_SIZE 0x104
    #define ESP_ERR_NOT_FOUND 0x105
    #define ESP_ERR_INVALID_CRC 0x107
    #define ESP_ERR_NVS_NOT_FOUND 0x1102
    const char *esp_err_to_name(esp_err_t);
    ''',
    'esp_log.h': '#pragma once\n#define ESP_LOGI(tag,...) ((void)(tag))\n#define ESP_LOGW(tag,...) ((void)(tag))\n#define ESP_LOGE(tag,...) ((void)(tag))\n',
    'esp_attr.h': '#pragma once\n#define RTC_NOINIT_ATTR\n',
    'esp_partition.h': '''#pragma once
    #include <stddef.h>
    #include <stdint.h>
    #include "esp_err.h"
    typedef unsigned esp_partition_subtype_t;
    typedef struct { uint32_t address,size; unsigned subtype; char label[16]; } esp_partition_t;
    #define ESP_PARTITION_TYPE_APP 0
    #define ESP_PARTITION_TYPE_DATA 1
    #define ESP_PARTITION_SUBTYPE_DATA_OTA 0
    #define ESP_PARTITION_SUBTYPE_APP_OTA_0 0x10
    #define ESP_PARTITION_SUBTYPE_APP_OTA_15 0x1f
    const esp_partition_t *esp_partition_find_first(unsigned,unsigned,const char*);
    esp_err_t esp_partition_read(const esp_partition_t*,size_t,void*,size_t);
    ''',
    'esp_ota_ops.h': '''#pragma once
    #include "esp_partition.h"
    typedef unsigned esp_ota_handle_t;
    typedef enum { ESP_OTA_IMG_NEW=0, ESP_OTA_IMG_PENDING_VERIFY=1, ESP_OTA_IMG_VALID=2, ESP_OTA_IMG_INVALID=3, ESP_OTA_IMG_ABORTED=4, ESP_OTA_IMG_UNDEFINED=-1 } esp_ota_img_states_t;
    #define OTA_WITH_SEQUENTIAL_WRITES 0xfffffffe
    const esp_partition_t *esp_ota_get_running_partition(void);
    const esp_partition_t *esp_ota_get_boot_partition(void);
    const esp_partition_t *esp_ota_get_next_update_partition(const esp_partition_t*);
    esp_err_t esp_ota_get_state_partition(const esp_partition_t*,esp_ota_img_states_t*);
    esp_err_t esp_ota_begin(const esp_partition_t*,size_t,esp_ota_handle_t*);
    esp_err_t esp_ota_write(esp_ota_handle_t,const void*,size_t);
    esp_err_t esp_ota_abort(esp_ota_handle_t);
    esp_err_t esp_ota_end(esp_ota_handle_t);
    esp_err_t esp_ota_set_boot_partition(const esp_partition_t*);
    esp_err_t esp_ota_mark_app_valid_cancel_rollback(void);
    esp_err_t esp_ota_mark_app_invalid_rollback_and_reboot(void);
    ''',
    'esp_app_desc.h': '''#pragma once
    #include <stdint.h>
    typedef struct { uint8_t app_elf_sha256[32]; char date[16],time[16],version[32],project_name[32]; } esp_app_desc_t;
    const esp_app_desc_t *esp_app_get_description(void);
    ''',
    'esp_system.h': '#pragma once\nvoid esp_restart(void);\nunsigned esp_reset_reason(void);\n',
    'esp_task_wdt.h': '#pragma once\n#include "esp_err.h"\nesp_err_t esp_task_wdt_add(void*);\nesp_err_t esp_task_wdt_reset(void);\n',
    'esp_timer.h': '#pragma once\n#include <stdint.h>\nint64_t esp_timer_get_time(void);\n',
    'esp_random.h': '#pragma once\n#include <stdint.h>\nuint32_t esp_random(void);\n',
    'nvs.h': '''#pragma once
    #include <stdint.h>
    #include <stddef.h>
    #include "esp_err.h"
    typedef unsigned nvs_handle_t;
    #define NVS_READWRITE 1
    esp_err_t nvs_open(const char*,unsigned,nvs_handle_t*);
    esp_err_t nvs_get_blob(nvs_handle_t,const char*,void*,size_t*);
    esp_err_t nvs_set_blob(nvs_handle_t,const char*,const void*,size_t);
    esp_err_t nvs_commit(nvs_handle_t);
    esp_err_t nvs_get_u8(nvs_handle_t,const char*,uint8_t*);
    esp_err_t nvs_set_u8(nvs_handle_t,const char*,uint8_t);
    ''',
    'freertos/FreeRTOS.h': '''#pragma once
    #define pdTRUE 1
    #define pdPASS 1
    #define pdMS_TO_TICKS(n) (n)
    typedef unsigned portMUX_TYPE;
    #define portMUX_INITIALIZER_UNLOCKED 0
    #define portENTER_CRITICAL(p) ((void)(p))
    #define portEXIT_CRITICAL(p) ((void)(p))
    ''',
    'freertos/queue.h': '''#pragma once
    #include <stddef.h>
    typedef struct test_queue *QueueHandle_t;
    QueueHandle_t xQueueCreate(unsigned,size_t);
    int xQueueSend(QueueHandle_t,const void*,unsigned);
    int xQueueReceive(QueueHandle_t,void*,unsigned);
    void xQueueReset(QueueHandle_t);
    unsigned uxQueueSpacesAvailable(QueueHandle_t);
    ''',
    'freertos/task.h': '#pragma once\nvoid vTaskDelay(unsigned);\nvoid vTaskDelete(void*);\nint xTaskCreate(void (*)(void*),const char*,unsigned,void*,unsigned,void*);\n',
    'mbedtls/sha256.h': '''#pragma once
    #include <openssl/sha.h>
    typedef SHA256_CTX mbedtls_sha256_context;
    static inline void mbedtls_sha256_init(mbedtls_sha256_context *c){(void)c;}
    static inline void mbedtls_sha256_free(mbedtls_sha256_context *c){(void)c;}
    static inline int mbedtls_sha256_starts(mbedtls_sha256_context *c,int n){(void)n;return SHA256_Init(c)==1?0:-1;}
    static inline int mbedtls_sha256_update(mbedtls_sha256_context *c,const unsigned char *p,size_t n){return SHA256_Update(c,p,n)==1?0:-1;}
    static inline int mbedtls_sha256_finish(mbedtls_sha256_context *c,unsigned char *p){return SHA256_Final(p,c)==1?0:-1;}
    ''',
}
