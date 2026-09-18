#pragma once
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#define MIX_MAX_PAYLOAD 512
/* Pixel bytes per MIX_SCREEN_DATA frame: the payload also carries the 4-byte
 * offset this run belongs at. Even, so an RGB565 pixel never straddles two
 * frames. */
#define MIX_SCREEN_CHUNK 508
#define MIX_MAX_WIRE 540
#define MIX_HEADER_SIZE 18
#define MIX_RX_WINDOW 4096
#define MIX_CH_CONTROL 0
#define MIX_CH_TERMINAL 1
#define MIX_CH_STATUS 2
#define MIX_CH_JOB 3
#define MIX_CH_MAINTENANCE 4
#define MIX_CH_LOG 5
#define MIX_CH_NET 6
#define MIX_CH_MAX MIX_CH_NET
enum { MIX_HELLO=1,MIX_HELLO_ACK,MIX_PING,MIX_PONG,MIX_ERROR,MIX_CREDIT,
 MIX_OPEN=16,MIX_OPENED,MIX_DATA,MIX_INPUT,MIX_RESIZE,MIX_CLOSE,MIX_EXIT,
 MIX_STATUS_REQUEST=32,MIX_STATUS,MIX_JOB_START=48,MIX_JOB_PROGRESS,MIX_JOB_CANCEL,MIX_JOB_RESULT,
 /* Maintenance: 64-66 are the legacy ROM-download escape hatch, 67-76 are the
  * in-protocol A/B update that replaces it for ordinary upgrades.
  *
  * IDENTIFY/IDENTITY are answerable at any time, with no update in progress,
  * because their whole purpose is to be asked after the post-update reboot. */
 MIX_PREPARE_UPDATE=64,MIX_UPDATE_READY,MIX_ENTER_BOOT,
 MIX_OTA_BEGIN=67,MIX_OTA_READY,MIX_OTA_DATA,MIX_OTA_ACK,MIX_OTA_END,MIX_OTA_DONE,
 MIX_OTA_ABORT,MIX_OTA_STATUS,MIX_OTA_IDENTIFY,MIX_OTA_IDENTITY,
 MIX_OTA_CAPS_QUERY=77,MIX_OTA_CAPS,MIX_OTA_REQUEST,
 MIX_LOG=80,MIX_OTA_RESPONSE=81,
 /* Screenshot. The host asks once; the device answers with one INFO frame
  * describing the framebuffer, then a run of DATA frames each carrying its own
  * byte offset, then END. The offset is what makes a dropped or reordered
  * frame detectable without the receiver having to trust frame ordering.
  *
  * Capture is deliberately not synchronised with drawing: the UI task may be
  * repainting while the buffer is read, so a capture can show a torn frame.
  * Blocking the draw path for a diagnostic would be the worse trade.
  */
 MIX_SCREEN_REQUEST=88,MIX_SCREEN_INFO,MIX_SCREEN_DATA,MIX_SCREEN_END,
 /* Network: the ESP asks, the host acts through its own bounded wrapper. No
  * command string ever crosses this channel. */
 MIX_NET_SCAN=96,MIX_NET_LIST,MIX_NET_CONNECT,MIX_NET_FORGET,MIX_NET_RESULT };
typedef struct { uint8_t channel,type; uint32_t epoch,session,sequence; uint16_t length; uint8_t payload[MIX_MAX_PAYLOAD]; } mix_frame_t;
typedef struct { uint8_t bytes[MIX_MAX_WIRE]; size_t used; bool discard; uint32_t errors; } mix_decoder_t;
uint16_t mix_get16(const uint8_t *p);
uint32_t mix_get32(const uint8_t *p);
void mix_put16(uint8_t *p,uint16_t v);
void mix_put32(uint8_t *p,uint32_t v);
uint32_t mix_crc32(const uint8_t *p,size_t n);
size_t mix_frame_encode(const mix_frame_t *f,uint8_t *wire,size_t cap);
bool mix_frame_decode(const uint8_t *wire,size_t n,mix_frame_t *out);
/* One byte at a time. True only on a valid delimiter-terminated frame. */
bool mix_decoder_push(mix_decoder_t *d,uint8_t b,mix_frame_t *out);
