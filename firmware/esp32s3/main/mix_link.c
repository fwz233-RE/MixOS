/* SPDX-License-Identifier: MIT */
#include "mix_link.h"
#include "mix_protocol.h"
#include "mix_terminal.h"
#include "mix_ota.h"
#include "mix_ota_tx.h"
#include "esp_timer.h"
#include "mix_health.h"
#include <stdatomic.h>
#include "mix_ui.h"
#include <string.h>
#include <stdio.h>
#include <math.h>
#include <float.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/queue.h"
#include "esp_random.h"
#include "cJSON.h"
#include "tusb.h"

static QueueHandle_t rxq,controlq,inputq;
/* Cross-core publication uses C11 sequentially consistent atomics throughout:
 * main publishes epoch only after revoking OTA ownership/resetting queues;
 * IO latches every observed disconnect before publishing transport_open.
 * Acknowledgement follows main's revocation, never merely its observation. */
static _Atomic uint32_t io_epoch,io_disconnects,io_disconnect_ack;
static _Atomic bool transport_open,usb_mounted,io_fault;
static bool online,terminal_open,opening,reset_input,boot_request,restart_request;
static uint32_t epoch,session,next_session,rx_sequence,now,rx_time,hello_time,ping_time,status_time;
static uint32_t granted,received,pending_update,update_deadline,update_grant,job;
static bool rx_seen,job_running,credit_dirty;
static uint32_t open_time,ota_session,ota_acked,ota_chunks,restart_at;
static bool ota_v2, host_exchange;
static uint32_t host_exchange_ms;
static _Atomic uint32_t io_progress_ms;
static _Atomic bool io_started;
static int ota_notified=-1;
static int job_percent;
static char notice[96];
static mix_view_t metrics;
static uint16_t req_cols,req_rows;
/* Screenshot transfer in flight, or 0. The framebuffer is 1.5 MiB and the
 * control queue holds twelve frames, so the capture is pumped a few frames per
 * tick from mix_link_tick rather than enqueued in one go: filling the queue is
 * what faults the link and would drop the terminal with it. */
static uint32_t shot_session,shot_offset,shot_total;
static uint8_t shot_frame[4+MIX_SCREEN_CHUNK];
static uint8_t open_app;
static uint32_t terminal_exit_serial;
static uint8_t terminal_exit_app;
static uint32_t net_session,net_deadline;
static uint32_t time_base_s,time_base_ms,wifi_rate_time;
/* The clock pair outlives USB epochs; neither part is link-local telemetry. */
static int16_t time_tz_offset_min;
static mix_net_entry_t net_list[MIX_NET_MAX];
static int net_count;
static char net_message[96];
static void set_notice(const char *s){snprintf(notice,sizeof(notice),"%s",s);}
const char *mix_link_notice(void){return notice;}
/* The host maps these to argv through its own fixed table; the identifier is
 * the whole request and no command string ever crosses the link. */
static const char *const app_names[MIX_APP_COUNT]={"translate","notes","agent","shell"};

static bool enqueue(uint8_t ch,uint8_t type,uint32_t sid,const void *data,size_t len){
    if(len>MIX_MAX_PAYLOAD||!epoch||io_disconnects!=io_disconnect_ack)return false;
    mix_frame_t f={.channel=ch,.type=type,.epoch=epoch,.session=sid,.length=(uint16_t)len};
    if(len)memcpy(f.payload,data,len);
    QueueHandle_t q=type==MIX_INPUT?inputq:controlq;
    if(xQueueSend(q,&f,0)!=pdTRUE){
        /* OTA results are queryable; ordinary TX backpressure must not abort
         * a receiving transaction by forcibly replacing the link epoch. */
        if(ota_session||mix_ota_transaction_busy())return false;
        io_fault=true;set_notice("Link queue full; reconnecting safely");return false;
    }
    return true;
}
static void clear_session(void){
    terminal_open=opening=false;credit_dirty=false;session=0;granted=received=0;reset_input=true;
    open_app=MIX_APP_SHELL;
    xQueueReset(inputq);
}
static void clear_ota(void){ota_session=ota_acked=ota_chunks=0;ota_notified=-1;ota_v2=false;}
static void clear_screenshot(void){shot_session=shot_offset=shot_total=0;}
/* Move a capture forward by whatever the control queue can take right now.
 *
 * Spare slots are left free on purpose: status, ping and terminal output share
 * this queue, and a capture that filled it would stall the very UI it is
 * photographing. A failed enqueue means the link is already resetting, so the
 * transfer is simply dropped; the host sees no END and reports a short read.
 */
