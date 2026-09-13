#!/usr/bin/env python3
"""Push an ESP32-S3 application into the inactive A/B slot over USB CDC.

This replaces the ROM-download-mode workflow for ordinary upgrades. Nothing is
erased until the image has been fully received and its SHA-256 matches, the
running slot is left untouched, and the new build must prove itself after
reboot or the bootloader rolls back on its own.

    tools/ota_esp.py --device /dev/serial/by-id/usb-TypixDeck_... \\
                     --image firmware/esp32s3/build/mixos_esp32s3.bin \\
                     --service mixosd.service

Only the standard library is used, so it runs on the CM5 as-is.
"""
import argparse
import hashlib
import os
from pathlib import Path
import struct
import subprocess
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'linux'))

from protocol import Channel as C, Type as T, Frame, Decoder, MAX_PAYLOAD  # noqa: E402

# Must match MIX_OTA_CHUNK / MIX_OTA_WINDOW in firmware/esp32s3/main/mix_ota.h.
CHUNK = MAX_PAYLOAD - 4
WINDOW = 8
SESSION = 0x0A7A0001
ESP32S3_CHIP_ID = 9
IMAGE_MAGIC = 0xE9


class UpdateError(Exception):
    pass


class Timeout(UpdateError):
    """The device went quiet. Distinct from a refusal, which is never retried."""


def inspect_image(path):
    """Reject anything that is not an ESP32-S3 application before touching flash."""
    data = Path(path).read_bytes()
    if len(data) < 1024:
        raise UpdateError(f'{path}: too small to be an application image')
    if data[0] != IMAGE_MAGIC:
        raise UpdateError(f'{path}: missing 0xE9 image magic; is this an .elf or a merged image?')
    chip_id = struct.unpack_from('<H', data, 12)[0]
    if chip_id != ESP32S3_CHIP_ID:
        raise UpdateError(f'{path}: built for chip id {chip_id}, expected {ESP32S3_CHIP_ID} (ESP32-S3)')
    return data, hashlib.sha256(data).digest()


class Link:
    """Minimal MixOS host: completes the handshake and answers heartbeats.

    `transport` only has to provide read(max_bytes) -> bytes (may be empty) and
    write(bytes) -> int (may be 0 when the kernel buffer is full), so the
    protocol is testable without hardware.

    Everything this host sends goes through one ordered outbox. A window of
    chunks is several kilobytes, which is more than a tty write buffer holds,
    so short writes are the normal case rather than a disconnection: the outbox
    is pushed out while the device's replies are read, because reading those
    replies is what makes room in the buffer.
    """

    def __init__(self, transport, clock=time.monotonic, write_timeout=10.0):
        self.transport = transport
        self.clock = clock
        self.write_timeout = write_timeout
        self.decoder = Decoder()
        self.epoch = 0
        self.sequence = 0
        self.pending = []
        self.outbox = bytearray()

    def queue(self, channel, kind, session=0, payload=b''):
        if not self.epoch:
            raise UpdateError('no link epoch; handshake first')
        self.outbox += Frame(channel, kind, self.epoch, session, self.sequence, payload).encode()
        self.sequence = (self.sequence + 1) & 0xFFFFFFFF

    def send(self, channel, kind, session=0, payload=b''):
        self.queue(channel, kind, session, payload)
        self.flush()

    def push(self):
        """One opportunistic pass over the outbox. True when it is empty."""
        while self.outbox:
            written = self.transport.write(bytes(self.outbox))
            if written <= 0:
                return False
            del self.outbox[:written]
        return True

    def flush(self):
        deadline = self.clock() + self.write_timeout
        while not self.push():
            self.pending.extend(self.receive())
            if self.clock() >= deadline:
                raise Timeout(f'the device stopped accepting data for '
                              f'{self.write_timeout:.0f} s; is it still attached?')
            time.sleep(0.001)

    def receive(self):
        """Decode what has arrived, answering handshake and heartbeats inline."""
        out = []
        for frame in self.decoder.feed(self.transport.read(4096)):
            if frame.channel == C.CONTROL and frame.type == T.HELLO:
                if frame.epoch != self.epoch:
                    # A new epoch means the device restarted the link; frames
                    # queued for the old one would be rejected as replays.
                    self.epoch = frame.epoch
                    self.sequence = 0
                    self.outbox.clear()
                self.queue(C.CONTROL, T.HELLO_ACK,
                           payload=struct.pack('<HH', MAX_PAYLOAD, 4096))
                continue
            if frame.epoch != self.epoch:
                continue
            if frame.channel == C.CONTROL and frame.type == T.PING:
                self.queue(C.CONTROL, T.PONG)
                continue
            if frame.channel == C.CONTROL and frame.type == T.PONG:
                continue
            if frame.channel == C.STATUS and frame.type == T.STATUS_REQUEST:
                continue
            out.append(frame)
        return out

    def poll(self):
        """Return application frames. Never blocks."""
        out, self.pending = self.pending, []
        out.extend(self.receive())
        self.push()
        return out

    def handshake(self, timeout=15.0):
        deadline = self.clock() + timeout
        while self.clock() < deadline:
            self.poll()
            if self.epoch:
                return
            time.sleep(0.01)
        raise Timeout('no HELLO from the device; is mixosd still holding the port?')

    def wait(self, predicate, timeout, what):
        """Pump the link until `predicate(frame)` returns a value, or time out."""
        deadline = self.clock() + timeout
        while self.clock() < deadline:
            for frame in self.poll():
                if frame.channel == C.MAINTENANCE and frame.type == T.ERROR:
                    raise UpdateError('device refused: ' +
                                      frame.payload.decode('utf-8', 'replace'))
                value = predicate(frame)
                if value is not None:
                    return value
            time.sleep(0.002)
        raise Timeout(f'timed out waiting for {what}')


