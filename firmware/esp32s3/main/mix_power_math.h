#pragma once
#include <stdbool.h>
#include <stdint.h>
/* Actual monotonic elapsed time; reject stale segments instead of inventing charge. */
static inline float mix_integrate_mah(float prev_ma,float now_ma,int64_t elapsed_us) {
    if(elapsed_us<=0 || elapsed_us>60000000)return 0;
    if(prev_ma<0)prev_ma=0;if(now_ma<0)now_ma=0;
    return (prev_ma+now_ma)*0.5f*((float)elapsed_us/3600000000.0f);
}
