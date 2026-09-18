/* SPDX-License-Identifier: MIT
 * Bounded text terminal. Not an xterm emulator; see protocol/TERMINAL.md.
 * Enough of VT100/VT220 plus xterm's alternate screen and extended colours to
 * host a full-screen TUI, with every write bounded by the active grid.
 */
#include "mix_terminal.h"
#include <string.h>
#ifdef ESP_PLATFORM
#include "esp_attr.h"
#define TERM_RAM EXT_RAM_BSS_ATTR
#else
#define TERM_RAM
#endif
#define PARAM_MAX 16

static TERM_RAM mix_cell_t primary[MIX_TERM_ROWS][MIX_TERM_COLS];
static TERM_RAM mix_cell_t alt_buffer[MIX_TERM_ROWS][MIX_TERM_COLS];
static TERM_RAM mix_cell_t history[MIX_TERM_HISTORY][MIX_TERM_COLS];
static mix_cell_t (*screen)[MIX_TERM_COLS] = primary;
static bool dirty[MIX_TERM_ROWS];
static bool tabstop[MIX_TERM_COLS];
static int cols = MIX_TERM_COLS, rows = MIX_TERM_ROWS;
static int x,y,head,count,offset,top,bottom;
static mix_color_t fg,bg;
static uint8_t attr,state,intermediate;
static bool wrap,visible,autowrap,origin,alt_active;
typedef struct { int x,y; mix_color_t fg,bg; uint8_t attr; bool origin; } cursor_t;
/* DECSC and the alternate screen keep independent cursors; sharing one lets a
 * TUI that saves the cursor lose it to an unrelated screen switch. */
static cursor_t saved, alt_saved;
static int params[PARAM_MAX],np;
static uint8_t colon[PARAM_MAX];
/* The byte after CSI that selects a private sequence family. Only '?' is
 * implemented; '>' and '=' queries are recognised so they cannot be mistaken
 * for the standard sequence that shares their final byte. */
static uint8_t lead;
static uint32_t cp,min_cp; static int utf_left;
static uint32_t last_print;
static mix_terminal_reply_cb reply_cb; static void *reply_ctx;

void mix_terminal_set_reply(mix_terminal_reply_cb cb,void *ctx){reply_cb=cb;reply_ctx=ctx;}
static void reply(const char *s){
    if(reply_cb&&s&&*s)reply_cb((const uint8_t*)s,strlen(s),reply_ctx);
}
static int clamp(int v,int lo,int hi){return v<lo?lo:(v>hi?hi:v);}
void mix_terminal_invalidate(void){for(int i=0;i<MIX_TERM_ROWS;i++)dirty[i]=true;}
int mix_terminal_cols(void){return cols;}
int mix_terminal_rows(void){return rows;}
bool mix_terminal_alternate(void){return alt_active;}

/* Erasure keeps the current background (background-colour erase) but drops the
 * foreground and attributes, so a stray bold/underline cannot flood the grid. */
static mix_cell_t blank(void){return (mix_cell_t){' ',MIX_COLOR_DEFAULT_FG,bg,0,1};}
static void clear_row(int row,int a,int b){
    if(row<0||row>=rows)return;
    a=clamp(a,0,cols-1);b=clamp(b,0,cols-1);
    if(a>b)return;
    /* Never leave half of a wide glyph behind: it would render as a lone
     * continuation cell with no character to continue. */
    if(a>0&&screen[row][a].width==0)a--;
    if(b<cols-1&&screen[row][b].width==2)b++;
    for(int i=a;i<=b;i++)screen[row][i]=blank();
    dirty[row]=true;
}
static void save_cursor(cursor_t *c){c->x=x;c->y=y;c->fg=fg;c->bg=bg;c->attr=attr;c->origin=origin;}
static void restore_cursor(const cursor_t *c){
    x=clamp(c->x,0,cols-1);y=clamp(c->y,0,rows-1);
    fg=c->fg;bg=c->bg;attr=c->attr;origin=c->origin;wrap=false;
}
static void reset_tabs(void){for(int i=0;i<MIX_TERM_COLS;i++)tabstop[i]=(i%8)==0;}

