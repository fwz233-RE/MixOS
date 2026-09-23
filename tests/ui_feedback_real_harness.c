/* Host-only production UI capture. No simulated typography or hardware I/O.
 * mix_ui.c is included to select static pages; ttf_font.c and mix_terminal.c
 * are linked unchanged. The only production UI framebuffer is the guarded
 * production allocation; host-only arrays hold submission/equivalence oracles.
 * Build/run through render_ui_feedback_r6.py (real vendored FreeType). */
#define _POSIX_C_SOURCE 200809L
#include <assert.h>
#include <inttypes.h>
#include <limits.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <ft2build.h>
#include FT_FREETYPE_H
#include "ttf_font.h"
#include "esp_heap_caps.h"
#include "esp_partition.h"

static int capture_text(uint16_t *,int,int,int,int,int,uint16_t,const char *);
static int capture_clipped_text(uint16_t *,int,int,int,int,int,int,int,uint16_t,const char *);
static void *capture_memmove(void *,const void *,size_t);
static void *capture_memcpy(void *,const void *,size_t,const char *);
static int capture_width(int,const char *);
static bool capture_glyph(uint32_t,int,ttf_glyph_t *);
static void *capture_ui_alloc(size_t,unsigned);
#define ttf_draw_text capture_text
#define ttf_draw_text_clipped capture_clipped_text
#define memmove capture_memmove
#define memcpy(dst,src,n) capture_memcpy(dst,src,n,__func__)
#define ttf_text_width capture_width
#define ttf_font_glyph capture_glyph
#define heap_caps_malloc capture_ui_alloc
#include "../firmware/esp32s3/main/mix_ui.c"
#undef ttf_draw_text
#undef ttf_draw_text_clipped
#undef memmove
#undef memcpy
#undef ttf_text_width
#undef ttf_font_glyph
#undef heap_caps_malloc

static uint32_t clock_ms=10000;
static unsigned ui_allocations;
static unsigned char *ui_raw,*font_data;
static size_t font_bytes;
static esp_partition_t font_partition;
static const char *output_dir;
static bool recording;
static unsigned text_count;
static unsigned layout_issues;
/* Host-only oracle storage, never requested through the UI allocator. The
 * panel mirror catches missing presents even when the canvas itself is right. */
static uint16_t panel_pixels[W*H],scroll_pixels[W*H];
static bool scroll_auditing;
static unsigned frame_row_copies,frame_presents,frame_rects;
static uint64_t frame_pixels;
static bool frame_full_body;
static uint64_t scroll_frames,scroll_partial_frames,scroll_one_px,scroll_large;
static uint64_t scroll_roundtrips,scroll_invalidations,scroll_clipped_calls;
static int ft_major,ft_minor,ft_patch;
static FILE *report;
static struct {
    uint64_t load_calls,size_calls,width_calls,text_calls,glyph_calls;
    uint64_t presents,presented_pixels,present_batches,present_rectangles,font_allocations,font_allocated_bytes;
    uint32_t missing[64];unsigned missing_count;
} counters;
static struct text_record {
    int x,y,size,clip0,limit,advance;
    uint16_t color;
    int left,top,right,bottom;
    unsigned glyphs,solid_pixels,overwritten_pixels;
    char text[384];
} texts[192];

