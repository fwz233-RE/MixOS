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
import subprocess
import sys
import time

import netctl
from protocol import Channel as C, Type as T, Frame, Decoder, ReceiveEpoch, Credit

TX_LIMIT = 16384
INPUT_LIMIT = 4096
DATA_HIGH_WATER = 8192
# One credit window per loop iteration. A single 512-byte read per pass made a
# full-screen redraw arrive in visible bands; draining the window keeps a TUI
# repaint inside one frame.
PTY_READS_PER_PASS = 8
SERIAL_WRITE_BUDGET = 8192
# Only these four applications can be started, and the name is the whole
# request: the daemon never receives, builds or interprets a command line.
APP_NAMES = ('shell', 'translate', 'notes', 'agent')
NETCTL_HELPER = str(Path(__file__).resolve().parent / 'netctl.py')
NET_TIMEOUTS = {
    T.NET_SCAN: 30.0,
    T.NET_CONNECT: 50.0,
    T.NET_FORGET: 10.0,
    'state': 12.0,
}
STATE_REFRESH = 10.0


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
        self.wifi = None
        self.address = ''

    def report_network(self, state, address):
        """Latest asynchronous network answer; None means still unknown."""
        self.wifi = state
        self.address = address or ''

    @staticmethod
    def _timezone_offset_min():
        local = time.localtime()
        seconds = -(time.altzone if (time.daylight and local.tm_isdst > 0) else time.timezone)
        return int(seconds // 60)

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
        # An absent Wi-Fi report stays absent: the device shows "unknown", never
        # a disconnected radio it was never told about.
        if self.wifi is not None:
            result['wifi'] = dict(connected=bool(self.wifi.get('connected')),
                                  ssid=str(self.wifi.get('ssid', ''))[:netctl.SSID_MAX],
                                  signal=self.wifi.get('signal'))
        if self.address:
            result['ip'] = self.address
        result['time_s'] = int(time.time())
        result['tz_offset_min'] = self._timezone_offset_min()
        return result


class NetWorker:
    """Runs one bounded netctl helper at a time as a non-blocking child.

    nmcli can take tens of seconds. Doing that inline would stop the heartbeat
    and drop the link, so the helper is a separate process and the event loop
    keeps running. The request travels on stdin rather than argv, so a Wi-Fi
    passphrase never appears in the process table.
    """

    def __init__(self, clock=time.monotonic, spawn=None):
        self.clock = clock
        self._spawn = spawn or self._default_spawn
        self.proc = None
        self.kind = None
        self.session = 0
        self.deadline = 0.0

    @staticmethod
    def _default_spawn():
        return subprocess.Popen(
            [sys.executable, NETCTL_HELPER],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)

    def busy(self):
        return self.proc is not None

    def start(self, kind, session, request, timeout):
        if self.busy():
            return False
        try:
            self.proc = self._spawn()
            self.proc.stdin.write(json.dumps(request).encode('utf-8'))
            self.proc.stdin.close()
        except (OSError, ValueError):
            self._discard()
            return False
        self.kind, self.session, self.deadline = kind, session, self.clock() + timeout
        return True

    def _discard(self):
        if self.proc is not None:
            for closer in (self.proc.stdin, self.proc.stdout):
                try:
                    if closer:
                        closer.close()
                except OSError:
                    pass
            try:
                self.proc.kill()
                self.proc.wait(timeout=2)
            except (OSError, subprocess.SubprocessError):
                pass
        self.proc = None
        self.kind = None
        self.session = 0

    def poll(self):
        """Returns (kind, session, answer) exactly once per finished request."""
        if not self.busy():
            return None
        finished = self.proc.poll() is not None
        if not finished and self.clock() < self.deadline:
            return None
        kind, session = self.kind, self.session
        if not finished:
            answer = {'ok': False, 'error': 'network request timed out'}
        else:
            try:
                raw = self.proc.stdout.read() or b''
                answer = json.loads(raw.decode('utf-8'))
                if not isinstance(answer, dict):
                    raise ValueError('helper answer is not an object')
            except (OSError, ValueError, UnicodeDecodeError):
                answer = {'ok': False, 'error': 'network helper returned nothing usable'}
        self._discard()
        return kind, session, answer

    def close(self):
        self._discard()


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
    def __init__(self, shell_factory, metrics=None, clock=time.monotonic, net=None):
        self.shell_factory = shell_factory
        self.metrics = metrics or HostMetrics()
        self.clock = clock
        self.net = net if net is not None else NetWorker(clock=clock)
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
        self.next_state_refresh = 0.0

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
        self.net.close()
        self.next_state_refresh = 0.0
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
            if f.type == T.OPEN and f.session and len(f.payload) >= 4:
                cols, rows = struct.unpack('<HH', f.payload[:4])
                app = self.requested_app(f)
                if app is None:
                    return
                if not (1 <= cols <= 240 and 1 <= rows <= 100):
                    self.error(f, 'invalid terminal dimensions')
                    return
                # Switching applications is an OPEN for a session the screen has
                # already replaced its old one with. Refusing it - which is what
                # 'terminal already open' did - left the screen titled as the new
                # application while the old one kept the pseudo-terminal, so the
                # notes editor stayed on screen under a "live translation"
                # heading. The screen sends CLOSE first and normally that has
                # already arrived; this covers the case where it was lost, and
                # keeps exactly one application alive either way.
                if self.session and f.session != self.session:
                    self.close_session()
                elif self.session:
                    self.error(f, 'terminal already open')
                    return
                try:
                    self.shell = self.shell_factory(cols, rows, app)
                except (OSError, ValueError) as exc:
                    self.error(f, 'session creation failed: ' + str(exc))
                    return
                self.session = f.session
                self.credit = Credit()
                self.send(C.TERMINAL, T.OPENED, f.session, f.payload[:4])
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
        elif f.channel == C.NET and f.session:
            self.network(f)
            return
        # Maintenance is deliberately not implemented by the shell service.
        self.error(f, 'unsupported request; maintenance requires local updater')

    def requested_app(self, f):
        """The application named by an OPEN frame, or None after reporting why.

        A four-byte payload is the original geometry-only request and still
        means the plain shell.
        """
        if len(f.payload) == 4:
            return 'shell'
        length = f.payload[4]
        if len(f.payload) != 5 + length or not length:
            self.error(f, 'malformed application identifier')
            return None
        name = f.payload[5:5 + length].decode('utf-8', 'replace')
        if name not in APP_NAMES:
            self.error(f, 'unknown application: ' + name[:32])
            return None
        return name

    def network(self, f):
        """Starts one bounded helper per request; nothing runs inline here."""
        if f.type not in (T.NET_SCAN, T.NET_CONNECT, T.NET_FORGET):
            self.error(f, 'unsupported network request')
            return
        if self.net.busy():
            self.send(C.NET, T.NET_RESULT, f.session,
                      b'\x01another network request is already running')
            return
        try:
            if f.type == T.NET_SCAN:
                request = {'verb': 'scan'}
            else:
                ssid, passphrase = netctl.unpack_request(f.payload)
                verb = 'connect' if f.type == T.NET_CONNECT else 'forget'
                request = {'verb': verb, 'ssid': ssid, 'passphrase': passphrase}
        except netctl.NetError as exc:
            self.send(C.NET, T.NET_RESULT, f.session, b'\x01' + str(exc).encode('utf-8')[:256])
            return
        if not self.net.start(f.type, f.session, request, NET_TIMEOUTS[f.type]):
            self.send(C.NET, T.NET_RESULT, f.session, b'\x01cannot start network helper')

    def net_answer(self, kind, session, answer):
        """Turns a finished helper result into the frame the device expects."""
        if kind == 'state':
            self.metrics.report_network(answer.get('state') if answer.get('ok') else None,
                                        answer.get('ip', ''))
            return
        if not answer.get('ok'):
            message = str(answer.get('error', 'network request failed'))
            self.send(C.NET, T.NET_RESULT, session, b'\x01' + message.encode('utf-8')[:256])
            return
        if kind == T.NET_SCAN:
            entries = answer.get('networks') or []
            self.send(C.NET, T.NET_LIST, session, netctl.pack_scan(entries))
            return
        message = str(answer.get('message', 'done'))
        self.send(C.NET, T.NET_RESULT, session, b'\x00' + message.encode('utf-8')[:256])


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
        finished = self.net.poll()
        if finished:
            self.net_answer(*finished)
        if self.rx.epoch and now - self.last_rx >= 8:
            self.reset()
            return
        if not self.rx.epoch:
            return
        if now - self.last_ping >= 2:
            self.send(C.CONTROL, T.PING)
            self.last_ping = now
        # A background state refresh only ever runs on an idle helper, so a
        # user-initiated scan or connect is never delayed behind it.
        if now >= self.next_state_refresh and not self.net.busy():
            self.next_state_refresh = now + STATE_REFRESH
            self.net.start('state', 0, {'verb': 'state'}, NET_TIMEOUTS['state'])
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
    # How long a program has between "you are finished" and "you are killed".
    GRACE_SECONDS = 0.6

    def __init__(self, cols, rows, argv, term='mixos'):
        """argv is a fully resolved absolute program and its fixed arguments.

        The caller has already mapped an allow-listed application name to this
        vector; nothing here parses, splits or interpolates a command string.
        """
        # The argument contract holds on every platform, so it is checked
        # before importing pty, which exists only on Unix.
        if not argv or not os.path.isabs(argv[0]):
            raise ValueError('application must be an absolute program path')
        import pty
        pid, fd = pty.fork()
        if pid == 0:
            try:
                os.environ['TERM'] = term
                os.environ['COLORTERM'] = ''
                os.environ['LANG'] = os.environ.get('LANG', 'C.UTF-8')
                os.environ['MIXOS_COLS'] = str(cols)
                os.environ['MIXOS_ROWS'] = str(rows)
                os.execv(argv[0], list(argv))
            except BaseException:
                os._exit(127)
        self.pid, self.fd = pid, fd
        self.status = None
        self.eof = False
        try:
            os.set_blocking(fd, False)
            self.disable_flow_control()
            self.resize(cols, rows)
        except BaseException:
            self.close()
            raise

    def disable_flow_control(self):
        """Stop the line discipline from eating Ctrl-S and Ctrl-Q.

        A fresh pseudo-terminal comes up with ``IXON`` set, so Ctrl-S is XOFF and
        Ctrl-Q is XON and neither reaches the program. A full-screen program that
        is the direct child does not notice, because it puts its own terminal in
        raw mode on the way up. One that runs *inside* another terminal does:
        term-ime clears only ``ICANON``, ``ECHO`` and ``ISIG``, so with flow
        control left on here, Ctrl-S and Ctrl-Q are consumed before term-ime can
        forward them, and the notes editor loses Save and Back.

        Only the three flow-control bits are cleared. ``OPOST`` in particular
        stays, because the shell application relies on newline translation to
        avoid stair-stepping every line it prints.
        """
        import termios
        attributes = termios.tcgetattr(self.fd)
        attributes[0] &= ~(termios.IXON | termios.IXOFF |
                           getattr(termios, 'IXANY', 0))
        termios.tcsetattr(self.fd, termios.TCSANOW, attributes)

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
        # Ending a session is now an ordinary thing - leaving an application on
        # the screen does it - so the program gets told before it gets killed.
        # SIGHUP first, a moment to write whatever it was holding, SIGKILL only
        # for something that ignored both. The wait is short enough that the
        # link's two-second ping is not disturbed and long enough for a note to
        # reach the eMMC.
        try:
            os.killpg(self.pid, signal.SIGHUP)
        except ProcessLookupError:
            pass
        deadline = time.monotonic() + self.GRACE_SECONDS
        while self.poll() is None and time.monotonic() < deadline:
            time.sleep(0.02)
        if self.poll() is None:
            try:
                os.killpg(self.pid, signal.SIGKILL)
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


def resolve_app(name, shell, app_dir):
    """Maps an allow-listed application name to an absolute argument vector."""
    if name not in APP_NAMES:
        raise ValueError('unknown application')
    if name == 'shell':
        return [shell, '-i']
    program = os.path.join(app_dir, name)
    if not os.path.isfile(program) or not os.access(program, os.X_OK):
        raise ValueError('%s is not installed on this host' % name)
    return [program]


def serve(device, shell, app_dir):
    running = True

    def stop(_sig, _frame):
        nonlocal running
        running = False

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    while running:
        link = Link(lambda cols, rows, app: PtyShell(
            cols, rows, resolve_app(app, shell, app_dir)))
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
                    # A running network helper must be noticed promptly without
                    # spinning the loop while nothing else is happening.
                    ready = sel.select(0.02 if not link.net.busy() else 0.05)
                for key, events in ready:
                    if key.data == 'serial':
                        if events & selectors.EVENT_READ:
                            data = os.read(fd, 4096)
                            if not data:
                                raise OSError('USB disconnected')
                            link.feed(data)
                        if events & selectors.EVENT_WRITE:
                            link.tx.flush(lambda data: os.write(fd, data),
                                          budget=SERIAL_WRITE_BUDGET)
                    elif link.shell is pty:
                        if events & selectors.EVENT_WRITE:
                            link.input.flush(lambda data: os.write(pty.fd, data))
                        # Drain a whole credit window per pass. One 512-byte read
                        # per loop made a full-screen TUI repaint arrive in
                        # visible bands instead of as one frame.
                        for _ in range(PTY_READS_PER_PASS):
                            if not (events & selectors.EVENT_READ) or not link.read_budget:
                                break
                            try:
                                data = os.read(pty.fd, link.read_budget)
                            except BlockingIOError:
                                break
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
                                break
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
    parser.add_argument('--app-dir', default='/usr/local/lib/mixos/apps',
                        help='directory holding the allow-listed application launchers')
    args = parser.parse_args(argv)
    if sys.platform != 'linux':
        parser.error('mixosd requires Linux; protocol tests are portable')
    if os.geteuid() == 0:
        parser.error('refusing root shell; configure an ordinary service user')
    if not os.path.isabs(args.shell) or not os.access(args.shell, os.X_OK):
        parser.error('shell must be an absolute executable path')
    if not os.path.isabs(args.app_dir):
        parser.error('application directory must be an absolute path')
    if not args.device.startswith('/dev/serial/by-id/'):
        parser.error('use an explicit stable /dev/serial/by-id/ identity')
    serve(args.device, args.shell, args.app_dir)


if __name__ == '__main__':
    main()
