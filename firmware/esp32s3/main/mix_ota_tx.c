/* SPDX-License-Identifier: MIT
 * Single-owner OTA worker. The link never invokes a flash operation. QUERY and
 * capabilities use locked snapshots, so a lengthy IDF validation cannot stall
 * heartbeats. See protocol/OTA_V2.md for the exact wire contract.
 */
#include "mix_ota_tx.h"
#include "mix_ota.h"
#include "mix_ota_baseline.h"
#include "mix_protocol.h"
#include <stdatomic.h>
#include <stddef.h>
#include <stdio.h>
#include <string.h>
#include "esp_attr.h"
#include "esp_app_desc.h"
#include "esp_partition.h"
#include "esp_ota_ops.h"
#include "esp_random.h"
#include "esp_timer.h"
#include "mix_health.h"
#include "nvs.h"
#include "freertos/FreeRTOS.h"
#include "freertos/queue.h"
#include "freertos/task.h"

#define JOURNAL_MAGIC 0x3241544du
#define JOURNAL_VERSION 1u
#define REBOOT_WAIT_MS 1200u
/* Every byte preceding crc is initialized, including padding. */
typedef struct {
    uint32_t magic, version, sequence;
    uint8_t id[16], sha[32], stored_sha[32];
    uint32_t size, received, source_boot;
    int32_t error;
    uint8_t phase, target, source, verified;
    char reason[48];
    uint32_t crc;
} journal_t;
_Static_assert(sizeof(journal_t) <= 192, "journal unexpectedly grew");
typedef struct {
    uint32_t magic, sequence, boot_id, stage, error, crc;
} diag_t;
/* Diagnostic only: never used as authorization or the durable commit source.
 * Build tooling records the ELF address; changing it loses cross-image RTC
 * history, not flash safety. CRC/magic reject stale or partially written data. */
RTC_NOINIT_ATTR diag_t mix_ota_rtc_diagnostics[2];
static journal_t journal;
static nvs_handle_t journal_nvs;
static bool journal_ok, layout_ok;
static QueueHandle_t commands, replies;
static _Atomic uint32_t generation = 1;
static _Atomic bool started, work_busy;
static uint32_t boot_id, owner_session, owner_generation;
static bool owner_v2;
static _Atomic bool restart_taken;
static _Atomic uint32_t restart_deadline;
static uint8_t current_sha[32];
static uint32_t current_sha_size;
static bool current_sha_valid, current_sha_matched;
/* Only worker mutates the proof. Link-side session changes revoke it through
 * an atomic revision, including a change away and back before worker runs. */
