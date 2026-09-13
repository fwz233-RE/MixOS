"""Drive tools/ota_esp.py against a simulated device; never opens hardware.

The simulator answers exactly like firmware/esp32s3/main/mix_link.c and
mix_ota.c: it accepts writes only at the offset it is waiting for, acknowledges
on its own cadence rather than on a boundary the host picked, and refuses with
a readable reason. The interesting cases are the ones that used to hang: a lost
chunk, an acknowledgement the host never predicted, and a link that restarts
in the middle of a transfer.
"""
import hashlib
from pathlib import Path
import struct
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
sys.path.insert(0, str(ROOT / 'linux'))

from protocol import Channel as C, Type as T, Frame, Decoder, MAX_PAYLOAD  # noqa: E402
import ota_esp  # noqa: E402

CHUNK = MAX_PAYLOAD - 4
WINDOW = 8
ACK_EVERY = 4


def image(size):
    """A byte pattern with a valid ESP32-S3 application header."""
    body = bytes((i * 7 + 11) & 0xFF for i in range(size))
    header = bytes([0xE9, 1, 0, 0]) + bytes(8) + struct.pack('<H', 9) + bytes(2)
    return header + body[len(header):]


class Clock:
    """Virtual time: every reading advances, so timeouts are deterministic."""

    def __init__(self, step=0.001):
        self.now = 0.0
        self.step = step

    def __call__(self):
        self.now += self.step
        return self.now


class Device:
    """A transport that is also the device. read/write is all ota_esp needs."""

    def __init__(self, slot=0x1F0000, epoch=0x51A7, drop=(), refuse=None,
                 write_limit=None, ack_every=ACK_EVERY):
        self.slot = slot
        self.epoch = epoch
        self.refuse = refuse
        self.drop = set(drop)
        self.write_limit = write_limit
        self.ack_every = ack_every
        self.decoder = Decoder()
        self.out = bytearray()
        self.sequence = 0
        self.online = False
        self.session = 0
        self.received = 0
        self.total = 0
        self.expected = b''
        self.chunks = 0
        self.stored = bytearray()
        self.booted = None
        self.errors = []
        self.hello()

    # -- transport ---------------------------------------------------------
    def read(self, limit):
        data, self.out = bytes(self.out[:limit]), self.out[limit:]
        return data

    def write(self, data):
        take = len(data) if self.write_limit is None else min(len(data), self.write_limit)
        for frame in self.decoder.feed(data[:take]):
            self.handle(frame)
        return take

    # -- device ------------------------------------------------------------
    def emit(self, channel, kind, session=0, payload=b''):
        self.out += Frame(channel, kind, self.epoch, session, self.sequence, payload).encode()
        self.sequence += 1

    def hello(self):
        self.emit(C.CONTROL, T.HELLO, payload=struct.pack('<HH', MAX_PAYLOAD, 4096))

    def restart_link(self):
        """What mix_link.c does on a fault: new epoch, everything dropped."""
        self.epoch += 1
        self.online = False
        self.session = self.received = self.total = 0
        self.sequence = 0
        self.hello()

    def error(self, session, reason):
        self.errors.append(reason)
        self.emit(C.MAINTENANCE, T.ERROR, session, reason.encode())

    def ack(self):
        self.chunks = 0
        self.emit(C.MAINTENANCE, T.OTA_ACK, self.session, struct.pack('<I', self.received))

    def handle(self, frame):
        if frame.channel == C.CONTROL and frame.type == T.HELLO_ACK:
            self.online = True
            return
        if frame.epoch != self.epoch or not self.online:
            return
        if frame.channel == C.CONTROL and frame.type == T.PING:
            self.emit(C.CONTROL, T.PONG)
            return
        if frame.channel != C.MAINTENANCE:
            return
        if frame.type == T.OTA_BEGIN:
            if self.refuse:
                self.error(frame.session, self.refuse)
                return
            self.total = struct.unpack_from('<I', frame.payload)[0]
            self.expected = frame.payload[4:]
            if self.total > self.slot:
                self.error(frame.session, 'image exceeds slot')
                self.total = 0
                return
            self.session = frame.session
            self.received = self.chunks = 0
            self.stored = bytearray()
            self.emit(C.MAINTENANCE, T.OTA_READY, frame.session,
                      struct.pack('<III', CHUNK, WINDOW, self.total))
        elif frame.type == T.OTA_DATA and frame.session == self.session:
            offset = struct.unpack_from('<I', frame.payload)[0]
            body = frame.payload[4:]
            if offset in self.drop:
                self.drop.discard(offset)  # lose it exactly once
                return
            if offset != self.received:
                self.ack()  # out of order: tell the host where we really are
                return
            self.stored += body
            self.received += len(body)
            self.chunks += 1
            if self.chunks >= self.ack_every or self.received == self.total:
                self.ack()
        elif frame.type == T.OTA_STATUS and frame.session == self.session:
            self.ack()
        elif frame.type == T.OTA_END and frame.session == self.session:
            if self.received != self.total:
                self.error(frame.session, 'incomplete image')
            elif hashlib.sha256(self.stored).digest() != self.expected:
                self.error(frame.session, 'sha256 mismatch')
            else:
                self.booted = bytes(self.stored)
                self.emit(C.MAINTENANCE, T.OTA_DONE, frame.session,
                          struct.pack('<I', self.total))

    def tick(self):
        """The per-tick acknowledgement mix_link_tick() sends after progress."""
        if self.session and self.received and self.chunks:
            self.ack()