/* Moves lines within an inclusive row range; the vacated end is cleared. */
static void rotate_up(int first,int last,int n){
    if(n<=0||first>last||first<0||last>=rows)return;
    int span=last-first+1;
    if(n>span)n=span;
    for(int r=first;r+n<=last;r++)memcpy(screen[r],screen[r+n],(size_t)cols*sizeof(mix_cell_t));
    for(int r=last-n+1;r<=last;r++)clear_row(r,0,cols-1);
    mix_terminal_invalidate();
}
static void rotate_down(int first,int last,int n){
    if(n<=0||first>last||first<0||last>=rows)return;
    int span=last-first+1;
    if(n>span)n=span;
    for(int r=last;r-n>=first;r--)memcpy(screen[r],screen[r-n],(size_t)cols*sizeof(mix_cell_t));
    for(int r=first;r<first+n&&r<=last;r++)clear_row(r,0,cols-1);
    mix_terminal_invalidate();
}
static void scroll_up(void){
    /* Only the primary screen with a full-height region feeds scrollback. The
     * alternate screen is transient by definition and must not pollute it. */
    if(!alt_active&&top==0&&bottom==rows-1){
        memcpy(history[head],screen[0],(size_t)cols*sizeof(mix_cell_t));
        for(int i=cols;i<MIX_TERM_COLS;i++)
            history[head][i]=(mix_cell_t){' ',MIX_COLOR_DEFAULT_FG,MIX_COLOR_DEFAULT_BG,0,1};
        head=(head+1)%MIX_TERM_HISTORY;
        if(count<MIX_TERM_HISTORY)count++;
        if(offset>0&&offset<count)offset++;
    }
    rotate_up(top,bottom,1);
}
static void newline(void){wrap=false;if(y==bottom)scroll_up();else if(y<rows-1)y++;}
static void reverse_newline(void){wrap=false;if(y==top)rotate_down(top,bottom,1);else if(y>0)y--;}
static int row_min(void){return origin?top:0;}
static int row_max(void){return origin?bottom:rows-1;}

static void reset_state(void){
    fg=MIX_COLOR_DEFAULT_FG;bg=MIX_COLOR_DEFAULT_BG;attr=0;
    x=y=head=count=offset=0;
    top=0;bottom=rows-1;
    visible=autowrap=true;wrap=origin=alt_active=false;
    state=0;intermediate=0;lead=0;utf_left=0;last_print=0;
    screen=primary;
    reset_tabs();
    memset(&saved,0,sizeof(saved));saved.fg=MIX_COLOR_DEFAULT_FG;saved.bg=MIX_COLOR_DEFAULT_BG;
    alt_saved=saved;
    for(int i=0;i<rows;i++)clear_row(i,0,cols-1);
    screen=alt_buffer;
    for(int i=0;i<rows;i++)clear_row(i,0,cols-1);
    screen=primary;
    mix_terminal_invalidate();
}
void mix_terminal_init(void){reset_state();}
bool mix_terminal_resize(int new_cols,int new_rows){
    if(new_cols<8||new_cols>MIX_TERM_COLS||new_rows<4||new_rows>MIX_TERM_ROWS)return false;
    if(new_cols==cols&&new_rows==rows)return true;
    cols=new_cols;rows=new_rows;
    reset_state();
    return true;
}
/* Soft reset (CSI ! p): attributes and modes return to power-on values while
 * the visible text stays where it is. */
