import copy
import json
import re
from unittest.mock import Mock,patch
from edition_helpers import EditionCase,draft,group,receipt,review_result,LEAD,BODY,TEXT
from newsroom.edition import model,store
from newsroom.edition.fallback import source_draft
from newsroom.edition.formatting import render,validate
from newsroom.edition.pipeline import process
from newsroom.edition.source_text import canonical,text,same_version,article_identity,formatted
from newsroom.delivery import TelegramReceipt
from newsroom.edition.worker import run_once
from newsroom.locking import acquire_cycle_lock


class RegressionTests(EditionCase):
    def test_excluded_preparation_has_explicit_retry_and_original_decision_is_preserved(self):
        mid=self.material();job=self.job()
        self.db.execute("UPDATE edition_jobs SET state='FILTERED' WHERE job_id=?",(job,));self.db.commit()
        retried=store.start(self.db,model.bundle()[2],retry_of=job)
        self.assertEqual(self.db.execute('SELECT state FROM edition_jobs WHERE job_id=?',(job,)).fetchone()[0],'FILTERED')
        row=self.db.execute('SELECT state,material_ids_json,retry_of FROM edition_jobs WHERE job_id=?',(retried,)).fetchone()
        self.assertEqual((row[0],store.read(row[1]),row[2]),('QUEUED',[mid],job))
        with self.assertRaises(ValueError):store.start(self.db,model.bundle()[2],retry_of=job)

    def test_checker_rejects_off_topic_source_without_forced_fallback(self):
        text='Зампред банка поделился опытом создания ИИ-агентов.'
        mid=self.material(text=text);job=self.job();output={'headline':'','headline_evidence':[],'lead':'','lead_evidence':[],'blocks':[]}
        issue={'code':'scope','material_id':mid,'post_fragment':'','source_fragment':text,'reason':'Исходник о личном опыте с ИИ, без цифровых активов.','main_fact':True}
        verdict=review_result(False,[issue]);verdict['source_scope']='out_of_scope';send=Mock();g=group(mid);g['focus']=[{'material_id':mid,'excerpt':text,'summary':text,'scope':text}]
        with patch.object(model,'plan',return_value=({'groups':[g],'excluded':[]},receipt())),patch.object(model,'draft',return_value=(output,receipt())) as writer,patch.object(model,'check',return_value=(verdict,receipt())):
            result=process(self.db,job,self.settings,send=send)
        self.assertEqual(result['state'],'FILTERED');send.assert_not_called();self.assertEqual(writer.call_count,1)
        self.assertEqual(self.db.execute('SELECT state FROM edition_documents').fetchone()[0],'FILTERED')
        self.assertEqual(self.db.execute("SELECT count(*) FROM edition_events WHERE stage='source_out_of_scope'").fetchone()[0],1)

    def test_missing_crypto_explanation_in_draft_does_not_discard_profiled_source(self):
        mid=self.material();job=self.job();output=draft(mid)
        issue={'code':'scope','material_id':mid,'post_fragment':output['lead'],'source_fragment':LEAD,'reason':'В тексте необходимо пояснить профильную роль участника.','main_fact':False}
        verdict=review_result(False,[issue]);verdict['source_scope']='in_scope';send=Mock(return_value=TelegramReceipt({'message_id':99,'chat':{'id':'@test'},'text':'saved'}))
        with patch.object(model,'plan',return_value=({'groups':[group(mid)],'excluded':[]},receipt())),patch.object(model,'draft',return_value=(output,receipt())) as writer,patch.object(model,'check',side_effect=[(verdict,receipt()),(review_result(),receipt())]):
            result=process(self.db,job,self.settings,send=send,publish=False)
        self.assertEqual(result['state'],'READY');self.assertEqual(writer.call_count,2)
        self.assertNotEqual(self.db.execute('SELECT state FROM edition_documents').fetchone()[0],'FILTERED')

    def test_collector_layout_change_does_not_enqueue_indexed_archive(self):
        old='Биржа открыла переводы криптовалют. Переводы доступны в пяти сетях.'
        mid=self.material(text=old,title=old[:55],queue=False,url='https://example.org/archived')
        revised=self.material(text=old.replace('. ','.\n'),title='Биржа открыла переводы криптовалют.',url='https://example.org/archived')
        self.assertEqual(self.db.execute('SELECT queue_state FROM edition_materials WHERE material_id=?',(revised,)).fetchone()[0],'DUPLICATE')
        self.assertIsNone(self.job())
        self.assertEqual(self.db.execute('SELECT related_material_id FROM edition_admissions WHERE material_id=?',(revised,)).fetchone()[0],mid)

    def test_telegram_intake_preserves_heading_and_paragraph_boundaries(self):
        from newsroom.core import TelegramPreviewParser
        parser=TelegramPreviewParser('example')
        parser.feed('<div class="tgme_widget_message_wrap" data-post="example/1"><div class="tgme_widget_message_text">Биржа открыла переводы<br>BTC доступен клиентам.<br>Комиссия — 0,1%.</div><time datetime="2026-10-09T10:00:00Z"></time></div>')
        self.assertEqual(parser.items[0]['title'],'Биржа открыла переводы')
        self.assertEqual(parser.items[0]['content'],'Биржа открыла переводы\nBTC доступен клиентам.\nКомиссия — 0,1%.')

    def test_source_fallback_has_bounded_blocks_and_no_raw_html_or_subscription(self):
        original='<p>Биржа открыла переводы криптовалют.</p><p>'+('Переводы доступны клиентам биржи. '*35)+'</p><p>Подписывайтесь на наш канал</p>'
        mid=self.material(text=original,title='🚀 Биржа открыла переводы криптовалют')
        materials=store.materials(self.db,[mid]);result=source_draft(group(mid),materials)
        markup,plain=render(result,materials)
        self.assertTrue(markup.startswith('<b>📰 Биржа'))
        self.assertIn('<blockquote expandable>',markup)
        self.assertNotIn('&lt;p&gt;',markup);self.assertNotIn('Подписывайтесь',plain)
        self.assertLessEqual(len(result['lead']),210)
        self.assertTrue(all(len(b['text'])<=210 for b in result['blocks']))
        self.assertFalse({i['code'] for i in validate(result,materials)} & {'paragraph','headline_emoji','headline_length','length','evidence'})

    def test_mechanical_paragraph_repair_preserves_words_and_numbers(self):
        original=draft();original['lead']=('Банк снизил комиссию до 0,1%. '*10).strip()
        result=formatted(original)
        merged=' '.join([result['lead'],*(b['text'] for b in result['blocks'][:-1])])
        self.assertEqual(canonical(merged),canonical(original['lead']))
        self.assertTrue(all(len(b['text'])<=210 for b in result['blocks']))
        self.assertEqual(result['blocks'][-1],original['blocks'][0])

    def test_non_topic_planner_exclusion_is_not_forced_back_into_publication(self):
        mid=self.material(text='Учёные изучили форму молекул.',title='Научная премия')
        job=self.job();send=Mock()
        with patch.object(model,'plan',return_value=({'groups':[],'excluded':[{'material_id':mid,'kind':'out_of_scope','reason':'Химия без связи с цифровыми активами.'}]},receipt())),patch.object(model,'draft') as writing:
            result=process(self.db,job,self.settings,send=send)
        self.assertEqual(result['state'],'FILTERED');send.assert_not_called();writing.assert_not_called()
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM edition_documents').fetchone()[0],0)

    def test_digest_evidence_cannot_come_from_a_neighbouring_news_item(self):
        crypto='Биржа открыла переводы криптовалют.'
        chemistry='Учёные получили премию за исследование молекул.'
        mid=self.material(text='1. '+crypto+' 2. '+chemistry,title='Утренний дайджест')
        g=group(mid);g['focus']=[{'material_id':mid,'excerpt':crypto,'summary':crypto,'scope':crypto}]
        output=draft(mid);output['lead']=chemistry;output['lead_evidence']=[{'material_id':mid,'quote':chemistry}]
        self.assertIn('scope',{i['code'] for i in validate(output,store.materials(self.db,[mid]),g)})

    def test_planner_receives_saved_thematic_authority(self):
        mid=self.material()
        settings={**self.settings,'_topic_registry':{'topics':[{'name':'Криптовалюты','scope':'Операции с цифровыми активами'}]}}
        response={'id':'test-response','status':'completed','output':[{'content':[{'type':'output_text','text':json.dumps({'groups':[],'excluded':[]})}]}]}
        with patch('newsroom.edition.model.request_response',return_value=response) as api:model.plan(store.materials(self.db,[mid]),settings)
        self.assertEqual(json.loads(api.call_args.args[0]['input'])['task']['thematic_policy'],settings['_topic_registry'])

    def test_terms_in_quotation_marks_do_not_require_verbatim_direct_speech(self):
        mid=self.material(text='Порядок позволяет отклонить зачисление. '+TEXT)
        output=draft(mid);output['blocks'][0]['text']='Правила описывают процедуру «отклонения зачисления».'
        output['blocks'][0]['evidence']=[{'material_id':mid,'quote':'Порядок позволяет отклонить зачисление.'}]
        self.assertNotIn('quotes',{i['code'] for i in validate(output,store.materials(self.db,[mid]))})

    def test_cosmetic_edits_do_not_republish_the_same_received_material(self):
        original=('Проект опубликовал правила. '*30)+'Для соблюдения требований создают отдельную службу, которая определяет, что нужно делать.'
        mid=self.material(text=original,url='https://example.org/one')
        self.db.execute("UPDATE edition_materials SET queue_state='DONE' WHERE material_id=?",(mid,));self.db.commit()
        edited=original.replace('отдельную','отдельный').replace('что нужно','как нужно').replace('службу,','службу;')
        next_mid=self.material(text=edited,url='https://example.org/one')
        self.assertEqual(self.db.execute('SELECT queue_state FROM edition_materials WHERE material_id=?',(next_mid,)).fetchone()[0],'DUPLICATE')
        self.assertIsNone(self.job())
        self.assertEqual(self.db.execute('SELECT related_material_id FROM edition_admissions WHERE material_id=?',(next_mid,)).fetchone()[0],mid)

    def test_changes_to_numbers_negation_and_action_are_not_cosmetic(self):
        material={'content':('Проект опубликовал правила. '*30)+'Лимит вырос до 300 рублей. Проект разрешил переводы.'}
        for edited in [material['content'].replace('300','500'),material['content'].replace('разрешил','запретил'),material['content'].replace('разрешил','не разрешил')]:
            self.assertFalse(same_version(material,{'content':edited}))
        self.assertFalse(same_version({'title':'BTC стоит $80 000','content':material['content']},
                                      {'title':'BTC стоит $90 000','content':material['content']}))

    def test_identical_feed_event_has_one_release_across_publishers(self):
        a={'title':'Регулятор ограничил USDT - Alpha','description':'<a href="https://redirect.example/1">Регулятор ограничил USDT</a> <font>Alpha</font>','primary_source_json':json.dumps({'_material_publisher':'Alpha'})}
        b={'title':'Регулятор ограничил USDT - Beta','description':'<a href="https://redirect.example/2">Регулятор ограничил USDT</a> <font>Beta</font>','primary_source_json':json.dumps({'_material_publisher':'Beta'})}
        self.assertEqual(article_identity(a),article_identity(b))
        b['description']+=' Ограничение действует только для неквалифицированных инвесторов.'
        self.assertNotEqual(article_identity(a),article_identity(b))

    def test_new_substantive_version_supersedes_only_unclaimed_snapshot(self):
        mid=self.material(text=TEXT,url='https://example.org/one')
        next_mid=self.material(text=TEXT+' Лимит вырос до 500 рублей.',url='https://example.org/one')
        self.assertEqual(self.db.execute('SELECT queue_state FROM edition_materials WHERE material_id=?',(mid,)).fetchone()[0],'SUPERSEDED')
        job=self.job();ids=store.read(self.db.execute('SELECT material_ids_json FROM edition_jobs WHERE job_id=?',(job,)).fetchone()[0],[])
        self.assertEqual(ids,[next_mid])

    def test_editor_freezes_cohort_after_the_collection_cycle_finishes(self):
        self.material()
        lock=acquire_cycle_lock(self.database)
        try:
            with patch.object(model,'plan') as planner:
                self.assertIsNone(run_once(self.config,publish=False));planner.assert_not_called()
            self.assertEqual(self.db.execute("SELECT COUNT(*) FROM edition_materials WHERE queue_state='QUEUED'").fetchone()[0],1)
        finally:lock.close()

    def test_context_search_does_not_import_an_off_topic_neighbour(self):
        self.material(text='Банк разработал правила криптовалютных переводов.')
        self.material(text='Банк изменил правила госпитализации.',queue=False)
        job=self.job()
        results=store.lookup(self.db,job,'Банк правила',topic_spec={'keywords':['криптовалютный перевод']})
        self.assertEqual(len(results),1)
        self.assertIn('криптовалютных',results[0]['content'])

    def test_rule_update_does_not_strand_interrupted_preparation(self):
        from newsroom.edition.worker import recover
        from newsroom.edition.publication import publish_document
        self.material();job=self.job()
        self.db.execute("UPDATE edition_jobs SET bundle_hash='previous-policy',state='CHECKING' WHERE job_id=?",(job,));self.db.commit()
        recover(self.db,self.config)
        document=self.db.execute('SELECT * FROM edition_documents WHERE job_id=?',(job,)).fetchone()
        send=Mock(return_value=TelegramReceipt({'message_id':707}))
        publish_document(self.db,document['document_id'],self.config,send=send)
        send.assert_called_once()
        self.assertEqual(self.db.execute('SELECT state FROM edition_jobs WHERE job_id=?',(job,)).fetchone()[0],'PUBLISHED')
