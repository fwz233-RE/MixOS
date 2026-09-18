#!/usr/bin/env python3
"""Portable image/legacy protocol helpers and a compatibility CLI.

Routine writes require --package and use mixos_esp_update's durable v2 worker.
--image --dry-run remains entirely local. --legacy-identify explicitly enables
read-only v1 inspection; no unsafe legacy/ROM bootstrap is selected implicitly.
The push() v1 API is retained only for explicit compatibility callers/tests.
"""
import argparse
from collections import deque
import hashlib
import json
import os
import secrets
from pathlib import Path
import struct
import subprocess
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'linux'))

from protocol import Channel as C, Type as T, Frame, Decoder, MAX_PAYLOAD, ReceiveEpoch  # noqa: E402

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


# The application descriptor the build system writes into every image: 24 bytes
# of image header plus one 8-byte segment header, then the struct itself. Field
# offsets are esp_app_desc_t's, which is part of the image format rather than
# of any one IDF build.
APP_DESC_OFFSET = 32
APP_DESC_MAGIC = 0xABCD5432
DESC_VERSION, DESC_PROJECT, DESC_TIME, DESC_DATE, DESC_ELF_SHA = 16, 48, 80, 96, 144


def _field(data, base, width):
    return data[base:base + width].split(b'\0', 1)[0].decode('ascii', 'replace')


def describe_image(data):
    """Read an image's own identity out of its header.

    This is what the device is asked to prove it is running. It comes from the
    image rather than from the build report, so it cannot disagree with the
    bytes that were actually sent.
    """
    magic = struct.unpack_from('<I', data, APP_DESC_OFFSET)[0]
    if magic != APP_DESC_MAGIC:
        raise UpdateError('image has no application descriptor '
                          f'(magic {magic:#010x}); it cannot be identified after reboot')
    base = APP_DESC_OFFSET
    return {'elf_sha256': data[base + DESC_ELF_SHA:base + DESC_ELF_SHA + 32],
            'version': _field(data, base + DESC_VERSION, 32),
            'project': _field(data, base + DESC_PROJECT, 32),
            'time': _field(data, base + DESC_TIME, 16),
            'date': _field(data, base + DESC_DATE, 16)}


# Mirrors MIX_OTA_IDENTITY_BYTES and its layout in firmware/esp32s3/main/mix_ota.h.
IDENTITY_BYTES = 136
OTA_IMG_STATES = {0: 'new', 1: 'pending-verify', 2: 'valid', 3: 'invalid',
                  4: 'aborted', 0xFF: 'undefined'}
# esp_reset_reason_t from ESP-IDF 5.4 esp_system.h, not the ROM reset codes.
RESET_REASONS = {
    0: 'unknown', 1: 'power-on', 2: 'external pin', 3: 'software restart',
    4: 'CPU panic or unhandled exception', 5: 'interrupt watchdog',
    6: 'task watchdog', 7: 'other watchdog', 8: 'deep sleep', 9: 'brownout',
    10: 'SDIO', 11: 'USB peripheral', 12: 'JTAG', 13: 'eFuse error',
    14: 'power glitch', 15: 'CPU lockup',
}


def decode_identity(payload):
    """Decode OTA_IDENTITY into what is running, or None if it is unusable."""
    if len(payload) < IDENTITY_BYTES or payload[0] != 1:
        return None
    slot, state, reset = payload[1], payload[2], payload[3]
    return {'slot': f'ota_{slot}' if slot != 0xFF else 'not an OTA slot',
            'state': OTA_IMG_STATES.get(state, f'unknown ({state})'),
            'reset': RESET_REASONS.get(reset, f'reason {reset}'),
            'reset_code': reset,
            'address': struct.unpack_from('<I', payload, 4)[0],
            'elf_sha256': bytes(payload[8:40]),
            'date': _field(payload, 40, 16),
            'time': _field(payload, 56, 16),
            'version': _field(payload, 72, 32),
            'project': _field(payload, 104, 32)}


