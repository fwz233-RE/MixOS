/* Exercise production link code, including queue dispatch and public view state. */
#include <assert.h>
#include <stdlib.h>
#include "../firmware/esp32s3/main/mix_link.c"

struct test_queue { mix_frame_t frames[16]; unsigned count, capacity; };
static struct test_queue queues[3];
static unsigned allocated;
QueueHandle_t xQueueCreate(unsigned n,size_t size){assert(allocated<3&&n<=16&&size==sizeof(mix_frame_t));QueueHandle_t q=&queues[allocated++];q->capacity=n;return q;}
int xQueueSend(QueueHandle_t q,const void *p,unsigned wait){(void)wait;if(q->count==q->capacity)return 0;q->frames[q->count++]=*(const mix_frame_t*)p;return pdTRUE;}
int xQueueReceive(QueueHandle_t q,void *p,unsigned wait){(void)wait;if(!q->count)return 0;*(mix_frame_t*)p=q->frames[0];memmove(q->frames,q->frames+1,--q->count*sizeof(mix_frame_t));return pdTRUE;}
void xQueueReset(QueueHandle_t q){q->count=0;}
void vTaskDelay(unsigned n){(void)n;}
int xTaskCreate(void (*fn)(void*),const char *name,unsigned stack,void *arg,unsigned priority,void *handle){(void)fn;(void)name;(void)stack;(void)arg;(void)priority;(void)handle;return pdPASS;}
uint32_t esp_random(void){static uint32_t n=100;return ++n;}
bool tud_cdc_connected(void){return false;}
bool tud_mounted(void){return false;}
uint32_t tud_cdc_read(void *p,uint32_t n){(void)p;(void)n;return 0;}
uint32_t tud_cdc_write(const void *p,uint32_t n){(void)p;return n;}
void tud_cdc_write_flush(void){}
cJSON *cJSON_ParseWithLength(const char *p,size_t n){(void)p;(void)n;return NULL;}
cJSON *cJSON_GetObjectItemCaseSensitive(cJSON *j,const char *key){(void)j;(void)key;return NULL;}
int cJSON_IsNumber(const cJSON *j){(void)j;return 0;}
void cJSON_Delete(cJSON *j){(void)j;}

/* A model of mix_ota.c: the slot, the in-order write rule and the refusal
 * reasons matter to the link; real flash does not. `ota_slot` of zero is the
 * device that still runs the factory-only partition table. */
static mix_ota_state_t ota_state;
static uint32_t ota_total,ota_got,ota_slot=0x1F0000;
static bool ota_trial,ota_was_reset;
static char ota_reason[128];
static const char *NO_SLOT="no OTA slot: device still has the factory-only partition table; run the one-time A/B migration";
void mix_ota_init(void){}
bool mix_ota_pending_verify(void){return ota_trial;}
void mix_ota_health_tick(uint32_t ms,bool healthy){(void)ms;(void)healthy;}
/* The codes are mix_ota.c's own. The link treats ESP_ERR_INVALID_STATE from a
 * still-receiving device as "resend from here" and every other code as a fatal
 * refusal, so the model has to be exact about which one it returns. */
