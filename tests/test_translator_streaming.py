"""Translator-only streaming, ordering and cancellation regression tests.

No models or sound card required. Gates deliberately stall each stage, so the
assertions prove overlap/ordering rather than relying on a fast CI machine.
"""
from __future__ import annotations

import importlib.util
import io
import json
import sys
import threading
import time
import types
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
APPS = ROOT / 'linux/apps'
sys.path.insert(0, str(APPS))

import audio
import backend
import tui

spec = importlib.util.spec_from_file_location('translator_streaming_app',
                                               APPS / 'translator/app.py')
app = importlib.util.module_from_spec(spec)
spec.loader.exec_module(app)

PCM = b'\x00\x10\x00\xf0' * 4000


def eventually(predicate, timeout=3.0, tick=None):
    deadline = time.perf_counter() + timeout
    while time.perf_counter() < deadline:
        if tick:
            tick()
        if predicate():
            return
        threading.Event().wait(0.002)
    raise AssertionError('timed out waiting for test stage')


class FakePlayer:
    def __init__(self, automatic=True):
        self.automatic = automatic
        self.played = []
        self.starts = []
        self.finished = []
        self.interruptions = 0
        self._playing = False
        self._released = threading.Event()

    @property
    def playing(self):
        return self._playing

    def play(self, wav):
        if self._playing:
            self.interruptions += 1
        self._released = threading.Event()
        self._playing = True
        self.played.append(wav)
        self.starts.append(time.perf_counter())

    def wait(self, cancel=None):
        released = self._released
        if not self.automatic:
            while not released.wait(0.002):
                if cancel is not None and cancel.is_set():
                    return False
                if self.automatic:
                    break
        if cancel is not None and cancel.is_set():
            return False
        self._playing = False
        self.finished.append(time.perf_counter())
        return True

    def release(self):
        self._released.set()

    def stop(self):
        self._playing = False
        self._released.set()


class FakeSpeech:
    def __init__(self):
        self.recognised = []
        self.synthesised = []
        self.on_transcribe = None
        self.on_synthesize = None

    def transcribe(self, pcm, language):
        self.recognised.append(language)
        if self.on_transcribe:
            return self.on_transcribe()
        return '原文'

    def synthesize(self, text, language):
        self.synthesised.append((text, language))
        if self.on_synthesize:
            return self.on_synthesize(text)
        return text.encode('utf-8')


class FakeModel:
    def __init__(self, pieces=('First sentence.', ' Second sentence.')):
        self.pieces = pieces
        self.prompt = None
        self.on_complete = None
        self.finished_at = None

    def complete(self, messages, on_token, cancel):
        self.prompt = messages
        if self.on_complete:
            result = self.on_complete(on_token, cancel)
        else:
            for piece in self.pieces:
                if cancel.is_set():
                    raise backend.Cancelled()
                on_token(piece)
            result = ''.join(self.pieces)
        self.finished_at = time.perf_counter()
        return result