/* These linker wrappers count actual FreeType work, including width queries. */
FT_Error __real_FT_Load_Char(FT_Face,FT_ULong,FT_Int32);
FT_Error __real_FT_Set_Pixel_Sizes(FT_Face,FT_UInt,FT_UInt);
FT_Error __wrap_FT_Load_Char(FT_Face face,FT_ULong cp,FT_Int32 flags) {
    if(!ft_major)FT_Library_Version(face->glyph->library,&ft_major,&ft_minor,&ft_patch);
    if(recording) {
        counters.load_calls++;
        if(!FT_Get_Char_Index(face,cp)) {
            unsigned i=0;while(i<counters.missing_count&&counters.missing[i]!=cp)i++;
            if(i==counters.missing_count&&i<64)counters.missing[counters.missing_count++]=(uint32_t)cp;
        }
    }
    return __real_FT_Load_Char(face,cp,flags);
}
FT_Error __wrap_FT_Set_Pixel_Sizes(FT_Face face,FT_UInt w,FT_UInt h) {
    if(recording)counters.size_calls++;
    return __real_FT_Set_Pixel_Sizes(face,w,h);
}
static void record_text(int x,int y,int size,int clip0,int limit,int advance,uint16_t color,const char *s) {
    if(recording) {
        counters.text_calls++;assert(text_count<sizeof(texts)/sizeof(texts[0]));
        struct text_record *t=&texts[text_count++];memset(t,0,sizeof(*t));
        t->x=x;t->y=y;t->size=size;t->clip0=clip0;t->limit=limit;t->advance=advance;t->color=color;
        assert(strlen(s)<sizeof(t->text));strcpy(t->text,s);
    }
}
static int capture_text(uint16_t *pixels,int w,int h,int x,int y,int size,uint16_t color,const char *s) {
    int advance=ttf_draw_text(pixels,w,h,x,y,size,color,s);
    record_text(x,y,size,0,h,advance,color,s);return advance;
}
static int capture_clipped_text(uint16_t *pixels,int w,int h,int clip0,int clip1,int x,int y,int size,uint16_t color,const char *s) {
    int advance=ttf_draw_text_clipped(pixels,w,h,clip0,clip1,x,y,size,color,s);
    if(scroll_auditing)scroll_clipped_calls++;
    record_text(x,y,size,clip0,clip1,advance,color,s);return advance;
}
static bool in_ui_framebuffer(const void *p) {
    return (uintptr_t)p>=(uintptr_t)fb&&
           (uintptr_t)p<(uintptr_t)fb+(size_t)W*H*sizeof(*fb);
}
static void *capture_memmove(void *dst,const void *src,size_t n) {
    /* ESP32-S3 ROM memmove is a byte loop. A regression to it may look fast
     * on the host, so explicitly reject it for framebuffer row reuse. */
    assert(!in_ui_framebuffer(dst)&&!in_ui_framebuffer(src));
    return memmove(dst,src,n);
}
static void *capture_memcpy(void *dst,const void *src,size_t n,const char *caller) {
    if(!strcmp(caller,"settings_body_scroll_draw")) {
        assert(in_ui_framebuffer(dst)&&in_ui_framebuffer(src));
        uintptr_t lo=(uintptr_t)fb,hi=lo+(size_t)W*CONTENT_H*sizeof(*fb);
        uintptr_t d=(uintptr_t)dst,s=(uintptr_t)src;
        assert(d+n<=hi&&s+n<=hi);
        assert(n==BODY_W*sizeof(*fb)&&d%4==0&&s%4==0);
        assert((d-lo)%(W*sizeof(*fb))==BODY_X*sizeof(*fb));
        assert((s-lo)%(W*sizeof(*fb))==BODY_X*sizeof(*fb));
        /* Even a 1px vertical delta has disjoint per-row addresses. The
         * oracle below verifies across-row order in both directions. */
        assert(d+n<=s||s+n<=d);
        if(scroll_auditing)frame_row_copies++;
    }
    return memcpy(dst,src,n);
}
static int capture_width(int size,const char *s) {
    if(recording)counters.width_calls++;
    return ttf_text_width(size,s);
}
static bool capture_glyph(uint32_t cp,int size,ttf_glyph_t *out) {
    if(recording)counters.glyph_calls++;
    return ttf_font_glyph(cp,size,out);
}
static void check_guard(void) {
    for(int i=0;i<32;i++)assert(ui_raw[i]==0xa5&&ui_raw[32+(size_t)W*H*2+i]==0x5a);
}
static void *capture_ui_alloc(size_t n,unsigned caps) {
    (void)caps;assert(++ui_allocations==1&&n==(size_t)W*H*2);
    ui_raw=malloc(n+64);assert(ui_raw);memset(ui_raw,0xa5,32);memset(ui_raw+32+n,0x5a,32);
    return ui_raw+32;
}
void *heap_caps_malloc(size_t n,unsigned caps) {
    (void)caps;assert(n<=(512u*1024u));
    if(recording){counters.font_allocations++;counters.font_allocated_bytes+=n;}
    return malloc(n);
}
void *heap_caps_calloc(size_t n,size_t bytes,unsigned caps) {(void)caps;return calloc(n,bytes);}
void test_log(const char *tag,const char *fmt,...) {(void)tag;(void)fmt;}
const char *esp_err_to_name(esp_err_t e) {(void)e;return "host error";}
const esp_partition_t *esp_partition_find_first(int type,int subtype,const char *label) {
    (void)type;(void)subtype;assert(!strcmp(label,"font"));return &font_partition;
}
esp_err_t esp_partition_mmap(const esp_partition_t *part,size_t offset,size_t n,int type,const void **data,esp_partition_mmap_handle_t *handle) {
    (void)type;assert(part==&font_partition&&!offset&&n==font_bytes);*data=font_data;*handle=1;return ESP_OK;
}
void esp_partition_munmap(esp_partition_mmap_handle_t handle) {assert(handle==1);}
int64_t esp_timer_get_time(void) {return (int64_t)clock_ms*1000;}
esp_err_t mix_present_init(esp_lcd_panel_handle_t panel) {assert(panel);return ESP_OK;}
static void copy_present_rect(const mix_present_rect_t *r,const uint16_t *pixels) {
    assert(pixels==fb&&r->x0>=0&&r->x0<r->x1&&r->x1<=W&&r->y0>=0&&r->y0<r->y1&&r->y1<=H);
    uint64_t area=(uint64_t)(r->x1-r->x0)*(r->y1-r->y0);check_guard();
    for(int y=r->y0;y<r->y1;y++)
        memcpy(panel_pixels+y*W+r->x0,pixels+y*W+r->x0,(size_t)(r->x1-r->x0)*sizeof(*pixels));
    if(recording){counters.presented_pixels+=area;counters.present_rectangles++;}
    if(scroll_auditing) {
        frame_rects++;frame_pixels+=area;
        if(r->x0==BODY_X&&r->x1==W&&r->y0==0&&r->y1==CONTENT_H)frame_full_body=true;
    }
}
esp_err_t mix_present_get_stats(mix_present_stats_t *out) {
    assert(out);memset(out,0,sizeof(*out));out->frame_count=clock_ms/35;out->present_count=(uint32_t)counters.presents;return ESP_OK;
}
esp_err_t mix_present_rects(const mix_present_rect_t *rects,unsigned count,const uint16_t *pixels) {
    assert(rects&&count>=1&&count<=MIX_PRESENT_MAX_RECTS&&count<=8);
    if(recording){counters.presents++;counters.present_batches++;}
    if(scroll_auditing)frame_presents++;
    for(unsigned i=0;i<count;i++)copy_present_rect(&rects[i],pixels);
    return ESP_OK; /* One no-wait batch, NOT one present per descriptor. */
}
esp_err_t mix_present_rects_deferred(const mix_present_rect_t *rects,unsigned count,const uint16_t *pixels) {
    return mix_present_rects(rects,count,pixels);
}
esp_err_t mix_present_rect(int x0,int y0,int x1,int y1,const uint16_t *pixels) {
    mix_present_rect_t r={x0,y0,x1,y1};
    if(recording)counters.presents++;
    if(scroll_auditing)frame_presents++;
    copy_present_rect(&r,pixels);return ESP_OK;
}
/* This capture suite never launches an app or claims to emulate LCD fades. */
esp_err_t mix_present_fade_begin(const uint16_t *pixels,uint16_t bg) {(void)pixels;(void)bg;assert(!"unexpected application fade");return ESP_FAIL;}
esp_err_t mix_present_fade_step(uint8_t opacity) {(void)opacity;assert(!"unexpected application fade");return ESP_FAIL;}
void mix_present_fade_end(void) {}
const esp_app_desc_t *esp_app_get_description(void) {
    static const esp_app_desc_t desc={.date="HOST PREVIEW",.time="00:00:00",.app_elf_sha256={0x12,0x34,0x56,0x78}};
    return &desc;
}
esp_err_t nvs_open(const char *ns,int mode,nvs_handle_t *handle) {(void)mode;assert(!strcmp(ns,"mixui"));*handle=1;return ESP_OK;}
esp_err_t nvs_get_u8(nvs_handle_t h,const char *key,uint8_t *v) {(void)h;(void)key;(void)v;return ESP_ERR_NVS_NOT_FOUND;}
esp_err_t nvs_set_u8(nvs_handle_t h,const char *key,uint8_t v) {(void)h;(void)key;(void)v;return ESP_OK;}
esp_err_t nvs_commit(nvs_handle_t h) {(void)h;return ESP_OK;}
void nvs_close(nvs_handle_t h) {(void)h;}
static unsigned history_revision;
int batt_log_get(batt_sample_t *out,int max) {
    assert(max==BATT_LOG_CAP);
    for(int i=0;i<max;i++)out[i]=(batt_sample_t){.mv=(uint16_t)(3810+i/12+history_revision*70),.ma=(int16_t)(430+i%37+history_revision*30),.soc=(int8_t)(72+i/120),.plugged=0};
    return max;
}
static mix_net_entry_t networks[MIX_NET_MAX];
static bool network_busy;
static const char *network_message="";
int mix_link_net_list(const mix_net_entry_t **out) {*out=networks;return 8;}
bool mix_link_net_busy(void) {return network_busy;}
const char *mix_link_net_message(void) {return network_message;}

