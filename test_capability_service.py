import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from capability_service import ensure_capability, _matching
from storage import ProfileStore


class CapabilityServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = ProfileStore(self.root)
        self.profile = self.store.save('fixture', 'gemini_native', 'https://example.test',
                                       'gemini-fixture', 'synthetic-key')
        self.ident = self.profile['id']

    def sample(self, runner, purpose='video', channel='gemini_media', effort=None):
        return ensure_capability(self.store, self.ident, 'gemini-fixture', purpose, channel, effort, runner)

    def test_result_is_cached_per_purpose_and_channel(self):
        calls = []
        def runner():
            calls.append(1)
            return {'status': 'passed'}
        self.assertEqual(self.sample(runner)['status'], 'passed')
        self.assertTrue(self.sample(runner)['cached'])
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.sample(runner, purpose='speech')['status'], 'passed')
        self.assertEqual(len(calls), 2)

    def test_errors_are_not_failures_and_are_temporarily_cached(self):
        result = self.sample(lambda: {'status': 'error', 'error_code': 'rate_limit'})
        self.assertEqual((result['status'], result['error_code']), ('error', 'rate_limit'))
        self.assertTrue(self.sample(lambda: self.fail('unexpected retry'))['cached'])

    def test_changed_credentials_cannot_receive_an_inflight_sample(self):
        def runner():
            self.store.save('fixture', 'gemini_native', 'https://example.test',
                            'gemini-fixture', 'different-synthetic-key', self.ident)
            return {'status': 'passed'}
        self.assertEqual(self.sample(runner)['reason'], 'sample_not_recorded')
        self.assertEqual(self.store.capability_evidence(self.ident, 'gemini-fixture'), [])

    def test_cached_result_is_not_reused_if_credentials_change_during_lookup(self):
        self.sample(lambda: {'status': 'passed'})
        cached = self.store.capability_evidence(self.ident, 'gemini-fixture')[0]
        def changed(*args):
            self.store.save('fixture', 'gemini_native', 'https://example.test',
                            'gemini-fixture', 'new-synthetic-key', self.ident)
            return cached
        with patch('capability_service._matching', side_effect=changed):
            result = self.sample(lambda: self.fail('do not send after a target change'))
        self.assertEqual(result['reason'], 'route_changed')

    def test_cache_lookup_requires_the_original_identity_even_during_aba(self):
        identity = self.store.capability_identity(self.ident, 'gemini-fixture', None, channel='gemini_media')
        other = {'identity':'different-binding', 'purpose':'video', 'channel':'gemini_media',
                 'effort':None, 'status':'passed', 'tested_at':100}
        with patch.object(self.store, 'capability_evidence', return_value=[other]):
            self.assertIsNone(_matching(self.store, self.ident, 'gemini-fixture', 'video',
                                        'gemini_media', None, 100, identity))

    def test_disabled_connection_is_not_tested(self):
        with self.store.atomic() as db:
            db.execute('INSERT INTO disabled_connections(provider_id) VALUES (?)', (self.ident,))
        result = self.sample(lambda: self.fail('disabled connections must not be tested'))
        self.assertEqual(result['reason'], 'connection_disabled')

    def test_concurrent_calls_share_a_single_test(self):
        started, release = threading.Event(), threading.Event()
        results = []
        def runner():
            started.set()
            self.assertTrue(release.wait(5))
            return {'status': 'passed'}
        def first():
            other = ProfileStore(self.root)
            results.append(ensure_capability(other, self.ident, 'gemini-fixture', 'video',
                                             'gemini_media', None, runner))
        thread = threading.Thread(target=first)
        thread.start()
        try:
            self.assertTrue(started.wait(5))
            result = self.sample(lambda: self.fail('duplicate sample'))
            self.assertEqual(result['status'], 'testing')
        finally:
            release.set()
            thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(results[0]['status'], 'passed')


if __name__ == '__main__':
    unittest.main()
