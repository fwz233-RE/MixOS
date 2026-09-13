/* SPDX-License-Identifier: MIT
 * Bounded text terminal. Not an xterm emulator; see protocol/TERMINAL.md.
 */
#include "mix_terminal.h"
#include <string.h>
#ifdef ESP_PLATFORM
#include "esp_attr.h"
#define TERM_RAM EXT_RAM_BSS_ATTR
#else
#define TERM_RAM
#endif
static TERM_RAM mix_cell_t screen[MIX_TERM_ROWS][MIX_TERM_COLS];
static TERM_RAM mix_cell_t history[MIX_TERM_HISTORY][MIX_TERM_COLS];
static bool dirty[MIX_TERM_ROWS];
static int x,y,saved_x,saved_y,head,count,offset,top,bottom;
static uint8_t fg,bg,attr,state;
static bool wrap,visible,autowrap,private_mode;
static int params[12],np;
static uint32_t cp,min_cp;static int utf_left;
static mix_cell_t blank(void){return (mix_cell_t){' ',fg,bg,attr,1};}
static int clamp(int v,int hi){return v<0?0:(v>hi?hi:v);}
void mix_terminal_invalidate(void){for(int i=0;i<MIX_TERM_ROWS;i++)dirty[i]=true;}
static void clear_row(int row,int a,int b){
    a=clamp(a,MIX_TERM_COLS-1);b=clamp(b,MIX_TERM_COLS-1);
    if(a>0&&screen[row][a].width==0)a--;
    if(b<MIX_TERM_COLS-1&&screen[row][b].width==2)b++;
    for(int i=a;i<=b;i++)screen[row][i]=blank();
    dirty[row]=true;
}
static void scroll_up(void){
    if(top==0&&bottom==MIX_TERM_ROWS-1){
        memcpy(history[head],screen[0],sizeof(screen[0]));head=(head+1)%MIX_TERM_HISTORY;
        if(count<MIX_TERM_HISTORY)count++;
        if(offset>0&&offset<count)offset++;
    }
    if(bottom>top)memmove(screen[top],screen[top+1],(size_t)(bottom-top)*sizeof(screen[0]));
    clear_row(bottom,0,MIX_TERM_COLS-1);mix_terminal_invalidate();
}
static void newline(void){wrap=false;if(y==bottom)scroll_up();else if(y<MIX_TERM_ROWS-1)y++;}
void mix_terminal_init(void){
    fg=7;bg=0;attr=0;x=y=saved_x=saved_y=head=count=offset=0;
    top=0;bottom=MIX_TERM_ROWS-1;visible=autowrap=true;wrap=false;state=0;utf_left=0;
    for(int i=0;i<MIX_TERM_ROWS;i++)clear_row(i,0,MIX_TERM_COLS-1);
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
    if(w==2&&x==MIX_TERM_COLS-1){if(autowrap){x=0;newline();}else {c=0xfffd;w=1;}}
    clear_row(y,x,x+w-1);
    screen[y][x]=(mix_cell_t){c,fg,bg,attr,(uint8_t)w};
    if(w==2)screen[y][x+1]=(mix_cell_t){0,fg,bg,attr,0};
    x+=w;if(x>=MIX_TERM_COLS){x=MIX_TERM_COLS-1;wrap=true;}
    dirty[y]=true;
}
static int par(int i,int def){return i<np&&params[i]>0?params[i]:def;}
static void csi(uint8_t cmd){
    int n=par(0,1);wrap=false;
    if(private_mode){
        if(cmd=='h'||cmd=='l')for(int i=0;i<np;i++){
            if(params[i]==25)visible=cmd=='h';if(params[i]==7)autowrap=cmd=='h';}
        return;
    }
    switch(cmd){
    case 'A':y=clamp(y-n,MIX_TERM_ROWS-1);break;
    case 'B':case 'e':y=clamp(y+n,MIX_TERM_ROWS-1);break;
    case 'C':case 'a':x=clamp(x+n,MIX_TERM_COLS-1);break;
    case 'D':x=clamp(x-n,MIX_TERM_COLS-1);break;
    case 'E':y=clamp(y+n,MIX_TERM_ROWS-1);x=0;break;
    case 'F':y=clamp(y-n,MIX_TERM_ROWS-1);x=0;break;
    case 'G':case '`':x=clamp(n-1,MIX_TERM_COLS-1);break;
    case 'd':y=clamp(n-1,MIX_TERM_ROWS-1);break;
    case 'H':case 'f':y=clamp(par(0,1)-1,MIX_TERM_ROWS-1);x=clamp(par(1,1)-1,MIX_TERM_COLS-1);break;
    case 'J':
        if(params[0]==2||params[0]==3){for(int r=0;r<MIX_TERM_ROWS;r++)clear_row(r,0,MIX_TERM_COLS-1);if(params[0]==3){count=offset=0;}}
        else if(params[0]==0){clear_row(y,x,MIX_TERM_COLS-1);for(int r=y+1;r<MIX_TERM_ROWS;r++)clear_row(r,0,MIX_TERM_COLS-1);}
        else if(params[0]==1){for(int r=0;r<y;r++)clear_row(r,0,MIX_TERM_COLS-1);clear_row(y,0,x);}break;
    case 'K':if(params[0]==0)clear_row(y,x,MIX_TERM_COLS-1);else if(params[0]==1)clear_row(y,0,x);else if(params[0]==2)clear_row(y,0,MIX_TERM_COLS-1);break;
    case 'm':for(int i=0;i<np;i++){int p=params[i];
        if(p==0){fg=7;bg=0;attr=0;}else if(p==1)attr|=1;else if(p==7)attr|=2;
        else if(p==22)attr&=(uint8_t)~1;else if(p==27)attr&=(uint8_t)~2;
        else if(p>=30&&p<=37)fg=(uint8_t)(p-30);else if(p>=40&&p<=47)bg=(uint8_t)(p-40);
        else if(p>=90&&p<=97)fg=(uint8_t)(p-90+8);else if(p>=100&&p<=107)bg=(uint8_t)(p-100+8);
        else if(p==39)fg=7;else if(p==49)bg=0;
    }break;
    case 's':saved_x=x;saved_y=y;break;
    case 'u':x=saved_x;y=saved_y;break;
    case 'r':{int a=par(0,1)-1,b=par(1,MIX_TERM_ROWS)-1;if(a>=0&&b<MIX_TERM_ROWS&&a<b){top=a;bottom=b;x=y=0;}}break;
    default:break; /* unsupported controls ignored, never executed */
    }
}
static void byte(uint8_t b){
    if(state==3){if(b==7)state=0;else if(b==27)state=4;return;} /* OSC/DCS ignored */
    if(state==4){state=b=='\\'?0:3;return;}
    if(state==1){state=0;
        if(b=='['){state=2;np=1;memset(params,0,sizeof(params));private_mode=false;}
        else if(b==']'||b=='P'||b=='^'||b=='_')state=3;
        else if(b=='7'){saved_x=x;saved_y=y;}else if(b=='8'){x=saved_x;y=saved_y;}
        else if(b=='D')newline();else if(b=='E'){x=0;newline();}
        else if(b=='c')mix_terminal_init();
        else if(b=='('||b==')')state=5;
        return;
    }
    if(state==5){state=0;return;} /* ignore charset designation */
    if(state==2){
        if(b==27){state=1;return;}
        if(b=='?'&&np==1&&params[0]==0){private_mode=true;return;}
        if(b>='0'&&b<='9'){if(params[np-1]<10000)params[np-1]=params[np-1]*10+b-'0';return;}
        if(b==';'){if(np<12)np++;else state=6;return;}
        if(b>=0x40&&b<=0x7e){csi(b);state=0;}else if(b<0x20){state=0;}return;
    }
    if(state==6){if(b>=0x40&&b<=0x7e)state=0;return;}
    if(utf_left){
        if((b&0xc0)==0x80){cp=(cp<<6)|(b&63);if(--utf_left==0)put_cp(cp<min_cp||cp>0x10ffff||(cp>=0xd800&&cp<=0xdfff)?0xfffd:cp);return;}
        utf_left=0;put_cp(0xfffd); /* reprocess invalid continuation */
    }
    if(b==27){state=1;return;}
    if(b=='\r'){x=0;wrap=false;return;}if(b=='\n'||b==11||b==12){newline();return;}
    if(b=='\b'){if(x>0)x--;if(x>0&&screen[y][x].width==0)x--;wrap=false;return;}
    if(b=='\t'){x=clamp((x/8+1)*8,MIX_TERM_COLS-1);wrap=false;return;}
    if(b<32||b==127)return;
    if(b<128){put_cp(b);return;}
    if(b>=0xc2&&b<=0xdf){cp=b&31;utf_left=1;min_cp=0x80;}
    else if(b>=0xe0&&b<=0xef){cp=b&15;utf_left=2;min_cp=0x800;}
    else if(b>=0xf0&&b<=0xf4){cp=b&7;utf_left=3;min_cp=0x10000;}
    else put_cp(0xfffd);
}
void mix_terminal_feed(const uint8_t *data,size_t len){
    if(!data)return;dirty[y]=true;for(size_t i=0;i<len;i++)byte(data[i]);dirty[y]=true;
    if(offset)mix_terminal_invalidate();
}
const mix_cell_t *mix_terminal_row(int row){
    if(row<0||row>=MIX_TERM_ROWS)return NULL;
    int logical=count-offset+row;
    if(logical<count)return history[(head-count+logical+MIX_TERM_HISTORY)%MIX_TERM_HISTORY];
    return screen[logical-count];
}
bool mix_terminal_dirty(int row){return row>=0&&row<MIX_TERM_ROWS&&dirty[row];}
void mix_terminal_clean(void){memset(dirty,0,sizeof(dirty));}
int mix_terminal_cursor_x(void){return x;}
int mix_terminal_cursor_y(void){return y;}
bool mix_terminal_cursor_visible(void){return visible&&!offset;}
void mix_terminal_scroll(int lines){offset=clamp(offset+lines,count);mix_terminal_invalidate();}
int mix_terminal_scroll_offset(void){return offset;}
