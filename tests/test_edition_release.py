from unittest.mock import Mock,patch
from edition_helpers import EditionCase,draft,group,receipt,review_result,TEXT
from newsroom.edition import model,store
from newsroom.edition.pipeline import process
from newsroom.edition.publication import gate,publish_document
from newsroom.edition.worker import recover
from newsroom.delivery import TelegramReceipt,DeliveryRejected


class ReleaseTests(EditionCase):
    def setUp(self):
        super().setUp();self.mid=self.material();self.send=Mock(return_value=TelegramReceipt({'message_id':987}))

    def run_failure(self,*,plan=None,writer=None,checker=None,publish=True):
        job=self.job()
        with patch.object(model,'plan',side_effect=plan or [({'groups':[group(self.mid)],'excluded':[]},receipt())]),patch.object(model,'draft',side_effect=writer or [(draft(self.mid),receipt())]),patch.object(model,'check',side_effect=checker or [(review_result(),receipt())]):
            result=process(self.db,job,self.settings,send=self.send,publish=publish)
        return result

    def test_planner_api_failure_still_publishes_every_admitted_material(self):
        second=self.material(text='Другой проект открыл сервис переводов.',title='Другой проект')
        result=self.run_failure(plan=TimeoutError())
        self.assertEqual(result['state'],'PUBLISHED');self.assertEqual(self.send.call_count,2)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM edition_documents WHERE state='PUBLISHED'").fetchone()[0],2)
        self.assertIn('Другой проект открыл сервис переводов.',self.send.call_args.args[1])

    def test_writer_failure_releases_source_without_another_model_attempt(self):
        result=self.run_failure(writer=TimeoutError())
        self.assertEqual(result['state'],'PUBLISHED');self.send.assert_called_once()
        from newsroom.edition.source_text import text
        self.assertIn(' '.join(TEXT.split()),' '.join(text(self.send.call_args.args[1]).split()))

    def test_checker_failure_releases_source_and_keeps_the_generated_draft(self):
        result=self.run_failure(checker=TimeoutError())
        self.assertEqual(result['state'],'PUBLISHED');self.send.assert_called_once()
        events=[store.read(r[0],{}) for r in self.db.execute("SELECT payload_json FROM edition_events WHERE stage='draft'")]
        self.assertEqual(events[0]['draft']['headline'],draft(self.mid)['headline'])

    def test_planner_exclusion_cannot_remove_an_admitted_material(self):
        result=self.run_failure(plan=[({'groups':[],'excluded':[{'material_id':self.mid,'reason':'Короткий материал'}]},receipt())])
        self.assertEqual(result['state'],'PUBLISHED');self.send.assert_called_once()

    def test_source_release_is_bound_to_exact_saved_material_and_text(self):
        self.run_failure(checker=TimeoutError(),publish=False)
        doc=self.db.execute('SELECT * FROM edition_documents').fetchone()
        gate(self.db,doc['document_id'],doc['rendered_text'])
        self.db.execute("UPDATE edition_documents SET rendered_text=rendered_text||' выдуманный факт' WHERE document_id=?",(doc['document_id'],));self.db.commit()
        with self.assertRaises(DeliveryRejected):gate(self.db,doc['document_id'],doc['rendered_text']+' выдуманный факт')
        self.send.assert_not_called()

    def test_interruption_creates_release_without_calling_the_model(self):
        job=self.job()
        with patch.object(model,'draft') as writing:recover(self.db,self.config)
        writing.assert_not_called()
        doc=self.db.execute('SELECT * FROM edition_documents').fetchone();self.assertEqual(doc['state'],'READY')
        publish_document(self.db,doc['document_id'],self.config,send=self.send);self.send.assert_called_once()

    def test_remaining_style_issues_publish_but_factual_issues_use_source(self):
        issue={'code':'terms','post_fragment':TEXT[:10],'source_fragment':'','material_id':self.mid,'reason':'Термин не пояснён','main_fact':False}
        text=draft(self.mid);issue['post_fragment']=text['lead']
        result=self.run_failure(writer=[(text,receipt())]*2,checker=[(review_result(False,[issue]),receipt())]*2)
        self.assertEqual(result['state'],'PUBLISHED')
        review=self.db.execute('SELECT review_json FROM edition_checks ORDER BY check_id DESC LIMIT 1').fetchone()[0]
        self.assertEqual(store.read(review,{})['release_mode'],'ADVISORY')
        self.assertIn('terms',self.db.execute('SELECT reasons_json FROM edition_documents').fetchone()[0])

    def test_invalid_model_assessment_is_saved_and_replaced_by_source(self):
        error=model.AIResponseError('EDITION_REVIEW_COVERAGE_MISSING');error.review={'approved':True,'issues':[]};error.receipt=receipt()
        result=self.run_failure(checker=error)
        self.assertEqual(result['state'],'PUBLISHED')
        event=self.db.execute("SELECT payload_json FROM edition_events WHERE stage='review_response'").fetchone()
        self.assertEqual(store.read(event[0],{})['verdict'],error.review)

    def test_large_source_bundle_is_split_to_fit_telegram_without_losing_materials(self):
        for n in range(69):self.material(text='Проект сообщил о запуске сервиса. '+str(n),title='Сообщение '+str(n))
        job=self.job();ids=store.read(self.db.execute('SELECT material_ids_json FROM edition_jobs WHERE job_id=?',(job,)).fetchone()[0],[])
        planned=group(self.mid);planned['material_ids']=ids
        self.db.execute('UPDATE edition_jobs SET groups_json=? WHERE job_id=?',(store.encoded([planned]),job));self.db.commit()
        from newsroom.edition.fallback import ensure_job
        ensure_job(self.db,job,{'code':'test_failure','reason':'Ошибка проверки'})
        docs=self.db.execute('SELECT * FROM edition_documents WHERE job_id=?',(job,)).fetchall()
        covered={i for d in docs for i in store.read(d['group_json'],{})['material_ids']}
        self.assertEqual(covered,set(ids))
        for doc in docs:
            self.assertEqual(doc['state'],'READY')
            self.assertLessEqual(len(doc['plain_text'].encode('utf-16-le'))//2,4096)
            gate(self.db,doc['document_id'],doc['rendered_text'])
