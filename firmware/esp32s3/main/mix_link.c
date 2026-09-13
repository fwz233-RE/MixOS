/* SPDX-License-Identifier: MIT */
#include "mix_link.h"
#include "mix_protocol.h"
#include "mix_terminal.h"
#include "mix_ota.h"
#include <string.h>
#include <stdio.h>
#include <math.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/queue.h"
#include "esp_random.h"
#include "cJSON.h"
#include "tusb.h"

static QueueHandle_t rxq,controlq,inputq;
static volatile uint32_t io_epoch;
static volatile bool transport_open,usb_mounted,io_fault;
static bool online,terminal_open,opening,reset_input,boot_request,restart_request;
static uint32_t epoch,session,next_session,rx_sequence,now,rx_time,hello_time,ping_time,status_time;
static uint32_t granted,received,pending_update,update_deadline,update_grant,job;
static bool rx_seen,job_running,credit_dirty;
static uint32_t open_time,ota_session,ota_acked,ota_chunks,restart_at;
static int ota_notified=-1;
static int job_percent;
static char notice[96];
static mix_view_t metrics;
static void set_notice(const char *s){snprintf(notice,sizeof(notice),"%s",s);}
const char *mix_link_notice(void){return notice;}

static bool enqueue(uint8_t ch,uint8_t type,uint32_t sid,const void *data,size_t len){
    if(len>MIX_MAX_PAYLOAD||!epoch)return false;
    mix_frame_t f={.channel=ch,.type=type,.epoch=epoch,.session=sid,.length=(uint16_t)len};
    if(len)memcpy(f.payload,data,len);
    QueueHandle_t q=type==MIX_INPUT?inputq:controlq;
    if(xQueueSend(q,&f,0)!=pdTRUE){io_fault=true;set_notice("Link queue full; reconnecting safely");return false;}
    return true;
}
static void clear_session(void){
    terminal_open=opening=false;credit_dirty=false;session=0;granted=received=0;reset_input=true;
    xQueueReset(inputq);
}
static void clear_ota(void){mix_ota_abort();mix_ota_reset();ota_session=ota_acked=ota_chunks=0;ota_notified=-1;}
static void restart_link(void){
    online=false;clear_session();clear_ota();job=0;job_running=false;pending_update=update_grant=0;rx_seen=false;
    epoch=esp_random();if(!epoch)epoch=1;
    xQueueReset(controlq);xQueueReset(rxq);io_epoch=epoch;io_fault=false;
    rx_time=now;hello_time=now-1000;ping_time=status_time=now;
    metrics.linux_cpu=metrics.linux_temp=NAN;
    metrics.linux_mem_used_kib=metrics.linux_mem_total_kib=metrics.linux_uptime_s=0;
}
/* Only this task touches CDC RX/TX. No terminal/UI/I2C calls inside it. */
static void io_task(void *arg){
    (void)arg;mix_decoder_t d={0};mix_frame_t rx,tx;
    uint8_t wire[MIX_MAX_WIRE],buf[256];size_t length=0,off=0;
    uint32_t generation=0,seq=0,filled=0,used=0;
    bool held=false;
    for(;;){
        transport_open=tud_cdc_connected();usb_mounted=tud_mounted();
        if(generation!=io_epoch){
            generation=io_epoch;seq=0;length=off=0;filled=used=0;held=false;memset(&d,0,sizeof(d));
            /* Abort any partial old frame at a delimiter. */
            if(transport_open){uint8_t zero=0;tud_cdc_write(&zero,1);tud_cdc_write_flush();}
        }
        if(!transport_open){length=off=0;filled=used=0;held=false;memset(&d,0,sizeof(d));vTaskDelay(pdMS_TO_TICKS(5));continue;}
        /* A decoded frame the link task has not taken yet is held here, and no
         * further CDC bytes are drained until it fits. TinyUSB then stops
         * accepting from the host, which is ordinary flow control. Faulting the
         * link instead would restart the epoch and abandon a firmware transfer
         * every time a flash erase kept the link task busy for a few
         * milliseconds, which is exactly when the queue backs up. */
        if(held&&xQueueSend(rxq,&rx,0)==pdTRUE)held=false;
        for(int batch=0;batch<4&&!held;batch++){
            if(used==filled){filled=tud_cdc_read(buf,sizeof(buf));used=0;if(!filled)break;}
            while(used<filled&&!held)
                if(mix_decoder_push(&d,buf[used++],&rx)&&xQueueSend(rxq,&rx,0)!=pdTRUE)held=true;
        }
        if(!length){
            bool sending=xQueueReceive(controlq,&tx,0)==pdTRUE;
            if(!sending)sending=xQueueReceive(inputq,&tx,0)==pdTRUE;
            if(sending&&tx.epoch==generation){tx.sequence=++seq;length=mix_frame_encode(&tx,wire,sizeof(wire));off=0;}
        }
        if(length){uint32_t n=tud_cdc_write(wire+off,(uint32_t)(length-off));off+=n;
            if(off==length)length=off=0;}
        tud_cdc_write_flush();vTaskDelay(1);
    }
}
void tud_cdc_rx_cb(uint8_t itf){(void)itf;} /* FIFO serviced by io_task, backpressure by TinyUSB */
esp_err_t mix_link_init(void){
    rxq=xQueueCreate(16,sizeof(mix_frame_t));controlq=xQueueCreate(12,sizeof(mix_frame_t));inputq=xQueueCreate(16,sizeof(mix_frame_t));
    if(!rxq||!controlq||!inputq)return ESP_ERR_NO_MEM;
    metrics.linux_cpu=metrics.linux_temp=NAN;mix_terminal_init();return ESP_OK;
}
esp_err_t mix_link_start_io(void){return xTaskCreate(io_task,"mix_usb",8192,NULL,6,NULL)==pdPASS?ESP_OK:ESP_ERR_NO_MEM;}
bool mix_link_usb_mounted(void){return usb_mounted;}
static double number(cJSON *j,const char *key){cJSON *v=cJSON_GetObjectItemCaseSensitive(j,key);return cJSON_IsNumber(v)?v->valuedouble:NAN;}
static void json_metrics(const mix_frame_t *f){
    cJSON *j=cJSON_ParseWithLength((const char*)f->payload,f->length);if(!j)return;
    metrics.linux_cpu=(float)number(j,"cpu_pct");metrics.linux_temp=(float)number(j,"temp_c");
    double a=number(j,"mem_used_kib"),b=number(j,"mem_total_kib"),c=number(j,"uptime_s");
    metrics.linux_mem_used_kib=isfinite(a)&&a>=0&&a<UINT32_MAX?(uint32_t)a:0;
    metrics.linux_mem_total_kib=isfinite(b)&&b>=0&&b<UINT32_MAX?(uint32_t)b:0;
    metrics.linux_uptime_s=isfinite(c)&&c>=0&&c<UINT32_MAX?(uint32_t)c:0;cJSON_Delete(j);
}
static void credit(void){uint8_t b[4];mix_put32(b,granted);enqueue(MIX_CH_TERMINAL,MIX_CREDIT,session,b,4);}
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
        if(f->length||pending_update||boot_request||ota_session)return;
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
    case MIX_OTA_BEGIN:{
        if(f->length!=36){maintenance_error(f->session,"OTA_BEGIN needs size and sha256");return;}
        if(ota_session&&ota_session!=f->session){maintenance_error(f->session,"another update is active");return;}
        if(pending_update||boot_request){maintenance_error(f->session,"ROM download already authorized");return;}
        if(restart_at){maintenance_error(f->session,"update already staged; reboot pending");return;}
        mix_link_close_terminal();
        esp_err_t e=mix_ota_begin(mix_get32(f->payload),f->payload+4);
        if(e!=ESP_OK){maintenance_error(f->session,mix_ota_error());clear_ota();return;}
        ota_session=f->session;ota_acked=ota_chunks=0;ota_notified=-1;
        uint8_t p[12];
        mix_put32(p,MIX_OTA_CHUNK);mix_put32(p+4,MIX_OTA_WINDOW);mix_put32(p+8,mix_get32(f->payload));
        if(!enqueue(MIX_CH_MAINTENANCE,MIX_OTA_READY,f->session,p,sizeof(p)))clear_ota();
        else set_notice("Receiving firmware update 0%");
        return;}
    case MIX_OTA_DATA:{
        if(!ota_session||f->session!=ota_session)return;
        if(f->length<5){maintenance_error(f->session,"short OTA_DATA");clear_ota();return;}
        esp_err_t e=mix_ota_write(mix_get32(f->payload),f->payload+4,f->length-4);
        if(e==ESP_ERR_INVALID_STATE&&mix_ota_state()==MIX_OTA_RECEIVING){
            ota_ack(f->session);ota_chunks=0;return; /* out of order: resynchronise */
        }
        if(e!=ESP_OK){maintenance_error(f->session,mix_ota_error());clear_ota();return;}
        if(++ota_chunks>=MIX_OTA_ACK_EVERY||mix_ota_received()==mix_ota_total()){
            ota_chunks=0;ota_acked=mix_ota_received();ota_ack(f->session);
        }
        int percent=mix_ota_percent();
        if(percent/5!=ota_notified){
            ota_notified=percent/5;
            char b[48];snprintf(b,sizeof(b),"Receiving firmware update %d%%",percent);set_notice(b);
        }
        return;}
    case MIX_OTA_STATUS:
        if(ota_session&&f->session==ota_session)ota_ack(f->session);
        return;
    case MIX_OTA_END:{
        if(!ota_session||f->session!=ota_session||f->length)return;
        esp_err_t e=mix_ota_finish();
        if(e!=ESP_OK){maintenance_error(f->session,mix_ota_error());clear_ota();return;}
        uint8_t p[4];mix_put32(p,mix_ota_total());
        enqueue(MIX_CH_MAINTENANCE,MIX_OTA_DONE,f->session,p,sizeof(p));
        /* Zero doubles as "no reboot pending", so a deadline that lands exactly
         * on the millisecond counter's wrap must not silently cancel the reboot
         * and strand a verified image that is already the boot partition. */
        ota_session=0;restart_at=now+1200;if(!restart_at)restart_at=1;
        set_notice("Update verified; restarting into the new build");
        return;}
    case MIX_OTA_ABORT:
        if(ota_session&&f->session==ota_session){clear_ota();set_notice("Firmware update cancelled");}
        return;
    default:
        return;
    }
}
static void handle(const mix_frame_t *f){
    if(f->epoch!=epoch)return;
    if(rx_seen&&(int32_t)(f->sequence-rx_sequence)<=0)return;
    rx_seen=true;rx_sequence=f->sequence;
    if(f->channel==MIX_CH_CONTROL&&f->session==0){
        if(f->type==MIX_HELLO_ACK&&f->length==4&&mix_get16(f->payload)==MIX_MAX_PAYLOAD&&mix_get16(f->payload+2)==MIX_RX_WINDOW){online=true;rx_time=now;set_notice("Linux connected");return;}
        if(!online)return;
        if(f->type==MIX_PING&&f->length==0)enqueue(MIX_CH_CONTROL,MIX_PONG,0,NULL,0);
        if(f->type==MIX_PING||f->type==MIX_PONG)rx_time=now;
    }
    if(!online)return;
    if(f->channel==MIX_CH_CONTROL&&f->type==MIX_ERROR){
        if(session&&f->session==session){clear_session();set_notice("Linux rejected terminal request");}
        else if(job&&f->session==job){job_running=false;set_notice("Linux task failed");}
        return;
    }
    if(f->channel==MIX_CH_TERMINAL&&session&&f->session==session){
        if(f->type==MIX_OPENED&&opening&&f->length==4){
            if(mix_get16(f->payload)!=MIX_TERM_COLS||mix_get16(f->payload+2)!=MIX_TERM_ROWS){mix_link_close_terminal();set_notice("Unsupported terminal geometry");return;}
            opening=false;terminal_open=true;mix_terminal_init();granted=MIX_RX_WINDOW;received=0;credit_dirty=true;reset_input=true;
        }else if(f->type==MIX_DATA&&terminal_open){
            uint32_t avail=granted-received;
            if(avail>MIX_RX_WINDOW||f->length>avail){mix_link_close_terminal();set_notice("Terminal flow control violation");return;}
            received+=f->length;mix_terminal_feed(f->payload,f->length);granted+=f->length;credit_dirty=true;
        }else if(f->type==MIX_EXIT){clear_session();set_notice("Terminal session ended");}
        else if(f->type==MIX_ERROR){clear_session();set_notice("Linux rejected terminal request");}
    }
    if(f->channel==MIX_CH_STATUS&&f->session==0&&f->type==MIX_STATUS)json_metrics(f);
    if(f->channel==MIX_CH_JOB&&job&&f->session==job){
        if(f->type==MIX_JOB_PROGRESS){cJSON *j=cJSON_ParseWithLength((const char*)f->payload,f->length);if(j){double v=number(j,"percent");if(isfinite(v)&&v>=0&&v<=100)job_percent=(int)v;cJSON_Delete(j);}}
        if(f->type==MIX_JOB_RESULT||f->type==MIX_ERROR){job_running=false;set_notice(f->type==MIX_ERROR?"Linux task failed":"Linux task completed");}
    }
    if(f->channel==MIX_CH_MAINTENANCE)maintenance(f);
}
void mix_link_tick(uint32_t now_ms,mix_view_t *v){
    now=now_ms;
    if(!transport_open){
        if(epoch){online=false;clear_session();clear_ota();epoch=io_epoch=0;pending_update=update_grant=0;job_running=false;set_notice("Linux disconnected");}
    }else{
        if(!epoch||io_fault||(uint32_t)(now-rx_time)>8000)restart_link();
        mix_frame_t f;for(int i=0;i<16&&xQueueReceive(rxq,&f,0)==pdTRUE;i++)handle(&f);
        if(!online&&(uint32_t)(now-hello_time)>=1000){uint8_t p[4];mix_put16(p,MIX_MAX_PAYLOAD);mix_put16(p+2,MIX_RX_WINDOW);enqueue(MIX_CH_CONTROL,MIX_HELLO,0,p,4);hello_time=now;}
        if(credit_dirty&&terminal_open){credit_dirty=false;credit();}
        if(opening&&(uint32_t)(now-open_time)>5000){mix_link_close_terminal();set_notice("Terminal open timed out");}
        if(online&&(uint32_t)(now-ping_time)>=2000){enqueue(MIX_CH_CONTROL,MIX_PING,0,NULL,0);ping_time=now;}
        if(online&&(uint32_t)(now-status_time)>=2000){enqueue(MIX_CH_STATUS,MIX_STATUS_REQUEST,0,NULL,0);status_time=now;}
        if(pending_update&&(int32_t)(now-update_deadline)>=0){pending_update=update_grant=0;set_notice("Host update grant expired");}
    }
    /* Report the true write position once per tick whenever it moved. The host
     * then never has to wait for a timeout because an acknowledgement landed on
     * a chunk boundary it did not predict, and a window that was cut short by
     * queue pressure reopens on the next tick instead of stalling. */
    if(ota_session&&mix_ota_state()==MIX_OTA_RECEIVING&&mix_ota_received()!=ota_acked){
        ota_acked=mix_ota_received();ota_chunks=0;ota_ack(ota_session);
    }
    mix_ota_tick(now);
    if(ota_session&&mix_ota_state()!=MIX_OTA_RECEIVING){
        maintenance_error(ota_session,mix_ota_error());set_notice(mix_ota_error());clear_ota();
    }
    if(restart_at&&(int32_t)(now-restart_at)>=0){restart_at=0;restart_request=true;}
    v->linux_online=online;v->terminal_open=terminal_open;
    v->maintenance_busy=pending_update||ota_session||restart_at;
    v->ota_state=(uint8_t)mix_ota_state();v->ota_percent=mix_ota_percent();
    v->job_running=job_running;v->job_percent=job_percent;
    v->linux_cpu=online?metrics.linux_cpu:NAN;v->linux_temp=online?metrics.linux_temp:NAN;
    v->linux_mem_used_kib=online?metrics.linux_mem_used_kib:0;v->linux_mem_total_kib=online?metrics.linux_mem_total_kib:0;
    v->linux_uptime_s=online?metrics.linux_uptime_s:0;
}
bool mix_link_open_terminal(void){
    if(!online||pending_update||ota_session)return false;if(terminal_open||opening)return true;
    session=++next_session;if(!session)session=++next_session;
    uint8_t p[4];mix_put16(p,MIX_TERM_COLS);mix_put16(p+2,MIX_TERM_ROWS);
    open_time=now;opening=enqueue(MIX_CH_TERMINAL,MIX_OPEN,session,p,4);return opening;
}
void mix_link_close_terminal(void){if(session&&online)enqueue(MIX_CH_TERMINAL,MIX_CLOSE,session,NULL,0);clear_session();}
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
