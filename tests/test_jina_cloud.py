import io
import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from newsroom.jina_cloud import api_key, reserve
from newsroom.jina_reader import read_article, read_cloud_article


class CloudReaderTests(unittest.TestCase):
    def config(self, directory):
        key = Path(directory)/'key';key.write_text('test-key');key.chmod(0o600)
        return dict(jina_cloud_enabled=True,jina_api_key_file=str(key),
                    jina_cloud_ledger=str(Path(directory)/'ledger.sqlite3'),jina_cloud_daily_requests=2)

    def test_reservations_survive_reopening_and_failed_requests(self):
        with tempfile.TemporaryDirectory() as directory:
            settings=self.config(directory)
            reserve(settings);reserve(settings)
            with self.assertRaisesRegex(ValueError,'JINA_CLOUD_DAILY_LIMIT'):
                reserve(dict(settings))

    def test_cloud_uses_fixed_endpoint_and_private_key_and_limits(self):
        with tempfile.TemporaryDirectory() as directory:
            settings=self.config(directory)
            payload=io.BytesIO(json.dumps({'code':200,'data':{'url':'https://example.com',
                'content':'<html><title>Событие</title><p>'+('Фактическое содержание статьи. '*30)+'</p></html>'}}).encode())
            with patch('newsroom.core._validate_public_http_url'), patch.dict(os.environ,{},clear=True), \
                 patch('newsroom.jina_reader.urllib.request.build_opener') as opener:
                opener.return_value.open.return_value=payload
                article=read_cloud_article('https://example.com','Издание',None,settings,8,discover_primary=False)
            request=opener.return_value.open.call_args.args[0]
            self.assertEqual(request.full_url,'https://r.jina.ai/https://example.com')
            self.assertEqual(request.get_header('Authorization'),'Bearer test-key')
            self.assertEqual(request.get_header('X-token-budget'),'20000')
            self.assertEqual(article['reading_method'],'jina_cloud')
            with sqlite3.connect(settings['jina_cloud_ledger']) as db:
                self.assertEqual(db.execute('SELECT status,actual_tokens FROM reader_attempts').fetchone(),('READ',None))

    def test_local_timeout_does_not_immediately_call_cloud(self):
        with patch('newsroom.jina_reader._read_article',side_effect=TimeoutError), \
             patch('newsroom.jina_reader.read_cloud_article') as cloud:
            with self.assertRaises(TimeoutError):
                read_article('https://example.com','Издание',None,{'jina_cloud_enabled':True},8)
            cloud.assert_not_called()

    def test_private_key_world_readable_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ,{},clear=True):
            settings=self.config(directory);Path(settings['jina_api_key_file']).chmod(0o644)
            with self.assertRaisesRegex(ValueError,'JINA_KEY_FILE_PUBLIC'):
                api_key(settings)

    def test_disabled_cloud_does_not_call_network(self):
        with patch('newsroom.jina_reader._read_article') as request:
            with self.assertRaisesRegex(ValueError,'JINA_CLOUD_DISABLED'):
                read_cloud_article('https://example.com','Издание',None,{},8)
            request.assert_not_called()

    def test_failed_cloud_request_spends_attempt_without_storing_secret(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ,{},clear=True):
            settings=self.config(directory)
            with patch('newsroom.core._validate_public_http_url'), \
                 patch('newsroom.jina_reader._read_article',side_effect=TimeoutError):
                with self.assertRaises(TimeoutError):
                    read_cloud_article('https://example.com','Издание',None,settings,8)
            with sqlite3.connect(settings['jina_cloud_ledger']) as db:
                self.assertEqual(db.execute('SELECT status,actual_tokens FROM reader_attempts').fetchone(),('FAILED',None))

    def test_cloud_markdown_preserves_end_of_text_and_primary_links(self):
        with tempfile.TemporaryDirectory() as directory:
            settings=self.config(directory)
            text='Текст сообщения. '*1000+'\n[Документ](https://www.cbr.ru/example)\nЗавершающее исключение.'
            payload=io.BytesIO(json.dumps({'code':200,'data':{'url':'https://example.com',
                'title':'Новое событие','content':text,'usage':{'tokens':4000}}}).encode())
            with patch('newsroom.core._validate_public_http_url'), patch.dict(os.environ,{},clear=True), \
                 patch('newsroom.jina_reader.urllib.request.build_opener') as opener:
                opener.return_value.open.return_value=payload
                article=read_cloud_article('https://example.com','Издание',None,settings,8,discover_primary=False)
            self.assertTrue(article['content'].endswith('Завершающее исключение.'))
            self.assertEqual(article['reader_content'],text)
            request=opener.return_value.open.call_args.args[0]
            self.assertEqual(request.get_header('X-respond-with'),'content')
            with sqlite3.connect(settings['jina_cloud_ledger']) as db:
                self.assertEqual(db.execute('SELECT actual_tokens FROM reader_attempts').fetchone()[0],4000)

    def test_private_url_is_rejected_before_cloud_reservation(self):
        with tempfile.TemporaryDirectory() as directory:
            settings=self.config(directory)
            with patch('newsroom.core._validate_public_http_url',side_effect=ValueError('URL_NOT_PUBLIC')):
                with self.assertRaisesRegex(ValueError,'URL_NOT_PUBLIC'):
                    read_cloud_article('https://127.0.0.1','Издание',None,settings,8)
            self.assertFalse(Path(settings['jina_cloud_ledger']).exists())

    def test_access_denial_uses_cloud_reserve(self):
        with patch('newsroom.jina_reader._read_article',side_effect=ValueError('JINA_ACCESS_DENIED')), \
             patch('newsroom.jina_reader.read_cloud_article',return_value={'material_read':True}) as cloud:
            row=read_article('https://tass.ru/ekonomika/1','ТАСС',None,{'jina_cloud_enabled':True},8)
            self.assertTrue(row['material_read'])
            cloud.assert_called_once()

    def test_tass_correct_metadata_does_not_validate_unrelated_body(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ,{},clear=True):
            settings=self.config(directory)
            payload=io.BytesIO(json.dumps({'code':200,'data':{'url':'https://tass.ru/ekonomika/1',
                'title':'ЦБ планирует усовершенствовать типы смарт-контрактов для бюджетного процесса',
                'content':'Люди с расстройством аутистического спектра ощущают мир иначе. '*20}}).encode())
            with patch('newsroom.core._validate_public_http_url'), \
                 patch('newsroom.jina_reader.urllib.request.build_opener') as opener:
                opener.return_value.open.return_value=payload
                with self.assertRaisesRegex(ValueError,'JINA_TITLE_BODY_MISMATCH'):
                    read_cloud_article('https://tass.ru/ekonomika/1','ТАСС',None,settings,8)