static void screenshot_pump(void){
    if(!shot_session)return;
    size_t bytes=0;
    const uint8_t *pixels=(const uint8_t *)mix_ui_framebuffer(&bytes,NULL,NULL);
    if(!pixels||bytes!=shot_total){clear_screenshot();return;}
    while(shot_offset<shot_total&&uxQueueSpacesAvailable(controlq)>4){
        uint32_t n=shot_total-shot_offset;
        if(n>MIX_SCREEN_CHUNK)n=MIX_SCREEN_CHUNK;
        mix_put32(shot_frame,shot_offset);
        memcpy(shot_frame+4,pixels+shot_offset,n);
        if(!enqueue(MIX_CH_MAINTENANCE,MIX_SCREEN_DATA,shot_session,shot_frame,4+n)){
            clear_screenshot();return;
        }
        shot_offset+=n;
    }
    if(shot_offset>=shot_total){
        uint8_t p[4];mix_put32(p,shot_total);
        enqueue(MIX_CH_MAINTENANCE,MIX_SCREEN_END,shot_session,p,sizeof(p));
        clear_screenshot();
    }
}
static void restart_link(void){
    static uint32_t previous_epoch;
    io_epoch=0;
    mix_ota_link_lost();host_exchange=false;
    online=false;clear_session();clear_ota();job=0;job_running=false;pending_update=update_grant=0;rx_seen=false;
    clear_screenshot();
    net_session=0;net_count=0;net_message[0]=0;
    epoch=esp_random();if(!epoch)epoch=1;
    if(epoch==previous_epoch){epoch++;if(!epoch)epoch=1;}
    previous_epoch=epoch;
    xQueueReset(controlq);xQueueReset(rxq);io_epoch=epoch;io_fault=false;
    rx_time=now;hello_time=now-1000;ping_time=status_time=now;
    metrics.linux_cpu=metrics.linux_temp=NAN;
    metrics.linux_mem_used_kib=metrics.linux_mem_total_kib=metrics.linux_uptime_s=0;
    metrics.wifi_reported=metrics.wifi_connected=metrics.wifi_speed_valid=false;
    metrics.wifi_signal=-1;metrics.wifi_rx_bps=-1;
    metrics.wifi_ssid[0]=metrics.host_ip[0]=0;
}
typedef struct {
    mix_decoder_t decoder;
    mix_frame_t rx,tx;
    uint8_t wire[MIX_MAX_WIRE],buf[256];
    size_t length,off;
    uint32_t generation,sequence,filled,used;
    bool rx_held,tx_held,delimiter_pending;
} mix_io_t;
/* IO alone owns this state. Queue reset cannot clear a held RX frame or a
 * partially encoded TX frame: those must follow the same generation fence.
 * Keep a dequeued NEW-generation frame when main changed epoch during dequeue.
 * Frames enqueued late across a reset retain their old wire epoch; handle()
 * rejects them even if the final queue send races main's queue reset. */
static bool io_sync(mix_io_t *s){
    uint32_t current=io_epoch;
    bool ready=transport_open && io_disconnects==io_disconnect_ack && current;
    if(!ready||s->generation!=current){
        s->generation=current;s->sequence=0;
        s->length=s->off=0;s->filled=s->used=0;s->rx_held=false;
        memset(&s->decoder,0,sizeof(s->decoder));
        s->delimiter_pending=true;
        if(!ready||!s->tx_held||s->tx.epoch!=current)s->tx_held=false;
    }
    return ready;
}
/* Only this task touches CDC RX/TX. No terminal/UI/I2C calls inside it. */
static void io_task(void *arg){
    (void)arg;mix_io_t s={0};bool was_open=false;
    if(mix_watchdog_task_begin(MIX_HEALTH_IO)!=ESP_OK){io_fault=true;vTaskDelete(NULL);return;}
    io_started=true;
    for(;;){
        /* Independent of whether Linux has mounted/opened CDC. */
        io_progress_ms=(uint32_t)(esp_timer_get_time()/1000);
        if(mix_watchdog_task_reset(MIX_HEALTH_IO)!=ESP_OK){io_fault=true;mix_watchdog_task_end(MIX_HEALTH_IO);vTaskDelete(NULL);return;}
        bool connected=tud_cdc_connected();
        if(was_open&&!connected)io_disconnects++;
        was_open=connected;transport_open=connected;usb_mounted=tud_mounted();
        if(!io_sync(&s)){
            /* Reopening DTR does not resume the old session. Wait for main to
             * revoke OTA ownership, but drain stale host bytes to avoid FIFO
             * deadlock. A new HELLO cannot be sent before that acknowledgement. */
            if(connected)tud_cdc_read(s.buf,sizeof(s.buf));
            tud_cdc_write_flush();vTaskDelay(pdMS_TO_TICKS(5));continue;
        }
        /* RX continues even when the TX delimiter is backpressured. */
        if(s.rx_held&&xQueueSend(rxq,&s.rx,0)==pdTRUE)s.rx_held=false;
        for(int batch=0;batch<4&&!s.rx_held;batch++){
            uint32_t before=s.generation;
            if(s.used==s.filled){s.filled=tud_cdc_read(s.buf,sizeof(s.buf));s.used=0;}
            if(!io_sync(&s)||s.generation!=before||!s.filled)break;
            while(s.used<s.filled&&!s.rx_held){
                if(mix_decoder_push(&s.decoder,s.buf[s.used++],&s.rx)){
                    if(s.rx.epoch==s.generation&&xQueueSend(rxq,&s.rx,0)!=pdTRUE)s.rx_held=true;
                }
            }
        }
        if(io_sync(&s)){
            if(!s.length&&!s.tx_held){
                s.tx_held=xQueueReceive(controlq,&s.tx,0)==pdTRUE;
                if(!s.tx_held)s.tx_held=xQueueReceive(inputq,&s.tx,0)==pdTRUE;
            }
            /* Recheck AFTER dequeue: never discard a new-epoch HELLO merely
             * because generation was cached before main's publication. */
            if(io_sync(&s)){
                if(s.tx_held&&s.tx.epoch!=s.generation)s.tx_held=false;
                if(s.delimiter_pending){
                    uint8_t zero=0;
                    if(tud_cdc_write(&zero,1)==1)s.delimiter_pending=false;
                }
                /* A generation change during delimiter write requires another
                 * delimiter. Never replace an incomplete delimiter with data. */
                if(io_sync(&s)&&!s.delimiter_pending){
                    if(s.tx_held&&!s.length){
                        s.tx.sequence=++s.sequence;
                        s.length=mix_frame_encode(&s.tx,s.wire,sizeof(s.wire));s.off=0;
                    }
                    if(s.length){
                        uint32_t n=tud_cdc_write(s.wire+s.off,(uint32_t)(s.length-s.off));s.off+=n;
                        if(s.off==s.length){s.length=s.off=0;s.tx_held=false;}
                    }
                }
            }
        }
        tud_cdc_write_flush();vTaskDelay(1);
    }
}
void tud_cdc_rx_cb(uint8_t itf){(void)itf;} /* FIFO serviced by io_task, backpressure by TinyUSB */
/* A cursor-position or device-attribute answer is ordinary terminal input.
 * Routing it through the same path keeps the credit and queue rules intact. */