esp_err_t mix_ota_begin(uint32_t size,const uint8_t sha[32]){
    (void)sha;
    if(ota_state==MIX_OTA_RECEIVING||ota_state==MIX_OTA_READY_TO_BOOT){snprintf(ota_reason,sizeof(ota_reason),"a transfer is already running");return ESP_ERR_INVALID_STATE;}
    if(ota_trial){snprintf(ota_reason,sizeof(ota_reason),"new build still on trial; retry in a minute");return ESP_ERR_INVALID_STATE;}
    if(!ota_slot){snprintf(ota_reason,sizeof(ota_reason),"%s",NO_SLOT);return ESP_ERR_NOT_FOUND;}
    if(size>ota_slot){snprintf(ota_reason,sizeof(ota_reason),"image exceeds slot");return ESP_ERR_INVALID_SIZE;}
    ota_state=MIX_OTA_RECEIVING;ota_total=size;ota_got=0;ota_reason[0]=0;return ESP_OK;
}
esp_err_t mix_ota_write(uint32_t offset,const uint8_t *data,size_t len){
    (void)data;
    if(ota_state!=MIX_OTA_RECEIVING){snprintf(ota_reason,sizeof(ota_reason),"no transfer is open");return ESP_ERR_INVALID_STATE;}
    if(!len||len>MIX_OTA_CHUNK){snprintf(ota_reason,sizeof(ota_reason),"chunk length out of range");return ESP_ERR_INVALID_ARG;}
    /* Deliberately leaves ota_reason alone: retransmission is not a failure. */
    if(offset!=ota_got)return ESP_ERR_INVALID_STATE;
    if(ota_got+len>ota_total){ota_state=MIX_OTA_FAILED;snprintf(ota_reason,sizeof(ota_reason),"image longer than announced");return ESP_ERR_INVALID_SIZE;}
    ota_got+=(uint32_t)len;return ESP_OK;
}
esp_err_t mix_ota_finish(void){
    if(ota_state!=MIX_OTA_RECEIVING){snprintf(ota_reason,sizeof(ota_reason),"no transfer is open");return ESP_ERR_INVALID_STATE;}
    if(ota_got!=ota_total){ota_state=MIX_OTA_FAILED;snprintf(ota_reason,sizeof(ota_reason),"incomplete image");return ESP_ERR_INVALID_SIZE;}
    ota_state=MIX_OTA_READY_TO_BOOT;return ESP_OK;
}
void mix_ota_abort(void){if(ota_state==MIX_OTA_RECEIVING){ota_state=MIX_OTA_FAILED;snprintf(ota_reason,sizeof(ota_reason),"aborted by host");ota_total=ota_got=0;}}
void mix_ota_reset(void){ota_was_reset=true;if(ota_state==MIX_OTA_FAILED){ota_state=MIX_OTA_IDLE;ota_total=ota_got=0;}}
void mix_ota_tick(uint32_t ms){(void)ms;}
mix_ota_state_t mix_ota_state(void){return ota_state;}
uint32_t mix_ota_received(void){return ota_got;}
uint32_t mix_ota_total(void){return ota_total;}
int mix_ota_percent(void){return ota_total?(int)((uint64_t)ota_got*100/ota_total):0;}
const char *mix_ota_running_slot(void){return "ota_0";}
const char *mix_ota_target_slot(void){return "ota_1";}
const char *mix_ota_error(void){return ota_reason[0]?ota_reason:"update failed for an unrecorded reason";}

static mix_view_t view;
static uint32_t seq;
static void frame(unsigned ch,unsigned type,uint32_t sid,unsigned len,uint32_t generation,uint32_t sequence){
    mix_frame_t f={.channel=ch,.type=type,.session=sid,.length=len,.epoch=generation,.sequence=sequence};
    if(type==MIX_HELLO_ACK){mix_put16(f.payload,MIX_MAX_PAYLOAD);mix_put16(f.payload+2,MIX_RX_WINDOW);}
    assert(xQueueSend(rxq,&f,0)==pdTRUE);mix_link_tick(now,&view);
}
static void send(unsigned type,uint32_t sid){frame(MIX_CH_MAINTENANCE,type,sid,0,epoch,++seq);}
/* Queue a payload-carrying maintenance frame without ticking, so a whole batch
 * can be delivered in one tick the way the io task really delivers it. */
