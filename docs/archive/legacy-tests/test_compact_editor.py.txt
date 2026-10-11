import copy
import json
import unittest
from unittest.mock import patch

from newsroom import ai, compact_editor
from newsroom.core import process_item, _saved_material, digest
from newsroom.ai import validate_draft as real_validate_draft
from compact_fixtures import compact_response, response
from test_intake_rules import fixture
import test_recovery_integration as recovery


class CompactEditorTests(unittest.TestCase):
    setUp = recovery.RecoveryIntegrationTests.setUp

    def prepare(self):
        self.config['ai'].update(fixture()[2])
        self.config['ai']['memory_mode'] = 'enforce'
        self.item['title'] = 'Сбер выпустил цифровые активы'
        self.item['content'] = 'Сбер выпустил цифровые активы.'
        self.article.update(self.item)
        self.article['primary_source_content'] = self.item['content']
        self.evidence = self.item['content']
        self.value = compact_response(self.evidence)
        self.audit = copy.deepcopy(self.result['editorial_check'])

    def provider(self, payload, settings):
        if payload['text']['format']['name'] == 'newsroom_evidence_and_draft':
            return response(self.value)
        self.assertEqual(payload['text']['format']['name'], 'newsroom_final_text_check')
        data = json.loads(payload['input'])
        self.assertTrue(data['post_text'].endswith(data['draft_contract']['source_footer']))
        self.assertNotIn('draft', data)
        self.assertNotIn('summary_ru', data['decision'])
        self.assertNotIn('what_is_new', data['decision'])
        return response({'issues': [], 'editorial_check': self.audit,
                         'covered_claims': [{'fact_id': f['fact_id'], 'post_quote': f['statement']}
                                            for f in data['draft_contract']['material_facts']]})

    def process(self, item=None, existing_item_id=None):
        return process_item(self.db, self.source, dict(item or self.item), .35, 3500, 24,
                            ai_settings=self.config['ai'], existing_item_id=existing_item_id)

    def test_ready_post_uses_one_combined_call_and_one_independent_check(self):
        self.prepare()
        with patch.object(ai, 'get_api_key', return_value='test'), \
             patch('newsroom.core.get_api_key', return_value='test'), \
             patch('newsroom.core.fetch_publisher_article', return_value=self.article), \
             patch.object(ai, 'draft_post') as writer, \
             patch.object(ai, 'validate_draft', wraps=real_validate_draft), \
             patch.object(ai, 'request_response', side_effect=self.provider) as request:
            self.assertEqual(self.process(), 'NEW_STORY')
        self.assertEqual(request.call_count, 2)
        writer.assert_not_called()
        post = self.db.execute('SELECT * FROM posts').fetchone()
        facts = json.loads(post['fact_check_result'])
        checked_text = json.loads(request.call_args.args[0]['input'])['post_text']
        self.assertEqual(post['text'], checked_text)
        self.assertEqual(facts['final_text_check']['text_sha256'], digest(checked_text))
        self.assertEqual(facts['final_text_check']['assembled_sha256'], digest(checked_text))
        self.assertTrue(facts['intake_filter']['passed'])
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM post_facts').fetchone()[0], 1)
        saved = json.loads(self.db.execute('SELECT result_json FROM item_analysis').fetchone()[0])
        self.assertTrue(saved['_combined_editor'])
        self.assertTrue(saved['original_reporting_check']['attribution_preserved'])

    def test_source_footer_and_whitespace_are_normalized_before_final_check(self):
        self.prepare()
        self.value['summary_ru'] = (self.evidence.replace(' ', '  ')
                                   + '\n\nИсточник: [Другой источник](https://wrong.example)')
        with patch.object(ai, 'get_api_key', return_value='test'), \
             patch('newsroom.core.get_api_key', return_value='test'), \
             patch('newsroom.core.fetch_publisher_article', return_value=self.article), \
             patch.object(ai, 'validate_draft', wraps=real_validate_draft), \
             patch.object(ai, 'request_response', side_effect=self.provider) as request:
            self.assertEqual(self.process(), 'NEW_STORY')
        text = json.loads(request.call_args.args[0]['input'])['post_text']
        self.assertEqual(text, self.db.execute('SELECT text FROM posts').fetchone()[0])
        self.assertIn(self.evidence, text)
        self.assertEqual(text.count('Источник:'), 1)
        self.assertTrue(text.endswith('Источник: [Банк России](https://www.cbr.ru/crypto)'))
        self.assertNotIn('wrong.example', text)

    def test_broken_evidence_never_reaches_writing_or_final_check(self):
        self.prepare()
        self.value['memory']['claims'][0]['source_quote'] = 'Выдуманная цитата о другом выпуске.'
        with patch.object(ai, 'get_api_key', return_value='test'), \
             patch('newsroom.core.get_api_key', return_value='test'), \
             patch('newsroom.core.fetch_publisher_article', return_value=self.article), \
             patch.object(ai, 'draft_post') as writer, \
             patch.object(ai, 'validate_draft') as check, \
             patch.object(ai, 'request_response', side_effect=self.provider) as request:
            self.assertEqual(self.process(), 'WAITING_CONFIRMATION')
        self.assertEqual(request.call_count, 1)
        check.assert_not_called()
        writer.assert_not_called()
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM posts').fetchone()[0], 0)

    def test_text_repair_resumes_saved_facts_without_combined_rerun(self):
        self.prepare()
        checks = [dict(issues=['Исправить формулировку'], editorial_check=self.audit, covered_claims=[]),
                  dict(issues=[], editorial_check=self.audit,
                       covered_claims=[dict(fact_id=1, post_quote=self.evidence)])]
        draft = dict(headline_ru=self.value['headline_ru'], summary_ru=self.evidence,
                     what_is_new=self.evidence, editorial_check=self.audit)
        with patch.object(ai, 'get_api_key', return_value='test'), \
             patch('newsroom.core.get_api_key', return_value='test'), \
             patch('newsroom.core.fetch_publisher_article', return_value=self.article) as reader, \
             patch.object(ai, 'draft_post', return_value=draft) as writer, \
             patch.object(ai, 'validate_draft', side_effect=checks) as check, \
             patch.object(ai, 'request_response', side_effect=self.provider) as request:
            self.assertEqual(self.process(), 'WAITING_CONFIRMATION')
            row = self.db.execute('SELECT * FROM items').fetchone()
            resumed = _saved_material(row, self.source)
            self.assertEqual(self.process(resumed, row['item_id']), 'NEW_STORY')
        self.assertEqual(request.call_count, 1)
        self.assertEqual(reader.call_count, 1)
        self.assertEqual(writer.call_count, 1)
        self.assertEqual(check.call_count, 2)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM story_facts').fetchone()[0], 1)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM posts').fetchone()[0], 1)

    def test_final_checker_failure_cannot_create_post(self):
        self.prepare()
        with patch.object(ai, 'get_api_key', return_value='test'), \
             patch('newsroom.core.get_api_key', return_value='test'), \
             patch('newsroom.core.fetch_publisher_article', return_value=self.article), \
             patch.object(ai, 'request_response', side_effect=self.provider), \
             patch.object(ai, 'validate_draft', side_effect=ai.AIResponseError('INCOMPLETE_TEXT_CHECK')):
            self.assertEqual(self.process(), 'AI_RETRY')
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM posts').fetchone()[0], 0)

    def test_published_repeat_does_not_get_a_second_post_or_final_check(self):
        self.prepare()
        with patch.object(ai, 'get_api_key', return_value='test'), \
             patch('newsroom.core.get_api_key', return_value='test'), \
             patch('newsroom.core.fetch_publisher_article', return_value=self.article), \
             patch.object(ai, 'request_response', side_effect=self.provider), \
             patch.object(ai, 'validate_draft', wraps=real_validate_draft):
            self.assertEqual(self.process(), 'NEW_STORY')
        # A saved publication is enough to test coverage; no Telegram side effect.
        self.db.execute("UPDATE posts SET status='PUBLISHED',published_at=?", (self.now,))
        self.db.commit()
        self.value['memory']['claims'][0].update(previous_fact_id='1', relation='REPEAT')
        item = dict(self.item, url='https://example.org/another-source', content='Другой пересказ цифровых активов')
        article = dict(self.article, url=item['url'], content=item['content'])
        with patch.object(ai, 'get_api_key', return_value='test'), \
             patch('newsroom.core.get_api_key', return_value='test'), \
             patch('newsroom.core.fetch_publisher_article', return_value=article), \
             patch.object(ai, 'request_response', side_effect=self.provider), \
             patch.object(ai, 'validate_draft') as check:
            self.assertEqual(self.process(item), 'DUPLICATE')
        check.assert_not_called()
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM posts').fetchone()[0], 1)

    def test_malformed_response_and_unverified_flags_are_not_accepted(self):
        self.prepare()
        value = copy.deepcopy(self.value)
        value['editorial_check'] = self.audit
        with patch.object(ai, 'request_response', return_value=response(value)):
            with self.assertRaisesRegex(ai.AIResponseError, 'INVALID_COMBINED_EDITOR_FIELDS'):
                compact_editor.analyze(self.item, self.source, [], self.config['ai'])
        with patch.object(ai, 'request_response', return_value={'status': 'incomplete',
                    'incomplete_details': {'reason': 'max_output_tokens'}}):
            with self.assertRaisesRegex(ai.AIResponseError, 'OUTPUT_TOKEN_LIMIT'):
                compact_editor.analyze(self.item, self.source, [], self.config['ai'])

    def test_context_keeps_exact_prior_fields_with_bounded_related_stories(self):
        self.prepare()
        story = dict(story_id='1', facts=[dict(fact_id=i, value='точное значение',
                     valid_from='2026-01-01', published=i % 2 == 0) for i in range(24)],
                     events=[dict(event_id=i, identity_json='{}') for i in range(10)],
                     historical_publication_coverage=[dict(value='старое значение') for _ in range(40)],
                     published_posts=[dict(text='Пост') for _ in range(3)])
        with patch('newsroom.knowledge.context', return_value=[copy.deepcopy(story) for _ in range(4)]) as full:
            result = compact_editor.context(self.db, list(range(12)))
        self.assertEqual(full.call_args.args[1], [0, 1, 2, 3])
        self.assertEqual(len(result[0]['facts']), 8)
        self.assertEqual(result[0]['facts'][0]['value'], 'точное значение')
        self.assertEqual(result[0]['facts'][0]['valid_from'], '2026-01-01')
        self.assertEqual(len(result[0]['events']), 3)
        self.assertEqual(len(result[0]['historical_publication_coverage']), 8)

    def test_main_text_is_passed_once_and_no_thematic_schema_is_requested(self):
        self.prepare()
        item = dict(self.item, primary_source=dict(content=self.evidence, url=self.item['url']),
                    publisher_report=dict(content=self.evidence, url=self.item['url']))
        with patch.object(ai, 'request_response', return_value=response(self.value)) as request:
            result = compact_editor.analyze(item, self.source, [], self.config['ai'])
        data = json.loads(request.call_args.args[0]['input'])
        self.assertEqual(json.dumps(data, ensure_ascii=False).count(self.evidence), 1)
        self.assertNotIn('content', data['material'])
        self.assertNotIn('is_relevant', compact_editor.schema()['properties'])
        self.assertNotIn('topic_match', compact_editor.schema()['properties'])
        self.assertNotIn('editorial_check', compact_editor.schema()['properties'])
        self.assertFalse(result['original_reporting_check']['attribution_preserved'])
        self.assertTrue(result['_validation_pending'])
        self.assertFalse(result['_needs_post_draft'])