static void soft_reset(void){
    fg=MIX_COLOR_DEFAULT_FG;bg=MIX_COLOR_DEFAULT_BG;attr=0;
    top=0;bottom=rows-1;origin=false;autowrap=true;visible=true;wrap=false;
    memset(&saved,0,sizeof(saved));saved.fg=MIX_COLOR_DEFAULT_FG;saved.bg=MIX_COLOR_DEFAULT_BG;
    reset_tabs();
}
static void switch_alternate(bool on){
    if(on==alt_active)return;
    if(on){
        save_cursor(&alt_saved);
        alt_active=true;screen=alt_buffer;
        for(int r=0;r<rows;r++)clear_row(r,0,cols-1);
        x=y=0;wrap=false;offset=0;top=0;bottom=rows-1;origin=false;
    }else{
        /* Clear while the alternate buffer is still selected so the next
         * application does not inherit the previous one's screen. */
        for(int r=0;r<rows;r++)clear_row(r,0,cols-1);
        alt_active=false;screen=primary;
        restore_cursor(&alt_saved);
        top=0;bottom=rows-1;
    }
    mix_terminal_invalidate();
}

static int width_of(uint32_t c){
    if((c>=0x300&&c<=0x36f)||(c>=0xfe00&&c<=0xfe0f))return 0;
    return (c>=0x1100&&((c<=0x115f)||(c>=0x2e80&&c<=0xa4cf)||(c>=0xac00&&c<=0xd7a3)||
           (c>=0xf900&&c<=0xfaff)||(c>=0xfe10&&c<=0xfe6f)||(c>=0xff01&&c<=0xff60)||
           (c>=0x1f300&&c<=0x1faff)||(c>=0x20000&&c<=0x3ffff)))?2:1;
}
static void put_cp(uint32_t c){
    int w=width_of(c);if(!w)return;
    if(wrap){if(autowrap){x=0;newline();}wrap=false;}
    if(w==2&&x==cols-1){if(autowrap){x=0;newline();}else {c=0xfffd;w=1;}}
    clear_row(y,x,x+w-1);
    screen[y][x]=(mix_cell_t){c,fg,bg,attr,(uint8_t)w};
    if(w==2)screen[y][x+1]=(mix_cell_t){0,fg,bg,attr,0};
    x+=w;if(x>=cols){x=cols-1;wrap=true;}
    dirty[y]=true;
    last_print=c;
}
/* A shift can cut a wide glyph in half, leaving a lead without its
 * continuation or a continuation with no lead. Either one renders as garbage,
 * so the whole broken pair is erased. */
static void repair_row(int row){
    for(int i=0;i<cols;i++){
        if(screen[row][i].width==2){
            if(i+1<cols&&screen[row][i+1].width==0)i++;
            else screen[row][i]=blank();
        }else if(screen[row][i].width==0)screen[row][i]=blank();
    }
    dirty[row]=true;
}
/* Insert/delete characters shift the rest of the row; the row length never
 * changes, so the tail is filled or dropped rather than reallocated. */
static void insert_chars(int n){
    n=clamp(n,0,cols-x);
    if(!n)return;
    for(int i=cols-1;i>=x+n;i--)screen[y][i]=screen[y][i-n];
    for(int i=x;i<x+n;i++)screen[y][i]=blank();
    repair_row(y);
}
static void delete_chars(int n){
    n=clamp(n,0,cols-x);
    if(!n)return;
    for(int i=x;i+n<cols;i++)screen[y][i]=screen[y][i+n];
    for(int i=cols-n;i<cols;i++)screen[y][i]=blank();
    repair_row(y);
}
static void tab_forward(int n){
    for(;n>0;n--){
        int next=cols-1;
        for(int i=x+1;i<cols;i++)if(tabstop[i]){next=i;break;}
        x=next;
        if(x>=cols-1)break;
    }
    wrap=false;
}
static void tab_backward(int n){
    for(;n>0;n--){
        int prev=0;
        for(int i=x-1;i>=0;i--)if(tabstop[i]){prev=i;break;}
        x=prev;
        if(!x)break;
    }
    wrap=false;
}

