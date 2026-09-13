// uac_dbg.h — UAC 数据通路无锁统计计数器（调试 dwc2 split-ISO 丢包用）
//
// 并发规则（严格遵守，否则重蹈 cdc_printf 重入死锁的覆辙）：
//   - 写入方：tinyusb 任务 / ISR 上下文，只做 32 位对齐自增/赋值（Xtensa 原子），
//     不打日志、不调 CDC API、不拿锁；
//   - 读取方：独立低优先级 stats 任务，1Hz 快照算增量。允许极小概率的
//     读撕裂（多个字段非同一瞬间），对诊断无影响。
#pragma once
#include <stdint.h>

typedef struct {
    // tud_audio_rx_done_isr（每个被 tinyusb 接受的 ISO OUT 包必经）
    volatile uint32_t rx_pkts;        // 收到的 ISO OUT 包数
    volatile uint32_t rx_bytes;       // 累计字节
    volatile uint32_t rx_min;         // 最小包长（0xFFFFFFFF=未见包）
    volatile uint32_t rx_max;         // 最大包长
    volatile uint32_t rx_gap_over;    // 流内相邻包间隔 >1.5ms 的次数（丢 1ms 微帧的痕迹）
    volatile uint32_t rx_max_gap_us;  // 流内最大包间隔（µs）
    volatile uint32_t fifo_clear;     // new_play 判定触发的 FIFO 清空次数（=流重启次数）
    // tud_audio_tx_done_isr（mic 方向：每个发出的 ISO IN 包必经；
    // 以后查 dwc2 ISO IN split 问题就对比这里的设备侧发包数 vs 主机侧收包数）
    volatile uint32_t tx_pkts;        // 发出的 ISO IN 包数（含 0 长包）
    volatile uint32_t tx_bytes;       // 累计发出字节
    // 控制面事件（tinyusb 任务上下文）
    volatile uint32_t set_itf;        // Set Interface alt!=0（开流）次数
    volatile uint32_t itf_close;      // Set Interface alt=0（关流）次数
    volatile uint32_t mount;          // USB mount 次数
    volatile uint32_t umount;
    volatile uint32_t suspend;
    volatile uint32_t resume;
} uac_dbg_stats_t;

extern uac_dbg_stats_t g_uac_dbg;
