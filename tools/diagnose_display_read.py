#!/usr/bin/env python3
"""One bounded read-only diagnostic; never erases/programs flash or retries reads."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _mixlib.guards import device_lock
import display_transport as transport


def identity(device):
    name = Path(device).resolve(strict=True).name
    node = (Path('/sys/class/tty') / name / 'device').resolve(strict=True)
    for p in (node, *node.parents):
        if (p / 'idVendor').exists():
            return {k: (p / f).read_text().strip() for k, f in
                    [('vid', 'idVendor'), ('pid', 'idProduct'), ('serial', 'serial')]} | {'location': p.name}
    raise ValueError('Not a USB serial device')


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--package', required=True, type=Path)
    p.add_argument('--workdir', required=True, type=Path)
    p.add_argument('--execute-read', action='store_true')
    p.add_argument('--before', choices=['usb-reset', 'no-reset'], default='no-reset')
    a = p.parse_args()
    if not a.execute_read:
        print('DRY RUN: 256KiB read only; no port opened')
        return
    # os.geteuid does not exist on Windows; the attribute lookup used to raise
    # AttributeError instead of the clear refusal below.
    if getattr(os, 'geteuid', lambda: 0)() == 0:
        raise ValueError('Run as ordinary user')
    a.workdir.mkdir(mode=0o700, exist_ok=False)
    cache = Path.home() / '.cache/mixos'
    cache.mkdir(parents=True, exist_ok=True)
    def record(event, **data):
        row = dict(event=event, time=time.time(), **data)
        with (a.workdir / 'audit.jsonl').open('a') as out:
            out.write(json.dumps(row) + '\n'); out.flush(); os.fsync(out.fileno())
        print(json.dumps(row), flush=True)
    def expired(*_):
        raise TimeoutError('Bounded diagnostic deadline; no retry')
    signal.signal(signal.SIGALRM, expired)
    with device_lock(cache / 'flash.lock'):
        env = transport.prepare(a.package, a.workdir / '.esptool')
        sys.path.insert(0, env['PYTHONPATH'])
        import esptool
        from esptool.cmds import connect_esp
        assert esptool.__version__ == transport.VERSION
        dev = '/dev/ttyACM0'
        before = identity(dev)
        if (before['vid'] != '303a' or before['pid'] not in ('0009', '1001')
                or before['serial'].lower() != '70:04:1d:d8:54:14' or before['location'] != '5-1.2'):
            raise ValueError('Unexpected ROM identity')
        busy = subprocess.run(['fuser', dev], capture_output=True, text=True, timeout=5)
        if busy.returncode != 1 or busy.stdout.strip():
            raise ValueError('Serial port busy or check failed')
        record('read_only_authorized', identity=before, before=a.before, bytes=0x40000)
        chip = None
        try:
            signal.setitimer(signal.ITIMER_REAL, 60)
            chip = connect_esp(port=dev, chip='esp32s3', before=a.before,
                               connect_attempts=1, open_port_attempts=1, initial_baud=115200)
            chip._port.timeout = chip._port.write_timeout = 10
            if identity(dev) != before or chip.sync_stub_detected:
                raise ValueError('Fresh identical ROM required; existing unknown stub rejected')
            security = chip.get_security_info()
            mac = ':'.join(f'{b:02x}' for b in chip.read_mac())
            if security['flags'] != 0 or security['chip_id'] != 9 or security['flash_crypt_cnt'] != 0 or mac != before['serial'].lower():
                raise ValueError('Unexpected chip/security state')
            from esptool.loader import StubFlasher
            stub = StubFlasher(chip)
            record('fresh_stub_upload', version=esptool.__version__, text_sha256=hashlib.sha256(stub.text).hexdigest())
            chip = chip.run_stub(stub)
            chip._port.timeout = chip._port.write_timeout = 10
            chip.flash_spi_attach(0)
            fid = chip.flash_id()
            if (fid >> 16) & 255 != 0x17:
                raise ValueError('Unexpected flash capacity')
            chip.flash_set_parameters(0x800000)
            signal.setitimer(signal.ITIMER_REAL, 60)
            frames=[]; start=time.monotonic()
            def progress(done, total, offset):
                frames.append({'done':done, 'total':total, 'offset':offset,
                               'elapsed':round(time.monotonic()-start, 6)})
            record('read_start', offset=0, bytes=0x40000)
            data=chip.read_flash(0,0x40000,progress_fn=progress)
            if len(data)!=0x40000:raise ValueError('Unexpected read size')
            with (a.workdir/'read-256KiB.bin').open('xb') as out:
                out.write(data);out.flush();os.fsync(out.fileno())
            record('read_verified', bytes=len(data), sha256=hashlib.sha256(data).hexdigest(),
                   seconds=time.monotonic()-start, frames=frames)
            # No reset or programming after this read; preserve the known stub.
        except Exception as exc:
            record('read_aborted', reason=str(exc), no_flash_writes=True)
            raise
        finally:
            signal.setitimer(signal.ITIMER_REAL,0)
            if chip is not None:chip._port.close()


if __name__ == '__main__':
    main()
