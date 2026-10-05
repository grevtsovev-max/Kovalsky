import gzip
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from newsroom.core import _request_with_url, _safe_source_error, fetch_publisher_article, run_cycle
from newsroom.cli import _performance_summary

class Response:
    def __init__(self,payload):self.payload=payload;self.headers=self
    def get_content_type(self):return 'text/html'
    def geturl(self):return 'https://publisher.example/article'
    def __enter__(self):return self
    def __exit__(self,*args):return False
    def read(self,limit):return self.payload[:limit]

class TransportTests(unittest.TestCase):
    def test_gzip_is_read_as_full_article(self):
        body='<html><h1>Новости цифровых активов</h1><p>'+('Полный текст статьи о криптовалютах. '*12)+'</p></html>'
        with patch('newsroom.core.urllib.request.urlopen',return_value=Response(gzip.compress(body.encode()))):
            article=fetch_publisher_article('https://publisher.example/article','Publisher',None,discover_primary=False)
        self.assertIn('Полный текст статьи',article['content'])
        self.assertNotEqual(article['primary_source_status'],'READ')
    def test_gzip_expansion_is_bounded(self):
        with patch('newsroom.core.urllib.request.urlopen',return_value=Response(gzip.compress(b'a'*10_000_001))):
            with self.assertRaisesRegex(ValueError,'RESPONSE_TOO_LARGE'):_request_with_url('https://publisher.example/article')
    def test_broken_gzip_has_safe_diagnostic(self):
        with patch('newsroom.core.urllib.request.urlopen',return_value=Response(b'\x1f\x8btruncated')):
            with self.assertRaises(ValueError) as caught:_request_with_url('https://publisher.example/article')
        self.assertEqual(_safe_source_error(caught.exception),'INVALID_GZIP_RESPONSE')
    def test_javascript_challenge_is_not_an_article(self):
        body=b'<html><js-challenge-loader></js-challenge-loader><p>'+b'placeholder '*100+b'</p></html>'
        with patch('newsroom.core._request_with_url',return_value=(body,'https://publisher.example/article','text/html')):
            with self.assertRaises(ValueError) as caught:fetch_publisher_article('https://publisher.example/article','Publisher',None)
        self.assertEqual(_safe_source_error(caught.exception),'PUBLISHER_BROWSER_CHALLENGE')
    def test_http_403_retries_once_with_browser_headers(self):
        error=HTTPError('https://publisher.example/article',403,'Forbidden',{},None)
        with patch('newsroom.core.time.sleep'),patch('newsroom.core.urllib.request.urlopen',side_effect=[error,Response(b'ok')]) as opener:
            self.assertEqual(_request_with_url('https://publisher.example/article')[0],b'ok')
        self.assertEqual(opener.call_count,2)
        self.assertIn('Mozilla',opener.call_args.args[0].get_header('User-agent'))
    def test_persistent_http_403_stops_after_two_requests(self):
        with patch('newsroom.core.time.sleep'),patch('newsroom.core.urllib.request.urlopen',side_effect=HTTPError('https://publisher.example/article',403,'Forbidden',{},None)) as opener:
            with self.assertRaises(HTTPError) as caught:_request_with_url('https://publisher.example/article')
        self.assertEqual(opener.call_count,2)
        self.assertEqual(_safe_source_error(caught.exception),'HTTP_403')

class StageTimingTests(unittest.TestCase):
    def test_cycle_stage_accounting_and_health_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg={'newsroom':{'database':str(Path(tmp)/'test.db')},'sources':[{'name':'Test','type':'rss','url':'https://example.org/feed'}]}
            with patch('newsroom.core.fetch_rss',return_value=[]),patch('newsroom.core._log_timing') as log:
                self.assertEqual(run_cycle(cfg),{})
            calls=[call for call in log.call_args_list if call.args[0]=='collection_stage_timing']
            self.assertEqual(len(calls),1);fields=calls[0].kwargs
            stages=['retry_seconds','fetch_wait_seconds','matching_seconds','processing_seconds','other_seconds']
            self.assertAlmostEqual(sum(fields[k] for k in stages),fields['total_seconds'],delta=0.004)
            self.assertTrue(all(fields[k]>=0 for k in stages))
            now=datetime.now(timezone.utc)
            event=dict(fields,event='collection_stage_timing',timestamp=now.isoformat())
            (Path(tmp)/'newsroom-runtime.log').write_text(json.dumps(event)+'\n')
            report='\n'.join(_performance_summary(cfg,now))
            self.assertIn('Разбивка сбора: 1 циклов',report)
            self.assertIn('Подбор независимых источников:',report)
