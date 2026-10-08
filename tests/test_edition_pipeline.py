import copy
import json
from unittest.mock import Mock,patch
from newsroom.edition import model,store
from newsroom.edition.pipeline import process,merge_groups
from newsroom.edition.publication import gate,publish_document
from newsroom.edition.views import retry,details,overview
from newsroom.delivery import DeliveryRejected,DeliveryUncertain,TelegramReceipt
from edition_helpers import review_result,EditionCase,draft,group,receipt,LEAD,TEXT


class PipelineTests(EditionCase):
    def run_job(self,*,writer=None,checker=None,send=None,publish=True,job_id=None):
        job_id=job_id or self.job()
        plan={'groups':[group(self.mid)],'excluded':[]}
        with patch.object(model,'plan',return_value=(plan,receipt())),patch.object(model,'draft',side_effect=writer or [(draft(self.mid),receipt())]) as writing,patch.object(model,'check',side_effect=checker or [(review_result(),receipt())]) as checking:
            result=process(self.db,job_id,self.settings,send=send or self.send,publish=publish)
        return result,writing,checking

    def setUp(self):
        super().setUp();self.mid=self.material();self.send=Mock(return_value=TelegramReceipt({'message_id':123,'text':'saved response'}))

    def document(self):return self.db.execute('SELECT * FROM edition_documents ORDER BY created_at DESC LIMIT 1').fetchone()

    def test_successfully_checked_text_is_automatically_sent_once(self):
        result,writer,checker=self.run_job()
        self.assertEqual(result['state'],'PUBLISHED');self.send.assert_called_once()
        self.assertEqual(writer.call_count,1);self.assertEqual(checker.call_count,1)
        doc=self.document();self.assertEqual(doc['state'],'PUBLISHED')
        self.assertEqual(self.db.execute('SELECT status FROM posts').fetchone()[0],'PUBLISHED')
        publish_document(self.db,doc['document_id'],self.config,send=self.send)
        self.send.assert_called_once()
        data=details(self.db,result['job_id'])
        self.assertTrue(data['materials']);self.assertEqual(data['checks'][0]['passed'],1)
        self.assertTrue(any(e['stage']=='published' for e in data['events']))

    def test_deferred_delivery_updates_job_and_uses_group_receipt_time(self):
        result,_,_=self.run_job(publish=False)
        self.db.execute("UPDATE edition_jobs SET received_at='2000-01-01T00:00:00+00:00' WHERE job_id=?",(result['job_id'],));self.db.commit()
        publish_document(self.db,self.document()['document_id'],self.config,send=self.send)
        job=self.db.execute('SELECT state FROM edition_jobs WHERE job_id=?',(result['job_id'],)).fetchone()
        self.assertEqual(job[0],'PUBLISHED')
        event=self.db.execute("SELECT payload_json FROM edition_events WHERE job_id=? AND stage='published'",(result['job_id'],)).fetchone()
        self.assertLess(store.read(event[0],{})['received_to_channel_seconds'],60)

    def test_oversized_block_gets_one_targeted_repair(self):
        bad=draft(self.mid);bad['blocks'][0]['text']='а'*211
        result,writer,checker=self.run_job(writer=[(bad,receipt()),(draft(self.mid),receipt())],checker=[(review_result(),receipt())]*2)
        self.assertEqual(result['state'],'PUBLISHED');self.assertEqual(writer.call_count,2);self.assertEqual(checker.call_count,2)
        self.assertEqual(self.document()['repairs'],1)
        self.assertEqual(writer.call_args.kwargs['previous'],bad)
        self.assertTrue(writer.call_args.kwargs['feedback'])

    def test_failed_style_repair_is_saved_and_published_without_automatic_replay(self):
        bad=draft(self.mid);bad['headline']='🏦 Коротко'
        result,writer,checker=self.run_job(writer=[(bad,receipt())]*2,checker=[(review_result(),receipt())]*2)
        self.assertEqual(result['state'],'PUBLISHED');self.send.assert_called_once()
        self.assertEqual(writer.call_count,2);self.assertEqual(self.document()['repairs'],1)
        self.assertEqual(store.start(self.db,model.bundle()[2]),None)
        self.assertIn('headline_length',self.document()['reasons_json'])
        self.assertEqual(len(details(self.db,result['job_id'])['checks']),3)

    def test_explicit_retry_preserves_old_attempt_and_starts_a_new_one(self):
        bad=draft(self.mid);bad['headline']='🏦 Коротко'
        result,_,_=self.run_job(writer=[(bad,receipt())]*2,checker=[(review_result(),receipt())]*2,publish=False)
        # A historical unfinished attempt remains available for an explicit retry.
        self.db.execute("UPDATE edition_jobs SET state='INCOMPLETE' WHERE job_id=?",(result['job_id'],))
        self.db.execute("UPDATE edition_documents SET state='INCOMPLETE' WHERE job_id=?",(result['job_id'],));self.db.commit()
        new=retry(self.db,self.config,result['job_id'])
        self.assertNotEqual(new['job_id'],result['job_id'])
        self.assertEqual(self.db.execute('SELECT state FROM edition_jobs WHERE job_id=?',(result['job_id'],)).fetchone()[0],'INCOMPLETE')
        with self.assertRaises(ValueError):retry(self.db,self.config,result['job_id'])
        from newsroom.edition.worker import recover
        recover(self.db,self.config)
        self.assertEqual(self.db.execute('SELECT state FROM edition_jobs WHERE job_id=?',(new['job_id'],)).fetchone()[0],'QUEUED')
        finished,writing,_=self.run_job(job_id=new['job_id'])
        self.assertEqual(writing.call_args.kwargs['previous'],bad)
        self.assertTrue(writing.call_args.kwargs['feedback'])
        self.assertEqual(finished['state'],'PUBLISHED')
        with self.assertRaises(ValueError):retry(self.db,self.config,result['job_id'])
        original=next(j for j in overview(self.db,self.config)['jobs'] if j['job_id']==result['job_id'])
        self.assertFalse(original['retryable'])

    def test_ungrounded_review_releases_source_instead_of_stranding_post(self):
        verdict=review_result(False,[{'code':'status','post_fragment':LEAD,'source_fragment':'Источник этого не говорил','material_id':self.mid,'reason':'Статус усилен','main_fact':True}])
        result,writer,checker=self.run_job(checker=[(verdict,receipt())])
        self.assertEqual(result['state'],'PUBLISHED');self.send.assert_called_once();self.assertEqual(writer.call_count,1)
        saved=details(self.db,result['job_id'])
        self.assertTrue(any(e['stage']=='review_response' and e['payload']['verdict']==verdict for e in saved['events']))
        self.assertIn(TEXT,self.document()['plain_text'])

    def test_status_mismatch_without_fragments_releases_source(self):
        verdict=review_result(False,[{'code':'status','post_fragment':LEAD,'source_fragment':'','material_id':self.mid,'reason':'Статус усилен','main_fact':True}])
        result,_,_=self.run_job(checker=[(verdict,receipt())])
        self.assertEqual(result['state'],'PUBLISHED');self.send.assert_called_once()

    def test_main_conflict_releases_source_after_one_correction(self):
        issue={'code':'main_conflict','post_fragment':LEAD,'source_fragment':LEAD,'material_id':self.mid,'reason':'Главный факт противоречит источнику','main_fact':True}
        result,writer,_=self.run_job(writer=[(draft(self.mid),receipt())]*2,checker=[(review_result(False,[issue]),receipt())]*2)
        self.assertEqual(result['state'],'PUBLISHED');self.send.assert_called_once();self.assertEqual(writer.call_count,2)

    def test_text_tampering_after_check_is_blocked(self):
        result,_,_=self.run_job(publish=False)
        doc=self.document();self.assertEqual(result['state'],'READY')
        self.db.execute("UPDATE edition_documents SET rendered_text=rendered_text||' изменено' WHERE document_id=?",(doc['document_id'],));self.db.commit()
        with self.assertRaises(DeliveryRejected):gate(self.db,doc['document_id'],doc['rendered_text']+' изменено')
        self.send.assert_not_called()

    def test_evidence_tampering_is_blocked(self):
        self.run_job(publish=False);doc=self.document();mats=store.read(doc['materials_json'],[]);mats[0]['content']='Подменено'
        self.db.execute('UPDATE edition_documents SET materials_json=? WHERE document_id=?',(store.encoded(mats),doc['document_id']));self.db.commit()
        with self.assertRaises(DeliveryRejected):gate(self.db,doc['document_id'],doc['rendered_text'])

    def test_rules_change_invalidates_final_admission(self):
        self.run_job(publish=False);doc=self.document();policy,refs,_=model.bundle()
        with patch.object(model,'bundle',return_value=(policy,refs,'different')):
            with self.assertRaises(DeliveryRejected):gate(self.db,doc['document_id'],doc['rendered_text'])

    def test_unknown_delivery_blocks_resend_and_preparation_retry(self):
        self.send.side_effect=TimeoutError()
        result,_,_=self.run_job();self.assertEqual(self.document()['state'],'UNKNOWN')
        self.send.assert_called_once()
        with self.assertRaises(ValueError):retry(self.db,self.config,result['job_id'])
        publish_document(self.db,self.document()['document_id'],self.config,send=self.send)
        self.send.assert_called_once()
        self.assertEqual(self.document()['state'],'UNKNOWN')
        with self.assertRaises(ValueError):retry(self.db,self.config,result['job_id'])

    def test_published_history_is_not_used_as_a_duplicate_gate(self):
        self.run_job();first=self.document()['job_id']
        self.mid=self.material(text=TEXT+' Ещё одно сообщение.',title='Та же новость другим источником')
        self.run_job();self.assertEqual(self.send.call_count,2)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM posts WHERE status='PUBLISHED'").fetchone()[0],2)

    def test_model_calls_receive_policy_and_verbatim_references_without_search_tools(self):
        response={'status':'completed','id':'response','output':[{'content':[{'type':'output_text','text':json.dumps(draft(self.mid),ensure_ascii=False)}]}]}
        with patch('newsroom.edition.model.request_response',return_value=response) as api:
            model.draft(group(self.mid),store.materials(self.db,[self.mid]),self.settings)
        payload,settings=api.call_args.args
        self.assertNotIn('tools',payload);self.assertFalse(settings['web_search_enabled'])
        data=json.loads(payload['input']);self.assertEqual(len(data['references']['references']),14)
        self.assertEqual(data['references']['references'][0]['text'],'Solana запустила обновление сети')
        self.assertEqual(data['rules']['limits']['headline_min'],80)

    def test_checker_receives_final_bold_headline_and_linked_sources(self):
        response={'status':'completed','id':'response','output':[{'content':[{'type':'output_text','text':json.dumps({'approved':True,'issues':[],'assessments':{key:{'passed':True,'explanation':'Проверено на условном материале'} for key in model.REVIEW_RULES}})}]}]}
        with patch('newsroom.edition.model.request_response',return_value=response) as api:
            model.check(draft(self.mid),store.materials(self.db,[self.mid]),self.settings)
        payload,_=api.call_args.args
        task=json.loads(payload['input'])['task']
        self.assertIn('<b>',task['rendered_html'])
        self.assertIn('Источник: <a href="https://example.org/news/1">',task['rendered_html'])
        self.assertIn('Источник: Проверенный источник',task['plain_text'])

    def test_missing_or_contradictory_semantic_assessment_cannot_get_admission(self):
        missing=review_result();missing['assessments'].pop('terms')
        with self.assertRaises(model.AIResponseError):model.validate_review(missing)
        inconsistent=review_result();inconsistent['assessments']['duplicates']['passed']=False
        with self.assertRaises(model.AIResponseError):model.validate_review(inconsistent)
        unexplained=review_result();unexplained['assessments']['headline_meaning']['explanation']=''
        with self.assertRaises(model.AIResponseError):model.validate_review(unexplained)

    def test_legacy_boolean_approval_cannot_pass_delivery_gate(self):
        self.run_job(publish=False)
        doc=self.document()
        self.db.execute("INSERT INTO edition_checks(document_id,draft_hash,rendered_hash,bundle_hash,evidence_hash,code_json,review_json,response_json,passed,created_at) SELECT document_id,draft_hash,rendered_hash,bundle_hash,evidence_hash,code_json,?,response_json,passed,created_at FROM edition_checks WHERE document_id=? ORDER BY check_id DESC LIMIT 1",(json.dumps({'approved':True,'issues':[]}),doc['document_id']));self.db.commit()
        with self.assertRaises(DeliveryRejected):gate(self.db,doc['document_id'],doc['rendered_text'])

    def test_merge_same_brand_and_shared_event_but_separate_other_brands(self):
        a=group(1);b=group(2);b['event_key']='Лицензия'
        c=group(3);c['entities']=['Компания Бета'];c['event_key']='Запуск другого сервиса'
        merged=merge_groups([a,b,c]);self.assertEqual(len(merged),2);self.assertEqual(merged[0]['material_ids'],[1,2])
        a=group(1);b=group(2);b['entities']=['Компания Бета']
        self.assertEqual(len(merge_groups([a,b])),1)
