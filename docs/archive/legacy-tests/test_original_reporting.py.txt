import unittest
from unittest.mock import patch
from newsroom.core import fetch_publisher_article, require_primary_source_review

class OriginalReportingTests(unittest.TestCase):
    def fetch(self, host='tass.ru', text=None, meta=False, linked=False):
        text = text or ('Секция совета предложила использовать цифровую валюту как залог. Об этом сообщил ТАСС сенатор Шейкин. ' * 2)
        page = ('<html><title>Залог цифровой валюты</title>' +
                ('<meta name="description" content="'+text+'">' if meta else '<p>'+text+'</p>') +
                ('<a href="https://cbr.ru/doc">официальный документ</a>' if linked else '') + '</html>')
        with patch('newsroom.core._request_with_url', side_effect=[(page.encode(), 'https://'+host+'/story', 'text/html'), TimeoutError()]):
            return fetch_publisher_article('https://'+host+'/story','ТАСС',None)
    def test_own_interview_is_read(self):
        a=self.fetch(); self.assertEqual(a['primary_source_type'],'ORIGINAL_MEDIA_INTERVIEW');self.assertEqual(a['primary_source_status'],'READ')
    def test_own_document_report_is_read_but_not_document(self):
        a=self.fetch(text='Решение секции совета имеется в распоряжении ТАСС. Предложено проработать залог цифровой валюты. '*2)
        self.assertEqual(a['primary_source_type'],'ORIGINAL_MEDIA_REPORT')
    def test_reprint_and_impostor_not_promoted(self):
        for host in ['bits.media','tass.ru.example.com']:
            self.assertNotEqual(self.fetch(host)['primary_source_status'],'READ')
    def test_brand_without_original_reporting_not_promoted(self):
        self.assertNotEqual(self.fetch(text='По данным другого издания, сенатор выступил с предложением проработать залог цифровой валюты. '*2)['primary_source_status'],'READ')
    def test_metadata_only_not_read(self):
        self.assertNotEqual(self.fetch(meta=True)['primary_source_status'],'READ')
    def test_unread_document_allows_only_original_report(self):
        self.assertEqual(self.fetch(linked=True)['primary_source_type'],'ORIGINAL_MEDIA_INTERVIEW')
    def test_editor_must_verify_central_claim_and_attribution(self):
        text='Об этом сообщил ТАСС сенатор Шейкин: цифровую валюту предложено использовать как залог.'
        source={'url':'https://tass.ru/story','content':text,'type':'ORIGINAL_MEDIA_INTERVIEW'}
        audit={'central_claim_supported':True,'attribution_preserved':True,'evidence':text}
        result={'publication_recommendation':'SEND_TO_REVIEW','facts':[{'text':'Предложение','claim_type':'CLAIM'}],'original_reporting_check':audit}
        self.assertEqual(require_primary_source_review(result,'READ',source)['publication_recommendation'],'SEND_TO_REVIEW')
        for bad in [dict(audit,evidence='выдуманная цитата '*4),dict(audit,central_claim_supported=False),dict(audit,attribution_preserved=False),{}]:
            held=require_primary_source_review(dict(result,original_reporting_check=bad),'READ',source)
            self.assertEqual(held['publication_recommendation'],'WAIT_FOR_AUTOMATION')
            self.assertTrue(held['source_review_issues'])
        bad=dict(result,facts=[{'claim_type':'FACT'}])
        self.assertEqual(require_primary_source_review(bad,'READ',source)['publication_recommendation'],'WAIT_FOR_AUTOMATION')
        self.assertIn('REPORT', require_primary_source_review(bad,'READ',source)['source_review_issues'][0])
    def test_unread_article_never_promoted(self):
        with patch('newsroom.core._request_with_url',side_effect=TimeoutError):
            with self.assertRaises(TimeoutError):fetch_publisher_article('https://tass.ru/story','ТАСС',None)
