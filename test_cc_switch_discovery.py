import unittest
from urllib.parse import urlsplit

from cc_switch_model_discovery import model_id, model_url_candidates


class CCSDiscoveryTests(unittest.TestCase):
    def test_existing_model_endpoint_stays_first(self):
        self.assertEqual(model_url_candidates('https://example.invalid/v1'),
                         ['https://example.invalid/v1/models'])
        self.assertEqual(model_url_candidates('https://example.invalid'),
                         ['https://example.invalid/models', 'https://example.invalid/v1/models'])

    def test_ccs_compatible_paths_stay_on_the_same_origin(self):
        urls = model_url_candidates('https://example.invalid/apps/anthropic')
        self.assertIn('https://example.invalid/compatible-mode/v1/models', urls)
        self.assertIn('https://example.invalid/v1/models', urls)
        self.assertEqual(len(urls), len(set(urls)))
        self.assertTrue(all(urlsplit(url).netloc == 'example.invalid' for url in urls))
        self.assertIn('https://example.invalid/api/v4/v1/models',
                      model_url_candidates('https://example.invalid/api/v4'))

    def test_rejects_nonlocal_http_and_embedded_credentials(self):
        for value in ('http://example.invalid/v1', 'https://key@example.invalid/v1',
                      'https://example.invalid/v1?secret=x', 'https://example.invalid/a/../b'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                model_url_candidates(value)
        self.assertTrue(model_url_candidates('http://127.0.0.1:1234/v1'))

    def test_supports_ccs_catalog_shapes(self):
        self.assertEqual(model_id({'id': 'alpha'}), 'alpha')
        self.assertEqual(model_id({'slug': 'beta'}), 'beta')
        self.assertEqual(model_id({'name': 'models/gamma'}), 'gamma')
        self.assertIsNone(model_id({'name': 'not-a-model'}))
        self.assertIsNone(model_id({'id': 42}))


if __name__ == '__main__':
    unittest.main()
