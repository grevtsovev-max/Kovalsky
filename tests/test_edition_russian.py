import copy
from unittest.mock import Mock,patch
from edition_helpers import EditionCase,draft,group,receipt,review_result
from newsroom.edition import model,store
from newsroom.edition.language import russian_prose,issues
from newsroom.edition.pipeline import process
from newsroom.edition.fallback import source_draft,release,can_release
from newsroom.edition.publication import gate,publish_document
from newsroom.edition.worker import run_once,recover
from newsroom.delivery import DeliveryRejected,TelegramReceipt

ENGLISH="Cardano founder Charles Hoskinson disputes Vitalik Buterin's AI cryptography warning, defending quantum-resistant security methods used in current standards."
RUSSIAN='Основатель Cardano Чарльз Хоскинсон оспорил предупреждение Виталика Бутерина об угрозах ИИ для криптографии и защитил методы безопасности, устойчивые к квантовым вычислениям.'


class RussianTests(EditionCase):
    def translated(self,original,settings=None,feedback=None):
        return {'headline':'📰 Хоскинсон оспорил предупреждение Бутерина об угрозах ИИ',
                'lead':RUSSIAN,'blocks':['Дополнительные сведения приведены в источнике.' for _ in original['blocks']]},receipt()

    def run_source(self,translator=None,checker=None):
        mid=self.material(text=ENGLISH,title='Cardano’s Hoskinson challenges Vitalik over AI threats to crypto');job=self.job()
        send=Mock(return_value=TelegramReceipt({'message_id':999}))
        with patch.object(model,'plan',side_effect=ValueError('EDITION_TOPIC_FROM_DIFFERENT_EVENT')),patch.object(model,'translate_source',side_effect=translator or self.translated),patch.object(model,'check_translation',side_effect=checker or (lambda *a: ({'approved':True,'issues':[]},receipt()))):
            result=process(self.db,job,self.settings,send=send)
        return mid,job,send,result

    def test_russian_names_brands_and_tickers_are_allowed_but_english_sentences_are_not(self):
        for value in ('🚀 OpenAI запустила сервис для BTC и ETH.','Charles Hoskinson оспорил предупреждение Vitalik Buterin.','Комиссия составляет 0,1%.'):
            self.assertTrue(russian_prose(value),value)
        for value in (ENGLISH,'Cardano’s Hoskinson challenges Vitalik over AI threats to crypto','This block is in English.','中文新闻'):
            self.assertFalse(russian_prose(value),value)

    def test_post_432_failure_path_publishes_verified_russian_translation_only(self):
        mid,job,send,result=self.run_source()
        self.assertEqual(result['state'],'PUBLISHED');send.assert_called_once()
        self.assertIn(RUSSIAN,send.call_args.args[1]);self.assertNotIn(ENGLISH,send.call_args.args[1])
        doc=self.db.execute('SELECT * FROM edition_documents').fetchone();proof=self.db.execute('SELECT review_json FROM edition_checks ORDER BY check_id DESC LIMIT 1').fetchone()
        self.assertEqual(store.read(proof[0],{})['release_mode'],'TRANSLATED_SOURCE')
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM edition_source_translations').fetchone()[0],1)
        gate(self.db,doc['document_id'],doc['rendered_text'])

    def test_translation_transport_failure_never_sends_english(self):
        mid,job,send,result=self.run_source(translator=TimeoutError())
        send.assert_not_called();self.assertEqual(result['state'],'INCOMPLETE')
        self.assertEqual(self.db.execute('SELECT state FROM edition_documents').fetchone()[0],'LANGUAGE_PENDING')
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM publication_attempts').fetchone()[0],0)

    def test_translation_is_independently_checked_and_gets_only_one_repair(self):
        translator=Mock(side_effect=self.translated);checker=Mock(return_value=({'approved':False,'issues':['Статус исходника изменён.']},receipt()))
        mid,job,send,result=self.run_source(translator=translator,checker=checker)
        send.assert_not_called();self.assertEqual(translator.call_count,2);self.assertEqual(checker.call_count,2)
        self.assertTrue(translator.call_args.kwargs['feedback'])
        self.assertEqual(self.db.execute('SELECT automatic FROM edition_translation_waits').fetchone()[0],0)

    def test_waiting_translation_resumes_without_restarting_planner(self):
        mid,job,send,result=self.run_source(translator=TimeoutError())
        self.db.execute("UPDATE edition_translation_waits SET retry_after='2000-01-01T00:00:00+00:00'");self.db.commit()
        with patch.object(model,'translate_source',side_effect=self.translated),patch.object(model,'check_translation',return_value=({'approved':True,'issues':[]},receipt())),patch.object(model,'plan') as planner:
            run_once(self.config,send=send)
        planner.assert_not_called();send.assert_called_once()
        self.assertEqual(self.db.execute('SELECT state FROM edition_jobs WHERE job_id=?',(job,)).fetchone()[0],'PUBLISHED')

    def test_verified_translation_is_reused_on_recovery(self):
        mid,job,send,result=self.run_source()
        doc=self.db.execute('SELECT * FROM edition_documents').fetchone()
        with patch.object(model,'translate_source') as translator:
            from newsroom.edition.source_translation import translate
            materials=store.read(doc['materials_json'],[]);g=store.read(doc['group_json'],{})
            saved,proof=translate(self.db,doc,source_draft(g,materials),materials,g,self.settings)
        translator.assert_not_called();self.assertEqual(saved,store.read(doc['draft_json'],{}))

    def test_translation_receipt_cannot_be_removed_or_replaced(self):
        mid,job,send,result=self.run_source();doc=self.db.execute('SELECT * FROM edition_documents').fetchone()
        self.db.execute('DELETE FROM edition_source_translations');self.db.commit()
        with self.assertRaises(DeliveryRejected):gate(self.db,doc['document_id'],doc['rendered_text'])

    def test_one_untranslated_paragraph_is_detected_in_a_russian_post(self):
        output=draft();output['blocks'][0]['text']=ENGLISH
        self.assertTrue(issues(output));self.assertFalse(can_release(issues(output)))

    def test_foreign_source_after_restart_waits_for_translation(self):
        self.material(text=ENGLISH);job=self.job()
        with patch.object(model,'translate_source',side_effect=TimeoutError()):recover(self.db,self.config)
        self.assertEqual(self.db.execute('SELECT state FROM edition_documents').fetchone()[0],'LANGUAGE_PENDING')
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM posts').fetchone()[0],0)

    def test_delivery_gate_rejects_english_even_if_marked_ready(self):
        mid,job,send,result=self.run_source(translator=TimeoutError())
        doc=self.db.execute('SELECT * FROM edition_documents').fetchone()
        original=source_draft(store.read(doc['group_json'],{}),store.read(doc['materials_json'],[]))
        from newsroom.edition.formatting import render
        markup,plain=render(original,store.read(doc['materials_json'],[]))
        self.db.execute("UPDATE edition_documents SET state='READY',draft_json=?,rendered_text=?,plain_text=? WHERE document_id=?",(store.encoded(original),markup,plain,doc['document_id']));self.db.commit()
        with self.assertRaisesRegex(DeliveryRejected,'EDITION_RUSSIAN_REQUIRED'):gate(self.db,doc['document_id'],markup)

    def test_explicit_retry_disables_automatic_translation_of_the_old_attempt(self):
        mid,job,send,result=self.run_source(translator=TimeoutError())
        retry=store.start(self.db,model.bundle()[2],retry_of=job)
        self.assertIsNotNone(retry)
        self.assertEqual(self.db.execute('SELECT automatic FROM edition_translation_waits').fetchone()[0],0)

    def test_waiting_english_post_does_not_block_other_russian_posts(self):
        english=self.material(text=ENGLISH);russian=self.material();job=self.job()
        first=group(english);second=group(russian);second['entities']=['Другой участник'];second['event_key']='Другое событие'
        first['focus']=[{'material_id':english,'excerpt':ENGLISH,'summary':'Спор о криптографии.'}]
        send=Mock(return_value=TelegramReceipt({'message_id':1000}))
        with patch.object(model,'plan',return_value=({'groups':[first,second],'excluded':[]},receipt())),patch.object(model,'draft',side_effect=[TimeoutError(),(draft(russian),receipt())]),patch.object(model,'check',return_value=(review_result(),receipt())),patch.object(model,'translate_source',side_effect=TimeoutError()):
            process(self.db,job,self.settings,send=send)
        send.assert_called_once();self.assertNotIn(ENGLISH,send.call_args.args[1])
        self.assertEqual(sorted(r[0] for r in self.db.execute('SELECT state FROM edition_documents')),['LANGUAGE_PENDING','PUBLISHED'])

    def test_identical_translated_reserves_have_one_verified_translation_and_delivery(self):
        mid=self.material(text=ENGLISH);job=self.job();groups=[]
        for n in range(3):
            g=group(mid);g.update(entities=['Участник '+str(n)],event_key=str(n),focus=[{'material_id':mid,'excerpt':ENGLISH,'summary':'Спор о криптографии.'}]);groups.append(g)
        translator=Mock(side_effect=self.translated);send=Mock(return_value=TelegramReceipt({'message_id':1001}))
        with patch.object(model,'plan',return_value=({'groups':groups,'excluded':[]},receipt())),patch.object(model,'draft',side_effect=TimeoutError()),patch.object(model,'translate_source',side_effect=translator),patch.object(model,'check_translation',return_value=({'approved':True,'issues':[]},receipt())):
            result=process(self.db,job,self.settings,send=send)
        self.assertEqual(result['state'],'PUBLISHED');translator.assert_called_once();send.assert_called_once()
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM edition_source_coverage').fetchone()[0],2)
