import copy
import csv
import io
import json
import unittest
from unittest.mock import patch

from newsroom import topic_registry as topics
from newsroom.keyword_filter import evaluate


def fixture():
    def word(text, role, refinement='', enabled=True, topic='Криптовалюты'):
        return dict(title=topic, description=text, role=role, refinement=refinement, enabled=enabled)
    rules = [
        dict(name='Бренд', required=['Бренд','Профильный'], optional=['Контекстный'], enabled=True),
        dict(name='Профильная', required=['Профильный'], optional=['Бренд'], enabled=True),
        dict(name='Уточнение', required=['Бренд','Требует уточнения','Уточнение'], optional=[], enabled=True),
        dict(name='Лицо', required=['Лицо в заголовке'], optional=[], enabled=True),
    ]
    snapshot = {'version':'rules-v1', 'sections':{
        'Темы':[dict(title='Криптовалюты', description='Криптотема', enabled=True)],
        'Ключевые слова':[word('ЦФА','Профильный'), word('криптовалюта','Профильный'),
            word('crypto','Профильный'), word('цифровой актив','Профильный'),
            word('форум','Контекстный'), word('лицензия','Контекстный'),
            word('токен','Требует уточнения','распределённый реестр | блокчейн'),
            word('выключенный ключ','Профильный',enabled=False)],
        'Исключения':[], 'География':[], topics.FILTER_RULES:rules}}
    entities = [dict(name=n, section='Лица' if n == 'Иван Петров' else 'Бренды', enabled=True)
                for n in ['Ростех','Сбер','ВТБ','Банк России','Crypto.com','Иван Петров']]
    config = {'ai':{}}
    thematic = topics.apply_snapshot(config, snapshot, entities)
    return snapshot, entities, config['ai'], thematic