static uint64_t monotonic_us(void) {
    struct timespec t;assert(!clock_gettime(CLOCK_MONOTONIC,&t));
    return (uint64_t)t.tv_sec*1000000+(uint64_t)t.tv_nsec/1000;
}
static uint64_t region_hash(int x0,int y0,int x1,int y1) {
    uint64_t h=UINT64_C(1469598103934665603);
    for(int y=y0;y<y1;y++)for(int x=x0;x<x1;x++){h^=fb[y*W+x];h*=UINT64_C(1099511628211);}
    return h;
}
static void json_string(FILE *f,const char *s) {
    fputc('"',f);
    for(const unsigned char *p=(const unsigned char *)s;*p;p++) {
        if(*p=='"'||*p=='\\'){fputc('\\',f);fputc(*p,f);}
        else if(*p<32)fprintf(f,"\\u%04x",*p);else fputc(*p,f);
    }
    fputc('"',f);
}
static uint32_t next_cp(const char **p) {
    const unsigned char *s=(const unsigned char *)*p;uint32_t cp;int n;
    if(s[0]<0x80){cp=s[0];n=1;}
    else if((s[0]&0xe0)==0xc0){cp=s[0]&31;n=2;}
    else if((s[0]&0xf0)==0xe0){cp=s[0]&15;n=3;}
    else {assert((s[0]&0xf8)==0xf0);cp=s[0]&7;n=4;}
    for(int i=1;i<n;i++){assert(s[i]&&(s[i]&0xc0)==0x80);cp=(cp<<6)|(s[i]&63);}
    *p+=n;return cp;
}
static void measure_ink(struct text_record *t) {
    ttf_metrics_t metrics;assert(ttf_font_metrics(t->size,&metrics));
    int pen=t->x,baseline=t->y+metrics.ascent;
    t->left=t->top=INT_MAX;t->right=t->bottom=INT_MIN;
    t->glyphs=t->solid_pixels=t->overwritten_pixels=0;
    for(const char *p=t->text;*p;) {
        ttf_glyph_t g;uint32_t cp=next_cp(&p);assert(ttf_font_glyph(cp,t->size,&g));t->glyphs++;
        for(int y=0;y<g.h;y++)for(int x=0;x<g.w;x++)if(g.bitmap[y*g.w+x]) {
            int px=pen+g.left+x,py=baseline-g.top+y;
            if(px<t->left)t->left=px;
            if(px+1>t->right)t->right=px+1;
            if(py<t->top)t->top=py;
            if(py+1>t->bottom)t->bottom=py+1;
            /* Both production alpha blenders return the exact foreground at
             * coverage >=250. A later primitive must not erase that ink. */
            if(g.bitmap[y*g.w+x]>=250&&px>=0&&px<W&&py>=t->clip0&&py<t->limit) {
                t->solid_pixels++;
                if(fb[py*W+px]!=t->color)t->overwritten_pixels++;
            }
        }
        pen+=g.advance;
    }
    assert(pen-t->x==t->advance);
    if(t->left==INT_MAX)t->left=t->right=t->x,t->top=t->bottom=t->y;
}
static void diagnose_text(void) {
    /* Run AFTER timing/counters stop. Read the production glyph alpha masks,
     * never Pillow/fontTools metrics. AABB overlap is a candidate, not proof
     * that two nonzero alpha pixels intersect. Vertical viewport cuts while
     * scrolling are reported separately, not treated as UTF-8 corruption. */
    fprintf(report,",\"text\":[");
    for(unsigned i=0;i<text_count;i++) {
        struct text_record *t=&texts[i];measure_ink(t);
        if(i)fputc(',',report);
        fprintf(report,"{\"value\":");json_string(report,t->text);
        fprintf(report,",\"x\":%d,\"y\":%d,\"size\":%d,\"advance\":%d,\"ink\":[%d,%d,%d,%d],\"clip_height\":%d,\"solid_pixels\":%u,\"overwritten_solid_pixels\":%u,\"viewport_cut\":%s}",
                t->x,t->y,t->size,t->advance,t->left,t->top,t->right,t->bottom,t->limit,t->solid_pixels,t->overwritten_pixels,
                (t->top<t->clip0||t->bottom>t->limit)?"true":"false");
    }
    fprintf(report,"],\"issues\":[");unsigned issues=0;
    for(unsigned i=0;i<text_count;i++) {
        struct text_record *t=&texts[i];
        if(t->bottom<=t->clip0||t->top>=t->limit)continue;
        if(t->overwritten_pixels) {
            if(issues++)fputc(',',report);
            fprintf(report,"{\"kind\":\"solid_text_ink_overwritten\",\"text_index\":%u,\"pixels\":%u}",i,t->overwritten_pixels);
        }
        int lo=0,hi=W;
        if(page==PAGE_SETTINGS&&t->limit==CONTENT_H) {
            lo=t->x<BODY_X?RAIL_X:BODY_X;hi=t->x<BODY_X?RAIL_X+RAIL_W:BODY_X+BODY_W;
        }
        if(t->left<lo||t->right>hi) {
            if(issues++)fputc(',',report);
            fprintf(report,"{\"kind\":\"horizontal_ink_overflow\",\"text_index\":%u,\"allowed\":[%d,%d]}",i,lo,hi);
        }
        for(unsigned j=0;j<i;j++) {
            struct text_record *u=&texts[j];
            int y0=t->top>u->top?t->top:u->top,y1=t->bottom<u->bottom?t->bottom:u->bottom;
            int limit=t->limit<u->limit?t->limit:u->limit;
            int clip0=t->clip0>u->clip0?t->clip0:u->clip0;
            if(y0<clip0)y0=clip0;
            if(y1>limit)y1=limit;
            if(t->left<u->right&&t->right>u->left&&y0<y1) {
                if(issues++)fputc(',',report);
                fprintf(report,"{\"kind\":\"ink_bbox_overlap_candidate\",\"text_indices\":[%u,%u]}",j,i);
            }
        }
    }
    layout_issues+=issues;fprintf(report,"],\"layout_issue_count\":%u",issues);
}
static void begin_capture(void) {memset(&counters,0,sizeof(counters));text_count=0;recording=true;}
static void emit_capture(const char *name,uint64_t elapsed,unsigned iterations,bool image,bool diagnose) {
    recording=false;check_guard();
    fprintf(report,"{\"scene\":");json_string(report,name);
    fprintf(report,",\"image\":%s,\"freetype_version\":\"%d.%d.%d\"",image?"true":"false",ft_major,ft_minor,ft_patch);
    fprintf(report,",\"host_elapsed_us\":%"PRIu64",\"iterations\":%u,\"scroll\":%d,\"ui_framebuffers\":%u,"
      "\"ft_loads\":%"PRIu64",\"ft_size_calls\":%"PRIu64",\"text_calls\":%"PRIu64",\"width_calls\":%"PRIu64","
      "\"icon_glyph_calls\":%"PRIu64",\"presents\":%"PRIu64",\"presented_pixels\":%"PRIu64","
      "\"present_batches\":%"PRIu64",\"present_rectangles\":%"PRIu64","
      "\"font_allocations\":%"PRIu64",\"font_allocated_bytes_total\":%"PRIu64",\"missing_codepoints\":[",
      elapsed,iterations,settings_scroll,ui_allocations,counters.load_calls,counters.size_calls,counters.text_calls,
      counters.width_calls,counters.glyph_calls,counters.presents,counters.presented_pixels,
      counters.present_batches,counters.present_rectangles,counters.font_allocations,counters.font_allocated_bytes);
    for(unsigned i=0;i<counters.missing_count;i++)fprintf(report,"%s%u",i?",":"",counters.missing[i]);
    fputc(']',report);
    if(diagnose)diagnose_text();
    fprintf(report,"}\n");fflush(report);
    if(image) {
        char path[1024];assert(snprintf(path,sizeof(path),"%s/%s.rgb565",output_dir,name)<(int)sizeof(path));
        FILE *f=fopen(path,"wb");assert(f);
        /* Explicit little-endian RGB565; no second framebuffer or PNG renderer. */
        unsigned char row[W*2];
        for(int y=0;y<H;y++) {
            for(int x=0;x<W;x++){uint16_t p=fb[y*W+x];row[x*2]=(unsigned char)p;row[x*2+1]=(unsigned char)(p>>8);}
            assert(fwrite(row,1,sizeof(row),f)==sizeof(row));
        }
        assert(!fclose(f));
    }
    printf("%s: host %.3f ms, FT loads=%"PRIu64", size changes=%"PRIu64", text=%"PRIu64", presents=%"PRIu64"\n",
           name,elapsed/1000.0,counters.load_calls,counters.size_calls,counters.text_calls,counters.presents);
}
static void snapshot(const char *name,const mix_view_t *v,unsigned step_ms,bool full) {
    begin_capture();clock_ms+=step_ms;if(full)repaint=true;
    uint64_t start=monotonic_us();mix_ui_tick(v,clock_ms);uint64_t elapsed=monotonic_us()-start;
    assert(!memcmp(panel_pixels,fb,sizeof(panel_pixels)));
    if(locked)assert(counters.presents<=1);
    emit_capture(name,elapsed,1,true,true);
}
static void hidden_primitives(void) {
    begin_capture();uint64_t start=monotonic_us();
    text(300,-1000,53,P.text,"不可见文字");text_fit(300,2000,300,53,P.text,"不可见文字");
    text_mid(500,2000,53,P.text,"不可见文字");text_mid_fit(500,-1000,300,53,P.text,"不可见文字");
    icon(500,-1000,53,P.text,ICON_SYSTEM);icon(500,2000,53,P.text,ICON_SYSTEM);
    uint64_t elapsed=monotonic_us()-start;
    assert(!counters.load_calls&&!counters.width_calls&&!counters.text_calls&&!counters.glyph_calls);
    emit_capture("offscreen-primitives",elapsed,1,false,false);
}
static void ink_audit_selftest(void) {
    /* Prove this test detects a primitive erasing a solid glyph pixel. This
     * modifies only the host framebuffer after all requested PNG captures. */
    begin_capture();rect(300,200,100,100,P.bg);text(320,210,50,P.text,"A");recording=false;
    assert(text_count==1);measure_ink(&texts[0]);assert(!texts[0].overwritten_pixels&&texts[0].solid_pixels);
    int at=-1;
    for(int y=texts[0].top;y<texts[0].bottom&&at<0;y++)for(int x=texts[0].left;x<texts[0].right;x++)
        if(fb[y*W+x]==P.text){at=y*W+x;break;}
    assert(at>=0);uint16_t saved=fb[at];fb[at]=P.warning;measure_ink(&texts[0]);
    assert(texts[0].overwritten_pixels>0);fb[at]=saved;measure_ink(&texts[0]);assert(!texts[0].overwritten_pixels);
    puts("INK_AUDIT_SELFTEST_OK detected and restored an intentionally erased production glyph pixel");
}
static mix_view_t example_view(void) {
    mix_view_t v={.battery_valid=true,.usb_valid=true,.soc_valid=true,.calibration_verified=true,
      .battery_v=3.88f,.battery_a=0.43f,.usb_v=0,.usb_a=0,.soc=78,.capacity_mah=4200,.runtime_hours=5.2f,
      .linux_online=true,.keyboard_online=true,.headphone_valid=true,.audio_ready=true,.mic_l=.35f,.mic_r=.22f,
      .linux_cpu=23,.linux_mem_used_kib=745*1024,.linux_mem_total_kib=4096*1024,.free_psram=4*1024*1024,
      .brightness=7,.sensors_checked=0xffff,.sensors_present=0xa57f,.wifi_reported=true,.wifi_connected=true,
      .wifi_speed_valid=true,.wifi_signal=82,.wifi_rx_bps=128000,.host_time_s=1789894500,.host_tz_offset_min=480};
    strcpy(v.wifi_ssid,"MixOS 实验室");strcpy(v.host_ip,"192.168.1.128");
    const char *names[]={"MixOS 实验室","家庭网络","Office-Guest","WWWWWWWWWWWWWWWWWWWWWWWWWWWWWWWW",
                         "测试热点","Studio Wi-Fi","Open Network","远端网络"};
    for(int i=0;i<8;i++) {
        snprintf(networks[i].ssid,sizeof(networks[i].ssid),"%s",names[i]);
        networks[i].secured=i!=6;networks[i].known=i==0;networks[i].signal=95-i*10;
    }
    return v;
}
/* Compare BOTH the optimized canvas and the pixels actually submitted to the
 * panel with a fresh production draw_all(). Restore the optimized canvas (not
 * the reference) so a wrong pixel can never be hidden before the next drag. */