class ChunkTests(unittest.TestCase):
    def chunks(self, text, language='en'):
        chunker = app.SpeechChunks(language)
        result = []
        for char in text:
            result.extend(chunker.feed(char))
        result.extend(chunker.finish())
        return result

    def test_complete_first_sentence_is_ready_without_another_token(self):
        chunker = app.SpeechChunks('en')
        self.assertEqual(list(chunker.feed('Hello world.')), ['Hello world.'])
        self.assertEqual(list(chunker.finish()), [])

    def test_chinese_and_english_tokens_are_grouped_into_phrases(self):
        for language, text in (
                ('en', 'Please come this way, we are waiting for you. Welcome!'),
                ('zh', '请大家跟着我一起往这边走，我们正在等待你。欢迎光临！')):
            with self.subTest(language=language):
                chunks = self.chunks(text, language)
                self.assertGreater(len(chunks), 1)
                self.assertTrue(all(len(chunk) >= 4 for chunk in chunks))
                self.assertEqual(''.join(chunks).replace(' ', ''), text.replace(' ', ''))

    def test_short_unpunctuated_reply_is_flushed_exactly_once(self):
        chunker = app.SpeechChunks('en')
        self.assertEqual(list(chunker.feed('OK')), [])
        self.assertEqual(list(chunker.finish()), ['OK'])
        self.assertEqual(list(chunker.finish()), [])
        self.assertEqual(self.chunks(' !?。。 '), [])

    def test_no_punctuation_still_starts_a_short_first_phrase(self):
        text = ('we would like to find the nearest railway station and then '
                'take the train to the town where our friends are staying today ' * 3)
        chunks = self.chunks(text)
        self.assertLess(len(chunks[0]), 50)
        self.assertGreater(len(chunks[1]), len(chunks[0]))
        self.assertEqual(' '.join(chunks), text.strip())
        chinese = self.chunks('我们希望找到最近的火车站然后乘坐火车前往朋友所在的城市' * 3, 'zh')
        self.assertLessEqual(len(chinese[0]), 16)
        self.assertTrue(all(len(chunk) <= app.SpeechChunks.MAX_CHARS for chunk in chinese))

    def test_decimal_thousands_time_and_abbreviation_survive_token_boundaries(self):
        for text, required in (
                ('Dr. Smith has 3.14 dollars.', 'Dr. Smith'),
                ('The price is 1,000 dollars.', '1,000'),
                ('Please arrive at 12:30 tomorrow.', '12:30'),
                ('Use e.g. this example.', 'e.g.'),
                ('This is the U.S. embassy.', 'U.S.')):
            with self.subTest(text=text):
                chunks = self.chunks(text)
                self.assertTrue(any(required in chunk for chunk in chunks), chunks)

    def test_pathological_word_and_single_large_callback_are_bounded(self):
        chunker = app.SpeechChunks('en')
        chunks = list(chunker.feed('x' * 5000)) + list(chunker.finish())
        self.assertEqual(''.join(chunks), 'x' * 5000)
        self.assertTrue(all(len(chunk) <= chunker.MAX_CHARS for chunk in chunks))
        self.assertLess(len(chunker.pending), chunker.MAX_CHARS)


