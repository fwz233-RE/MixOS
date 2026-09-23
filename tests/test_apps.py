"""Checks for the four-button interfaces: the toolkit, the audio path, the
clients for the two local services, the notes store and editor, and the
launchers that start them.

Everything here runs on any machine. Nothing needs the device, a sound card, a
model, or a network: the two service clients are exercised against a real HTTP
server started on loopback inside the test, which is the only way to find out
whether the request shapes are right without the services themselves.
"""
from __future__ import annotations

import importlib.util
import io
import json
import os
import socket
import sys
import tempfile
import threading
import types
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APPS = ROOT / 'linux/apps'
LAUNCHERS = ROOT / 'linux/launchers'

sys.path.insert(0, str(ROOT / 'linux'))
sys.path.insert(0, str(APPS))
sys.path.insert(0, str(ROOT / 'tools'))

import audio                                                  # noqa: E402
import backend                                                # noqa: E402
import tui                                                    # noqa: E402


def load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class ToolkitTests(unittest.TestCase):
    """The screen is 64 columns of a font where 汉字 take two of them."""

    def test_double_width_characters_count_as_two_columns(self):
        self.assertEqual(tui.char_width('a'), 1)
        self.assertEqual(tui.char_width('中'), 2)
        self.assertEqual(tui.char_width('\u0301'), 0)       # a combining accent
        self.assertEqual(tui.text_width('中文abc'), 7)

    def test_truncation_never_splits_a_double_width_cell(self):
        # '中中中' is six columns; five columns can hold one plus the ellipsis.
        cut = tui.truncate('中中中', 5)
        self.assertTrue(cut.endswith('…'))
        self.assertLessEqual(tui.text_width(cut), 5)

    def test_wrapping_breaks_chinese_by_column_and_english_by_word(self):
        self.assertEqual(tui.wrap('一二三四五', 4), ['一二', '三四', '五'])
        lines = tui.wrap('the quick brown fox', 10)
        self.assertTrue(all(tui.text_width(line) <= 10 for line in lines))
        self.assertIn('quick', ''.join(lines))

    def test_only_changed_cells_are_sent(self):
        """A full repaint every frame would starve the credit-limited link."""
        out = io.StringIO()
        screen = tui.Screen(out, columns=20, rows=4)
        screen.clear()
        screen.put(0, 0, 'hello')
        screen.flush()
        out.seek(0), out.truncate()
        screen.put(0, 0, 'hellp')          # one cell differs
        screen.flush()
        written = out.getvalue()
        self.assertIn('p', written)
        self.assertNotIn('hell', written)
        self.assertLess(len(written), 40)

    def test_a_trailing_cell_is_reserved_for_a_wide_glyph(self):
        screen = tui.Screen(io.StringIO(), columns=10, rows=2)
        screen.clear()
        screen.put(0, 0, '中')
        self.assertEqual(screen.back[0][0].width, 2)
        self.assertEqual(screen.back[0][1].width, 0)
        # A wide glyph that does not fit is not drawn as half of one.
        screen.put(9, 1, '中')
        self.assertEqual(screen.back[1][9].char, ' ')

    def test_size_comes_from_the_terminal_before_the_environment(self):
        """term-ime gives its child one row less than the real screen.

        Trusting MIXOS_ROWS there would draw one row too many and scroll the
        whole interface on every frame.
        """
        source = (APPS / 'tui.py').read_text(encoding='utf-8')
        self.assertIn('os.get_terminal_size', source)
        measured = tui.measure(columns=80, rows=28)
        self.assertEqual(measured, (80, 28))

    def test_the_screen_is_restored_on_the_way_out(self):
        out = io.StringIO()
        screen = tui.Screen(out, columns=10, rows=3)
        screen.enter()
        screen.leave()
        self.assertIn('\x1b[?1049l', out.getvalue())
        self.assertIn('\x1b[?25h', out.getvalue())


