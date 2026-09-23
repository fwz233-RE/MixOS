"""Capture boundary regressions, with synthetic PCM and no microphone access."""
from __future__ import annotations

import io
import os
import struct
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

APPS = Path(os.environ.get('MIXOS_TEST_APPS',
                           Path(__file__).resolve().parents[1] / 'linux/apps'))
sys.path.insert(0, str(APPS))
import audio

HEAD = struct.pack('<hh', 1100, -1100) * 1024
TAIL = struct.pack('<hh', 2300, -2300) * 137  # deliberately smaller than READ_BLOCK


class DeferredPipe:
    """A stopped producer still has a final partial block to be consumed."""
    def __init__(self):
        self.closed = False
        self.reading_tail = threading.Event()
        self.release_tail = threading.Event()
        self.calls = 0

    def read(self, size):
        self.calls += 1
        if self.calls == 1:
            return HEAD
        if self.calls == 2:
            self.reading_tail.set()
            if not self.release_tail.wait(3):
                raise OSError('test did not release the final pipe block')
            if self.closed:
                raise ValueError('pipe was closed before the last block was read')
            return TAIL
        return b''

    read1 = read

    def close(self):
        self.closed = True
        self.release_tail.set()


class CaptureProcess:
    def __init__(self, pipe=None, stderr=b''):
        self.stdout = pipe if pipe is not None else DeferredPipe()
        self.stderr = io.BytesIO(stderr)
        self.returncode = None
        self.terminated = threading.Event()

    def poll(self):
        return self.returncode

    def terminate(self):
        self.returncode = 0
        self.terminated.set()

    def kill(self):
        self.terminate()

    def wait(self, timeout=None):
        if self.returncode is None:
            self.returncode = 0
        return self.returncode


