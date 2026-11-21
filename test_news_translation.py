import copy
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent/'stock_monitor'))
from news_translation import fingerprint, update_cache, project, setting, direct
from news_runtime import NewsRuntime


def row(n=0): return dict(title='Apple reports quarterly earnings '+str(n), summary='Revenue rose by 12 percent.', url='https://example.org/'+str(n))
def translated(r, target): return dict(status='TRANSLATED', title='Переведённый заголовок', summary='Выручка выросла на 12 процентов.', target=target)


class TranslationTests(unittest.TestCase):
    def test_source_immutable_cache_reused_and_relevant_change_invalidates(self):
        rows = [row()]; before = copy.deepcopy(rows); calls = []
        def translator(r, target): calls.append(r); return translated(r, target)
        cache = update_cache(rows, 'ru', {}, translator=translator, now=1)
        cache = update_cache(rows, 'ru', cache, translator=translator, now=2)
        self.assertEqual(len(calls), 1); self.assertEqual(rows, before)
        changed = [{**rows[0], 'summary': 'New source facts'}]
        self.assertEqual(project(changed, cache, 'ru')[0]['translation']['status'], 'PENDING')
        self.assertEqual(project(rows, cache, 'de')[0]['title'], rows[0]['title'])
        self.assertEqual(project(rows, cache, 'ru')[0]['original_title'], rows[0]['title'])

    def test_53_and_200_synthetic_inputs_are_bounded_and_do_not_repeat_valid_work(self):
        for total in (53, 200):
            rows = [row(n) for n in range(total)]; cache = {}; calls = []
            def translator(r, target): calls.append(fingerprint(r)); return translated(r, target)
            for _ in range(35): cache = update_cache(rows, 'ru', cache, translator=translator, now=1)
            self.assertEqual(len(calls), min(total, 100)); self.assertEqual(len(set(calls)), len(calls))
            self.assertLessEqual(len(cache['items']), 100)

    def test_translation_failure_is_explicit_original_and_backoff_prevents_busy_loop(self):
        def failed(*_): raise ValueError('MODEL_MISSING')
        cache = update_cache([row()], 'ru', {}, translator=failed, now=__import__('time').time())
        out = project([row()], cache, 'ru')[0]
        self.assertEqual(out['title'], row()['title']); self.assertEqual(out['translation']['status'], 'UNAVAILABLE')
        with patch('news_translation.translate') as translated_mock:
            again = update_cache([row()], 'ru', cache, translator=translated_mock, now=__import__('time').time()+1)
            translated_mock.assert_not_called(); self.assertEqual(again, cache)

    def test_numeric_changes_and_oversize_tokens_are_rejected_without_truncation(self):
        tokenizer = SimpleNamespace(encode=lambda text, **kwargs: text.split(), decode=lambda tokens: 'Revenue grew by 19 percent.')
        translator = SimpleNamespace(translate_batch=lambda *args, **kwargs: [SimpleNamespace(hypotheses=[['x']])])
        with patch('news_translation.load_model', return_value=(translator, tokenizer)):
            with self.assertRaisesRegex(ValueError, 'NUMBERS_CHANGED'): direct('Revenue grew by 12 percent.', 'en', 'ru')
            with self.assertRaisesRegex(ValueError, 'TOO_LONG'): direct('word '*513, 'en', 'ru')

    def test_runtime_discards_changed_source_translation_before_cache_publication(self):
        state = {'news:A': {'articles': [row()]}}; jobs = []; callbacks = []
        store = SimpleNamespace(setting=lambda k, d: copy.deepcopy(state.get(k, d)), set_setting=lambda k, v: state.__setitem__(k, v))
        app = SimpleNamespace(store=store, closing=False, job=lambda w, d, f, **kwargs: jobs.append((w, d, kwargs)) or True)
        runtime = NewsRuntime(app)
        def update(rows, target, previous):
            cache = update_cache(rows, target, previous, translator=translated)
            state['news:A']['articles'][0]['summary'] = 'Changed while calculating'
            return cache
        with patch('news_translation.update_cache', side_effect=update):
            self.assertTrue(runtime.translate_news('A', [row()], 'ru', callbacks.append))
            w, d, meta = jobs.pop(); self.assertEqual(meta['lane'], 'misc'); result = w()
        self.assertFalse(state[setting('A', 'ru')]['items']); app.closing = True; d(result)
        self.assertFalse(callbacks); self.assertFalse(runtime.busy)

    def test_dashboard_preserves_translation_after_normalization_and_searches_original(self):
        from news_dashboard import prepare, filtered, details
        source = row(); cache = update_cache([source], 'ru', {}, translator=translated)
        packet = prepare(dict(articles=[source], _translation_cache=cache, _translation_target='ru'))
        out = packet['articles'][0]
        self.assertEqual(out['title'], 'Переведённый заголовок')
        self.assertEqual(len(filtered([out], query='quarterly')), 1)
        self.assertIn('Машинный перевод', details(out))


if __name__ == '__main__': unittest.main()
