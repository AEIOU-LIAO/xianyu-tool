"""Local HTTP boundary and CSV regression tests. No real browser or runtime data."""
import csv
import io
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app


class LocalSecurityTests(unittest.TestCase):
    base = 'http://localhost:8000'

    def setUp(self):
        self.client = app.app.test_client()

    def test_head_settings_only_reads(self):
        with patch.object(app.storage, 'load_settings', return_value={}) as read, \
             patch.object(app.storage, 'update_settings') as write:
            response = self.client.head('/api/settings', base_url=self.base)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data, b'')
        read.assert_called_once_with()
        write.assert_not_called()

    def test_page_cannot_be_framed_and_private_responses_are_not_cached(self):
        with self.client.get('/', base_url=self.base) as page:
            self.assertEqual(page.status_code, 200)
            self.assertIn("frame-ancestors 'none'", page.headers['Content-Security-Policy'])
            self.assertEqual(page.headers['X-Frame-Options'], 'DENY')
        with patch.object(app.storage, 'load_settings', return_value={}):
            response = self.client.get('/api/settings', base_url=self.base)
        self.assertTrue(response.cache_control.no_store)
        self.assertEqual(response.headers['Cross-Origin-Resource-Policy'], 'same-origin')

    def test_only_public_skin_assets_are_served_from_existing_packs(self):
        with tempfile.TemporaryDirectory() as root, patch.object(app, 'BASE_DIR', root):
            skins = Path(root, 'static', 'skins')
            (skins / 'thumbs').mkdir(parents=True)
            for filename in ('skins.json', 'wall.webp', 'thumbs/picture.png', 'notes.html'):
                (skins / filename).write_bytes(b'test fixture')
            for filename in ('skins.json', 'wall.webp', 'thumbs/picture.png'):
                with self.client.get('/static/skins/' + filename, base_url=self.base) as response:
                    self.assertEqual(response.status_code, 200)
                    self.assertIn('sandbox', response.headers['Content-Security-Policy'])
                    self.assertEqual(response.headers['X-Content-Type-Options'], 'nosniff')
            self.assertEqual(self.client.get('/static/skins/notes.html', base_url=self.base).status_code, 404)

    def test_cross_origin_metadata_rejected_before_api_access(self):
        with patch.object(app.storage, 'load_settings') as read, \
             patch.object(app.storage, 'update_settings') as write:
            for site in ('same-site', 'cross-site'):
                with self.subTest(site=site):
                    response = self.client.get('/api/settings', base_url=self.base,
                                               headers={'Sec-Fetch-Site': site})
                    self.assertEqual(response.status_code, 403)
            for headers in ({'Origin': 'http://127.0.0.1:8000'},
                            {'Referer': 'http://localhost:9000/'}):
                with self.subTest(headers=headers):
                    response = self.client.post('/api/settings', base_url=self.base,
                                                headers=headers, json={})
                    self.assertEqual(response.status_code, 403)
            read.assert_not_called()
            write.assert_not_called()

    def test_linked_files_are_never_published_as_skin_assets(self):
        with tempfile.TemporaryDirectory() as root, patch.object(app, 'BASE_DIR', root):
            skins = Path(root, 'static', 'skins')
            skins.mkdir(parents=True)
            private = Path(root, 'private.json')
            private.write_text('{"sample": 1}', encoding='utf-8')
            for filename in ('skins.json', 'wall.webp'):
                os.link(private, skins / filename)
                response = self.client.get('/static/skins/' + filename, base_url=self.base)
                self.assertEqual(response.status_code, 404)
            response = self.client.get('/api/skins', base_url=self.base)
            self.assertEqual(response.json, {'count': 0, 'roles': [], 'skins': []})

    def test_both_documented_origins_accept_same_origin_writes(self):
        for base in (self.base, 'http://127.0.0.1:8000'):
            with self.subTest(base=base), patch.object(app.storage, 'update_settings', return_value={}) as write:
                response = self.client.post('/api/settings', base_url=base, json={},
                                            headers={'Origin': base, 'Sec-Fetch-Site': 'same-origin'})
                self.assertEqual(response.status_code, 200)
                write.assert_called_once()

    def test_json_writes_are_bounded_and_validate_streamed_bodies(self):
        with patch.object(app.storage, 'update_settings') as write:
            with patch.dict(app.app.config, MAX_CONTENT_LENGTH=32):
                response = self.client.post('/api/settings', base_url=self.base,
                                            json={'description': 'test record above a small fixture limit'})
                self.assertEqual(response.status_code, 413)
            response = self.client.post('/api/settings', base_url=self.base,
                                        data=b'[]', content_type='application/json',
                                        environ_overrides={'CONTENT_LENGTH': '', 'wsgi.input_terminated': True})
            self.assertEqual(response.status_code, 400)
            response = self.client.post('/api/settings', base_url=self.base,
                                        data=b'', content_type='application/x-www-form-urlencoded')
            self.assertEqual(response.status_code, 400)
            write.assert_not_called()

    def test_csv_treats_legacy_text_and_numeric_cells_by_their_type(self):
        item = {'title': '＠sample', 'price': '+unparsed price', 'seller_score': -2, 'seller': 'sample'}
        with patch.object(app, '_query_items', return_value=([item], [], [], [])):
            response = self.client.get('/api/export.csv', base_url=self.base)
        rows = list(csv.reader(io.StringIO(response.data.decode('utf-8-sig'))))
        self.assertEqual(rows[1][0:2], ["'＠sample", "'+unparsed price"])
        self.assertEqual(rows[1][7], '-2')

    def test_snapshot_ignores_unrepresentable_numbers(self):
        self.assertEqual(app._clean_item_snapshot({'price': 10**400, 'title': 'sample'}),
                         {'title': 'sample'})


if __name__ == '__main__':
    unittest.main()
