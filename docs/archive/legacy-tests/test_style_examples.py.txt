import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from newsroom import ai, compact_editor, editorial_registry, policy, style_examples
from newsroom.db import connect
from newsroom.material_flow import load_draft_context, save_draft_context
from newsroom.quality import editorial_issues, paragraph_issues
from compact_fixtures import compact_response, response
import test_compact_editor as pipeline_fixture


def settings():
    return {'_policy_baseline': [], '_editorial_registry': {'rules': [], 'examples': [
        {'previous_text': 'Слишком подробно.', 'post_text': 'Банк запустил сервис.\n\nУсловия доступа.',
         'reason': '[format:short] Короткая новость'},
        {'previous_text': 'Перечень в одном абзаце.', 'post_text': 'Банк объявил условия.\n\n➠ Первый пункт.\n\n➠ Второй пункт.',
         'reason': '[format:structured] Условия'},
        {'previous_text': 'Сплошной текст.', 'post_text': 'Итоги форума\n\n➠ Банк запустил.\n\n➠ Регулятор предложил.',
         'reason': '[format:roundup] Обзор'},
    ]}}


class StyleExamplesTests(unittest.TestCase):
    def test_selection_matches_form_and_never_adds_unrelated_examples(self):
        config = settings()
        for title, text, facts, expected in (
                ('Банк запустил сервис', '', None, 'short'),
                ('Банк объявил условия', '', [{}, {}, {}], 'structured'),
                ('Итоги форума', '', None, 'roundup'),
                ('Банк запустил сервис на форуме', '', None, 'short'),
                ('Банк открыл доступ', '➠ Условия.\n\n➠ Сроки.', None, 'structured')):
            with self.subTest(title=title):
                result = style_examples.select(config, title=title, text=text, facts=facts)
                self.assertEqual([row['format'] for row in result], [expected])

    def test_disabled_table_rows_never_reach_the_model(self):
        snapshot = {'sections': {'Примеры редактуры': [
            {'enabled': True, 'values': ['До', 'После', 'Кратко', 'TRUE']},
            {'enabled': False, 'values': ['До', 'Отключённый образец', '', 'FALSE']}]}}
        config = {'_editorial_registry': editorial_registry.policy(snapshot)}
        self.assertEqual(len(style_examples.bank(config)), 1)
        self.assertNotIn('Отключённый', json.dumps(style_examples.select(config), ensure_ascii=False))

    def test_prompt_budget_preserves_whole_examples_and_caps_count(self):
        config = settings()
        config['_editorial_registry']['examples'] *= 20
        config['_editorial_registry']['examples'].append({
            'post_text': 'а' * 9000, 'previous_text': '', 'reason': '[format:short]'})
        result = style_examples.select(config, title='Новость')
        self.assertLessEqual(len(result), 2)
        self.assertLessEqual(len(json.dumps(result, ensure_ascii=False)), 8000)
        self.assertTrue(all(row['after'].endswith('Условия доступа.') for row in result))

    def test_combined_editor_and_checker_receive_examples_as_separate_data(self):
        config = settings()
        item = {'title': 'Банк запустил сервис', 'primary_source': {'content': 'Только факты источника.'}}
        with patch.object(ai, 'request_response', return_value=response(compact_response())) as request:
            compact_editor.analyze(item, {}, [], config)
        draft_payload = request.call_args.args[0]
        data = json.loads(draft_payload['input'])
        self.assertEqual(data['read_source']['content'], 'Только факты источника.')
        self.assertEqual(data['style_examples'][0]['format'], 'short')
        self.assertIn('не источники фактов', draft_payload['instructions'])
        self.assertIn('При расхождении образца', draft_payload['instructions'])
        checked = {'issues': [], 'editorial_check': {
            key: '' if key == 'history_note' else False
            for key in ai.SCHEMA['properties']['editorial_check']['required']}, 'covered_claims': []}
        with patch.object(ai, 'request_response', return_value=response(checked)) as request:
            ai.validate_draft({}, item['primary_source'], {
                'headline_ru': 'Банк запустил', 'post_text': 'Банк запустил\n\nНовый факт.'}, config)
        self.assertEqual(json.loads(request.call_args.args[0]['input'])['style_examples'][0]['format'], 'short')

    def test_separate_writer_and_correction_receive_matching_examples(self):
        config = settings()
        with patch.object(ai, 'request_response', return_value=response({})) as request:
            with self.assertRaises(ai.AIResponseError):
                ai.draft_post({'facts': [{}, {}, {}]}, {'title': 'Банк объявил условия'}, config)
        self.assertEqual(json.loads(request.call_args.args[0]['input'])['style_examples'][0]['format'], 'structured')
        with patch.object(ai, 'request_response', return_value=response({})) as request:
            ai.correct_published_post('Банк открыл доступ\n\nКороткий факт.', 'Сократить', {}, {}, config)
        self.assertEqual(json.loads(request.call_args.args[0]['input'][0]['content'][0]['text'])['style_examples'][0]['format'], 'short')

    def test_example_change_rewrites_saved_draft_without_repeating_evidence_analysis(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = connect(str(Path(tmp) / 'state.db'))
            self.addCleanup(db.close)
            db.execute("INSERT INTO sources(source_id,name,type,url) VALUES(1,'Банк','rss','https://example.org/feed')")
            db.execute("INSERT INTO items(item_id,source_id,url,canonical_url,title,discovered_at,content_hash,title_hash,ingest_revision) "
                       "VALUES(1,1,'https://example.org/1','https://example.org/1','Банк','2026-10-08','hash','title','v1')")
            db.commit()
            config = settings()
            item = {'title': 'Банк', 'url': 'https://example.org/1'}
            context = {'ai_result': {'_needs_post_draft': False, 'what_is_new': 'Сохранённый факт',
                                      'final_text_check': {'text_sha256': 'old-proof'}}}
            save_draft_context(db, 1, item, config, context)
            changed = copy.deepcopy(config)
            changed['_editorial_registry']['examples'][0]['post_text'] += '\n\nНовая форма.'
            self.assertEqual(policy.stage_signature('analysis', config), policy.stage_signature('analysis', changed))
            self.assertNotEqual(policy.snapshot(config)['sha256'], policy.snapshot(changed)['sha256'])
            resumed = load_draft_context(db, 1, item, changed)
            self.assertEqual(resumed['ai_result']['what_is_new'], 'Сохранённый факт')
            self.assertTrue(resumed['ai_result']['_needs_post_draft'])
            self.assertNotIn('final_text_check', resumed['ai_result'])


class ParagraphLimitTests(unittest.TestCase):
    def test_boundary_counts_visible_characters_and_spaces(self):
        self.assertEqual(paragraph_issues('я' * 210), [])
        self.assertEqual(paragraph_issues('я' * 211), ['PARAGRAPH_TOO_LONG:1:211>210'])
        self.assertEqual(paragraph_issues('а' * 105 + ' ' + 'б' * 105), ['PARAGRAPH_TOO_LONG:1:211>210'])
        self.assertEqual(paragraph_issues('**' + 'я' * 210 + '**'), [])
        self.assertEqual(paragraph_issues('[Короткая ссылка](https://example.org/' + 'a' * 300 + ')'), [])

    def test_single_newlines_do_not_bypass_the_paragraph_limit(self):
        self.assertTrue(paragraph_issues('я' * 110 + '\n' + 'я' * 110))
        self.assertEqual(paragraph_issues('я' * 110 + '\n\n' + 'я' * 110), [])

    def test_footer_does_not_consume_paragraph_budget(self):
        footer = 'Источник: [Издание](https://example.org/' + 'a' * 300 + ')'
        self.assertEqual(paragraph_issues('я' * 210 + '\n\n' + footer), [])

    def test_esma_regression_cannot_pass_an_optimistic_ai_check_or_send_gate(self):
        text = ('По данным Bits Media, Европейское управление по ценным бумагам и рынкам (ESMA) '
                'потребовало от зарегистрированных в ЕС криптокомпаний прекратить услуги со стейблкоинами, '
                'эмитенты которых не зарегистрированы по MiCA — регламенту о рынках криптоактивов. '
                'Среди них издание называет USDT и другие подобные токены.')
        self.assertEqual(len(text), 308)
        good_audit = {key: True for key in ('source_matches_event', 'attribution_preserved',
                                          'stage_preserved', 'headline_main_event', 'lead_event_first')}
        self.assertIn('PARAGRAPH_TOO_LONG:1:308>210', editorial_issues('ESMA потребовало', text,
                      {'editorial_check': good_audit}, final_post=True))
        with patch.object(ai, 'request_response') as request:
            checked = ai.validate_draft({}, {}, {'post_text': 'ESMA потребовало\n\n' + text}, settings())
        request.assert_not_called()
        self.assertEqual(checked['issues'], ['PARAGRAPH_TOO_LONG:1:308>210'])


class StylePipelineTests(unittest.TestCase):
    setUp = pipeline_fixture.CompactEditorTests.setUp
    prepare = pipeline_fixture.CompactEditorTests.prepare
    process = pipeline_fixture.CompactEditorTests.process

    def test_long_draft_is_repaired_with_examples_and_saved_evidence_only(self):
        self.prepare()
        self.config['ai'].update(settings())
        self.value['summary_ru'] = ('Сбер выпустил цифровые активы. ' * 12).strip()
        def provider(payload, options):
            name = payload['text']['format']['name']
            data = json.loads(payload['input'])
            self.assertEqual(data['style_examples'][0]['format'], 'short')
            if name == 'newsroom_post_draft':
                self.assertTrue(any('PARAGRAPH_TOO_LONG' in issue
                                    for issue in data['checked_decision']['editorial_issues']))
                return response(dict(headline_ru=self.value['headline_ru'], summary_ru=self.evidence,
                                     what_is_new=self.evidence, editorial_check=self.audit))
            return pipeline_fixture.CompactEditorTests.provider(self, payload, options)
        with patch.object(ai, 'get_api_key', return_value='test'), \
             patch('newsroom.core.get_api_key', return_value='test'), \
             patch('newsroom.core.fetch_publisher_article', return_value=self.article) as reader, \
             patch.object(ai, 'request_response', side_effect=provider) as request, \
             patch.object(ai, 'validate_draft', wraps=pipeline_fixture.real_validate_draft):
            self.assertEqual(self.process(), 'WAITING_CONFIRMATION')
            self.assertEqual(self.db.execute('SELECT COUNT(*) FROM posts').fetchone()[0], 0)
            row = self.db.execute('SELECT * FROM items').fetchone()
            from newsroom.core import _saved_material
            self.assertEqual(self.process(_saved_material(row, self.source), row['item_id']), 'NEW_STORY')
        self.assertEqual(reader.call_count, 1)
        self.assertEqual([call.args[0]['text']['format']['name'] for call in request.call_args_list],
                         ['newsroom_evidence_and_draft', 'newsroom_post_draft', 'newsroom_final_text_check'])
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM story_facts').fetchone()[0], 1)
        text = self.db.execute('SELECT text FROM posts').fetchone()[0]
        self.assertEqual(paragraph_issues(text.partition('\n')[2]), [])


if __name__ == '__main__':
    unittest.main()
