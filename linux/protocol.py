"""MixOS USB v1; standard library only. CRC is integrity, not authentication."""
from dataclasses import dataclass
from enum import IntEnum
import struct
import zlib

MAX_PAYLOAD = 512
MAX_DECODED = 534
MAX_ENCODED = 539  # delimiter excluded; fixed transport allocation is 540
WINDOW = 4096
MASK = 0xFFFFFFFF
HEADER = struct.Struct('<BBBBIIIH')


class Channel(IntEnum):
    CONTROL = 0
    TERMINAL = 1
    STATUS = 2
    JOB = 3
    MAINTENANCE = 4
    LOG = 5
    NET = 6


MAX_CHANNEL = int(Channel.NET)


class Type(IntEnum):
    HELLO = 1
    HELLO_ACK = 2
    PING = 3
    PONG = 4
    ERROR = 5
    CREDIT = 6
    OPEN = 16
    OPENED = 17
    DATA = 18
    INPUT = 19
    RESIZE = 20
    CLOSE = 21
    EXIT = 22
    STATUS_REQUEST = 32
    STATUS = 33
    JOB_START = 48
    JOB_PROGRESS = 49
    JOB_CANCEL = 50
    JOB_RESULT = 51
    PREPARE_UPDATE = 64
    UPDATE_READY = 65
    ENTER_BOOT = 66
    OTA_BEGIN = 67
    OTA_READY = 68
    OTA_DATA = 69
    OTA_ACK = 70
    OTA_END = 71
    OTA_DONE = 72
    OTA_ABORT = 73
    OTA_STATUS = 74
    OTA_IDENTIFY = 75
    OTA_IDENTITY = 76
    CAPS_QUERY = 77
    CAPS = 78
    OTA_REQUEST = 79
    LOG = 80
    OTA_RESPONSE = 81
    SCREEN_REQUEST = 88
    SCREEN_INFO = 89
    SCREEN_DATA = 90
    SCREEN_END = 91
    NET_SCAN = 96
    NET_LIST = 97
    NET_CONNECT = 98
    NET_FORGET = 99
    NET_RESULT = 100


def newer(value, previous):
    return 0 < ((value - previous) & MASK) < 0x80000000


def cobs_encode(data):
    out = bytearray([0])
    code_at, code = 0, 1
    for value in data:
        if value == 0:
            out[code_at] = code
            code_at = len(out)
            out.append(0)
            code = 1
        else:
            out.append(value)
            code += 1
            if code == 255:
                out[code_at] = code
                code_at = len(out)
                out.append(0)
                code = 1
    out[code_at] = code
    return bytes(out)


def cobs_decode(data):
    if not data or 0 in data:
        raise ValueError('invalid COBS bytes')
    out = bytearray()
    pos = 0
    while pos < len(data):
        code = data[pos]
        pos += 1
        end = pos + code - 1
        if end > len(data):
            raise ValueError('truncated COBS run')
        out.extend(data[pos:end])
        pos = end
        if code != 255 and pos < len(data):
            out.append(0)
    return bytes(out)


@dataclass(frozen=True)
class Frame:
    channel: int
    type: int
    epoch: int
    session: int = 0
    sequence: int = 0
    payload: bytes = b''

    def encode(self):
        if not 0 <= self.channel <= MAX_CHANNEL or len(self.payload) > MAX_PAYLOAD:
            raise ValueError('invalid channel or payload length')
        header = HEADER.pack(1, self.channel, self.type, 0, self.epoch,
                             self.session, self.sequence, len(self.payload))
        raw = header + self.payload
        return cobs_encode(raw + struct.pack('<I', zlib.crc32(raw))) + b'\0'


def decode(encoded):
    if len(encoded) > MAX_ENCODED:
        raise ValueError('oversized frame')
    raw = cobs_decode(encoded)
    if not 22 <= len(raw) <= MAX_DECODED:
        raise ValueError('invalid frame length')
    version, channel, kind, flags, epoch, session, sequence, length = HEADER.unpack_from(raw)
    if version != 1 or flags or channel > MAX_CHANNEL or length > MAX_PAYLOAD or len(raw) != 22 + length:
        raise ValueError('invalid header')
    if zlib.crc32(raw[:-4]) != struct.unpack_from('<I', raw, len(raw) - 4)[0]:
        raise ValueError('CRC mismatch')
    return Frame(channel, kind, epoch, session, sequence, raw[18:-4])


class Decoder:
    """Bounded streaming decoder; discard oversize through next delimiter."""
    def __init__(self):
        self.pending = bytearray()
        self.discarding = False
        self.errors = 0

    def feed(self, data):
        for value in data:
            if value == 0:
                if not self.discarding and self.pending:
                    try:
                        frame = decode(self.pending)
                    except ValueError:
                        self.errors += 1
                    else:
                        yield frame
                self.pending.clear()
                self.discarding = False
            elif not self.discarding:
                if len(self.pending) >= MAX_ENCODED:
                    self.pending.clear()
                    self.discarding = True
                    self.errors += 1
                else:
                    self.pending.append(value)


class ReceiveEpoch:
    """HELLO retries are idempotent; other frames require a fresh sequence."""
    def __init__(self):
        self.epoch = 0
        self.sequence = None

    def accept(self, frame):
        if (frame.channel == Channel.CONTROL and frame.type == Type.HELLO
                and frame.session == 0 and frame.epoch
                and frame.payload == struct.pack('<HH', MAX_PAYLOAD, WINDOW)):
            changed = frame.epoch != self.epoch
            if changed:
                self.epoch, self.sequence = frame.epoch, frame.sequence
            elif self.sequence is None or newer(frame.sequence, self.sequence):
                self.sequence = frame.sequence
            return 'new' if changed else 'hello'
        if not self.epoch or frame.epoch != self.epoch:
            return None
        if self.sequence is not None and not newer(frame.sequence, self.sequence):
            return None
        self.sequence = frame.sequence
        return 'frame'


class Credit:
    def __init__(self):
        self.sent = 0
        self.grant = 0

    @property
    def available(self):
        return (self.grant - self.sent) & MASK

    def update(self, absolute):
        if absolute == self.grant:
            return True
        if not newer(absolute, self.grant) or ((absolute - self.sent) & MASK) > WINDOW:
            return False
        self.grant = absolute
        return True

    def consume(self, length):
        if length < 0 or length > self.available:
            raise ValueError('credit exceeded')
        self.sent = (self.sent + length) & MASK
