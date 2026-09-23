/* Exercise production ttf_font.c with real FreeType, fault-injected ESP APIs,
 * sentinel framebuffers, and an every-ink-pixel preservation regression. */
#include <assert.h>
#include <limits.h>
#include <math.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <ft2build.h>
#include FT_FREETYPE_H

static int fault, maps, unmaps, allocs;
static size_t size_calls, load_calls;
static FT_Error counted_size(FT_Face face, FT_UInt w, FT_UInt h) {
    size_calls++; return fault==8?1:FT_Set_Pixel_Sizes(face,w,h);
}
static FT_Error counted_load(FT_Face face, FT_ULong cp, FT_Int32 flags) {
    load_calls++; return fault==9?1:FT_Load_Char(face,cp,flags);
}
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
#define FT_Set_Pixel_Sizes counted_size
#define FT_Load_Char counted_load
#include "../firmware/esp32s3/main/ttf_font.c"
#undef FT_Set_Pixel_Sizes
#undef FT_Load_Char
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
    for(size_t i=0;i<sizeof(s_metrics)/sizeof(s_metrics[0]);i++)assert(!s_metrics[i].valid);
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
    assert(memcmp(before,frame,sizeof(before))==0); /* cached metrics preserve the baseline across sizes */
    clear_frame();ttf_draw_cell(frame,FW,FH,4,4,12,24,0,0xffff,'j',0);
    assert(check_cell(4,4,12,24)==0);
    ttf_draw_cell(frame,FW,FH,4,4,12,24,256,0xffff,'j',0);assert(check_cell(4,4,12,24)==0);
    ttf_draw_cell(frame,FW,FH,4,4,12,24,20,0xffff,' ',0);assert(check_cell(4,4,12,24)==0);
}
static void notes_large_cells(void) {
    enum { NW=48, NH=44 };
    uint16_t pixels[PAD+NW*NH+PAD];
    const uint32_t cps[]={'A','g','j',0x4e2d,0x6587};
    for(unsigned i=0;i<sizeof(cps)/sizeof(cps[0]);i++) {
        for(unsigned p=0;p<sizeof(pixels)/sizeof(pixels[0]);p++)pixels[p]=0xffff;
        int width=cps[i]<128?21:42;
        ttf_draw_cell(pixels+PAD,NW,NH,3,2,width,40,34,0,cps[i],true);
        unsigned black=0,ink=0;
        for(int y=0;y<NH;y++)for(int x=0;x<NW;x++) {
            uint16_t p=pixels[PAD+y*NW+x];
            if(x<3||x>=3+width||y<2||y>=42)assert(p==0xffff);
            else {black+=p==0;ink+=p!=0xffff;}
        }
        assert(black>0&&ink>0);
        for(int p=0;p<PAD;p++)assert(pixels[p]==0xffff&&pixels[PAD+NW*NH+p]==0xffff);
    }
    puts("Notes 34px cells passed: black-on-white Latin and CJK, bold, bounded");
}
/* Independent forward-area reference: project every source pixel with ONE
 * floating-point scale, then take maximum coverage in all intersected cells.
 * Production uses inverse integer sampling. A line-metric fit is deliberately
 * NOT the reference: it was what squashed MiSans CJK vertically. */
#define SHAPE_SIDE 64
static double reference_cell(uint8_t *coverage,const glyph_t *g,int w,int h,int size,bool bold) {
    ttf_metrics_t m;assert(ttf_font_metrics(size,&m));
    int a=m.ascent>0?m.ascent:1,d=m.descent>0?m.descent:1,line=a+d;
    int line_h=line<h?line:h,b=(a*line_h+line/2)/line;
    if(b<1)b=1;
    if(b>=line_h)b=line_h-1;
    b+=(h-line_h)/2;
    int xmin=g->left<0?g->left:0,xmax=g->left+g->w+bold;
    if(xmax<g->adv)xmax=g->adv;
    int natural=xmax-xmin;
    double scale=fmin(1.0,(double)w/natural);
    if(g->top>0)scale=fmin(scale,(double)b/g->top);
    if(g->h>g->top)scale=fmin(scale,(double)(h-b)/(g->h-g->top));
    int draw_w=(int)ceil(natural*scale-1e-9),left=(w-draw_w)/2;
    memset(coverage,0,SHAPE_SIDE*SHAPE_SIDE);
    for(int sy=0;sy<g->h;sy++)for(int sx=0;sx<g->w+bold;sx++) {
        uint8_t alpha=sx<g->w?g->bmp[sy*g->w+sx]:0;
        if(bold&&sx>0&&g->bmp[sy*g->w+sx-1]>alpha)alpha=g->bmp[sy*g->w+sx-1];
        if(!alpha)continue;
        int x0=left+(int)floor((g->left+sx-xmin)*scale+1e-9);
        int x1=left+(int)ceil((g->left+sx+1-xmin)*scale-1e-9);
        int y0=b+(int)floor((sy-g->top)*scale+1e-9);
        int y1=b+(int)ceil((sy+1-g->top)*scale-1e-9);
        assert(x0>=0&&x1<=w&&y0>=0&&y1<=h&&x1>x0&&y1>y0);
        for(int y=y0;y<y1;y++)for(int x=x0;x<x1;x++)
            if(alpha>coverage[y*SHAPE_SIDE+x])coverage[y*SHAPE_SIDE+x]=alpha;
    }
    return scale;
}