static _Atomic uint32_t maintenance_session, maintenance_revision;
static struct {
    bool offered, accepted;
    uint32_t generation, revision, session, challenge, request_id;
    uint8_t binding[52]; /* request [8:60]: transaction, file SHA, exact size */
    uint8_t target;
} health_proof;
static void clear_health_proof(void) { memset(&health_proof,0,sizeof(health_proof)); }
static bool health_proof_current(void) {
    return health_proof.offered && health_proof.generation==generation &&
        health_proof.revision==maintenance_revision && health_proof.session==maintenance_session;
}
static void observe_maintenance_session(uint32_t session) {
    if(atomic_exchange(&maintenance_session,session)!=session)maintenance_revision++;
}
static portMUX_TYPE snapshot_lock = portMUX_INITIALIZER_UNLOCKED;
static uint8_t caps_snapshot[MIX_OTA_CAP_BYTES], state_snapshot[MIX_OTA_RESPONSE_BYTES];
static uint32_t millis(void) { return (uint32_t)(esp_timer_get_time() / 1000); }
static bool all_zero(const uint8_t *p, size_t n) { uint8_t v=0; while(n--) v|=*p++; return v==0; }
static bool has_journal(void) { return journal.magic==JOURNAL_MAGIC && !all_zero(journal.id,16); }
static bool selecting(uint8_t phase) { return phase>=MIX_TX_HASH_CHECK && phase<=MIX_TX_REBOOT_REQUESTED; }
static void diagnostic(uint32_t stage, int32_t error)
{
    diag_t previous={0};
    for(unsigned i=0;i<2;i++) {
        diag_t d=mix_ota_rtc_diagnostics[i];
        if(d.magic==JOURNAL_MAGIC && d.crc==mix_crc32((const uint8_t*)&d,offsetof(diag_t,crc)) &&
           (int32_t)(d.sequence-previous.sequence)>0) previous=d;
    }
    diag_t next={.magic=JOURNAL_MAGIC,.sequence=previous.sequence+1,.boot_id=boot_id,.stage=stage,.error=(uint32_t)error};
    next.crc=mix_crc32((const uint8_t*)&next,offsetof(diag_t,crc));
    mix_ota_rtc_diagnostics[next.sequence&1u]=next;
}
static bool save_journal(void)
{
    if(!journal_ok) return false;
    journal.magic=JOURNAL_MAGIC; journal.version=JOURNAL_VERSION; journal.sequence++;
    journal.crc=mix_crc32((const uint8_t*)&journal,offsetof(journal_t,crc));
    esp_err_t e=nvs_set_blob(journal_nvs,"txn",&journal,sizeof(journal));
    if(e==ESP_OK) e=nvs_commit(journal_nvs);
    if(e!=ESP_OK) { journal_ok=false; diagnostic(journal.phase,e); return false; }
    return true;
}
static const esp_partition_t *slot(unsigned i)
{
    return esp_partition_find_first(ESP_PARTITION_TYPE_APP,
        (esp_partition_subtype_t)(ESP_PARTITION_SUBTYPE_APP_OTA_0+i),NULL);
}
static void publish(void)
{
    uint8_t caps[MIX_OTA_CAP_BYTES]={0}, state[MIX_OTA_RESPONSE_BYTES]={0};
    uint8_t running=mix_ota_running_index(), boot=mix_ota_boot_index(), image=mix_ota_running_state();
    caps[0]=MIX_OTA_V2;
    caps[1]=(uint8_t)((journal_ok?1:0)|2|4|(mix_ota_baseline_protected()?8:0)|MIX_OTA_FEATURE_HEALTH_ACK);
    mix_put16(caps+2,MIX_OTA_CHUNK); mix_put16(caps+4,MIX_OTA_WINDOW); mix_put32(caps+8,boot_id);
    caps[12]=running; caps[13]=boot; caps[14]=image;
    for(unsigned i=0;i<2;i++) { const esp_partition_t *p=slot(i); if(p) { mix_put32(caps+16+i*8,p->address); mix_put32(caps+20+i*8,p->size); } }
    mix_put32(caps+32,mix_ota_baseline_protected()?1:0);
    state[0]=MIX_OTA_V2; state[2]=has_journal()?journal.phase:MIX_TX_IDLE;
    memcpy(state+8,journal.id,16); memcpy(state+24,journal.sha,32);
    mix_put32(state+56,journal.size); mix_put32(state+60,journal.received); mix_put32(state+64,boot_id);
    state[68]=has_journal()?journal.target:0xff; state[69]=running; state[70]=boot; state[71]=image;
    mix_put32(state+72,(uint32_t)journal.error);
    mix_put32(state+76,(journal_ok?1u:0u)|(journal.verified?2u:0u)|(mix_ota_baseline_protected()?4u:0u));
    memcpy(state+80,journal.stored_sha,32);
    const esp_app_desc_t *desc=esp_app_get_description(); if(desc)memcpy(state+112,desc->app_elf_sha256,32);
    memcpy(state+144,journal.reason,48);
    portENTER_CRITICAL(&snapshot_lock);
    memcpy(caps_snapshot,caps,sizeof(caps)); memcpy(state_snapshot,state,sizeof(state));
    portEXIT_CRITICAL(&snapshot_lock);
}
size_t mix_ota_capabilities(uint8_t *out,size_t cap)
{
    if(!out||cap<MIX_OTA_CAP_BYTES||!started) return 0;
    portENTER_CRITICAL(&snapshot_lock); memcpy(out,caps_snapshot,MIX_OTA_CAP_BYTES); portEXIT_CRITICAL(&snapshot_lock);
    return MIX_OTA_CAP_BYTES;
}
static void snapshot(uint8_t out[MIX_OTA_RESPONSE_BYTES])
{
    portENTER_CRITICAL(&snapshot_lock); memcpy(out,state_snapshot,MIX_OTA_RESPONSE_BYTES); portEXIT_CRITICAL(&snapshot_lock);
}
static bool emit(const mix_ota_reply_t *r)
{
    /* A dropped notification never changes a committed transaction. The host
     * queries the durable state using a fresh request. */
    return replies && xQueueSend(replies,r,0)==pdTRUE;
}
static void response(const mix_ota_reply_t *cmd,uint8_t result,const char *reason)
{
    mix_ota_reply_t r={.generation=cmd->generation,.session=cmd->session,.type=MIX_OTA_RESPONSE,.length=MIX_OTA_RESPONSE_BYTES};
    snapshot(r.payload); r.payload[1]=cmd->payload[1]; r.payload[3]=result;
    memcpy(r.payload+4,cmd->payload+4,4);
    if(reason)snprintf((char*)r.payload+144,48,"%s",reason);
    emit(&r);
}
static void legacy_reply(const mix_ota_reply_t *cmd,uint8_t type,const void *p,size_t n)
{
    mix_ota_reply_t r={.generation=cmd->generation,.session=cmd->session,.type=type,.length=(uint16_t)n};
    if(n)memcpy(r.payload,p,n); emit(&r);
}
static void legacy_error(const mix_ota_reply_t *cmd,const char *message)
{
    legacy_reply(cmd,MIX_ERROR,message,strnlen(message,127));
}
static void set_phase(uint8_t phase)
{
    journal.phase=phase; diagnostic(phase,0); publish();
}
static void transaction_fail(esp_err_t error,const char *reason)
{
    journal.error=error; journal.phase=MIX_TX_FAILED;
    snprintf(journal.reason,sizeof(journal.reason),"%s",reason);
    diagnostic(journal.phase,error); save_journal(); publish();
}
static bool binding_matches(const uint8_t *p)
{
    return has_journal() && !memcmp(p+8,journal.id,16) && !memcmp(p+24,journal.sha,32) &&
           mix_get32(p+56)==journal.size && p[64]==journal.target;
}
static bool binding_id_matches(const uint8_t *p) { return has_journal()&&!memcmp(p+8,journal.id,16); }
static bool validate_request(const uint8_t *p,size_t n)
{
    return n==MIX_OTA_REQUEST_BYTES && p[0]==MIX_OTA_V2 && p[1]>=MIX_TX_BEGIN && p[1]<=MIX_TX_HEALTH_ACK &&
        mix_get16(p+2)==0 && mix_get32(p+4)!=0 && !p[65]&&!p[66]&&!p[67] &&
        (p[1]==MIX_TX_HEALTH_ACK ? mix_get32(p+68)!=0 : mix_get32(p+68)==0);
}
static bool live_layout(void)
{
    const esp_partition_t *a=slot(0),*b=slot(1);
    const esp_partition_t *meta=esp_partition_find_first(ESP_PARTITION_TYPE_DATA,ESP_PARTITION_SUBTYPE_DATA_OTA,NULL);
    return a&&b&&meta&&a->address==0x10000&&a->size==0x1f0000&&b->address==0x610000&&b->size==0x1f0000&&
        meta->address==0x200000&&meta->size==0x2000;
}
static esp_err_t start_transfer(const mix_ota_reply_t *cmd,bool v2)
{
    const uint8_t *p=cmd->payload;
    uint32_t size=v2?mix_get32(p+56):mix_get32(p);
    const uint8_t *sha=v2?p+24:p+4;
    const esp_partition_t *target=esp_ota_get_next_update_partition(NULL);
    if(!journal_ok||!layout_ok||!target) return ESP_ERR_INVALID_STATE;
    uint8_t target_index=(uint8_t)(target->subtype-ESP_PARTITION_SUBTYPE_APP_OTA_0);
    if(v2 && (all_zero(p+8,16)||p[64]!=target_index||mix_get32(p+60)!=boot_id)) return ESP_ERR_INVALID_ARG;
    if(size<1024||size>target->size||mix_ota_pending_verify()||mix_ota_boot_index()!=mix_ota_running_index()||
       (mix_ota_baseline_protected()&&target_index==0)) return ESP_ERR_INVALID_STATE;
    if(mix_ota_state()==MIX_OTA_RECEIVING||selecting(journal.phase)) return ESP_ERR_INVALID_STATE;
    journal_t previous=journal;
    memset(&journal,0,sizeof(journal)); journal.magic=JOURNAL_MAGIC; journal.version=JOURNAL_VERSION;
    if(v2)memcpy(journal.id,p+8,16);
    else for(unsigned i=0;i<4;i++)mix_put32(journal.id+i*4,esp_random()|1u);
    memcpy(journal.sha,sha,32); journal.size=size; journal.target=target_index;
    journal.source=mix_ota_running_index(); journal.source_boot=boot_id; journal.phase=MIX_TX_RECEIVING;
    if(!save_journal()) { journal=previous; publish(); return ESP_FAIL; }
    mix_ota_reset();
    esp_err_t err=mix_ota_begin(size,sha);
    if(err!=ESP_OK) { transaction_fail(err,mix_ota_error()); return err; }
    owner_session=cmd->session; owner_generation=cmd->generation; owner_v2=v2;
    publish(); return ESP_OK;
}
static esp_err_t finish_transfer(void)
{
    set_phase(MIX_TX_HASH_CHECK);
    if(!save_journal()) { mix_ota_abort(); transaction_fail(ESP_FAIL,"journal unavailable before validation"); return ESP_FAIL; }
    set_phase(MIX_TX_IMAGE_VALIDATE);
    esp_err_t err=mix_ota_validate(journal.stored_sha);
    if(err!=ESP_OK) { transaction_fail(err,mix_ota_error()); return err; }
    journal.verified=1; set_phase(MIX_TX_SELECT_INTENT);
    if(!save_journal()) { mix_ota_abort(); transaction_fail(ESP_FAIL,"SELECT_INTENT not durable; selection refused"); return ESP_FAIL; }
    err=mix_ota_select();
    if(err!=ESP_OK) { transaction_fail(err,mix_ota_error()); return err; }
    set_phase(MIX_TX_BOOT_SELECTED);
    if(!save_journal()) {
        /* Selection is already real. Never describe this as safely aborted. */
        journal.error=ESP_FAIL; snprintf(journal.reason,sizeof(journal.reason),"boot selected; journal reconciliation required");
    }
    publish(); return ESP_OK;
}
static void request_reboot(const mix_ota_reply_t *cmd)
{
    if(!binding_matches(cmd->payload)) { response(cmd,MIX_TX_CONFLICT,"transaction binding mismatch"); return; }
    if(mix_ota_running_index()==journal.target) {
        response(cmd,MIX_TX_OK,NULL); return; /* Old reboot replay after the new boot. */
    }
    if(mix_get32(cmd->payload+60)!=boot_id||mix_ota_boot_index()!=journal.target||
       (journal.phase!=MIX_TX_BOOT_SELECTED&&journal.phase!=MIX_TX_REBOOT_REQUESTED)) {
        response(cmd,MIX_TX_REFUSED,"boot identity or selected slot mismatch"); return;
    }
    if(!restart_deadline&&!restart_taken) {
        journal.source_boot=boot_id; set_phase(MIX_TX_REBOOT_REQUESTED);
        if(!save_journal()) {
            /* Boot selection already happened. Expose the failed durability
             * flag without claiming rollback or arming an execution deadline. */
            publish();
            response(cmd,MIX_TX_ERROR,"reboot intent not durable; inspect selected boot slot"); return;
        }
        restart_deadline=millis()+REBOOT_WAIT_MS; if(!restart_deadline)restart_deadline=1;
    }
    response(cmd,MIX_TX_OK,NULL);
}
static void measurement_response(const mix_ota_reply_t *cmd, bool matched)
{
    mix_ota_reply_t r={.generation=cmd->generation,.session=cmd->session,.type=MIX_OTA_RESPONSE,.length=MIX_OTA_RESPONSE_BYTES};
    publish(); snapshot(r.payload); r.payload[1]=cmd->payload[1];
    r.payload[3]=matched?MIX_TX_OK:MIX_TX_ERROR; memcpy(r.payload+4,cmd->payload+4,4);
    const uint8_t *binding=cmd->payload[1]==MIX_TX_HEALTH_ACK?health_proof.binding:cmd->payload+8;
    uint32_t size=mix_get32(binding+48);
    memcpy(r.payload+8,binding,52);
    mix_put32(r.payload+60,matched?size:0); r.payload[68]=mix_ota_running_index();
    if(!has_journal())r.payload[2]=mix_ota_running_state()==2?MIX_TX_CONFIRMED:MIX_TX_RUNNING_PENDING;
    mix_put32(r.payload+72,(uint32_t)(matched?ESP_OK:ESP_ERR_INVALID_CRC));
    uint32_t flags=(journal_ok?1u:0u)|(matched?2u:0u)|(mix_ota_baseline_protected()?4u:0u);
    memset(r.payload+144,0,48);
    bool offer=matched && health_proof.challenge && health_proof.generation==generation &&
        health_proof.revision==maintenance_revision && health_proof.session==cmd->session;
    if(offer) {
        flags|=MIX_OTA_FLAG_HEALTH_CHALLENGE;
        if(health_proof.accepted)flags|=MIX_OTA_FLAG_HEALTH_ACKED;
        snprintf((char*)r.payload+144,48,"health-challenge:%08lx",(unsigned long)health_proof.challenge);
    } else if(!matched)snprintf((char*)r.payload+144,48,"running file SHA mismatch or read failed");
    mix_put32(r.payload+76,flags);
    if(current_sha_valid)memcpy(r.payload+80,current_sha,32); else memset(r.payload+80,0,32);
    /* Queue success is only an OFFER. The token echoed with exact ELF and
     * binding in a later request, never this enqueue, permits confirmation. */
    if(emit(&r) && offer)health_proof.offered=true;
}
static void verify_running(const mix_ota_reply_t *cmd)
{
    uint32_t size=mix_get32(cmd->payload+56);
    uint8_t running=mix_ota_running_index();
    if(cmd->payload[64]!=running || mix_get32(cmd->payload+60)!=boot_id || size<1024 ||
       !slot(running)||size>slot(running)->size || mix_ota_state()==MIX_OTA_RECEIVING || all_zero(cmd->payload+8,16)) {
        clear_health_proof();
        response(cmd,MIX_TX_REFUSED,"invalid running-image verification request"); return;
    }
    if(!health_proof_current() || memcmp(health_proof.binding,cmd->payload+8,52) || health_proof.target!=running)
        clear_health_proof();
    esp_err_t err=ESP_OK;
    if(!current_sha_valid||current_sha_size!=size) {
        diagnostic(MIX_TX_VERIFYING_RUNNING,0);
        err=mix_ota_hash_running(size,current_sha);
        current_sha_valid=err==ESP_OK; current_sha_size=size;
    }
    bool matched=err==ESP_OK&&!memcmp(current_sha,cmd->payload+24,32);
    current_sha_matched=matched;
    if(!matched)clear_health_proof();
    else if(!health_proof.challenge && journal_ok && layout_ok && mix_ota_boot_index()==running &&
            (mix_ota_running_state()==1 || mix_ota_running_state()==2) &&
            cmd->generation==generation && cmd->maintenance_revision==maintenance_revision) {
        health_proof.generation=cmd->generation; health_proof.revision=cmd->maintenance_revision;
        health_proof.session=cmd->session; health_proof.target=running;
        health_proof.challenge=esp_random(); if(!health_proof.challenge)health_proof.challenge=1;
        health_proof.request_id=mix_get32(cmd->payload+4);
        memcpy(health_proof.binding,cmd->payload+8,52);
    }
    measurement_response(cmd,matched);
}
static void acknowledge_health(const mix_ota_reply_t *cmd)
{
    const uint8_t *p=cmd->payload;
    const esp_app_desc_t *desc=esp_app_get_description();
    if(!health_proof_current() || health_proof.session!=cmd->session ||
       cmd->generation!=generation || cmd->maintenance_revision!=maintenance_revision ||
       !desc || memcmp(p+24,desc->app_elf_sha256,32) ||
       memcmp(p+8,health_proof.binding,16) || mix_get32(p+56)!=mix_get32(health_proof.binding+48) ||
       p[64]!=health_proof.target || mix_get32(p+60)!=boot_id ||
       mix_get32(p+68)!=health_proof.challenge ||
       (int32_t)(mix_get32(p+4)-health_proof.request_id)<=0 ||
       !current_sha_valid || !current_sha_matched || current_sha_size!=mix_get32(p+56) ||
       memcmp(current_sha,health_proof.binding+16,32) || !journal_ok || !layout_ok ||
       mix_ota_running_index()!=health_proof.target || mix_ota_boot_index()!=health_proof.target ||
       (mix_ota_running_state()!=1 && mix_ota_running_state()!=2) || mix_ota_transaction_busy()) {
        clear_health_proof(); response(cmd,MIX_TX_REFUSED,"health ACK binding/challenge/ELF mismatch"); return;
    }
    health_proof.accepted=true;
    measurement_response(cmd,true);
}
static void release_baseline(const mix_ota_reply_t *cmd)
{
    if(!journal_ok||cmd->payload[64]!=0||mix_get32(cmd->payload+56)!=MIX_BASELINE_BYTES||
       memcmp(cmd->payload+24,MIX_BASELINE_SHA,32)||mix_get32(cmd->payload+60)!=boot_id||
       mix_ota_running_index()!=1||mix_ota_running_state()!=2||mix_ota_boot_index()!=1||
       !current_sha_valid||!current_sha_matched||mix_ota_transaction_busy()) {
        response(cmd,MIX_TX_REFUSED,"baseline release requires verified VALID ota_1"); return;
    }
    esp_err_t err=nvs_set_u8(journal_nvs,"released",1);
    if(err==ESP_OK)err=nvs_commit(journal_nvs);
    if(err!=ESP_OK) { journal_ok=false; publish(); response(cmd,MIX_TX_ERROR,"baseline release not durable"); return; }
    mix_ota_set_baseline_protection(false); publish(); response(cmd,MIX_TX_OK,NULL);
}
static void dispatch_request(const mix_ota_reply_t *cmd)
{
    uint8_t op=cmd->payload[1];
    if(op==MIX_TX_HEALTH_ACK) { acknowledge_health(cmd); return; }
    if(op==MIX_TX_VERIFY_RUNNING) { verify_running(cmd); return; }
    if(op==MIX_TX_RELEASE_BASELINE) { release_baseline(cmd); return; }
    if(op==MIX_TX_BEGIN) {
        if(binding_id_matches(cmd->payload)) {
            if(!binding_matches(cmd->payload))response(cmd,MIX_TX_CONFLICT,"transaction ID reused with different image");
            else if(journal.phase==MIX_TX_RECEIVING && owner_session!=cmd->session)
                response(cmd,MIX_TX_CONFLICT,"transfer is bound to another session");
            else response(cmd,MIX_TX_OK,NULL);
            return;
        }
        esp_err_t err=start_transfer(cmd,true);
        response(cmd,err==ESP_OK?MIX_TX_OK:MIX_TX_REFUSED,err==ESP_OK?NULL:"BEGIN refused: layout, journal, slot, boot or baseline guard"); return;
    }
    if(!binding_matches(cmd->payload)) { response(cmd,MIX_TX_CONFLICT,"transaction binding mismatch"); return; }
    if(op==MIX_TX_END) {
        if(journal.phase==MIX_TX_RECEIVING) {
            if(!owner_v2 || owner_session!=cmd->session || owner_generation!=cmd->generation) {
                response(cmd,MIX_TX_REFUSED,"END does not own receiving session"); return;
            }
            response(cmd,MIX_TX_BUSY,NULL);
            esp_err_t err=finish_transfer(); response(cmd,err==ESP_OK?MIX_TX_OK:MIX_TX_ERROR,NULL);
        } else response(cmd,journal.phase==MIX_TX_FAILED||journal.phase==MIX_TX_ABORTED?MIX_TX_ERROR:MIX_TX_OK,NULL);
    } else if(op==MIX_TX_REBOOT) request_reboot(cmd);
    else if(op==MIX_TX_ABORT) {
        if(journal.phase==MIX_TX_RECEIVING) {
            mix_ota_abort(); journal.phase=MIX_TX_ABORTED; journal.error=0;
            snprintf(journal.reason,sizeof(journal.reason),"aborted by host"); save_journal(); publish();
            response(cmd,MIX_TX_OK,NULL);
        } else response(cmd,journal.phase==MIX_TX_ABORTED?MIX_TX_OK:MIX_TX_REFUSED,"cannot cancel a committed or finalizing update");
    }
}
static void dispatch_legacy(const mix_ota_reply_t *cmd)
{
    esp_err_t err; uint8_t p[12];
    if(cmd->type==MIX_OTA_BEGIN) {
        if(cmd->length!=36) { legacy_error(cmd,"OTA_BEGIN needs size and sha256"); return; }
        if(owner_session==cmd->session&&owner_generation==cmd->generation&&!owner_v2&&journal.phase==MIX_TX_RECEIVING) {
            if(mix_get32(cmd->payload)!=journal.size||memcmp(cmd->payload+4,journal.sha,32)) {
                legacy_error(cmd,"BEGIN binding conflict; original transfer preserved"); return;
            }
            err=ESP_OK;
        } else err=start_transfer(cmd,false);
        if(err!=ESP_OK) { legacy_error(cmd,"BEGIN refused: check trial, selected slot, baseline and journal"); return; }
        mix_put32(p,MIX_OTA_CHUNK); mix_put32(p+4,MIX_OTA_WINDOW); mix_put32(p+8,journal.size);
        legacy_reply(cmd,MIX_OTA_READY,p,12); return;
    }
    if(cmd->session!=owner_session||cmd->generation!=owner_generation) return;
    if(cmd->type==MIX_OTA_DATA) {
        if(journal.phase!=MIX_TX_RECEIVING){legacy_error(cmd,"DATA requires an active receive transaction");return;}
        if(cmd->length<5) { legacy_error(cmd,"short OTA_DATA"); return; }
        err=mix_ota_write(mix_get32(cmd->payload),cmd->payload+4,cmd->length-4);
        if(err!=ESP_OK && !(err==ESP_ERR_INVALID_STATE&&mix_ota_state()==MIX_OTA_RECEIVING)) {
            transaction_fail(err,mix_ota_error()); legacy_error(cmd,mix_ota_error()); return;
        }
        journal.received=mix_ota_received(); publish();
        mix_put32(p,journal.received); legacy_reply(cmd,MIX_OTA_ACK,p,4);
    } else if(cmd->type==MIX_OTA_STATUS) {
        mix_put32(p,journal.received); legacy_reply(cmd,MIX_OTA_ACK,p,4);
    } else if(cmd->type==MIX_OTA_END) {
        if(owner_v2) { legacy_error(cmd,"v2 transaction requires v2 END"); return; }
        if(cmd->length)return;
        if(journal.phase==MIX_TX_BOOT_SELECTED||journal.phase==MIX_TX_REBOOT_REQUESTED) {
            mix_put32(p,journal.size); legacy_reply(cmd,MIX_OTA_DONE,p,4); return;
        }
        if(journal.phase!=MIX_TX_RECEIVING){legacy_error(cmd,"END requires a receiving transaction");return;}
        if(finish_transfer()!=ESP_OK) {
            /* The journal stores only 48 bytes, not the generic legacy
             * error path's 127-byte limit. Bound reads to this object. */
            legacy_reply(cmd,MIX_ERROR,journal.reason,strnlen(journal.reason,sizeof(journal.reason)));
            return;
        }
        mix_put32(p,journal.size); legacy_reply(cmd,MIX_OTA_DONE,p,4);
        /* A lost DONE does not strand a legacy committed image. */
        restart_deadline=millis()+REBOOT_WAIT_MS; if(!restart_deadline)restart_deadline=1;
    } else if(cmd->type==MIX_OTA_ABORT) {
        if(owner_v2) { legacy_error(cmd,"v2 transaction requires v2 ABORT"); return; }
        if(!cmd->length&&journal.phase==MIX_TX_RECEIVING) {
            mix_ota_abort(); journal.phase=MIX_TX_ABORTED; save_journal(); publish();
        }
    }
}
static void periodic(void)
{
    uint32_t now=millis();
    if(journal.phase==MIX_TX_RECEIVING && owner_generation!=generation) {
        mix_ota_abort(); journal.phase=MIX_TX_ABORTED;
        snprintf(journal.reason,sizeof(journal.reason),"link lost during receive; no resume"); save_journal(); publish();
    }
    mix_ota_tick(now);
    if(journal.phase==MIX_TX_RECEIVING&&mix_ota_state()==MIX_OTA_FAILED)transaction_fail(ESP_FAIL,mix_ota_error());
    if(!health_proof_current())clear_health_proof();
    uint8_t before=mix_ota_running_state();
    mix_ota_process_health(now,health_proof_current() && health_proof.accepted);
    if(before!=mix_ota_running_state()) {
        if(has_journal()&&journal.target==mix_ota_running_index()&&mix_ota_running_state()==2) {
            journal.phase=MIX_TX_CONFIRMED; save_journal();
        }
        publish();
    }
}
static void reconcile(void)
{
    if(!has_journal())return;
    uint8_t running=mix_ota_running_index(),boot=mix_ota_boot_index();
    if((selecting(journal.phase)||journal.phase==MIX_TX_RUNNING_PENDING||journal.phase==MIX_TX_CONFIRMED)&&
       running==journal.target&&boot==running) {
        esp_err_t err=mix_ota_hash_running(journal.size,current_sha);
        current_sha_valid=err==ESP_OK; current_sha_size=journal.size;
        if(err!=ESP_OK||memcmp(current_sha,journal.sha,32)) { transaction_fail(ESP_FAIL,"booted candidate hash mismatch"); return; }
        journal.verified=1; memcpy(journal.stored_sha,current_sha,32);
        journal.phase=mix_ota_running_state()==2?MIX_TX_CONFIRMED:MIX_TX_RUNNING_PENDING;
        save_journal();
    } else if(selecting(journal.phase)&&boot==journal.target&&running!=boot) {
        journal.phase=MIX_TX_BOOT_SELECTED; save_journal();
    } else if(journal.phase==MIX_TX_RECEIVING||journal.phase==MIX_TX_HASH_CHECK||journal.phase==MIX_TX_IMAGE_VALIDATE) {
        journal.phase=MIX_TX_ABORTED; snprintf(journal.reason,sizeof(journal.reason),"reset before selection; no resume"); save_journal();
    } else if(selecting(journal.phase)||journal.phase==MIX_TX_RUNNING_PENDING) {
        transaction_fail(ESP_FAIL,"candidate not selected/running; cause unproven");
    }
    publish();
}
static bool step(uint32_t wait)
{
    mix_ota_reply_t cmd;
    periodic();
    if(xQueueReceive(commands,&cmd,wait)!=pdTRUE)return false;
    if(cmd.generation!=generation || (cmd.type==MIX_OTA_REQUEST && cmd.maintenance_revision!=maintenance_revision))return true;
    work_busy=true;
    if(cmd.type==MIX_OTA_REQUEST)dispatch_request(&cmd); else dispatch_legacy(&cmd);
    work_busy=false; periodic(); return true;
}
static void worker(void *unused)
{
    (void)unused;
    if(mix_watchdog_task_begin(MIX_HEALTH_OTA)!=ESP_OK){vTaskDelete(NULL);return;}
    reconcile();
    for(;;) {
        step(pdMS_TO_TICKS(20));
        if(mix_watchdog_task_reset(MIX_HEALTH_OTA)!=ESP_OK){mix_watchdog_task_end(MIX_HEALTH_OTA);vTaskDelete(NULL);return;}
    }
}
esp_err_t mix_ota_init_worker(void)
{
    if(started)return ESP_ERR_INVALID_STATE;
    clear_health_proof(); maintenance_session=0; maintenance_revision++;
    current_sha_valid=current_sha_matched=false;
    memset(&journal,0,sizeof(journal)); journal.target=0xff;
    journal_ok=nvs_open("mix_ota",NVS_READWRITE,&journal_nvs)==ESP_OK;
    if(journal_ok) {
        size_t n=sizeof(journal); esp_err_t e=nvs_get_blob(journal_nvs,"txn",&journal,&n);
        if(e==ESP_ERR_NVS_NOT_FOUND) { memset(&journal,0,sizeof(journal)); journal.target=0xff; }
        else if(e!=ESP_OK||n!=sizeof(journal)||journal.magic!=JOURNAL_MAGIC||journal.version!=JOURNAL_VERSION||
                journal.crc!=mix_crc32((const uint8_t*)&journal,offsetof(journal_t,crc))||journal.target>1||journal.phase>MIX_TX_VERIFYING_RUNNING) {
            memset(&journal,0,sizeof(journal)); journal.target=0xff; journal_ok=false;
        }
        uint8_t released=0; e=nvs_get_u8(journal_nvs,"released",&released);
        if(e!=ESP_OK&&e!=ESP_ERR_NVS_NOT_FOUND)journal_ok=false;
        mix_ota_set_baseline_protection(!(e==ESP_OK&&released==1));
    }
    boot_id=esp_random(); if(!boot_id)boot_id=1;
    layout_ok=live_layout(); publish();
    commands=xQueueCreate(12,sizeof(mix_ota_reply_t)); replies=xQueueCreate(16,sizeof(mix_ota_reply_t));
    if(!commands||!replies)return ESP_ERR_NO_MEM;
    started=true;
    if(xTaskCreate(worker,"mix_ota",8192,NULL,4,NULL)!=pdPASS) { started=false; return ESP_ERR_NO_MEM; }
    return ESP_OK;
}
bool mix_ota_submit_legacy(uint8_t type,uint32_t session,const uint8_t *p,size_t n)
{
    if(!started||!session||n>512||(n&&!p))return false;
    observe_maintenance_session(session);
    mix_ota_reply_t cmd={.generation=generation,.session=session,.type=type,.length=(uint16_t)n};
    if(n)memcpy(cmd.payload,p,n); return xQueueSend(commands,&cmd,0)==pdTRUE;
}
bool mix_ota_submit_request(uint32_t session,const uint8_t *p,size_t n)
{
    if(!started||!session||!p)return false;
    observe_maintenance_session(session);
    mix_ota_reply_t cmd={.generation=generation,.maintenance_revision=maintenance_revision,
        .session=session,.type=MIX_OTA_REQUEST,.length=(uint16_t)n};
    if(!validate_request(p,n)) {
        /* Malformed confirmation/measurement attempts also revoke an already
         * accepted proof. The link only changes the atomic revision; worker
         * remains the sole owner of proof and health-timer state. */
        if(n>=2 && (p[1]==MIX_TX_HEALTH_ACK || p[1]==MIX_TX_VERIFY_RUNNING))maintenance_revision++;
        if(n>=8)memcpy(cmd.payload,p,8);
        response(&cmd,MIX_TX_REFUSED,"malformed request"); return true;
    }
    memcpy(cmd.payload,p,n);
    if(p[1]==MIX_TX_QUERY) {
        uint8_t view[MIX_OTA_RESPONSE_BYTES]; snapshot(view);
        bool found=all_zero(p+8,16)||!memcmp(view+8,p+8,16);
        response(&cmd,found?MIX_TX_OK:MIX_TX_NOT_FOUND,found?NULL:"transaction not found"); return true;
    }
    if(xQueueSend(commands,&cmd,0)!=pdTRUE) {
        response(&cmd,MIX_TX_BUSY,"worker queue full; query before retry"); return true;
    }
    return true;
}
bool mix_ota_poll_reply(mix_ota_reply_t *r)
{
    if(!replies||!r)return false;
    while(xQueueReceive(replies,r,0)==pdTRUE)if(r->generation==generation)return true;
    return false;
}
void mix_ota_link_lost(void) { generation++; }
bool mix_ota_transaction_busy(void)
{
    uint8_t view[MIX_OTA_RESPONSE_BYTES]; snapshot(view);
    return view[2]==MIX_TX_RECEIVING||selecting(view[2]);
}
bool mix_ota_take_worker_restart(void)
{
    /* Main only reads a small atomic deadline in the integrated build. */
    if(restart_deadline&&!restart_taken&&(int32_t)(millis()-restart_deadline)>=0) {
        restart_taken=true; return true;
    }
    return false;
}
#ifdef MIX_OTA_HOST_TEST
bool mix_ota_worker_step(void) { return step(0); }
void mix_ota_test_reboot(void)
{
    started=false; current_sha_valid=current_sha_matched=false; restart_deadline=0; restart_taken=false; owner_session=0;
    generation++; mix_ota_init(); mix_ota_init_worker(); reconcile();
}
#endif