static void assert_pixels_equal(const char *what,const uint16_t *actual,const uint16_t *expected,const char *label) {
    if(!memcmp(actual,expected,sizeof(scroll_pixels)))return;
    for(int y=0;y<H;y++)for(int x=0;x<W;x++)if(actual[y*W+x]!=expected[y*W+x]) {
        fprintf(stderr,"SCROLL_PIXEL_MISMATCH %s %s section=%d lang=%u theme=%u scroll=%d x=%d y=%d actual=%04x expected=%04x moves=%u presents=%u pixels=%"PRIu64"\n",
                label,what,section,language,theme,settings_scroll,x,y,actual[y*W+x],expected[y*W+x],frame_row_copies,frame_presents,frame_pixels);
        abort();
    }
}
static void scroll_reference(const char *label) {
    check_guard();
    if(draw_clip_y0!=0||draw_limit_y!=CONTENT_H||draw_offset_y!=0) {
        fprintf(stderr,"SCROLL_CLIP_NOT_RESTORED %s clip=[%d,%d) offset=%d\n",label,draw_clip_y0,draw_limit_y,draw_offset_y);abort();
    }
    memcpy(scroll_pixels,fb,sizeof(scroll_pixels));
    bool was_recording=recording;recording=false;
    bool body_dirty=settings_body_dirty,scroll_dirty=settings_scroll_dirty;
    int painted_scroll=settings_painted_scroll;uint32_t frame_ms=settings_frame_ms;
    /* A fresh background proves full redraw does not accidentally depend on
     * the very stale rows under test. No alternate UI allocation is involved. */
    memset(fb,0x69,sizeof(scroll_pixels));draw_all();
    assert_pixels_equal("canvas",scroll_pixels,fb,label);
    assert_pixels_equal("submitted-panel",panel_pixels,fb,label);
    memcpy(fb,scroll_pixels,sizeof(scroll_pixels));
    settings_body_dirty=body_dirty;settings_scroll_dirty=scroll_dirty;
    settings_painted_scroll=painted_scroll;settings_frame_ms=frame_ms;
    recording=was_recording;check_guard();
}
static void scroll_tick(mix_view_t *v,unsigned step,const char *label,bool forbid_reuse) {
    int old=settings_painted_scroll;
    frame_row_copies=frame_presents=frame_rects=0;frame_pixels=0;frame_full_body=false;text_count=0;
    clock_ms+=step;scroll_auditing=true;mix_ui_tick(v,clock_ms);scroll_auditing=false;
    int delta=settings_painted_scroll-old;
    if(forbid_reuse) {
        if(frame_row_copies){fprintf(stderr,"SCROLL_STALE_REUSE %s section=%d delta=%d rows=%u\n",label,section,delta,frame_row_copies);abort();}
        scroll_invalidations++;
    }
    if(frame_row_copies) {
        assert(delta&&abs(delta)<CONTENT_H);
        if(frame_presents!=1||!frame_full_body||frame_pixels!=(uint64_t)(W-BODY_X)*CONTENT_H) {
            fprintf(stderr,"SCROLL_INCOMPLETE_PRESENT %s delta=%d presents=%u rects=%u full_body=%d pixels=%"PRIu64"\n",
                    label,delta,frame_presents,frame_rects,frame_full_body,frame_pixels);abort();
        }
        scroll_partial_frames++;if(abs(delta)==1)scroll_one_px++;
    }
    if(abs(delta)>=CONTENT_H){assert(!frame_row_copies&&frame_full_body);scroll_large++;}
    scroll_reference(label);scroll_frames++;
}
static void scroll_setup(mix_view_t *v,section_t s) {
    assert(!touch_down);navigate(PAGE_SETTINGS);go_section(s);
    settings_scroll=0;notice_visible=false;toast.active=toast.shown=toast.dirty=false;
    touch_test=false;test_dirty=false;repaint=true;view_ms=clock_ms-1000;
    scroll_tick(v,40,"setup",true);
}
static void scroll_stroke(mix_view_t *v,bool downwards,bool tick_each) {
    const int offsets[]={13,14,21,58,159,337,560};
    int start=downwards?40:600;
    mix_ui_touch(BODY_X+100,start,true);
    if(tick_each)scroll_tick(v,40,"touch-down",false);
    for(size_t i=0;i<sizeof(offsets)/sizeof(offsets[0]);i++) {
        int y=start+(downwards?offsets[i]:-offsets[i]);
        mix_ui_touch(BODY_X+100,y,true);
        if(tick_each)scroll_tick(v,40,downwards?"drag-down":"drag-up",false);
    }
    mix_ui_touch(BODY_X+100,downwards?600:40,false);
    if(tick_each)scroll_tick(v,40,"touch-release",false);
    assert(!touch_down&&settings_drag_y==-1&&!modal);
}
static void scroll_to_edge(mix_view_t *v,bool bottom) {
    int edge=bottom?settings_scroll_max():0;
    unsigned strokes=0;
    while(settings_scroll!=edge){scroll_stroke(v,!bottom,true);assert(++strokes<8);}
    /* Continue against the clamp, then reverse, to expose edge-only trails. */
    scroll_stroke(v,!bottom,true);
}
static void scroll_change_checks(mix_view_t *v) {
    /* These are real UI notifications and telemetry, not direct dirty-flag
     * writes. Changes coincide with a drag pending before the next tick. */
    scroll_setup(v,SEC_NETWORK);
    mix_ui_touch(BODY_X+100,600,true);mix_ui_touch(BODY_X+100,587,true);
    v->wifi_connected=false;strcpy(v->wifi_ssid,"Changed network");strcpy(v->host_ip,"10.0.0.9");
    scroll_tick(v,1040,"network-telemetry-during-drag",true);
    mix_ui_touch(BODY_X+100,570,true);v->linux_online=false;
    scroll_tick(v,40,"network-offline-during-drag",true);
    mix_ui_touch(BODY_X+100,550,true);
    strcpy(networks[0].ssid,"扫描结果 Changed");networks[0].signal=17;network_busy=true;network_message="Scan complete";
    /* The link's production event path announces scan changes as a notice. */
    mix_ui_notice("Scan complete 扫描完成");scroll_tick(v,40,"network-list-notice",true);
    mix_ui_touch(BODY_X+100,530,true);scroll_tick(v,40,"notice-visible-drag",true);
    mix_ui_touch(BODY_X+100,510,true);mix_ui_notice("");scroll_tick(v,40,"notice-clear-drag",true);
    mix_ui_touch(BODY_X+100,490,true);scroll_tick(v,40,"after-notice-clear",false);
    mix_ui_touch(BODY_X+100,490,false);scroll_tick(v,40,"network-release",false);
    *v=example_view();network_busy=false;network_message="";
    scroll_setup(v,SEC_POWER);
    mix_ui_touch(BODY_X+100,600,true);mix_ui_touch(BODY_X+100,587,true);
    v->battery_v=4.11f;v->battery_a=-.24f;v->usb_v=5.02f;v->usb_a=.87f;v->capacity_mah=3900;v->runtime_hours=6.7f;
    history_revision++;scroll_tick(v,1040,"power-telemetry-during-drag",true);
    mix_ui_touch(BODY_X+100,580,true);scroll_tick(v,40,"after-power-refresh",false);
    mix_ui_touch(BODY_X+100,580,false);scroll_tick(v,40,"power-release",false);
    scroll_to_edge(v,true);
    mix_ui_touch(BODY_X+100,40,true);mix_ui_touch(BODY_X+100,53,true);
    history_revision++;scroll_tick(v,1040,"history-only-during-drag",true);
    mix_ui_touch(BODY_X+100,53,false);scroll_tick(v,40,"history-release",false);
    scroll_setup(v,SEC_APPEARANCE);
    mix_ui_touch(BODY_X+100,600,true);mix_ui_touch(BODY_X+100,587,true);
    mix_ui_notice("First notice 通知");scroll_tick(v,40,"notice-appears",true);
    mix_ui_touch(BODY_X+100,580,true);mix_ui_notice("Replaced notice 新通知");scroll_tick(v,40,"notice-replaced",true);
    mix_ui_touch(BODY_X+100,573,true);scroll_tick(v,NOTICE_MS+40,"notice-expires-during-drag",true);
    mix_ui_touch(BODY_X+100,566,true);scroll_tick(v,40,"after-notice-expiry",false);
    mix_ui_touch(BODY_X+100,559,true);mix_ui_feedback(MIX_UI_BRIGHTNESS,60);scroll_tick(v,40,"toast-appears",true);
    mix_ui_touch(BODY_X+100,552,true);mix_ui_feedback(MIX_UI_BRIGHTNESS,70);scroll_tick(v,40,"toast-changes",true);
    mix_ui_touch(BODY_X+100,545,true);scroll_tick(v,40,"toast-visible-drag",true);
    mix_ui_touch(BODY_X+100,538,true);scroll_tick(v,1540,"toast-expires-during-drag",true);
    mix_ui_touch(BODY_X+100,531,true);scroll_tick(v,40,"after-toast-expiry",false);
    mix_ui_touch(BODY_X+100,531,false);scroll_tick(v,40,"overlays-release",false);
    /* A visible notice dismissed by real touch must not leave shifted pixels. */
    mix_ui_notice("Dismiss me 点击关闭");scroll_tick(v,40,"notice-before-dismiss",true);
    mix_ui_touch(BODY_X+100,40,true);scroll_tick(v,40,"notice-touch-dismiss",true);
    mix_ui_touch(BODY_X+100,40,false);scroll_tick(v,40,"notice-dismiss-release",false);
}
static void continuous_scroll_checks(mix_view_t *v) {
    for(unsigned lang=0;lang<2;lang++)for(unsigned style=0;style<MIX_THEME_COUNT;style++)for(int s=0;s<SEC_COUNT;s++) {
        language=(uint8_t)lang;theme=(uint8_t)style;
        uint64_t partial_before=scroll_partial_frames,one_before=scroll_one_px;
        scroll_setup(v,(section_t)s);
        uint64_t initial=region_hash(0,0,W,H);
        for(int trip=0;trip<2;trip++) {
            scroll_to_edge(v,true);scroll_to_edge(v,false);scroll_roundtrips++;
            assert(region_hash(0,0,W,H)==initial);
        }
        assert(scroll_partial_frames>partial_before&&scroll_one_px>one_before);
        /* Valid input samples are all within the viewport. Two strokes before
         * one render accumulate a >viewport delta on the two taller pages. */
        if(settings_scroll_max()>CONTENT_H) {
            scroll_stroke(v,false,false);scroll_stroke(v,false,false);
            assert(settings_scroll-settings_painted_scroll>CONTENT_H);
            scroll_tick(v,40,"coalesced-large-up",true);
            scroll_stroke(v,true,false);scroll_stroke(v,true,false);
            assert(settings_painted_scroll-settings_scroll>CONTENT_H);
            scroll_tick(v,40,"coalesced-large-down",true);
        }
        printf("SCROLL_VARIANT_OK section=%d lang=%u theme=%u partial_frames=%"PRIu64" one_pixel=%"PRIu64"\n",
               s,lang,style,scroll_partial_frames-partial_before,scroll_one_px-one_before);
    }
    language=1;theme=0;
    printf("SCROLL_MATRIX_OK variants=%d frames=%"PRIu64" partial=%"PRIu64" delta1=%"PRIu64" large=%"PRIu64" roundtrips=%"PRIu64"\n",
           SEC_COUNT*2*MIX_THEME_COUNT,scroll_frames,scroll_partial_frames,scroll_one_px,scroll_large,scroll_roundtrips);
    fflush(stdout);scroll_change_checks(v);
    assert(scroll_large&&scroll_clipped_calls&&scroll_one_px&&scroll_invalidations);
    fprintf(report,"{\"scene\":\"continuous-touch-scroll\",\"image\":false,\"frames\":%"PRIu64",\"partial_frames\":%"PRIu64",\"one_pixel_frames\":%"PRIu64",\"large_delta_frames\":%"PRIu64",\"roundtrips\":%"PRIu64",\"invalidation_checks\":%"PRIu64",\"clipped_text_calls\":%"PRIu64",\"sections\":%d,\"languages\":2,\"themes\":%d,\"byte_equal\":true,\"panel_equal\":true,\"test_oracle_bytes\":%zu}\n",
            scroll_frames,scroll_partial_frames,scroll_one_px,scroll_large,scroll_roundtrips,scroll_invalidations,scroll_clipped_calls,SEC_COUNT,MIX_THEME_COUNT,sizeof(panel_pixels)+sizeof(scroll_pixels));
    printf("CONTINUOUS_TOUCH_SCROLL_OK frames=%"PRIu64" partial=%"PRIu64" delta1=%"PRIu64" large=%"PRIu64" roundtrips=%"PRIu64" invalidations=%"PRIu64" sections=5 languages=2 themes=%d byte_equal=1 panel_equal=1\n",
           scroll_frames,scroll_partial_frames,scroll_one_px,scroll_large,scroll_roundtrips,scroll_invalidations,MIX_THEME_COUNT);
}

