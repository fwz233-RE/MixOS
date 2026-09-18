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
from mixosd import (Link, WriteQueue, QueueFull, PtyShell, HashJob, HostMetrics,
                    open_serial, resolve_app, APP_NAMES)
import netctl
import headless

spec = importlib.util.spec_from_file_location('update_esp', ROOT / 'tools' / 'update_esp.py')
update = importlib.util.module_from_spec(spec)
spec.loader.exec_module(update)


class FakeShell:
    def __init__(self, cols, rows, app='shell'):
        self.dimensions = cols, rows
        self.app = app
        self.closed = False

    def resize(self, cols, rows):
        self.dimensions = cols, rows

    def close(self):
        self.closed = True


class FakeNet:
    """Stands in for the helper subprocess: nothing is spawned in these tests."""

    def __init__(self):
        self.started = []
        self.pending = None
        self.answers = {}
        self.closed = 0

    def busy(self):
        return self.pending is not None

    def start(self, kind, session, request, timeout):
        if self.pending is not None:
            return False
        self.started.append((kind, session, request, timeout))
        self.pending = (kind, session)
        return True

    def finish(self, answer):
        self.answers[self.pending] = answer

    def poll(self):
        if self.pending is None or self.pending not in self.answers:
            return None
        key = self.pending
        self.pending = None
        return key[0], key[1], self.answers.pop(key)

    def close(self):
        self.pending = None
        self.closed += 1


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
        self.link = Link(FakeShell, clock=lambda: self.now, net=FakeNet())
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
            sample = HostMetrics().sample()
        measured = ('uptime_s', 'cpu_pct', 'mem_used_kib', 'mem_total_kib', 'temp_c')
        self.assertTrue(all(sample[key] is None for key in measured))
        # An unreported radio is absent from the report, never a fabricated
        # "disconnected" or "0%".
        self.assertNotIn('wifi', sample)
        self.assertNotIn('ip', sample)

    def test_network_report_only_appears_once_the_host_answers(self):
        metrics = HostMetrics()
        self.assertNotIn('wifi', metrics.sample())
        metrics.report_network({'connected': True, 'ssid': 'lab', 'signal': 71}, '10.0.0.5')
        sample = metrics.sample()
        self.assertEqual(sample['wifi'], {'connected': True, 'ssid': 'lab', 'signal': 71})
        self.assertEqual(sample['ip'], '10.0.0.5')
        # A later failed query withdraws the claim rather than freezing the old one.
        metrics.report_network(None, '')
        self.assertNotIn('wifi', metrics.sample())

    def test_clock_is_always_reported(self):
        sample = HostMetrics().sample()
        self.assertIsInstance(sample['time_s'], int)
        self.assertTrue(-1440 <= sample['tz_offset_min'] <= 1440)