static void shape_bbox(const uint16_t *pixels,int w,int h,int *bw,int *bh) {
    int x0=w,y0=h,x1=-1,y1=-1;
    for(int y=0;y<h;y++)for(int x=0;x<w;x++)if(pixels[y*w+x]) {
        if(x<x0)x0=x;
        if(x>x1)x1=x;
        if(y<y0)y0=y;
        if(y>y1)y1=y;
    }
    *bw=x1<0?0:x1-x0+1;*bh=y1<0?0:y1-y0+1;
}

static void cell_proportions(void) {
    const int geometries[][3]={{12,24,20},{16,32,26},{21,40,34}};
    const uint32_t cps[]={'A','W','M','i','g','j','p','q','y','_',0x00e9,0x00fc,0x01dc,
                          0x4e2d,0x6587,0x56fd,0x5706,0x7530,0x56de};
    enum { RW=74,RH=74 };
    uint16_t guarded_pixels[PAD+RW*RH+PAD],cell[SHAPE_SIDE*SHAPE_SIDE];
    uint8_t coverage[SHAPE_SIDE*SHAPE_SIDE];
    size_t cases=0,mismatches=0;
    for(size_t gi=0;gi<sizeof(geometries)/sizeof(geometries[0]);gi++)
    for(size_t ci=0;ci<sizeof(cps)/sizeof(cps[0]);ci++)for(int bold=0;bold<2;bold++) {
        int size=geometries[gi][2],w=geometries[gi][0]*(cps[ci]>=0x4e00?2:1),h=geometries[gi][1];
        assert(FT_Get_Char_Index(s_face,cps[ci])); /* Do not test .notdef instead of real CJK/accents. */
        glyph_t *g=cache_get(cps[ci],size);assert(g&&g->bmp);
        double scale=reference_cell(coverage,g,w,h,size,bold);
        memset(cell,0,sizeof(cell));ttf_draw_cell(cell,w,h,0,0,w,h,size,0xffff,cps[ci],bold);
        for(int y=0;y<h;y++)for(int x=0;x<w;x++)
            mismatches+=cell[y*w+x]!=blend565(0,0xffff,cell_alpha_boost(coverage[y*SHAPE_SIDE+x]));
        if(!bold&&(cps[ci]==0x4e2d||cps[ci]==0x6587)) {
            int bw,bh;shape_bbox(cell,w,h,&bw,&bh);
            ttf_metrics_t m;assert(ttf_font_metrics(size,&m));
            printf("CELL shape size=%d cp=U+%04X cell=%dx%d ascent=%d descent=%d source=%dx%d top=%d advance=%d ink=%dx%d uniform_scale=%.6f\n",
                   size,cps[ci],w,h,m.ascent,m.descent,g->w,g->h,g->top,g->adv,bw,bh,scale);
        }
        /* Same projected glyph must survive partial framebuffer clipping.
         * Adjacent single/double-width cells and external guards are untouched. */
        const int positions[][2]={{3,2},{-7,-5},{RW-4,RH-3},{RW,0},{0,RH},{-w,0},{0,-h}};
        for(size_t p=0;p<sizeof(positions)/sizeof(positions[0]);p++) {
            int ox=positions[p][0],oy=positions[p][1];
            for(size_t i=0;i<sizeof(guarded_pixels)/sizeof(guarded_pixels[0]);i++)guarded_pixels[i]=0x1357;
            ttf_draw_cell(guarded_pixels+PAD,RW,RH,ox,oy,w,h,size,0xffff,cps[ci],bold);
            for(int y=0;y<RH;y++)for(int x=0;x<RW;x++) {
                int cx=x-ox,cy=y-oy;uint16_t expected=0x1357;
                if(cx>=0&&cx<w&&cy>=0&&cy<h)
                    expected=blend565(expected,0xffff,cell_alpha_boost(coverage[cy*SHAPE_SIDE+cx]));
                mismatches+=guarded_pixels[PAD+y*RW+x]!=expected;
            }
            for(int i=0;i<PAD;i++)assert(guarded_pixels[i]==0x1357&&guarded_pixels[PAD+RW*RH+i]==0x1357);
            cases++;
        }
    }
    printf("CELL isotropic reference: cases=%zu mismatches=%zu\n",cases,mismatches);fflush(stdout);
    assert(mismatches==0);
    puts("Cell proportions passed: sizes=20,26,34 CJK=2 Latin=1 accents bold clipping uniform_reference=1");
}

