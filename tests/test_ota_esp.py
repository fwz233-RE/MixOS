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
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
sys.path.insert(0, str(ROOT / 'linux'))

from protocol import Channel as C, Type as T, Frame, Decoder, MAX_PAYLOAD  # noqa: E402
import ota_esp  # noqa: E402

CHUNK = MAX_PAYLOAD - 4
WINDOW = 8
ACK_EVERY = 4

DESC_AT = ota_esp.APP_DESC_OFFSET
ELF_SHA = bytes(range(32))


def descriptor(elf_sha=ELF_SHA, version='1.2.3', project='mixos',
               when='20:42:03', date='Sep 14 2026'):
    """The application descriptor the ESP-IDF build system embeds in an image.

    ota_esp reads this out of the file and the device reports its own copy back
    after the reboot, so the two have to be laid out identically here or the
    test would agree with itself while disagreeing with the firmware.
    """
    out = bytearray(256)
    struct.pack_into('<I', out, 0, ota_esp.APP_DESC_MAGIC)
    for offset, width, text in ((ota_esp.DESC_VERSION, 32, version),
                                (ota_esp.DESC_PROJECT, 32, project),
                                (ota_esp.DESC_TIME, 16, when),
                                (ota_esp.DESC_DATE, 16, date)):
        out[offset:offset + width] = text.encode().ljust(width, b'\0')
    out[ota_esp.DESC_ELF_SHA:ota_esp.DESC_ELF_SHA + 32] = elf_sha
    return bytes(out)


def image(size, **desc):
    """A byte pattern with a valid ESP32-S3 application header and descriptor."""
    body = bytes((i * 7 + 11) & 0xFF for i in range(size))
    header = bytes([0xE9, 1, 0, 0]) + bytes(8) + struct.pack('<H', 9) + bytes(2)
    data = bytearray(header + body[len(header):])
    fields = descriptor(**desc)
    data[DESC_AT:DESC_AT + len(fields)] = fields[:max(0, len(data) - DESC_AT)]
    return bytes(data)


def identity(elf_sha=ELF_SHA, slot=1, state=1, reset=3, address=0x610000,
             date='Sep 14 2026', when='20:42:03', version='1.2.3',
             project='mixos'):
    """An OTA_IDENTITY payload, laid out as mix_ota_identity() writes it."""
    out = bytearray(ota_esp.IDENTITY_BYTES)
    out[0], out[1], out[2], out[3] = 1, slot, state, reset
    struct.pack_into('<I', out, 4, address)
    out[8:40] = elf_sha
    for offset, width, text in ((40, 16, date), (56, 16, when),
                                (72, 32, version), (104, 32, project)):
        out[offset:offset + width] = text.encode().ljust(width, b'\0')
    return bytes(out)


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
                 write_limit=None, ack_every=ACK_EVERY, running=...,
                 ignore_identify=0):
        self.slot = slot
        self.epoch = epoch
        self.refuse = refuse
        self.drop = set(drop)
        self.write_limit = write_limit
        self.ack_every = ack_every
        # What this device answers OTA_IDENTIFY with. None models a build that
        # predates the question entirely, which stays silent.
        self.identity = identity() if running is ... else running
        # Requests swallowed before the first answer, standing in for the link
        # restart that drops whatever was queued against the previous epoch.
        self.ignore_identify = ignore_identify
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
        elif frame.type == T.OTA_IDENTIFY:
            # Answerable with no transfer open and in any session: this is what
            # the host asks once the device has come back from the reboot.
            if self.ignore_identify:
                self.ignore_identify -= 1
            elif self.identity is not None:
                self.emit(C.MAINTENANCE, T.OTA_IDENTITY, frame.session, self.identity)
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

    def test_lost_done_does_not_prove_no_commit(self):
        data = image(4096)
        device = Device()
        handle = device.handle

        def drop_done(frame):
            handle(frame)
            if frame.type == T.OTA_END:
                device.out.clear()  # commit happened, but its reply was lost

        device.handle = drop_done
        with self.assertRaisesRegex(ota_esp.Timeout, 'OTA_DONE'):
            run(device, data)
        self.assertEqual(device.booted, data)

    def test_silent_device_times_out_instead_of_hanging(self):
        data = image(20000)
        device = Device()
        link = ota_esp.Link(device, clock=Clock())
        link.handshake(5.0)
        device.handle = lambda frame: None
        with self.assertRaises(ota_esp.Timeout):
            ota_esp.push(link, data, hashlib.sha256(data).digest(), timeout=0.2, stall=0.05)