class StreamingTests(unittest.TestCase):
    def setUp(self):
        screen = tui.Screen(io.StringIO(), columns=64, rows=22)
        self.translator = app.Translator(screen)
        self.translator.source_language = 'zh'
        self.translator.target_language = 'en'
        self.translator.speech = self.speech = FakeSpeech()
        self.translator.model = self.model = FakeModel()
        self.translator.player = self.player = FakePlayer()
        self.translator.recorder = types.SimpleNamespace(
            stop=lambda: PCM, cancel=lambda: None, start=lambda: None, error=None)
        self.gates = []
        self.jobs = []
        self.threads = []
        self.addCleanup(self.cleanup)

    def gate(self):
        gate = threading.Event()
        self.gates.append(gate)
        return gate

    def cleanup(self):
        self.translator.abandon()
        self.player.automatic = True
        self.player.release()
        for gate in self.gates:
            gate.set()
        for thread in self.threads:
            thread.join(timeout=3)
            self.assertFalse(thread.is_alive(), 'leaked translation thread')
        self.translator.drain()

    def start(self):
        self.translator.state = 'recording'
        self.translator.end_recording()
        job = self.translator._job
        self.assertIsNotNone(job)
        self.jobs.append(job)
        self.threads.append(self.translator.worker)
        return job

    def finish(self):
        eventually(lambda: not self.translator.busy(), tick=self.translator.drain)
        self.threads[-1].join(timeout=1)
        self.assertFalse(self.threads[-1].is_alive())

    def test_first_audio_starts_before_model_has_finished(self):
        release_model = self.gate()

        def generate(on_token, cancel):
            on_token('The first sentence is ready.')
            self.assertTrue(release_model.wait(3))
            on_token(' The second sentence follows.')
            return 'The first sentence is ready. The second sentence follows.'

        self.model.on_complete = generate
        self.start()
        eventually(lambda: len(self.player.played) == 1, tick=self.translator.drain)
        self.assertIsNone(self.model.finished_at)
        self.assertTrue(self.translator.busy())
        self.assertIn('first sentence', self.translator.current.target)
        release_model.set()
        self.finish()
        self.assertEqual(len(self.player.played), 2)
        self.assertLess(self.player.starts[0], self.model.finished_at)
        self.assertEqual(len(self.translator.history), 1)

    def test_synthesis_prefetches_but_clips_never_replace_each_other(self):
        self.player.automatic = False
        self.model.pieces = ('First sentence.', ' Second sentence.', ' Third sentence.')
        self.start()
        eventually(lambda: self.player.playing and len(self.speech.synthesised) >= 2,
                   tick=self.translator.drain)
        self.assertEqual(len(self.player.played), 1)
        self.assertTrue(self.translator.busy())
        for index in range(3):
            eventually(lambda: len(self.player.played) == index + 1,
                       tick=self.translator.drain)
            self.assertTrue(self.translator.busy())
            self.player.release()
        self.finish()
        self.assertEqual(self.player.interruptions, 0)
        self.assertEqual(self.player.played,
                         [b'First sentence.', b'Second sentence.', b'Third sentence.'])
        self.assertEqual(len(self.player.finished), 3)
        for index in range(1, 3):
            self.assertGreaterEqual(self.player.starts[index], self.player.finished[index - 1])

    def test_busy_and_replay_cover_final_playback_not_just_model_completion(self):
        self.player.automatic = False
        self.model.pieces = ('Only this sentence.',)
        self.start()
        eventually(lambda: self.player.playing, tick=self.translator.drain)
        self.translator.drain()
        self.assertEqual(self.translator.state, 'speaking')
        self.translator.key(' ')
        self.translator.key('r')
        self.assertEqual(self.translator.state, 'speaking')
        self.assertEqual(len(self.player.played), 1)
        self.assertTrue(self.player.playing)
        self.assertEqual(self.player.interruptions, 0)
        self.player.release()
        self.finish()
        self.assertEqual(self.translator.last_wavs, (b'Only this sentence.',))
        self.translator.key('r')
        self.threads.append(self.translator.worker)
        eventually(lambda: len(self.player.played) == 2)
        self.assertTrue(self.translator.busy())
        self.player.release()
        self.finish()

    def test_source_target_and_speak_setting_are_snapshotted_before_stt(self):
        stt_started, release_stt = self.gate(), self.gate()

        def recognise():
            stt_started.set()
            self.assertTrue(release_stt.wait(3))
            return '原文'

        self.speech.on_transcribe = recognise
        job = self.start()
        self.assertTrue(stt_started.wait(3))
        self.translator.key('tab')
        self.translator.key('a')
        release_stt.set()
        self.finish()
        self.assertEqual(self.speech.recognised, ['zh'])
        self.assertIn('from Chinese into English', self.model.prompt[0]['content'])
        self.assertTrue(self.speech.synthesised)
        self.assertTrue(all(language == 'en' for _, language in self.speech.synthesised))
        self.assertEqual((job.source, job.target, job.speak), ('zh', 'en', True))
        self.assertEqual(self.translator.target_language, 'zh')
        self.assertFalse(self.translator.speak_result)

    def test_enabling_speech_mid_generation_applies_only_to_next_job(self):
        generated, release_model = self.gate(), self.gate()
        self.translator.speak_result = False

        def generate(on_token, cancel):
            on_token('A complete sentence.')
            generated.set()
            self.assertTrue(release_model.wait(3))
            return 'A complete sentence.'

        self.model.on_complete = generate
        self.start()
        self.assertTrue(generated.wait(3))
        self.translator.key('a')
        release_model.set()
        self.finish()
        self.assertFalse(self.speech.synthesised)
        self.assertFalse(self.player.played)

    def test_cancel_during_synthesis_never_plays_late_audio_or_reuses_event(self):
        started, release = self.gate(), self.gate()

        def synthesize(text):
            started.set()
            self.assertTrue(release.wait(3))
            return text.encode()

        self.speech.on_synthesize = synthesize
        old = self.start()
        self.assertTrue(started.wait(3))
        self.assertTrue(self.translator.key('escape'))
        self.assertEqual(self.translator.state, 'stopping')
        self.assertTrue(old.cancel.is_set())
        self.translator.key(' ')
        self.assertIs(self.translator._job, old)  # outstanding HTTP is not duplicated
        self.assertFalse(self.translator.key('escape'))
        release.set()
        self.finish()
        self.assertFalse(self.player.played)
        self.assertEqual(old.wav.qsize(), 0)
        self.assertEqual(old.clips, [])
        self.speech.on_synthesize = None
        new = self.start()
        self.assertIsNot(new.cancel, old.cancel)
        self.assertTrue(old.cancel.is_set())
        old.events.put(('token', 'STALE'))
        self.finish()
        self.assertNotIn('STALE', ''.join(item.target for item in self.translator.history))
        self.assertEqual(len(self.player.played), 2)

    def test_stale_job_cannot_play_or_stop_a_new_job(self):
        old = self.start()
        self.finish()
        self.player.automatic = False
        self.model.pieces = ('New translation.',)
        self.start()
        eventually(lambda: self.player.playing, tick=self.translator.drain)
        before = list(self.player.played)
        self.assertFalse(self.translator._play_clip(old, b'STALE'))
        self.translator._stop_job_audio(old)
        self.assertTrue(self.player.playing)
        self.assertEqual(self.player.played, before)
        self.player.release()
        self.finish()

    def test_replay_keeps_all_phrases_in_order_without_resynthesising(self):
        self.start()
        self.finish()
        requests = list(self.speech.synthesised)
        self.translator.key('r')
        self.threads.append(self.translator.worker)
        self.finish()
        self.assertEqual(self.player.played,
                         [b'First sentence.', b'Second sentence.'] * 2)
        self.assertEqual(self.player.interruptions, 0)
        self.assertEqual(self.speech.synthesised, requests)
        self.assertEqual(len(self.translator.history), 1)

    def test_leaving_run_during_synthesis_cannot_start_late_audio(self):
        started, release = self.gate(), self.gate()

        def synthesize(text):
            started.set()
            self.assertTrue(release.wait(3))
            return text.encode()

        self.speech.on_synthesize = synthesize
        self.start()
        self.assertTrue(started.wait(3))
        with mock.patch.object(self.translator, '_greet'):
            self.translator.run(types.SimpleNamespace(read=lambda timeout: 'q'))
        self.assertTrue(self.translator.cancel.is_set())
        release.set()
        self.finish()
        self.assertFalse(self.player.played)

    def test_cancel_during_stt_discards_result_and_never_calls_model(self):
        started, release = self.gate(), self.gate()

        def recognise():
            started.set()
            self.assertTrue(release.wait(3))
            return 'late result'

        self.speech.on_transcribe = recognise
        self.start()
        self.assertTrue(started.wait(3))
        self.translator.key('escape')
        release.set()
        self.finish()
        self.assertIsNone(self.model.prompt)
        self.assertFalse(self.player.played)
        self.assertEqual(self.translator.current.source, '')

    def test_cancel_stops_current_clip_and_all_buffered_phrases(self):
        self.player.automatic = False
        self.model.pieces = tuple(f'Sentence number {i}.' for i in range(100))
        job = self.start()
        eventually(lambda: job.text.full() and job.wav.full(), tick=self.translator.drain)
        self.assertLessEqual(job.text.qsize(), app.TEXT_QUEUE_SIZE)
        self.assertLessEqual(job.wav.qsize(), app.WAV_QUEUE_SIZE)
        self.assertLessEqual(job.events.qsize(), app.EVENT_QUEUE_SIZE)
        self.assertEqual(len(self.player.played), 1)
        self.assertTrue(self.translator.key('escape'))
        self.assertFalse(self.player.playing)
        self.finish()
        self.assertEqual(len(self.player.played), 1)
        self.assertEqual(job.text.qsize(), 0)
        self.assertEqual(job.wav.qsize(), 0)
        self.assertFalse(self.translator.busy())

    def test_a_full_ui_event_queue_cannot_deadlock_cancellation(self):
        self.translator.speak_result = False
        self.model.pieces = ('x',) * 2000
        job = self.start()
        eventually(job.events.full)
        self.translator.key('escape')
        self.assertTrue(job.done.wait(3))
        self.translator.drain()
        self.assertFalse(self.translator.busy())

    def test_model_failure_stops_audio_and_archives_visible_error(self):
        self.player.automatic = False

        def generate(on_token, cancel):
            on_token('A sentence before failure.')
            eventually(lambda: self.player.playing)
            raise backend.ServiceError('model disconnected')

        self.model.on_complete = generate
        self.start()
        self.finish()
        self.assertFalse(self.player.playing)
        self.assertEqual(self.translator.history[-1].error, 'model disconnected')
        self.assertEqual(self.translator.status_kind, 'bad')

    def test_tts_failure_keeps_translation_and_clears_busy_with_warning(self):
        def fail(_text):
            raise backend.ServiceError('TTS unavailable')

        self.speech.on_synthesize = fail
        job = self.start()
        self.finish()
        self.assertEqual(self.translator.history[-1].target, ''.join(self.model.pieces))
        self.assertEqual(self.translator.status_kind, 'warn')
        self.assertIn('TTS unavailable', self.translator.status)
        self.assertFalse(self.player.playing)
        self.assertFalse(job.clips)
        self.assertTrue(job.wav.empty())

    def test_empty_wav_is_a_warning_not_silent_success(self):
        self.speech.on_synthesize = lambda text: b''
        self.start()
        self.finish()
        self.assertIn('no audio', self.translator.status)
        self.assertEqual(self.translator.status_kind, 'warn')

    def test_playback_failure_does_not_stall_queue_producers(self):
        def fail(_wav):
            raise audio.AudioUnavailable('speaker disconnected')

        self.player.play = fail
        self.model.pieces = tuple(f'Sentence number {i}.' for i in range(50))
        self.start()
        self.finish()
        self.assertEqual(self.translator.status_kind, 'warn')
        self.assertIn('speaker disconnected', self.translator.status)
        self.assertEqual(self.translator.history[-1].target, ''.join(self.model.pieces))

    def test_completion_without_callbacks_and_short_tail_are_spoken_once(self):
        self.model.on_complete = lambda on_token, cancel: 'Hello'
        self.start()
        self.finish()
        self.assertEqual(self.speech.synthesised, [('Hello', 'en')])
        self.assertEqual(self.player.played, [b'Hello'])

    def test_idle_escape_exits_but_recording_escape_cancels_one_layer(self):
        self.assertFalse(self.translator.key('escape'))
        self.assertIn('Esc 返回', self.translator.hints(62))
        self.translator.key(' ')
        self.assertEqual(self.translator.state, 'recording')
        self.assertIn('Esc 中止', self.translator.hints(62))
        self.assertTrue(self.translator.key('escape'))
        self.assertEqual(self.translator.state, 'idle')
        self.assertFalse(self.translator.key('escape'))

    def test_replay_retention_has_a_byte_limit(self):
        job = app.TranslationJob('zh', 'en', True)
        with mock.patch.object(app, 'MAX_REPLAY_BYTES', 10):
            job.remember(b'123456')
            job.remember(b'abcdef')
            job.remember(b'x')
        self.assertFalse(job.replayable)
        self.assertEqual(job.clips, [])
        self.assertLessEqual(job.events.maxsize, 64)

    def test_real_http_clients_overlap_sse_with_phrase_synthesis(self):
        release_model = self.gate()
        generated = self.gate()
        requests = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def reply(self, body, content_type='application/json'):
                self.send_response(200)
                self.send_header('Content-Type', content_type)
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                requests.append((self.path, body))
                if self.path == '/api/stt':
                    self.reply(json.dumps({'text': '原文'}).encode())
                    return
                self.send_response(200)
                self.send_header('Content-Type', 'text/event-stream')
                self.end_headers()
                for index, piece in enumerate(('First sentence.', ' Second sentence.')):
                    event = json.dumps({'choices': [{'delta': {'content': piece}}]})
                    self.wfile.write(f'data: {event}\n\n'.encode())
                    self.wfile.flush()
                    if index == 0:
                        release_model.wait(3)
                self.wfile.write(b'data: [DONE]\n\n')
                self.wfile.flush()
                generated.set()

            def do_GET(self):
                query = parse_qs(urlparse(self.path).query)
                requests.append(('/api/tts', query))
                self.reply(query['text'][0].encode(), 'audio/wav')

        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()
        try:
            self.translator.speech = backend.Speech('127.0.0.1', server.server_port)
            self.translator.model = backend.LanguageModel('127.0.0.1', server.server_port,
                                                          name='loopback-test')
            self.start()
            eventually(lambda: self.player.played, tick=self.translator.drain)
            self.assertFalse(generated.is_set())
            self.assertEqual(self.player.played, [b'First sentence.'])
            release_model.set()
            self.finish()
            self.assertEqual(self.player.played, [b'First sentence.', b'Second sentence.'])
            calls = [body for path, body in requests if path == '/api/tts']
            self.assertEqual([call['lang'] for call in calls], [['en'], ['en']])
            model_request = next(body for path, body in requests if path == '/v1/chat/completions')
            self.assertTrue(model_request['stream'])
        finally:
            release_model.set()
            server.shutdown()
            server.server_close()
            server_thread.join(timeout=3)

    def test_controlled_latency_measurement(self):
        # The LLM deliberately takes another 300 ms after its first sentence;
        # TTS takes 20 ms per request. These are host simulation measurements,
        # not claims about model inference or the deck's acoustic latency.
        def generate(on_token, cancel):
            on_token('Ready to speak now.')
            cancel.wait(0.30)
            on_token(' The rest arrives later.')
            return 'Ready to speak now. The rest arrives later.'

        def synthesize(text):
            threading.Event().wait(0.02)
            return text.encode()

        self.model.on_complete = generate
        self.speech.on_synthesize = synthesize
        started = time.perf_counter()
        self.start()
        self.finish()
        first_ms = (self.player.starts[0] - started) * 1000
        complete_ms = (self.model.finished_at - started) * 1000
        self.assertLess(self.player.starts[0], self.model.finished_at)
        print(f'\nSimulated first playback: {first_ms:.1f} ms; '
              f'LLM complete: {complete_ms:.1f} ms; '
              f'lead: {complete_ms - first_ms:.1f} ms')


