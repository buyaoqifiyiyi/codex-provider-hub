import io
import json
import unittest
from unittest.mock import patch

import cc_switch_reference as reference


class _Response(io.BytesIO):
    status = 200


class ReferenceTests(unittest.TestCase):
    def setUp(self):
        reference._cached = None
        reference._expires = 0
        reference._indexed_document = None
        reference._index = None

    def test_public_request_contains_no_connection_or_credential(self):
        seen = []
        class Opener:
            def open(self, request, timeout):
                seen.append((request.full_url, dict(request.header_items()), timeout))
                return _Response(json.dumps({'vendor': {'models': {}}}).encode())
        self.assertEqual(reference.load_catalog(opener=Opener()), {'vendor': {'models': {}}})
        self.assertEqual(seen[0][0], 'https://models.dev/api.json')
        self.assertEqual(seen[0][1], {'Accept': 'application/json'})
        self.assertEqual(seen[0][2], 2)

    def test_invalid_or_oversized_catalog_is_optional(self):
        class Opener:
            def __init__(self, body): self.body = body
            def open(self, request, timeout): return _Response(self.body)
        for body in (b'bad', b'[]', b' ' * (reference.MAX_BYTES + 1)):
            with self.subTest(size=len(body)):
                self.assertIsNone(reference.load_catalog(opener=Opener(body)))

    def test_unavailable_reference_is_cached_briefly(self):
        class Opener:
            calls = 0
            def open(self, request, timeout):
                self.calls += 1
                raise OSError('offline')
        opener = Opener()
        with patch('urllib.request.build_opener', return_value=opener):
            self.assertIsNone(reference.load_catalog(now=10))
            self.assertIsNone(reference.load_catalog(now=11))
            self.assertEqual(opener.calls, 1)
            self.assertIsNone(reference.load_catalog(now=10 + reference.FAILURE_TTL))
            self.assertEqual(opener.calls, 2)

    def test_many_models_share_an_index_and_preserve_source(self):
        import cc_switch_metadata as metadata
        document = {'vendor': {'api': 'https://api.example.invalid/v1', 'models': {
            'alpha': {'limit': {'context': 128000}},
            'beta': {'reasoning_options': [{'type': 'effort', 'values': ['high']}]},
        }}}
        with patch('cc_switch_metadata.build_metadata_index', wraps=metadata.build_metadata_index) as build:
            values = reference.reference_for_models('https://api.example.invalid/v1', ['alpha', 'beta'], loader=lambda: document)
            reference.reference_for_models('https://api.example.invalid/v1', ['alpha'], loader=lambda: document)
        self.assertEqual(build.call_count, 1)
        self.assertEqual(values['alpha']['context_window'], 128000)
        self.assertEqual(values['alpha']['sources']['context_window'], 'models.dev:same-provider')
        self.assertEqual(values['beta']['reasoning_levels'], ['high'])


if __name__ == '__main__':
    unittest.main()
