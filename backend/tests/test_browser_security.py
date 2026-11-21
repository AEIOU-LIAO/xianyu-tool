import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import scraper
from browser_flow import BrowserCollection, is_search_response_url, platform_https_url


class BrowserSecurityTests(unittest.TestCase):
    def test_search_observer_uses_the_documented_endpoint(self):
        response = Mock(url=scraper.MTOP_BASE, status=200)
        response.request.url = scraper.MTOP_BASE
        response.request.post_data = 'data=%7B%22keyword%22%3A%22fixture%22%7D'
        response.json.return_value = {'ret': ['SUCCESS'], 'data': {'resultList': []}}
        collector = BrowserCollection()
        collector._results = []
        collector._observe(response)
        self.assertEqual(len(collector._results), 1)
        for url in (scraper.MTOP_BASE.replace('h5api.m.goofish.com', 'example.org'),
                    scraper.MTOP_BASE.replace('https:', 'http:'), scraper.MTOP_BASE + 'other'):
            with self.subTest(url=url):
                self.assertFalse(is_search_response_url(url))
                response.url = url
                collector._observe(response)
        self.assertEqual(len(collector._results), 1)
        response.json.assert_called_once_with()

    def test_verification_uses_https_platform_origins(self):
        self.assertTrue(platform_https_url('https://login.taobao.com/'))
        self.assertTrue(platform_https_url('https://www.goofish.com:443/'))
        for url in ('https://example.org/', 'http://www.goofish.com/', 'https://www.goofish.com:1234/'):
            self.assertIsNone(platform_https_url(url))

    def test_snapshot_and_login_only_use_unexpired_platform_cookies(self):
        trusted = {'domain': '.goofish.com', 'name': 'unb', 'value': 'fixture', 'expires': -1}
        unrelated = {'domain': '.example.org', 'name': 'cookie2', 'value': 'other', 'expires': -1}
        context = Mock()
        context.cookies.return_value = [trusted, unrelated, dict(trusted, name='expired', expires=1)]
        worker = scraper.Scraper()
        with tempfile.TemporaryDirectory() as directory, patch.object(scraper, 'DATA_DIR', directory):
            worker._save_session(context)
            target = Path(directory, 'browser-session.json')
            self.assertEqual(json.loads(target.read_text(encoding='utf-8')), {'cookies': [trusted], 'origins': []})
            self.assertFalse(worker._has_login_cookie(context))
            target.write_text(json.dumps({'cookies': [trusted, unrelated]}), encoding='utf-8')
            self.assertFalse(worker.is_logged_in())
            context.cookies.return_value.append(dict(unrelated, domain='.goofish.com'))
            worker._save_session(context)
            self.assertTrue(worker.is_logged_in())
            previous = target.read_bytes()
            with patch.object(scraper.os, 'replace', side_effect=OSError('write failed')):
                with self.assertRaises(OSError):
                    worker._save_session(context)
            self.assertEqual(target.read_bytes(), previous)
            self.assertEqual(list(Path(directory).iterdir()), [target])

    def test_legacy_migration_skips_redirected_origin(self):
        worker = scraper.Scraper()
        with tempfile.TemporaryDirectory() as directory:
            profile = Path(directory, 'profile')
            profile.mkdir()
            session = Path(directory, 'session.json')
            trusted = {'domain': '.taobao.com', 'name': 'cookie2', 'value': 'fixture'}
            session.write_text(json.dumps({
                'cookies': [trusted, dict(trusted, domain='.example.org')],
                'origins': [{'origin': 'https://www.goofish.com',
                             'localStorage': [{'name': 'test', 'value': 'fixture'}]}]
            }), encoding='utf-8')
            with patch.object(scraper, 'DATA_DIR', directory), patch.object(scraper, 'PROFILE_DIR', str(profile)), \
                 patch.object(scraper, 'SESSION_FILE', str(session)), patch.object(scraper, 'sync_playwright') as pw:
                context = pw.return_value.start.return_value.chromium.launch_persistent_context.return_value
                seed = context.new_page.return_value
                seed.evaluate.return_value = 'https://example.org'
                worker._launch()
                try:
                    context.add_cookies.assert_called_once_with([trusted])
                    seed.evaluate.assert_called_once_with('location.origin')
                finally:
                    worker.close()