class PlayerWaitTests(unittest.TestCase):
    def test_real_subprocess_waits_for_feed_and_releases_resources(self):
        player = audio.Player()
        self.addCleanup(player.stop)
        command = [sys.executable, '-c',
                   'import sys; data=sys.stdin.buffer.read(); sys.exit(0 if data else 1)']
        with mock.patch.object(audio, '_tool', return_value=sys.executable), \
                mock.patch.object(audio, 'playback_command', return_value=command):
            player.play(b'wav-buffer' * 10000)
            self.assertTrue(player.wait())
            self.assertFalse(player.playing)
            player.stop()
            self.assertIsNone(player._process)
            self.assertIsNone(player._thread)

    def test_real_subprocess_failure_surfaces_from_wait(self):
        player = audio.Player()
        self.addCleanup(player.stop)
        command = [sys.executable, '-c',
                   'import sys; sys.stdin.buffer.read(); '
                   'sys.stderr.write("speaker unavailable\\n"); sys.exit(2)']
        with mock.patch.object(audio, '_tool', return_value=sys.executable), \
                mock.patch.object(audio, 'playback_command', return_value=command):
            player.play(b'wav-buffer')
            with self.assertRaisesRegex(audio.AudioUnavailable, 'speaker unavailable'):
                player.wait()

    def test_wait_returns_only_after_process_completion(self):
        player = audio.Player()
        process = types.SimpleNamespace(returncode=0, poll=mock.Mock(side_effect=[None, 0]))
        player._process = process
        self.assertTrue(player.wait())
        self.assertEqual(process.poll.call_count, 2)

    def test_cancel_does_not_wait_for_or_touch_a_replacement(self):
        player = audio.Player()
        cancel = threading.Event()
        cancel.set()
        player._process = types.SimpleNamespace(poll=lambda: None)
        self.assertFalse(player.wait(cancel))

    def test_aplay_failure_reports_its_stderr(self):
        player = audio.Player()
        player._process = types.SimpleNamespace(
            poll=lambda: 1, returncode=1,
            stderr=io.BytesIO(b'aplay: device missing\nmore details'))
        with self.assertRaisesRegex(audio.AudioUnavailable, 'device missing'):
            player.wait()

    def test_stuck_player_has_a_deadline(self):
        player = audio.Player()
        player._process = types.SimpleNamespace(poll=lambda: None)
        with mock.patch.object(audio, 'MAX_SECONDS', -1):
            with self.assertRaisesRegex(audio.AudioUnavailable, 'in time'):
                player.wait()


if __name__ == '__main__':
    unittest.main()