static void queue_payload(unsigned type,uint32_t sid,const uint8_t *payload,unsigned len){
    mix_frame_t f={.channel=MIX_CH_MAINTENANCE,.type=type,.session=sid,.length=len,.epoch=epoch,.sequence=++seq};
    if(len)memcpy(f.payload,payload,len);
    assert(xQueueSend(rxq,&f,0)==pdTRUE);
}
static void ota_begin_frame(uint32_t sid,uint32_t size){
    uint8_t p[36]={0};mix_put32(p,size);queue_payload(MIX_OTA_BEGIN,sid,p,sizeof(p));mix_link_tick(now,&view);
}
static void ota_data_frame(uint32_t sid,uint32_t offset,unsigned len){
    uint8_t p[4+MIX_OTA_CHUNK]={0};mix_put32(p,offset);queue_payload(MIX_OTA_DATA,sid,p,4+len);
}
static bool saw(unsigned type,uint32_t sid,mix_frame_t *out){
    for(unsigned i=0;i<controlq->count;i++)
        if(controlq->frames[i].type==type&&controlq->frames[i].session==sid){
            if(out)*out=controlq->frames[i];return true;
        }
    return false;
}
static void reset_ota_model(void){ota_state=MIX_OTA_IDLE;ota_total=ota_got=0;ota_slot=0x1F0000;ota_trial=false;ota_was_reset=false;ota_reason[0]=0;}
static void setup(void){
    allocated=0;memset(queues,0,sizeof(queues));assert(mix_link_init()==ESP_OK);
    reset_ota_model();
    /* A staged reboot outlives restart_link() on real hardware, which is the
     * point of it. Each case here is a fresh device, so clear it explicitly
     * rather than letting one subtest's pending reboot refuse the next
     * OTA_BEGIN with "update already staged". */
    now=1000;boot_request=false;restart_request=false;restart_at=0;transport_open=true;restart_link();seq=0;
    frame(MIX_CH_CONTROL,MIX_HELLO_ACK,0,4,epoch,++seq);assert(online);xQueueReset(controlq);
}
static void no_boot(void){assert(!mix_link_take_boot_request());}
static void ready(uint32_t sid){
    send(MIX_PREPARE_UPDATE,sid);assert(pending_update==sid&&update_grant==sid);
    assert(update_deadline==now+15000);
    mix_frame_t f;assert(xQueueReceive(controlq,&f,0));
    assert(f.channel==MIX_CH_MAINTENANCE&&f.type==MIX_UPDATE_READY&&f.epoch==epoch&&f.session==sid&&f.length==0);
    assert(!xQueueReceive(controlq,&f,0));no_boot();
}
static void tick(uint32_t t){now=t;rx_time=t;mix_link_tick(t,&view);}