class RecorderTests(unittest.TestCase):
    def test_stop_reads_final_partial_block_before_closing_stdout(self):
        process = CaptureProcess()
        recorder = audio.Recorder()
        result, errors = [], []
        with mock.patch.object(audio, '_tool', return_value='arecord'), \
                mock.patch.object(audio.subprocess, 'Popen', return_value=process):
            recorder.start()
        self.assertTrue(process.stdout.reading_tail.wait(2))

        def stop():
            try:
                result.append(recorder.stop())
            except Exception as exc:
                errors.append(exc)

        stopper = threading.Thread(target=stop)
        stopper.start()
        try:
            self.assertTrue(process.terminated.wait(2))
            # The reader is deliberately held back after the producer exits.
            # stop must wait for it, not close its stream and discard the tail.
            self.assertFalse(process.stdout.closed, 'stop closed unread capture data')
        finally:
            process.stdout.release_tail.set()
            stopper.join(3)
        self.assertFalse(stopper.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(result, [audio.voice_channel(HEAD + TAIL)])
        self.assertEqual(result[0][-274:], struct.pack('<h', 2300) * 137)
        self.assertFalse(recorder.running)
        self.assertTrue(process.stdout.closed)

    def test_capture_uses_explicit_short_period_and_buffer(self):
        command = audio.capture_command('arecord', 'device', 16000)
        self.assertIn('--period-time=20000', command)
        self.assertIn('--buffer-time=100000', command)

    def test_limit_is_exact_and_stops_without_waiting_for_the_ui(self):
        process = CaptureProcess(io.BytesIO(HEAD + TAIL))
        recorder = audio.Recorder()
        with mock.patch.object(audio, '_tool', return_value='arecord'), \
                mock.patch.object(audio.subprocess, 'Popen', return_value=process), \
                mock.patch.object(audio, 'MAX_SECONDS', 0.01):
            recorder.start()
            recorder._thread.join(2)
            self.assertFalse(recorder.running)
            pcm = recorder.stop()
        self.assertEqual(len(pcm), 160 * 2)
        self.assertTrue(process.terminated.is_set())

    def test_natural_exit_and_stderr_are_collected(self):
        process = CaptureProcess(io.BytesIO(HEAD + TAIL),
                                 b'arecord: input/output error\n')
        process.returncode = 1
        recorder = audio.Recorder()
        with mock.patch.object(audio, '_tool', return_value='arecord'), \
                mock.patch.object(audio.subprocess, 'Popen', return_value=process):
            recorder.start()
            recorder._thread.join(2)
        self.assertFalse(recorder.running)
        self.assertEqual(recorder.stop(), audio.voice_channel(HEAD + TAIL))
        self.assertEqual(recorder.error, 'arecord: input/output error')
        self.assertTrue(process.stderr.closed)

    def test_cancel_discards_and_restart_cannot_mix_recordings(self):
        recorder = audio.Recorder()
        with mock.patch.object(audio, '_tool', return_value='arecord'), \
                mock.patch.object(audio.subprocess, 'Popen', side_effect=[
                    CaptureProcess(io.BytesIO(HEAD)), CaptureProcess(io.BytesIO(TAIL))]):
            recorder.start()
            recorder.cancel()
            recorder.start()
            self.assertEqual(recorder.stop(), audio.voice_channel(TAIL))
            self.assertEqual(recorder.stop(), audio.voice_channel(TAIL))

    def test_real_error_after_many_benign_lines_is_not_discarded(self):
        process = CaptureProcess(io.BytesIO(HEAD),
                                 b'Recording raw audio\n' * 1000 + b'arecord: input/output error\n')
        process.returncode = 0
        recorder = audio.Recorder()
        with mock.patch.object(audio, '_tool', return_value='arecord'), \
                mock.patch.object(audio.subprocess, 'Popen', return_value=process):
            recorder.start()
            recorder._thread.join(3)
            recorder.stop()
        self.assertEqual(recorder.error, 'arecord: input/output error')

    @unittest.skipIf(sys.platform == 'win32', 'POSIX arecord shutdown semantics')
    def test_stdout_eof_before_nonzero_exit_is_not_a_successful_stop(self):
        command = [sys.executable, '-u', '-c',
                   'import os, time; os.write(1, ' + repr(HEAD) + '); '
                   'os.close(1); time.sleep(0.1); os._exit(7)']
        recorder = audio.Recorder()
        with mock.patch.object(audio, '_tool', return_value=sys.executable), \
                mock.patch.object(audio, 'capture_command', return_value=command):
            recorder.start()
            recorder._thread.join(3)
            recorder.stop()
        self.assertIsNotNone(recorder.error)
        self.assertIn('status 7', recorder.error)

    @unittest.skipIf(sys.platform == 'win32', 'POSIX arecord shutdown semantics')
    def test_capacity_stop_has_its_own_kill_deadline_without_ui_stop(self):
        command = [sys.executable, '-u', '-c',
                   'import os, signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); '
                   'os.write(1, ' + repr(HEAD) + '); time.sleep(30)']
        recorder = audio.Recorder()
        with mock.patch.object(audio, '_tool', return_value=sys.executable), \
                mock.patch.object(audio, 'capture_command', return_value=command), \
                mock.patch.object(audio, 'MAX_SECONDS', 0.01), \
                mock.patch.object(audio, 'CAPTURE_STOP_TIMEOUT', 0.1):
            recorder.start()
            try:
                recorder._thread.join(2)
                self.assertFalse(recorder.running, 'capacity stop never escalated to KILL')
                self.assertIsNotNone(recorder.error)
            finally:
                recorder.cancel()

    @unittest.skipIf(sys.platform == 'win32', 'POSIX arecord shutdown semantics')
    def test_requested_stop_accepts_arecord_status_one_but_not_arbitrary_errors(self):
        for code in (1, 7):
            with self.subTest(code=code):
                command = [sys.executable, '-u', '-c',
                           'import os, signal, time\n'
                           'def stop(*args):\n'
                           ' os.write(1, ' + repr(TAIL) + ')\n'
                           f' raise SystemExit({code})\n'
                           'signal.signal(signal.SIGTERM, stop)\n'
                           'os.write(1, ' + repr(HEAD) + ')\n'
                           'while True: time.sleep(0.01)\n']
                recorder = audio.Recorder()
                with mock.patch.object(audio, '_tool', return_value=sys.executable), \
                        mock.patch.object(audio, 'capture_command', return_value=command):
                    recorder.start()
                    try:
                        deadline = time.monotonic() + 3
                        while recorder.seconds == 0 and time.monotonic() < deadline:
                            threading.Event().wait(0.005)
                        self.assertGreater(recorder.seconds, 0)
                        self.assertEqual(recorder.stop(), audio.voice_channel(HEAD + TAIL))
                        if code == 1:
                            self.assertIsNone(recorder.error)
                        else:
                            self.assertIn('status 7', recorder.error)
                    finally:
                        recorder.cancel()

    def test_failed_stop_returns_no_partial_audio_and_blocks_restart(self):
        recorder = audio.Recorder()
        process = CaptureProcess(io.BytesIO(HEAD))
        thread = mock.Mock()
        thread.is_alive.return_value = True
        recorder._process, recorder._thread = process, thread
        recorder._blocks = [HEAD]
        self.assertEqual(recorder.stop(), b'')
        self.assertIn('not submitted', recorder.error)
        self.assertIs(recorder._process, process)
        with self.assertRaises(audio.AudioUnavailable):
            recorder.start()
        thread.is_alive.return_value = False
        recorder.cancel()
        self.assertFalse(recorder.running)

    def test_stderr_cannot_block_stdout_when_a_child_reports_many_errors(self):
        command = [sys.executable, '-u', '-c',
                   'import sys; sys.stderr.buffer.write(b"device error\\n" * 30000); '
                   'sys.stderr.buffer.flush(); sys.stdout.buffer.write(' + repr(TAIL) + ')']
        recorder = audio.Recorder()
        with mock.patch.object(audio, '_tool', return_value=sys.executable), \
                mock.patch.object(audio, 'capture_command', return_value=command):
            recorder.start()
            recorder._thread.join(5)
            try:
                self.assertFalse(recorder._thread.is_alive(), 'stderr blocked capture')
                self.assertEqual(recorder.stop(), audio.voice_channel(TAIL))
                self.assertEqual(recorder.error, 'device error')
            finally:
                recorder.cancel()

    @unittest.skipIf(sys.platform == 'win32', 'POSIX arecord shutdown semantics')
    def test_a_child_ignoring_sigterm_is_killed_with_bounded_shutdown(self):
        command = [sys.executable, '-u', '-c',
                   'import os, signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); '
                   'os.write(1, ' + repr(HEAD) + '); time.sleep(30)']
        recorder = audio.Recorder()
        with mock.patch.object(audio, '_tool', return_value=sys.executable), \
                mock.patch.object(audio, 'capture_command', return_value=command), \
                mock.patch.object(audio, 'CAPTURE_STOP_TIMEOUT', 0.1):
            recorder.start()
            deadline = time.monotonic() + 3
            while recorder.seconds == 0 and time.monotonic() < deadline:
                threading.Event().wait(0.005)
            self.assertGreater(recorder.seconds, 0)
            before = time.monotonic()
            try:
                self.assertEqual(recorder.stop(), audio.voice_channel(HEAD))
                self.assertLess(time.monotonic() - before, 2)
                self.assertFalse(recorder.running)
            finally:
                recorder.cancel()

    def test_real_child_eof_keeps_an_unaligned_pipe_tail(self):
        recorder = audio.Recorder()
        payload = HEAD + TAIL
        command = [sys.executable, '-c',
                   'import sys; sys.stdout.buffer.write(' + repr(payload) + ')']
        with mock.patch.object(audio, '_tool', return_value=sys.executable), \
                mock.patch.object(audio, 'capture_command', return_value=command):
            recorder.start()
            recorder._thread.join(5)
            self.assertFalse(recorder._thread.is_alive())
            self.assertEqual(recorder.stop(), audio.voice_channel(payload))
            self.assertIsNone(recorder.error)

    @unittest.skipIf(sys.platform == 'win32', 'POSIX arecord shutdown semantics')
    def test_real_child_flushes_audio_on_sigterm(self):
        command = [sys.executable, '-u', '-c',
                   'import os, signal, time\n'
                   'def stop(*args):\n'
                   ' os.write(1, ' + repr(TAIL) + ')\n'
                   ' raise SystemExit(0)\n'
                   'signal.signal(signal.SIGTERM, stop)\n'
                   'os.write(1, ' + repr(HEAD) + ')\n'
                   'while True: time.sleep(0.01)\n']
        recorder = audio.Recorder()
        with mock.patch.object(audio, '_tool', return_value=sys.executable), \
                mock.patch.object(audio, 'capture_command', return_value=command):
            recorder.start()
            try:
                deadline = time.monotonic() + 3
                while recorder.seconds == 0 and time.monotonic() < deadline:
                    threading.Event().wait(0.005)
                self.assertGreater(recorder.seconds, 0)
                self.assertEqual(recorder.stop(), audio.voice_channel(HEAD + TAIL))
            finally:
                recorder.cancel()


if __name__ == '__main__':
    unittest.main()
