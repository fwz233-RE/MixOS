#!/usr/bin/env python3
"""Pi-side TypixDeck flashing worker. Keeps NVS and the 4 MiB font intact.

Invoked by flash_esp_remote.py with an uploaded, SHA256-checked package.
Two operations:

  default    app-only write at 0x10000, for a device whose live partition
             table already matches the built one.
  --migrate  one-time move from the historic factory-only table to the A/B
             table, writing the app, blanking otadata, then the table and the
             bootloader. After this the device can be updated over USB with
             tools/ota_esp.py and never needs esptool again.

Fails closed on identity/layout/security/backup mismatch; never erases the chip.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import struct
import subprocess
import sys
import time
import zipfile

ESP_ENV = None

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _mixlib.guards import device_lock
from _mixlib.durable import fsync_file

from update_esp import (validate_image, validate_partitions, physical_identity, Maintenance,
                        identify_partition_binary)
from protocol import Channel as C, Type as T, Frame, Decoder

ROOT = Path(__file__).resolve().parents[1]
# Where the A/B table keeps otadata: inside the tail of the old 2 MiB factory
# partition, so blanking it never touches nvs or the font.
ROOT_OTADATA = (0x200000, 0x2000)


def prepare_esptool(wheel, digest):
    """Use the uploaded official wheel only for esptool; never alter system Python."""
    global ESP_ENV
    if hashlib.sha256(wheel.read_bytes()).hexdigest() != digest:
        raise ValueError('Transferred esptool wheel hash mismatch')
    vendor = ROOT / '.esptool'
    with zipfile.ZipFile(wheel) as archive:
        if any(name.startswith('/') or '..' in Path(name).parts for name in archive.namelist()):
            raise ValueError('Invalid esptool wheel paths')
        archive.extractall(vendor)
    if not list((vendor / 'esptool').rglob('*32s3*.json')):
        raise ValueError('Uploaded esptool wheel lacks ESP32-S3 stub')
    ESP_ENV = dict(os.environ, PYTHONPATH=str(vendor))
    check = subprocess.run([sys.executable, '-m', 'esptool', 'version'],
                           env=ESP_ENV, capture_output=True, text=True, timeout=20)
    if check.returncode or '4.7.0' not in check.stdout:
        raise RuntimeError('Isolated esptool dependencies/version failed: ' + check.stdout + check.stderr)
    audit('isolated_esptool_verified', sha256=digest, version=check.stdout.strip())


def load_probe(job, serial, digest):
    """Resume ONLY an audited no-write probe, with exact ROM USB identity."""
    if not re.fullmatch(r'mixos-flash-\d{8}-\d{6}', job):
        raise ValueError('Invalid probe job name')
    source = ROOT.parent / job
    if (source / 'resume-claim.json').exists():
        raise ValueError('This probe was already used for a write attempt; inspect that attempt before recovery')
    records = [json.loads(line) for line in (source / 'flash-audit.jsonl').read_text().splitlines()]
    if not records or records[-1].get('event') != 'download_probe_verified':
        raise ValueError('Resume requires a successfully completed download-only probe')
    if any(r.get('event') in ('app_write_start', 'flash_and_boot_verified') for r in records):
        raise ValueError('Probe contains flash writes; refusing automatic retry')
    before = [r for r in records if r.get('event') == 'preflight_ok']
    after = [r for r in records if r.get('event') == 'rom_identified']
    if len(before) != 1 or len(after) != 1:
        raise ValueError('Probe identity evidence is ambiguous')
    app, rom = before[0]['identity'], after[0]['identity']
    if (before[0]['app_sha256'] != digest or app['serial'] != serial or app['vid'] != '303a'
            or app['pid'] != '80c3' or rom['vid'] != '303a' or rom['pid'] not in ('0009', '1001')
            or app['location'] != rom['location'] or not rom['serial']):
        raise ValueError('Probe identity or app hash does not match this migration')
    return source, app, rom


def audit(event, **details):
    line = json.dumps(dict(event=event, time=time.time(), **details))
    print(line, flush=True)
    with (ROOT / 'flash-audit.jsonl').open('a') as out:
        out.write(line + '\n')
        out.flush()
        os.fsync(out.fileno())


def ports():
    found = []
    for path in sorted(Path('/sys/class/tty').glob('ttyACM*')):
        dev = '/dev/' + path.name
        try:
            found.append((dev, physical_identity(dev)))
        except (OSError, ValueError):
            continue
    return found


def wait_port(location, rom, serial=None, timeout=25):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        found = [(dev, ident) for dev, ident in ports()
                 if ident['location'] == location and ident['vid'] == '303a'
                 and ((ident['pid'] in ('1001', '0009')) if rom else
                      (ident['pid'] == '80c3' and ident['serial'] == serial))]
        if len(found) == 1:
            return found[0]
        time.sleep(0.2)
    raise RuntimeError('Expected USB mode not observed on the same physical port; hardware BOOT/RESET may be needed')


def idle_port(dev):
    proc = subprocess.run(['fuser', dev], capture_output=True, text=True, timeout=5)
    if proc.returncode != 1 or proc.stdout.strip():
        raise RuntimeError('Serial port busy or ownership check failed: ' + proc.stdout + proc.stderr)


def open_port(dev):
    import serial
    idle_port(dev)
    port = serial.Serial(port=None, baudrate=115200, timeout=0.2, write_timeout=3, exclusive=True)
    port.dtr = True
    port.rts = False
    port.port = dev
    port.open()
    return port


def enter_download(dev, identity, mode):
    with open_port(dev) as port:
        if physical_identity(dev) != identity:
            raise RuntimeError('USB identity changed while opening serial port')
        if mode == 'legacy':
            # Only for the ORIGINAL firmware, never added to the MixOS binary parser.
            # Allow the USB line-state request to reach TinyUSB, and terminate
            # any partial line left by an earlier esptool binary handshake.
            deadline = time.monotonic() + 1.5
            while time.monotonic() < deadline:
                port.read(4096)
            port.write(b'\r\nEGGFLY_REBOOT_TO_BOOT_MODE\r\n')
            port.flush()
            audit('legacy_download_command_sent', device=dev)
            reply = bytearray()
            deadline = time.monotonic() + 8
            while time.monotonic() < deadline:
                try:
                    reply.extend(port.read(4096))
                except OSError:
                    break  # Expected when the device detaches to re-enumerate.
                if b'entering download mode' in reply or len(reply) >= 16384:
                    break  # Do not hold the old CDC handle across ROM re-enumeration.
            audit('legacy_console_reply', text=reply.decode('utf-8', 'replace'))
        else:
            audit('awaiting_local_screen_confirmation', timeout_seconds=90)
            state = Maintenance()
            deadline = time.monotonic() + 90
            while time.monotonic() < deadline:
                state.feed(port.read(4096))
                if state.ready and not state.boot_queued:
                    state.enter_boot()
                if state.tx.size:
                    state.tx.flush(port.write)
                if state.boot_queued and not state.tx.size:
                    port.flush()
                    audit('mixos_authorized_enter_boot_sent')
                    break
            else:
                raise RuntimeError('MixOS local confirmation timed out; nothing flashed')


def flash_session(dev, identity, image, table, digest, bootloader=None, migrate=False, source=None):
    """Keep one USB handle from ROM security query through readback and reset."""
    if ESP_ENV:
        sys.path.insert(0, ESP_ENV['PYTHONPATH'])
    import esptool
    idle_port(dev)
    chip = esptool.get_default_connected_device(
        [dev], port=dev, connect_attempts=3, initial_baud=115200, chip='esp32s3', before='no_reset')
    if chip is None:
        raise RuntimeError('Failed to connect to the identified ROM')
    try:
        if physical_identity(dev) != identity:
            raise RuntimeError('ROM changed during single-session connect')
        security = chip.get_security_info()
        validate_security(f"Flags: {security['flags']:#x}")
        if security['chip_id'] != 9 or security['flash_crypt_cnt'] != 0:
            raise ValueError('Unexpected ROM chip/encryption state')
        mac = ':'.join(f'{b:02x}' for b in chip.read_mac())
        if mac != identity['serial'].lower():
            raise ValueError('ROM MAC differs from audited USB serial')
        audit('rom_security_verified', mac=mac, **security)
        chip = chip.run_stub()
        chip.change_baud(460800)
        chip.flash_set_parameters(0x800000)
        audit('single_session_stub_ready', device=dev, version=esptool.__version__)

        def command(*args):
            audit('esptool_session_command', args=list(args))
            # The stub is already loaded on this handle. --no-stub prevents
            # esptool.main from uploading a second copy; reads still use chip.IS_STUB.
            esptool.main(['--chip', 'esp32s3', '--port', dev, '--baud', '460800', '--no-stub',
                          '--before', 'no_reset', '--after', 'no_reset_stub'] + list(args), esp=chip)

        backup = ROOT / 'original-flash-8MB.bin'
        command('read_flash', '0x0', '0x800000', str(backup), '--no-progress')
        data = backup.read_bytes()
        live = verify_snapshot(data, table, migrate)
        fsync_file(backup)
        audit('backup_and_layout_verified', file=str(backup), live_layout=live['name'],
              sha256=hashlib.sha256(data).hexdigest(), bytes=len(data))
        if physical_identity(dev) != identity:
            raise RuntimeError('ROM identity changed before write')
        if source:
            with (source / 'resume-claim.json').open('x') as claim:
                json.dump(dict(write_job=str(ROOT), app_sha256=digest), claim)
                claim.flush()
                os.fsync(claim.fileno())
        audit('app_write_start', offset='0x10000', bytes=image.stat().st_size,
              operation='migrate' if migrate else 'app_only')
        # The application goes first in both operations. 0x10000 is the app
        # offset in the old and the new table alike, so a power cut here leaves
        # a device that still boots, just without OTA slots.
        command('write_flash', '--compress', '--flash_mode', 'keep', '--flash_freq', 'keep',
                '--flash_size', '8MB', '0x10000', str(image))
        if not migrate:
            readback = ROOT / 'app-readback.bin'
            command('read_flash', '0x10000', str(image.stat().st_size), str(readback), '--no-progress')
            if hashlib.sha256(readback.read_bytes()).hexdigest() != digest:
                raise RuntimeError('App readback SHA256 mismatch; keep backup and inspect before retry')
            audit('app_readback_sha256_verified', sha256=digest)
        else:
            start, length = ROOT_OTADATA
            # Blank otadata: with no factory partition the bootloader then boots
            # ota_0, which is the app just written, and records ota_seq=1 itself.
            command('erase_region', hex(start), hex(length))
            # The new table is what actually turns the device into an A/B device.
            command('write_flash', '--flash_mode', 'keep', '--flash_freq', 'keep',
                    '--flash_size', 'keep', '0x8000', str(ROOT / 'partition-table.bin'))
            # The bootloader is last. It is the only write whose failure needs
            # hardware BOOT+RESET recovery, and the device is already bootable
            # and already A/B without it; it only adds automatic rollback.
            command('write_flash', '--flash_mode', 'keep', '--flash_freq', 'keep',
                    '--flash_size', 'keep', '0x0', str(bootloader))
            verify_migration(command, image, digest, table, bootloader, data)
        chip.hard_reset()
    finally:
        chip._port.close()


def verify_migration(command, image, digest, table, bootloader, backup):
    """Read the whole rewritten region back and prove nothing else moved."""
    readback = ROOT / 'migration-readback.bin'
    end = ROOT_OTADATA[0] + 0x12000  # through the first font sectors
    command('read_flash', '0x0', hex(end), str(readback), '--no-progress')
    data = readback.read_bytes()
    if len(data) != end:
        raise RuntimeError('Short migration readback; keep the backup and inspect before retry')
    boot = bootloader.read_bytes()
    if data[:len(boot)] != boot:
        raise RuntimeError('Bootloader readback mismatch; do not power-cycle, use BOOT+RESET recovery')
    if data[0x8000:0x8C00] != table:
        raise RuntimeError('Partition table readback mismatch; device may not boot, keep the backup')
    identify_partition_binary(data[0x8000:0x8C00], expect='ab')
    app = image.read_bytes()
    if hashlib.sha256(data[0x10000:0x10000 + len(app)]).hexdigest() != digest:
        raise RuntimeError('App readback SHA256 mismatch; keep backup and inspect before retry')
    start, length = ROOT_OTADATA
    if data[start:start + length] != b'\xff' * length:
        raise RuntimeError('otadata is not blank; the bootloader could select an unwritten slot')
    if data[0x9000:0x10000] != backup[0x9000:0x10000]:
        raise RuntimeError('nvs/phy_init changed during migration')
    if data[0x210000:end] != backup[0x210000:end]:
        raise RuntimeError('font partition changed during migration')
    audit('migration_readback_verified', app_sha256=digest,
          bootloader_sha256=hashlib.sha256(boot).hexdigest(),
          table_sha256=hashlib.sha256(table).hexdigest(),
          otadata='blank', preserved=['nvs', 'phy_init', 'font'])


def validate_security(text):
    flags = re.search(r'Flags:\s*(0x[0-9a-fA-F]+)', text)
    if not flags or int(flags.group(1), 16) != 0:
        raise ValueError('Secure boot/encryption/security flags not verified as zero; refusing migration')


def verify_snapshot(data, table, migrate):
    """Check the pre-write backup and identify the layout the device runs now."""
    if len(data) != 0x800000:
        raise ValueError('Incomplete 8 MiB recovery backup')
    if len(table) != 0xC00:
        raise ValueError('Built partition table is not 0xC00 bytes')
    if data[0] != 0xE9 or data[0x10000] != 0xE9:
        raise ValueError('Backup lacks original bootloader/app')
    if data[0x210000:0x210004] not in (b'\x00\x01\x00\x00', b'OTTO', b'ttcf'):
        raise ValueError('Existing font partition is missing a valid font header')
    live = identify_partition_binary(data[0x8000:0x8C00])
    if not migrate and data[0x8000:0x8C00] != table:
        raise ValueError(f'Live table is the {live["name"]} layout but the build expects a '
                         'different one; rerun with --migrate to rewrite the table')
    return live


def verify_running(dev, identity, seconds=15):
    decoder = Decoder()
    epoch = None
    sequence = 0
    pings = 0
    with open_port(dev) as port:
        if physical_identity(dev) != identity:
            raise RuntimeError('Application identity changed while opening')
        port.write(b'\0')
        start = time.monotonic()
        deadline = start + 25
        last = start
        while time.monotonic() < deadline:
            for frame in decoder.feed(port.read(4096)):
                if (frame.channel == C.CONTROL and frame.type == T.HELLO and frame.session == 0
                        and frame.epoch and frame.payload == struct.pack('<HH', 512, 4096)):
                    if epoch is not None and epoch != frame.epoch:
                        raise RuntimeError('MixOS restarted/disconnected during boot verification')
                    epoch = frame.epoch
                    sequence += 1
                    port.write(Frame(C.CONTROL, T.HELLO_ACK, epoch, sequence=sequence, payload=frame.payload).encode())
                    last = time.monotonic()
                elif (epoch and frame.epoch == epoch and frame.channel == C.CONTROL
                      and frame.type == T.PING and frame.session == 0 and not frame.payload):
                    sequence += 1
                    port.write(Frame(C.CONTROL, T.PONG, epoch, sequence=sequence).encode())
                    pings += 1
                    last = time.monotonic()
            if epoch and time.monotonic() - last > 8:
                raise RuntimeError('MixOS heartbeat timed out')
            if time.monotonic() - start >= seconds and pings >= 3:
                return {'epoch': epoch, 'pings': pings, 'observed_seconds': time.monotonic() - start}
    raise RuntimeError('USB enumerated but valid MixOS HELLO/heartbeat was not observed')


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--serial', required=True)
    p.add_argument('--boot-mode', choices=['legacy', 'mixos'], required=True)
    p.add_argument('--sha256', required=True)
    p.add_argument('--partition-sha256', required=True)
    p.add_argument('--probe-download', action='store_true', help='Test entry to ROM only; never read/write flash')
    p.add_argument('--resume-probe', help='Resume exact ROM identity from a completed no-write probe job')
    p.add_argument('--esptool-wheel-sha256', help='Verify and use staged esptool.whl with official stub')
    p.add_argument('--migrate', action='store_true',
                   help='Also write the partition table and bootloader and blank otadata, '
                        'moving the device to the A/B layout so future updates go over USB')
    p.add_argument('--bootloader-sha256', help='Required with --migrate: hash of the staged bootloader.bin')
    a = p.parse_args()
    if a.resume_probe and a.probe_download:
        p.error('--resume-probe cannot be combined with --probe-download')
    if a.migrate and not a.bootloader_sha256:
        p.error('--migrate requires --bootloader-sha256')
    if sys.platform != 'linux' or os.geteuid() == 0:
        raise RuntimeError('Run on the Pi as an ordinary dialout user')
    image = ROOT / 'firmware/esp32s3/build/mixos_esp32s3.bin'
    table = (ROOT / 'partition-table.bin').read_bytes()
    if hashlib.sha256(table).hexdigest() != a.partition_sha256:
        raise ValueError('Transferred partition table hash mismatch')
    layout = validate_partitions(ROOT / 'firmware/esp32s3/partitions.csv')
    identify_partition_binary(table, expect=layout['name'])
    validate_image(image, a.sha256, 'esp32s3', layout)
    bootloader = None
    if a.migrate:
        if layout['name'] != 'ab':
            raise ValueError('--migrate only makes sense when the build targets the A/B layout')
        bootloader = ROOT / 'bootloader.bin'
        if hashlib.sha256(bootloader.read_bytes()).hexdigest() != a.bootloader_sha256:
            raise ValueError('Transferred bootloader hash mismatch')
        if bootloader.stat().st_size > 0x8000 or bootloader.read_bytes()[0] != 0xE9:
            raise ValueError('Staged bootloader is not a bootloader image that fits before 0x8000')
    if not shutil.which('fuser') or shutil.disk_usage(ROOT).free < 32 * 1024 * 1024:
        raise RuntimeError('Missing fuser or insufficient recovery backup space')
    service = subprocess.run(['systemctl', 'is-active', 'mixosd.service'], capture_output=True, text=True)
    if service.stdout.strip() not in ('inactive', 'failed', 'unknown'):
        raise RuntimeError('Stop the normal mixosd service before updating')
    source = None
    if a.resume_probe:
        source, ident, expected_rom = load_probe(a.resume_probe, a.serial, a.sha256)
        candidates = [(dev, identity) for dev, identity in ports() if identity == expected_rom]
        if len(candidates) != 1:
            raise RuntimeError('Current ROM does not exactly match successful probe: ' + repr(ports()))
        rom, rom_ident = candidates[0]
        dev = rom
    else:
        candidates = [(dev, ident) for dev, ident in ports() if ident['vid'] == '303a'
                      and ident['pid'] == '80c3' and ident['serial'] == a.serial]
        if len(candidates) != 1:
            raise RuntimeError('Expected exactly one matching running application USB serial: ' + repr(ports()))
        dev, ident = candidates[0]
    lockdir = Path.home() / '.cache/mixos'
    lockdir.mkdir(parents=True, exist_ok=True)
    with device_lock(lockdir / 'flash.lock'):
        audit('preflight_ok', device=dev, identity=ident, app_sha256=a.sha256, mode=a.boot_mode,
              operation='migrate' if a.migrate else 'app_only')
        if a.esptool_wheel_sha256:
            prepare_esptool(ROOT / 'esptool.whl', a.esptool_wheel_sha256)
        if source:
            # Revalidate under the shared update lock (another job may have claimed it).
            load_probe(a.resume_probe, a.serial, a.sha256)
            if physical_identity(rom) != rom_ident:
                raise RuntimeError('ROM changed while obtaining update lock')
            audit('probe_resume_verified', source=str(source), identity=rom_ident)
        else:
            enter_download(dev, ident, a.boot_mode)
            rom, rom_ident = wait_port(ident['location'], rom=True)
        audit('rom_identified', device=rom, identity=rom_ident)
        if a.probe_download:
            audit('download_probe_verified', note='No flash reads or writes; device remains in ROM download mode')
            return
        flash_session(rom, rom_ident, image, table, a.sha256, bootloader, a.migrate, source)
        app, new_ident = wait_port(ident['location'], rom=False, serial=a.serial, timeout=40)
        result = verify_running(app, new_ident)
        audit('flash_and_boot_verified', identity=new_ident, **result)
        if a.migrate:
            print('SUCCESS: device migrated to the A/B layout, readback verified, MixOS handshake and '
                  'heartbeat verified. Future updates can use tools/ota_esp.py over USB.', flush=True)
        else:
            print('SUCCESS: app flashed, readback verified, MixOS USB handshake and heartbeat verified.', flush=True)


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        audit('aborted', reason=str(exc), note='Consult audit to distinguish preflight/write/boot failures. Backups are retained.')
        sys.exit(1)
