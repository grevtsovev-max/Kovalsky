from __future__ import annotations

import io
import json
import os
import unittest
from unittest.mock import patch

from newsroom.core import fetch_web_search, fetch_x_recent


class MockResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


class SearchIntegrationTests(unittest.TestCase):
    def test_discovery_only_search_saves_links_without_reading_pages(self):
        response = {'output': [{'content': [{'annotations': [
            {'type': 'url_citation', 'url': 'https://publisher.example/news', 'title': 'Новое решение'}
        ]}]}]}
        with patch('newsroom.core.request_response', return_value=response), \
             patch('newsroom.core.fetch_publisher_article') as read:
            items = fetch_web_search('news', {'web_search_enabled': True}, read_articles=False)
        read.assert_not_called()
        self.assertEqual(items[0]['url'], 'https://publisher.example/news')
        self.assertIsNone(items[0]['published_at'])
        self.assertNotIn('material_read', items[0])

    def test_discovery_fallback_also_defers_article_reading(self):
        from newsroom.ai import AIResponseError
        with patch('newsroom.core.request_response', side_effect=AIResponseError('NETWORK_TIMEOUT')), \
             patch('newsroom.core.fetch_google_news', return_value=[]) as fallback:
            fetch_web_search('news', {'web_search_enabled': True}, read_articles=False)
        self.assertFalse(fallback.call_args.kwargs['read_articles'])

    def test_web_search_citations_are_read_as_publisher_pages(self):
        response = {"output": [{"content": [{"type": "output_text", "text": "A recent report.",
                    "annotations": [{"type": "url_citation", "url": "https://publisher.example/news",
                                     "title": "Publisher report"}]}]}]}
        with patch.dict(os.environ, {"SEARCH_TEST_KEY": "test-key"}), \
             patch("newsroom.core.urllib.request.urlopen", return_value=MockResponse(json.dumps(response).encode())), \
             patch("newsroom.core.fetch_publisher_article", return_value={"url": "https://publisher.example/news",
                   "title": "Publisher report", "content": "Full publisher text", "primary_source_status": "READ"}) as read:
            items = fetch_web_search("crypto news", {"api_key_env": "SEARCH_TEST_KEY", "model": "test-model", "web_search_enabled": True})
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["content"], "Full publisher text")
        read.assert_called_once_with("https://publisher.example/news", "publisher.example", None,
                                     timeout=20, public_only=True)

    def test_x_recent_search_preserves_post_id_text_and_direct_source(self):
        response = {"data": [{"id": "123", "text": "A direct company announcement", "author_id": "9",
                              "created_at": "2026-09-26T09:00:00Z"}],
                    "includes": {"users": [{"id": "9", "username": "issuer", "name": "Issuer"}]}}
        with patch.dict(os.environ, {"X_SEARCH_TEST_TOKEN": "test-token"}), \
             patch("newsroom.core.urllib.request.urlopen", return_value=MockResponse(json.dumps(response).encode())):
            items = fetch_x_recent("crypto lang:en", {"token_env": "X_SEARCH_TEST_TOKEN"})
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["url"], "https://x.com/issuer/status/123")
        self.assertEqual(items[0]["primary_source"]["content"], "A direct company announcement")
        self.assertEqual(items[0]["primary_source_status"], "READ")
        self.assertEqual(items[0]["primary_source_type"], "SOCIAL_POST")

    def test_x_missing_token_fails_with_safe_code(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(RuntimeError, "X_CREDENTIALS_MISSING"):
                fetch_x_recent("crypto", {"token_env": "X_SEARCH_TEST_TOKEN"})


if __name__ == "__main__":
    unittest.main()