class IdentityTests(unittest.TestCase):
    """The check that separates "the transfer finished" from "the build runs".

    A build the bootloader rolled back comes back on USB and completes the
    handshake exactly like the build that was just installed, so without asking
    the device outright the updater reports success for both.
    """

    def link(self, device):
        link = ota_esp.Link(device, clock=Clock())
        link.handshake(5.0)
        return link

    def test_reads_the_running_build_back(self):
        device = Device()
        running = ota_esp.identify(self.link(device), 5.0)
        self.assertEqual(running['elf_sha256'], ELF_SHA)
        self.assertEqual(running['slot'], 'ota_1')
        self.assertEqual(running['date'], 'Sep 14 2026')
        self.assertEqual(running['time'], '20:42:03')
        self.assertEqual(running['state'], 'pending-verify')
        self.assertEqual(running['address'], 0x610000)

    def test_reset_reasons_match_esp_idf_5_4_enum(self):
        # Independent wire values from esp_reset_reason_t, not ROM reset codes.
        expected = (
            'unknown', 'power-on', 'external pin', 'software restart',
            'CPU panic or unhandled exception', 'interrupt watchdog',
            'task watchdog', 'other watchdog', 'deep sleep', 'brownout',
            'SDIO', 'USB peripheral', 'JTAG', 'eFuse error', 'power glitch',
            'CPU lockup',
        )
        for code, reason in enumerate(expected):
            with self.subTest(code=code):
                running = ota_esp.decode_identity(identity(reset=code))
                self.assertEqual(running['reset'], reason)
                self.assertEqual(running['reset_code'], code)

    def test_unknown_reset_reason_preserves_raw_value(self):
        running = ota_esp.decode_identity(identity(reset=255))
        self.assertEqual(running['reset'], 'reason 255')
        self.assertEqual(running['reset_code'], 255)

    def test_matching_build_passes(self):
        expected = ota_esp.describe_image(image(4096))
        running = ota_esp.verify_running(self.link(Device()), expected, 5.0)
        self.assertEqual(running['elf_sha256'], expected['elf_sha256'])

    def test_a_different_running_build_does_not_prove_rollback(self):
        expected = ota_esp.describe_image(image(4096, elf_sha=bytes(range(32, 64))))
        device = Device(running=identity(elf_sha=ELF_SHA, slot=0, state=2,
                                         reset=4, address=0x10000,
                                         date='Sep 13 2026', when='11:02:44'))
        with self.assertRaises(ota_esp.UpdateError) as caught:
            ota_esp.verify_running(self.link(device), expected, 5.0)
        message = str(caught.exception)
        self.assertIn('BUILD MISMATCH', message)
        self.assertIn('Sep 13 2026', message)
        self.assertIn('ota_0', message)
        self.assertIn('CPU panic', message)
        self.assertIn('(code 4)', message)
        self.assertNotIn('ROLLED BACK:', message)
        self.assertNotIn('did not survive its trial boot', message)
        self.assertIn('identity alone does not establish', message)

    def test_a_dropped_request_is_resent_rather_than_waited_out(self):
        """A lost request must not be reported as a rollback.

        This hardware restarts the USB link on its own, and a restart bumps the
        epoch and clears the outbox, so whatever was queued against the old
        epoch is dropped and can never be answered.
        """
        expected = ota_esp.describe_image(image(4096))
        device = Device(ignore_identify=3)
        running = ota_esp.verify_running(self.link(device), expected, 30.0)
        self.assertEqual(running['elf_sha256'], expected['elf_sha256'])
        self.assertEqual(device.ignore_identify, 0)

    def test_a_silent_device_is_not_treated_as_success(self):
        expected = ota_esp.describe_image(image(4096))
        with self.assertRaises(ota_esp.UpdateError) as caught:
            ota_esp.verify_running(self.link(Device(running=None)), expected, 0.2)
        self.assertIn('NOT installed', str(caught.exception))

    def test_an_unreadable_identity_is_not_treated_as_success(self):
        expected = ota_esp.describe_image(image(4096))
        device = Device(running=bytes([2]) + bytes(ota_esp.IDENTITY_BYTES - 1))
        with self.assertRaises(ota_esp.UpdateError) as caught:
            ota_esp.verify_running(self.link(device), expected, 0.2)
        self.assertIn('cannot read', str(caught.exception))

    def test_an_image_without_a_descriptor_is_refused_up_front(self):
        """Refuse before flashing rather than after: an image that cannot be
        identified would make every later update unverifiable."""
        data = bytearray(image(4096))
        struct.pack_into('<I', data, DESC_AT, 0)
        with self.assertRaises(ota_esp.UpdateError) as caught:
            ota_esp.describe_image(bytes(data))
        self.assertIn('cannot be identified', str(caught.exception))


