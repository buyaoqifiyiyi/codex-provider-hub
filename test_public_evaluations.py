import concurrent.futures
import io
import json
import multiprocessing
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import urllib.error
import urllib.parse

import public_evaluations as pe


MODEL = 'Example-V4.1'
REPO = 'publisher/' + MODEL


def evaluation(**overrides):
    row = {'verified': False, 'data': {
        'dataset': {'id': 'cais/hle', 'task_id': 'hle'}, 'value': 42.6,
        'date': '2026-10-17', 'source': {
            'name': 'Example model card', 'url': 'https://huggingface.co/' + REPO},
        'notes': 'Untrusted instructions and arbitrary body must not be saved'}}
    row['data'].update(overrides)
    return row


def process_refresh(root, entered, release, calls):
    # Child process owns its own Python locks; only SQLite coordinates these.
    def fake(model):
        with calls.get_lock():
            calls.value += 1
        entered.set()
        release.wait(10)
        result = pe._empty(model, 'missing')
        return result
    with patch.object(pe, '_fetch', side_effect=fake), patch.object(pe.time, 'time', return_value=1000):
        pe._refresh_one(root, MODEL)


class PublicEvaluationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.clock = 1000.0
        self.calls = []
        self.search = [{'id': REPO}]
        self.detail = {'id': REPO, 'evalResults': [evaluation()]}
        self.addCleanup(self.temp.cleanup)
        self.time_patch = patch.object(pe.time, 'time', side_effect=lambda: self.clock)
        self.time_patch.start()
        self.addCleanup(self.time_patch.stop)
        self.http_patch = patch.object(pe, '_http_json', side_effect=self.http)
        self.http_patch.start()
        self.addCleanup(self.http_patch.stop)

    def http(self, url):
        self.calls.append(url)
        return self.detail if '/api/models/' in url else self.search

    def refresh(self, model=MODEL):
        pe._refresh_one(self.root, model)
        return pe.read_references(self.root, [model])[0][model]

    def wait_worker(self):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            with pe._lock:
                if str(self.root.resolve()) not in pe._queues:
                    return
            threading.Event().wait(.005)
        self.fail('background worker did not finish')

    def test_exact_name_and_metadata_contract(self):
        result = self.refresh()
        self.assertEqual(result['status'], 'available')
        self.assertEqual(result['repository'], REPO)
        self.assertEqual(result['checked_at'], self.clock)
        self.assertEqual(result['expires_at'], self.clock + pe.TTL_SECONDS)
        self.assertFalse(result['stale'])
        row = result['results'][0]
        self.assertEqual({k:v for k,v in row.items() if k!='conditions_fingerprint'},
                         dict(benchmark='cais/hle', task_id='hle', score=42.6,
                              date='2026-10-17', verified=False,
                              source_name='Example model card', source_url='https://huggingface.co/' + REPO))
        self.assertRegex(row['conditions_fingerprint'],r'^[a-f0-9]{64}$')
        self.assertNotIn('Untrusted instructions',json.dumps(result))
        self.assertEqual(urllib.parse.parse_qs(urllib.parse.urlsplit(self.calls[0]).query),
                         {'search': [MODEL], 'limit': ['100'], 'full': ['true']})
        self.assertTrue(self.calls[1].endswith('?expand%5B%5D=evalResults'))

    def test_case_insensitive_exact_only_no_version_or_punctuation_guess(self):
        self.search = [{'id': REPO + '-quantized'}, {'id': REPO.lower()}]
        self.detail['id'] = REPO.lower()
        self.assertEqual(self.refresh()['repository'], REPO.lower())
        for candidates in ([{'id': 'publisher/Example-V4.2'}], [{'id': 'publisher/ExampleV4.1'}]):
            self.search = candidates
            self.assertEqual(pe._fetch(MODEL)['status'], 'missing')

    def test_multiple_owners_never_guess_and_no_detail_query(self):
        self.search += [{'id': 'mirror/' + MODEL}]
        self.assertEqual(self.refresh()['status'], 'ambiguous')
        self.assertEqual(len(self.calls), 1)

    def test_qualified_name_disambiguates_only_exact_owner(self):
        self.search += [{'id': 'mirror/' + MODEL}]
        self.assertEqual(self.refresh(REPO)['repository'], REPO)

    def test_duplicate_rows_do_not_create_ambiguity(self):
        self.search *= 2
        self.assertEqual(self.refresh()['status'], 'available')

    def test_full_search_page_cannot_prove_unique_basename(self):
        self.search += [{'id': 'other/Model' + str(i)} for i in range(99)]
        self.assertEqual(self.refresh()['status'], 'ambiguous')
        self.assertEqual(len(self.calls), 1)

    def test_user_selected_publisher_avoids_ambiguous_mirrors(self):
        name='fixture-model'
        repo='publisher/fixture-model'
        self.search=[{'id':repo}, {'id':'mirror/fixture-model'}]
        self.detail={'id':repo,'evalResults':[evaluation()]}
        (self.root/'public-reference-sources.json').write_text(json.dumps({
            'schemaVersion':1,'allowedReportHosts':[],
            'models':{name:{'type':'huggingface','repository':repo}}}),encoding='utf8')
        result=self.refresh(name)
        self.assertEqual((result['status'],result['repository']),('available',repo))
        self.assertEqual(len(self.calls),1)
        self.assertIn('/api/models/'+repo+'?',self.calls[0])
        self.assertFalse(result['results'][0]['verified'])

    def test_user_selected_report_is_a_distinct_reference(self):
        html = ('<table><tr><th>Evaluation benchmark</th><th>Model A</th><th>Model B</th></tr>'
                '<tr><td>Terminal-bench 2.1</td><td>90.8%</td><td>81.6%</td></tr>'
                '<tr><td>SWE-Bench Pro</td><td>61.6%</td><td>60.4%</td></tr></table>')
        with patch.object(pe, '_http_html', return_value=html) as fetch:
            result=pe._fetch('fixture-model',{'type':'report','url':'https://reports.example.org/benchmarks','column':'Model A'},['reports.example.org'])
        self.assertEqual((result['status'],result['source_kind']),('available','report'))
        self.assertEqual(result['results'][1]['score'],61.6)
        self.assertEqual(result['results'][1]['unit'],'%')
        self.assertIsNone(result['results'][1]['verified'])
        self.assertEqual(result['source_url'],'https://reports.example.org/benchmarks')
        fetch.assert_called_once_with(result['source_url'],['reports.example.org'])
        self.assertEqual(self.calls,[])

    def test_report_never_uses_a_different_or_missing_model_column(self):
        html='<table><tr><th>Evaluation benchmark</th><th>Model B</th></tr><tr><td>SWE-Bench Pro</td><td>99%</td></tr></table>'
        with patch.object(pe, '_http_html', return_value=html), self.assertRaises(ValueError):
            pe._fetch('fixture-model',{'type':'report','url':'https://reports.example.org/benchmarks','column':'Model A'},['reports.example.org'])

    def test_missing_closed_model_is_not_a_capability_result(self):
        self.search = []
        result = self.refresh('gemini-example')
        self.assertEqual(result['status'], 'missing')
        self.assertEqual(result['results'], [])
        self.assertIsNone(result['repository'])

    def test_no_eval_results_is_missing(self):
        self.detail.pop('evalResults')
        self.assertEqual(self.refresh()['status'], 'missing')

    def test_malformed_top_level_and_wrong_repository_are_unavailable(self):
        cases = [('search', {}), ('search', [None]), ('search', [{'id': '../bad'}]),
                 ('detail', []), ('detail', {'id': 'mirror/' + MODEL}),
                 ('detail', {'id': REPO, 'evalResults': {}})]
        for attr, data in cases:
            with self.subTest(data=data):
                original = getattr(self, attr)
                setattr(self, attr, data)
                with self.assertRaises(ValueError):
                    pe._fetch(MODEL)
                setattr(self, attr, original)

    def test_malformed_rows_scores_and_field_limits(self):
        self.detail['evalResults'] = [None, {}, {'data': None}, evaluation(dataset=[])]
        self.detail['evalResults'] += [evaluation(value=v) for v in
                                      [True, '42', None, float('nan'), float('inf'), 10 ** 400]]
        self.detail['evalResults'] += [evaluation(dataset={'id': '<script>bad</script>'}),
                                       evaluation(dataset={'id': 'x' * 161})]
        self.assertEqual(self.refresh()['status'], 'unavailable')

    def test_untrusted_optional_fields_dropped_and_verified_not_coerced(self):
        row = evaluation(date='2026-02-30', source={'name': '<script>bad</script>',
                         'url': 'https://evil.test/secret'})
        row['verified'] = 'true'
        self.detail['evalResults'] = [row]
        result = self.refresh()['results'][0]
        self.assertIsNone(result['date'])
        self.assertIsNone(result['verified'])
        self.assertIsNone(result['source_name'])
        self.assertIsNone(result['source_url'])

    def test_url_whitelist(self):
        for value in ['http://huggingface.co/a', 'https://huggingface.co.evil.test/a',
                      'https://user@huggingface.co/a', 'https://huggingface.co:443/a',
                      'https://huggingface.co/a?key=x', 'https://huggingface.co/a#x',
                      'https://huggingface.co/../a', 'https://huggingface.co/%2e/a',
                      'https://huggingface.co/a\n', 'https://huggingface.co/' + 'a' * 512]:
            with self.subTest(value=value):
                self.assertIsNone(pe._url(value))
        self.assertEqual(pe._url('https://huggingface.co/datasets/cais/hle'),
                         'https://huggingface.co/datasets/cais/hle')

    def test_result_count_bounded_and_notes_not_persisted(self):
        self.detail['evalResults'] *= 150
        self.assertEqual(len(self.refresh()['results']), 100)
        with pe._db(self.root) as db:
            payload = db.execute('SELECT payload FROM public_references').fetchone()[0]
        self.assertNotIn('Untrusted instructions', payload)
        self.assertNotIn('notes', payload)
        self.assertEqual([p.name for p in self.root.iterdir()], [pe.DB_FILENAME])

    def test_fresh_cache_no_network_or_schedule(self):
        self.refresh()
        self.clock += pe.TTL_SECONDS - 1
        with patch.object(pe, 'schedule_refresh') as schedule:
            refs, pending = pe.read_references(self.root, [MODEL])
        self.assertFalse(pending)
        schedule.assert_not_called()
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(refs[MODEL]['status'], 'available')

    def test_first_read_and_expired_read_automatically_schedule(self):
        with patch.object(pe, 'schedule_refresh') as schedule:
            refs, pending = pe.read_references(self.root, [MODEL])
        self.assertTrue(pending)
        self.assertEqual(refs[MODEL]['status'], 'pending')
        self.assertIsNone(refs[MODEL]['checked_at'])
        schedule.assert_called_once_with(self.root, [MODEL])
        self.refresh()
        self.clock += pe.TTL_SECONDS
        with patch.object(pe, 'schedule_refresh') as schedule:
            refs, pending = pe.read_references(self.root, [MODEL])
        self.assertTrue(pending)
        self.assertTrue(refs[MODEL]['stale'])
        self.assertEqual(refs[MODEL]['checked_at'], 1000)
        self.assertEqual(refs[MODEL]['results'], [])
        schedule.assert_called_once()

    def test_automatic_daemon_completes_and_expiry_refreshes(self):
        pe.read_references(self.root, [MODEL])
        self.wait_worker()
        self.assertFalse(pe.read_references(self.root, [MODEL])[1])
        self.clock += pe.TTL_SECONDS
        pe.read_references(self.root, [MODEL])
        self.wait_worker()
        self.assertEqual(len(self.calls), 4)
        self.assertEqual(pe.read_references(self.root, [MODEL])[0][MODEL]['checked_at'], self.clock)

    def test_timeout_backoff_then_retry_no_error_body_saved(self):
        with patch.object(pe, '_http_json', side_effect=TimeoutError('private response body')):
            result = self.refresh()
        self.assertEqual(result['status'], 'unavailable')
        self.assertEqual(result['expires_at'], self.clock + 300)
        pe.schedule_refresh(self.root, [MODEL])
        self.wait_worker()
        self.assertEqual(self.calls, [])
        self.assertFalse(pe.read_references(self.root, [MODEL])[1])
        self.clock += 300
        with patch.object(pe, '_http_json', side_effect=OSError('private response body')):
            self.assertEqual(self.refresh()['expires_at'], self.clock + 600)
        self.clock += 600
        self.assertEqual(self.refresh()['status'], 'available')
        with pe._db(self.root) as db:
            self.assertEqual(db.execute('SELECT failures FROM public_references').fetchone()[0], 0)
        self.assertNotIn(b'private response body', (self.root / pe.DB_FILENAME).read_bytes())

    def test_failed_refresh_does_not_resurrect_old_scores(self):
        self.refresh()
        self.clock += pe.TTL_SECONDS
        with patch.object(pe, '_http_json', side_effect=TimeoutError()):
            result = self.refresh()
        self.assertEqual(result['status'], 'unavailable')
        self.assertEqual(result['results'], [])

    def test_http_errors_back_off_without_becoming_capability_failures(self):
        for code in [401, 403, 404, 429, 500]:
            with self.subTest(code=code):
                self.clock += 86400
                error = urllib.error.HTTPError('https://huggingface.co/api/models', code, 'public failure', {}, None)
                with patch.object(pe, '_http_json', side_effect=error):
                    result = self.refresh()
                self.assertEqual(result['status'], 'unavailable')
                self.assertEqual(result['results'], [])
                self.assertFalse(pe.read_references(self.root, [MODEL])[1])

    def test_failure_backoff_is_capped(self):
        with patch.object(pe, '_http_json', side_effect=TimeoutError()):
            for _ in range(12):
                result = self.refresh()
                self.assertLessEqual(result['expires_at'] - self.clock, 21600)
                self.clock = result['expires_at']
        self.assertEqual(result['expires_at'] - result['checked_at'], 21600)

    def test_missing_cache_and_expired_lease_recovery(self):
        self.search = []
        self.refresh()
        self.assertFalse(pe.read_references(self.root, [MODEL])[1])
        self.clock += pe.TTL_SECONDS
        with pe._db(self.root) as db:
            db.execute('UPDATE public_references SET owner=?, lease=?', ('dead-process', self.clock + 120))
        pe._refresh_one(self.root, MODEL)
        self.assertEqual(len(self.calls), 1)
        self.clock += 120
        pe._refresh_one(self.root, MODEL)
        self.assertEqual(len(self.calls), 2)

    def test_lost_lease_cannot_overwrite_newer_result(self):
        def stolen(model):
            with pe._db(self.root) as db:
                db.execute('UPDATE public_references SET owner=?', ('new-worker',))
            return pe._empty(model, 'missing')
        with patch.object(pe, '_fetch', side_effect=stolen):
            pe._refresh_one(self.root, MODEL)
        with pe._db(self.root) as db:
            self.assertIsNone(db.execute('SELECT payload FROM public_references').fetchone()[0])

    def test_parallel_threads_one_fetch_and_nonblocking_reads(self):
        entered, release = threading.Event(), threading.Event()
        def blocked(model):
            entered.set()
            release.wait(5)
            return pe._empty(model, 'missing')
        with patch.object(pe, '_fetch', side_effect=blocked) as fetch:
            pe.schedule_refresh(self.root, [MODEL])
            self.assertTrue(entered.wait(3))
            try:
                with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
                    futures = [pool.submit(pe.read_references, self.root, [MODEL]) for _ in range(16)]
                    outcomes = [f.result(timeout=2) for f in futures]
                self.assertTrue(all(pending for _, pending in outcomes))
                self.assertEqual(fetch.call_count, 1)
            finally:
                release.set()
                self.wait_worker()
            self.assertEqual(fetch.call_count, 1)

    def test_cross_process_lease(self):
        context = multiprocessing.get_context('spawn')
        entered, release = context.Event(), context.Event()
        calls = context.Value('i', 0)
        first = context.Process(target=process_refresh, args=(str(self.root), entered, release, calls))
        second = context.Process(target=process_refresh, args=(str(self.root), entered, release, calls))
        first.start()
        try:
            self.assertTrue(entered.wait(5))
            second.start()
            second.join(5)
            self.assertEqual(second.exitcode, 0)
            self.assertEqual(calls.value, 1)
        finally:
            release.set()
            first.join(5)
            if first.is_alive():
                first.terminate()
                first.join()
            if second.pid and second.is_alive():
                second.terminate()
                second.join()
        self.assertEqual(first.exitcode, 0)

    def test_cache_failure_is_unavailable_not_save_failure(self):
        blocker = self.root / 'file'
        blocker.write_text('not a directory')
        refs, pending = pe.read_references(blocker, [MODEL])
        self.assertFalse(pending)
        self.assertEqual(refs[MODEL]['status'], 'unavailable')
        self.assertEqual(self.calls, [])

    def test_locked_database_does_not_block_caller(self):
        self.refresh()
        db = sqlite3.connect(self.root / pe.DB_FILENAME)
        try:
            db.execute('BEGIN EXCLUSIVE')
            started = time.monotonic()
            refs, pending = pe.read_references(self.root, [MODEL])
            self.assertLess(time.monotonic() - started, .5)
            self.assertEqual(refs[MODEL]['status'], 'unavailable')
            self.assertFalse(pending)
        finally:
            db.rollback()
            db.close()

    def test_only_explicit_models_and_no_configuration_access(self):
        (self.root / 'profiles.sqlite3').write_bytes(b'not a database; must never open')
        self.assertEqual(pe.read_references(self.root, []), ({}, False))
        pe.schedule_refresh(self.root, [MODEL, MODEL])
        self.wait_worker()
        self.assertEqual(len(self.calls), 2)
        self.assertEqual((self.root / 'profiles.sqlite3').read_bytes(), b'not a database; must never open')

    def test_invalid_names_rejected_without_network(self):
        for names in ['a', ['https://example.test'], ['../a'], ['a\n'], ['a?key=x'],
                      ['a/b/c'], ['a' * 201], [None], [[]], [{}], None,
                      [str(i) for i in range(257)]]:
            with self.subTest(names=str(names)[:40]), self.assertRaises(ValueError):
                pe.read_references(self.root, names)
        self.assertEqual(self.calls, [])

    def test_anonymous_transport_has_no_proxy_credentials_and_blocks_redirects(self):
        response = io.BytesIO(json.dumps([]).encode())
        response.status = 200
        opener = unittest.mock.Mock()
        opener.open.return_value = response
        # Restore the real function only for this isolated fake transport.
        self.http_patch.stop()
        with patch.object(pe.urllib.request, 'build_opener', return_value=opener) as build:
            self.assertEqual(pe._http_json('https://huggingface.co/api/models?search=example'), [])
        self.assertEqual(build.call_args.args[0].proxies, {})
        request = opener.open.call_args.args[0]
        self.assertEqual(dict(request.header_items()), {'Accept': 'application/json'})
        with self.assertRaises(ValueError):
            pe._NoRedirect().redirect_request(None, None, 302, '', {}, 'https://evil.test/')

    def test_transport_rejects_oversize_and_bad_json(self):
        self.http_patch.stop()
        for body in [b'bad json', b'x' * (pe._MAX_BYTES + 1)]:
            response = io.BytesIO(body)
            response.status = 200
            opener = unittest.mock.Mock()
            opener.open.return_value = response
            with patch.object(pe.urllib.request, 'build_opener', return_value=opener), self.assertRaises(ValueError):
                pe._http_json('https://huggingface.co/api/models?search=example')


if __name__ == '__main__':
    unittest.main()
