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

    def test_publication_needs_read_quote_and_enabled_topic(self):
        quote='Компания наняла сотрудников для работы с платежными агентами.'
        result={'topic_match':{'name':'Работа','evidence':quote}}
        self.assertTrue(topics.grounded_match(result,topics.policy(snapshot()),{'content':quote}))
        self.assertFalse(topics.grounded_match(result,topics.policy(snapshot()),{'content':'Другой текст'}))
        result['topic_match']['name']='Выключена'
        self.assertFalse(topics.grounded_match(result,topics.policy(snapshot()),{'content':quote}))

    def test_topic_quote_saves_source_span_without_extra_terminal_punctuation(self):
        source = 'Компания наняла сотрудников для работы с платежными агентами, расширяя отдел.'
        result = {'topic_match': {'name': 'Работа', 'evidence': 'Компания наняла сотрудников для работы с платежными агентами.'}}
        self.assertTrue(topics.grounded_match(result, topics.policy(snapshot()), {'content': source}))
        self.assertEqual(result['topic_match']['evidence'], source.split(',')[0])
        result['topic_match']['evidence'] = 'Компания уволила сотрудников для работы с платежными агентами.'
        self.assertFalse(topics.grounded_match(result, topics.policy(snapshot()), {'content': source}))

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

    def test_person_exception_cannot_be_used_for_brand_or_untracked_spokesperson(self):
        _, _, thematic = self.activity_policy()
        quote = 'Представитель Ростеха рассказал о развитии медицинской продукции.'
        match = {'name': topics.PUBLIC_ACTIVITY, 'evidence': quote,
                 'subject_type': 'BRAND', 'subject_name': 'Ростех', 'crypto_evidence': ''}
        self.assertFalse(topics.grounded_match({'topic_match': match}, thematic, {'content': quote}))
        match.update(subject_type='PERSON', subject_name='Ростех')
        self.assertFalse(topics.grounded_match({'topic_match': match}, thematic, {'content': quote}))
        match['subject_name'] = 'Представитель Ростеха'
        self.assertFalse(topics.grounded_match({'topic_match': match}, thematic, {'content': quote}))
        quote = 'Crypto Elina рассказала о своём участии в спортивном марафоне.'
        match.update(evidence=quote, subject_name='Crypto Elina')
        self.assertTrue(topics.grounded_match({'topic_match': match}, thematic, {'content': quote}))

    def test_brand_needs_one_extra_thematic_keyword_and_grounded_crypto_connection(self):
        _, _, thematic = self.activity_policy()
        quote = 'Ростех сообщил о пилотном выпуске цифровых активов для расчётов.'
        match = {'name': topics.PUBLIC_ACTIVITY, 'evidence': quote, 'subject_type': 'BRAND',
                 'subject_name': 'Ростех', 'crypto_related': True, 'crypto_evidence': quote}
        self.assertTrue(topics.grounded_match({'topic_match': match}, thematic, {'content': quote}))
        match['crypto_evidence'] = 'Ростех сообщил о сотрудничестве со Сбером в развитии медицины.'
        self.assertFalse(topics.grounded_match({'topic_match': match}, thematic, {'content': quote}))
        match['crypto_evidence'] = 'Ростех сообщил о другом выпуске цифровых активов в другой стране.'
        self.assertFalse(topics.grounded_match({'topic_match': match}, thematic, {'content': quote}))
        match.update(crypto_evidence=quote, crypto_related=False)
        self.assertFalse(topics.grounded_match({'topic_match': match}, thematic, {'content': quote}))
        match.pop('subject_type')
        self.assertFalse(topics.grounded_match({'topic_match': match}, thematic, {'content': quote}))

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

    def test_delivery_boundary_rejects_old_public_activity_permission(self):
        from newsroom.cli import is_eligible_for_auto_publish
        from newsroom.ai import FILTER_VERSION
        _, _, thematic = self.activity_policy()
        quote = 'Ростех рассказал о развитии медицинской продукции на форуме.'
        match = {'name': topics.PUBLIC_ACTIVITY, 'evidence': quote}
        facts = {'mode': 'AI', '_filter_version': FILTER_VERSION,
                 'topic_registry': {'version': 'v1', 'checked': True, 'match': match}}
        post = {'created_at': '2026-10-07T12:00:00+00:00', 'fact_check_result': json.dumps(facts)}
        self.assertFalse(is_eligible_for_auto_publish(post, '2026-10-07T00:00:00+00:00', thematic))
        match.update(subject_type='PERSON', subject_name='Crypto Elina', crypto_evidence='')
        post['fact_check_result'] = json.dumps(facts)
        self.assertTrue(is_eligible_for_auto_publish(post, '2026-10-07T00:00:00+00:00', thematic))

    def test_old_learning_database_is_not_a_second_thematic_authority(self):
        from newsroom.interests import learning_context, expand_search_queries
        self.db.execute("INSERT INTO monitoring_topics(topic,search_terms,examples,updated_at) VALUES('Hidden','[]','[]','2026')")
        self.enable()
        self.assertEqual([t['topic'] for t in learning_context(self.db)['topics']],['Работа'])
        config={'_topic_registry_authoritative':True,'sources':[{'type':'web_search','query':'sheet'}]}
        expand_search_queries(config);self.assertEqual(config['sources'][0]['query'],'sheet')

    def test_feedback_capture_is_idempotent_and_includes_general_comments(self):
        self.db.execute("INSERT INTO editorial_feedback(created_at,feedback_type,reason,item_title,post_text) VALUES('2026','OTHER','Отслеживай агентские договоры','','')")
        topics.collect_feedback(self.db);topics.collect_feedback(self.db)
        keys=self.db.execute("SELECT key FROM app_state WHERE key LIKE 'topic_learning:%'").fetchall()
        self.assertEqual(len(keys),1)
        self.assertIn('агентские договоры',state(self.db,keys[0][0])['signal']['reason'])

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

    def test_send_boundary_checks_sheet_version_and_enabled_topic(self):
        from newsroom.cli import is_eligible_for_auto_publish
        from newsroom.ai import FILTER_VERSION
        facts={'mode':'AI','_filter_version':FILTER_VERSION,'geographic_scope':'GLOBAL','russia_cis_impact':'NONE',
               'topic_registry':{'version':'v1','checked':True,'match':{'name':'Работа','evidence':'Компания объявила о новой публичной активности.'}}}
        post={'created_at':'2026-10-06T12:00:00+00:00','fact_check_result':json.dumps(facts)}
        cutoff='2026-10-06T00:00:00+00:00'
        self.assertTrue(is_eligible_for_auto_publish(post,cutoff,topics.policy(snapshot())))
        thematic=topics.policy(snapshot());thematic['version']='v2'
        self.assertFalse(is_eligible_for_auto_publish(post,cutoff,thematic))
        thematic['version']='v1';thematic['topics']=[]
        self.assertFalse(is_eligible_for_auto_publish(post,cutoff,thematic))

    def test_editor_prompt_and_schema_take_scope_only_from_sheet(self):
        from newsroom.ai import analyze
        settings={'_topic_registry':topics.policy(snapshot()),'memory_mode':'shadow'}
        with patch('newsroom.ai.get_api_key',return_value='test'),patch('newsroom.ai.request_response',return_value={'output':[{'content':[{'type':'output_text','text':'{}'}]}]}) as request:
            analyze({'title':'Материал'}, {'name':'Издание','reputation':'unknown','priority':1},[],settings)
        payload=request.call_args.args[0]
        self.assertNotIn('ЖЕСТКОЕ УСЛОВИЕ:',payload['instructions'])
        self.assertIn('topic_match',payload['text']['format']['schema']['required'])
        self.assertIn('subject_type',payload['text']['format']['schema']['properties']['topic_match']['required'])
        self.assertIn('memory',payload['text']['format']['schema']['required'])
        self.assertEqual(json.loads(payload['input'][0]['content'])['thematic_policy']['version'],'v1')

    def test_changed_topic_policy_invalidates_cached_selection(self):
        from newsroom.triage import screen
        settings={'_topic_registry':topics.policy(snapshot())}
        item={'title':'Компания объявила новые условия','content':'Компания объявила новые условия'}
        result={'decision':'KEEP','reason':'Условия','evidence':item['title'],'story_id':'','what_is_new':'Условия','confidence':1}
        with patch('newsroom.triage.classify',return_value=result) as classify:
            self.assertEqual(screen(self.db,1,item,settings)['decision'],'KEEP')
            self.assertEqual(screen(self.db,1,item,settings)['decision'],'KEEP')
            settings['_topic_registry']['version']='v2'
            self.assertEqual(screen(self.db,1,item,settings)['decision'],'KEEP')
            self.assertEqual(classify.call_count,2)