static int par(int i,int def){return i<np&&params[i]>0?params[i]:def;}
static uint8_t channel(int v){return (uint8_t)clamp(v,0,255);}
/* Parses 38/48/58 extended colour at index i; returns parameters consumed. */
static int sgr_color(int i,mix_color_t *out){
    if(i+1>=np)return 1;
    int kind=params[i+1];
    if(kind==5){
        if(i+2>=np)return 2;
        *out=(mix_color_t)clamp(params[i+2],0,255);
        return 3;
    }
    if(kind==2){
        /* The ITU form 38:2:<colourspace>:r:g:b carries one extra leading
         * field that the common 38;2;r;g;b form does not. */
        int base=i+2;
        if(colon[i+1]&&np-base>=4)base++;
        if(base+2>=np)return np-i;
        *out=MIX_COLOR_RGB(channel(params[base]),channel(params[base+1]),channel(params[base+2]));
        return base+3-i;
    }
    return 2;
}
static void sgr(void){
    if(!np){fg=MIX_COLOR_DEFAULT_FG;bg=MIX_COLOR_DEFAULT_BG;attr=0;return;}
    for(int i=0;i<np;i++){
        int p=params[i];
        if(p==0){fg=MIX_COLOR_DEFAULT_FG;bg=MIX_COLOR_DEFAULT_BG;attr=0;}
        else if(p==1)attr|=MIX_ATTR_BOLD;
        else if(p==2)attr|=MIX_ATTR_DIM;
        else if(p==4)attr|=MIX_ATTR_UNDERLINE;
        else if(p==7)attr|=MIX_ATTR_INVERSE;
        else if(p==21||p==22)attr&=(uint8_t)~(MIX_ATTR_BOLD|MIX_ATTR_DIM);
        else if(p==24)attr&=(uint8_t)~MIX_ATTR_UNDERLINE;
        else if(p==27)attr&=(uint8_t)~MIX_ATTR_INVERSE;
        else if(p==38)i+=sgr_color(i,&fg)-1;
        else if(p==48)i+=sgr_color(i,&bg)-1;
        else if(p==58){mix_color_t ignored=0;i+=sgr_color(i,&ignored)-1;}
        else if(p==39)fg=MIX_COLOR_DEFAULT_FG;
        else if(p==49)bg=MIX_COLOR_DEFAULT_BG;
        else if(p>=30&&p<=37)fg=(mix_color_t)(p-30);
        else if(p>=40&&p<=47)bg=(mix_color_t)(p-40);
        else if(p>=90&&p<=97)fg=(mix_color_t)(p-90+8);
        else if(p>=100&&p<=107)bg=(mix_color_t)(p-100+8);
        /* Italic, blink, overline and their resets have no cell representation
         * in a single-face renderer and are dropped rather than faked. */
    }
}
static void private_mode_set(bool on){
    for(int i=0;i<np;i++)switch(params[i]){
    case 6: origin=on;x=0;y=on?top:0;wrap=false;break;
    case 7: autowrap=on;break;
    case 25: visible=on;break;
    case 47: case 1047: case 1049: switch_alternate(on);break;
    case 1048: if(on)save_cursor(&saved);else restore_cursor(&saved);break;
    default: break; /* cursor keys, mouse, focus, paste and window modes are inert */
    }
}
static void csi(uint8_t cmd){
    int n=par(0,1);
    char out[32];
    if(intermediate=='!'){if(cmd=='p')soft_reset();return;}
    if(intermediate){return;} /* cursor style and other intermediates are inert */
    if(lead=='?'){
        if(cmd=='h'||cmd=='l')private_mode_set(cmd=='h');
        return;
    }
    if(lead){return;} /* secondary/tertiary device attributes are not answered */
    switch(cmd){
    case 'A':{int lim=(y>=top)?top:0;y=y-n<lim?lim:y-n;wrap=false;}break;
    case 'B':case 'e':{int lim=(y<=bottom)?bottom:rows-1;y=y+n>lim?lim:y+n;wrap=false;}break;
    case 'C':case 'a':x=clamp(x+n,0,cols-1);wrap=false;break;
    case 'D':x=clamp(x-n,0,cols-1);wrap=false;break;
    case 'E':{int lim=(y<=bottom)?bottom:rows-1;y=y+n>lim?lim:y+n;x=0;wrap=false;}break;
    case 'F':{int lim=(y>=top)?top:0;y=y-n<lim?lim:y-n;x=0;wrap=false;}break;
    case 'G':case '`':x=clamp(n-1,0,cols-1);wrap=false;break;
    case 'd':y=clamp(n-1+(origin?top:0),row_min(),row_max());wrap=false;break;
    case 'H':case 'f':
        y=clamp(par(0,1)-1+(origin?top:0),row_min(),row_max());
        x=clamp(par(1,1)-1,0,cols-1);wrap=false;break;
    case 'I':tab_forward(n);break;
    case 'Z':tab_backward(n);break;
    case 'J':
        wrap=false;
        if(params[0]==2||params[0]==3){
            for(int r=0;r<rows;r++)clear_row(r,0,cols-1);
            if(params[0]==3){count=offset=0;}
        }else if(params[0]==0){
            clear_row(y,x,cols-1);for(int r=y+1;r<rows;r++)clear_row(r,0,cols-1);
        }else if(params[0]==1){
            for(int r=0;r<y;r++)clear_row(r,0,cols-1);
            clear_row(y,0,x);
        }break;
    case 'K':
        wrap=false;
        if(params[0]==0)clear_row(y,x,cols-1);
        else if(params[0]==1)clear_row(y,0,x);
        else if(params[0]==2)clear_row(y,0,cols-1);
        break;
    case 'L':if(y>=top&&y<=bottom){rotate_down(y,bottom,n);x=0;wrap=false;}break;
    case 'M':if(y>=top&&y<=bottom){rotate_up(y,bottom,n);x=0;wrap=false;}break;
    case '@':insert_chars(n);wrap=false;break;
    case 'P':delete_chars(n);wrap=false;break;
    case 'X':{int end=clamp(x+n-1,0,cols-1);clear_row(y,x,end);wrap=false;}break;
    case 'S':rotate_up(top,bottom,n);wrap=false;break;
    case 'T':rotate_down(top,bottom,n);wrap=false;break;
    case 'b':{
        /* REP repeats the last graphic character; a bounded count keeps a
         * hostile stream from spinning here. */
        if(!last_print)break;
        int limit=clamp(n,0,cols*rows);
        for(int i=0;i<limit;i++)put_cp(last_print);
    }break;
    case 'g':
        if(params[0]==3){for(int i=0;i<MIX_TERM_COLS;i++)tabstop[i]=false;}
        else if(!params[0]&&x<MIX_TERM_COLS)tabstop[x]=false;
        break;
    case 'm':sgr();break;
    case 's':save_cursor(&saved);break;
    case 'u':restore_cursor(&saved);break;
    case 'n':
        if(params[0]==5)reply("\033[0n");
        else if(params[0]==6){
            int report_y=y-(origin?top:0);
            out[0]=0;
            /* Hand-built: no snprintf in the parser's hot path. */
            {
                char *p=out;int a=report_y+1,b=x+1;
                *p++='\033';*p++='[';
                if(a>=10)*p++=(char)('0'+a/10);
                *p++=(char)('0'+a%10);*p++=';';
                if(b>=10)*p++=(char)('0'+b/10);
                *p++=(char)('0'+b%10);*p++='R';*p=0;
            }
            reply(out);
        }break;
    case 'c':reply("\033[?62;22c");break; /* VT220 with colour, as terminfo claims */
    case 'r':{
        int a=par(0,1)-1,b=par(1,rows)-1;
        if(a>=0&&b<rows&&a<b){top=a;bottom=b;x=0;y=origin?top:0;wrap=false;}
    }break;
    default:break; /* unsupported controls ignored, never executed */
    }
}
static void escape(uint8_t b){
    switch(b){
    case '7':save_cursor(&saved);break;
    case '8':restore_cursor(&saved);break;
    case 'D':newline();break;
    case 'E':x=0;newline();break;
    case 'M':reverse_newline();break;
    case 'H':if(x<MIX_TERM_COLS)tabstop[x]=true;break;
    case 'c':reset_state();break;
    case '=':case '>':break; /* keypad modes: the local keyboard has one mapping */
    default:break;
    }
}
static void byte(uint8_t b){
    if(state==3){if(b==7)state=0;else if(b==27)state=4;return;} /* OSC/DCS ignored */
    if(state==4){state=b=='\\'?0:3;return;}
    if(state==1){
        state=0;
        if(b=='['){
            state=2;np=1;memset(params,0,sizeof(params));memset(colon,0,sizeof(colon));
            lead=0;intermediate=0;
        }
        else if(b==']'||b=='P'||b=='^'||b=='_')state=3;
        else if(b=='('||b==')'||b=='*'||b=='+'||b=='#'||b==' ')state=5;
        else escape(b);
        return;
    }
    if(state==5){state=0;return;} /* charset and other two-byte designations */
    if(state==2){
        if(b==27){state=1;return;}
        if(b>='<'&&b<='?'&&np==1&&!params[0]&&!intermediate&&!lead){lead=b;return;}
        if(b>='0'&&b<='9'){if(params[np-1]<100000)params[np-1]=params[np-1]*10+b-'0';return;}
        if(b==';'||b==':'){
            if(np<PARAM_MAX){colon[np]=(uint8_t)(b==':');params[np]=0;np++;}
            else state=6;
            return;
        }
        if(b>=0x20&&b<=0x2f){intermediate=b;return;}
        if(b>=0x40&&b<=0x7e){csi(b);state=0;}
        else if(b<0x20){state=0;}
        return;
    }
    if(state==6){if(b>=0x40&&b<=0x7e)state=0;return;} /* over-long parameter run */
    if(utf_left){
        if((b&0xc0)==0x80){
            cp=(cp<<6)|(b&63);
            if(--utf_left==0)put_cp(cp<min_cp||cp>0x10ffff||(cp>=0xd800&&cp<=0xdfff)?0xfffd:cp);
            return;
        }
        utf_left=0;put_cp(0xfffd); /* reprocess invalid continuation */
    }
    if(b==27){state=1;return;}
    if(b=='\r'){x=0;wrap=false;return;}
    if(b=='\n'||b==11||b==12){newline();return;}
    if(b=='\b'){if(x>0)x--;if(x>0&&screen[y][x].width==0)x--;wrap=false;return;}
    if(b=='\t'){tab_forward(1);return;}
    if(b<32||b==127)return;
    if(b<128){put_cp(b);return;}
    if(b>=0xc2&&b<=0xdf){cp=b&31;utf_left=1;min_cp=0x80;}
    else if(b>=0xe0&&b<=0xef){cp=b&15;utf_left=2;min_cp=0x800;}
    else if(b>=0xf0&&b<=0xf4){cp=b&7;utf_left=3;min_cp=0x10000;}
    else put_cp(0xfffd);
}
void mix_terminal_feed(const uint8_t *data,size_t len){
    if(!data)return;
    dirty[y]=true;
    for(size_t i=0;i<len;i++)byte(data[i]);
    dirty[y]=true;
    if(offset)mix_terminal_invalidate();
}
const mix_cell_t *mix_terminal_row(int row){
    if(row<0||row>=rows)return NULL;
    int logical=count-offset+row;
    if(logical<count)return history[(head-count+logical+MIX_TERM_HISTORY)%MIX_TERM_HISTORY];
    int line=logical-count;
    return line<rows?screen[line]:NULL;
}
bool mix_terminal_dirty(int row){return row>=0&&row<rows&&dirty[row];}
void mix_terminal_clean(void){memset(dirty,0,sizeof(dirty));}
int mix_terminal_cursor_x(void){return x;}
int mix_terminal_cursor_y(void){return y;}
bool mix_terminal_cursor_visible(void){return visible&&!offset;}
void mix_terminal_scroll(int lines){
    /* The alternate screen has no scrollback to reach. */
    if(alt_active){offset=0;return;}
    offset=clamp(offset+lines,0,count);
    mix_terminal_invalidate();
}
int mix_terminal_scroll_offset(void){return offset;}