def run(device, data, **kwargs):
    link = ota_esp.Link(device, clock=Clock())
    link.handshake(5.0)
    digest = hashlib.sha256(data).digest()
    return ota_esp.push(link, data, digest, timeout=1.0, stall=0.05, **kwargs), link


class TransferTests(unittest.TestCase):
    def test_clean_transfer_lands_byte_for_byte(self):
        data = image(40000)
        device = Device()
        taken, _ = run(device, data)
        self.assertEqual(taken, len(data))
        self.assertEqual(device.booted, data)

    def test_partial_last_chunk(self):
        data = image(CHUNK * 3 + 1)
        device = Device()
        run(device, data)
        self.assertEqual(device.booted, data)

    def test_reports_progress_monotonically_to_the_end(self):
        data = image(20000)
        seen = []
        run(Device(), data, progress=lambda done, total: seen.append(done))
        self.assertEqual(seen, sorted(seen))
        self.assertEqual(seen[-1], len(data))

    def test_recovers_from_a_dropped_chunk(self):
        data = image(30000)
        device = Device(drop=[CHUNK * 5])
        run(device, data)
        self.assertEqual(device.booted, data)

    def test_recovers_from_several_dropped_chunks(self):
        data = image(60000)
        device = Device(drop=[CHUNK * 3, CHUNK * 11, CHUNK * 40])
        run(device, data)
        self.assertEqual(device.booted, data)

    def test_survives_acknowledgements_on_an_unpredicted_cadence(self):
        """The device is free to acknowledge whenever it likes, including less
        often than the host's whole window."""
        for ack_every in (1, 3, 7, 13):
            with self.subTest(ack_every=ack_every):
                data = image(30000)
                device = Device(ack_every=ack_every)
                run(device, data)
                self.assertEqual(device.booted, data)

    def test_short_serial_writes_are_not_a_disconnection(self):
        data = image(20000)
        device = Device(write_limit=37)
        run(device, data)
        self.assertEqual(device.booted, data)


class RefusalTests(unittest.TestCase):
    def test_missing_ota_slot_is_reported_verbatim(self):
        reason = ('no OTA slot: device still has the factory-only partition '
                  'table; run the one-time A/B migration')
        with self.assertRaises(ota_esp.UpdateError) as caught:
            run(Device(refuse=reason), image(20000))
        self.assertIn('factory-only partition table', str(caught.exception))

    def test_oversized_image_is_refused_before_any_write(self):
        device = Device(slot=4096)
        with self.assertRaises(ota_esp.UpdateError) as caught:
            run(device, image(20000))
        self.assertIn('exceeds slot', str(caught.exception))
        self.assertIsNone(device.booted)

    def test_link_restart_is_named_as_safe(self):
        data = image(30000)
        device = Device()
        link = ota_esp.Link(device, clock=Clock())
        link.handshake(5.0)
        original = device.handle
        state = {'seen': 0}

        def restart_after_a_while(frame):
            if frame.channel == C.MAINTENANCE and frame.type == T.OTA_DATA:
                state['seen'] += 1
                if state['seen'] == 6:
                    device.restart_link()
                    return
            original(frame)

        device.handle = restart_after_a_while
        with self.assertRaises(ota_esp.UpdateError) as caught:
            ota_esp.push(link, data, hashlib.sha256(data).digest(), timeout=0.5, stall=0.05)
        self.assertIn('running build is untouched', str(caught.exception))

    def test_silent_device_times_out_instead_of_hanging(self):
        data = image(20000)
        device = Device()
        link = ota_esp.Link(device, clock=Clock())
        link.handshake(5.0)
        device.handle = lambda frame: None
        with self.assertRaises(ota_esp.Timeout):
            ota_esp.push(link, data, hashlib.sha256(data).digest(), timeout=0.2, stall=0.05)


class ImageTests(unittest.TestCase):
    def test_rejects_an_elf_or_merged_image(self):
        path = ROOT / 'build/scratch/not-an-app.bin'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'\x7fELF' + bytes(2000))
        with self.assertRaises(ota_esp.UpdateError):
            ota_esp.inspect_image(path)

    def test_rejects_another_chip(self):
        path = ROOT / 'build/scratch/wrong-chip.bin'
        path.parent.mkdir(parents=True, exist_ok=True)
        data = bytearray(image(2048))
        struct.pack_into('<H', data, 12, 5)
        path.write_bytes(bytes(data))
        with self.assertRaises(ota_esp.UpdateError):
            ota_esp.inspect_image(path)

    def test_accepts_an_esp32s3_application(self):
        path = ROOT / 'build/scratch/app.bin'
        path.parent.mkdir(parents=True, exist_ok=True)
        data = image(4096)
        path.write_bytes(data)
        loaded, digest = ota_esp.inspect_image(path)
        self.assertEqual(loaded, data)
        self.assertEqual(digest, hashlib.sha256(data).digest())


if __name__ == '__main__':
    unittest.main()
