import csv
import io
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from newsroom import topic_registry as topics
from newsroom.source_registry import save, state, entity_sources
from newsroom.db import connect


def row(title='Тема', description='Условия', enabled=True, number=2):
    return dict(title=title, description=description, enabled=enabled, row=number)


def snapshot():
    return {'version':'v1', 'checked_at':123, 'sections':{
        'Темы':[row('Работа'),row('Выключена',enabled=False)],
        'Ключевые слова':[row('Работа','платёжный агент'),row('Работа','старый ключ',False),row('Выключена','другой ключ')],
        'Исключения':[row('Случайное совпадение','Нет связи с участником')],
        'География':[row('Россия','Связь с Россией')]}}


def response(changes):
    return {'output':[{'content':[{'type':'output_text','text':json.dumps({'changes':changes},ensure_ascii=False)}]}]}


class TopicsTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.path=str(Path(self.temp.name)/'state.db')
        self.db=connect(self.path); self.addCleanup(self.db.close)
        self.settings={'spreadsheet_id':'x'*25,'url':'https://docs.google.com/spreadsheets/d/'+'x'*25+'/edit',
                       'tabs':[{'name':n,'gid':str(i+1)} for i,n in enumerate(topics.HEADERS)]}

    def enable(self):
        save(self.db,topics.SETTINGS,self.settings); save(self.db,topics.SNAPSHOT,snapshot()); self.db.commit()

    def test_flat_keyword_only_defaults_to_enabled_profile(self):
        body = 'Ключевик,Мониторинг,Роль,Уточнение\nмайнинг\nкошелёк,,Требует уточнения,криптовалюта | биткоин\nэкономика,FALSE,Контекстный,\n'
        entries = topics.parse_tab(body.encode(), 'Ключевые слова')
        self.assertTrue(entries[0]['enabled'])
        self.assertEqual(entries[0]['role'], 'Профильный')
        self.assertFalse(entries[2]['enabled'])
        self.assertEqual(entries[1]['refinement'], 'криптовалюта | биткоин')
        value = snapshot()
        value['sections']['Темы'] = []
        value['sections'][topics.FILTER_RULES] = [{'name':'Профильный', 'required':['Профильный'], 'enabled':True}]
        value['sections']['Ключевые слова'] = entries
        config = {'ai': {}}
        topics.apply_snapshot(config, value)
        self.assertEqual(config['ai']['_keyword_prefilter']['entries'], entries[:2])
        from newsroom.keyword_filter import evaluate
        self.assertTrue(evaluate('Запущен майнинг', config['ai']['_keyword_prefilter'])['passed'])

    def test_flat_invalid_role_flag_and_missing_refinement_are_rejected(self):
        for row in ['слово,MAYBE,,', 'слово,,Неизвестная,', 'слово,,Требует уточнения,']:
            with self.subTest(row=row), self.assertRaises(ValueError):
                topics.parse_tab(('Ключевик,Мониторинг,Роль,Уточнение\n' + row).encode(), 'Ключевые слова')

    def test_flat_writer_adds_without_topic_and_retry_preserves_disabled(self):
        metadata={'sheets':[{'properties':{'title':name,'sheetId':i,'gridProperties':{'columnCount':4 if name=='Ключевые слова' else 3}}} for i,name in enumerate(topics.HEADERS)]}
        def ranges(keyword_rows):
            return {'valueRanges':[{'values':([topics.FLAT_KEYWORD_HEADERS] + keyword_rows) if name=='Ключевые слова' else [topics.HEADERS[name]]} for name in topics.HEADERS]}
        change={'section':'Ключевые слова','title':'Несуществующая тема','description':'майнинг','operation':'ADD'}
        with patch.object(topics,'api',side_effect=[metadata,ranges([]),{'replies':[{},{}]}]) as api:
            topics.write_changes(self.settings,[change])
            cells=api.call_args.args[3]['requests'][1]['updateCells']['rows'][0]['values']
            self.assertEqual([c['userEnteredValue'] for c in cells], [{'stringValue':'майнинг'},{'boolValue':True},{'stringValue':'Профильный'},{'stringValue':''}])
        with patch.object(topics,'api',side_effect=[metadata,ranges([['майнинг',False,'Контекстный','']])]) as api:
            self.assertEqual(topics.write_changes(self.settings,[change]),0)
            self.assertEqual(api.call_count,2)

    def test_flat_learning_accepts_keyword_without_topic(self):
        value=snapshot()
        value['sections']['Ключевые слова']=topics.parse_tab('Ключевик,Мониторинг,Роль,Уточнение\nмайнинг\n'.encode(),'Ключевые слова')
        change={'section':'Ключевые слова','title':'','description':'майнинг','operation':'DISABLE','evidence':'не отслеживать майнинг'}
        with patch('newsroom.ai.request_response',return_value=response([change])):
            changes=topics.plan_learning({'reason':'не отслеживать майнинг'},'editorial',value,{})
        self.assertEqual(changes[0]['expected_description'],'майнинг')

    def test_flat_writer_expands_initial_three_column_read(self):
        metadata={'sheets':[{'properties':{'title':name,'sheetId':i,'gridProperties':{'columnCount':4 if name=='Ключевые слова' else 3}}} for i,name in enumerate(topics.HEADERS)]}
        ranges={'valueRanges':[{'values':([topics.FLAT_KEYWORD_HEADERS[:3],['майнинг',True,'Требует уточнения']]) if name=='Ключевые слова' else [topics.HEADERS[name]]} for name in topics.HEADERS]}
        expanded={'valueRanges':[{'values':[topics.FLAT_KEYWORD_HEADERS,['майнинг',True,'Требует уточнения','биткоин']]}]}
        change={'section':'Ключевые слова','title':'','description':'майнинг','operation':'ADD'}
        with patch.object(topics,'api',side_effect=[metadata,ranges,expanded]) as api:
            self.assertEqual(topics.write_changes(self.settings,[change]),0)
            self.assertIn('A1%3AD',api.call_args.args[2])

    def test_flat_writer_disables_monitoring_column(self):
        metadata={'sheets':[{'properties':{'title':name,'sheetId':i,'gridProperties':{'columnCount':4 if name=='Ключевые слова' else 3}}} for i,name in enumerate(topics.HEADERS)]}
        ranges={'valueRanges':[{'values':([topics.FLAT_KEYWORD_HEADERS,['майнинг']]) if name=='Ключевые слова' else [topics.HEADERS[name]]} for name in topics.HEADERS]}
        change={'section':'Ключевые слова','title':'','description':'майнинг','operation':'DISABLE','expected_description':'майнинг'}
        with patch.object(topics,'api',side_effect=[metadata,ranges,{'replies':[{}]}]) as api:
            topics.write_changes(self.settings,[change])
            request=api.call_args.args[3]['requests'][0]['updateCells']
            self.assertEqual(request['range']['startColumnIndex'],1)
            self.assertEqual(request['rows'][0]['values'][0]['userEnteredValue'],{'boolValue':False})

    def test_morphology_and_word_boundaries(self):
        for text in ['договор с платёжного агента','услуги платежными агентами','агенты по платёжным операциям']:
            self.assertTrue(topics.lexical_match(text,['платёжный агент']))
        self.assertTrue(topics.lexical_match('она получила лицензию',['получить лицензию']))
        self.assertFalse(topics.lexical_match('безопасный контракт TONIC',['TON']))
        self.assertFalse(topics.lexical_match('рынок и сотрудник далеко от этого агентства',['рынок агент']))

    def test_parse_native_flags_and_ignored_cleared_rows(self):
        text='Тема,Что отслеживать,Мониторинг\nДа,Условия,TRUE\nНет,Условия,FALSE\n,,FALSE\n'
        self.assertEqual([r['enabled'] for r in topics.parse_tab(text.encode(),'Темы')],[True,False])
        with self.assertRaises(ValueError): topics.parse_tab(b'<html>login</html>','Темы')
        with self.assertRaises(ValueError): topics.parse_tab('Тема,Что отслеживать,Мониторинг\nДа,Условия,MAYBE'.encode(),'Темы')

    def test_pasted_header_inside_keyword_tab_does_not_break_refresh(self):
        body = 'Тема,Слово или фраза,Мониторинг\nРабота,платёжный агент,TRUE\nТема,Слово или фраза,Мониторинг\nРабота,цифровой депозитарий,TRUE\n'
        result = topics.parse_tab(body.encode(), 'Ключевые слова')
        self.assertEqual([r['description'] for r in result], ['платёжный агент', 'цифровой депозитарий'])
        self.assertTrue(all(r['enabled'] for r in result))

    def test_all_tabs_are_atomic_and_unknown_keyword_parent_is_invalid(self):
        def body(tab):
            out=io.StringIO(); writer=csv.writer(out); writer.writerow(topics.HEADERS[tab['name']]);writer.writerow(['Другой' if tab['name']=='Ключевые слова' else 'Тема','Описание','TRUE']); return out.getvalue().encode(),{},''
        with patch('newsroom.core._request_with_url',side_effect=lambda url,**kw:body(next(t for t in self.settings['tabs'] if 'gid='+t['gid'] in url))):
            with self.assertRaises(ValueError): topics.read_registry(self.settings)
        self.assertIsNone(state(self.db,topics.SNAPSHOT))

    def test_cached_authority_replaces_every_legacy_topic(self):
        self.enable()
        config={'newsroom':{'database':self.path,'relevance_terms':['скрытая тема']},'ai':{},'sources':[{'type':'web_search','query':'legacy','interest_exclusions':['legacy']} ]}
        topics.attach_cached(config)
        self.assertTrue(config['_topic_registry_authoritative'])
        self.assertFalse(config['ai']['triage_enabled'])
        self.assertEqual(config['ai']['_keyword_prefilter'], {'version': 'v1', 'keywords': ['платёжный агент', 'другой ключ']})
        self.assertEqual(config['newsroom']['relevance_terms'],[])
        self.assertNotIn('legacy',config['sources'][0]['query'])
        self.assertNotIn('interest_exclusions',config['sources'][0])
        policy=config['ai']['_topic_registry']
        self.assertEqual([r['name'] for r in policy['topics']],['Работа'])
        self.assertEqual([r['concept'] for r in policy['keywords']],['платёжный агент'])

    def test_failed_refresh_uses_last_sheet_snapshot_never_legacy(self):
        self.enable();config={'newsroom':{'relevance_terms':['old']},'ai':{}}
        with patch.object(topics,'read_registry',side_effect=TimeoutError):topics.sync(self.db,config,True)
        self.assertEqual(config['ai']['_topic_registry']['version'],'v1')
        self.assertEqual(config['newsroom']['relevance_terms'],[])
        self.assertEqual(state(self.db,topics.ERROR),'TimeoutError')

    def test_configured_missing_cache_has_no_enabled_topics(self):
        save(self.db,topics.SETTINGS,self.settings);self.db.commit()
        config={'newsroom':{'database':self.path,'relevance_terms':['old']},'ai':{}}
        topics.attach_cached(config)
        self.assertEqual(config['ai']['_topic_registry']['topics'],[])

    def test_entity_discovery_uses_no_separate_crypto_word_list(self):
        from urllib.parse import urlsplit,parse_qs
        url=entity_sources([{'name':'Компания','enabled':True}],topics.policy(snapshot()))[0]['url']
        self.assertEqual(parse_qs(urlsplit(url).query)['q'],['("Компания") when:2d'])



    def activity_policy(self):
        value = snapshot()
        value['sections']['Темы'].append(row(topics.PUBLIC_ACTIVITY))
        value['sections']['Ключевые слова'].extend([
            row(topics.PUBLIC_ACTIVITY, 'Ростех'), row(topics.PUBLIC_ACTIVITY, 'Crypto Elina'),
            row('Работа', 'цифровые активы'), row('Работа', 'ЦФА')])
        entities = [{'name': 'Ростех', 'section': 'Бренды', 'enabled': True},
                    {'name': 'Сбер', 'section': 'Бренды', 'enabled': True},
                    {'name': 'Crypto Elina', 'section': 'Лица', 'enabled': True}]
        return value, entities, topics.policy(value, entities)



    def test_brand_combination_is_applied_locally_without_ai(self):
        from newsroom.keyword_filter import evaluate
        value, entities, thematic = self.activity_policy()
        config = {'ai': {}}
        topics.apply_snapshot(config, value, entities)
        spec = config['ai']['_keyword_prefilter']
        self.assertFalse(evaluate('Ростех рассказал о медицинской продукции', spec)['passed'])
        self.assertFalse(evaluate('Ростех и Сбер представили медицинский прибор', spec)['passed'])
        result = evaluate('Ростех рассказал о выпуске цифровых активов', spec)
        self.assertTrue(result['passed'])
        self.assertEqual(result['matched_brand'], 'Ростех')
        self.assertEqual(result['matched_topic_keyword'], 'цифровые активы')
        self.assertTrue(evaluate('Crypto Elina пробежала марафон', spec)['passed'])
        self.assertTrue(evaluate('Crypto Elina выступила на мероприятии Ростеха', spec)['passed'])




    def test_missing_google_write_credentials_keeps_outbox_and_sheet_authority(self):
        self.enable()
        change={'section':'Ключевые слова','title':'Работа','description':'агентский договор','operation':'ADD','evidence':'агентские договоры'}
        save(self.db,'topic_learning:editorial:1',{'status':'READY','kind':'editorial','signal':{},'changes':[change],'attempts':0});self.db.commit()
        with patch.object(topics,'credentials_available',return_value=False),patch.object(topics,'api') as api:
            topics.learn_cycle(self.db,{'ai':{}});api.assert_not_called()
        self.assertEqual(state(self.db,'topic_learning:editorial:1')['status'],'BLOCKED')
        self.assertEqual(state(self.db,topics.SNAPSHOT),snapshot())

    def test_negative_rating_never_disables_a_topic(self):
        change={'section':'Темы','title':'Работа','description':'','operation':'DISABLE','evidence':'Неинтересная тема'}
        with patch('newsroom.ai.request_response',return_value=response([change])):
            with self.assertRaises(ValueError):topics.plan_learning({'note':'Неинтересная тема','is_interesting':0},'rating',snapshot(),{})

    def test_style_only_lesson_has_no_thematic_change(self):
        with patch('newsroom.ai.request_response',return_value=response([])):
            self.assertEqual(topics.plan_learning({'reason':'Сократи заголовок'},'editorial',snapshot(),{}),[])

    def test_sheet_write_retry_deduplicates_and_preserves_manual_disabled_row(self):
        ranges=[]
        for name in topics.HEADERS:
            ranges.append({'values':[topics.HEADERS[name],['Работа','агентский договор',False]]})
        metadata={'sheets':[{'properties':{'title':name,'sheetId':i}} for i,name in enumerate(topics.HEADERS)]}
        change={'section':'Ключевые слова','title':'Работа','description':'агентский договор','operation':'ADD'}
        with patch.object(topics,'api',side_effect=[metadata,{'valueRanges':ranges}]) as api:
            self.assertEqual(topics.write_changes(self.settings,[change]),0)
            self.assertEqual(api.call_count,2)

    def test_new_keyword_inserts_a_row_instead_of_overwriting_staff_cells(self):
        ranges=[{'values':[topics.HEADERS[name],['Работа','Условия',True]]} for name in topics.HEADERS]
        metadata={'sheets':[{'properties':{'title':name,'sheetId':i}} for i,name in enumerate(topics.HEADERS)]}
        change={'section':'Ключевые слова','title':'Работа','description':'агентский договор','operation':'ADD'}
        with patch.object(topics,'api',side_effect=[metadata,{'valueRanges':ranges},{'replies':[{},{}]}]) as api:
            topics.write_changes(self.settings,[change])
            requests=api.call_args.args[3]['requests']
        self.assertIn('insertDimension',requests[0]);self.assertEqual(requests[0]['insertDimension']['range']['startIndex'],2)
        self.assertEqual(requests[1]['updateCells']['fields'],'userEnteredValue')

    def test_replacement_is_conditional_on_the_live_text(self):
        ranges=[{'values':[topics.HEADERS[name],['Работа','Условия',True]]} for name in topics.HEADERS]
        metadata={'sheets':[{'properties':{'title':name,'sheetId':i}} for i,name in enumerate(topics.HEADERS)]}
        change={'section':'Темы','title':'Работа','description':'Новые условия','operation':'REPLACE'}
        with patch.object(topics,'api',side_effect=[metadata,{'valueRanges':ranges},{'replies':[{'findReplace':{'occurrencesChanged':0}}]}]) as api:
            with self.assertRaises(ValueError):topics.write_changes(self.settings,[change])
            request=api.call_args.args[3]['requests'][0]['findReplace']
            self.assertEqual(request['find'],'Условия');self.assertTrue(request['matchEntireCell'])
