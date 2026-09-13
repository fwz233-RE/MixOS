#!/usr/bin/env python3
"""Conservative app-only preflight and opt-in maintenance; never writes flash.

This repository has no hardware-attested ROM identity/layout/readback profile.
--execute without --maintenance-only is therefore refused, not guessed.
"""
import argparse
import csv
import hashlib
import json
import os
from pathlib import Path
import secrets
import selectors
import struct
import subprocess
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'linux'))
from protocol import Channel as C, Type as T, Frame, Decoder, ReceiveEpoch
from mixosd import WriteQueue, open_serial

# The historic single-application layout the device shipped with. An app-only
# update writes 0x10000 and nothing else, so it can never gain OTA slots.
LEGACY_LAYOUT = [('nvs', 'data', 'nvs', 0x9000, 0x6000),
                 ('phy_init', 'data', 'phy', 0xF000, 0x1000),
                 ('factory', 'app', 'factory', 0x10000, 0x200000),
                 ('font', 'data', 'spiffs', 0x210000, 0x400000)]

# The A/B layout. nvs, phy_init and font keep their exact legacy offsets and
# sizes, so migrating to it never rewrites the 4 MiB font or the stored
# preferences; otadata lives in the tail of the old factory partition.
AB_LAYOUT = [('nvs', 'data', 'nvs', 0x9000, 0x6000),
             ('phy_init', 'data', 'phy', 0xF000, 0x1000),
             ('ota_0', 'app', 'ota_0', 0x10000, 0x1F0000),
             ('otadata', 'data', 'ota', 0x200000, 0x2000),
             ('font', 'data', 'spiffs', 0x210000, 0x400000),
             ('ota_1', 'app', 'ota_1', 0x610000, 0x1F0000)]

LAYOUTS = {
    'legacy': {'rows': LEGACY_LAYOUT, 'offset': 0x10000, 'size': 0x200000,
               'rollback': False, 'otadata': None},
    'ab': {'rows': AB_LAYOUT, 'offset': 0x10000, 'size': 0x1F0000,
           'rollback': True, 'otadata': (0x200000, 0x2000)},
}

# Regions the A/B migration is allowed to change, relative to a legacy device.
# Everything outside them must come back byte-identical from the readback.
MIGRATION_WRITES = {'bootloader': (0x0, 0x8000), 'table': (0x8000, 0xC00),
                    'app': (0x10000, 0x1F0000), 'otadata': (0x200000, 0x2000)}


def number(value):
    value = value.strip()
    suffix = value[-1:].upper()
    return int(value[:-1], 0) * {'K': 1024, 'M': 1048576}[suffix] if suffix in ('K', 'M') else int(value, 0)


def read_partitions(path):
    rows = []
    for row in csv.reader(line for line in Path(path).read_text(encoding='utf-8').splitlines()
                          if line.strip() and not line.lstrip().startswith('#')):
        row = [v.strip() for v in row]
        if len(row) < 5 or any(row[5:]):
            raise ValueError('unsupported partition flags/layout')
        rows.append((row[0], row[1], row[2], number(row[3]), number(row[4])))
    return rows


def validate_partitions(path, expect=None):
    """Identify the CSV as one of the two audited 8 MiB layouts.

    `expect` pins the result to 'legacy' or 'ab' when the caller already knows
    which one the operation requires, so a mismatched table fails here rather
    than half-way through a flash session.
    """
    rows = read_partitions(path)
    for name, layout in LAYOUTS.items():
        if rows == layout['rows']:
            if expect and expect != name:
                raise ValueError(f'expected the {expect} layout, got {name}')
            return dict(layout, name=name)
    raise ValueError('only the audited factory-only and A/B 8 MiB layouts are supported')


PARTITION_MAGIC = b'\xaa\x50'
PARTITION_TYPES = {0: 'app', 1: 'data'}
PARTITION_SUBTYPES = {('app', 0x00): 'factory', ('app', 0x10): 'ota_0', ('app', 0x11): 'ota_1',
                      ('data', 0x00): 'ota', ('data', 0x01): 'phy', ('data', 0x02): 'nvs',
                      ('data', 0x82): 'spiffs'}