class ApplicationTests(unittest.TestCase):
    """The launcher accepts four names and nothing else."""

    def setUp(self):
        self.now = 0.0
        self.net = FakeNet()
        self.link = Link(FakeShell, clock=lambda: self.now, net=self.net)
        self.seq = 1
        self.epoch = 77
        self.send(C.CONTROL, T.HELLO, payload=struct.pack('<HH', 512, 4096))
        drain(self.link.tx)

    def send(self, channel, kind, session=0, payload=b''):
        frame = Frame(channel, kind, self.epoch, session, self.seq, payload)
        self.seq += 1
        self.link.feed(frame.encode())
        return frame

    def open_app(self, name, cols=80, rows=28):
        payload = struct.pack('<HH', cols, rows)
        if name is not None:
            encoded = name.encode('utf-8')
            payload += bytes([len(encoded)]) + encoded
        self.send(C.TERMINAL, T.OPEN, 7, payload)
        return drain(self.link.tx)

    def test_each_allowed_application_starts(self):
        for name in APP_NAMES:
            with self.subTest(app=name):
                self.setUp()
                frames = self.open_app(name)
                self.assertEqual(frames[0].type, T.OPENED)
                # OPENED echoes only the geometry, so the device compares the
                # number it asked for and nothing else.
                self.assertEqual(frames[0].payload, struct.pack('<HH', 80, 28))
                self.assertEqual(self.link.shell.app, name)

    def test_geometry_only_request_still_means_the_shell(self):
        frames = self.open_app(None)
        self.assertEqual(frames[0].type, T.OPENED)
        self.assertEqual(self.link.shell.app, 'shell')

    def test_unknown_application_is_refused_without_starting_anything(self):
        for name in ('bash', 'translate ', 'Translate', '../../bin/sh', 'notes\x00'):
            with self.subTest(app=name):
                self.setUp()
                frames = self.open_app(name)
                self.assertEqual(frames[0].type, T.ERROR)
                self.assertIsNone(self.link.shell)
                self.assertEqual(self.link.session, 0)

    def test_malformed_identifier_is_refused(self):
        # Length byte that disagrees with the payload it describes.
        self.send(C.TERMINAL, T.OPEN, 7, struct.pack('<HH', 80, 28) + b'\x09notes')
        frames = drain(self.link.tx)
        self.assertEqual(frames[0].type, T.ERROR)
        self.assertIsNone(self.link.shell)

    def test_alternate_geometry_is_accepted_and_echoed(self):
        frames = self.open_app('notes', cols=64, rows=22)
        self.assertEqual(frames[0].type, T.OPENED)
        self.assertEqual(frames[0].payload, struct.pack('<HH', 64, 22))
        self.assertEqual(self.link.shell.dimensions, (64, 22))

    def test_switching_applications_replaces_the_running_one(self):
        """A new session id asking to open means the person changed application.

        Answering that with 'terminal already open' is what left the notes
        editor running under a "live translation" heading: the screen had
        already moved on, and the only program with a pseudo-terminal was the
        old one. Exactly one application is alive at any time.
        """
        self.open_app('notes')
        first = self.link.shell
        self.assertEqual(self.link.session, 7)

        payload = struct.pack('<HH', 80, 28) + b'\x09translate'
        self.send(C.TERMINAL, T.OPEN, 8, payload)
        frames = drain(self.link.tx)
        self.assertEqual([f.type for f in frames], [T.OPENED])
        self.assertEqual(frames[0].session, 8)
        self.assertTrue(first.closed)
        self.assertIsNot(self.link.shell, first)
        self.assertEqual(self.link.shell.app, 'translate')
        self.assertEqual(self.link.session, 8)

    def test_reopening_the_same_session_is_still_refused(self):
        """Only a *different* session id means "switch"; a repeat is a mistake."""
        self.open_app('notes')
        shell = self.link.shell
        self.send(C.TERMINAL, T.OPEN, 7, struct.pack('<HH', 80, 28) + b'\x09translate')
        frames = drain(self.link.tx)
        self.assertEqual(frames[0].type, T.ERROR)
        self.assertIn(b'already open', frames[0].payload)
        self.assertIs(self.link.shell, shell)
        self.assertFalse(shell.closed)

    def test_a_refused_switch_leaves_the_old_application_running(self):
        """Bad geometry and unknown names are refused before anything closes."""
        self.open_app('notes')
        shell = self.link.shell
        self.send(C.TERMINAL, T.OPEN, 8, struct.pack('<HH', 0, 28) + b'\x09translate')
        self.assertEqual(drain(self.link.tx)[0].type, T.ERROR)
        self.assertIs(self.link.shell, shell)
        self.assertFalse(shell.closed)
        self.send(C.TERMINAL, T.OPEN, 9, struct.pack('<HH', 80, 28) + b'\x04bash')
        self.assertEqual(drain(self.link.tx)[0].type, T.ERROR)
        self.assertIs(self.link.shell, shell)
        self.assertFalse(shell.closed)
        self.assertEqual(self.link.session, 7)

    def test_resolve_app_never_leaves_the_application_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            program = Path(directory) / 'notes'
            program.write_text('#!/bin/sh\n')
            program.chmod(0o755)
            self.assertEqual(resolve_app('notes', '/bin/sh', directory), [str(program)])
            self.assertEqual(resolve_app('shell', '/bin/sh', directory), ['/bin/sh', '-i'])
            # A name outside the table can never become a path.
            for bad in ('../sh', '/bin/sh', 'nope'):
                with self.assertRaises(ValueError):
                    resolve_app(bad, '/bin/sh', directory)
            # An allow-listed name that is not installed is an error, not a
            # silent fallback to some other program.
            with self.assertRaises(ValueError):
                resolve_app('translate', '/bin/sh', directory)

    def test_pty_shell_requires_an_absolute_program(self):
        with self.assertRaises(ValueError):
            PtyShell(80, 28, ['sh'])
        with self.assertRaises(ValueError):
            PtyShell(80, 28, [])


