#!/usr/bin/env python3
"""TypixDeck ESP32-S3 屏幕截图回传（跑在 Pi 上）。

固件收到 CDC 命令 "EGGFLY_SCREENSHOT" 后回传：
    ">>> SCREENSHOT 1024x768 RGB565LE 1572864\r\nSCRN" + 1.5MB 原始像素

用法: python3 esp_screenshot.py [/dev/ttyACM0] [/tmp/esp_screen.rgb565]
转 PNG（Mac 侧）: ffmpeg -f rawvideo -pix_fmt rgb565le -s 1024x768 \
                    -i esp_screen.rgb565 esp_screen.png
"""
import sys
import time

import serial

PORT = sys.argv[1] if len(sys.argv) > 1 else "/dev/ttyACM0"
OUT = sys.argv[2] if len(sys.argv) > 2 else "/tmp/esp_screen.rgb565"
W, H = 1024, 768
TOTAL = W * H * 2

s = serial.Serial(PORT, 115200, timeout=5)
s.dtr = True                      # tud_cdc_connected() 需要 DTR
time.sleep(0.2)
s.reset_input_buffer()
s.write(b"EGGFLY_SCREENSHOT\n")
s.flush()

# 跳过统计文本，定位 "SCRN" 定界符（逐字节滑窗，杜绝跨界漏检）
tail = b""
t0 = time.time()
while b"SCRN" not in tail:
    b1 = s.read(1)
    if not b1:
        if time.time() - t0 > 15:
            sys.exit("timeout: no SCRN marker (固件没带 SCREENSHOT 命令?)")
        continue
    tail = (tail + b1)[-4:]

data = bytearray()
t0 = time.time()
while len(data) < TOTAL:
    chunk = s.read(TOTAL - len(data))
    if not chunk:
        sys.exit(f"timeout: got {len(data)}/{TOTAL} bytes")
    data += chunk
    if time.time() - t0 > 120:
        sys.exit(f"overall timeout: got {len(data)}/{TOTAL} bytes")

with open(OUT, "wb") as f:
    f.write(data)
dt = time.time() - t0
print(f"saved {OUT}: {len(data)} bytes in {dt:.1f}s ({len(data)/dt/1024:.0f} KB/s)")
