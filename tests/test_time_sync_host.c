/* Host-only clock regression: production protocol, link and real cJSON. */
#define main maintenance_fixture_main
#include "link_fixture.h"
#undef main

int main(void){
    setup();printf("READY %u\n",epoch);
    char line[1200],hex[1100];unsigned ms;
    mix_decoder_t decoder={0};mix_frame_t f;
    while(fgets(line,sizeof(line),stdin)){
        if(sscanf(line,"T %u",&ms)==1){now=ms;rx_time=ms;}
        else if(sscanf(line,"D %u",&ms)==1){now=ms;transport_open=false;}
        else if(sscanf(line,"R %u",&ms)==1){now=ms;transport_open=true;restart_link();}
        else if(sscanf(line,"F %u",&ms)==1){now=ms;io_fault=true;}
        else if(sscanf(line,"X %u",&ms)==1){now=ms;}
        else{
            assert(sscanf(line,"%u %1099s",&ms,hex)==2);
            now=ms;rx_time=ms;
            size_t n=strlen(hex);assert(n%2==0);
            for(size_t i=0;i<n;i+=2){
                unsigned byte;assert(sscanf(hex+i,"%2x",&byte)==1);
                if(mix_decoder_push(&decoder,(uint8_t)byte,&f))
                    assert(xQueueSend(rxq,&f,0)==pdTRUE);
            }
        }
        mix_link_tick(now,&view);
        printf("%u %d %d %u\n",view.host_time_s,view.host_tz_offset_min,view.linux_online,epoch);
        xQueueReset(controlq);
    }
    assert(!ferror(stdin));return 0;
}