static void terminal_reply(const uint8_t *bytes,size_t len,void *ctx){
    (void)ctx;mix_link_input(bytes,len);
}
esp_err_t mix_link_init(void){
    rxq=xQueueCreate(16,sizeof(mix_frame_t));controlq=xQueueCreate(12,sizeof(mix_frame_t));inputq=xQueueCreate(16,sizeof(mix_frame_t));
    if(!rxq||!controlq||!inputq)return ESP_ERR_NO_MEM;
    metrics.linux_cpu=metrics.linux_temp=NAN;metrics.wifi_signal=-1;metrics.wifi_rx_bps=-1;
    metrics.wifi_speed_valid=false;
    mix_terminal_init();mix_terminal_set_reply(terminal_reply,NULL);return ESP_OK;
}
esp_err_t mix_link_start_io(void){return xTaskCreate(io_task,"mix_usb",8192,NULL,6,NULL)==pdPASS?ESP_OK:ESP_ERR_NO_MEM;}
bool mix_link_usb_mounted(void){return usb_mounted;}
bool mix_link_io_healthy(uint32_t ms){return io_started&&(uint32_t)(ms-io_progress_ms)<2000u;}
bool mix_link_host_healthy(uint32_t ms){return online&&host_exchange&&(uint32_t)(ms-host_exchange_ms)<6000u;}
static void ota_replies(void){
    mix_ota_reply_t reply;
    /* Leave replies in the worker queue until control TX has capacity. */
    while(uxQueueSpacesAvailable(controlq)>2&&mix_ota_poll_reply(&reply)){
        enqueue(MIX_CH_MAINTENANCE,reply.type,reply.session,reply.payload,reply.length);
        if(reply.type==MIX_OTA_READY){set_notice("Receiving firmware update");}
        if(reply.type==MIX_OTA_DONE){
            ota_session=0;
            restart_at=(uint32_t)(esp_timer_get_time()/1000)+1200u;if(!restart_at)restart_at=1;
            set_notice("Update verified; restarting");
        }
        if(reply.type==MIX_OTA_RESPONSE&&reply.length==MIX_OTA_RESPONSE_BYTES){
            uint8_t phase=reply.payload[2],result=reply.payload[3];
            if(reply.payload[1]==MIX_TX_BEGIN&&result==MIX_TX_OK&&phase==MIX_TX_RECEIVING){ota_session=reply.session;ota_v2=true;}
            if(phase==MIX_TX_FAILED||phase==MIX_TX_ABORTED||phase==MIX_TX_CONFIRMED){clear_ota();}
            if(reply.payload[1]==MIX_TX_BEGIN&&result!=MIX_TX_OK&&result!=MIX_TX_BUSY&&!mix_ota_transaction_busy())clear_ota();
        }
        if(reply.type==MIX_ERROR&&reply.session==ota_session&&mix_ota_state()!=MIX_OTA_RECEIVING){clear_ota();}
    }
}
static double number(cJSON *j,const char *key){cJSON *v=cJSON_GetObjectItemCaseSensitive(j,key);return cJSON_IsNumber(v)?v->valuedouble:NAN;}
static void copy_string(cJSON *j,const char *key,char *out,size_t cap){
    cJSON *v=cJSON_GetObjectItemCaseSensitive(j,key);
    if(cJSON_IsString(v)&&v->valuestring)snprintf(out,cap,"%s",v->valuestring);
    else out[0]=0;
}
static uint32_t whole(double v){return isfinite(v)&&v>=0&&v<UINT32_MAX?(uint32_t)v:0;}
static void json_metrics(const mix_frame_t *f){
    metrics.wifi_speed_valid=false;metrics.wifi_rx_bps=-1;
    cJSON *j=cJSON_ParseWithLength((const char*)f->payload,f->length);if(!j)return;
    metrics.linux_cpu=(float)number(j,"cpu_pct");metrics.linux_temp=(float)number(j,"temp_c");
    metrics.linux_mem_used_kib=whole(number(j,"mem_used_kib"));
    metrics.linux_mem_total_kib=whole(number(j,"mem_total_kib"));
    metrics.linux_uptime_s=whole(number(j,"uptime_s"));
    /* An absent Wi-Fi report stays absent. Reporting "0%" for a radio the host
     * never described would be an invented measurement. */
    cJSON *wifi=cJSON_GetObjectItemCaseSensitive(j,"wifi");
    metrics.wifi_reported=cJSON_IsObject(wifi);
    if(metrics.wifi_reported){
        copy_string(wifi,"ssid",metrics.wifi_ssid,sizeof(metrics.wifi_ssid));
        metrics.wifi_connected=cJSON_IsTrue(cJSON_GetObjectItemCaseSensitive(wifi,"connected"));
        double s=number(wifi,"signal");
        metrics.wifi_signal=(isfinite(s)&&s>=0&&s<=100)?(int)s:-1;
        double rx=number(wifi,"rx_bps");
        /* A rate is meaningful only for the connected state in this same
         * status object. A disconnected report must not retain or display a
         * counter from an earlier link. */
        metrics.wifi_speed_valid=metrics.wifi_connected&&isfinite(rx)&&rx>=0&&rx<=FLT_MAX;
        metrics.wifi_rx_bps=metrics.wifi_speed_valid?(float)rx:-1;
        if(metrics.wifi_speed_valid)wifi_rate_time=now;
    }else{
        metrics.wifi_connected=false;metrics.wifi_speed_valid=false;metrics.wifi_signal=-1;
        metrics.wifi_rx_bps=-1;metrics.wifi_ssid[0]=0;
    }
    copy_string(j,"ip",metrics.host_ip,sizeof(metrics.host_ip));
    double wall=number(j,"time_s"),tz=number(j,"tz_offset_min");
    /* Accept one complete clock pair, never reinterpret a held local clock as
     * UTC because STATUS omitted/nullified its timezone. Zero means unknown;
     * integers and current civil offsets (UTC-12..UTC+14) are the wire contract. */
    if(isfinite(wall)&&wall>=1&&wall<=UINT32_MAX&&floor(wall)==wall&&
       isfinite(tz)&&tz>=-720&&tz<=840&&floor(tz)==tz){
        time_base_s=(uint32_t)wall;time_base_ms=now;time_tz_offset_min=(int16_t)tz;
    }
    cJSON_Delete(j);
}
static void credit(void){uint8_t b[4];mix_put32(b,granted);enqueue(MIX_CH_TERMINAL,MIX_CREDIT,session,b,4);}
/* Scan results arrive packed rather than as JSON: at 35 bytes per entry a
 * single 512-byte frame carries a full list, and the parser needs no allocator. */
