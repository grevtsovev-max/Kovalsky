import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from newsroom.ai import AIResponseError, request_response
from newsroom.cli import load_config
from newsroom.core import fetch_web_search, run_cycle
from newsroom.db import connect
from newsroom.diagnostics import snapshot


class WebSearchDisabledTests(unittest.TestCase):
    def test_search_tools_never_reach_credentials_reservation_or_network(self):
        for tool in ('web_search', 'web_search_preview', 'web_search_preview_2025_03_11'):
            for flag in (None, False, 'true'):
                with self.subTest(tool=tool, flag=flag):
                    runtime = Mock()
                    settings = {'_runtime': runtime}
                    if flag is not None:
                        settings['web_search_enabled'] = flag
                    with patch('newsroom.ai.get_api_key') as key, \
                         patch('newsroom.ai.urllib.request.urlopen') as network:
                        with self.assertRaisesRegex(AIResponseError, 'WEB_SEARCH_DISABLED'):
                            request_response({'tools': [{'type': tool}]}, settings)
                    key.assert_not_called()
                    runtime.reserve.assert_not_called()
                    network.assert_not_called()

    def test_disabled_discovery_does_not_start_paid_search_or_fallback(self):
        with patch('newsroom.core.request_response') as api, \
             patch('newsroom.core.fetch_google_news') as fallback:
            result = fetch_web_search('news', {})
        self.assertEqual(result, [])
        self.assertIn('WEB_SEARCH_DISABLED', result.diagnostics)
        api.assert_not_called()
        fallback.assert_not_called()

    def test_ordinary_model_requests_still_work(self):
        response = Mock()
        response.read.return_value = json.dumps({'output': []}).encode()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        with patch('newsroom.ai.get_api_key', return_value='test'), \
             patch('newsroom.ai.urllib.request.urlopen', return_value=response) as network:
            self.assertEqual(request_response({'model': 'test'}, {}), {'output': []})
        network.assert_called_once()

    def test_loaded_legacy_config_excludes_search_sources_but_fetches_rss(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            database = path / 'news.sqlite3'
            config_file = path / 'config.toml'
            config_file.write_text(
                '[newsroom]\ndatabase = ' + json.dumps(str(database)) + '\n'
                '[ai]\nweb_search_enabled = true\n'
                '[[sources]]\nname = "Search"\ntype = "web_search"\n'
                'url = "https://example.org/search"\nactive = true\n'
                '[[sources]]\nname = "Feed"\ntype = "rss"\n'
                'url = "https://example.org/feed"\nactive = true\n', encoding='utf-8')
            config = load_config(str(config_file))
            self.assertFalse(config['ai']['web_search_enabled'])
            with connect(str(database)) as db:
                db.execute("INSERT INTO sources(name,type,url,active) VALUES('Search','web_search','https://example.org/search',1)")
            with patch('newsroom.core.fetch_web_search') as search, \
                 patch('newsroom.core.fetch_rss', return_value=[]) as rss:
                run_cycle(config)
            search.assert_not_called()
            rss.assert_called_once_with('https://example.org/feed')
            with connect(str(database)) as db:
                self.assertEqual(db.execute('SELECT COUNT(*) FROM api_usage').fetchone()[0], 0)
                state = snapshot(db, config)['web_search']
                self.assertEqual(state, {'enabled': False, 'active_sources': 0, 'last_api_attempt_at': None})


if __name__ == '__main__':
    unittest.main()
