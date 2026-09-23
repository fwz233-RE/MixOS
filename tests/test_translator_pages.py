"""Every recognised word remains reachable on fixed (non-scrolling) pages."""
import importlib.util
import io
import os
import sys
import types
import unittest
from pathlib import Path
from unittest import mock

APPS = Path(os.environ.get('MIXOS_TEST_APPS',
                           Path(__file__).resolve().parents[1] / 'linux/apps'))
sys.path.insert(0, str(APPS))
import tui
spec = importlib.util.spec_from_file_location('translator_pages', APPS / 'translator/app.py')
app = importlib.util.module_from_spec(spec)
spec.loader.exec_module(app)


class TranscriptPagesTests(unittest.TestCase):
    def make(self, columns=64, rows=20):
        translator = app.Translator(tui.Screen(io.StringIO(), columns=columns, rows=rows))
        self.addCleanup(translator.abandon)
        return translator

    @staticmethod
    def painted(translator):
        translator.draw()
        return '\n'.join(''.join(cell.char for cell in row) for row in translator.screen.back)

    def test_finished_translation_keeps_original_visible(self):
        translator = self.make()
        original = '请帮我买明天下午三点到北京的火车票。'
        translator.events.put(('heard', original))
        translator.events.put(('translated', 'Please buy me a train ticket to Beijing tomorrow at three. ' * 6))
        translator.drain()
        shown = self.painted(translator)
        self.assertIn('请帮我买明天下午三点到北京', shown)
        self.assertIn('原文', shown)
        self.assertIn('1/', shown)
        self.assertEqual(translator.history[-1].source, original)

    def test_all_chinese_characters_are_reachable_live_and_after_archive(self):
        for columns, rows in ((64, 20), (80, 27)):
            for archived in (False, True):
                with self.subTest(columns=columns, archived=archived):
                    translator = self.make(columns, rows)
                    source = ''.join(chr(0x4e00 + index) for index in range(450))
                    target = ''.join(chr(0x5200 + index) for index in range(450))
                    translator.current = app.Exchange(source, target)
                    translator.state = 'translating'
                    if archived:
                        translator._archive()
                        translator.state = 'idle'
                    observed = ''
                    for _ in range(32):
                        observed += self.painted(translator)
                        translator.key('pagedown')
                    self.assertTrue(set(source + target).issubset(set(observed)),
                                    'some recognised/translated characters cannot be viewed')
                    for _ in range(32):
                        translator.key('pageup')
                    self.assertIn(source[:20], self.painted(translator))

    def test_model_tokens_do_not_displace_the_original_page(self):
        translator = self.make()
        translator.events.put(('heard', '原文开头应当一直保留，不能被新增译文挤掉。'))
        translator.drain()
        for _ in range(20):
            translator.events.put(('token', 'This translation is getting longer. '))
            translator.drain()
            self.assertIn('原文开头应当一直保留', self.painted(translator))
        translator.key('pagedown')
        self.assertGreater(translator._view_page, 0)
        page = translator._view_page
        translator.events.put(('token', 'More words.'))
        translator.drain()
        translator.draw()
        self.assertEqual(translator._view_page, page)

    def test_archive_preserves_a_selected_page_and_old_exchanges_are_reachable(self):
        translator = self.make()
        translator.history = [app.Exchange('这是上一句', 'Previous exchange.')]
        current = app.Exchange('最新原文' * 100, 'Latest translation.')
        translator.current = current
        translator.draw()
        translator.key('pagedown')
        translator.draw()
        page = translator._view_page
        translator._archive()
        translator.state = 'idle'
        translator.draw()
        self.assertEqual(translator._view_page, page)
        for _ in range(32):
            translator.key('pageup')
        self.assertIn('这是上一句', self.painted(translator))
        translator.key('c')
        self.assertNotIn('这是上一句', self.painted(translator))
        self.assertEqual(translator._view_page, 0)

    def test_listening_is_announced_only_after_actual_capture_data(self):
        translator = self.make()
        translator.recorder = types.SimpleNamespace(
            running=True, seconds=0.0, level=0.0, start=lambda: None,
            cancel=lambda: None)
        translator.begin_recording()
        self.assertIn('开启麦克风', self.painted(translator))
        self.assertFalse(translator._recording_ready)
        calls = 0

        def key(_timeout):
            nonlocal calls
            calls += 1
            if calls == 1:
                self.assertFalse(translator._recording_ready)
                translator.recorder.seconds = 0.02
                return None
            self.assertTrue(translator._recording_ready)
            self.assertIn('录音中', self.painted(translator))
            return 'q'

        with mock.patch.object(translator, '_greet'):
            translator.run(types.SimpleNamespace(read=key))

    def test_capture_failure_automatically_leaves_recording_without_stt(self):
        translator = self.make()
        translator.recorder = types.SimpleNamespace(
            running=False, seconds=0.0, level=0.0, start=lambda: None,
            cancel=lambda: None, stop=lambda: b'', error='capture unavailable')
        translator.begin_recording()
        translator.speech = mock.Mock()
        with mock.patch.object(translator, '_greet'):
            translator.run(types.SimpleNamespace(read=lambda timeout: 'q'))
        self.assertEqual(translator.state, 'idle')
        translator.speech.transcribe.assert_not_called()

    def test_reading_page_navigation_does_not_change_language_or_start_audio(self):
        translator = self.make()
        translator.history = [app.Exchange('识别文本' * 200, 'Translation.')]
        direction = (translator.source_language, translator.target_language)
        for key in ('right', 'left', 'pagedown', 'pageup'):
            self.assertTrue(translator.key(key))
        self.assertEqual(direction, (translator.source_language, translator.target_language))
        self.assertFalse(translator.recorder.running)
        self.assertFalse(translator.player.playing)
        self.assertIn('翻页', self.painted(translator))


if __name__ == '__main__':
    unittest.main()
