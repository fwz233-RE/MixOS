/* Exercise production ttf_font.c with real FreeType, fault-injected ESP APIs,
 * sentinel framebuffers, and an every-ink-pixel preservation regression. */
#include <assert.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <ft2build.h>
#include FT_FREETYPE_H

static int fault, maps, unmaps, allocs;
static unsigned char *font_data;
static size_t font_bytes;
static FT_Error injected_init(FT_Library *lib) {return fault==3 ? 1 : FT_Init_FreeType(lib);}
static FT_Error injected_face(FT_Library lib,const FT_Byte *data,FT_Long n,FT_Long index,FT_Face *face) {
    return fault==4 ? 1 : FT_New_Memory_Face(lib,data,n,index,face);
}
static FT_Error injected_cmap(FT_Face face,FT_Encoding enc) {return fault==5 ? 1 : FT_Select_Charmap(face,enc);}
#define FT_Init_FreeType injected_init
#define FT_New_Memory_Face injected_face
#define FT_Select_Charmap injected_cmap
#include "../firmware/esp32s3/main/ttf_font.c"
#undef FT_Init_FreeType
#undef FT_New_Memory_Face
#undef FT_Select_Charmap

void test_log(const char *tag,const char *fmt,...) {(void)tag;(void)fmt;}
const char *esp_err_to_name(esp_err_t error) {(void)error;return "test error";}
void *heap_caps_malloc(size_t n,unsigned caps) {(void)caps;allocs++;return fault==7?NULL:malloc(n);}
void *heap_caps_calloc(size_t n,size_t size,unsigned caps) {(void)caps;allocs++;return fault==6?NULL:calloc(n,size);}
static esp_partition_t partition;
const esp_partition_t *esp_partition_find_first(int type,int subtype,const char *label) {
    (void)type;(void)subtype;assert(strcmp(label,"font")==0);return fault==1?NULL:&partition;
}
esp_err_t esp_partition_mmap(const esp_partition_t *part,size_t offset,size_t size,int type,const void **ptr,esp_partition_mmap_handle_t *handle) {
    (void)type;assert(part==&partition&&offset==0&&size==font_bytes);
    if(fault==2)return ESP_FAIL;
    *ptr=font_data;*handle=123;maps++;return ESP_OK;
}
void esp_partition_munmap(esp_partition_mmap_handle_t handle) {assert(handle==123);unmaps++;}
static void assert_clean(void) {
    assert(!ttf_font_ready()&&!s_face&&!s_lib&&!s_cache&&!s_mapped&&s_cur_size==-1);
    assert(maps==unmaps);
}
static void lifecycle(void) {
    for(int failure=1;failure<=6;failure++) {
        fault=failure;assert(ttf_font_init()!=ESP_OK);assert_clean();
        fault=0;assert(ttf_font_init()==ESP_OK);int old_maps=maps,old_allocs=allocs;
        assert(ttf_font_init()==ESP_OK&&maps==old_maps&&allocs==old_allocs);
        ttf_font_deinit();ttf_font_deinit();assert_clean();
    }
    unsigned char first=font_data[0];font_data[0]=0xff;
    assert(ttf_font_init()==ESP_ERR_INVALID_STATE);assert_clean();font_data[0]=first;
    partition.size=8;assert(ttf_font_init()==ESP_ERR_INVALID_SIZE);assert_clean();partition.size=font_bytes;
    /* Valid sfnt header followed by malformed tables must release FT + mmap. */
    unsigned char *saved_data=font_data; font_data=calloc(1,font_bytes);assert(font_data);font_data[1]=1;
    assert(ttf_font_init()!=ESP_OK);assert_clean();free(font_data);font_data=saved_data;
    assert(ttf_font_init()==ESP_OK);
    fault=7;assert(!cache_get('j',20));fault=0;assert(cache_get('j',20));
}
#define FW 40
#define FH 32
#define PAD 23
static uint16_t guarded[PAD+FW*FH+PAD];
static uint16_t *frame=guarded+PAD;
static void clear_frame(void) {
    for(size_t i=0;i<sizeof(guarded)/sizeof(guarded[0]);i++)guarded[i]=0x1357;
}
static int check_cell(int x,int y,int w,int h) {
    int changed=0;
    for(int i=0;i<PAD;i++)assert(guarded[i]==0x1357&&guarded[PAD+FW*FH+i]==0x1357);
    for(int yy=0;yy<FH;yy++)for(int xx=0;xx<FW;xx++) {
        int inside=xx>=x&&xx<x+w&&yy>=y&&yy<y+h;
        if(!inside)assert(frame[yy*FW+xx]==0x1357);
        else if(frame[yy*FW+xx]!=0x1357)changed++;
    }
    return changed;
}
static void bounds_and_cache(void) {
    uint32_t cps[]={'%','X','Y','_','g','j','p','q','r','y',0x4e2d,0x6587};
    for(size_t c=0;c<sizeof(cps)/sizeof(cps[0]);c++)for(int bold=0;bold<2;bold++)for(int w=12;w<=24;w+=12) {
        int positions[][2]={{4,4},{-3,-2},{30,20},{40,0},{0,32}};
        for(size_t p=0;p<sizeof(positions)/sizeof(positions[0]);p++) {
            int x=positions[p][0],y=positions[p][1];clear_frame();
            ttf_draw_cell(frame,FW,FH,x,y,w,24,20,0xffff,cps[c],bold);
            int ink=check_cell(x,y,w,24);if(p==0)assert(ink>0);
        }
    }
    clear_frame();ttf_draw_cell(frame,FW,FH,4,4,12,24,20,0xffff,'j',0);
    uint16_t before[FW*FH];memcpy(before,frame,sizeof(before));
    assert(ttf_text_width(64,"A")>0);clear_frame();
    ttf_draw_cell(frame,FW,FH,4,4,12,24,20,0xffff,'j',0);
    assert(memcmp(before,frame,sizeof(before))==0); /* cached glyph still resets face metrics */
    clear_frame();ttf_draw_cell(frame,FW,FH,4,4,12,24,0,0xffff,'j',0);
    assert(check_cell(4,4,12,24)==0);
    ttf_draw_cell(frame,FW,FH,4,4,12,24,256,0xffff,'j',0);assert(check_cell(4,4,12,24)==0);
    ttf_draw_cell(frame,FW,FH,4,4,12,24,20,0xffff,' ',0);assert(check_cell(4,4,12,24)==0);
}
static void shared_baseline(void) {
    assert(set_size(20));
    int ascent=(int)((s_face->size->metrics.ascender+63)/64);
    int descent=(int)((-s_face->size->metrics.descender+63)/64);
    int line=ascent+descent,height=line<24?line:24;
    int base=4+(24-height)/2+(ascent*height+line/2)/line;
    uint32_t cps[]={'g','j','p','q','y'};
    for(size_t c=0;c<sizeof(cps)/sizeof(cps[0]);c++) {
        glyph_t *g=cache_get(cps[c],20);assert(g&&g->top>0&&g->top<g->h);
        size_t n=(size_t)g->w*g->h;uint8_t *saved=malloc(n);assert(saved);memcpy(saved,g->bmp,n);
        for(int below=0;below<2;below++) {
            memset(g->bmp,0,n);memset(g->bmp+(g->top-1+below)*g->w,255,g->w);
            clear_frame();ttf_draw_cell(frame,FW,FH,4,4,12,24,20,0xffff,cps[c],false);
            assert(check_cell(4,4,12,24)>0);
            for(int yy=4;yy<28;yy++)for(int xx=4;xx<16;xx++)if(frame[yy*FW+xx]!=0x1357)
                assert(below ? yy>=base : yy<base);
        }
        memcpy(g->bmp,saved,n);free(saved);
    }
}
static void every_ink_pixel(void) {
    uint32_t cps[]={'%','X','Y','_','g','j','p','q','r','y',0x4e2d,0x6587};
    size_t checks=0;
    for(size_t c=0;c<sizeof(cps)/sizeof(cps[0]);c++) {
        glyph_t *g=cache_get(cps[c],20);assert(g&&g->bmp);
        size_t n=(size_t)g->w*g->h;uint8_t *saved=malloc(n);assert(saved);memcpy(saved,g->bmp,n);
        for(int w=12;w<=24;w+=12)for(size_t pixel=0;pixel<n;pixel++) {
            if(!saved[pixel])continue;
            memset(g->bmp,0,n);g->bmp[pixel]=255;clear_frame();
            ttf_draw_cell(frame,FW,FH,4,4,w,24,20,0xffff,cps[c],false);
            assert(check_cell(4,4,w,24)>0);checks++;
        }
        memcpy(g->bmp,saved,n);free(saved);
    }
    printf("Preserved %zu source ink pixels across 12px/24px cells\n",checks);
}
int main(int argc,char **argv) {
    assert(argc==2);FILE *f=fopen(argv[1],"rb");assert(f);assert(fseek(f,0,SEEK_END)==0);
    long n=ftell(f);assert(n>0);rewind(f);font_bytes=0x400000;font_data=malloc(font_bytes);assert(font_data);
    memset(font_data,0xff,font_bytes);assert(fread(font_data,1,(size_t)n,f)==(size_t)n);fclose(f);
    partition=(esp_partition_t){font_bytes,0x210000};lifecycle();bounds_and_cache();shared_baseline();every_ink_pixel();
    ttf_font_deinit();assert_clean();free(font_data);puts("TTF lifecycle, bounds, metric-cache and ink preservation passed");return 0;
}