class NetworkChannelTests(unittest.TestCase):
    def setUp(self):
        self.now = 0.0
        self.net = FakeNet()
        self.link = Link(FakeShell, clock=lambda: self.now, net=self.net)
        self.seq = 1
        self.epoch = 91
        self.send(C.CONTROL, T.HELLO, payload=struct.pack('<HH', 512, 4096))
        drain(self.link.tx)

    def send(self, channel, kind, session=0, payload=b''):
        frame = Frame(channel, kind, self.epoch, session, self.seq, payload)
        self.seq += 1
        self.link.feed(frame.encode())
        return frame

    @staticmethod
    def pack(*values):
        out = bytearray()
        for value in values:
            data = value.encode('utf-8')
            out += bytes([len(data)]) + data
        return bytes(out)

    def test_scan_runs_out_of_line_and_returns_a_packed_list(self):
        self.send(C.NET, T.NET_SCAN, 5)
        # Nothing is answered inline: the helper has not finished yet.
        self.assertEqual(drain(self.link.tx), [])
        self.assertEqual(self.net.started[0][0], T.NET_SCAN)
        self.net.finish({'ok': True, 'networks': [
            {'ssid': 'lab', 'signal': 80, 'secured': True, 'known': True},
            {'ssid': 'guest', 'signal': 40, 'secured': False, 'known': False}]})
        self.link.tick()
        frames = drain(self.link.tx)
        listing = [f for f in frames if f.channel == C.NET]
        self.assertEqual(listing[0].type, T.NET_LIST)
        self.assertEqual(listing[0].payload[0], 2)
        self.assertIn(b'lab', listing[0].payload)
        self.assertIn(b'guest', listing[0].payload)

    def test_connect_passes_the_passphrase_only_through_the_helper_request(self):
        self.send(C.NET, T.NET_CONNECT, 6, self.pack('lab', 'hunter2'))
        kind, session, request, _timeout = self.net.started[0]
        self.assertEqual((kind, session), (T.NET_CONNECT, 6))
        self.assertEqual(request, {'verb': 'connect', 'ssid': 'lab', 'passphrase': 'hunter2'})
        self.net.finish({'ok': True, 'message': 'connected to lab'})
        self.link.tick()
        answer = [f for f in drain(self.link.tx) if f.channel == C.NET][0]
        self.assertEqual(answer.type, T.NET_RESULT)
        self.assertEqual(answer.payload[0], 0)
        self.assertIn(b'connected to lab', answer.payload)

    def test_failed_request_reports_the_reason_with_a_nonzero_code(self):
        self.send(C.NET, T.NET_CONNECT, 8, self.pack('lab', 'bad'))
        self.net.finish({'ok': False, 'error': 'Secrets were required, but not provided'})
        self.link.tick()
        answer = [f for f in drain(self.link.tx) if f.channel == C.NET][0]
        self.assertEqual(answer.type, T.NET_RESULT)
        self.assertEqual(answer.payload[0], 1)
        self.assertIn(b'Secrets were required', answer.payload)

    def test_second_request_is_refused_while_one_is_running(self):
        self.send(C.NET, T.NET_SCAN, 5)
        drain(self.link.tx)
        self.send(C.NET, T.NET_CONNECT, 6, self.pack('lab', 'pw'))
        answer = [f for f in drain(self.link.tx) if f.channel == C.NET][0]
        self.assertEqual(answer.type, T.NET_RESULT)
        self.assertEqual(answer.payload[0], 1)
        self.assertEqual(len(self.net.started), 1)

    def test_background_state_refresh_never_delays_a_user_request(self):
        # The idle link starts its own state query.
        self.link.tick()
        self.assertEqual(self.net.started[0][0], 'state')
        # While that runs, a user scan is refused rather than queued behind it,
        # so the device sees an immediate answer instead of an unexplained wait.
        self.send(C.NET, T.NET_SCAN, 5)
        answer = [f for f in drain(self.link.tx) if f.channel == C.NET][0]
        self.assertEqual(answer.payload[0], 1)
        # A finished state query updates the metrics without emitting a frame.
        self.net.finish({'ok': True, 'state': {'connected': True, 'ssid': 'lab', 'signal': 60},
                         'ip': '10.0.0.9'})
        self.link.tick()
        self.assertEqual([f for f in drain(self.link.tx) if f.channel == C.NET], [])
        self.assertEqual(self.link.metrics.sample()['wifi']['ssid'], 'lab')

    def test_unsupported_network_type_is_refused(self):
        self.send(C.NET, T.NET_LIST, 5)
        answer = drain(self.link.tx)[0]
        self.assertEqual(answer.type, T.ERROR)
        self.assertEqual(self.net.started, [])

    def test_link_reset_stops_a_running_helper(self):
        self.send(C.NET, T.NET_SCAN, 5)
        self.link.reset()
        self.assertEqual(self.net.closed, 1)
        self.assertFalse(self.net.busy())