def parse_partition_binary(data):
    """Decode a 0xC00-byte partition-table image into validate_partitions rows.

    Used to recognise the layout a live device is actually running, which is
    what decides whether an update can be app-only or has to migrate.
    """
    rows = []
    for offset in range(0, len(data), 32):
        entry = data[offset:offset + 32]
        if len(entry) < 32 or entry[:2] != PARTITION_MAGIC:
            break
        kind = PARTITION_TYPES.get(entry[2])
        subtype = PARTITION_SUBTYPES.get((kind, entry[3]))
        if not kind or not subtype:
            raise ValueError(f'unsupported partition type/subtype {entry[2]:#x}/{entry[3]:#x}')
        start, size = struct.unpack_from('<II', entry, 4)
        if struct.unpack_from('<I', entry, 28)[0]:
            raise ValueError('unsupported partition flags')
        rows.append((entry[12:28].split(b'\0')[0].decode('ascii'), kind, subtype, start, size))
    if not rows:
        raise ValueError('no partition entries found')
    return rows


def identify_partition_binary(data, expect=None):
    """Name the layout a live partition-table image implements."""
    rows = parse_partition_binary(data)
    for name, layout in LAYOUTS.items():
        if rows == layout['rows']:
            if expect and expect != name:
                raise ValueError(f'device is running the {name} layout, expected {expect}')
            return dict(layout, name=name)
    raise ValueError('live partition table is neither the audited factory-only nor the A/B layout: '
                     + repr(rows))


def validate_image(path, expected_sha256, chip, partition):
    if chip != 'esp32s3':
        raise ValueError('only ESP32-S3 is supported')
    if len(expected_sha256) != 64 or any(c not in '0123456789abcdefABCDEF' for c in expected_sha256):
        raise ValueError('explicit 64-digit image SHA256 required')
    size = Path(path).stat().st_size
    if not 32 <= size <= partition['size']:
        raise ValueError('image does not fit app partition')
    data = Path(path).read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    if digest != expected_sha256.lower():
        raise ValueError('image SHA256 mismatch')
    if data[0] != 0xE9 or not 1 <= data[1] <= 16:
        raise ValueError('invalid ESP app image header')
    if struct.unpack_from('<H', data, 12)[0] != 9:
        raise ValueError('image chip ID is not ESP32-S3')
    if data[23] not in (0, 1):
        raise ValueError('invalid appended image digest flag')
    offset, checksum = 24, 0xEF
    for _ in range(data[1]):
        if offset + 8 > len(data):
            raise ValueError('truncated segment header')
        address, length = struct.unpack_from('<II', data, offset)
        offset += 8
        if length > partition['size'] or offset + length > len(data):
            raise ValueError('truncated/oversized image segment')
        # Prevent wrapping load addresses; detailed ROM validation still required.
        if address + length > 0x100000000:
            raise ValueError('segment address wraps')
        for byte in data[offset:offset + length]:
            checksum ^= byte
        offset += length
    checksum_at = offset | 15
    if checksum_at >= len(data) or data[checksum_at] != checksum:
        raise ValueError('invalid ESP image checksum')
    if any(data[offset:checksum_at]):
        raise ValueError('nonzero image checksum padding')
    end = checksum_at + 1
    if data[23]:
        if data[end:end + 32] != hashlib.sha256(data[:end]).digest():
            raise ValueError('invalid appended ESP image SHA256')
        end += 32
    if end != len(data):
        raise ValueError('trailing/signature/full-flash data unsupported; manual review required')
    return {'sha256': digest, 'bytes': len(data), 'chip': chip,
            'offset': partition['offset'], 'partition_size': partition['size']}


def physical_identity(device):
    """Resolve USB ancestor of a Linux tty; bus-port path survives ROM re-enumeration."""
    tty = Path(device).resolve(strict=True).name
    node = (Path('/sys/class/tty') / tty / 'device').resolve(strict=True)
    for parent in [node, *node.parents]:
        if (parent / 'idVendor').exists() and (parent / 'idProduct').exists():
            return {'vid': (parent / 'idVendor').read_text().strip().lower(),
                    'pid': (parent / 'idProduct').read_text().strip().lower(),
                    'serial': (parent / 'serial').read_text().strip(),
                    'location': parent.name}
    raise ValueError('not a USB serial device with explicit physical identity')


