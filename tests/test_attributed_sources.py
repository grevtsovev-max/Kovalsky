# -*- coding: utf-8 -*-
import copy
import json
import unittest
from unittest.mock import patch

import test_memory_integration as integration
from newsroom.core import fetch_publisher_article, process_item, _retry_ai_held_items, make_post
from newsroom.cli import publish, auto_publish_since
from newsroom.quality import editorial_issues


class AttributedSourceTests(integration.MemoryIntegrationTests):
    def prepare_report(self, status='NO_LINK'):
        self.prepare()
        self.db.execute("UPDATE sources SET name='Издание',priority=1,reputation='unknown',source_role='aggregator'")
        self.source = self.db.execute('SELECT * FROM sources').fetchone()
        self.evidence = f'По данным издания, Банк России установил условия доступа российских участников к цифровым активам {self.now[:10]}.'
        self.article = dict(self.item, content=self.evidence, material_read=True,
                            material_url=self.item['url'], primary_source_status=status)
        result = self.result()
        result['summary_ru'] = self.evidence
        result['impact_evidence'] = self.evidence
        result['facts'][0]['claim_type'] = 'REPORT'
        result['memory']['claims'][0]['claim_type'] = 'REPORT'
        result['original_reporting_check'] = dict(central_claim_supported=True,
                                                 attribution_preserved=True, evidence=self.evidence)
        return result

    def process_report(self, result):
        with patch('newsroom.core.fetch_publisher_article', return_value=self.article), \
             patch('newsroom.core.get_api_key', return_value='test'), \
             patch('newsroom.core.analyze_with_ai', return_value=result), \
             patch('newsroom.core._recover_primary', side_effect=AssertionError('Read reports must not wait for search')):
            return process_item(self.db, self.source, dict(self.item), .35, 3500, 48,
                                ai_settings=self.config['ai'])

    def test_low_rank_aggregator_report_passes_memory_and_send_without_original(self):
        result = self.prepare_report()
        self.assertEqual(self.process_report(result), 'NEW_STORY')
        with patch('newsroom.cli.telegram_send', return_value='901') as send:
            publish(self.db, self.config, 1, automatic=True)
            self.assertIn(self.item['url'], send.call_args.args[1])
        row = self.db.execute('SELECT * FROM items').fetchone()
        self.assertEqual(json.loads(row['primary_source_json'])['status'], 'NO_LINK')
        self.assertEqual(self.db.execute('SELECT fact_type FROM story_facts').fetchone()[0], 'REPORT')

    def test_unread_document_does_not_override_read_article(self):
        result = self.prepare_report('OCR_REVIEW')
        self.article.update(primary_source_url='https://example.org/unread.pdf',
                            primary_source_content='OCR text must not be cited', primary_source_type='LINKED_DOCUMENT')
        self.assertEqual(self.process_report(result), 'NEW_STORY')
        with patch('newsroom.cli.telegram_send', return_value='902') as send:
            publish(self.db, self.config, 1, automatic=True)
            self.assertNotIn('unread.pdf', send.call_args.args[1])
        facts = json.loads(self.db.execute('SELECT fact_check_result FROM posts').fetchone()[0])
        self.assertIsNone(facts['primary_source'])
        self.assertEqual(facts['primary_source_status'], 'OCR_REVIEW')

    def test_report_cannot_promote_claim_to_fact(self):
        result = self.prepare_report()
        result['facts'][0]['claim_type'] = 'FACT'
        self.assertEqual(self.process_report(result), 'WAITING_CONFIRMATION')
        self.assertEqual(self.db.execute('SELECT count(*) FROM posts').fetchone()[0], 0)

    def test_admits_possible_market_access_is_an_event_headline(self):
        result = self.prepare_report()
        issues = editorial_issues('🇷🇺 Минфин допускает выход иностранных криптокомпаний в Россию',self.evidence,result)
        self.assertNotIn('HEADLINE_NOT_EVENT_LED',issues)

    def test_report_with_invented_evidence_does_not_publish(self):
        result = self.prepare_report()
        result['original_reporting_check']['evidence'] = 'Этой выдуманной цитаты нет в прочитанном материале.'
        self.assertEqual(self.process_report(result), 'WAITING_CONFIRMATION')

    def test_final_gate_rechecks_report_content(self):
        result = self.prepare_report()
        self.process_report(result)
        facts = json.loads(self.db.execute('SELECT fact_check_result FROM posts').fetchone()[0])
        facts['publisher_report']['content'] = 'Different content'
        self.db.execute('UPDATE posts SET fact_check_result=?', (json.dumps(facts),))
        with patch('newsroom.cli.telegram_send') as send:
            with self.assertRaises(RuntimeError):
                publish(self.db, self.config, 1, automatic=True)
            send.assert_not_called()

    def test_scoped_autopublish_does_not_touch_unselected_post(self):
        result = self.prepare_report()
        self.process_report(result)
        db_path = str(self.db.execute('PRAGMA database_list').fetchone()[2])
        self.db.commit()
        with patch('newsroom.cli.telegram_send') as send:
            self.assertEqual(auto_publish_since(db_path,self.config,post_ids=[]),(0,0,0))
            send.assert_not_called()
        self.assertEqual(self.db.execute('SELECT status FROM posts').fetchone()[0], 'PENDING')

    def test_ai_retry_keeps_read_report_without_requiring_original(self):
        result = self.prepare_report()
        self.article['publisher_name'] = 'Прочитанное издание'
        self.assertEqual(self.process_report(None), 'AI_RETRY')
        with patch('newsroom.core.fetch_publisher_article', side_effect=AssertionError('Already read')), \
             patch('newsroom.core.get_api_key', return_value='test'), \
             patch('newsroom.core.analyze_with_ai', return_value=result):
            outcome = _retry_ai_held_items(self.db, {self.source['source_id']: self.source}, self.config)
        self.assertEqual(outcome, {'NEW_STORY': 1})
        facts = json.loads(self.db.execute('SELECT fact_check_result FROM posts').fetchone()[0])
        self.assertEqual(facts['publisher_report']['publisher'], 'Прочитанное издание')

    def test_inconsistent_publish_recommendation_gets_rechecked(self):
        result = self.prepare_report()
        result['memory']['claims'][0]['material'] = False
        result['memory']['claims'][0]['material_reason'] = ''
        self.assertEqual(self.process_report(result), 'WAITING_CONFIRMATION')
        analysis = json.loads(self.db.execute('SELECT result_json FROM item_analysis').fetchone()[0])
        self.assertIn('PUBLICATION_RECOMMENDATION_WITHOUT_MATERIAL_FACT', analysis['memory_issues'][0])