class ImageTests(unittest.TestCase):
    """Image validation, with fixtures in a temporary directory.

    These used to be written into the tracked ``build/scratch`` folder, which
    left files behind and let one run observe another run's leftovers.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix='mixos-ota-image-')
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)

    def test_rejects_an_elf_or_merged_image(self):
        path = self.tmp / 'not-an-app.bin'
        path.write_bytes(b'\x7fELF' + bytes(2000))
        with self.assertRaises(ota_esp.UpdateError):
            ota_esp.inspect_image(path)

    def test_rejects_another_chip(self):
        path = self.tmp / 'wrong-chip.bin'
        data = bytearray(image(2048))
        struct.pack_into('<H', data, 12, 5)
        path.write_bytes(bytes(data))
        with self.assertRaises(ota_esp.UpdateError):
            ota_esp.inspect_image(path)

    def test_accepts_an_esp32s3_application(self):
        path = self.tmp / 'app.bin'
        data = image(4096)
        path.write_bytes(data)
        loaded, digest = ota_esp.inspect_image(path)
        self.assertEqual(loaded, data)
        self.assertEqual(digest, hashlib.sha256(data).digest())


class LinkSafetyTests(unittest.TestCase):
    def link(self):
        device = Device()
        link = ota_esp.Link(device, clock=Clock())
        link.handshake(1)
        return link, device

    def test_invalid_hello_never_changes_epoch_or_session(self):
        link, device = self.link()
        epoch = link.epoch
        device.out += Frame(C.CONTROL, T.HELLO, epoch + 1, 0, 0, b'bad!').encode()
        device.out += Frame(C.CONTROL, T.HELLO, epoch + 2, 1, 0,
                            struct.pack('<HH', 512, 4096)).encode()
        self.assertEqual(link.poll(), [])
        self.assertEqual(link.epoch, epoch)

    def test_wrong_session_and_stale_sequence_do_not_acknowledge(self):
        link, device = self.link()
        wrong = Frame(C.MAINTENANCE, T.OTA_ACK, link.epoch, ota_esp.SESSION + 1,
                      100, struct.pack('<I', 4000))
        valid = Frame(C.MAINTENANCE, T.OTA_ACK, link.epoch, ota_esp.SESSION,
                      2, struct.pack('<I', 10))
        device.out += wrong.encode() + valid.encode() + valid.encode()
        frames = link.poll()
        self.assertEqual(len(frames), 1)
        self.assertEqual(ota_esp._ack_offset(frames[0]), 10)

    def test_retired_epoch_hello_cannot_restore_an_old_link(self):
        link, device = self.link()
        old = link.epoch
        device.restart_link()
        link.poll()
        device.out += Frame(C.CONTROL, T.HELLO, old, 0, 999,
                            struct.pack('<HH', 512, 4096)).encode()
        link.poll()
        self.assertEqual(link.epoch, old + 1)

    def test_v2_session_rotates_after_valid_new_epoch(self):
        device = Device()
        link = ota_esp.Link(device, clock=Clock(), random_sessions=True)
        link.handshake(1)
        first = link.session
        device.restart_link()
        link.poll()
        self.assertNotEqual(link.session, 0)
        self.assertNotEqual(link.session, first)

    def test_legacy_ack_above_sent_is_refused_not_clamped(self):
        device = Device()
        original = device.ack
        def forged():
            device.emit(C.MAINTENANCE, T.OTA_ACK, device.session,
                        struct.pack('<I', 19000))
        device.ack = forged
        with self.assertRaisesRegex(ota_esp.UpdateError, 'exceeds sent'):
            run(device, image(20000))
        self.assertIsNone(device.booted)

    def test_fast_reenumeration_helper_does_not_require_absence(self):
        from unittest import mock
        with mock.patch.object(ota_esp.os.path, 'exists', return_value=True) as exists, \
                mock.patch.object(ota_esp.time, 'sleep'):
            self.assertTrue(ota_esp.wait_for_device('/fake/by-id', 2))
            self.assertEqual(exists.call_count, 1)

    def test_raw_legacy_cli_is_not_a_default_write_path(self):
        from unittest import mock
        with mock.patch.object(ota_esp, 'SerialTransport') as transport, \
                mock.patch('sys.stderr'):
            with self.assertRaises(SystemExit):
                ota_esp.main(['--image', 'anything.bin', '--device', '/dev/ttyACM0'])
            transport.assert_not_called()


if __name__ == '__main__':
    unittest.main()
