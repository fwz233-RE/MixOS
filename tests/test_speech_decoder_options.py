"""Chinese decoder-budget configuration without models or external services."""
import importlib.util
import os
import sys
import threading
import types
import unittest
from collections import OrderedDict
from pathlib import Path
from unittest import mock

APPS = Path(os.environ.get('MIXOS_TEST_APPS',
                           Path(__file__).resolve().parents[1] / 'linux/apps'))
spec = importlib.util.spec_from_file_location('speech_options_service', APPS / 'translator/service.py')
service = importlib.util.module_from_spec(spec)
spec.loader.exec_module(service)


class DecoderOptionsTests(unittest.TestCase):
    def setUp(self):
        self.upstream = types.SimpleNamespace(
            get_stt_recognizer=mock.Mock(return_value='original'),
            _stt_lock=threading.RLock(), _stt_recognizers=OrderedDict(), MAX_MODELS=2)
        self.original = self.upstream.get_stt_recognizer
        self.factory = mock.Mock(side_effect=lambda **kwargs: types.SimpleNamespace(options=kwargs))
        self.models = mock.Mock(return_value=('/cache/base-zh', 1))
        self.package = types.SimpleNamespace(Transcriber=self.factory, get_model_for_language=self.models)
        patch = mock.patch.dict(sys.modules, {'moonshine_voice': self.package})
        patch.start()
        self.addCleanup(patch.stop)
        service.apply_stt_options(self.upstream)

    def test_chinese_receives_finite_sixteen_token_budget_without_changing_model(self):
        result = self.upstream.get_stt_recognizer('zh')
        self.models.assert_called_once_with('zh')
        self.factory.assert_called_once_with(model_path='/cache/base-zh', model_arch=1,
                                              options={'max_tokens_per_second': 16})
        self.assertIs(self.upstream._stt_recognizers['zh'], result)
        self.original.assert_not_called()

    def test_other_languages_and_unsupported_language_use_upstream_unchanged(self):
        for language in ('en', 'ja', 'unsupported'):
            self.assertEqual(self.upstream.get_stt_recognizer(language), 'original')
        self.upstream.get_stt_recognizer()
        self.assertEqual(self.original.call_args_list,
                         [mock.call('en'), mock.call('ja'), mock.call('unsupported'), mock.call('en')])
        self.factory.assert_not_called()

    def test_cached_model_is_reused_and_marked_recent(self):
        recognizer = self.upstream.get_stt_recognizer('zh')
        self.upstream._stt_recognizers['en'] = object()
        self.assertIs(self.upstream.get_stt_recognizer('zh'), recognizer)
        self.assertEqual(list(self.upstream._stt_recognizers), ['en', 'zh'])
        self.assertEqual(self.factory.call_count, 1)

    def test_two_model_cache_evicts_oldest_before_loading_chinese(self):
        self.upstream._stt_recognizers.update([('ja', object()), ('en', object())])
        self.upstream.get_stt_recognizer('zh')
        self.assertEqual(list(self.upstream._stt_recognizers), ['en', 'zh'])

    def test_concurrent_requests_construct_only_one_recognizer(self):
        barrier = threading.Barrier(5)
        results = []
        def request():
            barrier.wait(timeout=3)
            results.append(self.upstream.get_stt_recognizer('zh'))
        threads = [threading.Thread(target=request) for _ in range(5)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(3)
            self.assertFalse(thread.is_alive())
        self.assertEqual(len(results), 5)
        self.assertTrue(all(result is results[0] for result in results))
        self.assertEqual(self.factory.call_count, 1)

    def test_load_failure_is_not_cached_as_a_working_model(self):
        self.factory.side_effect = RuntimeError('model unavailable')
        with self.assertRaisesRegex(RuntimeError, 'model unavailable'):
            self.upstream.get_stt_recognizer('zh')
        self.assertNotIn('zh', self.upstream._stt_recognizers)

    def test_options_are_copied_and_installation_is_idempotent(self):
        before = self.upstream.get_stt_recognizer
        service.apply_stt_options(self.upstream)
        self.assertIs(self.upstream.get_stt_recognizer, before)
        self.upstream.get_stt_recognizer('zh').options['options']['max_tokens_per_second'] = 999
        self.assertEqual(service.STT_OPTIONS['zh']['max_tokens_per_second'], 16)

    def test_server_installs_configuration_before_accepting_requests(self):
        source = (APPS / 'translator/service.py').read_text(encoding='utf-8')
        main = source[source.index('def main('):]
        self.assertLess(main.index('apply_stt_options(upstream)'), main.index('with LocalServer('))
        self.assertLess(main.index('apply_stt_options(upstream)'), main.index('if args.prewarm:'))


if __name__ == '__main__':
    unittest.main()