def _ack_offset(frame):
    if frame.channel == C.MAINTENANCE and frame.type == T.OTA_ACK and len(frame.payload) == 4:
        return struct.unpack('<I', frame.payload)[0]
    return None


def _acks(frames):
    """Yield acknowledged offsets, turning a device refusal into an exception."""
    for frame in frames:
        if frame.channel == C.MAINTENANCE and frame.type == T.ERROR:
            raise UpdateError('device refused: ' + frame.payload.decode('utf-8', 'replace'))
        offset = _ack_offset(frame)
        if offset is not None:
            yield offset


def push(link, image, digest, progress=None, timeout=30.0, stall=5.0):
    """Stream `image` into the inactive slot. Returns the bytes the device took."""
    epoch = link.epoch
    total = len(image)
    link.send(C.MAINTENANCE, T.OTA_BEGIN, SESSION, struct.pack('<I', total) + digest)

    def ready(frame):
        if frame.channel == C.MAINTENANCE and frame.type == T.OTA_READY and len(frame.payload) == 12:
            return struct.unpack('<III', frame.payload)
        return None

    try:
        chunk, window, _ = link.wait(ready, timeout, 'OTA_READY')
    except Timeout:
        # A device that answers the handshake but ignores OTA_BEGIN entirely is
        # almost always running a build from before USB updates existed, which
        # is a different problem from a slow or unplugged device.
        raise Timeout('the device answered the handshake but never replied to OTA_BEGIN; '
                      'the build it is running most likely predates USB updates and has '
                      'to be installed once over serial') from None
    chunk = min(chunk, CHUNK) or CHUNK
    window = min(window, WINDOW) or 1
    stall = min(stall, timeout)

    acked = sent = stalls = repeats = 0
    quiet_since = link.clock()
    while acked < total:
        # Keep the device's window full rather than stopping after every batch.
        # The device acknowledges on its own clock, so waiting for one exact
        # offset per batch spent a round trip - or a whole timeout - per 4 KiB.
        while sent < total and sent - acked < window * chunk:
            link.send(C.MAINTENANCE, T.OTA_DATA, SESSION,
                      struct.pack('<I', sent) + image[sent:sent + chunk])
            sent = min(sent + chunk, total)
        moved = behind = False
        for offset in _acks(link.poll()):
            if acked < offset <= total:
                acked, moved = offset, True
            elif offset == acked and sent > acked:
                # The device is naming a position it already reported while
                # more data is in flight. Repeated, that means a lost chunk.
                behind = True
        if moved:
            stalls = repeats = 0
            quiet_since = link.clock()
            if progress:
                progress(acked, total)
            continue
        if link.epoch != epoch:
            raise UpdateError('the device restarted the USB link mid-transfer; '
                              'the running build is untouched, just run this again')
        if behind:
            repeats += 1
        if repeats < 2 and link.clock() - quiet_since < stall:
            time.sleep(0.002)
            continue
        # A chunk was lost on the way in. Ask where the device actually is and
        # resend from exactly there; it ignores and re-acknowledges the rest.
        link.send(C.MAINTENANCE, T.OTA_STATUS, SESSION)
        position = min(max(acked, link.wait(_ack_offset, timeout, 'OTA_STATUS reply')), total)
        # Only a resynchronisation that learned nothing new counts as a stall,
        # so a device that acknowledges more coarsely than the window is slow
        # rather than fatal.
        stalls = 0 if position > acked else stalls + 1
        if stalls > 4:
            raise UpdateError(f'transfer stuck at {acked}/{total} bytes')
        acked = position
        sent = acked
        repeats = 0
        quiet_since = link.clock()
        if progress:
            progress(acked, total)

    link.send(C.MAINTENANCE, T.OTA_END, SESSION)

    def done(frame):
        if frame.channel == C.MAINTENANCE and frame.type == T.OTA_DONE:
            return struct.unpack('<I', frame.payload)[0] if len(frame.payload) == 4 else 0
        return None

    return link.wait(done, timeout, 'OTA_DONE')


