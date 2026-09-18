#!/usr/bin/env python3
"""Capture the ESP32-S3 panel over the MixOS USB link and save it as a PNG.

Runs on the Pi. The device owns the display; this asks it for a copy of the
composed framebuffer and writes the result out as an image.

Why it takes the port for itself
--------------------------------
mixosd holds the CDC link continuously. There is no side channel into it, so
this tool stops the service, runs its own minimal protocol session, and starts
the service again on the way out - including when it fails. Dropping the link
makes the device restart its protocol epoch, which is a reconnect on the panel,
not a reboot.

The session implemented here is the smallest one the device accepts: the ESP
announces itself with HELLO, we answer HELLO_ACK, and from then on the link is
live and the screenshot request can be sent. PING is answered so the device
does not judge the host dead mid-transfer.

The capture is not synchronised with drawing, so an image can show a torn
frame. See mix_ui.h for why that trade is deliberate.
"""
import argparse
from pathlib import Path
import struct
import subprocess
import sys
import time
import zlib

sys.path.insert(0, '/opt/mixos/linux')
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'linux'))
# Highest priority: a protocol.py sitting next to this script. The installed
# copy under /opt can predate the screenshot message types, and importing that
# one instead fails with a confusing AttributeError on Type.SCREEN_REQUEST.
sys.path.insert(0, str(Path(__file__).resolve().parent))

import serial                                            # noqa: E402
from protocol import Channel as C, Type as T, Frame, Decoder   # noqa: E402

SERVICE = 'mixosd'
SESSION = 0x5C0FFEE          # any non-zero value; the device rejects session 0


def service(action):
    """Best-effort service control.

    sudo on this device asks for a password, so this can legitimately fail. The
    caller may have stopped mixosd already; the capture itself will say clearly
    whether the port was actually free.
    """
    done = subprocess.run(['sudo', '-n', 'systemctl', action, SERVICE],
                          check=False, capture_output=True, timeout=30)
    if done.returncode:
        print(f'note: could not {action} {SERVICE} automatically '
              f'(needs sudo); assuming it is already handled', flush=True)
    return done.returncode == 0


def rgb565_to_png(pixels, width, height, path):
    """Write RGB565 little-endian pixels as a PNG, using only the stdlib."""
    rows = bytearray()
    for y in range(height):
        rows.append(0)                                   # filter type: None
        row = pixels[y * width * 2:(y + 1) * width * 2]
        for x in range(0, len(row), 2):
            value = row[x] | (row[x + 1] << 8)
            r = (value >> 11) & 0x1F
            g = (value >> 5) & 0x3F
            b = value & 0x1F
            # Replicate the high bits into the low ones so full-scale stays
            # full-scale; a plain shift would cap white at 248.
            rows += bytes(((r << 3) | (r >> 2), (g << 2) | (g >> 4), (b << 3) | (b >> 2)))

    def chunk(tag, data):
        return (struct.pack('>I', len(data)) + tag + data
                + struct.pack('>I', zlib.crc32(tag + data)))

    png = (b'\x89PNG\r\n\x1a\n'
           + chunk(b'IHDR', struct.pack('>IIBBBBB', width, height, 8, 2, 0, 0, 0))
           + chunk(b'IDAT', zlib.compress(bytes(rows), 6))
           + chunk(b'IEND', b''))
    Path(path).write_bytes(png)


def capture(port, timeout):
    link = serial.Serial(port, 115200, timeout=0.05)
    link.dtr = True                                      # tud_cdc_connected()
    decoder = Decoder()
    epoch = None
    sequence = 0
    requested = False
    info = None
    pixels = None
    received = 0
    deadline = time.time() + timeout

    def send(channel, kind, session=0, payload=b''):
        nonlocal sequence
        link.write(Frame(channel, kind, epoch, session, sequence, payload).encode())
        link.flush()
        sequence = (sequence + 1) & 0xFFFFFFFF

    while time.time() < deadline:
        data = link.read(8192)
        if not data:
            continue
        for frame in decoder.feed(data):
            if frame.channel == C.CONTROL and frame.type == T.HELLO:
                # A new epoch restarts everything, including a transfer that was
                # already running: its frames carried the old epoch and are gone.
                if frame.epoch != epoch:
                    epoch, sequence = frame.epoch, 0
                    requested, info, pixels, received = False, None, None, 0
                send(C.CONTROL, T.HELLO_ACK, payload=struct.pack('<HH', 512, 4096))
                continue
            if epoch is None or frame.epoch != epoch:
                continue
            if frame.channel == C.CONTROL and frame.type == T.PING:
                send(C.CONTROL, T.PONG)
                continue
            if frame.channel != C.MAINTENANCE:
                continue
            if frame.type == T.ERROR:
                raise SystemExit('device refused: '
                                 + frame.payload.decode('utf-8', 'replace'))
            if frame.type == T.SCREEN_INFO and len(frame.payload) >= 12:
                width, height, total, chunk_size = struct.unpack('<HHII', frame.payload[:12])
                info = (width, height, total, chunk_size)
                pixels = bytearray(total)
                received = 0
                print(f'{width}x{height} RGB565, {total} bytes, {chunk_size} per frame',
                      flush=True)
                continue
            if frame.type == T.SCREEN_DATA and info and len(frame.payload) > 4:
                offset = struct.unpack_from('<I', frame.payload)[0]
                body = frame.payload[4:]
                if offset + len(body) > info[2]:
                    raise SystemExit('device sent data past the framebuffer end')
                pixels[offset:offset + len(body)] = body
                received += len(body)
                continue
            if frame.type == T.SCREEN_END and info:
                if received != info[2]:
                    raise SystemExit(f'short capture: {received}/{info[2]} bytes')
                return info[0], info[1], bytes(pixels)

        if epoch is not None and not requested:
            send(C.MAINTENANCE, T.SCREEN_REQUEST, SESSION)
            requested = True

    if info:
        raise SystemExit(f'timed out after {received}/{info[2]} bytes')
    raise SystemExit('timed out before the device answered; is mixosd stopped?')


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--port', default='/dev/ttyACM0')
    p.add_argument('--out', default='/tmp/esp_screen.png', type=Path)
    p.add_argument('--timeout', default=60.0, type=float)
    p.add_argument('--keep-service-stopped', action='store_true')
    a = p.parse_args()

    service('stop')
    time.sleep(1.5)                       # let the node settle after the release
    try:
        started = time.time()
        width, height, pixels = capture(a.port, a.timeout)
        rgb565_to_png(pixels, width, height, a.out)
        print(f'saved {a.out} ({width}x{height}) in {time.time() - started:.1f}s')
    finally:
        if not a.keep_service_stopped:
            service('start')


if __name__ == '__main__':
    main()
