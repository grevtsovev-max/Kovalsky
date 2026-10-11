import copy
from datetime import datetime,timedelta,timezone
from unittest.mock import Mock,patch
from edition_helpers import EditionCase,draft,group,receipt,review_result,LEAD
from newsroom.edition import model,store,publication_history
from newsroom.edition.pipeline import process
from newsroom.edition.publication import publish_document
from newsroom.edition.fallback import release
from newsroom.edition.worker import recover


class PublicationHistoryTests(EditionCase):
    def post(self,text=LEAD,*,days=0,status='PUBLISHED',message='430'):
        now=datetime.now(timezone.utc)-timedelta(days=days)
        story=self.db.execute("INSERT INTO stories(canonical_topic,headline,first_seen_at,last_updated_at,entities) VALUES('Альфа','Альфа',?,?,'[]')",(now.isoformat(),now.isoformat())).lastrowid
        post=self.db.execute('INSERT INTO posts(story_id,text,status,created_at,published_at,external_id,version,source_ids,post_hash) VALUES(?,?,?,?,?,?,1,?,?)',(story,text,status,now.isoformat(),now.isoformat(),message,'[]',store.sha(text+str(story)))).lastrowid
        self.db.commit();return post

    def run_job(self,decision,*,search_error=False):
        mid=self.material();job=self.job();g=group(mid);send=Mock()
        verdict=review_result();verdict['publication_history']=copy.deepcopy(decision)
        for e in verdict['publication_history'].get('evidence',[]):e['material_id']=mid
        with patch.object(model,'plan',return_value=({'groups':[g],'excluded':[]},receipt())),patch.object(model,'draft',return_value=(draft(mid),receipt())) as writer,patch.object(model,'check',return_value=(verdict,receipt())) as checker:
            if search_error:
                with patch.object(publication_history,'candidates',side_effect=RuntimeError('failure')):result=process(self.db,job,self.settings,publish=False)
            else:result=process(self.db,job,self.settings,publish=False)
        doc=self.db.execute('SELECT * FROM edition_documents WHERE job_id=?',(job,)).fetchone()
        return result,doc,writer,checker,send

    def decision(self,post):
        return {'decision':'duplicate','post_id':post,'reason':'То же событие и комиссия; новых фактов нет.','published_fragment':LEAD,'evidence':[{'material_id':0,'quote':LEAD}]}

    def test_three_candidates_only_published_within_seven_days(self):
        ids=[self.post(message=str(i)) for i in range(5)]
        self.post(days=8);self.post(status='PENDING');self.post(status='UNKNOWN')
        self.post(text='Другая организация получила лицензию на хранение активов.')
        rows=publication_history.candidates(self.db,draft(),group())
        self.assertEqual(len(rows),3);self.assertTrue({r['post_id'] for r in rows}<=set(ids))

    def test_confirmed_repeat_is_terminal_without_repair_send_or_fallback_on_restart(self):
        post=self.post(text=LEAD+' Сервис работает в пяти сетях. Банк планирует расширить доступ в декабре.')
        result,doc,writer,checker,send=self.run_job(self.decision(post))
        self.assertEqual(doc['state'],'DUPLICATE');self.assertEqual(result['state'],'PUBLISHED')
        self.assertEqual(writer.call_count,1);self.assertEqual(checker.call_count,1)
        self.assertEqual(len(checker.call_args.kwargs['publication_history']),1)
        self.assertIsNone(doc['post_id']);self.assertEqual(doc['repairs'],0)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM edition_checks WHERE document_id=?',(doc['document_id'],)).fetchone()[0],1)
        self.assertIn('430',doc['reasons_json'])
        publish_document(self.db,doc['document_id'],self.config,send=send)
        release(self.db,doc,source=True,settings=self.settings)
        self.db.execute("UPDATE edition_jobs SET state='CHECKING' WHERE job_id=?",(result['job_id'],));self.db.commit()
        recover(self.db,self.config)
        self.assertEqual(self.db.execute('SELECT state FROM edition_documents WHERE document_id=?',(doc['document_id'],)).fetchone()[0],'DUPLICATE')
        send.assert_not_called();self.assertEqual(self.db.execute('SELECT COUNT(*) FROM posts').fetchone()[0],1)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM publication_attempts').fetchone()[0],0)

    def assert_release(self,kind):
        post=self.post();decision=self.decision(post);decision['decision']=kind
        if kind=='duplicate':decision['post_id']=999999
        _,doc,writer,checker,_=self.run_job(decision)
        self.assertEqual(doc['state'],'READY');self.assertEqual(writer.call_count,1);self.assertEqual(checker.call_count,1)

    def test_new_facts_keep_release(self):self.assert_release('new_facts')
    def test_uncertainty_keeps_release(self):self.assert_release('uncertain')
    def test_unsubstantiated_repeat_keeps_release(self):self.assert_release('duplicate')

    def test_false_quote_does_not_suppress(self):
        post=self.post();decision=self.decision(post);decision['evidence'][0]['quote']='Несуществующий факт'
        _,doc,_,_,_=self.run_job(decision);self.assertEqual(doc['state'],'READY')

    def test_search_failure_does_not_add_requests_or_delay_release(self):
        post=self.post();_,doc,writer,checker,_=self.run_job(self.decision(post),search_error=True)
        self.assertEqual(doc['state'],'READY');self.assertEqual(writer.call_count,1);self.assertEqual(checker.call_count,1)
        self.assertNotIn('publication_history',checker.call_args.kwargs)

    def test_checker_receives_history_in_same_request(self):
        history=[{'post_id':2,'text':LEAD}]
        materials=[{'material_id':1,'title':'Альфа','content':LEAD,'description':'','url':'https://example.org','source_name':'Тест'}]
        with patch.object(model,'ask',return_value=(review_result(),receipt())) as asking:
            model.check(draft(),materials,{},group=group(),publication_history=history)
        asking.assert_called_once();task=asking.call_args.args[3]
        self.assertEqual(task['publication_history'],history)
        self.assertNotIn('Сравнение с опубликованной историей запрещено',asking.call_args.args[2])