class Maintenance:
    """ESP initiates HELLO. Do not ACK a stale epoch inferred from PING.

    Silence before HELLO lets the stopped daemon's epoch expire within 8 s.
    After HELLO, reply to PING every 2 s and require READY for this request.
    """
    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.rx = ReceiveEpoch()
        self.decoder = Decoder()
        self.tx = WriteQueue()
        self.sequence = 0
        self.request = secrets.randbelow(0xFFFFFFFF) + 1
        self.prepared = False
        self.ready = False
        self.boot_queued = False
        self.last_rx = clock()
        self.ready_at = None

    def send(self, channel, kind, session=0, payload=b''):
        self.tx.put(Frame(channel, kind, self.rx.epoch, session, self.sequence, payload).encode())
        self.sequence = (self.sequence + 1) & 0xFFFFFFFF

    def feed(self, data):
        for frame in self.decoder.feed(data):
            state = self.rx.accept(frame)
            if state is None:
                continue
            self.last_rx = self.clock()
            if state == 'new':
                if self.prepared:
                    raise ValueError('epoch changed during maintenance; restart explicit confirmation')
                self.tx.put(b'\0')  # discard a partial frame left by the stopped service
                self.send(C.CONTROL, T.HELLO_ACK, payload=struct.pack('<HH', 512, 4096))
                self.send(C.MAINTENANCE, T.PREPARE_UPDATE, self.request)
                self.prepared = True
            elif state == 'hello':
                self.send(C.CONTROL, T.HELLO_ACK, payload=struct.pack('<HH', 512, 4096))
            elif frame.channel == C.CONTROL and frame.type == T.PING and not frame.session and not frame.payload:
                self.send(C.CONTROL, T.PONG)
            elif (frame.channel == C.MAINTENANCE and frame.type == T.UPDATE_READY
                  and frame.session == self.request and not frame.payload and self.prepared):
                if not self.ready:
                    self.ready = True
                    self.ready_at = self.clock()
            elif frame.type == T.ERROR:
                raise ValueError('device refused maintenance: ' + frame.payload.decode('utf-8', 'replace'))

    def enter_boot(self):
        if not self.ready or self.clock() - self.ready_at >= 15 or self.boot_queued:
            raise ValueError('missing/expired/already-used local grant')
        self.send(C.MAINTENANCE, T.ENTER_BOOT, self.request)
        self.boot_queued = True


