import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from newsroom import material_store
from newsroom.core import fetch_publisher_article


class MaterialStoreTests(unittest.TestCase):
    def tearDown(self):
        material_store.configure({})

    def test_full_text_is_preserved_and_versions_survive_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            material_store.configure({'newsroom': {'database': str(Path(directory)/'db.sqlite3')}})
            url = 'https://news.example/article'
            tail = 'Конец статьи с исключением из правила.'
            text = 'Подробное описание события. ' * 700 + tail
            payload = ('<html><title>Новость</title><p>'+text+'</p></html>').encode()
            with patch('newsroom.core._request_with_url', return_value=(payload, url, 'text/html')):
                article = fetch_publisher_article(url, 'Издание', None, discover_primary=False)
            target = Path(directory)/'materials'/article['archive_key']
            self.assertTrue(article['content'].endswith(tail))
            self.assertEqual(json.loads(target.read_text())['content'], article['content'])
            original = target.read_bytes()
            material_store.save({**article, 'content': article['content']+' Дополнение.'})
            self.assertEqual(target.read_bytes(), original)
            self.assertEqual(len(list((Path(directory)/'materials').rglob('*.json'))), 2)

    def test_unread_material_is_not_archived(self):
        with tempfile.TemporaryDirectory() as directory:
            material_store.configure({'reader': {'archive_directory': directory}})
            material_store.save({'url': 'https://example.com', 'content': 'анонс', 'material_read': False})
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_timeout_does_not_trigger_jina(self):
        material_store.configure({'reader': {'jina_enabled': True}})
        with patch('newsroom.core._fetch_publisher_article_native', side_effect=TimeoutError), \
             patch('newsroom.jina_reader.read_article') as fallback:
            with self.assertRaises(TimeoutError):
                fetch_publisher_article('https://example.com/article', 'Издание', None)
            fallback.assert_not_called()

    def test_blocked_private_address_does_not_trigger_jina(self):
        material_store.configure({'reader': {'jina_enabled': True}})
        with patch('newsroom.core._fetch_publisher_article_native', side_effect=ValueError('URL_NOT_PUBLIC')), \
             patch('newsroom.jina_reader.read_article') as fallback:
            with self.assertRaises(ValueError):
                fetch_publisher_article('https://127.0.0.1/article', 'Издание', None, public_only=True)
            fallback.assert_not_called()

    def test_jina_rendered_article_uses_normal_parser_and_keeps_full_text(self):
        import io
        from newsroom.jina_reader import read_article
        url = 'https://news.example/article'
        text = 'Прочитанный текст о событии. ' * 600
        response = io.BytesIO(json.dumps({'code': 200, 'data': {
            'url': url, 'content': '<html><title>Событие</title><p>'+text+'</p></html>'}}).encode())
        with patch('newsroom.core._validate_public_http_url'), \
             patch('newsroom.jina_reader.urllib.request.build_opener') as opener:
            opener.return_value.open.return_value = response
            article = read_article(url, 'Издание', None,
                {'jina_endpoint': 'http://127.0.0.1:3000'}, 8, discover_primary=False)
        self.assertTrue(article['material_read'])
        self.assertEqual(article['reading_method'], 'jina')
        self.assertGreater(len(article['content']), 12000)
        self.assertEqual(article['primary_source_status'], 'NOT_CHECKED')

    def test_jina_rejects_remote_endpoint(self):
        from newsroom.jina_reader import read_article
        with patch('newsroom.core._validate_public_http_url'):
            with self.assertRaisesRegex(ValueError, 'JINA_ENDPOINT_NOT_LOOPBACK'):
                read_article('https://news.example/article', 'Издание', None,
                    {'jina_endpoint': 'https://r.jina.ai'}, 8)

    def test_concurrent_save_keeps_first_snapshot(self):
        from concurrent.futures import ThreadPoolExecutor
        with tempfile.TemporaryDirectory() as directory:
            material_store.configure({'reader': {'archive_directory': directory}})
            article = {'url': 'https://news.example/article', 'material_read': True,
                       'content': 'Полный прочитанный текст события.'}
            with ThreadPoolExecutor(max_workers=4) as workers:
                results = list(workers.map(lambda _: material_store.save(dict(article)), range(12)))
            self.assertEqual(len({row['archive_key'] for row in results}), 1)
            files = list(Path(directory).rglob('*.json'))
            self.assertEqual(len(files), 1)
            self.assertEqual(json.loads(files[0].read_text())['content'], article['content'])
            self.assertEqual(len(list(files[0].parent.iterdir())), 1)

    def test_jina_does_not_archive_publisher_error_page(self):
        import io
        from newsroom.jina_reader import read_article
        payload = io.BytesIO(json.dumps({'code': 200, 'data': {'url': 'https://example.com',
            'content': '<html><title>404 Page not found</title><p>' + 'Ошибка. ' * 50 + '</p></html>'}}).encode())
        with patch('newsroom.core._validate_public_http_url'), \
             patch('newsroom.jina_reader.urllib.request.build_opener') as opener:
            opener.return_value.open.return_value = payload
            with self.assertRaisesRegex(ValueError, 'JINA_PUBLISHER_ERROR'):
                read_article('https://example.com', 'Издание', None,
                    {'jina_endpoint': 'http://127.0.0.1:3000'}, 8)

    def test_jina_forbidden_page_is_not_a_read_article(self):
        import io
        from newsroom.jina_reader import _read_article
        payload=io.BytesIO(json.dumps({'code':200,'data':{'url':'https://tass.ru/ekonomika/1',
            'content':'<html><title>Forbidden</title><p>'+('Доступ запрещён. '*20)+'</p></html>'}}).encode())
        with patch('newsroom.core._validate_public_http_url'), \
             patch('newsroom.jina_reader.urllib.request.build_opener') as opener:
            opener.return_value.open.return_value=payload
            with self.assertRaisesRegex(ValueError,'JINA_ACCESS_DENIED'):
                _read_article('https://tass.ru/ekonomika/1','ТАСС',None,
                    {'jina_endpoint':'http://127.0.0.1:3000'},8,discover_primary=False)