def identify(link, timeout, attempt=3.0):
    """Ask the device which build it is running, resending if the link resets.

    The request has to be repeated rather than waited on once. This hardware
    restarts the USB link on its own - a transfer in this very session hit it -
    and a restart bumps the epoch and clears the outbox, so a request sent
    against the previous epoch is dropped by the device and no answer to it can
    ever arrive. Spending the whole timeout on a single attempt would report
    that as a rollback, which is a different fault with a different remedy.
    """

    def answer(frame):
        if frame.channel == C.MAINTENANCE and frame.type == T.OTA_IDENTITY:
            # False rather than None: an identity this tool cannot parse is an
            # answer, and has to end the wait instead of looking like silence.
            return decode_identity(frame.payload) or False
        return None

    deadline = link.clock() + timeout
    while True:
        if link.clock() >= deadline:
            raise Timeout('timed out waiting for OTA_IDENTITY')
        link.send(C.MAINTENANCE, T.OTA_IDENTIFY, link.session, b'')
        try:
            return link.wait(answer, min(attempt, deadline - link.clock()),
                             'OTA_IDENTITY')
        except Timeout:
            continue


def verify_running(link, expected, timeout):
    """Fail unless the build that came back up is the one that was just sent.

    A completed transfer and a reboot are not evidence of an installed build.
    The bootloader rolls a trial build back when it crashes before confirming
    itself, and the reverted build re-enumerates over USB and completes this
    same handshake, so every check up to this point passes identically whether
    the update took effect or was undone.
    """
    try:
        running = identify(link, timeout)
    except Timeout:
        raise UpdateError(
            'the device completed the handshake but never answered OTA_IDENTIFY. '
            'The running build is unknown: a link fault, unsupported request, '
            'stalled firmware or rollback are possible. Treat the update as '
            'NOT installed until the expected running identity is verified.') from None
    if not running:
        raise UpdateError('the device answered OTA_IDENTIFY with an identity this '
                          'tool cannot read; treat the update as not installed')
    if running['elf_sha256'] != expected['elf_sha256']:
        raise UpdateError(
            'BUILD MISMATCH: the device is not running the expected image.\n'
            f"  sent:    {expected['project']} {expected['version']} "
            f"built {expected['date']} {expected['time']}\n"
            f"           elf {expected['elf_sha256'].hex()}\n"
            f"  running: {running['project']} {running['version']} "
            f"built {running['date']} {running['time']}\n"
            f"           elf {running['elf_sha256'].hex()}\n"
            f"  slot {running['slot']} at {running['address']:#x}, "
            f"image state {running['state']}, last reset: {running['reset']} "
            f"(code {running['reset_code']})\n"
            'A rollback is possible, but identity alone does not establish why '
            'this build was selected. Inspect boot state before retrying.')
    print(f"running {running['project']} {running['version']} "
          f"built {running['date']} {running['time']} "
          f"from {running['slot']} ({running['state']})")
    return running


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

    def __init__(self, transport, clock=time.monotonic, write_timeout=10.0,
                 random_sessions=False, sleep=time.sleep):
        self.transport = transport
        self.clock = clock
        self.sleep = sleep
        self.write_timeout = write_timeout
        self.decoder = Decoder()
        self.rx = ReceiveEpoch()
        self.retired_epochs = set()
        self.epoch = 0
        self.sequence = 0
        self.random_sessions = random_sessions
        self.session = (secrets.randbits(32) or 1) if random_sessions else SESSION
        self.heartbeat_times = deque(maxlen=256)
        # Lifetime accepted-frame total, including reads during a blocked send.
        # Diagnostic only; never reset or consulted by protocol decisions.
        self.maintenance_frames_received = 0
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
            if written < 0 or written > len(self.outbox):
                raise UpdateError('invalid serial write count')
            if written == 0:
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
        """Validate HELLO, epoch, sequence and maintenance session before use."""
        out = []
        for frame in self.decoder.feed(self.transport.read(4096)):
            hello = frame.channel == C.CONTROL and frame.type == T.HELLO
            if hello and (not frame.epoch or frame.session or frame.payload != struct.pack('<HH', MAX_PAYLOAD, 4096)):
                continue
            if hello and frame.epoch in self.retired_epochs:
                continue
            if frame.channel == C.MAINTENANCE and frame.session != self.session:
                continue
            if frame.channel == C.CONTROL and frame.session != 0:
                continue
            accepted = self.rx.accept(frame)
            if accepted is None:
                continue
            if hello:
                if accepted == 'new':
                    if self.epoch:
                        self.retired_epochs.add(self.epoch)
                    self.epoch = frame.epoch
                    self.sequence = 0
                    self.outbox.clear()
                    self.pending.clear()
                    out.clear()
                    self.heartbeat_times.clear()
                    if self.random_sessions:
                        self.session = secrets.randbits(32) or 1
                self.queue(C.CONTROL, T.HELLO_ACK,
                           payload=struct.pack('<HH', MAX_PAYLOAD, 4096))
                continue
            if frame.channel == C.CONTROL and frame.type == T.PING:
                if not frame.payload:
                    self.heartbeat_times.append(self.clock())
                    self.queue(C.CONTROL, T.PONG)
                continue
            if frame.channel == C.CONTROL and frame.type == T.PONG:
                continue
            if frame.channel == C.STATUS and frame.type == T.STATUS_REQUEST:
                continue
            if frame.channel == C.MAINTENANCE:
                self.maintenance_frames_received += 1
            out.append(frame)
        return out

    def poll(self):
        """Return application frames. Never blocks."""
        out, self.pending = self.pending, []
        out.extend(self.receive())
        self.push()
        return [frame for frame in out if frame.epoch == self.epoch and
                (frame.channel != C.MAINTENANCE or frame.session == self.session)]

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
        # Silence cannot distinguish unsupported OTA from an unresponsive link
        # or firmware; only an explicit reply establishes the device's layout.
        raise Timeout('the device answered the handshake but never replied to OTA_BEGIN; '
                      'check link health and the live partition layout before retrying '
                      'or selecting a recovery procedure') from None
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
            if offset > sent:
                raise UpdateError(f'ACK {offset} exceeds sent bytes {sent}')
            if acked < offset <= sent:
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
        position = link.wait(_ack_offset, timeout, 'OTA_STATUS reply')
        if not acked <= position <= sent:
            raise UpdateError(f'ACK {position} outside acknowledged/sent range {acked}..{sent}')
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