class AudioTests(unittest.TestCase):
    def test_the_capture_device_is_named_not_numbered(self):
        """Card numbers move when an HDMI monitor appears; the name does not."""
        self.assertIn('CARD=UACCDC', audio.DEFAULT_DEVICE)
        self.assertTrue(audio.DEFAULT_DEVICE.startswith('plughw:'),
                        'plughw lets ALSA convert the rate; hw does not')

    def test_recording_asks_for_what_the_recogniser_wants(self):
        command = audio.capture_command('/usr/bin/arecord', 'dev', 16000)
        self.assertEqual(command[0], '/usr/bin/arecord')
        self.assertIn('S16_LE', command)
        self.assertIn('16000', command)
        self.assertIn('raw', command)
        self.assertEqual(audio.SAMPLE_RATE, 16000)

    def test_recording_captures_both_channels_rather_than_asking_for_one(self):
        """Asking ALSA for mono averages two microphones; it does not pick one.

        This card captures only in stereo, and the two microphones on this board
        differ by about 9 dB of signal-to-noise. Averaging them drags the usable
        one down to the noisy one's level, which is the state in which every
        recognition request returned HTTP 200 and no text.
        """
        command = audio.capture_command('/usr/bin/arecord', 'dev', 16000)
        self.assertEqual(command[command.index('-c') + 1], '2')
        self.assertEqual(audio.CAPTURE_CHANNELS, 2)

    def test_one_microphone_is_taken_out_of_the_interleaved_capture(self):
        import array
        left = array.array('h', [100, -200, 300, -400])
        right = array.array('h', [1, 2, 3, 4])
        interleaved = array.array('h')
        for pair in zip(left, right):
            interleaved.extend(pair)

        taken = array.array('h')
        taken.frombytes(audio.voice_channel(interleaved.tobytes(), channels=2,
                                            channel=0))
        self.assertEqual(list(taken), list(left))

        taken = array.array('h')
        taken.frombytes(audio.voice_channel(interleaved.tobytes(), channels=2,
                                            channel=1))
        self.assertEqual(list(taken), list(right))

    def test_a_mono_capture_passes_through_channel_selection_unchanged(self):
        import array
        pcm = array.array('h', [1, 2, 3]).tobytes()
        self.assertEqual(audio.voice_channel(pcm, channels=1), pcm)
        self.assertEqual(audio.voice_channel(b'', channels=2), b'')

    def test_the_level_meter_reads_the_channel_that_will_be_recognised(self):
        """A bar driven by the noisy microphone reads half full in a silent room,
        which tells the person holding the device the opposite of the truth."""
        import array
        kept, discarded = 40, 8000
        interleaved = array.array('h')
        for _ in range(64):
            interleaved.extend((kept, discarded))
        measured = audio.voice_channel_rms(interleaved.tobytes(), channels=2,
                                           channel=0)
        self.assertAlmostEqual(measured, kept / 32768, places=4)
        # The averaged reading would be dominated by the channel being thrown away.
        self.assertLess(measured, audio.rms(interleaved.tobytes()))
        self.assertAlmostEqual(
            audio.voice_channel_rms(interleaved.tobytes(), channels=2, channel=1),
            discarded / 32768, places=3)

    def test_samples_convert_to_the_float_buffer_the_backend_expects(self):
        import array
        pcm = array.array('h', [0, 32767, -32768, 16384]).tobytes()
        floats = array.array('f')
        floats.frombytes(audio.pcm16_to_float32(pcm))
        self.assertEqual(len(floats), 4)
        self.assertAlmostEqual(floats[0], 0.0)
        self.assertAlmostEqual(floats[1], 32767 / 32768, places=6)
        self.assertAlmostEqual(floats[2], -1.0)
        self.assertAlmostEqual(floats[3], 0.5)

    def test_an_odd_trailing_byte_is_dropped_rather_than_misread(self):
        self.assertEqual(audio.pcm16_to_float32(b'\x00'), b'')

    def test_being_told_to_stop_is_not_a_recording_failure(self):
        """Stopping a recording sends SIGTERM to a process blocked in read, and
        ALSA reports the interrupted syscall. Treating that as an error threw
        away every recording the device made: the interface showed the message
        and never sent the audio to be recognised, so speech recognition looked
        broken while every part of it worked."""
        self.assertIsNone(audio.complaint(
            'arecord: pcm_read:2272: read error: Interrupted system call'))
        self.assertIsNone(audio.complaint(''))
        self.assertIsNone(audio.complaint('Recording raw data \'stdin\' : Signed 16 bit'))

    def test_a_real_fault_is_still_reported(self):
        for line in ('arecord: main:830: audio open error: No such file or directory',
                     'arecord: pcm_read:2181: read error: Input/output error'):
            self.assertEqual(audio.complaint(line), line)

    def test_a_fault_after_a_benign_line_is_not_hidden_by_it(self):
        """The old code read splitlines()[0] and stopped, so a real problem
        behind the banner was invisible."""
        self.assertEqual(
            audio.complaint('Recording raw data\n'
                            'arecord: pcm_read:2181: read error: Input/output error'),
            'arecord: pcm_read:2181: read error: Input/output error')

    def test_the_level_meter_is_bounded(self):
        import array
        self.assertEqual(audio.rms(b''), 0.0)
        loud = array.array('h', [32767, -32768] * 1000).tobytes()
        self.assertLessEqual(audio.rms(loud), 1.0)
        self.assertGreater(audio.rms(loud), 0.9)
        quiet = array.array('h', [0] * 1000).tobytes()
        self.assertEqual(audio.rms(quiet), 0.0)

    def test_a_missing_device_is_described_rather_than_raised(self):
        """The interface puts this on the screen before anything is recorded."""
        message = audio.describe_device('plughw:CARD=NOSUCHCARD,DEV=0')
        self.assertIsInstance(message, str)
        self.assertTrue(message)

    # -- level, and why recordings are scaled before being recognised --------
    @staticmethod
    def _tone(peak: float, rms: float, samples: int = 16000) -> bytes:
        """A tone at a chosen loudness with one sample at a chosen peak."""
        import array
        import math
        amplitude = rms * math.sqrt(2)
        data = array.array('h', [int(amplitude * 32768 * math.sin(2 * math.pi * 220 * i / 16000))
                                 for i in range(samples)])
        data[0] = int(peak * 32767)
        return data.tobytes()

    def test_levels_reports_the_exact_peak_and_a_usable_loudness(self):
        import array
        self.assertEqual(audio.levels(b''), (0.0, 0.0))
        pcm = array.array('h', [0, 16384, -16384, 0] * 500).tobytes()
        peak, rms = audio.levels(pcm)
        self.assertAlmostEqual(peak, 0.5, places=3)
        self.assertGreater(rms, 0.3)
        self.assertLess(rms, 0.4)

    def test_a_recording_too_quiet_to_recognise_is_scaled_up(self):
        """Measured on typixdeck on 2026-09-14 with the left microphone and the
        ES8389 PGA at 36 dB, which is what recordings go through.

        A voice arrives with a peak near 0.29 and an rms near 0.022 where the
        recogniser wants roughly 0.08, so even the loudest of these still needs
        scaling. The quieter rows are a voice further from the device.

        The levels in this test were four times lower before the analogue gain
        was raised, and at those levels the recogniser returned an empty string
        rather than an error - which is what "speech recognition does not work"
        looked like from the outside.
        """
        for peak, rms in ((0.080, 0.0060), (0.150, 0.0120),
                          (0.230, 0.0180), (0.290, 0.0222)):
            with self.subTest(peak=peak):
                measured_peak, measured_rms = audio.levels(self._tone(peak, rms))
                gain = audio.recognition_gain(measured_peak, measured_rms)
                self.assertGreater(gain, 3.0)
                # Loud enough to be recognised, and not clipped getting there.
                self.assertAlmostEqual(measured_rms * gain, audio.TARGET_RMS,
                                       delta=audio.TARGET_RMS * 0.2)
                self.assertLessEqual(measured_peak * gain, 1.0)

    def test_scaling_never_clips_and_never_makes_anything_quieter(self):
        for peak, rms in ((0.010, 0.0022), (0.052, 0.0039), (0.60, 0.080),
                          (1.00, 0.300)):
            with self.subTest(peak=peak):
                measured_peak, measured_rms = audio.levels(self._tone(peak, rms))
                gain = audio.recognition_gain(measured_peak, measured_rms)
                self.assertGreaterEqual(gain, 1.0)
                self.assertLessEqual(measured_peak * gain, 1.0)
                self.assertLessEqual(gain, audio.MAX_GAIN)

    def test_a_recording_that_is_already_loud_enough_is_left_alone(self):
        peak, rms = audio.levels(self._tone(0.60, audio.TARGET_RMS))
        self.assertEqual(audio.recognition_gain(peak, rms), 1.0)

    def test_an_empty_room_is_not_amplified_into_invented_words(self):
        """Scaling noise up hands the recogniser something it will answer.

        The room levels here are what the left microphone measures with the PGA
        at 36 dB: three takes peaked at 0.027, 0.031 and 0.031. The threshold has
        to sit above that and below the 0.29 a voice reaches, and it is tied to
        both the analogue gain and the channel selection - raising the PGA
        without moving the threshold left silence classified as speech.
        """
        for peak, rms in ((0.010, 0.0022), (0.027, 0.0072), (0.031, 0.0075)):
            with self.subTest(peak=peak):
                measured_peak, measured_rms = audio.levels(self._tone(peak, rms))
                self.assertEqual(audio.recognition_gain(measured_peak, measured_rms), 1.0)
                self.assertTrue(audio.too_quiet(measured_peak))
        # And a real voice is not mistaken for an empty room.
        for peak, rms in ((0.080, 0.0060), (0.290, 0.0222)):
            with self.subTest(voice=peak):
                loud_peak, _ = audio.levels(self._tone(peak, rms))
                self.assertFalse(audio.too_quiet(loud_peak))

    def test_a_capture_with_no_microphone_in_it_is_named_as_such(self):
        """The fault measured on typixdeck on 2026-09-14.

        Five seconds from the ESP32's USB audio card in which every one of
        240000 samples was exactly -1, on both channels, while the deck's own
        speaker played a tone into the room. A microphone cannot produce that;
        an undriven I2S data line held high by a pull-up can, and so can the
        zero-fill the firmware substitutes when it cannot read the codec.

        Both have to be told apart from a quiet room, because the recogniser
        answers all three with an empty transcript and the interfaces then said
        the only thing that was certainly wrong: stand closer.
        """
        import array
        for stuck in (-1, 0, 32767):
            with self.subTest(value=stuck):
                dead = array.array('h', [stuck] * 4096).tobytes()
                self.assertTrue(audio.no_signal(dead))
        self.assertTrue(audio.no_signal(b''))

    def test_a_silent_room_is_not_mistaken_for_a_dead_microphone(self):
        """The room alone spans hundreds of counts; that is a live capture.

        Getting this backwards would replace a true "nobody spoke" with a false
        "the device is broken", which sends the person to reboot for nothing.
        """
        for peak, rms in ((0.010, 0.0022), (0.027, 0.0072), (0.290, 0.0222)):
            with self.subTest(peak=peak):
                self.assertFalse(audio.no_signal(self._tone(peak, rms)))


        import array
        quiet = self._tone(0.290, 0.0222)
        floats = array.array('f')
        floats.frombytes(audio.for_recognition(quiet))
        self.assertEqual(len(floats), len(quiet) // 2)
        self.assertLessEqual(max(abs(value) for value in floats), 1.0)
        # The same audio converted faithfully stays where it was captured. The
        # multiple is three rather than the ten it was before the analogue gain
        # went up: raising the PGA is what left less for software to do.
        faithful = array.array('f')
        faithful.frombytes(audio.pcm16_to_float32(quiet))
        self.assertGreater(max(abs(v) for v in floats),
                          max(abs(v) for v in faithful) * 3)
        self.assertEqual(audio.for_recognition(b''), b'')

    def test_the_meter_moves_at_the_levels_this_microphone_produces(self):
        """Drawn from the raw loudness the bar never left its left-hand end, so
        a working microphone and a dead one looked the same."""
        self.assertEqual(audio.meter(0.0), 0.0)
        self.assertGreater(audio.meter(0.028), 0.4)      # a voice, as measured
        self.assertGreater(audio.meter(0.05), audio.meter(0.028))
        self.assertEqual(audio.meter(audio.TARGET_RMS), 1.0)
        self.assertEqual(audio.meter(1.0), 1.0)     # bounded

    def test_a_silent_room_draws_an_empty_bar(self):
        """The room measures an rms near 0.0074 on this device. Drawn against
        TARGET_RMS alone that filled a third of the bar in complete silence,
        which reads as "your voice is arriving" when nobody has spoken."""
        for room in (0.0, 0.0072, 0.0075, audio.NOISE_RMS):
            with self.subTest(room=room):
                self.assertEqual(audio.meter(room), 0.0)
        self.assertGreater(audio.meter(0.028), 0.0)

    def test_both_interfaces_send_scaled_audio_to_be_recognised(self):
        """A faithful conversion here is the bug this had; name it in the code."""
        for name in ('notes/app.py', 'translator/app.py'):
            with self.subTest(app=name):
                source = (APPS / name).read_text(encoding='utf-8')
                self.assertIn('audio.for_recognition(pcm)', source)
                self.assertNotIn('audio.pcm16_to_float32(pcm)', source)
                # And say which of the two empty answers it was.
                self.assertIn('audio.too_quiet(peak)', source)
                # A capture with no microphone in it is neither of those two,
                # and must not be reported as the person being too quiet.
                self.assertIn('audio.no_signal(pcm)', source)
                self.assertIn('audio.NO_MICROPHONE', source)


class FakeService(HTTPServer):
    """A loopback stand-in for whichever service the test is about."""

    def __init__(self, handler):
        super().__init__(('127.0.0.1', 0), handler)
        self.requests: list[tuple[str, str, bytes]] = []
        self.thread = threading.Thread(target=self.serve_forever, daemon=True)
        self.thread.start()

    def stop(self):
        self.shutdown()
        self.server_close()
        self.thread.join(timeout=5)


def recording_handler(responder):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def _record(self):
            length = int(self.headers.get('Content-Length', 0))
            body = self.rfile.read(length) if length else b''
            self.server.requests.append((self.command, self.path, body))
            return body

        def do_GET(self):
            responder(self, self._record())

        def do_POST(self):
            responder(self, self._record())
    return Handler


class SpeechClientTests(unittest.TestCase):
    def respond(self, handler, body):
        payload = json.dumps({'text': '你好'}).encode()
        handler.send_response(200)
        handler.send_header('Content-Type', 'application/json')
        handler.send_header('Content-Length', str(len(payload)))
        handler.end_headers()
        handler.wfile.write(payload)

    def setUp(self):
        self.service = FakeService(recording_handler(self.respond))
        self.addCleanup(self.service.stop)
        self.speech = backend.Speech('127.0.0.1', self.service.server_port)

    def test_audio_is_sent_base64_encoded_with_the_language(self):
        import base64
        self.assertEqual(self.speech.transcribe(b'\x00\x00\x80\x3f', 'zh'), '你好')
        method, path, body = self.service.requests[0]
        self.assertEqual((method, path), ('POST', '/api/stt'))
        sent = json.loads(body)
        self.assertEqual(sent['language'], 'zh')
        self.assertEqual(base64.b64decode(sent['audio_base64']), b'\x00\x00\x80\x3f')

    def test_empty_audio_never_reaches_the_service(self):
        self.assertEqual(self.speech.transcribe(b'', 'en'), '')
        self.assertFalse(self.service.requests)

    def test_synthesis_puts_the_text_in_the_query_not_the_path(self):
        self.speech.synthesize('hello & goodbye', 'en')
        _, path, _ = self.service.requests[0]
        self.assertTrue(path.startswith('/api/tts?'))
        self.assertIn('lang=en', path)
        self.assertNotIn(' ', path)          # properly encoded, not concatenated

    def test_a_service_that_is_not_running_is_named_in_the_message(self):
        with socket.socket() as probe:       # a port nothing is listening on
            probe.bind(('127.0.0.1', 0))
            port = probe.getsockname()[1]
        speech = backend.Speech('127.0.0.1', port)
        with self.assertRaises(backend.ServiceError) as caught:
            speech.transcribe(b'\x00\x00\x00\x00')
        self.assertIn('not running', str(caught.exception))

    def test_a_model_that_was_never_staged_is_reported_as_a_missing_language(self):
        """The backend fails a name lookup inside its own request handler.

        Its first line is a 200-character urllib3 message; truncated onto a
        64-column screen it says nothing anybody can act on. What it means is
        one sentence long.
        """
        def refuse(handler, body):
            payload = (b"HTTPSConnectionPool(host='download.moonshine.ai', "
                       b"port=443): Max retries exceeded with url: "
                       b"/model/base-ja/quantized/base-ja/encoder_model.ort "
                       b"(Caused by NameResolutionError(...))")
            handler.send_response(500)
            handler.send_header('Content-Length', str(len(payload)))
            handler.end_headers()
            handler.wfile.write(payload)

        service = FakeService(recording_handler(refuse))
        self.addCleanup(service.stop)
        speech = backend.Speech('127.0.0.1', service.server_port)
        with self.assertRaises(backend.ServiceError) as caught:
            speech.transcribe(b'\x00\x00\x00\x00', 'ja')
        message = str(caught.exception)
        self.assertIn('not installed', message)
        self.assertNotIn('moonshine.ai', message)
        self.assertLess(tui.text_width(message), 62)


class LanguageModelTests(unittest.TestCase):
    """Streaming is the reason this talks to the model directly.

    The vendored backend's proxy reads the whole response before returning it,
    so a translation through it would appear only when it was finished.
    """

    def stream(self, handler, body):
        chunks = ['Hel', 'lo', ' world']
        handler.send_response(200)
        handler.send_header('Content-Type', 'text/event-stream')
        handler.end_headers()
        for piece in chunks:
            event = json.dumps({'choices': [{'delta': {'content': piece}}]})
            handler.wfile.write(f'data: {event}\n\n'.encode())
            handler.wfile.flush()
        handler.wfile.write(b'data: [DONE]\n\n')

    def setUp(self):
        self.service = FakeService(recording_handler(self.stream))
        self.addCleanup(self.service.stop)
        self.model = backend.LanguageModel('127.0.0.1', self.service.server_port,
                                           name='test-model')

    def test_tokens_arrive_one_at_a_time_and_add_up(self):
        seen = []
        answer = self.model.complete([{'role': 'user', 'content': 'hi'}],
                                     on_token=seen.append)
        self.assertEqual(answer, 'Hello world')
        self.assertEqual(seen, ['Hel', 'lo', ' world'])

    def test_the_request_asks_for_streaming(self):
        self.model.complete([{'role': 'user', 'content': 'hi'}])
        _, path, body = self.service.requests[0]
        self.assertEqual(path, '/v1/chat/completions')
        sent = json.loads(body)
        self.assertTrue(sent['stream'])
        self.assertEqual(sent['model'], 'test-model')
        self.assertEqual(sent['messages'][0]['content'], 'hi')

    def test_a_cancelled_generation_raises_rather_than_returning_half(self):
        cancel = threading.Event()
        cancel.set()
        with self.assertRaises(backend.Cancelled):
            self.model.complete([{'role': 'user', 'content': 'hi'}], cancel=cancel)

    def test_both_chunk_shapes_are_understood(self):
        delta = {'choices': [{'delta': {'content': 'a'}}]}
        whole = {'choices': [{'message': {'content': 'b'}}]}
        legacy = {'choices': [{'text': 'c'}]}
        self.assertEqual(backend.LanguageModel._content(delta), 'a')
        self.assertEqual(backend.LanguageModel._content(whole), 'b')
        self.assertEqual(backend.LanguageModel._content(legacy), 'c')
        self.assertEqual(backend.LanguageModel._content({'choices': []}), '')

    def test_event_lines_that_are_not_data_are_ignored(self):
        self.assertIsNone(backend.LanguageModel._event(b': keep-alive\n'))
        self.assertIsNone(backend.LanguageModel._event(b'\n'))
        self.assertEqual(backend.LanguageModel._event(b'data: [DONE]'), '')

    def test_the_model_never_answers_two_things_at_once(self):
        """One generation at a time: a 4 GiB machine has room for one."""
        self.assertTrue(self.model._lock.acquire())
        try:
            with self.assertRaises(backend.ServiceError):
                self.model.complete([{'role': 'user', 'content': 'hi'}])
        finally:
            self.model._lock.release()


class NotesStoreTests(unittest.TestCase):
    def setUp(self):
        self.store_module = load('mixos_notes_store', APPS / 'notes/store.py')
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.store = self.store_module.Store(self.temporary.name)

    def test_a_note_is_written_whole_or_not_at_all(self):
        """The rename is atomic; the usual way this device stops is the cable."""
        source = (APPS / 'notes/store.py').read_text(encoding='utf-8')
        self.assertIn('os.replace', source)
        self.assertIn('os.fsync', source)

    def test_round_trip(self):
        name = self.store.new_name()
        self.store.write(name, '第一行\n第二行\n')
        self.assertEqual(self.store.read(name), '第一行\n第二行\n')
        self.assertEqual([note.name for note in self.store.list()], [name])
        self.assertEqual(self.store.list()[0].title(), '第一行')

    def test_names_that_escape_the_directory_are_refused(self):
        for bad in ('../secret', 'a/b', '', '.hidden', 'x' * 65, 'a b'):
            with self.assertRaises(ValueError, msg=bad):
                self.store.path_for(bad)

    def test_a_missing_note_reads_as_empty_rather_than_raising(self):
        self.assertEqual(self.store.read('20000101-000000'), '')

    def test_newest_first(self):
        first, second = self.store.new_name(), None
        self.store.write(first, 'one')
        os.utime(self.store.path_for(first), (1, 1))
        second = self.store.new_name()
        self.store.write(second, 'two')
        self.assertEqual([note.name for note in self.store.list()], [second, first])


class NotesEditorTests(unittest.TestCase):
    def setUp(self):
        self.app = load('mixos_notes_app', APPS / 'notes/app.py')

    def test_wrapping_reports_where_each_row_started(self):
        rows = self.app.layout(['一二三四', 'ab'], 4)
        self.assertEqual([(r[0], r[1], r[2]) for r in rows],
                         [(0, 0, '一二'), (0, 2, '三四'), (1, 0, 'ab')])

    def test_an_empty_line_still_occupies_a_row(self):
        self.assertEqual(self.app.layout(['', 'a'], 8), [(0, 0, ''), (1, 0, 'a')])

    def test_typing_inserts_at_the_cursor(self):
        buffer = self.app.Buffer('hello')
        buffer.column = 5
        buffer.insert_text('!')
        self.assertEqual(buffer.text(), 'hello!')
        self.assertEqual(buffer.column, 6)
        self.assertTrue(buffer.modified)

    def test_recognised_speech_can_be_several_lines(self):
        buffer = self.app.Buffer('ab')
        buffer.column = 1
        buffer.insert_text('X\nY')
        self.assertEqual(buffer.text(), 'aX\nYb')
        self.assertEqual((buffer.row, buffer.column), (1, 1))

    def test_backspace_joins_lines_and_stops_at_the_start(self):
        buffer = self.app.Buffer('ab\ncd')
        buffer.row, buffer.column = 1, 0
        buffer.backspace()
        self.assertEqual(buffer.text(), 'abcd')
        self.assertEqual((buffer.row, buffer.column), (0, 2))
        buffer.row, buffer.column = 0, 0
        buffer.backspace()
        self.assertEqual(buffer.text(), 'abcd')

    def test_the_cursor_maps_onto_the_wrapped_row_it_is_on(self):
        buffer = self.app.Buffer('一二三四')
        buffer.column = 3
        rows = self.app.layout(buffer.lines, 4)
        row, cells = buffer.visual(rows)
        self.assertEqual(row, 1)
        self.assertEqual(cells, 2)          # one wide glyph into the second row

    def test_moving_down_a_wrapped_line_keeps_the_column(self):
        buffer = self.app.Buffer('一二三四')
        buffer.column = 0
        rows = self.app.layout(buffer.lines, 4)
        buffer.move_visual(rows, 1)
        self.assertEqual(buffer.column, 2)

    def test_every_editor_command_is_a_control_key(self):
        """term-ime converts printable keys into 汉字 before this sees them.

        A printable command key would be unreachable while writing Chinese.
        """
        source = (APPS / 'notes/app.py').read_text(encoding='utf-8')
        editor = source.split('def _edit_key', 1)[1].split('def run', 1)[0]
        for command in ("'ctrl-s'", "'ctrl-r'", "'ctrl-q'", "'ctrl-k'", "'ctrl-t'"):
            self.assertIn(command, editor)
        self.assertIn("len(name) == 1 and name >= ' '", editor)

    def test_the_list_is_usable_with_the_input_method_in_chinese_mode(self):
        """Coming back from the editor, N, D and Q are pinyin, not commands.

        The editor was written for term-ime and the list was not: it took
        printable letters, so anybody who had switched to Chinese to write a note
        arrived back at a list whose every command composed a syllable instead.
        The control keys do the same three things and always arrive.
        """
        notes = self._notes()
        self.assertTrue(notes.key('ctrl-n'))
        self.assertEqual(notes.mode, 'edit')

    def test_ctrl_q_returns_one_level_then_exits_from_root(self):
        notes = self._editor()
        notes.buffer.insert_text('保存后返回')
        name = notes.name
        self.assertTrue(notes.key('ctrl-q'))
        self.assertEqual(notes.mode, 'list')
        self.assertEqual(notes.store.read(name), '保存后返回')
        self.assertFalse(notes.key('ctrl-q'))
        self.assertEqual(notes.mode, 'list')

    def test_back_at_list_root_exits(self):
        notes = self._notes()
        self.assertFalse(notes.key('ctrl-q'))
        self.assertFalse(notes.key('escape'))
        self.assertFalse(notes.key('ctrl-c'))

    def test_back_dismisses_delete_confirmation_before_exiting(self):
        notes = self._notes()
        notes.confirm_delete = True
        self.assertTrue(notes.key('ctrl-q'))
        self.assertFalse(notes.confirm_delete)
        self.assertFalse(notes.key('ctrl-q'))

    def test_ctrl_d_asks_before_deleting_and_enter_confirms(self):
        """'Y' is the start of a syllable in Chinese mode; Enter is not."""
        notes = self._notes()
        notes.store.write('first', 'one')
        notes.notes = notes.store.list()
        notes.key('ctrl-d')
        self.assertTrue(notes.confirm_delete)
        self.assertIn('Enter', notes.status)
        notes.key('enter')
        self.assertEqual(notes.store.list(), [])

    def test_the_letters_still_work_without_an_input_method(self):
        """A device with no term-ime has no reason to lose the short keys."""
        notes = self._notes()
        self.assertTrue(notes.key('n'))
        self.assertEqual(notes.mode, 'edit')

    def test_the_list_footer_advertises_the_keys_that_always_arrive(self):
        source = (APPS / 'notes/app.py').read_text(encoding='utf-8')
        footer = source.split('def _footer', 1)[1].split('def key', 1)[0]
        for hint in ('^N 新建', '^D 删除', '^Q 桌面'):
            self.assertIn(hint, footer)

    def test_recognition_language_can_be_changed_from_the_editor(self):
        """A recogniser is one model per language, so the language is a setting.

        Fixed at 'zh' this looked like broken recognition to anybody speaking
        English: the Chinese model answered, with nothing useful in it.
        """
        notes = self._editor()
        self.assertEqual(notes.speech_language, 'zh')
        notes.key('ctrl-t')
        self.assertEqual(notes.speech_language, 'en')
        self.assertIn('English', notes.status)
        # Round trip: the list is walked, not toggled between two fixed values.
        for _ in self.app.SPEECH_LANGUAGES:
            notes.key('ctrl-t')
        self.assertEqual(notes.speech_language, 'en')

    def test_every_offered_language_is_one_the_device_has_a_model_for(self):
        """Offering a language whose model was never staged offers a failure.

        mixos-aiserver.service runs with IPAddressDeny=any, so the backend
        cannot fetch a recogniser at request time. tools/stage_speech.py is the
        authority on what is on the device.
        """
        import stage_speech

        offered = [code for code, _ in self.app.SPEECH_LANGUAGES]
        self.assertEqual(offered, sorted(set(offered), key=offered.index))
        self.assertTrue(set(offered) <= set(stage_speech.MODELS),
                        (offered, sorted(stage_speech.MODELS)))

    def test_the_language_in_use_is_read_once_per_recognition(self):
        """Ctrl-T during recognition must not relabel audio already sent."""
        source = (APPS / 'notes/app.py').read_text(encoding='utf-8')
        worker = source.split('def _recognise', 1)[1].split('def drain', 1)[0]
        self.assertIn('language = self.speech_language', worker)
        self.assertNotIn('self.speech_language)', worker)

    def test_an_ended_session_writes_the_note_before_exiting(self):
        """mixosd sends SIGHUP when the person leaves this application.

        Python's default action for SIGHUP is to die at once, which would lose
        everything typed since the last autosave.
        """
        source = (APPS / 'notes/app.py').read_text(encoding='utf-8')
        self.assertIn('signal.SIGHUP', source)
        self.assertIn('finally:', source.split('def run', 1)[1])

        notes = self._editor()
        notes.buffer.insert_text('unsaved words')

        def interrupt(_timeout):
            raise self.app.Interrupted()

        keyboard = types.SimpleNamespace(read=interrupt)
        with self.assertRaises(self.app.Interrupted):
            notes.run(keyboard)
        self.assertEqual(notes.store.read(notes.name), 'unsaved words')

    def _notes(self):
        """The interface on its list, with an empty store of its own."""
        directory = tempfile.TemporaryDirectory(prefix='mixos-notes-')
        self.addCleanup(directory.cleanup)
        store_module = load('mixos_notes_store_list', APPS / 'notes/store.py')
        store = store_module.Store(directory.name)
        store.ensure()
        screen = tui.Screen(io.StringIO(), columns=64, rows=22)
        return self.app.Notes(screen, store)

    def _editor(self):
        directory = tempfile.TemporaryDirectory(prefix='mixos-notes-')
        self.addCleanup(directory.cleanup)
        store_module = load('mixos_notes_store_editor', APPS / 'notes/store.py')
        store = store_module.Store(directory.name)
        store.ensure()
        screen = tui.Screen(io.StringIO(), columns=64, rows=22)
        notes = self.app.Notes(screen, store)
        notes.create()
        return notes


    def test_editor_uses_a_fixed_header_and_high_contrast_body(self):
        source = (APPS / 'notes/app.py').read_text(encoding='utf-8')
        editor = source.split('def _draw_editor', 1)[1].split('def _meter', 1)[0]
        self.assertIn("heading = '笔记'", editor)
        self.assertIn('metadata = f\'{self.name}.md', editor)
        self.assertIn('self.screen.put(1, EDITOR_BODY_Y + offset, rows[index][2], INK,', editor)
        self.assertNotIn('title = self.buffer.lines[0]', editor)
        self.assertIn('NOTES_BG = 15', source)
        self.assertIn('NOTES_INK = 0', source)
        self.assertIn('BODY_ATTR = tui.BOLD', source)

    def test_editor_draw_does_not_promote_the_first_line(self):
        for columns, rows in ((48, 15), (64, 20), (80, 27)):
            with self.subTest(columns=columns):
                notes = self._editor()
                notes.screen = tui.Screen(io.StringIO(), columns=columns, rows=rows)
                notes.name = '20260922-012345'
                notes.note_modified = 1789992000
                notes.buffer = self.app.Buffer('正文第一行\n正文第二行')
                notes.draw()
                lines = [''.join(cell.char for cell in row)
                         for row in notes.screen.back]
                self.assertIn('笔记', lines[0])
                self.assertIn('20260922-012345.md', lines[0])
                self.assertNotIn('正文', lines[0])
                stamp = self.app.time.strftime('%Y-%m-%d %H:%M',
                    self.app.time.localtime(notes.note_modified))
                self.assertIn(stamp, lines[1])
                self.assertFalse(lines[2].strip())
                self.assertIn('正文第一行', lines[3])
                self.assertIn('正文第二行', lines[4])
                self.assertIn('^Q 返回', lines[-1])
                self.assertEqual(notes.screen._cursor, (1, 3))
                for row in notes.screen.back[:5]:
                    for cell in row:
                        self.assertEqual(cell.bg, self.app.NOTES_BG)
                        if cell.char.strip():
                            self.assertEqual(cell.fg, self.app.NOTES_INK)
                            self.assertTrue(cell.attr & tui.BOLD)

    def test_large_font_editor_scroll_and_page_keep_cursor_in_body(self):
        notes = self._editor()
        notes.screen = tui.Screen(io.StringIO(), columns=48, rows=15)
        notes.buffer = self.app.Buffer('\n'.join(f'第{i}行' for i in range(40)))
        for key in ('pagedown', 'pagedown', 'pagedown', 'pageup', 'pageup'):
            self.assertTrue(notes.key(key))
            notes.draw()
            x, y = notes.screen._cursor
            self.assertGreaterEqual(y, self.app.EDITOR_BODY_Y)
            self.assertLess(y, notes.screen.rows - 2)
            self.assertGreaterEqual(x, 1)


class TranslatorInterfaceTests(unittest.TestCase):
    def setUp(self):
        self.app = load('mixos_translator_app', APPS / 'translator/app.py')
        self.screen = tui.Screen(io.StringIO(), columns=64, rows=22)
        self.translator = self.app.Translator(self.screen)

    def test_the_model_is_told_to_translate_and_nothing_else(self):
        self.translator.source_language = 'zh'
        self.translator.target_language = 'en'
        messages = self.translator._prompt('你好')
        self.assertEqual(messages[1], {'role': 'user', 'content': '你好'})
        system = messages[0]['content']
        self.assertIn('Chinese', system)
        self.assertIn('English', system)
        self.assertIn('nothing else', system)

    def test_only_languages_this_device_has_models_for_are_offered(self):
        """The list used to hold six and the device was staged for two.

        Recognition and synthesis are one model per language, and
        mixos-aiserver.service runs with IPAddressDeny=any, so a language that
        was never staged answers HTTP 500 from a failed name lookup rather than
        answering slowly. One press of `l` reached the first such language.
        """
        import stage_speech

        offered = [code for code, _ in self.app.LANGUAGES]
        self.assertEqual(offered, ['zh', 'en'])
        # Recognition and synthesis are staged separately and both have to be
        # there, or half the interface works for that language.
        self.assertTrue(set(offered) <= set(stage_speech.MODELS),
                        (offered, sorted(stage_speech.MODELS)))
        self.assertTrue(set(offered) <= set(stage_speech.TTS_ASSETS),
                        (offered, sorted(stage_speech.TTS_ASSETS)))
        # Everything offered is also something the vendored backend knows.
        vendored = (ROOT / 'linux/apps/translator/vendor/server.py').read_text(
            encoding='utf-8')
        for code in offered:
            self.assertIn(f'"{code}"', vendored)
        # The model is prompted with language names, so every offered code must
        # have one; a code in the prompt would be a worse translation.
        for code in offered:
            self.assertIn(code, self.app.LANGUAGE_NAMES)

    def test_a_device_staged_with_more_languages_can_say_so(self):
        """Adding a language is a deployment change, not a code change."""
        os.environ['MIXOS_TRANSLATE_LANGUAGES'] = 'en,ja,en'
        try:
            app = load('mixos_translator_app_more', APPS / 'translator/app.py')
        finally:
            del os.environ['MIXOS_TRANSLATE_LANGUAGES']
        # Order preserved, duplicates dropped, labels resolved.
        self.assertEqual(app.LANGUAGES, [('en', 'English'), ('ja', '日本語')])

    def test_a_direction_naming_an_uninstalled_language_is_corrected(self):
        """MIXOS_TRANSLATE_FROM used to be trusted and failed on first use."""
        os.environ['MIXOS_TRANSLATE_FROM'] = 'ja'
        os.environ['MIXOS_TRANSLATE_TO'] = 'ko'
        try:
            translator = self.app.Translator(
                tui.Screen(io.StringIO(), columns=64, rows=22))
        finally:
            del os.environ['MIXOS_TRANSLATE_FROM']
            del os.environ['MIXOS_TRANSLATE_TO']
        offered = [code for code, _ in self.app.LANGUAGES]
        self.assertIn(translator.source_language, offered)
        self.assertIn(translator.target_language, offered)
        self.assertNotEqual(translator.source_language, translator.target_language)

    def test_cycling_a_language_never_leaves_both_sides_the_same(self):
        """With two installed, the first press used to make the pair useless.

        It reported "both sides are the same language" and stayed there, so the
        interface had to be restarted to translate anything again.
        """
        for key in ('l', 'L', 'l', 'l', 'L'):
            self.translator.key(key)
            self.assertNotEqual(self.translator.source_language,
                                self.translator.target_language, key)
            offered = [code for code, _ in self.app.LANGUAGES]
            self.assertIn(self.translator.source_language, offered)
            self.assertIn(self.translator.target_language, offered)
        self.assertNotIn('same language', self.translator.status)

    def test_tab_swaps_the_direction(self):
        self.translator.source_language, self.translator.target_language = 'zh', 'en'
        self.translator.key('tab')
        self.assertEqual((self.translator.source_language,
                          self.translator.target_language), ('en', 'zh'))

    def test_the_key_hints_always_include_how_to_leave(self):
        """The hints were one 70-cell string truncated onto a 64-cell screen.

        The line ended mid-word and 'Q 退出' was never drawn, so there was no
        way to find out how to close the interface.
        """
        for columns in (64, 80):
            with self.subTest(columns=columns):
                line = self.translator.hints(columns - 2)
                self.assertLessEqual(tui.text_width(line), columns - 2)
                self.assertIn('Q 退出', line)
                self.assertIn('空格 录音', line)
                self.assertIn('Esc 返回', line)
        # A wider screen spends the room on the hints that were dropped.
        self.assertIn('R 重放', self.translator.hints(78))
        self.assertNotIn('R 重放', self.translator.hints(62))

    def test_the_speaking_hint_follows_the_setting(self):
        self.translator.speak_result = True
        self.assertIn('朗读:开', self.translator.hints(62))
        self.translator.speak_result = False
        self.assertIn('朗读:关', self.translator.hints(62))

    def test_tokens_accumulate_and_the_finished_line_is_archived(self):
        self.translator.events.put(('heard', '你好'))
        self.translator.events.put(('token', 'Hel'))
        self.translator.events.put(('token', 'lo'))
        self.translator.drain()
        self.assertEqual(self.translator.current.target, 'Hello')
        self.translator.events.put(('translated', 'Hello'))
        self.translator.drain()
        self.assertEqual(len(self.translator.history), 1)
        self.assertEqual(self.translator.history[0].source, '你好')
        self.assertEqual(self.translator.state, 'idle')

    def test_a_failure_is_kept_as_text_and_never_raised_at_the_screen(self):
        self.translator.events.put(('failed', 'the language model is not running'))
        self.translator.drain()
        self.assertEqual(self.translator.state, 'idle')
        self.assertIn('not running', self.translator.status)
        self.assertEqual(self.translator.status_kind, 'bad')

    def test_the_transcript_is_bounded(self):
        for index in range(self.app.HISTORY_LIMIT + 10):
            self.translator.current = self.app.Exchange(str(index), str(index))
            self.translator._archive()
        self.assertEqual(len(self.translator.history), self.app.HISTORY_LIMIT)

    def test_drawing_a_full_screen_does_not_raise(self):
        self.translator.history = [self.app.Exchange('一' * 200, 'x' * 200)]
        self.translator.current = self.app.Exchange('中' * 50, 'y' * 300)
        self.translator.state = 'translating'
        self.translator.draw()          # the layout must survive overflow
        self.translator.state = 'recording'
        self.translator.draw()


class LauncherTests(unittest.TestCase):
    """One executable per button, taking no arguments, in mixosd's app dir."""

    def names(self):
        return sorted(path.name for path in LAUNCHERS.iterdir() if path.is_file())

    def test_there_is_one_launcher_per_allow_listed_application(self):
        allowed = set(load('mixos_mixosd_for_apps', ROOT / 'linux/mixosd.py').APP_NAMES)
        self.assertEqual(set(self.names()) | {'shell'}, allowed)

    def test_each_launcher_is_a_shell_script_with_unix_line_endings(self):
        for name in self.names():
            data = (LAUNCHERS / name).read_bytes()
            self.assertTrue(data.startswith(b'#!/bin/sh\n'), name)
            self.assertNotIn(b'\r', data, f'{name} has Windows line endings')

    def test_no_launcher_interprets_anything_it_is_given(self):
        """mixosd passes no arguments; a launcher that read some would be a way in."""
        for name in self.names():
            text = (LAUNCHERS / name).read_text(encoding='utf-8')
            for forbidden in ('eval', '$@', '$*', '$1'):
                self.assertNotIn(forbidden, text, f'{name} uses {forbidden}')

    def test_notes_hands_its_own_program_to_the_input_method(self):
        text = (LAUNCHERS / 'notes').read_text(encoding='utf-8')
        self.assertIn('"shell": "$APP"', text)
        self.assertIn('exec "$IME" "$CONFIG"', text)
        # And still runs without it, because the input method is optional.
        self.assertIn('exec "$PYTHON" "$APP"', text)

    def test_notes_says_where_the_pinyin_data_is_instead_of_searching(self):
        """term-ime's first guess is the directory it was compiled in.

        Left to search, it finds /home/pi/mixos-ime/build, which works until the
        build tree is deleted and then stops working for no visible reason. The
        launcher names the installed copy, and declines to use the input method
        at all when that copy is absent, rather than starting one that has
        nothing to convert with.
        """
        text = (LAUNCHERS / 'notes').read_text(encoding='utf-8')
        self.assertIn('/usr/local/share/term-ime/rime-data', text)
        self.assertIn('"rime_shared_data_dir": "$RIME_DATA"', text)
        self.assertIn('[ -d "$RIME_DATA" ]', text)

    def test_the_agent_button_says_it_does_nothing_rather_than_failing(self):
        text = (LAUNCHERS / 'agent').read_text(encoding='utf-8')
        self.assertIn('1049h', text)         # its own screen
        self.assertIn('1049l', text)         # and it gives the old one back


class DeployAppsTests(unittest.TestCase):
    def setUp(self):
        self.deploy = load('mixos_deploy_apps', ROOT / 'tools/deploy_apps.py')

    def test_the_archive_holds_the_interfaces_the_launchers_and_the_units(self):
        import tarfile
        with tarfile.open(fileobj=io.BytesIO(self.deploy.build_archive())) as archive:
            members = {member.name: member for member in archive.getmembers()}
        for expected in ('apps/tui.py', 'apps/audio.py', 'apps/backend.py',
                         'apps/notes/app.py', 'apps/translator/app.py',
                         'apps/translator/vendor/server.py',
                         'launchers/translate', 'launchers/notes', 'launchers/agent',
                         'units/mixos-aiserver.service',
                         'polkit/50-mixos-network.rules'):
            self.assertIn(expected, members)
        for executable in ('launchers/notes', 'apps/notes/app.py',
                           'apps/translator/app.py'):
            self.assertEqual(members[executable].mode & 0o111, 0o111, executable)
        self.assertFalse([name for name in members if '__pycache__' in name])

    def test_text_files_are_shipped_with_unix_line_endings(self):
        self.assertEqual(self.deploy.payload('a.sh', b'one\r\ntwo'), b'one\ntwo')
        self.assertEqual(self.deploy.payload('a.bin', b'one\r\ntwo'), b'one\r\ntwo')

    def test_the_privileged_step_installs_where_the_units_expect(self):
        script = self.deploy.install_script('/home/pi/mixos-install', enable=False)
        unit = (ROOT / 'linux/mixos-aiserver.service').read_text(encoding='utf-8')
        self.assertIn(self.deploy.APP_LIB, script)
        self.assertIn(self.deploy.APP_LIB + '/translator/service.py', unit)
        launcher = (LAUNCHERS / 'translate').read_text(encoding='utf-8')
        self.assertIn(self.deploy.APP_LIB, launcher)
        self.assertIn(self.deploy.APP_DIR, script)
        mixosd = (ROOT / 'linux/mixosd.py').read_text(encoding='utf-8')
        # The launchers must land in the directory mixosd actually looks in.
        self.assertIn(f"default='{self.deploy.APP_DIR}'", mixosd)

    def test_services_are_not_enabled_without_the_environment_they_need(self):
        script = self.deploy.install_script('/tmp/stage', enable=True)
        self.assertIn(f'if [ -x {self.deploy.VENV}/bin/python ]', script)
        self.assertIn('systemctl enable --now', script)

    def test_the_password_never_becomes_an_argument(self):
        source = (ROOT / 'tools/deploy_apps.py').read_text(encoding='utf-8')
        self.assertIn('sudo -S -p ""', source)
        self.assertIn("data=(password + '\\n').encode('utf-8')", source)

    def test_the_daemon_travels_with_everything_else(self):
        """Until 2026-09-14 nothing shipped mixosd.py, so the device kept a
        hand-copied 2026-09-10 version with no application table and every
        launcher button did nothing."""
        import tarfile
        with tarfile.open(fileobj=io.BytesIO(self.deploy.build_archive())) as archive:
            members = {member.name for member in archive.getmembers()}
        self.assertIn('daemon/mixosd.py', members)
        self.assertIn('daemon/protocol.py', members)

    def test_everything_the_daemon_imports_is_shipped_with_it(self):
        """A module left behind makes the daemon exit on import and restart for
        ever. Read the imports rather than trusting the list."""
        source = (ROOT / 'linux/mixosd.py').read_text(encoding='utf-8')
        local = {name for name in ('netctl', 'protocol')
                 if f'import {name}' in source or f'from {name} import' in source}
        shipped = {Path(name).stem for name in self.deploy.DAEMON_FILES}
        self.assertTrue(local, 'expected mixosd.py to import its siblings')
        self.assertTrue(local <= shipped, f'not shipped: {sorted(local - shipped)}')

    def test_the_updater_ships_them_too(self):
        """ota_esp.py takes open_serial from mixosd, so the OTA package needs
        the same siblings. Missing one stopped an update on 2026-09-14 before
        the device was opened - harmless, and entirely avoidable."""
        ota = load('mixos_deploy_ota', ROOT / 'tools/deploy_ota.py')
        source = (ROOT / 'linux/mixosd.py').read_text(encoding='utf-8')
        local = {name for name in ('netctl', 'protocol')
                 if f'import {name}' in source or f'from {name} import' in source}
        carried = {Path(name).stem for name in ota.PAYLOAD}
        self.assertTrue(local <= carried, f'not in the OTA package: {sorted(local - carried)}')
        self.assertIn('linux/mixosd.py', ota.PAYLOAD)

    def test_the_daemon_lands_where_its_unit_runs_it_from(self):
        script = self.deploy.install_script('/home/pi/mixos-install', enable=False)
        unit = (ROOT / 'linux/mixosd.typixdeck.service').read_text(encoding='utf-8')
        self.assertIn(f'{self.deploy.DAEMON_DIR}/mixosd.py', script)
        self.assertIn(f'{self.deploy.DAEMON_DIR}/mixosd.py', unit)
        for name in self.deploy.DAEMON_FILES:
            self.assertIn(f'{self.deploy.DAEMON_DIR}/{name}', script)

    def test_the_daemon_is_run_not_merely_compiled_before_the_restart(self):
        """py_compile accepts a file whose imports do not resolve, which is how
        a restart loop got installed on 2026-09-14."""
        script = self.deploy.install_script('/home/pi/mixos-install', enable=False)
        self.assertIn('mixosd.py --help', script)
        checked_at = script.index('mixosd.py --help')
        restart_at = script.index(f'systemctl restart {self.deploy.DAEMON_SERVICE}')
        self.assertLess(checked_at, restart_at)

    def test_the_daemon_can_be_left_alone(self):
        """Installing it restarts mixosd, which drops whatever is on screen."""
        import tarfile
        with tarfile.open(fileobj=io.BytesIO(
                self.deploy.build_archive(daemon=False))) as archive:
            members = {member.name for member in archive.getmembers()}
        self.assertEqual([], [name for name in members if name.startswith('daemon/')])
        script = self.deploy.install_script('/tmp/stage', enable=False, daemon=False)
        self.assertNotIn(f'systemctl restart {self.deploy.DAEMON_SERVICE}', script)
        self.assertIn(self.deploy.APP_LIB, script)

    def test_every_name_the_screen_can_ask_for_has_a_launcher(self):
        """mixosd's table and the installed launchers are the same vocabulary.
        A name in one and not the other is a button that reports a missing
        program, or a program nothing can reach."""
        mixosd = load('mixos_daemon_for_apps', ROOT / 'linux/mixosd.py')
        shipped = {path.name for path in LAUNCHERS.iterdir() if path.is_file()}
        for name in mixosd.APP_NAMES:
            if name == 'shell':          # the shell is the daemon's own PTY
                continue
            self.assertIn(name, shipped, f'{name} has no launcher')


class UsbGadgetTests(unittest.TestCase):
    """The two USB arrangements are mutually exclusive, and one of them is
    the device working. Everything here is about not getting stuck in the
    other one."""

    def setUp(self):
        self.gadget = load('mixos_usb_gadget', ROOT / 'tools/usb_gadget.py')

    def test_the_addresses_are_the_ones_measured_on_this_device(self):
        self.assertEqual(self.gadget.USB_ADDRESS, '10.12.194.1')
        self.assertEqual(self.gadget.WIFI_ADDRESS, '192.168.1.22')

    def test_the_arrangement_is_read_from_the_device_not_assumed(self):
        host = {'dr_mode': 'dr_mode=host', 'esp32': '1', 'hub': '1'}
        peripheral = {'dr_mode': 'dr_mode=peripheral', 'esp32': '0', 'hub': '0'}
        self.assertIn('Host', self.gadget.describe(host))
        self.assertIn('Device', self.gadget.describe(peripheral))

    def test_configured_one_way_and_wired_the_other_is_named_as_such(self):
        """The commonest mistake is forgetting SW8, and it must not read as
        either working state."""
        confused = {'dr_mode': 'dr_mode=peripheral', 'esp32': '1', 'hub': '1'}
        message = self.gadget.describe(confused)
        self.assertIn('SW8', message)

    def test_the_switch_is_verified_before_the_reboot(self):
        source = (ROOT / 'tools/usb_gadget.py').read_text(encoding='utf-8')
        enable = source.split('def enable', 1)[1].split('def disable', 1)[0]
        self.assertIn('rpi-usb-gadget on', enable)
        # Reading the configuration back before rebooting is the whole point:
        # a reboot into an arrangement nobody checked is how a device is lost.
        self.assertIn("'peripheral' not in after", enable)
        self.assertLess(enable.index("'peripheral' not in after"),
                        enable.index('systemctl reboot'))

    def test_coming_back_is_verified_the_same_way(self):
        source = (ROOT / 'tools/usb_gadget.py').read_text(encoding='utf-8')
        disable = source.split('def disable', 1)[1].split('def main', 1)[0]
        self.assertIn('rpi-usb-gadget off', disable)
        self.assertIn("'peripheral' in after", disable)

    def test_the_reboot_does_not_depend_on_the_session_surviving_it(self):
        """--disable arrives over the interface it is about to remove."""
        source = (ROOT / 'tools/usb_gadget.py').read_text(encoding='utf-8')
        self.assertIn('systemd-run --on-active=2 systemctl reboot', source)
        self.assertNotIn('sudo -S -p "" systemctl reboot', source)

    def test_a_missing_gadget_tool_stops_rather_than_editing_boot_config(self):
        source = (ROOT / 'tools/usb_gadget.py').read_text(encoding='utf-8')
        self.assertIn('is not on this device', source)
        for forbidden in ('tee /boot', 'sed -i', '>> ' + self.gadget.BOOT_CONFIG):
            self.assertNotIn(forbidden, source)

    def test_the_measurement_uses_bytes_that_cannot_compress(self):
        source = (ROOT / 'tools/usb_gadget.py').read_text(encoding='utf-8')
        self.assertIn('os.urandom', source)
        self.assertGreaterEqual(self.gadget.PROBE_BYTES, 8 << 20)

    def test_the_password_never_becomes_an_argument(self):
        source = (ROOT / 'tools/usb_gadget.py').read_text(encoding='utf-8')
        self.assertIn('sudo -S -p ""', source)
        self.assertNotIn('--password', source)


class ModelTransferBlockTests(unittest.TestCase):
    """One SSH connection per block is free at 0.20 MB/s and expensive at 20."""

    def setUp(self):
        self.deploy = load('mixos_deploy_models_blocks', ROOT / 'tools/deploy_models.py')

    def test_the_block_size_is_a_parameter_with_the_slow_link_as_default(self):
        import inspect
        signature = inspect.signature(self.deploy.send)
        self.assertIn('block', signature.parameters)
        self.assertEqual(signature.parameters['block'].default, self.deploy.BLOCK)
        self.assertEqual(self.deploy.BLOCK, 4 << 20)

    def test_an_absurd_block_size_is_refused_before_anything_connects(self):
        argv = sys.argv
        try:
            sys.argv = ['deploy_models.py', '--block-mb', '4096']
            with self.assertRaises(SystemExit) as caught:
                self.deploy.main()
        finally:
            sys.argv = argv
        self.assertIn('--block-mb', str(caught.exception))


class WheelStagingTests(unittest.TestCase):
    """The device cannot install its own dependencies; these arrive with it."""

    def setUp(self):
        self.stage = load('mixos_stage_wheels', ROOT / 'tools/stage_wheels.py')

    def test_wheels_are_resolved_for_the_device_not_for_this_machine(self):
        self.assertEqual(self.stage.PYTHON_VERSION, '3.13')
        self.assertTrue(all('aarch64' in tag for tag in self.stage.PLATFORMS))

    def test_the_tags_publishers_actually_used_are_all_asked_for(self):
        """moonshine-voice tags manylinux_2_34 and litert-lm-api tags
        manylinux_2_27. Asking only for manylinux2014 reports both as having no
        aarch64 build, which is not what is wrong."""
        for tag in ('manylinux_2_34_aarch64', 'manylinux_2_27_aarch64',
                    'manylinux2014_aarch64'):
            self.assertIn(tag, self.stage.PLATFORMS)

    def test_the_download_never_reaches_for_a_source_distribution(self):
        """A source build would need a toolchain on a machine that has none."""
        command = self.stage.download.__code__.co_consts
        source = (ROOT / 'tools/stage_wheels.py').read_text(encoding='utf-8')
        self.assertIn("'--only-binary=:all:'", source)
        self.assertIn('--no-index', (ROOT / 'tools/deploy_venv.py').read_text(
            encoding='utf-8'))
        self.assertTrue(command)

    def test_a_wheel_for_the_wrong_machine_is_reported_not_shipped(self):
        source = (ROOT / 'tools/stage_wheels.py').read_text(encoding='utf-8')
        for foreign in ('x86_64', 'win_amd64', 'macosx'):
            self.assertIn(f"'{foreign}' in wheel.name", source)


class VenvDeploymentTests(unittest.TestCase):
    def setUp(self):
        self.deploy = load('mixos_deploy_venv', ROOT / 'tools/deploy_venv.py')

    def test_the_environment_is_built_where_both_units_look_for_it(self):
        for name in ('mixos-litertlm.service', 'mixos-aiserver.service'):
            unit = (ROOT / 'linux' / name).read_text(encoding='utf-8')
            self.assertIn(self.deploy.VENV, unit, name)

    def test_pip_is_forbidden_from_reaching_the_network(self):
        """A blocked lookup from this device is a stall, not an error."""
        source = (ROOT / 'tools/deploy_venv.py').read_text(encoding='utf-8')
        self.assertIn('--no-index', source)
        self.assertIn('--find-links', source)

    def test_the_environment_is_built_only_after_every_wheel_verifies(self):
        source = (ROOT / 'tools/deploy_venv.py').read_text(encoding='utf-8')
        self.assertLess(source.index('Verifying every wheel on the device'),
                        source.index('Building the environment'))

    def test_it_reuses_the_resumable_transfer_rather_than_its_own(self):
        source = (ROOT / 'tools/deploy_venv.py').read_text(encoding='utf-8')
        self.assertIn('from deploy_models import', source)
        self.assertIn('send(remote, local, destination', source)


class SpeechStagingTests(unittest.TestCase):
    """The backend runs with IPAddressDeny=any. A model it has to download is a
    model it never gets, so these files have to be right before they travel."""

    def setUp(self):
        self.stage = load('mixos_stage_speech', ROOT / 'tools/stage_speech.py')
        self.deploy = load('mixos_deploy_speech', ROOT / 'tools/deploy_speech.py')
        self.service = (APPS / 'translator/service.py').read_text(encoding='utf-8')
        self.vendor = (APPS / 'translator/vendor/server.py').read_text(encoding='utf-8')
        self.unit = (ROOT / 'linux/mixos-aiserver.service').read_text(encoding='utf-8')

    def test_the_staged_path_is_the_one_moonshine_will_look_in(self):
        """download_model_from_info joins the cache with the download URL
        stripped of its scheme. Any other layout stages files nobody reads and
        leaves the device downloading from a host it cannot reach."""
        for item in self.stage.planned(['en', 'zh']):
            self.assertEqual(item['path'], item['url'].replace('https://', ''))
            self.assertFalse(item['path'].startswith('/'))
            self.assertTrue(item['path'].startswith('download.moonshine.ai/'))
    def test_english_asks_for_the_size_that_was_staged(self):
        """The service names an architecture and the stager downloads one. If
        they disagree the first English request tries to fetch the difference,
        which on this unit cannot succeed."""
        self.assertIn("STT_MODEL_ARCH = {'en': 4}", self.service)
        self.assertEqual(self.stage.MODELS['en']['model_arch'], 4)
        self.assertEqual(self.stage.MODELS['en']['model_name'], 'small-streaming-en')

    def test_the_size_is_chosen_without_touching_the_vendored_file(self):
        """vendor/ is byte-exact upstream and PROVENANCE.json says so. The
        choice is a wrapper around the package function, which the vendored
        code re-imports on every request and therefore picks up."""
        self.assertNotIn('STT_MODEL_ARCH', self.vendor)
        self.assertIn('def apply_model_choice', self.service)
        self.assertIn('moonshine_voice.get_model_for_language = choose', self.service)

    def test_a_failure_to_set_the_size_is_announced_not_swallowed(self):
        """Falling back to moonshine's default silently would produce a service
        that starts cleanly and fails on the first press of the microphone."""
        self.assertIn('WARNING: could not set the speech model sizes', self.service)

    def test_the_streaming_model_brings_its_streaming_components(self):
        """get_components_for_model_info returns different file names for
        streaming and non-streaming architectures, plus one extra for English."""
        english = {Path(i['path']).name for i in self.stage.planned(['en'])}
        chinese = {Path(i['path']).name for i in self.stage.planned(['zh'])}
        self.assertIn('decoder_kv_with_attention.ort', english)
        self.assertIn('streaming_config.json', english)
        self.assertIn('encoder_model.ort', chinese)
        self.assertNotIn('streaming_config.json', chinese)
        self.assertIn('tokenizer.bin', chinese)

    def test_the_spelling_model_travels_with_english(self):
        """get_model_for_language prefetches it, so leaving it behind puts a
        network call back into the first English request."""
        names = {Path(i['path']).name for i in self.stage.planned(['en'])}
        self.assertIn('spelling_cnn.ort', names)
        self.assertEqual([], [i for i in self.stage.planned(['zh'])
                              if 'spelling' in i['path']])

    def test_synthesis_assets_land_where_the_tts_cache_looks(self):
        """download_tts_assets writes to get_cache_dir()/download.moonshine.ai/tts
        and the keys are the relative paths under it."""
        tts = [i for i in self.stage.planned(['en', 'zh']) if i['purpose'] == 'synthesis']
        self.assertTrue(tts)
        for item in tts:
            self.assertTrue(item['path'].startswith('download.moonshine.ai/tts/'),
                            item['path'])

    def test_the_shared_voice_model_is_sent_once(self):
        """kokoro/model.onnx is 92 MB and both languages name it. Sending it
        twice is 92 MB of a slow link spent on a file already there."""
        paths = [i['path'] for i in self.stage.planned(['en', 'zh'])]
        self.assertEqual(len(paths), len(set(paths)))
        self.assertIn('download.moonshine.ai/tts/kokoro/model.onnx', paths)

    def test_synthesis_can_be_left_out_without_losing_recognition(self):
        without = self.stage.planned(['en', 'zh'], synthesis=False)
        self.assertTrue(without)
        self.assertEqual([], [i for i in without if i['purpose'] == 'synthesis'])

    def test_the_chinese_voice_staged_is_the_one_the_backend_asks_for(self):
        """TTS_VOICE_MAP pins Chinese to kokoro_zf_xiaoxiao. Staging any other
        voice leaves the backend asking the CDN for the one it wants."""
        self.assertIn('"zh": "kokoro_zf_xiaoxiao"', self.vendor)
        paths = [i['path'] for i in self.stage.planned(['zh'])]
        self.assertIn('download.moonshine.ai/tts/kokoro/voices/zf_xiaoxiao.kokorovoice',
                      paths)

    def test_the_cdn_is_asked_the_question_it_answers(self):
        """download.moonshine.ai returns 403 to HEAD and to the default urllib
        user agent, both measured 2026-09-13."""
        source = (ROOT / 'tools/stage_speech.py').read_text(encoding='utf-8')
        self.assertIn("'Range': 'bytes=0-0'", source)
        self.assertNotIn("method='HEAD'", source)
        self.assertIn('Mozilla/5.0', self.stage.USER_AGENT)

    def test_files_are_refused_where_the_service_could_not_read_them(self):
        """A copy into a directory outside ReadWritePaths= succeeds and changes
        nothing, which is the failure that looks most like success."""
        for prefix in self.deploy.ALLOWED_CACHE_PREFIXES:
            self.assertIn(prefix, self.unit)

    def test_the_cache_location_is_read_from_the_device(self):
        """MOONSHINE_VOICE_CACHE can move it; a hardcoded path would be wrong
        without saying so."""
        self.assertIn('get_cache_dir', self.deploy.CACHE_QUERY)

    def test_it_reuses_the_resumable_transfer_rather_than_its_own(self):
        from deploy_models import send
        self.assertIs(self.deploy.send, send)

    def test_the_password_never_becomes_an_argument(self):
        source = (ROOT / 'tools/deploy_speech.py').read_text(encoding='utf-8')
        self.assertNotIn('sshpass', source)
        self.assertIn('MIXOS_SSH_PASSWORD', source)


class SmokeToolTests(unittest.TestCase):
    def setUp(self):
        self.smoke = load('mixos_smoke_apps', ROOT / 'tools/smoke_apps.py')

    def test_every_launcher_is_exercised(self):
        names = {case[0] for case in self.smoke.CASES}
        self.assertEqual(names, {'translate', 'notes', 'agent'})

    def test_it_runs_them_on_a_real_pseudo_terminal(self):
        """All three refuse to draw on something that is not a terminal."""
        self.assertIn('pty.fork', self.smoke.DRIVER)
        self.assertIn('TIOCSWINSZ', self.smoke.DRIVER)

    def test_a_traceback_counts_as_a_failure(self):
        for marker in ('Traceback', 'ModuleNotFoundError', 'is not installed'):
            self.assertIn(marker, self.smoke.DRIVER)


class RemoteChannelTests(unittest.TestCase):
    """The script and the data are two channels, and they must stay two.

    Piping the script into the remote shell puts it on standard input, and then
    the first command in it that reads standard input consumes the rest of the
    script instead of the payload. That failure is silent in the worst way:
    `cat` writes the tail of a shell script into the file it was filling.
    """

    def test_the_script_does_not_travel_on_standard_input(self):
        source = (ROOT / 'tools/deploy_models.py').read_text(encoding='utf-8')
        run = source.split('    def run(', 1)[1].split('    def text(', 1)[0]
        self.assertIn('bash -c', run)
        self.assertNotIn('| bash', run)
        self.assertIn('input=data', run)


if __name__ == '__main__':
    unittest.main()