/* Isolating each ink pixel prevents a filled body from hiding a lost accent,
 * edge or descender. Also check which side of the shared baseline it reaches.
 * The original 20px tests below remain unchanged. */
static void large_cell_ink(void) {
    const int geometries[][3]={{16,32,26},{21,40,34}};
    const uint32_t cps[]={'g','j','_',0x00e9,0x01dc,0x4e2d,0x6587};
    uint16_t pixels[PAD+SHAPE_SIDE*SHAPE_SIDE+PAD];
    size_t checks=0;
    for(size_t gi=0;gi<sizeof(geometries)/sizeof(geometries[0]);gi++)
    for(size_t ci=0;ci<sizeof(cps)/sizeof(cps[0]);ci++) {
        int size=geometries[gi][2],w=geometries[gi][0]*(cps[ci]>=0x4e00?2:1),h=geometries[gi][1];
        ttf_metrics_t m;assert(ttf_font_metrics(size,&m));
        int line=m.ascent+m.descent,lh=line<h?line:h;
        int base=1+(h-lh)/2+(m.ascent*lh+line/2)/line;
        glyph_t *g=cache_get(cps[ci],size);assert(g&&g->bmp);
        size_t n=(size_t)g->w*g->h;uint8_t *saved=malloc(n);assert(saved);memcpy(saved,g->bmp,n);
        for(int bold=0;bold<2;bold++)for(size_t p=0;p<n;p++) {
            if(!saved[p])continue;
            memset(g->bmp,0,n);g->bmp[p]=255;memset(pixels,0,sizeof(pixels));
            ttf_draw_cell(pixels+PAD,SHAPE_SIDE,SHAPE_SIDE,1,1,w,h,size,0xffff,cps[ci],bold);
            int ink=0;
            for(int y=0;y<SHAPE_SIDE;y++)for(int x=0;x<SHAPE_SIDE;x++)if(pixels[PAD+y*SHAPE_SIDE+x]) {
                assert(x>=1&&x<1+w&&y>=1&&y<1+h);
                assert((int)(p/g->w)<g->top?y<base:y>=base);ink++;
            }
            for(int i=0;i<PAD;i++)assert(!pixels[i]&&!pixels[PAD+SHAPE_SIDE*SHAPE_SIDE+i]);
            assert(ink>0);checks++;
        }
        memcpy(g->bmp,saved,n);free(saved);
    }
    printf("CELL large ink preservation: pixels=%zu sizes=26,34 accents descenders baseline bold guards\n",checks);
}

static void extreme_cell_bounds(void) {
    const int geometries[][3]={{1,2,1},{1,2,255},{64,2,255},{1,64,255},{64,64,255},{9,11,7}};
    const uint32_t cps[]={'j','_',0x01dc,0x4e2d};
    uint16_t pixels[PAD+SHAPE_SIDE*SHAPE_SIDE+PAD];uint8_t coverage[SHAPE_SIDE*SHAPE_SIDE];
    for(size_t gi=0;gi<sizeof(geometries)/sizeof(geometries[0]);gi++)
    for(size_t ci=0;ci<sizeof(cps)/sizeof(cps[0]);ci++)for(int bold=0;bold<2;bold++) {
        int w=geometries[gi][0],h=geometries[gi][1],size=geometries[gi][2];
        glyph_t *g=cache_get(cps[ci],size);assert(g);
        reference_cell(coverage,g,w,h,size,bold);
        memset(pixels,0,sizeof(pixels));
        ttf_draw_cell(pixels+PAD,SHAPE_SIDE,SHAPE_SIDE,0,0,w,h,size,0xffff,cps[ci],bold);
        for(int y=0;y<SHAPE_SIDE;y++)for(int x=0;x<SHAPE_SIDE;x++) {
            uint16_t expected=x<w&&y<h?blend565(0,0xffff,cell_alpha_boost(coverage[y*SHAPE_SIDE+x])):0;
            assert(pixels[PAD+y*SHAPE_SIDE+x]==expected);
        }
        for(int i=0;i<PAD;i++)assert(!pixels[i]&&!pixels[PAD+SHAPE_SIDE*SHAPE_SIDE+i]);
    }
    memset(pixels,0,sizeof(pixels));
    const int invalid[][4]={{INT_MIN,0,12,24},{0,INT_MIN,12,24},{INT_MAX,0,12,24},{0,INT_MAX,12,24},
                           {0,0,0,24},{0,0,65,24},{0,0,12,1},{0,0,12,65}};
    for(size_t i=0;i<sizeof(invalid)/sizeof(invalid[0]);i++)
        ttf_draw_cell(pixels+PAD,SHAPE_SIDE,SHAPE_SIDE,invalid[i][0],invalid[i][1],invalid[i][2],invalid[i][3],26,0xffff,'j',true);
    for(size_t i=0;i<sizeof(pixels)/sizeof(pixels[0]);i++)assert(!pixels[i]);
    puts("CELL extreme bounds passed: 1..64px cells, 1..255px fonts, signed coordinates, invalid cells");
}

