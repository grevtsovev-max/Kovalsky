import copy
import json
import sqlite3
from unittest.mock import patch

from edition_helpers import EditionCase, draft, group, receipt, review_result, LEAD, BODY, TEXT
import test_edition_publication_history as history_tests
from test_edition_short_release import verdict
from newsroom.edition import model, store, publication_history, short_release
from newsroom.edition.pipeline import process
from newsroom.edition.publication import gate
from newsroom.edition.semantic_filter import screen
from newsroom.runtime import Runtime


class FastRuntime(Runtime):
    def db(self, timeout=30):
        return super().db(timeout=0.1)


class FilterQualityTests(EditionCase):
    def test_semantic_request_can_reserve_usage_through_real_second_connection(self):
        mid=self.material();job=self.job()
        runtime=FastRuntime(self.database, {'_agent_control_config':self.config})
        def classify(materials,settings):
            call=runtime.reserve({'model':'test'}, {'_work_stage':'edition_semantic_filter'})
            runtime.finish(call, {'usage':{}}, 0)
            return {'decisions':[{'material_id':m['material_id'],'decision':'allow',
                'reason':'Проверено.','evidence':[]} for m in materials]},receipt()
        with patch.object(model,'semantic_screen',side_effect=classify) as check:
            allowed,excluded=screen(self.db,job,store.materials(self.db,[mid]),self.settings)
        check.assert_called_once();self.assertEqual(len(allowed),1);self.assertEqual(excluded,[])
        result=store.read(self.db.execute('SELECT result_json FROM edition_semantic_decisions').fetchone()[0])
        self.assertEqual(result['decision'],'allow')
        self.assertEqual(self.db.execute('SELECT status FROM api_usage').fetchone()[0],'SUCCEEDED')
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM edition_events WHERE stage='semantic_error'").fetchone()[0],0)

    def test_technical_uncertainty_is_rechecked_but_confirmed_admission_is_cached(self):
        mid=self.material();job=self.job();materials=store.materials(self.db,[mid])
        with patch.object(model,'semantic_screen',side_effect=TimeoutError()):
            allowed,_=screen(self.db,job,materials,self.settings)
        self.assertEqual(len(allowed),1)
        with patch.object(model,'semantic_screen',return_value=({'decisions':[{
                'material_id':mid,'decision':'allow','reason':'Проверка восстановлена.','evidence':[]}]},receipt())) as check:
            screen(self.db,job,materials,self.settings)
            screen(self.db,job,materials,self.settings)
        check.assert_called_once()
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM edition_events WHERE stage='semantic_error'").fetchone()[0],1)

    def test_confirmed_rejection_is_not_reopened(self):
        mid=self.material();job=self.job();materials=store.materials(self.db,[mid])
        rejected={'decisions':[{'material_id':mid,'decision':'reject','reason':'Непрофильный сюжет.',
            'evidence':[{'material_id':mid,'quote':LEAD}]}]}
        with patch.object(model,'semantic_screen',return_value=(rejected,receipt())) as check:
            for _ in range(2):
                allowed,excluded=screen(self.db,job,materials,self.settings)
                self.assertEqual(allowed,[]);self.assertEqual(len(excluded),1)
        check.assert_called_once()

    def test_database_error_records_safe_name_and_affected_materials(self):
        mid=self.material();job=self.job();exc=sqlite3.OperationalError('private details')
        exc.sqlite_errorname='SQLITE_BUSY'
        with patch.object(model,'semantic_screen',side_effect=exc):
            screen(self.db,job,store.materials(self.db,[mid]),self.settings)
        payload=store.read(self.db.execute("SELECT payload_json FROM edition_events WHERE stage='semantic_error'").fetchone()[0])
        self.assertEqual(payload,{'code':'OperationalError','sqlite_errorname':'SQLITE_BUSY','material_ids':[mid]})

    def test_novelty_must_belong_to_selected_event_and_appear_in_post(self):
        mid=self.material(text=TEXT+' Соседний сюжет о приложении для подростков.')
        materials=store.materials(self.db,[mid]);selected=group(mid)
        selected['focus'][0]['scope']=TEXT
        history=[{'post_id':1,'text':LEAD+' '+BODY}]
        def issues(quote,fragment):
            result={'publication_history':{'decision':'new_facts','updates':[
                {'material_id':mid,'quote':quote,'post_fragment':fragment}]}}
            return publication_history.update_issues(result,history,materials,selected,draft(mid))
        self.assertEqual(issues(BODY,BODY),[])
        self.assertEqual(issues(BODY,'')[0]['code'],'new_clarification')
        self.assertEqual(issues('Соседний сюжет о приложении для подростков.',LEAD)[0]['code'],'duplicates')
        self.assertEqual(issues('Несуществующий факт.',LEAD)[0]['code'],'duplicates')

    def test_history_search_uses_body_to_find_event_among_similar_headlines(self):
        common='Биржа запустила сервис. Сервис доступен пользователям.'
        detail='Комиссия переводов USDT в сети Arbitrum составляет 0,05%.'
        target=history_tests.PublicationHistoryTests.post(self,text=common+' '+detail,days=1)
        for extra in ('Доступ открыт в Европе.','Доступ открыт в Азии.','Доступ открыт в Америке.'):
            history_tests.PublicationHistoryTests.post(self,text=common+' '+extra)
        candidate={'headline':'Биржа запустила сервис','lead':'Сервис доступен пользователям.',
                   'blocks':[{'text':detail}]}
        selected={'subject':'Биржа','entities':[],'event_key':'Запуск сервиса'}
        history=publication_history.candidates(self.db,candidate,selected)
        self.assertEqual(len(history),3)
        self.assertEqual(history[0]['post_id'],target)

    def test_omitted_real_update_gets_only_one_repair_then_release(self):
        history_tests.PublicationHistoryTests.post(self,text=LEAD+' '+BODY)
        update='Позже комиссия сервиса снизилась до 0,05%.'
        mid=self.material(text=TEXT+' '+update);job=self.job();selected=group(mid)
        selected['focus'][0]['scope']=TEXT+' '+update
        before=draft(mid);after=copy.deepcopy(before)
        after['blocks'].append({'text':update,'kind':'paragraph','evidence':[{'material_id':mid,'quote':update}],
                                'quote_text':'','quote_author':''})
        first=review_result();second=review_result()
        for result,fragment in [(first,''),(second,update)]:
            result['publication_history']={'decision':'new_facts','updates':[
                {'material_id':mid,'quote':update,'post_fragment':fragment}]}
        with patch.object(model,'plan',return_value=({'groups':[selected],'excluded':[]},receipt())), \
                patch.object(model,'draft',side_effect=[(before,receipt()),(after,receipt())]) as writer, \
                patch.object(model,'check',side_effect=[(first,receipt()),(second,receipt())]) as checker:
            result=process(self.db,job,self.settings,publish=False)
        self.assertEqual(result['state'],'READY')
        self.assertEqual((writer.call_count,checker.call_count),(2,2))
        self.assertEqual(self.db.execute('SELECT repairs FROM edition_documents').fetchone()[0],1)
        self.assertIn(update,self.db.execute('SELECT plain_text FROM edition_documents').fetchone()[0])

    def short_document(self,mid):
        job=self.job();now=store.stamp()
        self.db.execute('INSERT INTO edition_documents(document_id,job_id,group_json,materials_json,created_at,updated_at) VALUES(?,?,?,?,?,?)',
            ('short-test',job,store.encoded(group(mid)),store.encoded(store.materials(self.db,[mid])),now,now))
        self.db.commit()
        return self.db.execute("SELECT * FROM edition_documents WHERE document_id='short-test'").fetchone()

    def test_short_uncertain_update_does_not_repeat_check_or_block_release(self):
        self.legacy_policy.stop();history_tests.PublicationHistoryTests.post(self)
        mid=self.material();doc=self.short_document(mid);candidate=draft(mid)
        result=verdict(candidate,publication_history={'decision':'new_facts','updates':[]})
        with patch.object(model,'short_draft',return_value=(candidate,receipt())) as writer, \
                patch.object(model,'short_check',return_value=(result,receipt())) as checker:
            short_release.prepare(self.db,doc,self.settings)
        self.assertEqual((writer.call_count,checker.call_count),(1,1))
        self.assertEqual(self.db.execute('SELECT state FROM edition_documents').fetchone()[0],'READY')
        proof=store.read(self.db.execute('SELECT response_json FROM edition_checks').fetchone()[0])
        self.assertEqual(proof['verdict']['publication_history']['decision'],'uncertain')
        rendered=self.db.execute('SELECT rendered_text FROM edition_documents').fetchone()[0]
        gate(self.db,doc['document_id'],rendered)

    def test_short_confirmed_duplicate_has_no_post_and_no_fallback(self):
        self.legacy_policy.stop();post=history_tests.PublicationHistoryTests.post(self)
        mid=self.material();doc=self.short_document(mid);candidate=draft(mid)
        duplicate={'decision':'duplicate','post_id':post,'reason':'Те же факты.',
            'published_fragment':LEAD,'evidence':[{'material_id':mid,'quote':LEAD}],'updates':[]}
        with patch.object(model,'short_draft',return_value=(candidate,receipt())), \
                patch.object(model,'short_check',return_value=(verdict(candidate,publication_history=duplicate),receipt())) as checker, \
                patch.object(short_release,'extractive') as fallback:
            short_release.prepare(self.db,doc,self.settings)
        checker.assert_called_once();fallback.assert_not_called()
        self.assertEqual(self.db.execute('SELECT state,post_id FROM edition_documents').fetchone()[:],('DUPLICATE',None))
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM posts').fetchone()[0],1)

    def test_contradictory_duplicate_with_update_does_not_suppress_news(self):
        mid=self.material();materials=store.materials(self.db,[mid])
        history=[{'post_id':1,'message_id':'1','text':LEAD}]
        result={'publication_history':{'decision':'duplicate','post_id':1,'reason':'Повтор.',
            'published_fragment':LEAD,'evidence':[{'material_id':mid,'quote':LEAD}],
            'updates':[{'material_id':mid,'quote':BODY,'post_fragment':BODY}]}}
        self.assertIsNone(publication_history.confirmed(result,history,materials,group(mid)))

    def test_existing_model_calls_require_novelty_proof_without_extra_calls(self):
        with patch.object(model,'request_response',return_value={'id':'test','output':[
                {'content':[{'type':'output_text','text':json.dumps(review_result())}]}]}) as request:
            for call in (lambda:model.check(draft(),[],{},group=group(),publication_history=[]),
                         lambda:model.short_check(draft(),[],group(),{},publication_history=[])):
                call()
        self.assertEqual(request.call_count,2)
        for call in request.call_args_list:
            schema=call.args[0]['text']['format']['schema']
            self.assertIn('updates',schema['properties']['publication_history']['required'])
            self.assertIn('post_fragment',call.args[0]['instructions'])
