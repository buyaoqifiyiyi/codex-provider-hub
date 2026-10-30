"""Offline synthetic checks for public reference selection and transport."""
import io
import json
from pathlib import Path
import socket
import tempfile
import unittest
from unittest.mock import Mock, patch

import public_evaluations as pe


ORIGINAL_HTTP_JSON = pe._http_json
MODEL = 'Example-Model-v2'
REPO = 'example-publisher/Example-Model-v2'
HOST = 'reports.example.org'
URL = 'https://' + HOST + '/public/benchmarks'


def card(repo=REPO):
    return {'id': repo, 'evalResults': [{'verified': False, 'data': {
        'dataset': {'id': 'example/test', 'task_id': 'test'}, 'value': 72,
        'date': '2026-01-01', 'unit': '%',
        'source': {'name': 'Example', 'url': 'https://huggingface.co/' + repo}}}]}


def config(source=None, hosts=()):
    return {'schemaVersion': 1, 'allowedReportHosts': list(hosts),
            'models': {MODEL: source} if source else {}}


class NeutralPublicReferences(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='public-reference-test-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.file = self.root / 'public-reference-sources.json'
        self.calls = []
        self.clock = 1000.0
        clock = patch.object(pe.time, 'time', side_effect=lambda: self.clock)
        clock.start()
        self.addCleanup(clock.stop)
        http = patch.object(pe, '_http_json', side_effect=self.fake_http)
        http.start()
        self.addCleanup(http.stop)
        queue = patch.object(pe, 'schedule_refresh')
        queue.start()
        self.addCleanup(queue.stop)

    def fake_http(self, url):
        self.calls.append(url)
        return card() if '/api/models/' in url else [{'id': REPO}]

    def write(self, data):
        self.file.write_text(json.dumps(data), encoding='utf-8')

    def refresh(self, model=MODEL):
        pe._refresh_one(self.root, model)
        return pe.read_references(self.root, [model])[0][model]

    def test_absent_config_searches_exactly_and_keeps_cache(self):
        result = self.refresh()
        self.assertEqual((result['status'], result['repository']), ('available', REPO))
        self.assertEqual(result['results'][0]['score'], 72)
        self.assertEqual(len(self.calls), 2)
        self.assertIn('search=Example-Model-v2', self.calls[0])
        self.assertTrue(self.calls[1].startswith('https://huggingface.co/api/models/'))
        self.assertEqual(self.refresh()['status'], 'available')
        self.assertEqual(len(self.calls), 2)
        self.assertFalse(self.file.exists())

    def test_ambiguous_mirrors_full_page_and_no_match_do_not_guess(self):
        for response, expected in [
            ([{'id': REPO}, {'id': 'mirror/' + MODEL}], 'ambiguous'),
            ([{'id': REPO}] + [{'id': 'owner/Other-' + str(i)} for i in range(99)], 'ambiguous'),
            ([{'id': 'owner/Example-Model-v3'}], 'missing'),
        ]:
            with self.subTest(status=expected), patch.object(pe, '_http_json', return_value=response) as fetch:
                self.assertEqual(pe._fetch(MODEL)['status'], expected)
                self.assertEqual(fetch.call_count, 1)

    def test_explicit_repository_skips_ambiguous_search_only_when_configured(self):
        self.write(config({'type': 'huggingface', 'repository': REPO}))
        result = self.refresh()
        self.assertEqual(result['repository'], REPO)
        self.assertEqual(len(self.calls), 1)
        self.assertIn('/api/models/' + REPO + '?', self.calls[0])

    def test_explicit_report_uses_exact_column_and_unverified_metadata(self):
        self.write(config({'type': 'report', 'url': URL, 'column': 'Example Model'}, [HOST]))
        html = ('<table><tr><th>Evaluation benchmark</th><th>Example Model</th><th>Other</th></tr>'
                '<tr><td>Example Benchmark</td><td>81.5%</td><td>99%</td></tr></table>')
        with patch.object(pe, '_http_html', return_value=html) as fetch:
            result = self.refresh()
        self.assertEqual(result['status'], 'available')
        self.assertEqual(result['source_kind'], 'report')
        self.assertEqual(result['results'][0]['score'], 81.5)
        self.assertIsNone(result['results'][0]['verified'])
        self.assertEqual(result['source_url'], URL)
        self.assertFalse(self.calls)
        self.assertEqual(fetch.call_count, 1)
        self.assertEqual(fetch.call_args.args[0], URL)

    def test_report_missing_or_ambiguous_column_fails_without_other_fetch(self):
        self.write(config({'type': 'report', 'url': URL, 'column': 'Example Model'}, [HOST]))
        for heads in ['<th>Other</th>', '<th>Example Model</th><th>Example Model</th>']:
            html = '<table><tr><th>Evaluation benchmark</th>' + heads + '</tr></table>'
            with patch.object(pe, '_http_html', return_value=html), self.assertRaises(ValueError):
                pe._fetch_report(MODEL, URL, 'Example Model', [HOST])
        self.assertFalse(self.calls)

    def test_cache_source_changes_invalidate_old_scores_and_do_not_alter_result_contract(self):
        self.assertEqual(self.refresh()['status'], 'available')
        source = {'type': 'report', 'url': URL, 'column': 'Example Model'}
        self.write(config(source, [HOST]))
        with patch.object(pe, 'schedule_refresh') as schedule:
            refs, pending = pe.read_references(self.root, [MODEL])
        self.assertTrue(pending)
        self.assertEqual(refs[MODEL]['results'], [])
        schedule.assert_called_once()
        html = ('<table><tr><th>Evaluation benchmark</th><th>Example Model</th></tr>'
                '<tr><td>Example Benchmark</td><td>85%</td></tr></table>')
        with patch.object(pe, '_http_html', return_value=html):
            result = self.refresh()
        self.assertEqual(result['results'][0]['score'], 85)
        self.assertNotIn('_source_signature', result)
        self.file.unlink()
        with patch.object(pe, 'schedule_refresh'):
            result, pending = pe.read_references(self.root, [MODEL])
        self.assertTrue(pending)
        self.assertEqual(result[MODEL]['results'], [])

    def test_old_cache_has_no_source_identity_and_is_not_reused(self):
        with pe._db(self.root) as db:
            old = pe._empty(MODEL, 'available')
            old['results'] = [{'score': 99}]
            db.execute('INSERT INTO public_references(model,payload,checked,expires) VALUES (?,?,?,?)',
                       (MODEL, json.dumps(old), self.clock, self.clock + pe.TTL_SECONDS))
        refs, pending = pe.read_references(self.root, [MODEL])
        self.assertTrue(pending)
        self.assertEqual(refs[MODEL]['results'], [])
        with pe._db(self.root) as db:
            self.assertEqual(db.execute('SELECT expires FROM public_references').fetchone()[0], 0)

    def test_config_change_during_fetch_discards_the_inflight_result(self):
        def fetch_and_change(url):
            if '/api/models/' not in url:
                return [{'id': REPO}]
            self.write(config({'type':'report', 'url':URL, 'column':'Example Model'}, [HOST]))
            return card()
        with patch.object(pe, '_http_json', side_effect=fetch_and_change):
            pe._refresh_one(self.root, MODEL)
        with pe._db(self.root) as db:
            payload, expires = db.execute('SELECT payload,expires FROM public_references').fetchone()
        self.assertIsNone(payload)
        self.assertEqual(expires, 0)
        refs, pending = pe.read_references(self.root, [MODEL])
        self.assertTrue(pending)
        self.assertEqual(refs[MODEL]['results'], [])

    def test_direct_cache_reader_can_check_current_source_without_network(self):
        self.refresh()
        with pe._db(self.root) as db:
            payload = json.loads(db.execute('SELECT payload FROM public_references').fetchone()[0])
        calls = list(self.calls)
        self.assertTrue(pe.cache_matches_source(self.root, MODEL, payload))
        self.assertFalse(pe.cache_matches_source(self.root, 'Other-Model', payload))
        self.write(config({'type':'report', 'url':URL, 'column':'Example Model'}, [HOST]))
        self.assertFalse(pe.cache_matches_source(self.root, MODEL, payload))
        self.file.write_text('{broken', encoding='utf-8')
        self.assertFalse(pe.cache_matches_source(self.root, MODEL, payload))
        self.assertEqual(self.calls, calls)

    def test_fetch_failure_remains_unavailable_backoff_and_does_not_leak(self):
        with patch.object(pe, '_http_json', side_effect=TimeoutError('secret-body')):
            result = self.refresh()
        self.assertEqual(result['status'], 'unavailable')
        self.assertEqual(result['expires_at'], self.clock + 300)
        self.assertEqual(result['results'], [])
        self.assertNotIn(b'secret-body', (self.root / pe.DB_FILENAME).read_bytes())

    def test_malformed_config_fails_closed_without_sending_any_request(self):
        invalid = [
            '{', 'null', '{"schemaVersion":1,"schemaVersion":1,"allowedReportHosts":[],"models":{}}',
            json.dumps({'schemaVersion': 2, 'allowedReportHosts': [], 'models': {}}),
            json.dumps({'schemaVersion': 1, 'allowedReportHosts': [], 'models': {}, 'token':'secret'}),
            json.dumps(config({'type':'huggingface','repository':'example/Model','token':'secret'})),
            json.dumps(config({'type':'huggingface','repository':'https://api.example.org/v1'})),
            json.dumps(config({'type':'report','url':URL,'column':'Example Model'}, [])),
            '[' * 1500 + ']' * 1500,
            ' ' * (128 * 1024),
        ]
        for text in invalid:
            with self.subTest(config=text[:80]):
                self.file.write_text(text, encoding='utf-8')
                with patch.object(pe, '_http_html') as report:
                    pe._refresh_one(self.root, MODEL)
                    refs, pending = pe.read_references(self.root, [MODEL])
                self.assertFalse(pending)
                self.assertEqual(refs[MODEL]['status'], 'unavailable')
                report.assert_not_called()
                self.assertFalse(self.calls)

    def test_url_policy_rejects_credentials_private_literal_and_unsafe_hosts(self):
        bad = [
            ('http://' + HOST + '/public', [HOST]),
            ('https://user:password@' + HOST + '/public', [HOST]),
            ('https://' + HOST + ':443/public', [HOST]),
            ('https://' + HOST + '/public?token=secret', [HOST]),
            ('https://' + HOST + '/public?', [HOST]),
            ('https://' + HOST + '/public#fragment', [HOST]),
            ('https://' + HOST + '/public#', [HOST]),
            ('https://' + HOST + '/public\n', [HOST]),
            ('https://' + HOST + '/%2e%2e/private', [HOST]),
            ('https://' + HOST + '/a/../private', [HOST]),
            ('https://localhost/public', ['localhost']),
            ('https://service.local/public', ['service.local']),
            ('https://127.0.0.1/public', ['127.0.0.1']),
            ('https://192.168.1.1/public', ['192.168.1.1']),
            ('https://8.8.8.8/public', ['8.8.8.8']),
            ('https://[::1]/public', ['::1']),
            ('https://other.example.org/public', [HOST]),
            ('https://' + HOST + '/sk-abcdefghijklmnop', [HOST]),
        ]
        for url, hosts in bad:
            with self.subTest(url=url):
                self.write(config({'type':'report','url':url,'column':'Model'}, hosts))
                with patch.object(pe, '_http_html') as report:
                    refs, pending = pe.read_references(self.root, [MODEL])
                    pe._refresh_one(self.root, MODEL)
                self.assertEqual(refs[MODEL]['status'], 'unavailable')
                self.assertFalse(pending)
                report.assert_not_called()
                self.assertFalse(self.calls)

    def test_model_names_reject_addresses_and_likely_credentials_before_io(self):
        for model in ['https://private.example/v1', 'sk-abcdefghijklmnop', 'hf_abcdefghijklmnop',
                      'publisher/sk-abcdefghijklmnop', 'model?token=secret', '127.0.0.1', 'localhost',
                      'api.provider.example/v1', 'fixture.invalid/v1']:
            with self.subTest(model=model), self.assertRaises(ValueError):
                pe.read_references(self.root, [model])
        self.assertFalse(self.calls)
        self.assertFalse((self.root / pe.DB_FILENAME).exists())

    def test_report_table_format_and_parser_limits(self):
        for html in [
            '<table><tr><th>Evaluation benchmark</th><th>Model</th></tr></table>' * 2,
            '<table><tr><th>Evaluation benchmark</th><th>Model</th></tr>'
            '<tr><td>' + 'x' * 2049 + '</td><td>99%</td></tr></table>',
        ]:
            with self.subTest(length=len(html)), patch.object(pe, '_http_html', return_value=html), \
                    self.assertRaises(ValueError):
                pe._fetch_report(MODEL, URL, 'Model', [HOST])

    def test_huggingface_transport_does_not_use_proxy_or_credentials(self):
        response = io.BytesIO(b'[]')
        response.status = 200
        opener = Mock()
        opener.open.return_value = response
        # Restore only the real transport while its opener is a synthetic object.
        with patch.object(pe, '_http_json', new=ORIGINAL_HTTP_JSON), \
                patch.object(pe.urllib.request, 'build_opener', return_value=opener) as build:
            self.assertEqual(pe._http_json('https://huggingface.co/api/models?search=Example'), [])
        self.assertEqual(build.call_args.args[0].proxies, {})
        self.assertEqual(dict(opener.open.call_args.args[0].header_items()), {'Accept': 'application/json'})
        with self.assertRaises(ValueError):
            pe._NoRedirect().redirect_request(None, None, 302, '', {}, 'https://elsewhere.example.org/')


class ReportTransportTests(unittest.TestCase):
    @staticmethod
    def address(ip):
        return (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, '', (ip, 443))

    def test_rejects_private_mixed_or_unresolvable_dns_before_connecting(self):
        cases = [[self.address('127.0.0.1')], [self.address('8.8.8.8'), self.address('10.0.0.1')], []]
        for addresses in cases:
            with self.subTest(addresses=addresses), patch.object(pe.socket, 'getaddrinfo', return_value=addresses), \
                    patch.object(pe, '_PinnedReportConnection') as connection:
                with self.assertRaises(ValueError):
                    pe._http_html(URL, [HOST])
                connection.assert_not_called()

    def test_public_dns_is_resolved_once_and_connection_is_pinned_without_auth(self):
        response = io.BytesIO(b'<table></table>')
        response.status = 200
        response.getheader = lambda name, default=None: 'text/html' if name == 'Content-Type' else default
        connection = Mock()
        connection.getresponse.return_value = response
        with patch.object(pe.socket, 'getaddrinfo', return_value=[self.address('8.8.8.8')]) as dns, \
                patch.object(pe, '_PinnedReportConnection', return_value=connection) as make:
            self.assertEqual(pe._http_html(URL, [HOST]), '<table></table>')
        dns.assert_called_once()
        self.assertEqual(make.call_args.args[0], HOST)
        self.assertEqual(make.call_args.args[1][-1], ('8.8.8.8', 443))
        self.assertEqual(connection.request.call_args.args[:2], ('GET', '/public/benchmarks'))
        headers = connection.request.call_args.kwargs['headers']
        self.assertEqual(headers, {'Accept':'text/html'})
        connection.close.assert_called_once()

    def test_connection_pins_socket_and_uses_hostname_for_tls_verification(self):
        sock = Mock()
        context = Mock()
        with patch.object(pe.socket, 'socket', return_value=sock), \
                patch.object(pe.ssl, 'create_default_context', return_value=context):
            conn = pe._PinnedReportConnection(HOST, self.address('8.8.8.8'))
            conn.connect()
        sock.connect.assert_called_once_with(('8.8.8.8', 443))
        context.wrap_socket.assert_called_once_with(sock, server_hostname=HOST)

    def test_redirects_and_oversized_bodies_fail_without_follow_up(self):
        for status, body in [(302, b''), (307, b''), (200, b'x' * (pe._MAX_BYTES + 1))]:
            response = io.BytesIO(body)
            response.status = status
            response.getheader = lambda name, default=None: 'text/html' if name == 'Content-Type' else default
            connection = Mock()
            connection.getresponse.return_value = response
            with self.subTest(status=status), patch.object(pe.socket, 'getaddrinfo', return_value=[self.address('8.8.8.8')]), \
                    patch.object(pe, '_PinnedReportConnection', return_value=connection):
                with self.assertRaises(ValueError):
                    pe._http_html(URL, [HOST])
                self.assertEqual(connection.request.call_count, 1)
                connection.close.assert_called_once()


if __name__ == '__main__':
    unittest.main()
