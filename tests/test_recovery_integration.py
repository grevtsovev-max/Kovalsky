import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch
from newsroom.db import connect
from newsroom.ai import AIResponseError
from newsroom.core import process_item, _retry_ai_held_items
from newsroom.cli import is_eligible_for_auto_publish

class RecoveryIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.db = connect(str(Path(self.tmp.name)/'test.db')); self.addCleanup(self.db.close)
        self.db.execute("INSERT INTO sources(name,type,url) VALUES('Регулятор','rss','https://example.org/feed')")
        self.source = self.db.execute('SELECT * FROM sources').fetchone()
        self.now = datetime.now(timezone.utc).isoformat(timespec='seconds')
        self.evidence = 'Банк России установил условия доступа российских участников к цифровым активам.'
        self.item = {'url':'https://example.org/story','title':'Новые правила цифровых активов в России','content':'Изменения условий для участников рынка.', 'published_at': self.now}
        self.article = dict(self.item, primary_source_status='READ', primary_source_url='https://www.cbr.ru/crypto', primary_source_title='Новые правила', primary_source_content=self.evidence, primary_source_type='OFFICIAL')
        self.result = {'action':'NEW_STORY','is_relevant':True,'geographic_scope':'RUSSIA','confidence':0.9,'russia_cis_impact':'DIRECT','impact_evidence':self.evidence,'topic_category':'REGULATION','importance':'HIGH','editorial_check':{"source_matches_event": True, "attribution_preserved": True, "stage_preserved": True, "history_required": False, "history_explained": False, "history_note": "", "headline_main_event": True, "lead_event_first": True, "paragraphs_concise_distinct": True, "no_editorial_process_notes": True},'headline_ru':'🇷🇺 Банк России установил новые правила цифровых активов','summary_ru':self.evidence,'publication_recommendation':'AUTO_PUBLISH','independent_check':'NO_MATCH'}
        self.config = {'newsroom':{},'ai':{'model':'test'}}

    def process(self, settings=None):
        return process_item(self.db,self.source,dict(self.item),0.35,3500,48,ai_settings=settings or {'model':'test'})

    def assert_ready(self):
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM items').fetchone()[0],1)
        posts = self.db.execute('SELECT * FROM posts').fetchall()
        self.assertEqual(len(posts),1)
        self.assertTrue(is_eligible_for_auto_publish(posts[0],self.now))
        self.assertIn('https://www.cbr.ru/crypto',posts[0]['text'])

    def test_primary_failure_then_recovery_reuses_item_and_creates_one_post(self):
        with patch('newsroom.core.fetch_publisher_article',side_effect=TimeoutError), patch('newsroom.core.analyze_with_ai') as analyze:
            self.assertEqual(self.process(),'PRIMARY_RETRY'); analyze.assert_not_called()
        with patch('newsroom.core.fetch_publisher_article',return_value=self.article), patch('newsroom.core.get_api_key',return_value='test'), patch('newsroom.core.analyze_with_ai',return_value=self.result):
            self.assertEqual(_retry_ai_held_items(self.db,{self.source['source_id']:self.source},self.config),{'NEW_STORY':1})
            self.assertEqual(_retry_ai_held_items(self.db,{self.source['source_id']:self.source},self.config),{})
        self.assert_ready()

    def test_ai_failure_then_recovery_preserves_read_primary(self):
        with patch('newsroom.core.fetch_publisher_article',return_value=self.article), patch('newsroom.core.get_api_key',return_value='test'), patch('newsroom.core.analyze_with_ai',side_effect=AIResponseError('INVALID_STRUCTURED_OUTPUT_JSON')):
            self.assertEqual(self.process(),'AI_RETRY')
        with patch('newsroom.core.fetch_publisher_article') as fetch, patch('newsroom.core.get_api_key',return_value='test'), patch('newsroom.core.analyze_with_ai',return_value=self.result) as analyze:
            _retry_ai_held_items(self.db,{self.source['source_id']:self.source},self.config)
            fetch.assert_not_called()
            self.assertEqual(analyze.call_args.args[0]['primary_source']['content'],self.evidence)
        self.assert_ready()

    def test_token_limit_recovers_with_one_larger_request(self):
        with patch('newsroom.core.fetch_publisher_article',return_value=self.article), patch('newsroom.core.get_api_key',return_value='test'), patch('newsroom.core.analyze_with_ai',side_effect=[AIResponseError('OUTPUT_TOKEN_LIMIT'),self.result]) as analyze:
            self.assertEqual(self.process({'max_output_tokens':1800}),'NEW_STORY')
            self.assertEqual(analyze.call_count,2)
            self.assertEqual(analyze.call_args_list[1].args[3]['max_output_tokens'],3600)
        self.assert_ready()

    def test_two_token_limits_hold_item_without_post_or_third_attempt(self):
        with patch('newsroom.core.fetch_publisher_article',return_value=self.article), patch('newsroom.core.get_api_key',return_value='test'), patch('newsroom.core.analyze_with_ai',side_effect=AIResponseError('OUTPUT_TOKEN_LIMIT')) as analyze:
            self.assertEqual(self.process(),'AI_RETRY'); self.assertEqual(analyze.call_count,2)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM posts').fetchone()[0],0)

    def test_ungrounded_impact_does_not_create_post(self):
        result = dict(self.result,impact_evidence='Этой длинной цитаты нет в прочитанном первоисточнике о российском рынке.')
        with patch('newsroom.core.fetch_publisher_article',return_value=self.article), patch('newsroom.core.get_api_key',return_value='test'), patch('newsroom.core.analyze_with_ai',return_value=result):
            self.assertEqual(self.process(),'NOISE')
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM posts').fetchone()[0],0)
