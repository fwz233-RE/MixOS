"""Portable USB contract tests. Run: python -B -m unittest discover -s tests."""
from pathlib import Path
import random
import struct
import sys
import unittest
import zlib

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'linux'))
from protocol import (Frame, Decoder, ReceiveEpoch, Credit, cobs_encode,
                      cobs_decode, decode, newer, MAX_ENCODED, MAX_CHANNEL)

# Shared deterministic vectors for the portable C codec. Wire includes delimiter.
GOLDEN_HELLO = '02010201057856341201010102010101020401020206103b55d14a00'
GOLDEN_DATA = '04010112067856341207010102020101020802410b421b5b33316d0c1929ef00'


class ProtocolTests(unittest.TestCase):
    def hello(self, epoch=0x12345678, seq=1):
        return Frame(0, 1, epoch, 0, seq, struct.pack('<HH', 512, 4096))

    def test_golden(self):
        for frame, golden in [(self.hello(), GOLDEN_HELLO),
                              (Frame(1, 18, 0x12345678, 7, 2, b'A\0B\x1b[31m'), GOLDEN_DATA)]:
            self.assertEqual(frame.encode().hex(), golden)
            self.assertEqual(decode(bytes.fromhex(golden)[:-1]), frame)

    def test_standard_crc(self):
        self.assertEqual(zlib.crc32(b'123456789'), 0xCBF43926)

    def test_cobs_roundtrip(self):
        rng = random.Random(1729)
        for size in [0, 1, 253, 254, 255, 256, 512, 534]:
            for data in [b'\0' * size, b'X' * size, bytes(rng.randrange(256) for _ in range(size))]:
                encoded = cobs_encode(data)
                self.assertNotIn(0, encoded)
                self.assertEqual(cobs_decode(encoded), data)

    def test_fragment_concat(self):
        frames = [self.hello(), Frame(1, 19, 0x12345678, 7, 2, bytes(range(256)) * 2)]
        wire = b''.join(f.encode() for f in frames)
        for split in range(len(wire) + 1):
            parser = Decoder()
            self.assertEqual(list(parser.feed(wire[:split])) + list(parser.feed(wire[split:])), frames)
        parser = Decoder()
        result = []
        for byte in wire:
            result.extend(parser.feed(bytes([byte])))
        self.assertEqual(result, frames)

    def test_malformed_cobs(self):
        for data in [b'', b'\0', b'\x03x', b'\xffa', b'\x01\0']:
            with self.assertRaises(ValueError):
                cobs_decode(data)

    def test_crc_and_header_rejection(self):
        original = bytearray(cobs_decode(self.hello().encode()[:-1]))
        bad_crc = original.copy()
        bad_crc[-1] ^= 1
        with self.assertRaises(ValueError):
            decode(cobs_encode(bad_crc))
        # Offset 1 is the channel: NET=6 is the highest defined one, so 7 is
        # the first value that must still be rejected.
        for offset, value in [(0, 2), (1, 7), (3, 1), (16, 255), (17, 3)]:
            raw = original.copy()
            raw[offset] = value
            raw[-4:] = struct.pack('<I', zlib.crc32(raw[:-4]))
            with self.assertRaises(ValueError):
                decode(cobs_encode(raw))
        # Every defined channel decodes; the bound moved, it did not vanish.
        for channel in range(0, MAX_CHANNEL + 1):
            raw = original.copy()
            raw[1] = channel
            raw[-4:] = struct.pack('<I', zlib.crc32(raw[:-4]))
            self.assertEqual(decode(cobs_encode(raw)).channel, channel)
        with self.assertRaises(ValueError):
            Frame(MAX_CHANNEL + 1, 18, 1).encode()
        with self.assertRaises(ValueError):
            Frame(1, 18, 1, payload=b'x' * 513).encode()

    def test_bounded_oversize_recovery(self):
        parser = Decoder()
        self.assertEqual(list(parser.feed(b'x' * 100000)), [])
        self.assertLessEqual(len(parser.pending), MAX_ENCODED)
        self.assertTrue(parser.discarding)
        self.assertEqual(list(parser.feed(b'\0' + self.hello().encode())), [self.hello()])
        self.assertEqual(parser.errors, 1)

    def test_bad_frame_followed_by_good(self):
        parser = Decoder()
        bad = b'\x05abc\0'
        self.assertEqual(list(parser.feed(bad + self.hello().encode())), [self.hello()])
        self.assertEqual(parser.errors, 1)

    def test_epoch_duplicates_and_wrap(self):
        rx = ReceiveEpoch()
        self.assertIsNone(rx.accept(Frame(0, 3, 1, sequence=5)))
        self.assertEqual(rx.accept(self.hello(seq=0xFFFFFFFE)), 'new')
        self.assertEqual(rx.accept(self.hello(seq=0xFFFFFFFE)), 'hello')
        self.assertEqual(rx.accept(Frame(0, 3, rx.epoch, sequence=0xFFFFFFFF)), 'frame')
        self.assertEqual(rx.accept(Frame(0, 3, rx.epoch, sequence=0)), 'frame')
        self.assertIsNone(rx.accept(Frame(0, 3, rx.epoch, sequence=0)))
        self.assertIsNone(rx.accept(Frame(0, 3, rx.epoch, sequence=0xFFFFFFFF)))
        self.assertIsNone(rx.accept(Frame(0, 3, 17, sequence=10)))
        self.assertEqual(rx.accept(self.hello(epoch=17)), 'new')
        self.assertFalse(newer(0x80000000, 0))

    def test_credit_absolute_duplicate_stale_wrap(self):
        credit = Credit()
        self.assertEqual(credit.available, 0)
        self.assertFalse(credit.update(4097))
        self.assertTrue(credit.update(4096))
        credit.consume(512)
        self.assertTrue(credit.update(4096))
        self.assertEqual(credit.available, 3584)
        self.assertFalse(credit.update(4095))
        self.assertTrue(credit.update(4608))
        self.assertEqual(credit.available, 4096)
        with self.assertRaises(ValueError):
            credit.consume(4097)
        credit.sent = credit.grant = 0xFFFFFFF0
        self.assertTrue(credit.update(0xFF0))
        credit.consume(4096)
        self.assertEqual(credit.sent, 0xFF0)
        self.assertEqual(credit.available, 0)


if __name__ == '__main__':
    unittest.main()