class NetctlTests(unittest.TestCase):
    """The nmcli wrapper never builds a command string and never guesses."""

    def test_terse_fields_survive_a_colon_in_the_name(self):
        entries = list(netctl._split(r'80:WPA2:*:my\:network'))
        self.assertEqual(entries, ['80', 'WPA2', '*', 'my:network'])

    def test_scan_merges_duplicates_and_sorts_by_signal(self):
        listing = '\n'.join([
            '40:WPA2: :lab',
            '80:WPA2:*:lab',      # same network, stronger radio
            '55::  :open',
        ])
        # A saved connection reports nmcli's connection type, not the device type.
        profiles = '802-11-wireless:lab\n802-3-ethernet:wired'
        with patch.object(netctl, '_run', side_effect=[listing, profiles]):
            found = netctl.scan(rescan=False)
        self.assertEqual([e['ssid'] for e in found], ['lab', 'open'])
        self.assertEqual(found[0]['signal'], 80)
        self.assertTrue(found[0]['secured'])
        self.assertTrue(found[0]['known'])
        self.assertFalse(found[1]['secured'])
        self.assertFalse(found[1]['known'])

    def test_hidden_networks_are_dropped_rather_than_shown_blank(self):
        with patch.object(netctl, '_run', side_effect=['70:WPA2: :', '802-11-wireless:other']):
            self.assertEqual(netctl.scan(rescan=False), [])

    def test_connect_builds_an_argument_vector_with_no_shell(self):
        calls = []

        def fake_run(args, timeout):
            calls.append(args)
            # The device query is the only call that returns anything useful here.
            return 'wifi:wlan0' if args[-1] == 'device' else ''

        with patch.object(netctl, '_run', side_effect=fake_run):
            netctl.connect('lab', 'p a s s')
        connect_call = [c for c in calls if 'connect' in c][0]
        self.assertEqual(connect_call,
                         ['device', 'wifi', 'connect', 'lab', 'password', 'p a s s',
                          'ifname', 'wlan0'])

    def test_rejected_inputs_never_reach_nmcli(self):
        with patch.object(netctl, '_run') as run:
            for ssid in ('', 'x' * 33, 'bad\nname'):
                with self.assertRaises(netctl.NetError):
                    netctl.connect(ssid, 'pw')
            with self.assertRaises(netctl.NetError):
                netctl.connect('lab', 'x' * 64)
            with self.assertRaises(netctl.NetError):
                netctl.connect('lab', 'pw\n--rescan')
            run.assert_not_called()

    def test_authorisation_failure_is_explained_not_escalated(self):
        message = netctl._reason('Error: Not authorized to control networking.', 4)
        self.assertIn('netdev', message)

    def test_packed_scan_fits_one_frame_and_drops_rather_than_truncates(self):
        entries = [{'ssid': 'n' * 32, 'signal': 90, 'secured': True, 'known': False}
                   for _ in range(netctl.SCAN_MAX)]
        packed = netctl.pack_scan(entries)
        self.assertLessEqual(len(packed), netctl.FRAME_LIMIT)
        self.assertEqual(packed[0], netctl.SCAN_MAX)
        at = 1
        for _ in range(packed[0]):
            flags, signal, length = packed[at], packed[at + 1], packed[at + 2]
            self.assertEqual(flags, 1)
            self.assertEqual(signal, 90)
            self.assertEqual(length, 32)
            at += 3 + length
        self.assertEqual(at, len(packed))

    def test_unpack_request_rejects_a_truncated_frame(self):
        self.assertEqual(netctl.unpack_request(b'\x03lab\x02pw'), ('lab', 'pw'))
        self.assertEqual(netctl.unpack_request(b'\x03lab'), ('lab', ''))
        with self.assertRaises(netctl.NetError):
            netctl.unpack_request(b'\x09lab')
        with self.assertRaises(netctl.NetError):
            netctl.unpack_request(b'')

    def test_state_distinguishes_unknown_from_disconnected(self):
        with patch.object(netctl, '_run', side_effect=netctl.NetError('no nmcli')):
            self.assertIsNone(netctl.state())
        with patch.object(netctl, '_run', return_value=' :40:other\n*:65:lab\n'):
            self.assertEqual(netctl.state(),
                             {'connected': True, 'ssid': 'lab', 'signal': 65})
        with patch.object(netctl, '_run', return_value=' :40:other\n'):
            self.assertEqual(netctl.state(),
                             {'connected': False, 'ssid': '', 'signal': None})

    def test_helper_answers_json_for_every_verb(self):
        with patch.object(netctl, 'scan', return_value=[]):
            self.assertEqual(netctl.handle({'verb': 'scan'}), {'ok': True, 'networks': []})
        with patch.object(netctl, 'connect', side_effect=netctl.NetError('nope')):
            self.assertEqual(netctl.handle({'verb': 'connect', 'ssid': 'a'}),
                             {'ok': False, 'error': 'nope'})
        self.assertFalse(netctl.handle({'verb': 'reboot'})['ok'])
        self.assertFalse(netctl.handle('not a request')['ok'])


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
        diagnostics = tempfile.TemporaryFile(mode='w+t')
        self.addCleanup(diagnostics.close)
        process = subprocess.Popen([sys.executable, '-B', '-c',
                                    'import sys;from mixosd import serve;serve(sys.argv[1],"/bin/sh",sys.argv[2])',
                                    device, str(ROOT / 'linux' / 'apps')],
                                   env=env, stdout=subprocess.DEVNULL, stderr=diagnostics)
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
            # A cold Python import on WSL/NTFS can exceed one second while a
            # target build is active. Bound process startup separately from the
            # protocol exchange deadlines; retry HELLO as a real device does.
            startup_deadline = time.monotonic() + 10
            while process.poll() is None and time.monotonic() < startup_deadline:
                send(T.HELLO, payload=struct.pack('<HH', 512, 4096))
                if receive_until(lambda: any(f.type == T.HELLO_ACK for f in received), 0.1):
                    break
            diagnostics.seek(0)
            self.assertTrue(any(f.type == T.HELLO_ACK for f in received), diagnostics.read())
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
        shell = PtyShell(80, 28, ['/bin/sh', '-i'])
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