static void net_scan_result(const mix_frame_t *f){
    net_count=0;
    if(!f->length)return;
    unsigned entries=f->payload[0],at=1;
    for(unsigned i=0;i<entries&&net_count<MIX_NET_MAX;i++){
        if(at+3>f->length)break;
        uint8_t flags=f->payload[at],signal=f->payload[at+1],len=f->payload[at+2];
        at+=3;
        if(len>MIX_SSID_MAX||at+len>f->length)break;
        mix_net_entry_t *e=&net_list[net_count++];
        memcpy(e->ssid,f->payload+at,len);e->ssid[len]=0;
        e->signal=signal>100?100:signal;
        e->secured=(flags&1)!=0;e->known=(flags&2)!=0;
        at+=len;
    }
}
static void net_frame(const mix_frame_t *f){
    if(!net_session||f->session!=net_session)return;
    if(f->type==MIX_NET_LIST){
        net_scan_result(f);net_session=0;
        snprintf(net_message,sizeof(net_message),"%d network%s found",net_count,net_count==1?"":"s");
        return;
    }
    if(f->type==MIX_NET_RESULT||f->type==MIX_ERROR){
        const uint8_t *text=f->payload;size_t len=f->length;
        if(f->type==MIX_NET_RESULT&&len){text++;len--;}
        size_t cap=sizeof(net_message)-1;
        if(len>cap)len=cap;
        memcpy(net_message,text,len);net_message[len]=0;
        if(!net_message[0])snprintf(net_message,sizeof(net_message),"%s",
            f->type==MIX_ERROR?"Network request refused":"Done");
        net_session=0;
        return;
    }
}
static bool net_request(uint8_t type,const void *payload,size_t len){
    if(!online||net_session)return false;
    /* A counter, not a clock: two requests in the same millisecond must not
     * share an identifier and confuse each other's replies. */
    static uint32_t next_net;
    net_session=0x40000000u|(++next_net&0x3fffffffu);
    if(!net_session)net_session=0x40000001u;
    if(!enqueue(MIX_CH_NET,type,net_session,payload,len)){net_session=0;return false;}
    net_deadline=now+20000;net_message[0]=0;
    return true;
}
static size_t pack_string(uint8_t *out,size_t at,size_t cap,const char *s,size_t max_len){
    size_t len=s?strlen(s):0;
    if(len>max_len)len=max_len;
    if(at+1+len>cap)return at;
    out[at++]=(uint8_t)len;memcpy(out+at,s,len);return at+len;
}
static void maintenance_error(uint32_t sid,const char *reason){
    enqueue(MIX_CH_MAINTENANCE,MIX_ERROR,sid,reason,strlen(reason));
}
static void ota_ack(uint32_t sid){uint8_t b[4];mix_put32(b,mix_ota_received());enqueue(MIX_CH_MAINTENANCE,MIX_OTA_ACK,sid,b,4);}
/* Maintenance frames are host-authorized; see protocol/USB_V1.md for why this
 * sequencing is transport hygiene and not peer authentication. */
