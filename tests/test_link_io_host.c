/* Deterministic interleavings execute production io_task and main/link code.
 * CDC/queues are in-memory only. Longjmp stops the otherwise infinite task.
 * Generation filtering in the OTA stub mirrors mix_ota_poll_reply; the real
 * worker's generation/health contract is covered by test_ota_firmware.py. */
#include <setjmp.h>
#define MIX_LINK_IO_TEST 1
#define main link_update_scenarios
#include "test_link_update_host.c"
#undef main

static jmp_buf stop_io;
static unsigned cycle,limit,write_limit,read_limit,split_at;
static unsigned delimiter_zeros,flushes,read_calls,hook_calls;
static bool hook_done,auto_host,split_written;
static const char *scenario;
static uint32_t old_epoch,new_epoch,old_model_generation;
static uint8_t wire_out[32768],wire_in[32768];
static size_t out_count,in_count;
static mix_decoder_t host_decoder;
static unsigned hello_count,identity_count,stale_count,rx_stale_after_reset;
static unsigned writes_while_gated,rx_during_pressure,completed_old_prefix;

static void host_queue(uint8_t type,uint32_t ep,uint32_t sid,uint32_t sequence){
    mix_frame_t f={.channel=type==MIX_HELLO_ACK||type==MIX_PING?MIX_CH_CONTROL:MIX_CH_MAINTENANCE,
                   .type=type,.epoch=ep,.session=sid,.sequence=sequence};
    if(type==MIX_HELLO_ACK){f.length=4;mix_put16(f.payload,512);mix_put16(f.payload+2,4096);}
    uint8_t bytes[MIX_MAX_WIRE];size_t n=mix_frame_encode(&f,bytes,sizeof(bytes));
    assert(in_count+n<=sizeof(wire_in));memcpy(wire_in+in_count,bytes,n);in_count+=n;
}
static void new_link(void){
    restart_link();new_epoch=epoch;
    uint8_t p[4];mix_put16(p,512);mix_put16(p+2,4096);
    assert(enqueue(MIX_CH_CONTROL,MIX_HELLO,0,p,4));
    /* Match main's real send schedule: an immediate duplicate HELLO must not
     * hide loss of the first new-generation frame during dequeue. */
    hello_time=now;
}
static int raw_send(QueueHandle_t q,const void *p){
    if(q->count==q->capacity)return 0;
    q->frames[q->count++]=*(const mix_frame_t*)p;return pdTRUE;
}
int xQueueSend(QueueHandle_t q,const void *p,unsigned wait){
    (void)wait;
    if(q==rxq&&!hook_done&&!strcmp(scenario,"rx-send-reset")){
        hook_done=true;new_link();
    }
    if(q==rxq&&new_epoch&&((const mix_frame_t*)p)->epoch==old_epoch)rx_stale_after_reset++;
    if(q==rxq&&delimiter_zeros)rx_during_pressure++;
    return raw_send(q,p);
}
int xQueueReceive(QueueHandle_t q,void *p,unsigned wait){
    (void)wait;
    if(q==controlq&&!hook_done&&!strcmp(scenario,"epoch-dequeue-before")){
        hook_done=true;new_link();
    }
    if(!q->count)return 0;
    *(mix_frame_t*)p=q->frames[0];memmove(q->frames,q->frames+1,--q->count*sizeof(mix_frame_t));
    if(q==controlq&&!hook_done&&!strcmp(scenario,"epoch-dequeue-after")){
        hook_done=true;new_link();
    }
    return pdTRUE;
}
bool tud_cdc_connected(void){
    if(!strcmp(scenario,"short-disconnect"))return cycle!=1;
    if(!strcmp(scenario,"persistent-disconnect"))return cycle<1||cycle>=12;
    if(!strcmp(scenario,"double-disconnect"))return cycle!=1&&cycle!=5;
    return true;
}
bool tud_mounted(void){return true;}
uint32_t tud_cdc_read(void *p,uint32_t n){
    read_calls++;
    if(!hook_done&&!strcmp(scenario,"epoch-read")){hook_done=true;new_link();}
    if(n>in_count)n=(uint32_t)in_count;
    if(n>read_limit)n=read_limit;
    memcpy(p,wire_in,n);memmove(wire_in,wire_in+n,in_count-n);in_count-=n;
    return n;
}
uint32_t tud_cdc_write(const void *p,uint32_t n){
    if(io_disconnects!=io_disconnect_ack||!transport_open||!io_epoch)writes_while_gated++;
    if(n==1&&*(const uint8_t*)p==0&&delimiter_zeros){delimiter_zeros--;return 0;}
    if(!hook_done&&!strcmp(scenario,"epoch-delimiter")&&n==1&&*(const uint8_t*)p==0){
        hook_done=true;new_link();
    }
    if(cycle==0&&n>1&&split_at&&!split_written){if(n>split_at)n=split_at;split_written=true;}
    else if(n>write_limit)n=write_limit;
    assert(out_count+n<=sizeof(wire_out));memcpy(wire_out+out_count,p,n);out_count+=n;
    mix_frame_t f;
    for(uint32_t i=0;i<n;i++)if(mix_decoder_push(&host_decoder,((const uint8_t*)p)[i],&f)){
        if(f.type==MIX_HELLO){
            hello_count++;
            if(auto_host){host_queue(MIX_HELLO_ACK,f.epoch,0,1);host_queue(MIX_OTA_IDENTIFY,f.epoch,91,2);}
        }
        if(f.type==MIX_OTA_IDENTITY){assert(f.session==91&&f.epoch==epoch);identity_count++;}
        if(f.type==MIX_OTA_RESPONSE||f.session==98){
            /* When only the final delimiter was unsent, the resynchronizing
             * zero completes bytes already sent BEFORE disconnect. This is
             * not a re-emitted response; it must retain its exact old epoch.
             * All other stale frames remain forbidden. */
            uint8_t old_wire[MIX_MAX_WIRE];
            size_t old_length=mix_frame_encode(&f,old_wire,sizeof(old_wire));
            if(!strcmp(scenario,"short-disconnect")&&f.epoch==old_epoch&&
               f.type==MIX_OTA_RESPONSE&&f.session==98&&f.length==MIX_OTA_RESPONSE_BYTES&&
               n==1&&*(const uint8_t*)p==0&&new_epoch==epoch&&new_epoch!=old_epoch&&
               io_disconnects==1&&io_disconnect_ack==1&&split_at+1==old_length)
                completed_old_prefix++;
            else stale_count++;
        }
    }
    return n;
}
void tud_cdc_write_flush(void){flushes++;}
void vTaskDelay(unsigned n){
    cycle++;now+=n;
    if(!strcmp(scenario,"rx-held-reset")&&cycle==1){
        /* IO has a decoded held frame PLUS unread bytes in its local buffer. */
        assert(rxq->count==rxq->capacity);hook_done=true;new_link();
    }
    if(!strcmp(scenario,"rx-partial-reset")&&cycle==1){
        hook_done=true;new_link();host_queue(MIX_OTA_IDENTIFY,old_epoch,98,99);
    }
    if(!strcmp(scenario,"short-disconnect")||!strcmp(scenario,"double-disconnect")){
        if(cycle==4||(cycle==8&&!strcmp(scenario,"double-disconnect"))){
            assert(io_disconnects!=io_disconnect_ack);
            /* Neither main sample observes false. The latched event is enough. */
            assert(transport_open);mix_link_tick(now,&view);new_epoch=epoch;
            assert(!online&&!ota_session&&io_disconnect_ack==io_disconnects&&epoch!=old_epoch);
        }else if(cycle>8){mix_link_tick(now,&view);}
    }else if(!strcmp(scenario,"persistent-disconnect")){
        if(cycle>=3){
            mix_link_tick(now,&view);
            if(cycle>=2&&cycle<=12){assert(!epoch&&!online&&!ota_session);assert(model_generation==old_model_generation+1);}
            if(epoch)new_epoch=epoch;
        }
    }else if(strcmp(scenario,"rx-held-reset")||cycle>1){
        mix_link_tick(now,&view);
    }
    if(cycle>=limit)longjmp(stop_io,1);
}
static void init_case(const char *name){
    scenario=name;cycle=0;limit=400;write_limit=1024;read_limit=256;split_at=0;
    delimiter_zeros=flushes=read_calls=hook_calls=0;hook_done=false;auto_host=true;split_written=false;
    out_count=in_count=0;memset(&host_decoder,0,sizeof(host_decoder));
    hello_count=identity_count=stale_count=rx_stale_after_reset=writes_while_gated=rx_during_pressure=completed_old_prefix=0;
    io_disconnects=io_disconnect_ack=0;io_epoch=0;io_fault=false;new_epoch=0;
    /* Hooks exercise IO/main interleavings, not setup's HELLO_ACK. */
    hook_done=true;setup();old_epoch=epoch;old_model_generation=model_generation;hook_done=false;
}
static void stale_tx(void){
    uint8_t p[MIX_OTA_RESPONSE_BYTES];memset(p,0xa5,sizeof(p));
    assert(enqueue(MIX_CH_MAINTENANCE,MIX_OTA_RESPONSE,98,p,sizeof(p)));
    /* Queued worker reply must be revoked separately, not relabeled with the
     * fresh epoch by ota_replies(). */
    model_reply(98,MIX_OTA_RESPONSE,p,sizeof(p));
    ota_session=98;ota_v2=true;
}
static void run_io(void){if(!setjmp(stop_io))io_task(NULL);assert(flushes==cycle);assert(!writes_while_gated);}
static void recovered(void){
    assert(hello_count>=1&&identity_count==1&&online);
    assert(!stale_count&&!pending_update&&!worker_reply_count);
    assert(io_disconnects==io_disconnect_ack);
}
int main(int argc,char **argv){
    assert(argc==2);
    if(!strcmp(argv[1],"epoch-interleavings")){
        const char *cases[]={"epoch-read","epoch-dequeue-before","epoch-dequeue-after","epoch-delimiter"};
        for(unsigned i=0;i<sizeof(cases)/sizeof(cases[0]);i++){
            init_case(cases[i]);stale_tx();run_io();assert(hook_done);recovered();
            assert(host_decoder.errors==0);
        }
    }else if(!strcmp(argv[1],"delimiter-pressure")){
        init_case("delimiter-pressure");delimiter_zeros=20;
        /* Seed an abandoned prefix in the host decoder, as in the original
         * full-FIFO delimiter reproduction. A valid delimiter must separate it. */
        mix_frame_t old={.channel=MIX_CH_CONTROL,.type=MIX_PONG,.epoch=old_epoch-1};
        uint8_t bytes[MIX_MAX_WIRE];mix_frame_encode(&old,bytes,sizeof(bytes));
        for(unsigned i=0;i<5;i++)mix_decoder_push(&host_decoder,bytes[i],&old);
        host_queue(MIX_PING,epoch,0,0);
        uint8_t p[4];mix_put16(p,512);mix_put16(p+2,4096);assert(enqueue(MIX_CH_CONTROL,MIX_HELLO,0,p,4));
        /* We already have a handshake in setup; ACK sequence must be newer. */
        rx_seen=false;
        run_io();assert(!delimiter_zeros&&rx_during_pressure&&read_calls>20);
        assert(hello_count==1&&host_decoder.errors==1);
    }else if(!strcmp(argv[1],"disconnect-every-split")){
        mix_frame_t f={.channel=MIX_CH_MAINTENANCE,.type=MIX_OTA_RESPONSE,.epoch=100,.session=98,.length=192};
        memset(f.payload,0xa5,192);uint8_t bytes[MIX_MAX_WIRE];size_t length=mix_frame_encode(&f,bytes,sizeof(bytes));
        for(unsigned split=1;split<length;split++){
            init_case("short-disconnect");split_at=split;stale_tx();
            host_queue(MIX_PREPARE_UPDATE,old_epoch,98,99);
            run_io();recovered();assert(io_disconnects==1&&model_generation==old_model_generation+2);
            assert(split_written&&host_decoder.errors<=1);
            assert(completed_old_prefix==(split+1==length));
        }
    }else if(!strcmp(argv[1],"persistent-disconnect")){
        init_case("persistent-disconnect");split_at=5;stale_tx();run_io();recovered();
        assert(io_disconnects==1&&model_generation==old_model_generation+2);
    }else if(!strcmp(argv[1],"double-disconnect")){
        init_case("double-disconnect");split_at=5;stale_tx();run_io();recovered();
        assert(io_disconnects==2&&model_generation==old_model_generation+4);
    }else if(!strcmp(argv[1],"rx-reset-interleavings")){
        init_case("rx-held-reset");
        mix_frame_t old={.channel=MIX_CH_MAINTENANCE,.type=MIX_PREPARE_UPDATE,.epoch=old_epoch,.session=98,.sequence=99};
        while(raw_send(rxq,&old)==pdTRUE){}
        host_queue(MIX_PREPARE_UPDATE,old_epoch,98,100);host_queue(MIX_ENTER_BOOT,old_epoch,98,101);
        run_io();recovered();assert(hook_done&&!rx_stale_after_reset&&!boot_request);
        init_case("rx-send-reset");host_queue(MIX_PREPARE_UPDATE,old_epoch,98,99);
        run_io();recovered();assert(hook_done&&rx_stale_after_reset==1&&!boot_request);
        init_case("rx-partial-reset");read_limit=1;host_queue(MIX_PREPARE_UPDATE,old_epoch,98,99);
        run_io();recovered();assert(hook_done&&!boot_request);
    }else if(!strcmp(argv[1],"fragmented-recovery")){
        const unsigned fragments[]={1,2,7,64,256};
        for(unsigned i=0;i<sizeof(fragments)/sizeof(fragments[0]);i++){
            init_case("short-disconnect");split_at=3;write_limit=read_limit=fragments[i];stale_tx();
            run_io();recovered();assert(host_decoder.errors<=1);
        }
    }else{fprintf(stderr,"unknown scenario %s\n",argv[1]);return 2;}
    printf("PASS %s\n",argv[1]);return 0;
}
