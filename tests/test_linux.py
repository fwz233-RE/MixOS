"""Portable state tests plus real PTY tests, explicitly skipped on Windows."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import selectors
import struct
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'linux'))
from protocol import Frame, Decoder, Channel as C, Type as T
from mixosd import Link, WriteQueue, QueueFull, PtyShell, HashJob, HostMetrics, open_serial
import headless

spec = importlib.util.spec_from_file_location('update_esp', ROOT / 'tools' / 'update_esp.py')
update = importlib.util.module_from_spec(spec)
spec.loader.exec_module(update)


class FakeShell:
    def __init__(self, cols, rows):
        self.dimensions = cols, rows
        self.closed = False

    def resize(self, cols, rows):
        self.dimensions = cols, rows

    def close(self):
        self.closed = True


def drain(queue):
    data = bytearray()
    def write(part):
        data.extend(part)
        return len(part)
    while queue.size:
        queue.flush(write)
    return list(Decoder().feed(data))


class QueueTests(unittest.TestCase):
    def test_partial_blocked_writes(self):
        queue = WriteQueue(12)
        queue.put(b'abcdef')
        queue.put(b'ghijkl')
        with self.assertRaises(QueueFull):
            queue.put(b'x')
        result = bytearray()
        calls = 0
        def write(data):
            nonlocal calls
            calls += 1
            if calls % 3 == 0:
                raise BlockingIOError()
            result.extend(data[:2])
            return min(2, len(data))
        while queue.size:
            queue.flush(write, 3)
        self.assertEqual(result, b'abcdefghijkl')
        self.assertEqual(queue.size, 0)

    def test_zero_write_disconnect(self):
        queue = WriteQueue()
        queue.put(b'data')
        with self.assertRaises(OSError):
            queue.flush(lambda _: 0)
        self.assertEqual(queue.size, 4)


class LinkTests(unittest.TestCase):
    def setUp(self):
        self.now = 0.0
        self.link = Link(FakeShell, clock=lambda: self.now)
        self.seq = 1
        self.epoch = 123
        self.send(C.CONTROL, T.HELLO, payload=struct.pack('<HH', 512, 4096))
        self.assertEqual(drain(self.link.tx)[0].type, T.HELLO_ACK)

    def send(self, channel, kind, session=0, payload=b''):
        frame = Frame(channel, kind, self.epoch, session, self.seq, payload)
        self.seq += 1
        self.link.feed(frame.encode())
        return frame

    def open(self):
        self.send(C.TERMINAL, T.OPEN, 7, struct.pack('<HH', 80, 28))
        self.assertEqual(drain(self.link.tx)[0].type, T.OPENED)

    def grant(self, amount):
        self.send(C.TERMINAL, T.CREDIT, 7, struct.pack('<I', amount))

    def test_session_and_stale_input(self):
        self.open()
        frame = self.send(C.TERMINAL, T.INPUT, 7, b'echo safe\n')
        self.link.feed(frame.encode())
        self.assertEqual(self.link.input.size, len(b'echo safe\n'))
        self.send(C.TERMINAL, T.INPUT, 9, b'wrong session')
        self.assertEqual(self.link.input.size, len(b'echo safe\n'))
        self.assertEqual(drain(self.link.tx)[0].type, T.ERROR)

    def test_epoch_clears_everything(self):
        self.open()
        shell = self.link.shell
        self.grant(4096)
        self.link.output(b'old output')
        self.send(C.TERMINAL, T.INPUT, 7, b'old input')
        self.send(C.JOB, T.JOB_START, 5, b'sha256')
        self.epoch = 456
        self.send(C.CONTROL, T.HELLO, payload=struct.pack('<HH', 512, 4096))
        self.assertTrue(shell.closed)
        self.assertIsNone(self.link.job)
        self.assertEqual(self.link.input.size, 0)
        self.assertEqual(self.link.credit.available, 0)
        self.assertEqual([f.type for f in drain(self.link.tx)], [T.HELLO_ACK])
        self.link.feed(Frame(C.TERMINAL, T.INPUT, 123, 7, 100, b'old').encode())
        self.assertEqual(self.link.input.size, 0)

    def test_new_epoch_resynchronizes_partial_tx(self):
        self.open()
        self.grant(4096)
        self.link.output(b'old data')
        wire = bytearray()
        self.link.tx.flush(lambda data: (wire.extend(data), len(data))[1], budget=5)
        receiver = Decoder()
        self.assertEqual(list(receiver.feed(wire)), [])
        self.epoch = 456
        self.send(C.CONTROL, T.HELLO, payload=struct.pack('<HH', 512, 4096))
        wire.clear()
        self.link.tx.flush(lambda data: (wire.extend(data), len(data))[1])
        frames = list(receiver.feed(wire))
        self.assertEqual([frame.type for frame in frames], [T.HELLO_ACK])
        self.assertEqual(frames[0].epoch, 456)

    def test_hello_retry_keeps_session(self):
        self.open()
        shell = self.link.shell
        self.send(C.CONTROL, T.HELLO, payload=struct.pack('<HH', 512, 4096))
        self.assertIs(self.link.shell, shell)

    def test_overflow_explicitly_closes(self):
        self.open()
        shell = self.link.shell
        for _ in range(9):
            self.send(C.TERMINAL, T.INPUT, 7, b'x' * 512)
        self.assertTrue(shell.closed)
        self.assertEqual(self.link.input.size, 0)
        self.assertEqual([f.type for f in drain(self.link.tx)], [T.ERROR, T.CLOSE])

    def test_output_flood_and_absolute_credit(self):
        self.open()
        self.assertEqual(self.link.read_budget, 0)
        self.grant(4096)
        for _ in range(8):
            self.link.output(b'x' * 512)
        self.assertEqual(self.link.read_budget, 0)
        self.grant(4096)
        self.assertEqual(self.link.read_budget, 0)
        with self.assertRaises(ValueError):
            self.link.output(b'x')
        frames = drain(self.link.tx)
        self.assertEqual(sum(len(f.payload) for f in frames), 4096)
        total = 4096
        for _ in range(1000):
            self.grant(total + 4096)
            while self.link.read_budget:
                count = self.link.read_budget
                self.link.output(b'f' * count)
                total += count
            self.assertLessEqual(self.link.tx.size, 16384)
            if self.link.tx.size >= 8192:
                break
        else:
            self.fail('flood never hit bounded TX backpressure')
        self.send(C.CONTROL, T.PING)
        self.assertEqual(drain(self.link.tx)[-1].type, T.PONG)

    def test_heartbeat_and_disconnect(self):
        self.open()
        shell = self.link.shell
        self.now = 2
        self.link.tick()
        self.assertEqual(drain(self.link.tx)[0].type, T.PING)
        self.now = 8
        self.link.tick()
        self.assertTrue(shell.closed)
        self.assertEqual(self.link.rx.epoch, 0)
        self.assertEqual(self.link.tx.size, 0)

    def test_resize_close_exit(self):
        self.open()
        self.send(C.TERMINAL, T.RESIZE, 7, struct.pack('<HH', 100, 30))
        self.assertEqual(self.link.shell.dimensions, (100, 30))
        self.link.exited(3)
        self.assertEqual(struct.unpack('<i', drain(self.link.tx)[0].payload), (3,))
        self.assertEqual(self.link.session, 0)

    def test_magic_is_plain_input_and_maintenance_denied(self):
        self.open()
        magic = b'REBOOT_TO_BOOT_MODE'
        self.send(C.TERMINAL, T.INPUT, 7, magic)
        self.assertEqual(b''.join(self.link.input.parts), magic)
        self.send(C.MAINTENANCE, T.PREPARE_UPDATE, 1)
        self.assertEqual(drain(self.link.tx)[0].type, T.ERROR)
        self.assertIsNotNone(self.link.shell)

    def test_fixed_hash_progress_cancel_and_complete(self):
        self.send(C.JOB, T.JOB_START, 5, b'sha256;rm -rf /')
        self.assertIsNone(self.link.job)
        drain(self.link.tx)
        self.send(C.JOB, T.JOB_START, 5, b'sha256')
        self.link.tick()
        self.assertEqual(drain(self.link.tx)[0].type, T.JOB_PROGRESS)
        self.send(C.JOB, T.JOB_CANCEL, 5)
        self.assertTrue(json.loads(drain(self.link.tx)[0].payload)['cancelled'])
        self.send(C.JOB, T.JOB_START, 6, b'sha256')
        frames = []
        for _ in range(HashJob.STEPS):
            self.link.tick()
            frames.extend(drain(self.link.tx))
        result = json.loads(frames[-1].payload)
        expected = hashlib.sha256()
        for _ in range(HashJob.STEPS):
            expected.update(HashJob.BLOCK)
        self.assertEqual(result['sha256'], expected.hexdigest())
        self.assertEqual(result['bytes'], 64 * 1024 * 1024)
        self.assertEqual(json.loads(frames[-2].payload)['percent'], 100)
        self.assertIsNone(self.link.job)

    def test_root_entrypoint_refuses_before_serial(self):
        import mixosd
        with patch.object(mixosd.sys, 'platform', 'linux'), \
                patch.object(mixosd.os, 'geteuid', return_value=0, create=True), \
                patch.object(mixosd, 'serve') as serve:
            with self.assertRaises(SystemExit):
                mixosd.main(['--device', '/dev/serial/by-id/EXAMPLE'])
            serve.assert_not_called()

    def test_missing_metrics_are_null(self):
        with patch.object(Path, 'read_text', side_effect=OSError('unavailable')):
            self.assertTrue(all(v is None for v in HostMetrics().sample().values()))


class MaintenanceTests(unittest.TestCase):
    def setUp(self):
        self.now = 0
        self.state = update.Maintenance(clock=lambda: self.now)

    def test_wait_for_esp_hello_not_stale_ping(self):
        self.state.feed(Frame(C.CONTROL, T.PING, 41, sequence=20).encode())
        self.assertEqual(self.state.tx.size, 0)
        self.state.feed(Frame(C.CONTROL, T.HELLO, 42, sequence=1,
                              payload=struct.pack('<HH', 512, 4096)).encode())
        self.assertEqual([f.type for f in drain(self.state.tx)], [T.HELLO_ACK, T.PREPARE_UPDATE])
        self.state.feed(Frame(C.CONTROL, T.PING, 42, sequence=2).encode())
        self.assertEqual(drain(self.state.tx)[0].type, T.PONG)

    def test_local_grant_request_epoch_expiry(self):
        self.test_wait_for_esp_hello_not_stale_ping()
        with self.assertRaises(ValueError):
            self.state.enter_boot()
        self.state.feed(Frame(C.MAINTENANCE, T.UPDATE_READY, 42,
                              self.state.request ^ 1, 3).encode())
        self.assertFalse(self.state.ready)
        frame = Frame(C.MAINTENANCE, T.UPDATE_READY, 42, self.state.request, 4)
        self.state.feed(frame.encode())
        self.assertTrue(self.state.ready)
        self.now = 15
        with self.assertRaises(ValueError):
            self.state.enter_boot()
        self.now = 1
        self.state.enter_boot()
        self.assertEqual(drain(self.state.tx)[0].type, T.ENTER_BOOT)
        with self.assertRaises(ValueError):
            self.state.enter_boot()
        with self.assertRaises(ValueError):
            self.state.feed(Frame(C.CONTROL, T.HELLO, 43, sequence=1,
                                  payload=struct.pack('<HH', 512, 4096)).encode())

    def test_magic_cannot_grant(self):
        self.test_wait_for_esp_hello_not_stale_ping()
        self.state.feed(Frame(C.TERMINAL, T.DATA, 42, self.state.request, 3,
                              b'REBOOT_TO_BOOT_MODE').encode())
        self.assertFalse(self.state.ready)


class UpdatePreflightTests(unittest.TestCase):
    def make_image(self):
        header = bytearray(24)
        header[0:2] = bytes([0xE9, 1])
        header[12:14] = struct.pack('<H', 9)
        header[23] = 1
        payload = b'test app'
        raw = header + struct.pack('<II', 0x3C000020, len(payload)) + payload
        checksum = 0xEF
        for b in payload:
            checksum ^= b
        raw.extend(b'\0' * ((len(raw) | 15) - len(raw)))
        raw.append(checksum)
        return bytes(raw) + hashlib.sha256(raw).digest()

    def test_image_hash_chip_layout_rejections(self):
        partition = update.validate_partitions(ROOT / 'firmware' / 'esp32s3' / 'partitions.csv')
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'app.bin'
            image = self.make_image()
            path.write_bytes(image)
            digest = hashlib.sha256(image).hexdigest()
            self.assertEqual(update.validate_image(path, digest, 'esp32s3', partition)['offset'], 0x10000)
            with self.assertRaises(ValueError):
                update.validate_image(path, '0' * 64, 'esp32s3', partition)
            for offset, value in [(12, 0), (1, 17), (32, 0), (len(image) - 1, 0)]:
                bad = bytearray(image)
                bad[offset] = value
                path.write_bytes(bad)
                with self.assertRaises(ValueError):
                    update.validate_image(path, hashlib.sha256(bad).hexdigest(), 'esp32s3', partition)
            table = Path(temp) / 'partitions.csv'
            table.write_text('factory,app,factory,0x10000,0x300000\n')
            with self.assertRaises(ValueError):
                update.validate_partitions(table)

    def test_dry_run_never_opens_device_and_flash_refused(self):
        with tempfile.TemporaryDirectory() as temp:
            image = self.make_image()
            path = Path(temp) / 'app.bin'
            path.write_bytes(image)
            args = ['--device', '/dev/serial/by-id/EXAMPLE', '--vid', '303a', '--pid', '4001',
                    '--serial', 'EXAMPLE', '--location', '1-2', '--image', str(path),
                    '--sha256', hashlib.sha256(image).hexdigest(), '--chip', 'esp32s3',
                    '--partitions', str(ROOT / 'firmware/esp32s3/partitions.csv')]
            with patch.object(update, 'open_serial', side_effect=AssertionError('hardware touched')):
                self.assertEqual(update.main(args), 0)
                self.assertEqual(update.main(args + ['--execute', '--audit', str(Path(temp) / 'audit.jsonl')]), 2)


class HeadlessTests(unittest.TestCase):
    def test_dry_run_backup_restore_and_conflict(self):
        with tempfile.TemporaryDirectory() as temp:
            backup = Path(temp) / 'headless.json'
            with patch.object(headless.sys, 'platform', 'linux'), \
                    patch.object(headless, 'current_target', return_value='graphical.target'), \
                    patch.object(headless.subprocess, 'run') as run:
                self.assertEqual(headless.main(['enable', '--backup', str(backup)]), 0)
                run.assert_not_called()
                self.assertFalse(backup.exists())
                with patch.object(headless.os, 'geteuid', return_value=0, create=True):
                    self.assertEqual(headless.main(['enable', '--backup', str(backup),
                                                   '--execute', '--confirm', 'multi-user.target']), 0)
                self.assertEqual(json.loads(backup.read_text())['original_target'], 'graphical.target')
                run.assert_called_once_with(['systemctl', 'set-default', 'multi-user.target'], check=True)
            with patch.object(headless.sys, 'platform', 'linux'), \
                    patch.object(headless, 'current_target', return_value='multi-user.target'), \
                    patch.object(headless.os, 'geteuid', return_value=0, create=True), \
                    patch.object(headless.subprocess, 'run') as run:
                self.assertEqual(headless.main(['restore', '--backup', str(backup),
                                               '--execute', '--confirm', 'graphical.target']), 0)
                self.assertTrue(backup.exists())
                run.assert_called_once_with(['systemctl', 'set-default', 'graphical.target'], check=True)
            with patch.object(headless.sys, 'platform', 'linux'), \
                    patch.object(headless, 'current_target', return_value='graphical.target'):
                with self.assertRaises(SystemExit):
                    headless.main(['restore', '--backup', str(backup)])


@unittest.skipUnless(sys.platform == 'linux', 'Linux-only PTY/raw serial tests; explicitly skipped on Windows')
class LinuxPtyTests(unittest.TestCase):
    @unittest.skipUnless(sys.platform == 'linux' and getattr(os, 'geteuid', lambda: 0)() != 0,
                         'end-to-end shell test requires an ordinary Linux user')
    def test_daemon_selectors_end_to_end_and_disconnect(self):
        import pty
        import subprocess
        master, slave = pty.openpty()
        device = os.ttyname(slave)
        env = dict(os.environ, PYTHONPATH=str(ROOT / 'linux'))
        process = subprocess.Popen([sys.executable, '-B', '-c',
                                    'import sys;from mixosd import serve;serve(sys.argv[1],"/bin/sh")', device],
                                   env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        decoder = Decoder()
        received = []
        seq = 1
        def send(kind, channel=C.CONTROL, session=0, payload=b''):
            nonlocal seq
            data = Frame(channel, kind, 1234, session, seq, payload).encode()
            seq += 1
            # Exercise fragmented transport writes, not only decoder units.
            for offset in range(0, len(data), 3):
                os.write(master, data[offset:offset + 3])
        def receive_until(predicate, timeout=3):
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                with selectors.DefaultSelector() as selector:
                    selector.register(master, selectors.EVENT_READ)
                    if selector.select(0.05):
                        for frame in decoder.feed(os.read(master, 4096)):
                            received.append(frame)
                            if frame.type == T.PING:
                                send(T.PONG)
                if predicate():
                    return True
            return False
        try:
            # Retry HELLO while the daemon opens and flushes the virtual serial port.
            for _ in range(10):
                send(T.HELLO, payload=struct.pack('<HH', 512, 4096))
                if receive_until(lambda: any(f.type == T.HELLO_ACK for f in received), 0.1):
                    break
            self.assertTrue(any(f.type == T.HELLO_ACK for f in received))
            send(T.OPEN, C.TERMINAL, 9, struct.pack('<HH', 80, 28))
            self.assertTrue(receive_until(lambda: any(f.type == T.OPENED for f in received)))
            send(T.CREDIT, C.TERMINAL, 9, struct.pack('<I', 4096))
            send(T.INPUT, C.TERMINAL, 9, b"printf 'MIXOS_E2E_%s\\n' PASS\n")
            self.assertTrue(receive_until(lambda: b'MIXOS_E2E_PASS' in
                                          b''.join(f.payload for f in received if f.type == T.DATA)))
            send(T.STATUS_REQUEST, C.STATUS)
            self.assertTrue(receive_until(lambda: any(f.type == T.STATUS for f in received)))
            send(T.PING)
            self.assertTrue(receive_until(lambda: any(f.type == T.PONG for f in received)))
            os.close(master)
            master = -1
            # A physical-loss analogue must leave the daemon alive to reconnect.
            self.assertIsNone(process.poll())
        finally:
            process.terminate()
            process.wait(timeout=4)
            if master >= 0:
                os.close(master)
            os.close(slave)

    def test_real_shell_input_resize_and_exit(self):
        shell = PtyShell(80, 28)
        output = bytearray()
        try:
            shell.resize(100, 30)
            os.write(shell.fd, b"printf 'MIXOS_PTY_%s\\n' OK\n")
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline and b'MIXOS_PTY_OK' not in output:
                with selectors.DefaultSelector() as sel:
                    sel.register(shell.fd, selectors.EVENT_READ)
                    if sel.select(0.1):
                        output.extend(os.read(shell.fd, 512))
            self.assertIn(b'MIXOS_PTY_OK', output)
            os.write(shell.fd, b'exit 7\n')
            while time.monotonic() < deadline and shell.poll() is None:
                time.sleep(0.01)
            self.assertEqual(shell.status, 7)
        finally:
            shell.close()
            PtyShell.reap()

    def test_pty_raw_serial_partial_io(self):
        import pty
        master, slave = pty.openpty()
        fd = None
        try:
            fd = open_serial(os.ttyname(slave))
            message = Frame(C.CONTROL, T.HELLO, 1, payload=struct.pack('<HH', 512, 4096)).encode()
            queue = WriteQueue()
            queue.put(message)
            while queue.size:
                queue.flush(lambda data: os.write(fd, data[:3]))
            with selectors.DefaultSelector() as sel:
                sel.register(master, selectors.EVENT_READ)
                self.assertTrue(sel.select(1))
            self.assertEqual(os.read(master, 4096), message)
        finally:
            if fd is not None:
                os.close(fd)
            os.close(master)
            os.close(slave)


if __name__ == '__main__':
    unittest.main()
