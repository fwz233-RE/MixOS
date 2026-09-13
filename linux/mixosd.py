#!/usr/bin/env python3
"""Unprivileged, single-threaded MixOS host. No third-party runtime dependency."""
import argparse
from collections import deque
import errno
import hashlib
import json
import os
from pathlib import Path
import selectors
import signal
import struct
import sys
import time

from protocol import Channel as C, Type as T, Frame, Decoder, ReceiveEpoch, Credit

TX_LIMIT = 16384
INPUT_LIMIT = 4096
DATA_HIGH_WATER = 8192


class QueueFull(Exception):
    pass


class WriteQueue:
    def __init__(self, limit=TX_LIMIT):
        self.parts = deque()
        self.size = 0
        self.limit = limit

    def put(self, data):
        if self.size + len(data) > self.limit:
            raise QueueFull('bounded queue exhausted')
        if data:
            self.parts.append(bytes(data))
            self.size += len(data)

    def flush(self, writer, budget=4096):
        while self.parts and budget > 0:
            part = self.parts[0]
            try:
                count = writer(part[:budget])
            except (BlockingIOError, InterruptedError):
                return
            if count <= 0:
                raise OSError('zero-length write/disconnected')
            if count > min(len(part), budget):
                raise OSError('invalid write result')
            budget -= count
            self.size -= count
            if count == len(part):
                self.parts.popleft()
            else:
                self.parts[0] = part[count:]


class HostMetrics:
    def __init__(self):
        self.previous = None

    def sample(self):
        result = dict(uptime_s=None, cpu_pct=None, mem_used_kib=None,
                      mem_total_kib=None, temp_c=None)
        try:
            result['uptime_s'] = float(Path('/proc/uptime').read_text().split()[0])
            ticks = list(map(int, Path('/proc/stat').read_text().splitlines()[0].split()[1:9]))
            total, idle = sum(ticks), ticks[3] + ticks[4]
            if self.previous:
                dt, di = total - self.previous[0], idle - self.previous[1]
                if dt > 0:
                    result['cpu_pct'] = round(max(0, min(100, 100 * (dt - di) / dt)), 2)
            self.previous = total, idle
            mem = {line.split(':')[0]: int(line.split()[1])
                   for line in Path('/proc/meminfo').read_text().splitlines()}
            result['mem_total_kib'] = mem['MemTotal']
            result['mem_used_kib'] = mem['MemTotal'] - mem['MemAvailable']
        except (OSError, ValueError, IndexError, KeyError):
            pass
        # Temperatures are unknown unless a known sensor is explicitly configured.
        return result


class HashJob:
    """Fixed 64 MiB example, incremental 64 KiB chunks; never accepts a path."""
    BLOCK = (b'MixOS fixed sha256 example\n' * 2521)[:65536]
    STEPS = 1024

    def __init__(self, session):
        self.session = session
        self.hash = hashlib.sha256()
        self.step = 0
        self.reported = -1

    def advance(self):
        self.hash.update(self.BLOCK)
        self.step += 1
        return self.step * 100 // self.STEPS


