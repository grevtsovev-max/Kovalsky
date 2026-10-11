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