int main(int argc,char **argv){
    assert(argc==2);setup();
    if(!strcmp(argv[1],"automatic")){
        /* Readiness and ENTER_BOOT succeed with zero UI input. */
        ready(55);assert(!mix_link_open_terminal());send(MIX_ENTER_BOOT,55);
        assert(mix_link_take_boot_request());no_boot();assert(!pending_update&&!update_grant);
        setup();session=9;terminal_open=true;send(MIX_PREPARE_UPDATE,56);
        assert(!terminal_open&&!session&&update_grant==56);
        assert(controlq->count==2&&controlq->frames[0].type==MIX_CLOSE&&controlq->frames[0].session==9);
        assert(controlq->frames[1].type==MIX_UPDATE_READY&&controlq->frames[1].session==56);
        assert(mix_link_take_input_reset());no_boot();
        assert(update_grant==56&&controlq->count==2);send(MIX_ENTER_BOOT,56);assert(mix_link_take_boot_request());
    }else if(!strcmp(argv[1],"invalid")){
        send(MIX_ENTER_BOOT,55);no_boot();assert(!controlq->count);
        frame(4,MIX_PREPARE_UPDATE,55,1,epoch,++seq);assert(!update_grant);
        frame(4,MIX_PREPARE_UPDATE,0,0,epoch,++seq);assert(!update_grant);
        frame(3,MIX_PREPARE_UPDATE,55,0,epoch,++seq);assert(!update_grant);
        frame(4,MIX_PREPARE_UPDATE,55,0,epoch+1,++seq);assert(!update_grant);
        online=false;send(MIX_PREPARE_UPDATE,55);assert(!update_grant);online=true;xQueueReset(controlq);
        ready(55);
        frame(4,MIX_ENTER_BOOT,55,1,epoch,++seq);no_boot();
        frame(3,MIX_ENTER_BOOT,55,0,epoch,++seq);no_boot();
        frame(4,MIX_ENTER_BOOT,55,0,epoch+1,++seq);no_boot();
        send(MIX_ENTER_BOOT,0);no_boot();send(MIX_ENTER_BOOT,56);no_boot();
        uint32_t deadline=update_deadline;send(MIX_PREPARE_UPDATE,56);assert(update_grant==55&&update_deadline==deadline&&!controlq->count);
        send(MIX_ENTER_BOOT,55);assert(mix_link_take_boot_request());
    }else if(!strcmp(argv[1],"expiry")){
        ready(55);now=update_deadline;rx_time=now;send(MIX_ENTER_BOOT,55);no_boot();assert(!update_grant&&!pending_update);
        setup();ready(55);tick(update_deadline+1);send(MIX_ENTER_BOOT,55);no_boot();
        setup();tick(UINT32_MAX-10000);xQueueReset(controlq);ready(56);tick(update_deadline-1);send(MIX_ENTER_BOOT,56);assert(mix_link_take_boot_request());
        setup();tick(UINT32_MAX-10000);xQueueReset(controlq);ready(57);tick(update_deadline);send(MIX_ENTER_BOOT,57);no_boot();
    }else if(!strcmp(argv[1],"replay")){
        ready(55);uint32_t prepare_seq=seq,deadline=update_deadline;
        now++;send(MIX_PREPARE_UPDATE,55);assert(update_deadline==deadline&&!controlq->count);
        frame(4,MIX_ENTER_BOOT,55,0,epoch,prepare_seq);no_boot();
        send(MIX_ENTER_BOOT,55);assert(boot_request);uint32_t enter_seq=seq;
        send(MIX_PREPARE_UPDATE,56);assert(!update_grant&&!controlq->count);
        assert(mix_link_take_boot_request());no_boot();
        frame(4,MIX_ENTER_BOOT,55,0,epoch,enter_seq);no_boot();send(MIX_ENTER_BOOT,55);no_boot();
        frame(4,MIX_PREPARE_UPDATE,55,0,epoch,prepare_seq);assert(!update_grant);no_boot();
        ready(56);frame(4,MIX_ENTER_BOOT,56,0,epoch,enter_seq);no_boot();send(MIX_ENTER_BOOT,56);assert(mix_link_take_boot_request());
    }else if(!strcmp(argv[1],"reset")){
        ready(55);uint32_t old_epoch=epoch;transport_open=false;mix_link_tick(now,&view);assert(!update_grant&&!pending_update);
        transport_open=true;mix_link_tick(now,&view);assert(epoch!=old_epoch);send(MIX_ENTER_BOOT,55);no_boot();
        setup();ready(55);io_fault=true;mix_link_tick(now,&view);assert(!update_grant&&!online);send(MIX_ENTER_BOOT,55);no_boot();
        setup();ready(55);mix_link_tick(now+8001,&view);assert(!update_grant&&!online);no_boot();
    }else if(!strcmp(argv[1],"queue")){
        controlq->count=controlq->capacity;send(MIX_PREPARE_UPDATE,55);assert(io_fault&&!update_grant&&!pending_update);send(MIX_ENTER_BOOT,55);no_boot();
    }else if(!strcmp(argv[1],"ota")){
        /* A whole transfer, including the acknowledgement the tick owes the host. */
        mix_frame_t f;
        ota_begin_frame(70,5*MIX_OTA_CHUNK);
        assert(ota_session==70&&ota_state==MIX_OTA_RECEIVING);
        assert(saw(MIX_OTA_READY,70,&f)&&f.length==12);
        assert(mix_get32(f.payload)==MIX_OTA_CHUNK&&mix_get32(f.payload+4)==MIX_OTA_WINDOW);
        assert(mix_get32(f.payload+8)==5*MIX_OTA_CHUNK);
        for(unsigned i=0;i<3;i++)ota_data_frame(70,i*MIX_OTA_CHUNK,MIX_OTA_CHUNK);
        xQueueReset(controlq);tick(now+5);assert(ota_got==3*MIX_OTA_CHUNK);
        /* Three chunks is below MIX_OTA_ACK_EVERY and short of the end, so only
         * the per-tick acknowledgement can tell the host where the device is.
         * Without it the host waits for a position it can never predict. */
        assert(saw(MIX_OTA_ACK,70,&f)&&mix_get32(f.payload)==3*MIX_OTA_CHUNK);
        assert(ota_acked==3*MIX_OTA_CHUNK);
        xQueueReset(controlq);tick(now+5);assert(!saw(MIX_OTA_ACK,70,NULL)); /* no idle chatter */
        for(unsigned i=3;i<5;i++)ota_data_frame(70,i*MIX_OTA_CHUNK,MIX_OTA_CHUNK);
        tick(now+5);assert(ota_got==5*MIX_OTA_CHUNK);
        xQueueReset(controlq);send(MIX_OTA_END,70);
        assert(ota_state==MIX_OTA_READY_TO_BOOT&&!ota_session&&restart_at==now+1200);
        assert(saw(MIX_OTA_DONE,70,&f)&&mix_get32(f.payload)==5*MIX_OTA_CHUNK);
        assert(view.maintenance_busy&&!mix_link_take_restart_request());
        tick(restart_at);assert(mix_link_take_restart_request()&&!restart_at);

        /* Out-of-order data is answered with the true position, not an error. */
        setup();ota_begin_frame(71,4*MIX_OTA_CHUNK);xQueueReset(controlq);
        ota_data_frame(71,0,MIX_OTA_CHUNK);ota_data_frame(71,2*MIX_OTA_CHUNK,MIX_OTA_CHUNK);
        tick(now+5);assert(ota_state==MIX_OTA_RECEIVING&&ota_got==MIX_OTA_CHUNK);
        assert(!saw(MIX_ERROR,71,NULL));
        assert(saw(MIX_OTA_ACK,71,&f)&&mix_get32(f.payload)==MIX_OTA_CHUNK);

        /* The factory-only device must say so in the refusal itself. */
        setup();ota_slot=0;ota_begin_frame(72,4096);
        assert(!ota_session&&saw(MIX_ERROR,72,&f)&&f.length);
        assert(!memcmp(f.payload,"no OTA slot",11));
        assert(ota_state==MIX_OTA_IDLE);

        /* A failure is reported once, then the view stops advertising it. */
        setup();ota_begin_frame(73,2*MIX_OTA_CHUNK);xQueueReset(controlq);
        ota_state=MIX_OTA_FAILED;snprintf(ota_reason,sizeof(ota_reason),"sha256 mismatch");
        tick(now+5);
        assert(saw(MIX_ERROR,73,&f)&&f.length==15&&!memcmp(f.payload,"sha256 mismatch",15));
        assert(!ota_session&&ota_was_reset&&view.ota_state==MIX_OTA_IDLE&&!view.maintenance_busy);

        /* A transfer owns the link: terminal and job requests wait. */
        setup();ota_begin_frame(74,2*MIX_OTA_CHUNK);
        assert(!mix_link_open_terminal()&&!mix_link_input((const uint8_t*)"x",1));
        mix_link_job(true);assert(!job_running);
        assert(view.maintenance_busy);
        send(MIX_OTA_ABORT,74);assert(!ota_session&&ota_state==MIX_OTA_IDLE);
        xQueueReset(controlq);assert(mix_link_open_terminal());

        /* A disconnect abandons the transfer without stranding the state. */
        setup();ota_begin_frame(75,2*MIX_OTA_CHUNK);
        transport_open=false;mix_link_tick(now,&view);
        assert(!ota_session&&ota_state==MIX_OTA_IDLE&&!view.maintenance_busy);
    }else assert(0);
    puts("production maintenance tests passed");return 0;
}