class Link:
    """Protocol state independent of the OS; all queues are bounded."""
    def __init__(self, shell_factory, metrics=None, clock=time.monotonic):
        self.shell_factory = shell_factory
        self.metrics = metrics or HostMetrics()
        self.clock = clock
        self.decoder = Decoder()
        self.rx = ReceiveEpoch()
        self.sequence = 0
        self.tx = WriteQueue()
        self.input = WriteQueue(INPUT_LIMIT)
        self.credit = Credit()
        self.session = 0
        self.shell = None
        self.job = None
        self.last_rx = self.last_ping = clock()
        self.last_status = -10.0

    def close_session(self):
        if self.shell is not None:
            self.shell.close()
        self.shell = None
        self.session = 0
        self.input = WriteQueue(INPUT_LIMIT)
        self.credit = Credit()

    def reset(self):
        self.close_session()
        self.job = None
        self.rx = ReceiveEpoch()
        self.decoder = Decoder()
        self.tx = WriteQueue()
        self.sequence = 0

    def send(self, channel, kind, session=0, payload=b''):
        packet = Frame(channel, kind, self.rx.epoch, session,
                       self.sequence, payload).encode()
        self.tx.put(packet)
        self.sequence = (self.sequence + 1) & 0xFFFFFFFF

    def error(self, frame, reason):
        self.send(C.CONTROL, T.ERROR, frame.session, reason.encode('utf-8')[:512])

    def send_json(self, channel, kind, session, value):
        self.send(channel, kind, session,
                  json.dumps(value, separators=(',', ':'), allow_nan=False).encode())

    def feed(self, data):
        for frame in self.decoder.feed(data):
            self.handle(frame)

    def handle(self, f):
        accepted = self.rx.accept(f)
        if accepted is None:
            return
        now = self.clock()
        if accepted in ('new', 'hello'):
            if accepted == 'new':
                self.close_session()
                self.job = None
                self.tx = WriteQueue()
                # Terminate any partially transmitted old-epoch frame before ACK.
                self.tx.put(b'\0')
                self.sequence = 0
            self.last_rx = self.last_ping = now
            self.send(C.CONTROL, T.HELLO_ACK, payload=struct.pack('<HH', 512, 4096))
            return
        self.last_rx = now
        if f.channel == C.CONTROL and f.session == 0 and not f.payload:
            if f.type == T.PING:
                self.send(C.CONTROL, T.PONG)
                return
            if f.type == T.PONG:
                return
        if f.channel == C.TERMINAL:
            if f.type == T.OPEN and f.session and len(f.payload) == 4:
                cols, rows = struct.unpack('<HH', f.payload)
                if not (1 <= cols <= 240 and 1 <= rows <= 100):
                    self.error(f, 'invalid terminal dimensions')
                elif self.session:
                    self.error(f, 'terminal already open')
                else:
                    try:
                        self.shell = self.shell_factory(cols, rows)
                    except (OSError, ValueError) as exc:
                        self.error(f, 'PTY creation failed: ' + str(exc))
                        return
                    self.session = f.session
                    self.credit = Credit()
                    self.send(C.TERMINAL, T.OPENED, f.session, f.payload)
                return
            if not self.session or f.session != self.session:
                self.error(f, 'inactive terminal session')
                return
            if f.type == T.CREDIT and len(f.payload) == 4:
                if not self.credit.update(struct.unpack('<I', f.payload)[0]):
                    self.error(f, 'invalid cumulative credit')
                return
            if f.type == T.INPUT:
                try:
                    self.input.put(f.payload)
                except QueueFull:
                    self.error(f, 'input overflow; terminal closed')
                    self.close_session()
                    self.send(C.TERMINAL, T.CLOSE, f.session)
                return
            if f.type == T.RESIZE and len(f.payload) == 4:
                cols, rows = struct.unpack('<HH', f.payload)
                if 1 <= cols <= 240 and 1 <= rows <= 100:
                    self.shell.resize(cols, rows)
                else:
                    self.error(f, 'invalid terminal dimensions')
                return
            if f.type == T.CLOSE and not f.payload:
                self.close_session()
                return
        elif (f.channel == C.STATUS and f.type == T.STATUS_REQUEST
              and f.session == 0 and not f.payload):
            if now - self.last_status >= 1:
                self.send_json(C.STATUS, T.STATUS, 0, self.metrics.sample())
                self.last_status = now
            return
        elif f.channel == C.JOB and f.session:
            if f.type == T.JOB_START and f.payload == b'sha256':
                if self.job:
                    self.error(f, 'job already active')
                else:
                    self.job = HashJob(f.session)
                return
            if f.type == T.JOB_CANCEL and not f.payload:
                if self.job and f.session == self.job.session:
                    self.send_json(C.JOB, T.JOB_RESULT, f.session, {'cancelled': True})
                    self.job = None
                else:
                    self.error(f, 'inactive job')
                return
        # Maintenance is deliberately not implemented by the shell service.
        self.error(f, 'unsupported request; maintenance requires local updater')

    @property
    def read_budget(self):
        if not self.shell or getattr(self.shell, 'eof', False) or self.tx.size >= DATA_HIGH_WATER:
            return 0
        return min(512, self.credit.available)

    def output(self, data):
        if len(data) > self.read_budget:
            raise ValueError('PTY read exceeded budget')
        if data:
            self.send(C.TERMINAL, T.DATA, self.session, data)
            self.credit.consume(len(data))

    def exited(self, code):
        session = self.session
        self.close_session()
        self.send(C.TERMINAL, T.EXIT, session, struct.pack('<i', code))

    def tick(self):
        now = self.clock()
        if self.rx.epoch and now - self.last_rx >= 8:
            self.reset()
            return
        if not self.rx.epoch:
            return
        if now - self.last_ping >= 2:
            self.send(C.CONTROL, T.PING)
            self.last_ping = now
        if self.job and self.tx.size < DATA_HIGH_WATER:
            percent = self.job.advance()
            if percent != self.job.reported:
                self.send_json(C.JOB, T.JOB_PROGRESS, self.job.session, {'percent': percent})
                self.job.reported = percent
            if self.job.step == self.job.STEPS:
                self.send_json(C.JOB, T.JOB_RESULT, self.job.session,
                               {'sha256': self.job.hash.hexdigest(),
                                'bytes': len(self.job.BLOCK) * self.job.STEPS})
                self.job = None