# --------------------------------------------------------------------------
# Linux plumbing. Everything above is portable and unit-testable.
# --------------------------------------------------------------------------

class SerialTransport:
    def __init__(self, device):
        from mixosd import open_serial
        self.fd = open_serial(device)

    def read(self, limit):
        try:
            return os.read(self.fd, limit)
        except BlockingIOError:
            return b''

    def write(self, data):
        try:
            return os.write(self.fd, data)
        except BlockingIOError:
            return 0

    def close(self):
        if self.fd >= 0:
            os.close(self.fd)
            self.fd = -1


def systemctl(action, service):
    command = ['systemctl', action, service]
    if os.geteuid() != 0:
        command = ['sudo', '-n'] + command
    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode:
        raise UpdateError(f'{" ".join(command)} failed: {result.stderr.strip()}')


def wait_for_device(device, timeout, absent_first=True):
    """Wait for the CDC node to disappear and come back after the reboot."""
    deadline = time.monotonic() + timeout
    if absent_first:
        while time.monotonic() < deadline and os.path.exists(device):
            time.sleep(0.1)
    while time.monotonic() < deadline:
        if os.path.exists(device):
            time.sleep(0.5)  # let udev finish applying permissions
            return True
        time.sleep(0.1)
    return False


def bar(done, total):
    filled = done * 40 // total if total else 40
    sys.stderr.write(f'\r  [{"#" * filled}{"." * (40 - filled)}] '
                     f'{done * 100 // total if total else 100}%  {done}/{total} B')
    sys.stderr.flush()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--image', required=True, help='ESP32-S3 application .bin')
    parser.add_argument('--device', help='explicit /dev/serial/by-id/... path')
    parser.add_argument('--service', help='systemd unit to stop during the update')
    parser.add_argument('--timeout', type=float, default=30.0, help='per-step timeout in seconds')
    parser.add_argument('--dry-run', action='store_true',
                        help='validate the image and exit without opening the device')
    args = parser.parse_args(argv)

    try:
        image, digest = inspect_image(args.image)
    except UpdateError as exc:
        parser.error(str(exc))

    print(f'image  {args.image}')
    print(f'size   {len(image)} bytes')
    print(f'sha256 {digest.hex()}')
    if args.dry_run:
        return 0
    if not args.device:
        parser.error('--device is required unless --dry-run is given')
    if not args.device.startswith('/dev/serial/by-id/'):
        parser.error('use an explicit stable /dev/serial/by-id/ identity')

    stopped = False
    transport = None
    try:
        if args.service:
            print(f'stopping {args.service}')
            systemctl('stop', args.service)
            stopped = True
            time.sleep(0.5)
        transport = SerialTransport(args.device)
        link = Link(transport)
        print('waiting for the device handshake')
        link.handshake(args.timeout)
        print(f'streaming into the inactive slot ({CHUNK} B chunks)')
        push(link, image, digest, progress=bar, timeout=args.timeout)
        sys.stderr.write('\n')
        print('image verified on device; it is rebooting into the new slot')
        transport.close()
        transport = None

        if not wait_for_device(args.device, 60.0):
            raise UpdateError('device did not come back; power-cycle and check the LCD')
        transport = SerialTransport(args.device)
        link = Link(transport)
        link.handshake(args.timeout)
        print('new build is up and talking; it self-confirms after ~20 s of health')
    except UpdateError as exc:
        print(f'\nota_esp: {exc}', file=sys.stderr)
        if 'no OTA slot' in str(exc):
            print('ota_esp: this device still runs the single-application layout. '
                  'A firmware update over USB needs ota_0/ota_1/otadata, so it has '
                  'to be flashed once with tools/flash_esp_remote.py --migrate. '
                  'Every later update then runs through this tool.', file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print('\nota_esp: interrupted; the device keeps running its current slot',
              file=sys.stderr)
        return 130
    finally:
        if transport:
            transport.close()
        if stopped:
            print(f'starting {args.service}')
            try:
                systemctl('start', args.service)
            except UpdateError as exc:
                print(f'ota_esp: {exc}', file=sys.stderr)
    return 0


if __name__ == '__main__':
    sys.exit(main())
