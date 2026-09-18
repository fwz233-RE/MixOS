/* Real mix_ota.c + mix_ota_tx.c, injected SDK faults and real OpenSSL SHA256. */
#include <assert.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <openssl/sha.h>
#include "mix_ota.h"
#include "mix_ota_tx.h"
#include "mix_health.h"
#include "mix_protocol.h"
#include "esp_ota_ops.h"
#include "esp_app_desc.h"
#include "nvs.h"
#include "freertos/queue.h"
#define APP_SIZE 2048u
static unsigned char flash[2][0x1f0000], image[APP_SIZE], digest[32];
static esp_partition_t parts[3]={{0x10000,0x1f0000,0x10,"ota_0"},{0x610000,0x1f0000,0x11,"ota_1"},{0x200000,0x2000,0,"otadata"}};
static unsigned running,boot,write_slot,write_pos,image_state=2;
static unsigned begins,writes,ends,selects,aborts,marks,restarts,invalids;
static bool handle_open,fail_end,fail_select,select_then_fail,fail_read,fail_mark,fail_state,fail_nvs,corrupt_flash,lose_link_on_read;
static unsigned fail_commit_at,commits;
static uint32_t clock_ms=100;
static unsigned char persisted[256],staged[256];static size_t persisted_size,staged_size;
static uint8_t released,staged_release;static bool release_pending;
static esp_app_desc_t desc={.date="Sep 16 2026",.time="13:31:00",.project_name="mixos_esp32s3"};
struct test_queue{size_t size;unsigned capacity,count;unsigned char data[16][sizeof(mix_ota_reply_t)];};
static struct test_queue queues[20];static unsigned allocated;
QueueHandle_t xQueueCreate(unsigned n,size_t size){assert(allocated<20&&n<=16&&size<=sizeof(mix_ota_reply_t));QueueHandle_t q=&queues[allocated++];memset(q,0,sizeof(*q));q->capacity=n;q->size=size;return q;}
int xQueueSend(QueueHandle_t q,const void *p,unsigned wait){(void)wait;if(q->count==q->capacity)return 0;memcpy(q->data[q->count++],p,q->size);return 1;}
int xQueueReceive(QueueHandle_t q,void *p,unsigned wait){(void)wait;if(!q->count)return 0;memcpy(p,q->data[0],q->size);--q->count;for(unsigned i=0;i<q->count;i++)memcpy(q->data[i],q->data[i+1],q->size);return 1;}
void xQueueReset(QueueHandle_t q){q->count=0;}
unsigned uxQueueSpacesAvailable(QueueHandle_t q){return q->capacity-q->count;}
int xTaskCreate(void(*fn)(void*),const char*n,unsigned s,void*a,unsigned p,void*h){(void)fn;(void)n;(void)s;(void)a;(void)p;(void)h;return 1;}
void vTaskDelay(unsigned n){clock_ms+=n;}void vTaskDelete(void*p){(void)p;}
esp_err_t mix_watchdog_task_begin(mix_health_task_t r){(void)r;return 0;}
esp_err_t mix_watchdog_task_reset(mix_health_task_t r){(void)r;return 0;}
esp_err_t mix_watchdog_task_end(mix_health_task_t r){(void)r;return 0;}
uint32_t esp_random(void){static unsigned x=1234;return ++x;}
int64_t esp_timer_get_time(void){return (int64_t)clock_ms*1000;}
void esp_restart(void){restarts++;}unsigned esp_reset_reason(void){return 3;}
const char *esp_err_to_name(esp_err_t e){return e==0?"ESP_OK":"injected error";}
const esp_app_desc_t *esp_app_get_description(void){return &desc;}
const esp_partition_t *esp_partition_find_first(unsigned type,unsigned sub,const char *label){(void)label;if(type==1)return sub==0?&parts[2]:NULL;return sub>=0x10&&sub<=0x11?&parts[sub-0x10]:NULL;}
const esp_partition_t *esp_ota_get_running_partition(void){return &parts[running];}
const esp_partition_t *esp_ota_get_boot_partition(void){return &parts[boot];}
const esp_partition_t *esp_ota_get_next_update_partition(const esp_partition_t*p){(void)p;return &parts[1-running];}
esp_err_t esp_ota_get_state_partition(const esp_partition_t*p,esp_ota_img_states_t*out){(void)p;if(fail_state)return ESP_FAIL;*out=(esp_ota_img_states_t)image_state;return 0;}
esp_err_t esp_ota_begin(const esp_partition_t*p,size_t size,esp_ota_handle_t*h){(void)size;assert(!handle_open);begins++;write_slot=p->subtype-0x10;assert(write_slot!=running);write_pos=0;*h=99;handle_open=true;return 0;}
esp_err_t esp_ota_write(esp_ota_handle_t h,const void*p,size_t n){assert(h==99&&handle_open);writes++;memcpy(flash[write_slot]+write_pos,p,n);write_pos+=(unsigned)n;return 0;}
esp_err_t esp_ota_abort(esp_ota_handle_t h){assert(h==99&&handle_open);aborts++;handle_open=false;return 0;}
esp_err_t esp_ota_end(esp_ota_handle_t h){assert(h==99&&handle_open);ends++;handle_open=false;if(corrupt_flash)flash[write_slot][17]^=1;return fail_end?ESP_FAIL:0;}
esp_err_t esp_partition_read(const esp_partition_t*p,size_t offset,void*out,size_t n){if(fail_read)return ESP_FAIL;if(lose_link_on_read){lose_link_on_read=false;mix_ota_link_lost();}assert(offset+n<=p->size);memcpy(out,flash[p->subtype-0x10]+offset,n);return 0;}
esp_err_t esp_ota_set_boot_partition(const esp_partition_t*p){selects++;if(!fail_select||select_then_fail)boot=p->subtype-0x10;return fail_select?ESP_FAIL:0;}
esp_err_t esp_ota_mark_app_valid_cancel_rollback(void){marks++;if(fail_mark)return ESP_FAIL;image_state=2;return 0;}
esp_err_t esp_ota_mark_app_invalid_rollback_and_reboot(void){invalids++;return ESP_FAIL;}
esp_err_t nvs_open(const char*n,unsigned mode,nvs_handle_t*h){assert(!strcmp(n,"mix_ota")&&mode==1);*h=1;return fail_nvs?ESP_FAIL:0;}
esp_err_t nvs_get_blob(nvs_handle_t h,const char*n,void*out,size_t*size){(void)h;assert(!strcmp(n,"txn"));if(!persisted_size)return ESP_ERR_NVS_NOT_FOUND;assert(*size>=persisted_size);*size=persisted_size;memcpy(out,persisted,persisted_size);return 0;}
esp_err_t nvs_set_blob(nvs_handle_t h,const char*n,const void*p,size_t size){(void)h;assert(!strcmp(n,"txn")&&size<=sizeof(staged));if(fail_nvs)return ESP_FAIL;staged_size=size;memcpy(staged,p,size);return 0;}
esp_err_t nvs_commit(nvs_handle_t h){(void)h;commits++;if(fail_nvs||(fail_commit_at&&commits==fail_commit_at))return ESP_FAIL;if(staged_size){memcpy(persisted,staged,staged_size);persisted_size=staged_size;staged_size=0;}if(release_pending){released=staged_release;release_pending=false;}return 0;}
esp_err_t nvs_get_u8(nvs_handle_t h,const char*n,uint8_t*out){(void)h;assert(!strcmp(n,"released"));if(!released)return ESP_ERR_NVS_NOT_FOUND;*out=released;return 0;}
esp_err_t nvs_set_u8(nvs_handle_t h,const char*n,uint8_t v){(void)h;assert(!strcmp(n,"released"));staged_release=v;release_pending=true;return fail_nvs?ESP_FAIL:0;}
static uint32_t get_boot_id(void){uint8_t c[36];assert(mix_ota_capabilities(c,sizeof(c))==36);return mix_get32(c+8);}
static uint8_t req[72];static uint32_t reqid;
static void request_init(uint8_t op){memset(req,0,sizeof(req));req[0]=2;req[1]=op;mix_put32(req+4,++reqid);memset(req+8,0xa5,16);memcpy(req+24,digest,32);mix_put32(req+56,APP_SIZE);mix_put32(req+60,get_boot_id());req[64]=1;}
static mix_ota_reply_t request(uint8_t op){req[1]=op;mix_put32(req+4,++reqid);assert(mix_ota_submit_request(77,req,72));while(mix_ota_worker_step()){}mix_ota_reply_t r={0},last={0};while(mix_ota_poll_reply(&r))if(r.type==MIX_OTA_RESPONSE&&mix_get32(r.payload+4)==reqid)last=r;assert(last.length==192);return last;}
static void receive_bound_image(void){mix_ota_reply_t r=request(MIX_TX_BEGIN);assert(r.payload[3]==MIX_TX_OK&&r.payload[2]==MIX_TX_RECEIVING);for(unsigned at=0;at<APP_SIZE;){unsigned n=APP_SIZE-at;if(n>508)n=508;uint8_t p[512];mix_put32(p,at);memcpy(p+4,image+at,n);assert(mix_ota_submit_legacy(MIX_OTA_DATA,77,p,n+4));assert(mix_ota_worker_step());mix_ota_reply_t ack;while(mix_ota_poll_reply(&ack)){assert(ack.type==MIX_OTA_ACK);assert(mix_get32(ack.payload)==at+n);}at+=n;}assert(mix_ota_received()==APP_SIZE);}
static void receive_image(void){request_init(MIX_TX_BEGIN);receive_bound_image();}
static void release_request_init(void){
    /* Independently pinned recovery read-back SHA, NOT MIX_BASELINE_SHA/HEX.
     * Copying the production array here hid its historical transcription bug. */
    static const char verified_sha_hex[]="7875d9a513acb95463b72e785ebd160c70d03f85e965c3a30a93d954bb4cff5f";
    request_init(MIX_TX_RELEASE_BASELINE);req[64]=0;mix_put32(req+56,894560u);
    for(unsigned i=0;i<32;i++){unsigned b;assert(sscanf(verified_sha_hex+2*i,"%2x",&b)==1);req[24+i]=(uint8_t)b;}
}
static uint32_t health_challenge(mix_ota_reply_t r){
    unsigned value=0;assert(r.payload[3]==MIX_TX_OK&&(mix_get32(r.payload+76)&MIX_OTA_FLAG_HEALTH_CHALLENGE));
    assert(sscanf((char*)r.payload+144,"health-challenge:%8x",&value)==1&&value);return value;
}
static void prepare_health_ack(void){
    request_init(MIX_TX_VERIFY_RUNNING);req[64]=(uint8_t)running;
    SHA256(flash[running],APP_SIZE,req+24);
    mix_ota_reply_t measured=request(MIX_TX_VERIFY_RUNNING);
    uint32_t token=health_challenge(measured);
    memcpy(req+24,desc.app_elf_sha256,32);mix_put32(req+68,token);req[1]=MIX_TX_HEALTH_ACK;
}
static void authorize_health(void){
    prepare_health_ack();mix_ota_reply_t ack=request(MIX_TX_HEALTH_ACK);
    assert(ack.payload[3]==MIX_TX_OK&&(mix_get32(ack.payload+76)&MIX_OTA_FLAG_HEALTH_ACKED));
    request_init(MIX_TX_QUERY);
}
static void health(unsigned duration,bool local,bool host){for(unsigned i=0;i<duration;i+=100){clock_ms+=100;mix_ota_local_health_tick(clock_ms,local);mix_ota_health_tick(clock_ms,host);mix_ota_worker_step();}}
int main(int argc,char **argv){
    assert(argc==2);for(unsigned i=0;i<32;i++)desc.app_elf_sha256[i]=(uint8_t)i;for(unsigned i=0;i<APP_SIZE;i++)image[i]=(uint8_t)(i*13u+7u);SHA256(image,APP_SIZE,digest);memset(flash,0xcc,sizeof(flash));
    if(!strcmp(argv[1],"unknown-state"))fail_state=true;
    if(!strcmp(argv[1],"health-journal-unavailable"))fail_nvs=true;
    if(!strcmp(argv[1],"pending")||!strcmp(argv[1],"mark-failure")||!strcmp(argv[1],"local-failure")||!strcmp(argv[1],"host-absent")||!strncmp(argv[1],"health-",7))image_state=1;
    mix_ota_init();assert(mix_ota_init_worker()==0);
    if(!strcmp(argv[1],"rpc")||!strcmp(argv[1],"rpc-bootstrap")){
        if(!strcmp(argv[1],"rpc-bootstrap")){
            running=boot=1;image_state=1;memcpy(flash[1],image,APP_SIZE);mix_ota_test_reboot();
        }
        char line[1300];
        while(fgets(line,sizeof(line),stdin)){
            unsigned type,sid,advance;char hex[1100]={0};
            if(!strncmp(line,"BOOT",4)){running=boot;image_state=1;mix_ota_test_reboot();puts("READY");fflush(stdout);continue;}
            if(sscanf(line,"HEALTH %u",&advance)==1){health(advance,true,true);puts("READY");fflush(stdout);continue;}
            if(!strncmp(line,"CAPS",4)){
                uint8_t c[36];assert(mix_ota_capabilities(c,sizeof(c))==36);printf("78 ");for(unsigned i=0;i<36;i++)printf("%02x",c[i]);puts("");puts("READY");fflush(stdout);continue;
            }
            assert(sscanf(line,"%u %u %1099s",&type,&sid,hex)==3);size_t len=strlen(hex)/2;uint8_t p[512];assert(len<=512);
            for(size_t i=0;i<len;i++){unsigned b;assert(sscanf(hex+2*i,"%2x",&b)==1);p[i]=(uint8_t)b;}
            if(type==MIX_OTA_REQUEST)assert(mix_ota_submit_request(sid,p,len));else assert(mix_ota_submit_legacy((uint8_t)type,sid,p,len));
            while(mix_ota_worker_step()){}
            mix_ota_reply_t r;while(mix_ota_poll_reply(&r)){printf("%u ",r.type);for(unsigned i=0;i<r.length;i++)printf("%02x",r.payload[i]);puts("");}
            puts("READY");fflush(stdout);
        }
    }else if(!strcmp(argv[1],"success")){
        receive_image();mix_ota_reply_t r=request(MIX_TX_END);assert(r.payload[2]==MIX_TX_BOOT_SELECTED&&r.payload[3]==0&&ends==1&&selects==1&&boot==1);
        assert(!memcmp(r.payload+80,digest,32));r=request(MIX_TX_END);assert(ends==1&&selects==1&&r.payload[3]==0);
        r=request(MIX_TX_REBOOT);assert(r.payload[3]==0);uint32_t deadline=clock_ms+1200;clock_ms+=100;request(MIX_TX_REBOOT);clock_ms=deadline;assert(mix_ota_take_worker_restart());assert(!mix_ota_take_worker_restart());
        running=1;image_state=1;mix_ota_test_reboot();r=request(MIX_TX_QUERY);assert(r.payload[2]==MIX_TX_RUNNING_PENDING);request(MIX_TX_REBOOT);assert(!mix_ota_take_worker_restart());
        authorize_health();health(20500,true,true);assert(marks==1&&mix_ota_running_state()==2&&!mix_ota_pending_verify());r=request(MIX_TX_QUERY);assert(r.payload[2]==MIX_TX_CONFIRMED);
        request_init(MIX_TX_VERIFY_RUNNING);r=request(MIX_TX_VERIFY_RUNNING);assert(r.payload[3]==0&&(mix_get32(r.payload+76)&2));
        assert(begins==1&&flash[0][0]==0xcc);
    }else if(!strcmp(argv[1],"end-failure")){
        receive_image();fail_end=true;mix_ota_reply_t r=request(MIX_TX_END);assert(r.payload[2]==MIX_TX_FAILED&&ends==1&&!selects&&!aborts&&boot==0);
    }else if(!strcmp(argv[1],"flash-corruption")){
        receive_image();corrupt_flash=true;request(MIX_TX_END);assert(!selects&&boot==0);
    }else if(!strcmp(argv[1],"sha-mismatch")){
        digest[0]^=1;receive_image();request(MIX_TX_END);assert(!ends&&!selects&&aborts==1&&boot==0);
    }else if(!strcmp(argv[1],"select-failure")){
        receive_image();fail_select=true;mix_ota_reply_t r=request(MIX_TX_END);assert(r.payload[2]==MIX_TX_FAILED&&boot==0&&ends==1&&selects==1);
    }else if(!strcmp(argv[1],"select-uncertain")){
        receive_image();fail_select=select_then_fail=true;mix_ota_reply_t r=request(MIX_TX_END);assert(r.payload[2]==MIX_TX_BOOT_SELECTED&&boot==1);
    }else if(!strcmp(argv[1],"reboot-journal-set-failure")||!strcmp(argv[1],"reboot-journal-commit-failure")){
        receive_image();mix_ota_reply_t r=request(MIX_TX_END);
        assert(r.payload[2]==MIX_TX_BOOT_SELECTED&&boot==1);
        if(!strcmp(argv[1],"reboot-journal-set-failure"))fail_nvs=true;
        else fail_commit_at=commits+1;
        r=request(MIX_TX_REBOOT);
        assert(r.payload[3]==MIX_TX_ERROR&&r.payload[2]==MIX_TX_REBOOT_REQUESTED);
        assert(!(mix_get32(r.payload+76)&1u)); /* failed durability is published */
        r=request(MIX_TX_QUERY);
        assert(!(mix_get32(r.payload+76)&1u)&&boot==1&&running==0);
        uint8_t caps[36];assert(mix_ota_capabilities(caps,sizeof(caps))==36);
        assert(!(caps[1]&1u));
        clock_ms+=5000;assert(!mix_ota_take_worker_restart()&&!restarts);
        fail_nvs=false;fail_commit_at=0;r=request(MIX_TX_REBOOT);
        assert(r.payload[3]==MIX_TX_ERROR&&!(mix_get32(r.payload+76)&1u));
        clock_ms+=5000;assert(!mix_ota_take_worker_restart()&&!restarts);
        assert(boot==1&&selects==1); /* selected image is not silently cancelled */
    }else if(!strcmp(argv[1],"journal-before-select")){
        receive_image();fail_commit_at=commits+2;request(MIX_TX_END);assert(!selects&&boot==0);
    }else if(!strcmp(argv[1],"journal-after-select")){
        receive_image();fail_commit_at=commits+3;mix_ota_reply_t r=request(MIX_TX_END);assert(selects==1&&boot==1&&r.payload[2]==MIX_TX_BOOT_SELECTED&&!(mix_get32(r.payload+76)&1));
        fail_commit_at=0;running=1;image_state=1;mix_ota_test_reboot();r=request(MIX_TX_QUERY);assert(r.payload[2]==MIX_TX_RUNNING_PENDING);
    }else if(!strcmp(argv[1],"idempotent-conflict")){
        receive_image();unsigned before=writes;mix_ota_reply_t r=request(MIX_TX_BEGIN);assert(r.payload[3]==0&&begins==1&&writes==before);req[24]^=1;r=request(MIX_TX_BEGIN);assert(r.payload[3]==MIX_TX_CONFLICT&&mix_ota_state()==MIX_OTA_RECEIVING&&begins==1&&!aborts);
    }else if(!strcmp(argv[1],"link-loss")){
        receive_image();mix_ota_link_lost();mix_ota_worker_step();mix_ota_reply_t r=request(MIX_TX_QUERY);assert(r.payload[2]==MIX_TX_ABORTED&&aborts==1&&!selects);
    }else if(!strcmp(argv[1],"pending")){
        request_init(MIX_TX_BEGIN);mix_ota_reply_t r=request(MIX_TX_BEGIN);assert(r.payload[3]==MIX_TX_REFUSED&&!begins);
    }else if(!strcmp(argv[1],"unknown-state")){
        assert(mix_ota_pending_verify());request_init(MIX_TX_BEGIN);request(MIX_TX_BEGIN);assert(!begins&&!marks);
    }else if(!strcmp(argv[1],"mark-failure")){
        fail_mark=true;authorize_health();health(25000,true,true);assert(marks>=3&&restarts&&mix_ota_running_state()!=2&&mix_ota_pending_verify());
    }else if(!strcmp(argv[1],"local-failure")){
        health(120500,false,true);assert(invalids&&restarts&&!marks);
    }else if(!strcmp(argv[1],"host-absent")){
        health(120500,true,false);assert(!invalids&&!restarts&&!marks&&mix_ota_pending_verify());
    }else if(!strcmp(argv[1],"protect-baseline")){
        running=boot=1;mix_ota_init();request_init(MIX_TX_BEGIN);req[64]=0;mix_ota_reply_t r=request(MIX_TX_BEGIN);assert(r.payload[3]==MIX_TX_REFUSED&&!begins&&mix_ota_baseline_protected());
    }else if(!strcmp(argv[1],"no-journal-verify")){
        running=boot=1;memcpy(flash[1],image,APP_SIZE);mix_ota_test_reboot();
        request_init(MIX_TX_VERIFY_RUNNING);mix_ota_reply_t r=request(MIX_TX_VERIFY_RUNNING);
        assert(r.payload[3]==MIX_TX_OK&&r.payload[2]==MIX_TX_CONFIRMED&&mix_get32(r.payload+60)==APP_SIZE);
        assert(mix_get32(r.payload+76)&2);assert(!persisted_size&&!memcmp(r.payload+80,digest,32));
        memset(req+8,0,16);r=request(MIX_TX_QUERY);assert(r.payload[2]==MIX_TX_IDLE&&!persisted_size);
    }else if(!strcmp(argv[1],"release-requires-matched-hash")){
        running=boot=1;memcpy(flash[1],image,APP_SIZE);mix_ota_test_reboot();
        request_init(MIX_TX_VERIFY_RUNNING);req[24]^=1;mix_ota_reply_t r=request(MIX_TX_VERIFY_RUNNING);
        assert(r.payload[3]==MIX_TX_ERROR&&!(mix_get32(r.payload+76)&2));
        release_request_init();r=request(MIX_TX_RELEASE_BASELINE);assert(r.payload[3]==MIX_TX_REFUSED&&!released&&mix_ota_baseline_protected());
    }else if(!strcmp(argv[1],"release-known-baseline")||!strcmp(argv[1],"release-rejects-obsolete-digest")||
             !strcmp(argv[1],"release-requires-measurement")){
        running=boot=1;memcpy(flash[1],image,APP_SIZE);mix_ota_test_reboot();
        assert(mix_ota_baseline_protected()&&!released&&mix_ota_running_state()==2);
        mix_ota_reply_t r;
        if(!strcmp(argv[1],"release-requires-measurement")){
            unsigned before=commits;
            release_request_init();r=request(MIX_TX_RELEASE_BASELINE);
            assert(r.payload[3]==MIX_TX_REFUSED&&!released&&mix_ota_baseline_protected()&&commits==before);
        }
        request_init(MIX_TX_VERIFY_RUNNING);r=request(MIX_TX_VERIFY_RUNNING);
        assert(r.payload[3]==MIX_TX_OK&&(mix_get32(r.payload+76)&2)); /* exact running hash */
        assert(!memcmp(r.payload+80,digest,32));
        if(!strcmp(argv[1],"release-rejects-obsolete-digest")){
            /* Historical malformed array, preserved only as a rejection fixture. */
            static const unsigned char obsolete_sha[32]={
                0x78,0x75,0xd9,0xa5,0x13,0xac,0xb9,0x54,0x63,0xb7,0x2e,0x78,0x5e,0xb1,0x60,0xc7,
                0x0d,0x03,0xf8,0x5e,0x96,0x5c,0x3a,0x30,0xa9,0x3d,0x95,0x4b,0xb4,0xcf,0xf5,0x5f};
            unsigned before=commits;
            release_request_init();memcpy(req+24,obsolete_sha,32);r=request(MIX_TX_RELEASE_BASELINE);
            assert(r.payload[3]==MIX_TX_REFUSED&&!released&&mix_ota_baseline_protected()&&commits==before);
        }
        unsigned before=commits;
        release_request_init();r=request(MIX_TX_RELEASE_BASELINE);
        assert(r.payload[3]==MIX_TX_OK&&released&&!mix_ota_baseline_protected()&&commits==before+1);
        mix_ota_test_reboot();assert(released&&!mix_ota_baseline_protected());
        assert(!begins&&!writes&&!ends&&!selects&&!aborts&&running==1&&boot==1);
        for(unsigned i=0;i<sizeof(flash[0]);i++)assert(flash[0][i]==0xcc);
        assert(!memcmp(flash[1],image,APP_SIZE));
    }else if(!strcmp(argv[1],"release-durability")){
        running=boot=1;memcpy(flash[1],image,APP_SIZE);mix_ota_test_reboot();
        request_init(MIX_TX_VERIFY_RUNNING);request(MIX_TX_VERIFY_RUNNING);
        fail_commit_at=commits+1;release_request_init();mix_ota_reply_t r=request(MIX_TX_RELEASE_BASELINE);
        assert(r.payload[3]==MIX_TX_ERROR&&!released&&mix_ota_baseline_protected());
    }else if(!strcmp(argv[1],"round-trip")){
        receive_image();request(MIX_TX_END);running=boot=1;image_state=1;mix_ota_test_reboot();
        assert(mix_ota_state()==MIX_OTA_IDLE&&mix_ota_received()==0);authorize_health();health(20500,true,true);
        request_init(MIX_TX_VERIFY_RUNNING);request(MIX_TX_VERIFY_RUNNING);
        release_request_init();mix_ota_reply_t r=request(MIX_TX_RELEASE_BASELINE);
        assert(r.payload[3]==MIX_TX_OK&&released&&!mix_ota_baseline_protected());
        mix_ota_test_reboot();assert(!mix_ota_baseline_protected());
        image[0]^=1;SHA256(image,APP_SIZE,digest);request_init(MIX_TX_BEGIN);req[8]^=1;req[64]=0;
        receive_bound_image();r=request(MIX_TX_END);assert(r.payload[2]==MIX_TX_BOOT_SELECTED&&boot==0&&begins==2);
        running=0;image_state=1;mix_ota_test_reboot();authorize_health();health(20500,true,true);
        r=request(MIX_TX_QUERY);assert(r.payload[2]==MIX_TX_CONFIRMED&&r.payload[69]==0&&r.payload[71]==2);
        assert(flash[1][0]!=(uint8_t)image[0]&&!memcmp(flash[0],image,APP_SIZE));
    }else if(!strcmp(argv[1],"late-data")){
        receive_image();request(MIX_TX_END);uint8_t p[5]={0};
        assert(mix_ota_submit_legacy(MIX_OTA_DATA,77,p,sizeof(p)));mix_ota_worker_step();
        mix_ota_reply_t r=request(MIX_TX_QUERY);assert(r.payload[2]==MIX_TX_BOOT_SELECTED&&selects==1&&ends==1&&!aborts);
    }else if(!strcmp(argv[1],"legacy-late-end")){
        uint8_t begin[36];mix_put32(begin,APP_SIZE);memcpy(begin+4,digest,32);
        assert(mix_ota_submit_legacy(MIX_OTA_BEGIN,77,begin,sizeof(begin)));mix_ota_worker_step();
        assert(mix_ota_submit_legacy(MIX_OTA_ABORT,77,NULL,0));mix_ota_worker_step();
        assert(mix_ota_submit_legacy(MIX_OTA_END,77,NULL,0));mix_ota_worker_step();
        request_init(MIX_TX_QUERY);memset(req+8,0,16);mix_ota_reply_t r=request(MIX_TX_QUERY);
        assert(r.payload[2]==MIX_TX_ABORTED&&!ends&&!selects);
    }else if(!strcmp(argv[1],"reset-receiving")){
        receive_image();handle_open=false;mix_ota_test_reboot();mix_ota_reply_t r=request(MIX_TX_QUERY);
        assert(r.payload[2]==MIX_TX_ABORTED&&mix_ota_state()==MIX_OTA_IDLE&&!aborts&&!selects);
    }else if(!strcmp(argv[1],"health-heartbeat-only")){
        for(unsigned i=0;i<130;i++){uint8_t id[136];assert(mix_ota_identity(id,sizeof(id))==136);health(1000,true,true);}
        assert(!marks&&!invalids&&!restarts&&mix_ota_pending_verify());
    }else if(!strcmp(argv[1],"health-measurement-only")){
        prepare_health_ack();health(130000,true,true);assert(!marks&&!invalids&&!restarts);
    }else if(!strcmp(argv[1],"health-ack")){
        health(50000,true,true);assert(!marks);prepare_health_ack();
        mix_ota_reply_t r=request(MIX_TX_HEALTH_ACK);assert(r.payload[3]==MIX_TX_OK);
        health(10000,true,true);r=request(MIX_TX_HEALTH_ACK);assert(r.payload[3]==MIX_TX_OK);
        health(9900,true,true);assert(!marks);health(200,true,true);assert(marks==1);
        r=request(MIX_TX_HEALTH_ACK);assert(r.payload[3]==MIX_TX_OK&&marks==1);
    }else if(!strcmp(argv[1],"health-bad-elf")||!strcmp(argv[1],"health-bad-size")||
             !strcmp(argv[1],"health-bad-boot")||!strcmp(argv[1],"health-bad-token")||
             !strcmp(argv[1],"health-bad-id")||!strcmp(argv[1],"health-bad-slot")||
             !strcmp(argv[1],"health-bad-request")){
        prepare_health_ack();
        if(!strcmp(argv[1],"health-bad-elf"))req[24]^=1;
        if(!strcmp(argv[1],"health-bad-size"))req[56]^=1;
        if(!strcmp(argv[1],"health-bad-boot"))req[60]^=1;
        if(!strcmp(argv[1],"health-bad-token"))req[68]^=0x80;
        if(!strcmp(argv[1],"health-bad-id"))req[8]^=1;
        if(!strcmp(argv[1],"health-bad-slot"))req[64]^=1;
        if(!strcmp(argv[1],"health-bad-request"))reqid-=2;
        mix_ota_reply_t r=request(MIX_TX_HEALTH_ACK);assert(r.payload[3]==MIX_TX_REFUSED);
        health(25000,true,true);assert(!marks);
    }else if(!strcmp(argv[1],"health-wrong-hash")){
        prepare_health_ack();request(MIX_TX_HEALTH_ACK);
        request_init(MIX_TX_VERIFY_RUNNING);req[64]=(uint8_t)running;memset(req+24,0,32);
        mix_ota_reply_t r=request(MIX_TX_VERIFY_RUNNING);assert(r.payload[3]==MIX_TX_ERROR&&!(mix_get32(r.payload+76)&MIX_OTA_FLAG_HEALTH_CHALLENGE));
        health(25000,true,true);assert(!marks);
    }else if(!strcmp(argv[1],"health-zero-token")||!strcmp(argv[1],"health-malformed-ack")||
             !strcmp(argv[1],"health-malformed-verify")){
        prepare_health_ack();assert(request(MIX_TX_HEALTH_ACK).payload[3]==MIX_TX_OK);
        health(10000,true,true);
        uint8_t saved[72];memcpy(saved,req,sizeof(saved));
        uint8_t op=MIX_TX_HEALTH_ACK;
        if(!strcmp(argv[1],"health-zero-token"))mix_put32(req+68,0);
        else if(!strcmp(argv[1],"health-malformed-verify")){
            op=MIX_TX_VERIFY_RUNNING;mix_put32(req+68,0);req[65]=1;
        }else req[65]=1;
        mix_ota_reply_t r=request(op);assert(r.payload[3]==MIX_TX_REFUSED);
        memcpy(req,saved,sizeof(req));r=request(MIX_TX_HEALTH_ACK);assert(r.payload[3]==MIX_TX_REFUSED);
        health(25000,true,true);assert(!marks&&!invalids&&!restarts);
        authorize_health();health(20500,true,true);assert(marks==1);
    }else if(!strcmp(argv[1],"health-link-lost")||!strcmp(argv[1],"health-session-change")||
             !strcmp(argv[1],"health-legacy-session-change")){
        prepare_health_ack();request(MIX_TX_HEALTH_ACK);health(10000,true,true);
        if(!strcmp(argv[1],"health-link-lost"))mix_ota_link_lost();
        else if(!strcmp(argv[1],"health-legacy-session-change")){
            assert(mix_ota_submit_legacy(MIX_OTA_STATUS,88,NULL,0));
            assert(mix_ota_submit_legacy(MIX_OTA_STATUS,77,NULL,0));
        }else {
            uint8_t q[72]={0};q[0]=2;q[1]=MIX_TX_QUERY;mix_put32(q+4,999);
            assert(mix_ota_submit_request(88,q,sizeof(q)));assert(mix_ota_submit_request(77,q,sizeof(q)));
        }
        mix_ota_reply_t r=request(MIX_TX_HEALTH_ACK);assert(r.payload[3]==MIX_TX_REFUSED);
        health(25000,true,true);assert(!marks);authorize_health();health(20500,true,true);assert(marks==1);
    }else if(!strcmp(argv[1],"health-old-boot")){
        prepare_health_ack();mix_ota_test_reboot();mix_ota_reply_t r=request(MIX_TX_HEALTH_ACK);
        assert(r.payload[3]==MIX_TX_REFUSED);health(25000,true,true);assert(!marks);
    }else if(!strcmp(argv[1],"health-state-read-failure")){
        authorize_health();fail_state=true;health(25000,true,true);
        assert(marks>=3&&restarts&&mix_ota_running_state()!=2&&mix_ota_pending_verify());
    }else if(!strcmp(argv[1],"health-reply-full")){
        uint8_t q[72]={0};q[0]=2;q[1]=MIX_TX_QUERY;mix_put32(q+4,999);
        for(unsigned i=0;i<16;i++)assert(mix_ota_submit_request(77,q,sizeof(q)));
        request_init(MIX_TX_VERIFY_RUNNING);req[64]=(uint8_t)running;SHA256(flash[running],APP_SIZE,req+24);
        uint32_t guessed=get_boot_id()+1;
        assert(mix_ota_submit_request(77,req,sizeof(req)));mix_ota_worker_step();
        mix_ota_reply_t r;unsigned replies_seen=0;while(mix_ota_poll_reply(&r))replies_seen++;
        assert(replies_seen==16);memcpy(req+24,desc.app_elf_sha256,32);mix_put32(req+68,guessed);
        r=request(MIX_TX_HEALTH_ACK);assert(r.payload[3]==MIX_TX_REFUSED);
        health(25000,true,true);assert(!marks);
    }else if(!strcmp(argv[1],"health-queue-busy")){
        prepare_health_ack();
        uint8_t q[72];memcpy(q,req,sizeof(q));q[1]=MIX_TX_VERIFY_RUNNING;
        SHA256(flash[running],APP_SIZE,q+24);mix_put32(q+68,0);
        for(unsigned i=0;i<12;i++)assert(mix_ota_submit_request(77,q,sizeof(q)));
        assert(mix_ota_submit_request(77,req,sizeof(req)));mix_ota_reply_t r;
        assert(mix_ota_poll_reply(&r)&&r.payload[3]==MIX_TX_BUSY);
        health(25000,true,true);assert(!marks);
    }else if(!strcmp(argv[1],"health-interrupted-hash")){
        fail_read=true;request_init(MIX_TX_VERIFY_RUNNING);req[64]=(uint8_t)running;
        SHA256(flash[running],APP_SIZE,req+24);mix_ota_reply_t r=request(MIX_TX_VERIFY_RUNNING);
        assert(r.payload[3]==MIX_TX_ERROR&&!(mix_get32(r.payload+76)&MIX_OTA_FLAG_HEALTH_CHALLENGE));
        health(25000,true,true);assert(!marks);
    }else if(!strcmp(argv[1],"health-link-loss-during-hash")){
        lose_link_on_read=true;request_init(MIX_TX_VERIFY_RUNNING);req[64]=(uint8_t)running;
        SHA256(flash[running],APP_SIZE,req+24);assert(mix_ota_submit_request(77,req,sizeof(req)));
        mix_ota_worker_step();mix_ota_reply_t r;assert(!mix_ota_poll_reply(&r));
        health(25000,true,true);assert(!marks);authorize_health();health(20500,true,true);assert(marks==1);
    }else if(!strcmp(argv[1],"health-local-flap")){
        authorize_health();health(19000,true,true);health(100,false,true);health(19900,true,true);
        assert(!marks);health(200,true,true);assert(marks==1);
    }else if(!strcmp(argv[1],"health-host-absent-after-ack")){
        authorize_health();health(130000,true,false);assert(!marks&&!invalids&&!restarts);
        health(20500,true,true);assert(marks==1);
    }else if(!strcmp(argv[1],"health-journal-unavailable")){
        request_init(MIX_TX_VERIFY_RUNNING);req[64]=(uint8_t)running;SHA256(flash[running],APP_SIZE,req+24);
        mix_ota_reply_t r=request(MIX_TX_VERIFY_RUNNING);assert(r.payload[3]==MIX_TX_OK);
        assert(!(mix_get32(r.payload+76)&MIX_OTA_FLAG_HEALTH_CHALLENGE));
        health(25000,true,true);assert(!marks);
    }else if(!strcmp(argv[1],"health-ack-reply-lost")){
        prepare_health_ack();
        uint8_t q[72]={0};q[0]=2;q[1]=MIX_TX_QUERY;mix_put32(q+4,999);
        for(unsigned i=0;i<16;i++)assert(mix_ota_submit_request(77,q,sizeof(q)));
        mix_put32(req+4,++reqid);assert(mix_ota_submit_request(77,req,sizeof(req)));mix_ota_worker_step();
        mix_ota_reply_t r;while(mix_ota_poll_reply(&r)){}
        r=request(MIX_TX_HEALTH_ACK);assert(r.payload[3]==MIX_TX_OK&&(mix_get32(r.payload+76)&MIX_OTA_FLAG_HEALTH_ACKED));
        health(20500,true,true);assert(marks==1);
    }else if(!strcmp(argv[1],"malformed")){
        request_init(MIX_TX_BEGIN);req[65]=1;mix_ota_reply_t r=request(MIX_TX_BEGIN);assert(r.payload[3]==MIX_TX_REFUSED&&!begins);
    }else {fprintf(stderr,"unknown %s\n",argv[1]);return 2;}
    printf("PASS %s\n",argv[1]);return 0;
}