class ReadProvenanceTests(unittest.TestCase):
    def test_source_footer_is_assembled_once(self):
        post = make_post('Регулятор предложил изменение', 'Текст сообщения.\n\nИсточник: [Издание](https://example.org/story)', 'Издание', 'https://example.org/story',3500)
        self.assertEqual(post.count('Источник:'), 1)

    def test_metadata_is_not_read_material(self):
        text = 'A long description that is only metadata, not the body of the article. ' * 3
        page = '<html><title>News</title><meta name="description" content="' + text + '"></html>'
        with patch('newsroom.core._request_with_url', return_value=(page.encode(), 'https://example.org/story', 'text/html')):
            article = fetch_publisher_article('https://example.org/story', 'Publisher', None)
        self.assertFalse(article['material_read'])

    def test_read_article_survives_linked_document_failure(self):
        text = 'This is a full article reporting a relevant decision and attributing it to the named institution. ' * 3
        page = '<html><p>' + text + '<a href="https://cbr.ru/doc">официальный документ</a></p></html>'
        with patch('newsroom.core._request_with_url', side_effect=[(page.encode(), 'https://example.org/story', 'text/html'), TimeoutError()]):
            article = fetch_publisher_article('https://example.org/story', 'Publisher', None)
        self.assertTrue(article['material_read'])
        self.assertEqual(article['primary_source_status'], 'UNREADABLE')