static void maintenance(const mix_frame_t *f){
    if(!f->session)return;
    switch(f->type){
    case MIX_PREPARE_UPDATE:
        /* Legacy ROM-download escape hatch, kept for full-flash recovery. */
        if(f->length||pending_update||boot_request||ota_session||mix_ota_transaction_busy())return;
        mix_link_close_terminal();
        if(enqueue(MIX_CH_MAINTENANCE,MIX_UPDATE_READY,f->session,NULL,0)){
            pending_update=update_grant=f->session;update_deadline=now+15000;
            set_notice("Host update ready; waiting for boot request");
        }
        return;
    case MIX_ENTER_BOOT:
        if(!f->length&&update_grant==f->session&&(int32_t)(update_deadline-now)>0){
            update_grant=pending_update=0;clear_session();boot_request=true;
        }
        return;
    case MIX_OTA_CAPS_QUERY:{
        if(f->length)return;
        uint8_t p[MIX_OTA_CAP_BYTES];size_t n=mix_ota_capabilities(p,sizeof(p));
        if(n)enqueue(MIX_CH_MAINTENANCE,MIX_OTA_CAPS,f->session,p,n);
        else maintenance_error(f->session,"OTA worker unavailable");
        return;}
    case MIX_OTA_REQUEST:{
        if(pending_update||boot_request){maintenance_error(f->session,"ROM download already authorized");return;}
        if(f->length==MIX_OTA_REQUEST_BYTES&&f->payload[1]==MIX_TX_BEGIN){
            if(ota_session&&ota_session!=f->session){maintenance_error(f->session,"another update owns link");return;}
            mix_link_close_terminal();clear_screenshot();
            if(!ota_session){ota_session=f->session;ota_v2=true;}
        }
        if(!mix_ota_submit_request(f->session,f->payload,f->length))maintenance_error(f->session,"OTA worker unavailable");
        return;}
    case MIX_OTA_BEGIN:
        if(f->length!=36){maintenance_error(f->session,"OTA_BEGIN needs size and sha256");return;}
        if(ota_session&&(ota_session!=f->session||ota_v2)){maintenance_error(f->session,"another update is active");return;}
        if(pending_update||boot_request||restart_at){maintenance_error(f->session,"boot already pending");return;}
        mix_link_close_terminal();clear_screenshot();
        if(mix_ota_submit_legacy(f->type,f->session,f->payload,f->length)){
            ota_session=f->session;ota_v2=false;ota_notified=-1;
        }else maintenance_error(f->session,"OTA worker busy; query before retry");
        return;
    case MIX_OTA_DATA:
    case MIX_OTA_STATUS:
        if(!ota_session||f->session!=ota_session)return;
        /* Queue pressure leaves offset unchanged; cumulative ACK/query lets the
         * host retransmit without running flash from the UI thread. */
        if(!mix_ota_submit_legacy(f->type,f->session,f->payload,f->length))ota_ack(f->session);
        return;
    /* Answerable whenever the UI exists, including with no update in progress:
     * its whole purpose is to let the host see what is actually on the panel. */
    case MIX_UI_PERF_REQUEST:{
        if(f->length)return;
        char p[MIX_MAX_PAYLOAD];size_t n=mix_ui_performance(p,sizeof(p));
        if(n)enqueue(MIX_CH_MAINTENANCE,MIX_UI_PERF_RESPONSE,f->session,p,n);
        return;}
    case MIX_SCREEN_REQUEST:{
        if(f->length)return;
        if(ota_session||mix_ota_transaction_busy()){maintenance_error(f->session,"capture deferred during firmware update");return;}
        /* Session zero would make shot_session indistinguishable from idle. */
        if(!f->session)return;
        if(shot_session){maintenance_error(f->session,"capture already in progress");return;}
        size_t bytes=0;uint16_t w=0,h=0;
        if(!mix_ui_framebuffer(&bytes,&w,&h)||!bytes){
            maintenance_error(f->session,"framebuffer unavailable");return;}
        uint8_t p[12];
        mix_put16(p,w);mix_put16(p+2,h);
        mix_put32(p+4,(uint32_t)bytes);
        mix_put32(p+8,MIX_SCREEN_CHUNK);
        if(!enqueue(MIX_CH_MAINTENANCE,MIX_SCREEN_INFO,f->session,p,sizeof(p)))return;
        shot_session=f->session;shot_offset=0;shot_total=(uint32_t)bytes;
        return;}
    /* Deliberately answerable with no transfer in progress and in a session
     * the device has never seen before: the host asks this after the reboot,
     * over a link that was re-established from scratch. */
    case MIX_OTA_IDENTIFY:{
        if(f->length)return;
        uint8_t p[MIX_OTA_IDENTITY_BYTES];
        size_t n=mix_ota_identity(p,sizeof(p));
        if(!n){maintenance_error(f->session,"identity unavailable");return;}
        enqueue(MIX_CH_MAINTENANCE,MIX_OTA_IDENTITY,f->session,p,n);
        return;}
    case MIX_OTA_END:
    case MIX_OTA_ABORT:
        if(f->length||!ota_session||f->session!=ota_session)return;
        if(ota_v2){maintenance_error(f->session,"v2 control command required");return;}
        if(!mix_ota_submit_legacy(f->type,f->session,NULL,0))maintenance_error(f->session,"OTA worker busy; query before retry");
        return;
    default:
        return;
    }
}
static void handle(const mix_frame_t *f){
    if(io_disconnects!=io_disconnect_ack||!transport_open||f->epoch!=epoch)return;
    if(rx_seen&&(int32_t)(f->sequence-rx_sequence)<=0)return;
    rx_seen=true;rx_sequence=f->sequence;
    if(f->channel==MIX_CH_CONTROL&&f->session==0){
        if(f->type==MIX_HELLO_ACK&&f->length==4&&mix_get16(f->payload)==MIX_MAX_PAYLOAD&&mix_get16(f->payload+2)==MIX_RX_WINDOW){online=true;rx_time=now;set_notice("Linux connected");return;}
        if(!online)return;
        if(f->type==MIX_PING&&f->length==0)enqueue(MIX_CH_CONTROL,MIX_PONG,0,NULL,0);
        if((f->type==MIX_PING||f->type==MIX_PONG)&&f->length==0){rx_time=now;host_exchange=true;host_exchange_ms=now;}
    }
    if(!online)return;
    if(f->channel==MIX_CH_CONTROL&&f->type==MIX_ERROR){
        if(session&&f->session==session){clear_session();set_notice("Linux rejected terminal request");}
        else if(job&&f->session==job){job_running=false;set_notice("Linux task failed");}
        return;
    }
    if(f->channel==MIX_CH_TERMINAL&&session&&f->session==session){
        if(f->type==MIX_OPENED&&opening&&f->length==4){
            if(mix_get16(f->payload)!=req_cols||mix_get16(f->payload+2)!=req_rows){mix_link_close_terminal();set_notice("Unsupported terminal geometry");return;}
            opening=false;terminal_open=true;mix_terminal_init();granted=MIX_RX_WINDOW;received=0;credit_dirty=true;reset_input=true;
        }else if(f->type==MIX_DATA&&terminal_open){
            uint32_t avail=granted-received;
            if(avail>MIX_RX_WINDOW||f->length>avail){mix_link_close_terminal();set_notice("Terminal flow control violation");return;}
            received+=f->length;mix_terminal_feed(f->payload,f->length);granted+=f->length;credit_dirty=true;
        }else if(f->type==MIX_EXIT&&f->length==4){
            if(terminal_open&&mix_get32(f->payload)==0){
                terminal_exit_app=open_app;++terminal_exit_serial;
            }
            clear_session();set_notice("Terminal session ended");
        }
        else if(f->type==MIX_ERROR){clear_session();set_notice("Linux rejected terminal request");}
    }
    if(f->channel==MIX_CH_STATUS&&f->session==0&&f->type==MIX_STATUS)json_metrics(f);
    if(f->channel==MIX_CH_JOB&&job&&f->session==job){
        if(f->type==MIX_JOB_PROGRESS){cJSON *j=cJSON_ParseWithLength((const char*)f->payload,f->length);if(j){double v=number(j,"percent");if(isfinite(v)&&v>=0&&v<=100)job_percent=(int)v;cJSON_Delete(j);}}
        if(f->type==MIX_JOB_RESULT||f->type==MIX_ERROR){job_running=false;set_notice(f->type==MIX_ERROR?"Linux task failed":"Linux task completed");}
    }
    if(f->channel==MIX_CH_MAINTENANCE)maintenance(f);
    if(f->channel==MIX_CH_NET)net_frame(f);
}
void mix_link_tick(uint32_t now_ms,mix_view_t *v){
    now=now_ms;
    uint32_t disconnects=io_disconnects;
    if(!transport_open||disconnects!=io_disconnect_ack){
        io_epoch=0;
        if(epoch){mix_ota_link_lost();host_exchange=false;online=false;clear_session();clear_ota();epoch=0;pending_update=update_grant=0;job_running=false;set_notice("Linux disconnected");}
        xQueueReset(controlq);xQueueReset(rxq);
        /* Acknowledge only this snapshot AFTER revocation. A second disconnect
         * racing this tick remains pending and keeps IO gated. */
        io_disconnect_ack=disconnects;
    }
    if(transport_open&&io_disconnects==io_disconnect_ack){
        if(!epoch||io_fault)restart_link();
        mix_frame_t f;for(int i=0;i<16&&xQueueReceive(rxq,&f,0)==pdTRUE;i++)handle(&f);
        /* Consume queued valid heartbeats before judging their deadline. */
        if((uint32_t)(now-rx_time)>8000)restart_link();
        ota_replies();
        if(!online&&(uint32_t)(now-hello_time)>=1000){uint8_t p[4];mix_put16(p,MIX_MAX_PAYLOAD);mix_put16(p+2,MIX_RX_WINDOW);enqueue(MIX_CH_CONTROL,MIX_HELLO,0,p,4);hello_time=now;}
        if(credit_dirty&&terminal_open){credit_dirty=false;credit();}
        if(opening&&(uint32_t)(now-open_time)>5000){mix_link_close_terminal();set_notice("Terminal open timed out");}
        if(online&&(uint32_t)(now-ping_time)>=2000){enqueue(MIX_CH_CONTROL,MIX_PING,0,NULL,0);ping_time=now;}
        if(online&&(uint32_t)(now-status_time)>=2000){enqueue(MIX_CH_STATUS,MIX_STATUS_REQUEST,0,NULL,0);status_time=now;}
        if(pending_update&&(int32_t)(now-update_deadline)>=0){pending_update=update_grant=0;set_notice("Host update grant expired");}
        if(net_session&&(int32_t)(now-net_deadline)>=0){
            net_session=0;snprintf(net_message,sizeof(net_message),"Network request timed out");
        }
        screenshot_pump();
    }
    /* The OTA worker alone owns stall checks, abort and confirmation. */
    if(ota_session&&!mix_ota_transaction_busy()&&mix_ota_state()==MIX_OTA_FAILED){clear_ota();}
    if(mix_ota_take_worker_restart()){restart_at=0;restart_request=true;}
    v->linux_online=online;v->terminal_open=terminal_open;
    v->terminal_exit_serial=terminal_exit_serial;v->terminal_exit_app=terminal_exit_app;
    v->maintenance_busy=pending_update||ota_session||restart_at||mix_ota_transaction_busy();
    v->ota_state=(uint8_t)mix_ota_state();v->ota_percent=mix_ota_percent();
    v->job_running=job_running;v->job_percent=job_percent;
    v->linux_cpu=online?metrics.linux_cpu:NAN;v->linux_temp=online?metrics.linux_temp:NAN;
    v->linux_mem_used_kib=online?metrics.linux_mem_used_kib:0;v->linux_mem_total_kib=online?metrics.linux_mem_total_kib:0;
    v->linux_uptime_s=online?metrics.linux_uptime_s:0;
    v->running_app=open_app;
    v->wifi_reported=online&&metrics.wifi_reported;
    v->wifi_connected=v->wifi_reported&&metrics.wifi_connected;
    v->wifi_signal=v->wifi_reported?metrics.wifi_signal:-1;
    if(!online||(uint32_t)(now-wifi_rate_time)>=6000u)metrics.wifi_speed_valid=false;
    v->wifi_speed_valid=v->wifi_reported&&metrics.wifi_connected&&metrics.wifi_speed_valid;
    v->wifi_rx_bps=v->wifi_speed_valid?metrics.wifi_rx_bps:-1;
    snprintf(v->wifi_ssid,sizeof(v->wifi_ssid),"%s",v->wifi_reported?metrics.wifi_ssid:"");
    snprintf(v->host_ip,sizeof(v->host_ip),"%s",online?metrics.host_ip:"");
    /* Roll the baseline forward on every tick, retaining subsecond remainder.
     * A fixed last-STATUS baseline would rewind after 49.7 days without a new
     * sample when the uint32 millisecond counter wraps. Tick gaps must remain
     * below one complete counter period, as elsewhere in the link timers. */
    if(time_base_s){
        uint32_t elapsed=(uint32_t)(now-time_base_ms)/1000u;
        if(elapsed>UINT32_MAX-time_base_s)time_base_s=0;
        else time_base_s+=elapsed;
        time_base_ms+=elapsed*1000u;
    }
    v->host_time_s=time_base_s;
    v->host_tz_offset_min=time_tz_offset_min;
}
bool mix_link_open_app(mix_app_t app){
    if(!online||pending_update||ota_session)return false;
    if((unsigned)app>=MIX_APP_COUNT)return false;
    /* A session already showing the requested application *is* the answer to
     * the request, so pressing the same card twice keeps the half-written note
     * rather than throwing it away. A session showing a different application
     * is not: it has to end first. The host refuses a second OPEN while one
     * session is alive ("terminal already open"), so returning true here left
     * the screen drawing the old application while the UI titled it as the new
     * one. CLOSE and OPEN travel the same channel in order, so by the time the
     * host reads the OPEN it has already torn the old pseudo-terminal down. */
    if(terminal_open||opening){
        if(open_app==(uint8_t)app)return true;
        mix_link_close_terminal();
    }
    session=++next_session;if(!session)session=++next_session;
    req_cols=(uint16_t)mix_terminal_cols();req_rows=(uint16_t)mix_terminal_rows();
    const char *name=app_names[app];
    size_t name_len=strlen(name);
    uint8_t p[5+16];
    mix_put16(p,req_cols);mix_put16(p+2,req_rows);
    p[4]=(uint8_t)name_len;memcpy(p+5,name,name_len);
    open_time=now;open_app=(uint8_t)app;
    opening=enqueue(MIX_CH_TERMINAL,MIX_OPEN,session,p,5+name_len);
    if(!opening)session=0;
    return opening;
}
void mix_link_close_terminal(void){if(session&&online)enqueue(MIX_CH_TERMINAL,MIX_CLOSE,session,NULL,0);clear_session();}
void mix_link_resize(int cols,int rows){
    if(!terminal_open||cols<1||rows<1)return;
    req_cols=(uint16_t)cols;req_rows=(uint16_t)rows;
    uint8_t p[4];mix_put16(p,req_cols);mix_put16(p+2,req_rows);
    enqueue(MIX_CH_TERMINAL,MIX_RESIZE,session,p,4);
}
bool mix_link_net_scan(void){return net_request(MIX_NET_SCAN,NULL,0);}
bool mix_link_net_connect(const char *ssid,const char *passphrase){
    if(!ssid||!*ssid)return false;
    uint8_t p[1+MIX_SSID_MAX+1+64];
    size_t at=pack_string(p,0,sizeof(p),ssid,MIX_SSID_MAX);
    at=pack_string(p,at,sizeof(p),passphrase,63);
    return net_request(MIX_NET_CONNECT,p,at);
}
bool mix_link_net_forget(const char *ssid){
    if(!ssid||!*ssid)return false;
    uint8_t p[1+MIX_SSID_MAX];
    size_t at=pack_string(p,0,sizeof(p),ssid,MIX_SSID_MAX);
    return net_request(MIX_NET_FORGET,p,at);
}
int mix_link_net_list(const mix_net_entry_t **out){if(out)*out=net_list;return net_count;}
bool mix_link_net_busy(void){return net_session!=0;}
const char *mix_link_net_message(void){return net_message;}
bool mix_link_input(const uint8_t *b,size_t n){
    if(!terminal_open||pending_update||ota_session||!n)return false;
    if(n>MIX_MAX_PAYLOAD||!enqueue(MIX_CH_TERMINAL,MIX_INPUT,session,b,n)){mix_link_close_terminal();return false;}return true;
}
void mix_link_job(bool start){
    if(!online||pending_update||ota_session)return;
    if(start&&!job_running){job=0x80000000u|((job+1)&0x7fffffffu);job_percent=0;job_running=enqueue(MIX_CH_JOB,MIX_JOB_START,job,"sha256",6);}
    else if(!start&&job_running)enqueue(MIX_CH_JOB,MIX_JOB_CANCEL,job,NULL,0);
}
bool mix_link_take_boot_request(void){bool b=boot_request;boot_request=false;return b;}
bool mix_link_take_restart_request(void){bool b=restart_request;restart_request=false;return b;}
bool mix_link_take_input_reset(void){bool b=reset_input;reset_input=false;return b;}