from serial_transport import SerialTransport  # noqa: E402


def systemctl(action, service):
    command = ['systemctl', action, service]
    if os.geteuid() != 0:
        command = ['sudo', '-n'] + command
    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode:
        raise UpdateError(f'{" ".join(command)} failed: {result.stderr.strip()}')


def wait_for_device(device, timeout, absent_first=False):
    """Compatibility helper; fast reenumeration need not show an absent node."""
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
    """Compatibility entry; all writes now use the native durable v2 job.

    The v1 push API remains for explicit offline compatibility tests. Raw v1
    flashing is intentionally no longer exposed as a routine command: the old
    receiver cannot provide a durable outcome or actual-running-file proof.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--image', help='raw app image (local --dry-run only)')
    parser.add_argument('--package', help='release directory with app.bin and manifest.json')
    parser.add_argument('--device')
    parser.add_argument('--service', choices=['mixosd.service'], default='mixosd.service')
    parser.add_argument('--timeout', type=float, default=30.0)
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--legacy-identify', action='store_true', help='explicit read-only v1 compatibility')
    parser.add_argument('--allow-replace-baseline', action='store_true')
    parser.add_argument('--wait', action='store_true')
    args = parser.parse_args(argv)
    if args.image:
        if not args.dry_run or args.package or args.legacy_identify:
            parser.error('raw --image supports local --dry-run only; apply requires a v2 release --package')
        try:
            image, digest = inspect_image(args.image)
            expected = describe_image(image)
        except (OSError, UpdateError) as exc:
            parser.error(str(exc))
        print(json.dumps(dict(dry_run=True, image=args.image, size=len(image),
                              sha256=digest.hex(), elf_sha256=expected['elf_sha256'].hex(),
                              firmware={'state': 'not-started'}, service={'state': 'untouched'},
                              error=None), sort_keys=True))
        return 0
    import mixos_esp_update
    if args.legacy_identify:
        if args.dry_run or args.package:
            parser.error('--legacy-identify is explicit live read-only inspection; use native inspect for package dry-run')
        command = ['inspect', '--live', '--legacy-identify']
    elif args.package:
        command = ['apply', '--package', args.package]
        if args.dry_run:
            command.append('--dry-run')
        if args.wait:
            command.append('--wait')
        if args.allow_replace_baseline:
            command.append('--allow-replace-baseline')
    else:
        parser.error('supply --package, --image --dry-run, or explicit --legacy-identify')
    if args.device:
        command += ['--device', args.device]
    return mixos_esp_update.main(command + ['--timeout', str(args.timeout)])


if __name__ == '__main__':
    sys.exit(main())