class PtyShell:
    children = set()

    def __init__(self, cols, rows, shell='/bin/sh'):
        import pty
        pid, fd = pty.fork()
        if pid == 0:
            try:
                os.environ['TERM'] = 'mixos'
                os.environ['COLORTERM'] = ''
                os.execv(shell, [shell, '-i'])
            except BaseException:
                os._exit(127)
        self.pid, self.fd = pid, fd
        self.status = None
        self.eof = False
        try:
            os.set_blocking(fd, False)
            self.resize(cols, rows)
        except BaseException:
            self.close()
            raise

    def resize(self, cols, rows):
        import fcntl
        import termios
        fcntl.ioctl(self.fd, termios.TIOCSWINSZ, struct.pack('HHHH', rows, cols, 0, 0))

    def poll(self):
        if self.status is None:
            try:
                pid, status = os.waitpid(self.pid, os.WNOHANG)
            except ChildProcessError:
                return self.status
            if pid:
                self.children.discard(pid)
                self.status = os.waitstatus_to_exitcode(status)
        return self.status

    def close(self):
        if self.fd >= 0:
            os.close(self.fd)
            self.fd = -1
        # Initial prototype terminates the session and its process group on loss.
        for sig in (signal.SIGHUP, signal.SIGKILL):
            try:
                os.killpg(self.pid, sig)
            except ProcessLookupError:
                pass
        if self.poll() is None:
            self.children.add(self.pid)

    @classmethod
    def reap(cls):
        for pid in list(cls.children):
            try:
                exited, _ = os.waitpid(pid, os.WNOHANG)
            except ChildProcessError:
                exited = pid
            if exited:
                cls.children.discard(pid)


def open_serial(device):
    """Exclusive raw CDC ownership; never toggles DTR/RTS for reset."""
    import fcntl
    import termios
    import tty
    fd = os.open(device, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK | os.O_CLOEXEC)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.ioctl(fd, termios.TIOCEXCL)
        tty.setraw(fd, termios.TCSANOW)
        attrs = termios.tcgetattr(fd)
        attrs[2] |= termios.CLOCAL | termios.CREAD
        attrs[2] &= ~(termios.HUPCL | getattr(termios, 'CRTSCTS', 0))
        attrs[4] = attrs[5] = termios.B115200
        termios.tcsetattr(fd, termios.TCSANOW, attrs)
        termios.tcflush(fd, termios.TCIOFLUSH)
        return fd
    except BaseException:
        os.close(fd)
        raise


def serve(device, shell):
    running = True

    def stop(_sig, _frame):
        nonlocal running
        running = False

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    while running:
        link = Link(lambda cols, rows: PtyShell(cols, rows, shell))
        fd = None
        try:
            fd = open_serial(device)
            while running:
                link.tick()
                PtyShell.reap()
                # Rebuild the small interest set so credit changes apply immediately.
                with selectors.DefaultSelector() as sel:
                    sel.register(fd, selectors.EVENT_READ |
                                 (selectors.EVENT_WRITE if link.tx.size else 0), 'serial')
                    pty = link.shell
                    if pty:
                        events = (selectors.EVENT_READ if link.read_budget else 0)
                        if link.input.size:
                            events |= selectors.EVENT_WRITE
                        if events:
                            sel.register(pty.fd, events, 'pty')
                    ready = sel.select(0.02)
                for key, events in ready:
                    if key.data == 'serial':
                        if events & selectors.EVENT_READ:
                            data = os.read(fd, 4096)
                            if not data:
                                raise OSError('USB disconnected')
                            link.feed(data)
                        if events & selectors.EVENT_WRITE:
                            link.tx.flush(lambda data: os.write(fd, data))
                    elif link.shell is pty:
                        if events & selectors.EVENT_WRITE:
                            link.input.flush(lambda data: os.write(pty.fd, data))
                        if events & selectors.EVENT_READ and link.read_budget:
                            try:
                                data = os.read(pty.fd, link.read_budget)
                            except OSError as exc:
                                if exc.errno != errno.EIO:
                                    raise
                                data = b''
                            if data:
                                link.output(data)
                            else:
                                pty.eof = True
                                link.input = WriteQueue(INPUT_LIMIT)
                                code = pty.poll()
                                if code is not None:
                                    link.exited(code)
                if link.shell and link.shell.poll() is not None:
                    # Credit-starved trailing output is deliberately discarded on exit.
                    link.exited(link.shell.status)
        except (OSError, QueueFull) as exc:
            print('mixosd: link closed:', exc, file=sys.stderr)
        finally:
            link.reset()
            if fd is not None:
                os.close(fd)
            PtyShell.reap()
        if running:
            time.sleep(1)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--device', required=True, help='explicit /dev/serial/by-id/... path')
    parser.add_argument('--shell', default='/bin/sh', help='absolute ordinary interactive shell')
    args = parser.parse_args(argv)
    if sys.platform != 'linux':
        parser.error('mixosd requires Linux; protocol tests are portable')
    if os.geteuid() == 0:
        parser.error('refusing root shell; configure an ordinary service user')
    if not os.path.isabs(args.shell) or not os.access(args.shell, os.X_OK):
        parser.error('shell must be an absolute executable path')
    if not args.device.startswith('/dev/serial/by-id/'):
        parser.error('use an explicit stable /dev/serial/by-id/ identity')
    serve(args.device, args.shell)


if __name__ == '__main__':
    main()
