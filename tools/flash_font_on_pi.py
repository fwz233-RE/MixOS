#!/usr/bin/env python3
"""Font updater with an explicit, validated app+font option; dry-run by default.

Requires explicit host --execute authorization; no on-screen confirmation.
Uses existing audited USB/ROM helpers without changing the app-only updater.
Finite serial/deadline timeouts and progress-audited chunks bound reads.
A fresh full backup and exact live layout/app checks precede every write.
--new-app-sha256 explicitly enables app replacement.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import stat
import time
import shutil
import struct
import subprocess
import sys

# The launcher uses -I. Add only this root-owned package's helper directory;
# never depend on user-site packages or the process working directory.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _mixlib.guards import device_lock, operation_timeout
from _mixlib.durable import durable_new, sync_directory
import flash_esp_on_pi as esp
import display_transport as transport
from update_esp import (physical_identity, validate_image, validate_partitions,
                        identify_partition_binary)

FONT_OFFSET = 0x210000
FONT_SIZE = 0x400000
FLASH_SIZE = 0x800000
# Fixed regions of the 8 MiB layout. The bootloader owns everything before the
# partition table, the table owns one sector before nvs, and otadata lives in
# the tail of the old 2 MiB factory partition so migrating never touches nvs.
APP_OFFSET = 0x10000
LEGACY_APP_SIZE = 0x200000
AB_APP_SIZE = 0x1F0000
BOOTLOADER_REGION = (0x0, 0x8000)
TABLE_REGION = (0x8000, 0x1000)
OTADATA_REGION = (0x200000, 0x2000)
SECTOR = 0x1000
ESPTOOL_VERSION = transport.VERSION
WHEEL_SHA256 = transport.WHEEL_SHA256
ROOT = Path(__file__).resolve().parents[1]
PACKAGE_BASE = Path('/opt/mixos-display-packages')
READ_CHUNK = 0x40000
PORT_TIMEOUT = 10


def prepare_esptool(wheel):
    """Use the pinned display runtime without modifying the app-only updater."""
    esp.ESP_ENV = transport.prepare(wheel.parent, esp.ROOT / '.esptool')
    esp.audit('isolated_esptool_verified', sha256=WHEEL_SHA256, version=ESPTOOL_VERSION)


def enter_download_direct(dev, identity):
    """An explicit host execute command authorizes the new firmware's grant.

    Never synthesizes local touch/keys. Older firmware requiring a local grant
    fails closed here; its ROM can instead be entered using hardware recovery.
    """
    from update_esp import Maintenance
    with esp.open_port(dev) as port:
        if physical_identity(dev) != identity:
            raise RuntimeError('Application identity changed')
        state = Maintenance()
        deadline = time.monotonic() + 25
        prepared = False
        while time.monotonic() < deadline:
            state.feed(port.read(4096))
            if state.prepared and not prepared:
                prepared = True
                esp.audit('display_host_prepare_queued', request=state.request, epoch=state.rx.epoch)
            if state.ready and not state.boot_queued:
                state.enter_boot()
            if state.tx.size:
                state.tx.flush(port.write)
            if state.boot_queued and not state.tx.size:
                port.flush()
                esp.audit('display_host_enter_boot_sent', request=state.request, epoch=state.rx.epoch)
                return
        raise RuntimeError('Automatic host update grant unavailable; no flash written. '
                           'Old firmware may require hardware ROM entry for this first upgrade.')


def select_direct_device(serial, location, rom_serial):
    matches = [(dev, ident) for dev, ident in esp.ports() if ident.get('location') == location]
    if len(matches) != 1:
        raise ValueError('Expected one ESP on the exact physical USB path')
    dev, ident = matches[0]
    app = ident == {'vid': '303a', 'pid': '80c3', 'serial': serial, 'location': location}
    rom = (ident.get('vid') == '303a' and ident.get('pid') in ('0009', '1001')
           and ident.get('serial', '').lower() == rom_serial.lower())
    if not app and not rom:
        raise ValueError('Unexpected ESP application/ROM identity')
    return dev, ident, rom


# operation_timeout moved to tools/_mixlib/guards.py, which degrades on
# platforms without SIGALRM instead of failing at import.


def configure_port(chip):
    chip._port.timeout = PORT_TIMEOUT
    chip._port.write_timeout = PORT_TIMEOUT
    if chip._port.timeout != PORT_TIMEOUT or chip._port.write_timeout != PORT_TIMEOUT:
        raise RuntimeError('Finite serial timeouts could not be established')


def read_full_flash(chip, phase):
    esp.audit('font_' + phase + '_read_start', bytes=FLASH_SIZE, chunk_bytes=READ_CHUNK,
              serial_timeout_seconds=PORT_TIMEOUT, total_timeout_seconds=600)
    start = time.monotonic()
    pieces = []
    for offset in range(0, FLASH_SIZE, READ_CHUNK):
        remaining = 600 - (time.monotonic() - start)
        if remaining <= 0:
            raise TimeoutError(phase + ' total deadline expired')
        last_report = 0
        def progress(done, total, read_offset):
            nonlocal last_report
            if read_offset != offset or total != READ_CHUNK:
                raise ValueError('Unexpected flash-read progress geometry')
            if done - last_report >= 0x10000 or done == total:
                esp.audit('font_' + phase + '_read_progress', bytes=offset + done, total=FLASH_SIZE)
                last_report = done
        with operation_timeout(phase + ' chunk at ' + hex(offset), min(60, remaining)):
            data = chip.read_flash(offset, READ_CHUNK, progress_fn=progress)
        if len(data) != READ_CHUNK:
            raise ValueError(phase + ' returned a short/oversized flash chunk')
        pieces.append(data)
    data = b''.join(pieces)
    esp.audit('font_' + phase + '_read_complete', bytes=len(data), sha256=sha(data))
    return data


def stopped_job(job):
    result = subprocess.run(['systemctl', 'show', job + '.service', '--no-pager',
                             '-p', 'LoadState', '-p', 'ActiveState', '-p', 'MainPID', '-p', 'ControlPID'],
                            capture_output=True, text=True, timeout=10, check=False)
    fields = dict(line.split('=', 1) for line in result.stdout.splitlines() if '=' in line)
    if (result.returncode != 0 or fields.get('LoadState') not in ('loaded', 'not-found')
            or fields.get('ActiveState') not in ('inactive', 'failed')
            or fields.get('MainPID') != '0' or fields.get('ControlPID') != '0'):
        raise ValueError('Prior job is not demonstrably stopped with zero live/control PIDs')


def trusted_package(path):
    for directory in (path, *path.parents):
        info = directory.lstat()
        if (directory.resolve(strict=True) != directory or not stat.S_ISDIR(info.st_mode)
                or info.st_uid != 0 or info.st_mode & 0o022):
            raise ValueError('Prior package must have canonical root-owned protected ancestors')


def trusted_bytes(path, read=True):
    info = path.lstat()
    if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
            or info.st_uid != 0 or info.st_mode & 0o022
            or path.resolve(strict=True) != path):
        raise ValueError('Prior package artifact is not an immutable root-owned regular file')
    trusted_package(path.parent)
    return path.read_bytes() if read else b''


def resume_evidence(source, requested, serial, location, rom_serial):
    """Read-only proof for one previously consented request that never began writing."""
    source = Path(source)
    relative = source.relative_to(PACKAGE_BASE)
    if (len(relative.parts) != 3 or relative.parts[1:] != ('work', 'session')
            or not re.fullmatch(r'mixos-display-\d{8}-\d{6}', relative.parts[0])
            or source.resolve(strict=True) != source):
        raise ValueError('Resume source must be an exact prior package work/session path')
    job = relative.parts[0]
    package = PACKAGE_BASE / job
    if package == ROOT or source == esp.ROOT:
        raise ValueError('Resume must use a separate prior package and workdir')
    trusted_package(package)
    stopped_job(job)
    trusted_bytes(package / 'start-claim', read=False)
    # Permit only known pre-write artifacts. This also rejects alternate write
    # claims, readbacks, unrecognized recovery files, and already-claimed sources.
    allowed_files = {'flash-audit.jsonl', '.esptool', 'font-job-claim.json',
                     'original-flash-8MB.bin', 'original-flash-8MB.bin.sha256'}
    if any(path.name not in allowed_files or path.is_symlink() for path in source.iterdir()):
        raise ValueError('Prior source has write/resume evidence or unknown artifacts')
    if not (source / 'font-job-claim.json').is_file():
        raise ValueError('Missing original one-shot job claim')
    audit_path = source / 'flash-audit.jsonl'
    info = audit_path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > 4 * 1024 * 1024:
        raise ValueError('Unsafe or oversized prior audit')
    audit_bytes = audit_path.read_bytes()
    records = [json.loads(line) for line in audit_bytes.decode().splitlines()]
    allowed_events = {'font_preflight_ok', 'isolated_esptool_verified', 'awaiting_local_screen_confirmation',
                      'mixos_authorized_enter_boot_sent', 'display_host_execute_authorized', 'font_resume_no_write_claimed', 'font_rom_security_verified', 'font_aborted',
                      'font_stub_start', 'font_stub_ready', 'font_spi_attach_start', 'font_spi_attached',
                      'font_flash_identified', 'font_flash_parameters_ready', 'font_backup_read_start',
                      'font_backup_read_progress', 'font_backup_read_complete', 'font_backup_verified'}
    if any(row.get('event') not in allowed_events for row in records):
        raise ValueError('Prior audit contains write/unknown events; resume forbidden')
    def unique(event):
        matches = [(index, row) for index, row in enumerate(records) if row.get('event') == event]
        if len(matches) != 1:
            raise ValueError('Missing/ambiguous prior evidence: ' + event)
        return matches[0]
    pre_i, pre = unique('font_preflight_ok')
    wait_i, _ = unique('awaiting_local_screen_confirmation')
    consent_i, _ = unique('mixos_authorized_enter_boot_sent')
    security_i, security = unique('font_rom_security_verified')
    identity = {'vid': '303a', 'pid': '80c3', 'serial': serial, 'location': location}
    if not pre_i < wait_i < consent_i < security_i or pre.get('identity') != identity:
        raise ValueError('Prior consent ordering or application identity does not match')
    if (security.get('flags') != 0 or security.get('chip_id') != 9
            or security.get('flash_crypt_cnt') != 0 or security.get('mac') != rom_serial.lower()):
        raise ValueError('Prior zero-security/ROM MAC evidence does not match')
    if pre.get('font_sha256') != requested['font.ttf'] or pre.get('app_sha256') != requested['firmware/esp32s3/build/mixos_esp32s3.bin']:
        raise ValueError('Prior consent artifact hashes do not match requested old app/font')
    for name, digest in requested.items():
        if sha(trusted_bytes(package / name)) != digest:
            raise ValueError('Prior immutable artifact differs: ' + name)
    # The old worker did not log the new-app hash. Bind it to the immutable
    # launch command, rather than treating an arbitrary staged new-app as consent.
    launch = trusted_bytes(package / 'launch.sh').decode()
    lines = [shlex.split(line) for line in launch.splitlines() if line.startswith('exec runuser ')]
    if len(lines) != 1 or lines[0][:5] != ['exec', 'runuser', '-u', 'pi', '--']:
        raise ValueError('Prior immutable launch command is ambiguous')
    command = lines[0]
    def option(name):
        if command.count(name) != 1 or command.index(name) + 1 >= len(command):
            raise ValueError('Missing/duplicate prior launch option: ' + name)
        return command[command.index(name) + 1]
    bindings = {'--sha256': 'font.ttf', '--app-sha256': 'firmware/esp32s3/build/mixos_esp32s3.bin',
                '--partition-sha256': 'partition-table.bin'}
    if 'new-app.bin' in requested:
        bindings['--new-app-sha256'] = 'new-app.bin'
    elif '--new-app-sha256' in command:
        raise ValueError('Prior consent included a different app-update request')
    if 'bootloader.bin' in requested:
        bindings['--bootloader-sha256'] = 'bootloader.bin'
        if command.count('--migrate') != 1:
            raise ValueError('Prior consent did not authorize the A/B migration')
    elif '--bootloader-sha256' in command or '--migrate' in command:
        raise ValueError('Prior consent included a migration this request does not ask for')
    if (any(option(flag) != requested[name] for flag, name in bindings.items())
            or command.count('--execute') != 1 or option('--workdir') != str(source)
            or '--resume-no-write-source' in command
            or str(package / 'tools/flash_font_on_pi.py') not in command):
        raise ValueError('Prior launch did not authorize this exact request')
    return {'source': str(source), 'job': job, 'prior_audit_sha256': sha(audit_bytes),
            'requested_hashes': requested, 'application_identity': identity, 'rom_serial': rom_serial.lower()}


def claim_resume(source, requested, serial, location, rom_serial):
    evidence = resume_evidence(source, requested, serial, location, rom_serial)
    candidates = [(dev, ident) for dev, ident in esp.ports() if ident.get('location') == location]
    if len(candidates) != 1:
        raise ValueError('Resume requires one current ROM on the exact physical USB location')
    dev, identity = candidates[0]
    if (identity.get('vid') != '303a' or identity.get('pid') not in ('0009', '1001')
            or identity.get('serial', '').lower() != rom_serial.lower()):
        raise ValueError('Current ROM identity differs from prior locally authorized request')
    evidence['destination'] = str(esp.ROOT)
    evidence['current_rom_identity'] = identity
    durable_new(Path(source) / 'resume-claim.json', json.dumps(evidence, sort_keys=True).encode())
    esp.audit('font_resume_no_write_claimed', **evidence)
    return dev, identity


def sha(data):
    return hashlib.sha256(data).hexdigest()


def validate_font(data, expected):
    if sha(data) != expected.lower():
        raise ValueError('Font SHA256 mismatch')
    if not 12 <= len(data) <= FONT_SIZE or data[:4] != b'\x00\x01\x00\x00':
        raise ValueError('Expected a bounded, standalone TrueType sfnt')
    count = struct.unpack_from('>H', data, 4)[0]
    if not 1 <= count <= 128 or 12 + count * 16 > len(data):
        raise ValueError('Invalid sfnt directory')
    tags = set()
    for i in range(count):
        tag, _, offset, size = struct.unpack_from('>4sIII', data, 12 + i * 16)
        if tag in tags or offset < 12 + count * 16 or offset + size > len(data):
            raise ValueError('Invalid sfnt table bounds or duplicate tag')
        tags.add(tag)
    if not {b'cmap', b'glyf', b'loca', b'head', b'hhea', b'hmtx', b'maxp'} <= tags:
        raise ValueError('Required TrueType tables absent')
    return data + b'\xff' * (FONT_SIZE - len(data))


def validate_table(table):
    """Bind binary partition entries to one of the two audited fixed layouts.

    Returns the identified layout. `identify_partition_binary` only accepts the
    historic factory-only table and the A/B table, both pinned to exact offsets
    and sizes, so this stays as strict as the previous hard-coded row list while
    also recognising the table the one-time migration installs.
    """
    if len(table) != 0xc00:
        raise ValueError('Invalid binary partition table size')
    layout = identify_partition_binary(table)
    entries = 32 * len(layout['rows'])
    md5_record = b'\xeb\xeb' + b'\xff' * 14 + hashlib.md5(table[:entries]).digest()
    if (table[entries:entries + 32] != md5_record
            or table[entries + 32:] != b'\xff' * (len(table) - entries - 32)):
        raise ValueError('Invalid partition MD5 or unexpected extra partitions')
    return layout


def validate_backup(data, table, app, migrate=False):
    """Check the staged table, the layout the device runs now, and its app."""
    staged = validate_table(table)
    if migrate and staged['name'] != 'ab':
        raise ValueError('Migration requires the staged A/B partition table')
    live = esp.verify_snapshot(data, table, migrate)
    if migrate and live['name'] != 'legacy':
        raise ValueError('Device already runs the A/B layout; migration is not needed')
    if data[APP_OFFSET:APP_OFFSET + len(app)] != app:
        raise ValueError('Live application differs from the staged, verified MixOS app')
    return live


def verify_migrated(after, before):
    """Name, rather than merely imply, what the migration achieved.

    The full-image comparison in `session` is already byte-exact, so this adds
    no safety. It exists so the audit records the facts an operator actually
    cares about - the device is now A/B, it will boot the slot that was just
    written, and the stored preferences were carried across - instead of
    leaving them to be re-derived from one 8 MiB hash.
    """
    identify_partition_binary(after[TABLE_REGION[0]:TABLE_REGION[0] + 0xc00], expect='ab')
    start, length = OTADATA_REGION
    if after[start:start + length] != b'\xff' * length:
        raise RuntimeError('otadata is not blank; the bootloader could select an unwritten slot')
    # The font is deliberately excluded: this tool rewrites it in the same
    # session, so it is covered by the exact full-image comparison instead.
    carried = {'nvs': (0x9000, 0x6000), 'phy_init': (0xf000, 0x1000)}
    for name, (offset, size) in carried.items():
        if after[offset:offset + size] != before[offset:offset + size]:
            raise RuntimeError(name + ' changed during migration')
    return {'layout': 'ab', 'boot_slot': 'ota_0', 'otadata': 'blank', 'carried_over': sorted(carried)}


# durable_new and operation_timeout now live in tools/_mixlib; see the imports
# at the top of this file. They were duplicated across three tools, and the
# copies used os.O_DIRECTORY and signal.SIGALRM unconditionally, which made
# every module that imported them unusable off Linux.


def partition_payload(data, size, what, magic=None):
    """Pad an already validated image out to the whole partition it lands in."""
    if not 32 <= len(data) <= size or (magic is not None and data[0] != magic):
        raise ValueError('Invalid bounded ' + what + ' payload')
    return data + b'\xff' * (size - len(data))


def write_regions(payload, table, new_app=None, bootloader=None):
    """Plan every whole-partition write, in the exact order it is committed.

    `table` is the staged binary partition table, and it alone decides how much
    room the application has: 0x200000 on the historic factory-only layout, but
    only 0x1F0000 on A/B, where otadata starts immediately afterwards. Deriving
    the size instead of assuming the old one is what stops an app write from
    silently erasing otadata once the device has been migrated.

    Passing `bootloader` additionally plans the one-time migration to the A/B
    layout. Its three extra regions come last and in this order so the device
    stays bootable for as long as possible:

      * otadata is blanked first. It is dead space inside the tail of the old
        factory partition, and all-0xFF is exactly what makes the bootloader
        boot ota_0 - the app just written - and record ota_seq=1 by itself.
      * the partition table is the single write that switches the device over.
      * the bootloader is last, because it is the only region whose failure
        needs hardware BOOT+RESET recovery. Without it the device is already
        bootable and already A/B; it only adds automatic rollback.

    nvs, phy_init and font are never planned by the migration at all: both
    layouts place them at identical offsets and sizes, so the stored
    preferences and the 4 MiB font survive untouched.
    """
    if len(payload) != FONT_SIZE:
        raise ValueError('Font payload must fill exactly its partition')
    layout = validate_table(table)
    regions = []
    if new_app is not None:
        app_size = AB_APP_SIZE if layout['name'] == 'ab' else LEGACY_APP_SIZE
        regions.append((APP_OFFSET, partition_payload(new_app, app_size, 'application', 0xe9)))
    regions.append((FONT_OFFSET, payload))
    if bootloader is not None:
        if layout['name'] != 'ab' or new_app is None:
            raise ValueError('Migration needs the new application and the staged A/B partition table')
        regions.append((OTADATA_REGION[0], b'\xff' * OTADATA_REGION[1]))
        regions.append((TABLE_REGION[0], partition_payload(table, TABLE_REGION[1], 'partition table')))
        regions.append((BOOTLOADER_REGION[0],
                        partition_payload(bootloader, BOOTLOADER_REGION[1], 'bootloader', 0xe9)))
    if any(offset % SECTOR or len(data) % SECTOR for offset, data in regions):
        raise ValueError('Planned regions are not whole flash sectors')
    return regions


def flash_block_once(chip, data, seq):
    # esptool 5.4 flash_block() silently retries FatalError.
    # Use its exact wire encoding, but submit the write command only once.
    chip.check_command('write to target Flash after seq %d' % seq, chip.ESP_CMDS['FLASH_DATA'],
                       struct.pack('<IIII', len(data), seq, 0, 0) + data,
                       chip.checksum(data), timeout=30)


def session(dev, identity, payload, table, app, new_app=None, bootloader=None, reset_rom=False):
    sys.path.insert(0, esp.ESP_ENV['PYTHONPATH'])
    import esptool
    from esptool.loader import StubFlasher
    StubFlasher.STUB_SUBDIRS = ['2']  # Never fall back to the legacy stub.
    if esptool.__version__ != ESPTOOL_VERSION:
        raise ValueError('Only reviewed esptool ' + ESPTOOL_VERSION + ' direct APIs are supported')
    migrate = bootloader is not None
    regions = write_regions(payload, table, new_app, bootloader)
    esp.idle_port(dev)
    with operation_timeout('ROM connection', 60):
        from esptool.cmds import connect_esp
        chip = connect_esp(port=dev, connect_attempts=3, open_port_attempts=1,
                           initial_baud=115200, chip='esp32s3',
                           before='usb-reset' if reset_rom else 'no-reset')
    if chip is None:
        raise RuntimeError('ROM connection failed; no write attempted')
    try:
        configure_port(chip)
        if getattr(chip, 'sync_stub_detected', False) or getattr(chip, 'IS_STUB', False):
            raise RuntimeError('An existing unidentified stub is running; fresh ROM required')
        if physical_identity(dev) != identity:
            raise RuntimeError('ROM identity changed during connect')
        security = chip.get_security_info()
        esp.validate_security(f"Flags: {security['flags']:#x}")
        mac = ':'.join(f'{b:02x}' for b in chip.read_mac())
        if security['chip_id'] != 9 or security['flash_crypt_cnt'] or mac != identity['serial'].lower():
            raise ValueError('Unexpected chip/security/MAC')
        esp.audit('font_rom_security_verified', mac=mac, **security)
        esp.audit('font_stub_start', serial_timeout_seconds=PORT_TIMEOUT)
        original_port = chip._port
        with operation_timeout('stub upload', 60):
            stub = chip.run_stub()
        if stub._port is not original_port or not stub.IS_STUB:
            if stub._port is not original_port:
                stub._port.close()
            raise RuntimeError('Fresh stub did not retain the verified serial handle')
        chip = stub
        configure_port(chip)  # The stub wraps the same handle; keep stream reads finite.
        esp.audit('font_stub_ready', version=esptool.__version__)
        chip.change_baud(460800)
        esp.audit('font_spi_attach_start')
        chip.flash_spi_attach(0)  # Same default attach as esptool CLI --no-stub.
        esp.audit('font_spi_attached')
        flash_id = chip.flash_id()
        if (flash_id >> 16) & 0xff != 0x17:
            raise ValueError('JEDEC flash capacity is not 8 MiB')
        esp.audit('font_flash_identified', flash_id=flash_id, bytes=FLASH_SIZE)
        chip.flash_set_parameters(FLASH_SIZE)
        esp.audit('font_flash_parameters_ready')
        before = read_full_flash(chip, 'backup')
        validate_backup(before, table, app, migrate)
        backup = esp.ROOT / 'original-flash-8MB.bin'
        durable_new(backup, before)
        durable_new(esp.ROOT / 'original-flash-8MB.bin.sha256',
                    (sha(before) + '  original-flash-8MB.bin\n').encode())
        esp.audit('font_backup_verified', file=str(backup), bytes=len(before), sha256=sha(before), flash_id=flash_id)
        if physical_identity(dev) != identity:
            raise RuntimeError('ROM identity changed before write')
        if not chip.IS_STUB or chip.FLASH_WRITE_SIZE not in (0x800, 0x4000):
            raise ValueError('Unexpected ESP32-S3 stub/write geometry')
        if any(len(data) % chip.FLASH_WRITE_SIZE for _, data in regions):
            # Every region is a whole number of 4 KiB sectors, so this can only
            # trip on the migration's one-sector table and two-sector otadata
            # under a 0x4000 stub. Refuse rather than pad: padding a 0x1000
            # table write out to 0x4000 would run straight into nvs at 0x9000.
            raise ValueError('Unexpected write geometry for this block size; '
                             'the migration needs the 0x800 USB-OTG stub geometry')
        durable_new(esp.ROOT / 'write-claim.json', json.dumps([
            {'offset': offset, 'bytes': len(data), 'sha256': sha(data)} for offset, data in regions]).encode())
        expected = bytearray(before)
        for address, data in regions:
            esp.audit('display_write_start', offset=hex(address), bytes=len(data), sha256=sha(data))
            # Only whole, sector-aligned, validated app/font partitions. Avoid
            # FLASH_END before readback; retain the same stub and USB handle.
            chip.flash_begin(len(data), address)
            for seq, offset in enumerate(range(0, len(data), chip.FLASH_WRITE_SIZE)):
                flash_block_once(chip, data[offset:offset + chip.FLASH_WRITE_SIZE], seq)
                if seq % 32 == 0:
                    print(f'Write {address:#x}: {offset}/{len(data)}', flush=True)
            chip.read_reg(esptool.ESPLoader.CHIP_DETECT_MAGIC_REG_ADDR, timeout=30)
            if chip.flash_md5sum(address, len(data)) != hashlib.md5(data).hexdigest():
                raise RuntimeError('Chip-side hash mismatch; no automatic retry')
            esp.audit('display_chip_hash_verified', offset=hex(address))
            expected[address:address + len(data)] = data
        after = read_full_flash(chip, 'readback')
        durable_new(esp.ROOT / 'flash-readback-8MB.bin', after)
        durable_new(esp.ROOT / 'flash-readback-8MB.bin.sha256',
                    (sha(after) + '  flash-readback-8MB.bin\n').encode())
        if after != expected:
            raise RuntimeError('Full-flash readback differs from exact expected image; preserve backup and inspect')
        migrated = verify_migrated(after, before) if migrate else None
        esp.audit('display_readback_verified', font_sha256=sha(payload), full_flash_sha256=sha(after),
                  old_app_sha256=sha(app), new_app_sha256=sha(new_app) if new_app else None,
                  app_updated=new_app is not None, all_other_flash_unchanged=True,
                  migrated_to_ab=migrated)
        chip.hard_reset()
    finally:
        chip._port.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--sha256', required=True)
    parser.add_argument('--app-sha256', required=True, help='Exact currently deployed app hash')
    parser.add_argument('--new-app-sha256', help='Explicitly replace app too, using validated new-app.bin')
    parser.add_argument('--partition-sha256', required=True)
    parser.add_argument('--serial', default='TD0720')
    parser.add_argument('--rom-serial', default='70:04:1d:d8:54:14')
    parser.add_argument('--location', default='5-1.2')
    parser.add_argument('--execute', action='store_true')
    parser.add_argument('--workdir', type=Path, help='New exclusive artifact directory outside the immutable package')
    parser.add_argument('--resume-no-write-source', type=Path,
                        help='Explicit prior immutable package work/session; consumes its no-write consent once')
    parser.add_argument('--migrate', action='store_true',
                        help='One-time move from the factory-only layout to A/B, so later updates go over USB')
    parser.add_argument('--bootloader-sha256', help='Required with --migrate: hash of the staged bootloader.bin')
    args = parser.parse_args()
    if args.location != '5-1.2':
        raise ValueError('Only the corroborated ESP physical path 5-1.2 is supported')
    if args.migrate and not (args.bootloader_sha256 and args.new_app_sha256):
        raise ValueError('--migrate requires --bootloader-sha256 and --new-app-sha256')
    if args.bootloader_sha256 and not args.migrate:
        raise ValueError('--bootloader-sha256 is only meaningful with --migrate')
    font = (ROOT / 'font.ttf').read_bytes()
    payload = validate_font(font, args.sha256)
    app_path = ROOT / 'firmware/esp32s3/build/mixos_esp32s3.bin'
    validate_image(app_path, args.app_sha256, 'esp32s3', validate_partitions(ROOT / 'firmware/esp32s3/partitions.csv'))
    app = app_path.read_bytes()
    if sha(app) != args.app_sha256.lower():
        raise ValueError('Old app changed after validation')
    new_app = None
    if args.new_app_sha256:
        new_path = ROOT / 'new-app.bin'
        validate_image(new_path, args.new_app_sha256, 'esp32s3',
                       validate_partitions(ROOT / 'firmware/esp32s3/partitions.csv'))
        new_app = new_path.read_bytes()
        if sha(new_app) != args.new_app_sha256.lower():
            raise ValueError('New app changed after validation')
    table = (ROOT / 'partition-table.bin').read_bytes()
    if len(table) != 0xc00 or sha(table) != args.partition_sha256.lower():
        raise ValueError('Staged partition-table hash/size mismatch')
    validate_table(table)
    bootloader = None
    if args.migrate:
        bootloader = (ROOT / 'bootloader.bin').read_bytes()
        if sha(bootloader) != args.bootloader_sha256.lower():
            raise ValueError('Staged bootloader hash mismatch')
        # Plan the regions before anything is opened, so a table, app or
        # bootloader that cannot be migrated fails here rather than with a ROM
        # already in download mode.
        write_regions(payload, table, new_app, bootloader)
    if not args.execute:
        print(json.dumps({'dry_run': True, 'font_bytes': len(font), 'sha256': sha(font),
                          'offset': hex(FONT_OFFSET), 'partition_bytes': FONT_SIZE,
                          'app_update': sha(new_app) if new_app else None,
                          'migrate_to_ab': sha(bootloader) if bootloader else None}))
        return
    if sys.platform != 'linux' or getattr(os, 'geteuid', lambda: 0)() == 0:
        raise RuntimeError('Run as an ordinary Linux dialout user')
    if args.workdir is None or not args.workdir.is_absolute():
        raise ValueError('--execute requires a new absolute --workdir')
    if args.workdir.parent.resolve(strict=True) != args.workdir.parent:
        raise ValueError('Workdir parent must be canonical and existing')
    if not shutil.which('fuser') or shutil.disk_usage(args.workdir.parent).free < 64 * 1024 * 1024:
        raise RuntimeError('Missing fuser or insufficient backup space')
    service = subprocess.run(['systemctl', 'is-active', 'mixosd.service'], capture_output=True, text=True)
    if service.stdout.strip() not in ('inactive', 'failed'):
        raise RuntimeError('Stop mixosd before font maintenance')
    cache = Path.home() / '.cache/mixos'
    cache.mkdir(parents=True, exist_ok=True)
    with device_lock(cache / 'flash.lock'):
        args.workdir.mkdir(mode=0o700, exist_ok=False)
        sync_directory(args.workdir.parent)
        esp.ROOT = args.workdir  # Shared helper logs/vendor files must never alter immutable code.
        durable_new(esp.ROOT / 'font-job-claim.json', b'No automatic retry; inspect audit before recovery.\n')
        reset_rom = False
        esp.audit('display_host_execute_authorized', screen_confirmation_required=False,
                  font_sha256=sha(font), old_app_sha256=sha(app),
                  new_app_sha256=sha(new_app) if new_app else None,
                  migrate_to_ab=sha(bootloader) if bootloader else None,
                  rom_mac=args.rom_serial.lower(), location=args.location)
        if args.resume_no_write_source:
            requested = {'font.ttf': sha(font), 'firmware/esp32s3/build/mixos_esp32s3.bin': sha(app),
                         'partition-table.bin': sha(table),
                         'firmware/esp32s3/partitions.csv': sha((ROOT / 'firmware/esp32s3/partitions.csv').read_bytes())}
            if new_app is not None:
                requested['new-app.bin'] = sha(new_app)
            if bootloader is not None:
                requested['bootloader.bin'] = sha(bootloader)
            rom, rom_ident = claim_resume(args.resume_no_write_source, requested, args.serial,
                                         args.location, args.rom_serial)
            prepare_esptool(ROOT / 'esptool.whl')
        else:
            dev, ident, is_rom = select_direct_device(args.serial, args.location, args.rom_serial)
            esp.audit('font_preflight_ok', identity=ident, font_sha256=sha(font), app_sha256=sha(app),
                      new_app_sha256=sha(new_app) if new_app else None)
            prepare_esptool(ROOT / 'esptool.whl')
            if is_rom:
                rom, rom_ident = dev, ident
                # A direct ROM session must already be fresh: session rejects
                # any running stub instead of resetting or silently reusing it.
                esp.audit('display_direct_rom_selected', identity=ident, usb_reset_before_connect=False)
            else:
                enter_download_direct(dev, ident)
                rom, rom_ident = esp.wait_port(args.location, rom=True)
        if rom_ident['serial'].lower() != args.rom_serial.lower():
            raise ValueError('Unexpected ROM serial; nothing written')
        session(rom, rom_ident, payload, table, app, new_app, bootloader, reset_rom=reset_rom)
        dev, ident = esp.wait_port(args.location, rom=False, serial=args.serial, timeout=40)
        result = esp.verify_running(dev, ident)
        esp.audit('display_flash_and_boot_verified', identity=ident, app_updated=new_app is not None,
                  migrated_to_ab=bootloader is not None, **result)
        print('SUCCESS: requested app/font readback verified; all other flash unchanged; MixOS heartbeat verified.', flush=True)
        if bootloader is not None:
            print('MIGRATED: the device now runs the A/B layout with nvs and the font carried over. '
                  'Later firmware updates can go over USB with tools/ota_esp.py instead of ROM download mode.',
                  flush=True)


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print('STOP: ' + str(exc), file=sys.stderr, flush=True)
        try:
            esp.audit('font_aborted', reason=str(exc), note='No automatic write retry. Inspect backup and audit before recovery.')
        except OSError:
            pass  # Preflight may fail before a writable workdir exists.
        sys.exit(1)