def maintenance(device, identity, rom_identity, audit):
    if physical_identity(device) != identity:
        raise ValueError('physical device identity mismatch')
    fd = open_serial(device)
    state = Maintenance()
    deadline = time.monotonic() + 90
    try:
        if physical_identity(device) != identity:
            raise ValueError('device identity changed while opening')
        while time.monotonic() < deadline:
            with selectors.DefaultSelector() as selector:
                selector.register(fd, selectors.EVENT_READ |
                                  (selectors.EVENT_WRITE if state.tx.size else 0))
                ready = selector.select(0.1)
            for _, events in ready:
                if events & selectors.EVENT_READ:
                    data = os.read(fd, 4096)
                    if not data:
                        raise ValueError('disconnected before boot command completed')
                    state.feed(data)
                if events & selectors.EVENT_WRITE:
                    state.tx.flush(lambda data: os.write(fd, data))
            if state.rx.epoch and time.monotonic() - state.last_rx >= 8:
                raise ValueError('maintenance heartbeat timeout')
            if state.ready and not state.boot_queued:
                audit('device_local_confirm_received', {'epoch': state.rx.epoch, 'request': state.request})
                state.enter_boot()
            if state.boot_queued and not state.tx.size:
                audit('enter_boot_sent', {'request': state.request})
                break
        else:
            raise ValueError('HELLO/local confirmation timed out; no flashing attempted')
    finally:
        os.close(fd)
    # Observe the expected ROM identity on the SAME physical USB port.
    # This is not sufficient to authorize flash and is never advertised as attestation.
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        for tty in Path('/sys/class/tty').glob('ttyACM*'):
            candidate = '/dev/' + tty.name
            try:
                observed = physical_identity(candidate)
            except (OSError, ValueError):
                continue
            if observed == rom_identity:
                audit('rom_reconnect_observed', {'device': candidate, 'identity': observed})
                return candidate
        time.sleep(0.1)
    raise ValueError('expected ROM reconnect not observed; use hardware BOOT+RESET recovery')


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--device', required=True)
    p.add_argument('--vid', required=True)
    p.add_argument('--pid', required=True)
    p.add_argument('--serial', required=True)
    p.add_argument('--location', required=True, help='physical Linux USB port path, e.g. 1-2.3')
    p.add_argument('--image', required=True, type=Path)
    p.add_argument('--sha256', required=True)
    p.add_argument('--chip', required=True, choices=['esp32s3'])
    p.add_argument('--partitions', required=True, type=Path)
    p.add_argument('--execute', action='store_true')
    p.add_argument('--maintenance-only', action='store_true', help='enter ROM only; never flash')
    p.add_argument('--service', default='mixosd.service')
    p.add_argument('--service-stopped', action='store_true', help='operator acknowledges stopping normal service')
    p.add_argument('--rom-vid')
    p.add_argument('--rom-pid')
    p.add_argument('--rom-serial')
    p.add_argument('--audit', type=Path, help='required append-only user-local audit JSONL for execution')
    a = p.parse_args(argv)
    identity = dict(vid=a.vid.lower(), pid=a.pid.lower(), serial=a.serial, location=a.location)

    def audit(event, detail):
        record = dict(time=time.time(), event=event, detail=detail)
        print(json.dumps(record, ensure_ascii=False))
        if a.execute and a.audit:
            fd = os.open(a.audit, os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, 'O_NOFOLLOW', 0), 0o600)
            with os.fdopen(fd, 'a', encoding='utf-8') as stream:
                stream.write(json.dumps(record, ensure_ascii=False) + '\n')
                stream.flush()
                os.fsync(stream.fileno())

    try:
        image = validate_image(a.image, a.sha256, a.chip, validate_partitions(a.partitions))
        audit('preflight', {'dry_run': not a.execute, 'identity': identity, 'image': image,
                            'live_chip_layout_verified': False, 'flash_supported': False})
        if not a.execute:
            print('DRY RUN: no serial open, service change, reset or flash. Live ROM chip/layout/readback verification remains mandatory.')
            return 0
        if not a.audit:
            raise ValueError('--audit required for execution/refusal records')
        if not a.maintenance_only:
            raise ValueError('automatic flashing REFUSED: no audited ROM identity continuity, live partition-table verification, secure-boot/eFuse policy or readback profile. No hardware was opened.')
        if sys.platform != 'linux' or os.geteuid() == 0:
            raise ValueError('maintenance requires Linux and an ordinary serial-authorized user')
        if not a.device.startswith('/dev/serial/by-id/'):
            raise ValueError('explicit stable /dev/serial/by-id device required')
        if not a.service_stopped or not all((a.rom_vid, a.rom_pid, a.rom_serial)):
            raise ValueError('operator service-stopped acknowledgement and explicit ROM VID/PID/serial required')
        if a.service.startswith('-') or '/' in a.service:
            raise ValueError('invalid service unit name')
        status = subprocess.run(['systemctl', 'is-active', a.service], capture_output=True, text=True)
        if status.returncode not in (3,) or status.stdout.strip() not in ('inactive', 'failed'):
            raise ValueError('service is active or stopped state cannot be verified; stop it manually')
        if not sys.stdin.isatty():
            raise ValueError('interactive local operator confirmation required')
        phrase = 'ENTER BOOT ' + a.serial
        if input('No A/B rollback. BOOT+RESET recovery may be needed. Type ' + phrase + ': ') != phrase:
            raise ValueError('operator confirmation declined')
        rom = dict(vid=a.rom_vid.lower(), pid=a.rom_pid.lower(), serial=a.rom_serial, location=a.location)
        if rom == identity:
            raise ValueError('ROM identity must be distinguishable from running application')
        audit('operator_confirmed_maintenance_only', identity)
        port = maintenance(a.device, identity, rom, audit)
        print('ROM reconnect observed at ' + port + '; no flash performed. Use an independently audited app-only procedure.')
        return 0
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        audit('refused_or_aborted', {'reason': str(exc), 'flash_written': False})
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