int main(int argc,char **argv) {
    (void)capture_memmove; /* Intentionally unused unless the slow path regresses. */
    assert(argc==3);output_dir=argv[2];
    FILE *f=fopen(argv[1],"rb");assert(f);assert(!fseek(f,0,SEEK_END));long n=ftell(f);assert(n>0&&n<=0x400000);
    rewind(f);font_bytes=0x400000;font_data=malloc(font_bytes);assert(font_data);memset(font_data,0xff,font_bytes);
    assert(fread(font_data,1,(size_t)n,f)==(size_t)n);assert(!fclose(f));font_partition=(esp_partition_t){font_bytes,0x210000};
    char path[1024];snprintf(path,sizeof(path),"%s/scenes.jsonl",output_dir);report=fopen(path,"w");assert(report);
    assert(ttf_font_init()==ESP_OK);mix_terminal_init();assert(mix_ui_init((void *)1)==ESP_OK);
    language=1;theme=0;mix_view_t v=example_view();
    snapshot("home-zh",&v,40,true);
    mix_ui_lock_key();mix_ui_lock_key();snapshot("lock-idle",&v,40,false);
    uint64_t idle_lock=region_hash(0,0,W,CONTENT_H);
    for(unsigned i=0;i<text_count;i++)if(texts[i].limit==CONTENT_H)assert(!strcmp(texts[i].text,"MixOS"));
    mix_ui_touch(512,600,true);snapshot("lock-touch",&v,80,false);
    mix_ui_touch(512,500,true);snapshot("lock-swipe",&v,35,false);
    assert(mix_ui_locked());mix_ui_touch(512,500,false);snapshot("lock-return",&v,70,false);
    snapshot("lock-returned",&v,150,false);assert(region_hash(0,0,W,CONTENT_H)==idle_lock);
    mix_ui_touch(512,700,true);mix_ui_touch(512,500,true);snapshot("lock-ready",&v,35,false);
    assert(mix_ui_locked()&&unlock_ready);mix_ui_touch(512,500,false);assert(!mix_ui_locked());
    snapshot("home-unlocked",&v,40,false);
    const char *sections[]={"appearance","network","power","device","system"};
    navigate(PAGE_SETTINGS);
    for(int s=0;s<SEC_COUNT;s++) {
        go_section((section_t)s);settings_scroll=0;
        ttf_font_deinit();assert(ttf_font_init()==ESP_OK); /* genuine per-page cold cache */
        char name[80];snprintf(name,sizeof(name),"settings-%s-top",sections[s]);snapshot(name,&v,40,true);
        begin_capture();uint64_t start=monotonic_us();
        for(int i=0;i<20;i++){text_count=0;repaint=true;mix_ui_tick(&v,clock_ms);}
        uint64_t elapsed=monotonic_us()-start;
        assert(!counters.load_calls&&!counters.size_calls);
        snprintf(name,sizeof(name),"settings-%s-warm-20",sections[s]);emit_capture(name,elapsed,20,false,false);
        uint64_t rail=region_hash(0,0,BODY_X,CONTENT_H),footer=region_hash(0,STATUS_Y,W,H);
        settings_scroll=settings_scroll_max();settings_body_dirty=true;view_ms=clock_ms;
        snprintf(name,sizeof(name),"settings-%s-bottom",sections[s]);snapshot(name,&v,40,false);
        assert(region_hash(0,0,BODY_X,CONTENT_H)==rail&&region_hash(0,STATUS_Y,W,H)==footer);
    }
    hidden_primitives();ink_audit_selftest();continuous_scroll_checks(&v);assert(ui_allocations==1);check_guard();
    assert(!fclose(report));ttf_font_deinit();free(font_data);free(ui_raw);fb=NULL;
    printf("PRODUCTION_UI_CAPTURE_OK framebuffer_count=1 framebuffer_bytes=1572864 layout_issues=%u\n",layout_issues);
    return 0;
}