class IntakeRulesTests(unittest.TestCase):
    def test_entity_context_and_profile_combinations(self):
        _, _, settings, _ = fixture()
        spec = settings['_keyword_prefilter']
        for title in ['Ростех представил медицинскую продукцию на форуме', 'ВТБ получил лицензию',
                      'Банк России провёл форум', 'Crypto.com объявил о спортивном партнёрстве']:
            with self.subTest(title=title):
                self.assertFalse(evaluate(title, spec, title)['passed'])
        for title in ['Сбер выпустил ЦФА', 'ВТБ получил лицензию на операции с криптовалютами',
                      'Новая компания выпустила цифровые активы']:
            with self.subTest(title=title):
                result = evaluate(title, spec, title)
                self.assertTrue(result['passed'])
                self.assertTrue(result['matched_topic_keyword'])
        result = evaluate('ВТБ получил лицензию на операции с криптовалютами', spec)
        self.assertIn('ВТБ + криптовалюта', result['reason'])
        self.assertEqual(result['matched_context_keyword'], 'лицензия')

    def test_person_exception_uses_title_only_and_respects_registry_switch(self):
        snapshot, entities, settings, _ = fixture()
        title = 'Иван Петров пробежал марафон'
        self.assertTrue(evaluate(title, settings['_keyword_prefilter'], title)['passed'])
        self.assertFalse(evaluate('Ростех выпустил медицинский прибор. Иван Петров дал комментарий.',
                                 settings['_keyword_prefilter'], 'Ростех выпустил медицинский прибор')['passed'])
        entities[-1]['enabled'] = False
        config = {'ai':{}}
        topics.apply_snapshot(config, snapshot, entities)
        self.assertFalse(evaluate(title, config['ai']['_keyword_prefilter'], title)['passed'])

    def test_ambiguous_keyword_requires_its_own_clarifier_and_brand(self):
        spec = fixture()[2]['_keyword_prefilter']
        self.assertFalse(evaluate('Сбер выдал токены для входа в кабинет', spec)['passed'])
        self.assertFalse(evaluate('Токены в распределённом реестре', spec)['passed'])
        result = evaluate('Сбер выпустил токены в распределённом реестре', spec)
        self.assertTrue(result['passed'])
        self.assertEqual(result['rule'], 'Уточнение')
        self.assertEqual(result['matched_refinement'], 'распределённый реестр')

    def test_disabled_topic_keyword_and_rule_do_not_admit_material(self):
        snapshot, entities, _, _ = fixture()
        snapshot['sections']['Темы'][0]['enabled'] = False
        config = {'ai':{}}
        topics.apply_snapshot(config, snapshot, entities)
        self.assertTrue(evaluate('Сбер выпустил ЦФА', config['ai']['_keyword_prefilter'])['configuration_error'])
        spec = fixture()[2]['_keyword_prefilter']
        self.assertFalse(evaluate('Сбер: выключенный ключ', spec)['passed'])
        for rule in spec['rules']:
            rule['enabled'] = False
        self.assertFalse(evaluate('Сбер выпустил ЦФА', spec)['passed'])

    def test_sheet_parsers_validate_roles_and_rule_expressions(self):
        body = 'Тема,Слово или фраза,Мониторинг,Роль,Уточнение\nКриптовалюты,ЦФА,TRUE,Профильный,\nКриптовалюты,форум,TRUE,,\n'
        parsed = topics.parse_tab(body.encode(), 'Ключевые слова')
        self.assertEqual([r['role'] for r in parsed], ['Профильный','Контекстный'])
        with self.assertRaises(ValueError):
            topics.parse_tab(body.replace('Профильный','Неизвестная роль').encode(), 'Ключевые слова')
        out = io.StringIO()
        writer = csv.writer(out)
        writer.writerow(topics.FILTER_HEADERS)
        writer.writerow(['Бренд','Бренд + Профильный','Контекстный','TRUE','Допуск'])
        self.assertEqual(topics.parse_filter_rules(out.getvalue().encode())[0]['required'], ['Бренд','Профильный'])
        with self.assertRaises(ValueError):
            topics.parse_filter_rules(out.getvalue().replace('Бренд + Профильный','Контекстный').encode())

    def test_editor_is_not_asked_to_reclassify_topic(self):
        from newsroom.ai import analyze
        settings = fixture()[2]
        item = {'title':'Сбер выпустил ЦФА', '_intake_filter':{'passed':True,'rule':'Бренд'}}
        response = {'output':[{'content':[{'type':'output_text','text':json.dumps({'action':'NEW_STORY'})}]}]}
        with patch('newsroom.ai.get_api_key', return_value='test'), patch('newsroom.ai.request_response', return_value=response) as request:
            result = analyze(item, {'name':'Сбер','reputation':'unknown','priority':1}, [], settings)
        payload = request.call_args.args[0]
        schema = payload['text']['format']['schema']
        self.assertNotIn('is_relevant', schema['properties'])
        self.assertNotIn('topic_match', schema['properties'])
        self.assertNotIn('NOISE', schema['properties']['action']['enum'])
        context = json.loads(payload['input'][0]['content'])
        self.assertIsNone(context['thematic_policy'])
        self.assertTrue(context['intake_selection']['passed'])
        self.assertTrue(result['is_relevant'])

    def test_full_pipeline_uses_saved_intake_instead_of_ai_topic_decision(self):
        from test_workflow import WorkflowTests
        from newsroom.core import process_item
        from newsroom.cli import is_eligible_for_auto_publish
        runner = WorkflowTests(methodName='test_post_writing_runs_only_after_analysis_gates')
        runner.setUp()
        self.addCleanup(runner.doCleanups)
        _, _, settings, thematic = fixture()
        runner.config['ai'].update(settings)
        item = runner.item()
        decision = runner.publish_result(item)
        decision['is_relevant'] = False  # A stale model opinion has no topical veto.
        with patch('newsroom.core.get_api_key', return_value='test'), \
             patch('newsroom.core.analyze_with_ai', return_value=decision), \
             patch('newsroom.topic_registry.grounded_match') as second_filter:
            outcome = process_item(runner.db, runner.source, item, .35, 3500, 24, ai_settings=runner.config['ai'])
        self.assertEqual(outcome, 'NEW_STORY')
        second_filter.assert_not_called()
        post = dict(runner.db.execute('SELECT * FROM posts').fetchone())
        facts = json.loads(post['fact_check_result'])
        self.assertTrue(facts['intake_filter']['passed'])
        self.assertIsNone(facts['topic_registry'])
        self.assertTrue(is_eligible_for_auto_publish(post, runner.now, thematic))
        facts['intake_filter']['passed'] = False
        post['fact_check_result'] = json.dumps(facts)
        self.assertFalse(is_eligible_for_auto_publish(post, runner.now, thematic))
        facts['intake_filter']['passed'] = True
        facts['intake_filter']['version'] = 'old'
        post['fact_check_result'] = json.dumps(facts)
        self.assertFalse(is_eligible_for_auto_publish(post, runner.now, thematic))

    def test_saved_intake_is_reused_on_retry_without_rereading_new_body(self):
        from test_workflow import WorkflowTests
        from newsroom.core import _save_item
        from newsroom.keyword_filter import screen
        runner = WorkflowTests(methodName='test_post_writing_runs_only_after_analysis_gates')
        runner.setUp()
        self.addCleanup(runner.doCleanups)
        settings = fixture()[2]
        item = runner.item()
        item_id = _save_item(runner.db, runner.source, item)
        self.assertIsNone(screen(runner.db, item_id, item, settings))
        proof = copy.deepcopy(item['_intake_filter'])
        item.update(title='Заголовок изменился при чтении', content='', description='')
        with patch('newsroom.keyword_filter.evaluate', side_effect=AssertionError('Repeated selection')):
            self.assertIsNone(screen(runner.db, item_id, item, settings))
        self.assertEqual(item['_intake_filter'], proof)
        settings['_keyword_prefilter']['version'] = 'rules-v2'
        self.assertEqual(screen(runner.db, item_id, item, settings), 'NOISE')
