/*
 * The MIT License (MIT)
 *
 * Copyright (c) 2019 Ha Thach (tinyusb.org)
 * Copyright (c) 2020 Jerzy Kasenbreg
 *
 * Permission is hereby granted, free of charge, to any person obtaining a copy
 * of this software and associated documentation files (the "Software"), to deal
 * in the Software without restriction, including without limitation the rights
 * to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
 * copies of the Software, and to permit persons to whom the Software is
 * furnished to do so, subject to the following conditions:
 *
 * The above copyright notice and this permission notice shall be included in
 * all copies or substantial portions of the Software.
 *
 * THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
 * IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
 * FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
 * AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
 * LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
 * OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
 * THE SOFTWARE.
 *
 */

#include "tusb.h"
#include "uac_descriptors.h"

//--------------------------------------------------------------------+
// Device Descriptors
//--------------------------------------------------------------------+
tusb_desc_device_t const desc_device = {
    .bLength            = sizeof(tusb_desc_device_t),
    .bDescriptorType    = TUSB_DESC_DEVICE,
    .bcdUSB             = 0x0200,

    // Use Interface Association Descriptor (IAD) for CDC
    // As required by USB Specs IAD's subclass must be common class (2) and protocol must be IAD (1)
    .bDeviceClass       = TUSB_CLASS_MISC,
    .bDeviceSubClass    = MISC_SUBCLASS_COMMON,
    .bDeviceProtocol    = MISC_PROTOCOL_IAD,
    .bMaxPacketSize0    = CFG_TUD_ENDPOINT0_SIZE,

    .idVendor           = CONFIG_UAC_TUSB_VID,
    .idProduct          = CONFIG_UAC_TUSB_PID,
    .bcdDevice          = 0x0100,

    .iManufacturer      = 0x01,
    .iProduct           = 0x02,
    .iSerialNumber      = 0x03,

    .bNumConfigurations = 0x01
};

// Invoked when received GET DEVICE DESCRIPTOR
// Application return pointer to descriptor
uint8_t const *tud_descriptor_device_cb(void)
{
    return (uint8_t const *)&desc_device;
}

//--------------------------------------------------------------------+
// Configuration Descriptor
//--------------------------------------------------------------------+
#define CONFIG_TOTAL_LEN        (TUD_CONFIG_DESC_LEN + CFG_TUD_AUDIO * TUD_AUDIO_DEVICE_DESC_LEN \
                                 + CFG_TUD_CDC * TUD_CDC_DESC_LEN)
#define EPNUM_AUDIO_OUT   0x01
#define EPNUM_AUDIO_FB    0x81
#define EPNUM_AUDIO_IN    0x82

// CDC 接口跟在 UAC 后面（spk+mic 时 UAC 占 itf 0/1/2，CDC=3/4）。
// mic 启用后 ISO IN 用 0x82，CDC notify 挪到 0x84——ESP32-S3 dwc2 共 5 个
// IN EP（含 EP0）：0x81 FB / 0x82 mic ISO IN / 0x83 CDC bulk IN / 0x84 notify，
// 刚好占满。描述符结构变了 → PID 从 0x80C2 挪到 0x80C3（sdkconfig）避开主机缓存。
enum {
    ITF_NUM_CDC_COMM = ITF_NUM_TOTAL,
    ITF_NUM_CDC_DATA,
    ITF_NUM_TOTAL_COMPOSITE,
};
#define EPNUM_CDC_NOTIFY  0x84
#define EPNUM_CDC_OUT     0x03
#define EPNUM_CDC_IN      0x83
// CDC 接口字符串索引 = 现有字符串表末尾（lang/mfr/prod/serial/"usb uac" = 0..4，
// 之后 speaker / microphone 按配置各占一个）
#define STRID_CDC  (5 + (SPEAK_CHANNEL_NUM ? 1 : 0) + (MIC_CHANNEL_NUM ? 1 : 0))

uint8_t const desc_configuration[] = {
    // Config number, interface count, string index, total length, attribute, power in mA
    TUD_CONFIG_DESCRIPTOR(1, ITF_NUM_TOTAL_COMPOSITE, 0, CONFIG_TOTAL_LEN, 0x00, 100),
    // Interface number, string index, EP Out & EP In address, EP size
    TUD_AUDIO_DESCRIPTOR(ITF_NUM_AUDIO_CONTROL, 4, EPNUM_AUDIO_OUT, EPNUM_AUDIO_IN, EPNUM_AUDIO_FB),
    // CDC: comm + data（宏自带 IAD），notify 8B，bulk 64B（FS）
    TUD_CDC_DESCRIPTOR(ITF_NUM_CDC_COMM, STRID_CDC, EPNUM_CDC_NOTIFY, 8, EPNUM_CDC_OUT, EPNUM_CDC_IN, 64),
};

// Invoked when received GET CONFIGURATION DESCRIPTOR
// Application return pointer to descriptor
// Descriptor contents must exist long enough for transfer to complete
uint8_t const *tud_descriptor_configuration_cb(uint8_t index)
{
    (void)index; // for multiple configurations
    return desc_configuration;
}

//--------------------------------------------------------------------+
// String Descriptors
//--------------------------------------------------------------------+

// array of pointer to string descriptors
char const *string_desc_arr [] = {
    (const char[]) { 0x09, 0x04 },  // 0: is supported language is English (0x0409)
    CONFIG_UAC_TUSB_MANUFACTURER,       // 1: Manufacturer
    CONFIG_UAC_TUSB_PRODUCT,            // 2: Product
    CONFIG_UAC_TUSB_SERIAL_NUM,         // 3: Serials, should use chip ID
    "usb uac",                      // 4: UAC control Interface
#if SPEAK_CHANNEL_NUM
    "speaker",                     // 5: Speak Interface
#endif
#if MIC_CHANNEL_NUM
    "microphone",                   // 6: Mic Interface
#endif
    "cdc debug",                    // STRID_CDC: CDC Interface
};

static uint16_t _desc_str[32];

// Invoked when received GET STRING DESCRIPTOR request
// Application return pointer to descriptor, whose contents must exist long enough for transfer to complete
uint16_t const *tud_descriptor_string_cb(uint8_t index, uint16_t langid)
{
    (void)langid;

    uint8_t chr_count;

    if (index == 0) {
        memcpy(&_desc_str[1], string_desc_arr[0], 2);
        chr_count = 1;
    } else {

        if (!(index < sizeof(string_desc_arr) / sizeof(string_desc_arr[0]))) {
            return NULL;
        }

        const char *str = string_desc_arr[index];

        // Cap at max char
        chr_count = (uint8_t) strlen(str);
        if (chr_count > 31) {
            chr_count = 31;
        }

        for (uint8_t i = 0; i < chr_count; i++) {
            _desc_str[1 + i] = str[i];
        }
    }

    // first byte is length (including header), second byte is string type
    _desc_str[0] = (uint16_t)((TUSB_DESC_STRING << 8) | (2 * chr_count + 2));

    return _desc_str;
}