static void terminal_contrast(void) {
    assert(cell_alpha_boost(0)==0&&cell_alpha_boost(255)==255);
    for(unsigned a=0;a<256;a++) {
        unsigned expected=a+(a*20u+99u)/100u;if(expected>255)expected=255;
        assert(cell_alpha_boost((uint8_t)a)==expected);
        if(a)assert(cell_alpha_boost((uint8_t)a)>=cell_alpha_boost((uint8_t)(a-1)));
    }
    glyph_t *g=cache_get('A',20);assert(g&&g->bmp);
    size_t n=(size_t)g->w*g->h;
    uint8_t *saved=malloc(n);assert(saved);memcpy(saved,g->bmp,n);
    for(int dark=0;dark<2;dark++)for(unsigned a=0;a<256;a++) {
        uint16_t fg=dark?0x0000:0xffff;
        memset(g->bmp,(int)a,n);clear_frame();
        ttf_draw_cell(frame,FW,FH,4,4,12,24,20,fg,'A',false);
        unsigned boosted=a+(a*20u+99u)/100u;if(boosted>255)boosted=255;
        uint16_t expected=blend565(0x1357,fg,(uint8_t)boosted);
        int changed=check_cell(4,4,12,24);
        if(expected!=0x1357)assert(changed>0);
        for(int i=0;i<FW*FH;i++)assert(frame[i]==0x1357||frame[i]==expected);
        /* Ordinary UI text still uses the unmodified coverage, even with the
         * same cached bitmap. The boost must not contaminate font caching. */
        clear_frame();ttf_draw_text(frame,FW,FH,4,4,20,fg,"A");
        expected=blend565(0x1357,fg,(uint8_t)a);changed=0;
        for(int i=0;i<FW*FH;i++) {
            assert(frame[i]==0x1357||frame[i]==expected);
            changed+=frame[i]!=0x1357;
        }
        if(expected!=0x1357)assert(changed>0);
        for(int i=0;i<PAD;i++)assert(guarded[i]==0x1357&&guarded[PAD+FW*FH+i]==0x1357);
    }
    memcpy(g->bmp,saved,n);free(saved);
    puts("Terminal contrast passed: 256 coverage levels, light/dark ink, ordinary UI text unchanged");
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
static void cache_benchmark(void) {
    const int sizes[]={14,17,20,23,26,30,32,64};
    const char *text="AXgj 中文";
    ttf_glyph_t glyph;
    ttf_metrics_t metrics;
    for(int pass=0;pass<2;pass++) {
        size_calls=load_calls=0;
        int rounds=pass?100:1;
        for(int r=0;r<rounds;r++)for(size_t s=0;s<sizeof(sizes)/sizeof(sizes[0]);s++) {
            int size=sizes[s];
            assert(ttf_font_glyph('A',size,&glyph));
            assert(ttf_text_width(size,text)>0);
            assert(ttf_font_metrics(size,&metrics));
            clear_frame();ttf_draw_text(frame,FW,FH,0,0,size,0xffff,text);
            ttf_draw_cell(frame,FW,FH,4,4,12,24,size,0xffff,'j',false);
        }
        printf("CACHE mixed_sizes phase=%s size_calls=%zu loads=%zu entries=%d bytes=%zu\n",
               pass?"warm":"cold",size_calls,load_calls,s_cache_n,s_cache_bytes);
        if(pass)assert(size_calls==0&&load_calls==0);
    }
    cache_flush();
    for(uint32_t cp='A';cp<'A'+32;cp++)assert(ttf_font_glyph(cp,20,&glyph));
    int pressure_entries=0;
    for(uint32_t cp=0x4e00;cp<0x9fff;cp++) {
        for(uint32_t hot='A';hot<'A'+32;hot++)assert(ttf_font_glyph(hot,20,&glyph));
        int old_n=s_cache_n;
        assert(ttf_font_glyph(cp,64,&glyph));
        assert(s_cache_bytes<=GLYPH_BYTES_MAX);
        pressure_entries++;
        if(s_cache_n<old_n+1)break;
    }
    int retained=s_cache_n;
    size_t retained_bytes=s_cache_bytes,before=load_calls;
    for(uint32_t cp='A';cp<'A'+32;cp++)assert(ttf_font_glyph(cp,20,&glyph));
    printf("CACHE pressure added=%d retained=%d bytes=%zu hot_reloads=%zu/32\n",
           pressure_entries,retained,retained_bytes,load_calls-before);
    assert(retained>32&&load_calls==before);
}

static void assert_cache_consistent(void) {
    int count=0;size_t bytes=0,loads=load_calls,sets=size_calls;
    for(int i=0;i<CACHE_CAP;i++) {
        glyph_t *e=&s_cache[i];if(!e->key)continue;
        count++;bytes+=(size_t)e->w*e->h;
        /* Also checks that no deletion has broken a probe chain. Hits may
         * update the reference bit, but never move metadata/bitmap memory. */
        assert(cache_get(e->key>>8,(int)(e->key&255))==e);
    }
    assert(count==s_cache_n&&bytes==s_cache_bytes);
    assert(count<=CACHE_CAP-64&&bytes<=GLYPH_BYTES_MAX);
    assert(loads==load_calls&&sets==size_calls);
}

static void cache_collisions(void) {
    cache_flush();
    uint32_t cps[9];const uint8_t *bitmaps[9];int found=0;
    for(uint32_t cp=0x100;cp<0x10ffff&&found<9;cp++) {
        if(cp>=0xd800&&cp<=0xdfff)continue;
        if(cache_index((cp<<8)|20)!=(CACHE_CAP-2))continue;
        cps[found++]=cp;
    }
    assert(found==9);
    for(int i=0;i<8;i++) {
        glyph_t *g=cache_get(cps[i],20);assert(g);bitmaps[i]=g->bmp;
    }
    /* This cluster wraps from slot 2047 to slot 0. Delete its head and middle
     * while keeping the surviving bitmap allocations and lookup paths intact. */
    cache_remove((uint32_t)(cache_get(cps[0],20)-s_cache));
    cache_remove((uint32_t)(cache_get(cps[4],20)-s_cache));
    size_t loads=load_calls;
    for(int i=1;i<8;i++)if(i!=4)assert(cache_get(cps[i],20)->bmp==bitmaps[i]);
    assert(load_calls==loads);
    int count=s_cache_n;size_t bytes=s_cache_bytes;
    fault=7;assert(!cache_get('A',31));fault=0;
    assert(s_cache_n==count&&s_cache_bytes==bytes);assert_cache_consistent();
    assert(cache_get('A',31)&&cache_get(cps[8],20));assert_cache_consistent();
    puts("CACHE wraparound deletion, bitmap addresses and allocation rollback passed");
}

static void cache_lifetime_and_failures(void) {
    cache_flush();
    ttf_glyph_t glyph,again;ttf_metrics_t metrics;
    assert(ttf_font_glyph('j',20,&glyph)&&glyph.bitmap);
    size_t n=(size_t)glyph.w*glyph.h;
    uint8_t *saved=malloc(n);assert(saved);memcpy(saved,glyph.bitmap,n);
    assert(ttf_font_metrics(64,&metrics));
    assert(memcmp(saved,glyph.bitmap,n)==0); /* metrics never invalidate a bitmap */
    assert(ttf_font_glyph('A',64,&again));
    size_t loads=load_calls,sets=size_calls;
    fault=8; /* A glyph cache hit must work even if face size changes fail. */
    assert(ttf_font_glyph('j',20,&again)&&again.bitmap==glyph.bitmap);
    assert(!ttf_font_glyph('X',53,&again));
    assert(s_cur_size==64);
    fault=0;assert(load_calls==loads&&size_calls==sets+1);
    assert(memcmp(saved,glyph.bitmap,n)==0);free(saved);
    fault=9;assert(!ttf_font_glyph('X',53,&again));fault=0;
    assert_cache_consistent();assert(ttf_font_glyph('X',53,&again));
    assert(ttf_font_glyph(' ',20,&again)&&!again.bitmap&&again.advance>0);
    assert(!ttf_font_glyph('A',0,&again)&&!ttf_font_glyph('A',256,&again));
    assert(!ttf_font_metrics(0,&metrics)&&!ttf_font_metrics(256,&metrics));
    assert(!ttf_font_glyph('A',20,NULL)&&!ttf_font_metrics(20,NULL));
    assert(ttf_text_width(0,"A")==0&&ttf_text_width(256,"A")==0);
    assert(ttf_font_glyph(0xfffd,20,&glyph));loads=load_calls;
    const uint32_t invalid[]={0,0xd800,0xdfff,0x110000,UINT32_MAX};
    for(size_t i=0;i<sizeof(invalid)/sizeof(invalid[0]);i++)
        assert(ttf_font_glyph(invalid[i],20,&again)&&again.bitmap==glyph.bitmap);
    assert(load_calls==loads);assert_cache_consistent();
    puts("CACHE lifetime, invalid inputs and FreeType failure recovery passed");
}

static void cached_metrics_and_pixels(void) {
    for(int size=1;size<=255;size++) {
        ttf_metrics_t first,again;
        assert(ttf_font_metrics(size,&first)&&set_size(size));
        /* FreeType rounds these scaled face metrics to whole pixels; keep
         * exactly the previous draw_text baseline while memoizing them. */
        assert(first.ascent==(s_face->size->metrics.ascender>>6));
        assert(first.descent==(-s_face->size->metrics.descender+63)/64);
        assert(first.line_height>=first.ascent+first.descent);
        assert(set_size(size==255?1:size+1));
        size_t sets=size_calls;
        assert(ttf_font_metrics(size,&again)&&memcmp(&first,&again,sizeof(first))==0);
        assert(size_calls==sets);
    }
    const int sizes[]={14,17,20,23,26,30,32,64};
    uint16_t saved[FW*FH];
    for(size_t i=0;i<sizeof(sizes)/sizeof(sizes[0]);i++) {
        int size=sizes[i];clear_frame();
        int width=ttf_draw_text(frame,FW,FH,1,1,size,0xffff,"gjA");
        memcpy(saved,frame,sizeof(saved));assert(set_size(size+1));
        size_t sets=size_calls,loads=load_calls;clear_frame();
        assert(ttf_draw_text(frame,FW,FH,1,1,size,0xffff,"gjA")==width);
        assert(memcmp(saved,frame,sizeof(saved))==0);
        assert(size_calls==sets&&load_calls==loads);
    }
    puts("CACHE 255-size metrics and mixed-size text pixels passed");
}

static void cache_capacity_and_churn(void) {
    cache_flush();
    size_t peak=0;
    for(uint32_t i=0;i<3*CACHE_CAP;i++) {
        assert(cache_get(0x10000+i,1));
        int expected=i+1<CACHE_CAP-64?(int)i+1:CACHE_CAP-64;
        assert(s_cache_n==expected);assert(s_cache_bytes<=GLYPH_BYTES_MAX);
        if((i%127)==0)assert_cache_consistent();
    }
    assert_cache_consistent();
    printf("CACHE table_pressure requests=%d retained=%d bytes=%zu\n",
           3*CACHE_CAP,s_cache_n,s_cache_bytes);
    const int sizes[]={1,14,20,32,64,128,255};
    uint32_t random=0x31415926;
    for(int i=0;i<3000;i++) {
        random^=random<<13;random^=random>>17;random^=random<<5;
        uint32_t cp=0x4e00+random%2000;
        int size=sizes[(random>>16)%(sizeof(sizes)/sizeof(sizes[0]))];
        assert(cache_get(cp,size));
        if(s_cache_bytes>peak)peak=s_cache_bytes;
        assert(s_cache_bytes<=GLYPH_BYTES_MAX);
        if((i%31)==0)assert_cache_consistent();
        if((i%67)==0) {
            /* A large failed allocation may first evict entries. The survivors
             * must remain findable and accounting must still match ownership. */
            fault=7;assert(!cache_get('A',255));fault=0;
            assert_cache_consistent();
            glyph_t *retry=cache_get('A',255);assert(retry);
            cache_remove((uint32_t)(retry-s_cache));
        }
    }
    assert_cache_consistent();
    printf("CACHE randomized_pressure requests=3000 peak_bytes=%zu budget=%u\n",peak,GLYPH_BYTES_MAX);
}

static void cache_hash_and_memory(void) {
    typedef struct {
        uint32_t key;int16_t w,h,left,top,adv;uint8_t *bmp;
    } old_glyph_t;
    _Static_assert(sizeof(glyph_t)==sizeof(old_glyph_t),"clock flag must fit existing padding");
    _Static_assert(sizeof(s_metrics)<=2048,"metric memoization stays within 2 KiB");
    bool old_buckets[CACHE_CAP]={false},new_buckets[CACHE_CAP]={false};
    int old_n=0,new_n=0;
    for(uint32_t cp=0x4e00;cp<0x4e00+512;cp++) {
        uint32_t key=(cp<<8)|20,old=(key*2654435761u)&(CACHE_CAP-1),now=cache_index(key);
        if(!old_buckets[old]){old_buckets[old]=true;old_n++;}
        if(!new_buckets[now]){new_buckets[now]=true;new_n++;}
    }
    assert(old_n==8&&new_n>256);
    printf("CACHE hash_512_codepoints old_buckets=%d new_buckets=%d entry_bytes=%zu metrics_bytes=%zu\n",
           old_n,new_n,sizeof(glyph_t),sizeof(s_metrics));
}

/* The reference is the real un-clipped renderer on the SAME nonuniform RGB565
 * background. Compare every byte, including rows on both sides of the clip and
 * guards. This catches alpha blending against the wrong row, not just ink loss. */
#define CLIP_W 173
#define CLIP_H 137
#define CLIP_PIXELS (CLIP_W*CLIP_H)
static void clipped_text(void) {
    uint16_t initial[PAD+CLIP_PIXELS+PAD],full[PAD+CLIP_PIXELS+PAD],actual[PAD+CLIP_PIXELS+PAD];
    for(size_t i=0;i<sizeof(initial)/sizeof(initial[0]);i++)
        initial[i]=(uint16_t)(0x1357u+(uint32_t)i*4051u+((uint32_t)i>>3));
    const int sizes[]={1,7,14,17,20,23,26,30,32,50,64,128,255};
    const char *strings[]={"Agjpqy_ %XY", "中文 gjA", "j中文_"};
    size_t cases=0,ink_cases=0,cut_top=0,cut_bottom=0;
    for(size_t s=0;s<sizeof(sizes)/sizeof(sizes[0]);s++) {
        int size=sizes[s];
        int positions[][2]={{6,7},{-5,-size/2},{CLIP_W-9,CLIP_H-size/2},
                            {0,CLIP_H-1},{4,-size},{CLIP_W,15}};
        for(size_t t=0;t<sizeof(strings)/sizeof(strings[0]);t++)
        for(size_t p=0;p<sizeof(positions)/sizeof(positions[0]);p++) {
            int x=positions[p][0],y=positions[p][1];
            uint16_t color=(s&1)?0x07e0:0xffff;
            memcpy(full,initial,sizeof(full));
            int advance=ttf_draw_text(full+PAD,CLIP_W,CLIP_H,x,y,size,color,strings[t]);
            assert(advance==ttf_text_width(size,strings[t]));
            assert(!memcmp(full,initial,PAD*sizeof(uint16_t)));
            assert(!memcmp(full+PAD+CLIP_PIXELS,initial+PAD+CLIP_PIXELS,PAD*sizeof(uint16_t)));
            /* All single-row clips exercise actual glyph crossings at every
             * possible row, including first/last framebuffer rows. */
            for(int c=-6;c<CLIP_H;c++) {
                int lo=c<0?0:c,hi=c<0?CLIP_H:c+1;
                if(c==-5){lo=0;hi=0;}
                if(c==-4){lo=CLIP_H;hi=CLIP_H;}
                if(c==-3){lo=CLIP_H/2;hi=lo;}
                if(c==-2){lo=9;hi=79;}
                if(c==-1){lo=CLIP_H/2;hi=CLIP_H;}
                memcpy(actual,initial,sizeof(actual));
                assert(ttf_draw_text_clipped(actual+PAD,CLIP_W,CLIP_H,lo,hi,x,y,size,color,strings[t])==advance);
                assert(!memcmp(actual,initial,PAD*sizeof(uint16_t)));
                assert(!memcmp(actual+PAD+CLIP_PIXELS,initial+PAD+CLIP_PIXELS,PAD*sizeof(uint16_t)));
                bool ink=false,above=false,below=false;
                for(int yy=0;yy<CLIP_H;yy++) {
                    const uint16_t *expected=(yy>=lo&&yy<hi?full:initial)+PAD+yy*CLIP_W;
                    assert(!memcmp(actual+PAD+yy*CLIP_W,expected,CLIP_W*sizeof(uint16_t)));
                    bool changed=memcmp(full+PAD+yy*CLIP_W,initial+PAD+yy*CLIP_W,CLIP_W*sizeof(uint16_t))!=0;
                    if(yy<lo)above|=changed;
                    else if(yy>=hi)below|=changed;
                    else ink|=changed;
                }
                cases++;ink_cases+=ink;cut_top+=ink&&above;cut_bottom+=ink&&below;
            }
        }
    }
    assert(ink_cases>100&&cut_top>100&&cut_bottom>100);
    const int invalid[][2]={{-1,CLIP_H},{INT_MIN,0},{0,-1},{20,19},
                           {0,CLIP_H+1},{CLIP_H+1,CLIP_H+1},{0,INT_MAX},{INT_MAX,INT_MIN}};
    for(size_t i=0;i<sizeof(invalid)/sizeof(invalid[0]);i++) {
        memcpy(actual,initial,sizeof(actual));
        assert(ttf_draw_text_clipped(actual+PAD,CLIP_W,CLIP_H,invalid[i][0],invalid[i][1],0,0,20,0xffff,"Ag中文")==0);
        assert(!memcmp(actual,initial,sizeof(actual)));
    }
    memcpy(actual,initial,sizeof(actual));
    assert(!ttf_draw_text_clipped(NULL,CLIP_W,CLIP_H,0,CLIP_H,0,0,20,0xffff,"A"));
    assert(!ttf_draw_text_clipped(actual+PAD,CLIP_W,CLIP_H,0,CLIP_H,0,0,20,0xffff,NULL));
    const int invalid_size[]={INT_MIN,-1,0,256,INT_MAX};
    for(size_t i=0;i<sizeof(invalid_size)/sizeof(invalid_size[0]);i++)
        assert(!ttf_draw_text_clipped(actual+PAD,CLIP_W,CLIP_H,0,CLIP_H,0,0,invalid_size[i],0xffff,"A"));
    assert(!ttf_draw_text_clipped(actual+PAD,0,CLIP_H,0,CLIP_H,0,0,20,0xffff,"A"));
    assert(!ttf_draw_text_clipped(actual+PAD,-1,CLIP_H,0,CLIP_H,0,0,20,0xffff,"A"));
    assert(!ttf_draw_text_clipped(actual+PAD,CLIP_W,0,0,0,0,0,20,0xffff,"A"));
    assert(!ttf_draw_text_clipped(actual+PAD,CLIP_W,-1,0,0,0,0,20,0xffff,"A"));
    assert(!ttf_draw_text_clipped(actual+PAD,CLIP_W,CLIP_H,0,CLIP_H,0,0,20,0xffff,""));
    ttf_font_deinit();
    assert(!ttf_draw_text_clipped(actual+PAD,CLIP_W,CLIP_H,0,CLIP_H,0,0,20,0xffff,"A"));
    assert(!memcmp(actual,initial,sizeof(actual)));assert(ttf_font_init()==ESP_OK);
    printf("TTF clipped text passed: cases=%zu ink_cases=%zu top_crossings=%zu bottom_crossings=%zu sizes=13 invalid_clips=8 outside_unchanged=1 inside_full_equal=1\n",
           cases,ink_cases,cut_top,cut_bottom);
}

int main(int argc,char **argv) {
    assert(argc==2||argc==3);FILE *f=fopen(argv[1],"rb");assert(f);assert(fseek(f,0,SEEK_END)==0);
    long n=ftell(f);assert(n>0);rewind(f);font_bytes=0x400000;assert((size_t)n<=font_bytes);
    font_data=malloc(font_bytes);assert(font_data);
    memset(font_data,0xff,font_bytes);assert(fread(font_data,1,(size_t)n,f)==(size_t)n);fclose(f);
    partition=(esp_partition_t){font_bytes,0x210000};
    if(argc==3&&!strcmp(argv[2],"--cell-proportions")) {
        assert(ttf_font_init()==ESP_OK);cell_proportions();large_cell_ink();extreme_cell_bounds();
    } else if(argc==3&&!strcmp(argv[2],"--clipped-text")) {
        assert(ttf_font_init()==ESP_OK);clipped_text();
    } else if(argc==3) {
        assert(strcmp(argv[2],"--cache-benchmark")==0);assert(ttf_font_init()==ESP_OK);
        int major,minor,patch;FT_Library_Version(s_lib,&major,&minor,&patch);
        printf("FreeType %d.%d.%d cache regression\n",major,minor,patch);
        cache_benchmark();cache_hash_and_memory();cache_collisions();
        cache_lifetime_and_failures();cached_metrics_and_pixels();cache_capacity_and_churn();
        puts("TTF cache regressions passed");
    } else {
        lifecycle();bounds_and_cache();notes_large_cells();terminal_contrast();shared_baseline();every_ink_pixel();
        puts("TTF lifecycle, bounds, metric-cache and ink preservation passed");
    }
    ttf_font_deinit();assert_clean();free(font_data);return 0;
}
