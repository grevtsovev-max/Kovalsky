import copy
from unittest.mock import patch,Mock
from edition_helpers import EditionCase,draft,group,receipt
from newsroom.edition import model,store,short_release,fragments
from newsroom.edition.pipeline import process,plan_all
from newsroom.edition.publication import gate,publish_document
from newsroom.delivery import TelegramReceipt,DeliveryRejected


def verdict(candidate,**changes):
    return {'final_draft':copy.deepcopy(candidate),'corrections':[],
            'headline_supported':True,'lead_supported':True,
            'blocks_supported':[True]*len(candidate['blocks']),'issues':[],
            'publication_history':{'decision':'uncertain','post_id':0,'reason':'Нет истории.',
                                   'published_fragment':'','evidence':[]},**changes}


class ShortReleaseTests(EditionCase):
    def setUp(self):
        super().setUp();self.legacy_policy.stop()
        self.new_policy=patch.object(model,'bundle',side_effect=lambda: (
            {**self.real_bundle()[0],'short_release':True},*self.real_bundle()[1:]))
        self.new_policy.start();self.addCleanup(self.new_policy.stop)
        self.mid=self.material();self.candidate=draft(self.mid)
        self.send=Mock(return_value=TelegramReceipt({'message_id':501}))

    def document(self):
        job=self.job();materials=store.materials(self.db,[self.mid]);now=store.stamp()
        self.db.execute('INSERT INTO edition_documents(document_id,job_id,group_json,materials_json,created_at,updated_at) VALUES(?,?,?,?,?,?)',
                        ('short-test',job,store.encoded(group(self.mid)),store.encoded(materials),now,now))
        self.db.commit();return self.db.execute('SELECT * FROM edition_documents').fetchone()

    def test_forklog_navigation_is_never_selected_as_offline_lead(self):
        headline='В Южной Корее начали выдавать кредиты под залог биткоина'
        fact='Южнокорейский Jeonbuk Bank первым среди банков страны начал выдавать бизнесу кредиты в вонах под залог первой криптовалюты.'
        source='Искусственный интеллект\nДайджест месяца\nКорпораты\nЦены криптовалют\nСпецпроекты\nНовости\n'+headline+'\n'+fact+'\nМы используем файлы cookie для улучшения качества работы.'
        mid=self.material(text=source,title=headline)
        selected={**group(mid),'focus':[{'material_id':mid,'scope':source,'excerpt':source}]}
        materials=store.materials(self.db,[mid])
        result=short_release.extractive(selected,materials)
        self.assertIsNotNone(result)
        self.assertEqual(result['headline'],'📰 '+headline)
        self.assertEqual(result['lead'],fact)
        self.assertEqual(short_release.problems(result,materials,selected),[])
        short_release.verify(result,{'kind':'short_source','group_hash':store.sha(selected)},materials,selected)
        bad={**result,'lead':'Искусственный интеллект',
             'lead_evidence':[{'material_id':mid,'quote':'Искусственный интеллект'}]}
        with self.assertRaisesRegex(ValueError,'EDITION_SHORT_SOURCE_CHANGED'):
            short_release.verify(bad,{'kind':'short_source','group_hash':store.sha(selected)},materials,selected)

    def test_offline_label_guard_preserves_verbless_access_conditions(self):
        source='Банк запустил сервис переводов. Сервис работает в пяти сетях. Доступ только для квалифицированных инвесторов.'
        mid=self.material(text=source,title='Банк запустил сервис переводов')
        selected={**group(mid),'focus':[{'material_id':mid,'scope':source,'excerpt':source}]}
        result=short_release.extractive(selected,store.materials(self.db,[mid]))
        self.assertIsNotNone(result)
        self.assertIn('Доступ только для квалифицированных инвесторов.',[b['text'] for b in result['blocks']])

    def test_service_prose_and_labels_cannot_supply_offline_news(self):
        for source in ('Искусственный интеллект\nЦены криптовалют\nНовости',
                       'Мы используем файлы cookie для улучшения качества работы. Пользуясь сайтом, вы соглашаетесь с Политикой приватности.'):
            mid=self.material(text=source,title='Новости криптовалют')
            self.assertIsNone(short_release.extractive(group(mid),store.materials(self.db,[mid])))

    def test_model_headline_bold_markers_do_not_leak_into_telegram(self):
        from newsroom.edition.formatting import render
        candidate=copy.deepcopy(self.candidate)
        candidate['headline']='🏦 **Банк запустил сервис цифровых активов**'
        final=short_release.layout(candidate)
        markup,plain=render(final,store.materials(self.db,[self.mid]))
        self.assertEqual(final['headline'],'🏦 Банк запустил сервис цифровых активов')
        self.assertNotIn('**',markup)
        self.assertTrue(markup.startswith('<b>🏦 Банк'))
        self.assertEqual(final['headline_evidence'],candidate['headline_evidence'])
        self.assertEqual(candidate['headline'],'🏦 **Банк запустил сервис цифровых активов**')

    def test_failed_writer_uses_independent_short_stage_and_delivery_gate(self):
        job=self.job()
        with patch.object(model,'plan',return_value=({'groups':[group(self.mid)],'excluded':[]},receipt())) as planner, \
             patch.object(model,'draft',side_effect=TimeoutError()) as writer, \
             patch.object(model,'short_draft',return_value=(self.candidate,receipt())) as terminal, \
             patch.object(model,'short_check',return_value=(verdict(self.candidate),receipt())) as check:
            result=process(self.db,job,self.settings,send=self.send)
        self.assertEqual(result['state'],'PUBLISHED')
        for call in (planner,writer,terminal,check,self.send):call.assert_called_once()
        doc=self.db.execute('SELECT * FROM edition_documents').fetchone()
        gate(self.db,doc['document_id'],doc['rendered_text'])
        self.assertEqual(store.read(self.db.execute('SELECT review_json FROM edition_checks ORDER BY check_id DESC LIMIT 1').fetchone()[0],{})['release_mode'],'SHORT')

    def test_checker_can_return_corrected_final_text_without_another_writer(self):
        doc=self.document();bad=copy.deepcopy(self.candidate);bad['lead']='Банк запустил сервис во всех странах мира.'
        with patch.object(model,'short_draft',return_value=(bad,receipt())) as writer, \
             patch.object(model,'short_check',return_value=(verdict(self.candidate,corrections=['Убрано неподтверждённое расширение.']),receipt())) as check:
            short_release.prepare(self.db,doc,self.settings)
        writer.assert_called_once();check.assert_called_once()
        row=self.db.execute('SELECT * FROM edition_documents').fetchone()
        self.assertEqual(store.read(row['draft_json'],{})['lead'],self.candidate['lead'])
        gate(self.db,row['document_id'],row['rendered_text'])

    def test_transport_resume_reuses_saved_short_candidate(self):
        doc=self.document()
        with patch.object(short_release,'extractive',return_value=None), \
             patch.object(model,'short_draft',return_value=(self.candidate,receipt())) as writer, \
             patch.object(model,'short_check',side_effect=[TimeoutError(),(verdict(self.candidate),receipt())]) as check:
            short_release.prepare(self.db,doc,self.settings)
            self.db.execute("UPDATE edition_short_work SET retry_after='' ");self.db.commit()
            short_release.prepare(self.db,self.db.execute('SELECT * FROM edition_documents').fetchone(),self.settings)
        writer.assert_called_once();self.assertEqual(check.call_count,2)
        self.assertEqual(self.db.execute('SELECT state FROM edition_documents').fetchone()[0],'READY')

    def test_substantive_rejection_never_restarts_writer_or_checker(self):
        doc=self.document()
        with patch.object(short_release,'extractive',return_value=None), \
             patch.object(model,'short_draft',return_value=(self.candidate,receipt())) as writer, \
             patch.object(model,'short_check',return_value=(verdict(self.candidate,lead_supported=False),receipt())) as check:
            short_release.prepare(self.db,doc,self.settings)
            self.db.execute("UPDATE edition_short_work SET retry_after='' ");self.db.commit()
            short_release.prepare(self.db,self.db.execute('SELECT * FROM edition_documents').fetchone(),self.settings)
        writer.assert_called_once();check.assert_called_once()
        self.assertEqual(self.db.execute('SELECT error FROM edition_short_work').fetchone()[0],'SOURCE_ONLY')
        publish_document(self.db,doc['document_id'],self.config,send=self.send);self.send.assert_not_called()

    def test_final_rejection_stops_unchanged_automatic_replays(self):
        doc=self.document()
        with patch.object(short_release,'extractive',return_value=None) as source, \
             patch.object(model,'short_draft',return_value=(self.candidate,receipt())) as writer, \
             patch.object(model,'short_check',return_value=(verdict(self.candidate,lead_supported=False),receipt())) as checker:
            short_release.prepare(self.db,doc,self.settings)
            for _ in range(3):
                short_release.prepare(self.db,self.db.execute('SELECT * FROM edition_documents').fetchone(),self.settings)
        writer.assert_called_once();checker.assert_called_once();source.assert_called_once()
        work=self.db.execute('SELECT * FROM edition_short_work').fetchone()
        self.assertEqual(work['attempts'],1);self.assertIsNone(work['retry_after'])
        self.assertEqual(self.db.execute("SELECT count(*) FROM edition_events WHERE stage='short_error'").fetchone()[0],1)

    def test_negative_issue_overrides_positive_boolean(self):
        with self.assertRaisesRegex(ValueError,'MAIN_UNVERIFIED'):
            short_release.selected(self.candidate,verdict(self.candidate,issues=[{'field':'lead','block_index':-1,'reason':'План выдан за решение.'}]))

    def test_unverified_optional_block_is_removed_and_receipt_binds_final_text(self):
        doc=self.document();result=verdict(self.candidate,blocks_supported=[False])
        with patch.object(model,'short_draft',return_value=(self.candidate,receipt())),patch.object(model,'short_check',return_value=(result,receipt())):
            short_release.prepare(self.db,doc,self.settings)
        row=self.db.execute('SELECT * FROM edition_documents').fetchone();self.assertEqual(store.read(row['draft_json'],{})['blocks'],[])
        gate(self.db,row['document_id'],row['rendered_text'])
        self.db.execute("UPDATE edition_documents SET rendered_text=rendered_text||' выдумка'");self.db.commit()
        with self.assertRaises(DeliveryRejected):gate(self.db,row['document_id'],row['rendered_text']+' выдумка')

    def test_bad_plan_group_or_exclusion_does_not_destroy_valid_group(self):
        second=self.material(text='Компания Бета запустила криптокошелёк. Он поддерживает три сети.',title='Компания Бета запустила криптокошелёк')
        job=self.job();bad=group(second);bad['focus'][0]['excerpt']='Нет такого текста.'
        with patch.object(model,'plan',return_value=({'groups':[group(self.mid),bad],
             'excluded':[{'material_id':999,'reason':'Несуществующий материал'}]},receipt())):
            groups,excluded=plan_all(self.db,job,store.materials(self.db,[self.mid,second]),self.settings)
        self.assertEqual(len(groups),2);self.assertEqual(groups[0]['material_ids'],[self.mid])
        self.assertTrue(groups[1]['_short_required']);self.assertFalse(excluded)

    def test_fragment_ids_preserve_conditions_verbatim_and_reject_unknown_ids(self):
        material={'material_id':1,'content':'Банк планирует запустить криптосервис. Доступ откроют только после разрешения регулятора.'}
        indexed=fragments.index([material]);selection={'subject':'Банк','entities':['Банк'],'event_key':'Запуск',
            'material_ids':[1],'focus':[{'material_id':1,'fragment_ids':[x['id'] for x in indexed],'summary':'План'}],'lookup_query':''}
        expanded=fragments.expand({'groups':[selection],'excluded':[]},indexed)
        self.assertEqual([x['scope'] for x in expanded['groups'][0]['focus']],[x['text'] for x in indexed])
        selection['focus'][0]['fragment_ids']=['1:content:999']
        expanded=fragments.expand({'groups':[selection],'excluded':[]},indexed)
        self.assertFalse(expanded['groups']);self.assertEqual(len(expanded['fragment_errors']),1)

    def test_layout_fixes_actual_sandbox_failures_without_new_facts(self):
        candidate=copy.deepcopy(self.candidate)
        candidate['headline']='Компания Альфа запустила сервис 🏦'
        candidate['lead']='15 октября компания Альфа планирует открыть сервис. Доступ получат клиенты банка.'
        candidate['blocks'][0]['kind']='bullet';candidate['blocks'][0]['text']='➤ '+candidate['blocks'][0]['text']
        result=short_release.selected(candidate,verdict(candidate))
        self.assertEqual(result['headline'],'🏦 Компания Альфа запустила сервис')
        self.assertEqual(result['lead'],'Компания Альфа планирует открыть сервис 15 октября. Доступ получат клиенты банка.')
        self.assertFalse(result['blocks'][0]['text'].startswith('➤'))
        self.assertEqual(result['lead_evidence'],candidate['lead_evidence'])
        self.assertEqual(short_release.layout(result),result)

    def test_finished_or_unknown_delivery_is_never_prepared_again(self):
        doc=self.document()
        for state in ('PUBLISHED','UNKNOWN','SENDING','FILTERED','DUPLICATE'):
            self.db.execute('UPDATE edition_documents SET state=?',(state,));self.db.commit()
            with patch.object(model,'short_draft') as writer:
                short_release.prepare(self.db,doc,self.settings)
            writer.assert_not_called()

    def test_adjacent_selected_sentences_allow_one_exact_proof_without_neighbor_news(self):
        source={'material_id':1,'content':'Банк планирует криптосервис. Доступ откроют после разрешения. Другой бренд запустил игру.'}
        indexed=fragments.index([source])
        selection={'subject':'Банк','entities':['Банк'],'event_key':'Сервис','material_ids':[1],
            'focus':[{'material_id':1,'fragment_ids':[x['id'] for x in indexed[:2]],'summary':'План'}],'lookup_query':''}
        result=fragments.expand({'groups':[selection],'excluded':[]},indexed,[source])
        scopes=[f['scope'] for f in result['groups'][0]['focus']]
        self.assertEqual(scopes,['Банк планирует криптосервис. Доступ откроют после разрешения.'])
        self.assertNotIn('игру',scopes[0])

    def test_rollback_converts_only_preparation_without_delivery_intent(self):
        import importlib.util
        from pathlib import Path
        spec=importlib.util.spec_from_file_location('named_update',Path(__file__).resolve().parents[1]/'ops/short_release_update.py')
        update=importlib.util.module_from_spec(spec);spec.loader.exec_module(update)
        doc=self.document();now=store.stamp()
        self.db.execute("UPDATE edition_documents SET state='SHORT_PENDING'")
        for docid in ('unknown','sent','published'):
            self.db.execute('INSERT INTO edition_documents(document_id,job_id,group_json,materials_json,state,created_at,updated_at) VALUES(?,?,?,?,?,?,?)',
                (docid,doc['job_id'],doc['group_json'],doc['materials_json'],'PUBLISHED' if docid=='published' else 'SHORT_PENDING',now,now))
        for docid,status in (('unknown','UNKNOWN'),('sent','SENT')):
            self.db.execute('INSERT INTO publication_attempts(delivery_key,channel_id,text,content_hash,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?)',
                ('@test:edition:'+docid,'@test','Текст','hash',status,now,now))
        self.db.commit();path=Path(self.tmp.name)/'config.toml'
        path.write_text('[newsroom]\ndatabase = "'+self.database+'"\n')
        update.normalize_pending(path)
        states=dict(self.db.execute('SELECT document_id,state FROM edition_documents'))
        self.assertEqual(states,{'short-test':'INCOMPLETE','unknown':'SHORT_PENDING','sent':'SHORT_PENDING','published':'PUBLISHED'})
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM publication_attempts').fetchone()[0],2)

    def test_interrupted_save_reuses_both_writer_and_checker_receipts(self):
        doc=self.document();save=short_release.save
        with patch.object(short_release,'extractive',return_value=None), \
             patch.object(short_release,'save',side_effect=[TimeoutError(),None]) as saving, \
             patch.object(model,'short_draft',return_value=(self.candidate,receipt())) as writer, \
             patch.object(model,'short_check',return_value=(verdict(self.candidate),receipt())) as check:
            short_release.prepare(self.db,doc,self.settings)
            self.db.execute("UPDATE edition_short_work SET retry_after='' ");self.db.commit()
            short_release.prepare(self.db,self.db.execute('SELECT * FROM edition_documents').fetchone(),self.settings)
        writer.assert_called_once();check.assert_called_once();self.assertEqual(saving.call_count,2)

    def test_source_safety_net_does_not_turn_a_clipped_quote_into_a_fact(self):
        text='Иван Петров, директор криптокомпании Альфа, выступил экспертом в отраслевом обзоре о проверках криптотранзакций. «Регуляторы давно опасаются криптобирж. Их можно проверить при наличии инструментов».'
        mid=self.material(text=text,title='Комментарий о криптопроверках')
        materials=store.materials(self.db,[mid]);planned=group(mid)
        planned['focus']=[{'material_id':mid,'excerpt':text,'scope':text,'summary':'Мнение эксперта'}]
        result=short_release.extractive(planned,materials)
        self.assertIsNotNone(result);self.assertIn('Иван Петров',result['headline'])
        self.assertNotIn('Регуляторы',result['headline']);self.assertNotIn('«',result['lead'])
        self.assertFalse(short_release.problems(result,materials,planned))

    def test_one_complete_price_fact_can_be_released_without_repeating_headline(self):
        mid=self.material(text='Биткойн опустился до $80 000.',title='Биткойн опустился до $80 000.')
        planned=group(mid);planned['focus']=[]
        result=short_release.extractive(planned,store.materials(self.db,[mid]))
        self.assertEqual(result['headline'],'📰 Биткойн опустился')
        self.assertEqual(result['lead'],'Биткойн опустился до $80 000.')

    def test_policy_recovery_splits_capacity_failure_without_cross_source_focus(self):
        from newsroom.edition.worker import recover
        from newsroom.edition import fallback
        second=self.material(text='Компания Бета открыла криптосервис.',title='Компания Бета открыла криптосервис')
        doc=self.document();materials=store.materials(self.db,[self.mid,second])
        planned=group(self.mid);planned['material_ids']=[self.mid,second]
        planned['focus'].append({'material_id':second,'excerpt':'Компания Бета открыла криптосервис.','summary':'Сервис'})
        self.db.execute("UPDATE edition_jobs SET state='READY',bundle_hash='old-policy'")
        self.db.execute("UPDATE edition_documents SET state='READY',group_json=?,materials_json=?",(store.encoded(planned),store.encoded(materials)));self.db.commit()
        calls=[]
        def release(db,row,**options):
            selected=store.read(row['group_json'],{});calls.append(selected)
            if len(selected['material_ids'])>1:raise ValueError('EDITION_FALLBACK_NEEDS_SPLIT')
            self.assertEqual({f['material_id'] for f in selected['focus']},set(selected['material_ids']))
            db.execute("UPDATE edition_documents SET state='READY' WHERE document_id=?",(row['document_id'],));db.commit()
        with patch.object(fallback,'release',side_effect=release):recover(self.db,self.config)
        self.assertEqual([g['material_ids'] for g in calls],[[self.mid,second],[self.mid,second],[self.mid],[second]])
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM edition_documents').fetchone()[0],2)
        self.assertEqual(self.db.execute('SELECT bundle_hash FROM edition_jobs').fetchone()[0],model.bundle()[2])

    def test_source_proof_restores_punctuation_spacing_but_not_words_or_numbers(self):
        source={'material_id':1,'content':'Банк предложил комиссию 0,1% . Доступ не открыт .'}
        candidate=copy.deepcopy(self.candidate)
        candidate['headline_evidence']=[{'material_id':1,'quote':'Банк предложил комиссию 0,1%.'}]
        candidate['lead_evidence']=[{'material_id':1,'quote':'Доступ не открыт.'}]
        candidate['blocks']=[]
        result=short_release.align_evidence(candidate,[source],{'focus':[]})
        self.assertEqual(result['headline_evidence'][0]['quote'],'Банк предложил комиссию 0,1% .')
        self.assertEqual(result['lead_evidence'][0]['quote'],'Доступ не открыт .')
        candidate['headline_evidence'][0]['quote']='Банк предложил комиссию 1%.'
        candidate['lead_evidence'][0]['quote']='Доступ открыт.'
        self.assertEqual(short_release.align_evidence(candidate,[source],{'focus':[]}),candidate)

    def test_spacing_fix_recovers_saved_positive_check_without_new_model_calls(self):
        from edition_helpers import TEXT,BODY
        self.mid=self.material(text=TEXT.replace('банка.','банка .'))
        self.candidate=draft(self.mid);doc=self.document()
        with patch.object(short_release,'extractive',return_value=None), \
             patch.object(model,'short_draft',return_value=(self.candidate,receipt())) as writer, \
             patch.object(model,'short_check',return_value=(verdict(self.candidate),receipt())) as checker:
            with patch.object(short_release,'align_evidence',side_effect=lambda d,m,g:d):
                short_release.prepare(self.db,doc,self.settings)
            self.assertEqual(self.db.execute('SELECT error FROM edition_short_work').fetchone()[0],'SOURCE_ONLY')
            self.db.execute("UPDATE edition_short_work SET retry_after='' ");self.db.commit()
            short_release.prepare(self.db,self.db.execute('SELECT * FROM edition_documents').fetchone(),self.settings)
        writer.assert_called_once();checker.assert_called_once()
        row=self.db.execute('SELECT * FROM edition_documents').fetchone()
        self.assertEqual(row['state'],'READY')
        self.assertEqual(store.read(row['draft_json'],{})['blocks'][0]['text'],BODY)
        gate(self.db,row['document_id'],row['rendered_text'])

    def test_extractive_preserves_quoted_document_name_and_full_explanation(self):
        source='Законопроект «О цифровой валюте и цифровых правах», который должен определить правила работы крипторынка в России, доработали ко второму чтению.'
        mid=self.material(text=source);materials=store.materials(self.db,[mid])
        selected={**group(mid),'focus':[{'material_id':mid,'scope':source,'excerpt':source}]}
        result=short_release.extractive(selected,materials)
        self.assertIsNotNone(result)
        self.assertEqual(result['headline'],'📰 Законопроект «О цифровой валюте и цифровых правах» доработали ко второму чтению')
        self.assertEqual(result['lead'],source)
        self.assertEqual(short_release.problems(result,materials,selected),[])

    def test_extractive_does_not_treat_speech_as_document_title(self):
        for source in ('Политик сказал: «Законопроект приняли».',
                       'Законопроект «О криптовалютах» описали словами «принят окончательно».'):
            mid=self.material(text=source,title=source)
            selected={**group(mid),'focus':[{'material_id':mid,'scope':source,'excerpt':source}]}
            self.assertIsNone(short_release.extractive(selected,store.materials(self.db,[mid])))

    def test_oversized_single_sentence_advisory_enters_short_stage(self):
        from newsroom.edition import fallback
        doc=self.document();bad=copy.deepcopy(self.candidate)
        bad['lead']='Компания Альфа '+('планирует расширить доступ к переводам для клиентов банка ' * 5)+'в декабре.'
        self.db.execute('UPDATE edition_documents SET draft_json=? WHERE document_id=?',(store.encoded(bad),doc['document_id']));self.db.commit()
        with patch.object(model,'short_draft',return_value=(self.candidate,receipt())) as writer, \
             patch.object(model,'short_check',return_value=(verdict(self.candidate),receipt())) as checker:
            fallback.release(self.db,self.db.execute('SELECT * FROM edition_documents').fetchone(),settings=self.settings)
        writer.assert_called_once();checker.assert_called_once()
        row=self.db.execute('SELECT * FROM edition_documents').fetchone()
        self.assertEqual(store.read(row['draft_json'],{})['lead'],self.candidate['lead'])
        review=store.read(self.db.execute('SELECT review_json FROM edition_checks ORDER BY check_id DESC LIMIT 1').fetchone()[0],{})
        self.assertEqual(review['release_mode'],'SHORT');gate(self.db,row['document_id'],row['rendered_text'])

    def test_short_layout_never_chops_single_sentence_at_word_limit(self):
        candidate=copy.deepcopy(self.candidate)
        candidate['lead']='Компания Альфа '+('планирует расширить доступ к переводам для клиентов банка ' * 5)+'в декабре.'
        result=short_release.layout(candidate)
        self.assertEqual(result['lead'],candidate['lead'])
        self.assertEqual(len(result['blocks']),len(candidate['blocks']))

    def test_read_primary_source_is_indexed_and_checked_but_unread_metadata_is_not(self):
        from newsroom.edition.source_text import saved_fields
        from newsroom.edition.formatting import contains
        primary={'status':'READ','type':'OFFICIAL','url':'https://example.org/official',
                 'title':'Банк запустил цифровой сервис','content':'Банк запустил цифровой сервис. Доступ откроют в декабре.'}
        material={'material_id':1,'content':'Банк запустил цифровой сервис | Банк https://example.org/official',
                  'primary_source_json':store.encoded(primary)}
        self.assertTrue(contains(material,'Доступ откроют в декабре.'))
        self.assertTrue(any(f['field']=='primary_content' for f in fragments.index([material])))
        primary['status']='UNREADABLE';material['primary_source_json']=store.encoded(primary)
        self.assertFalse(contains(material,'Доступ откроют в декабре.'))
        self.assertNotIn('primary_content',saved_fields(material))

    def test_only_exact_selected_link_preview_expands_to_saved_primary_source(self):
        from newsroom.edition.source_text import expand_link_preview
        primary={'status':'READ','type':'OFFICIAL','url':'https://example.org/official',
                 'title':'Банк запустил цифровой сервис','content':'Банк запустил цифровой сервис. Доступ откроют в декабре.'}
        preview='Банк запустил цифровой сервис | Банк https://example.org/official'
        material={'material_id':1,'content':preview,'primary_source_json':store.encoded(primary)}
        selected={**group(1),'focus':[{'material_id':1,'scope':preview,'excerpt':preview}]}
        expanded=expand_link_preview(selected,[material])
        self.assertEqual(len(expanded['focus']),2)
        self.assertEqual(expanded['focus'][1]['scope'],primary['content'])
        self.assertEqual(expand_link_preview(expanded,[material]),expanded)
        for value in ('Другая новость. '+preview,preview+' Ещё одна новость.',
                      preview.replace('/official','/other')):
            unrelated={**selected,'focus':[{'material_id':1,'scope':value,'excerpt':value}]}
            self.assertEqual(expand_link_preview(unrelated,[material]),unrelated)

    def test_primary_adjacent_fragments_expand_using_their_original_field(self):
        primary={'status':'READ','type':'OFFICIAL','url':'https://example.org/official',
                 'title':'Официальный документ','content':'Банк запустил цифровой сервис. Доступ откроют в декабре.'}
        material={'material_id':1,'content':'Анонс документа','primary_source_json':store.encoded(primary)}
        numbered=fragments.index([material]);ids=[f['id'] for f in numbered if f['field']=='primary_content']
        selected={**group(1),'focus':[{'material_id':1,'fragment_ids':ids,'summary':'Сервис банка'}]}
        result=fragments.expand({'groups':[selected]},numbered,[material])
        self.assertEqual(result['fragment_errors'],[])
        self.assertEqual(result['groups'][0]['focus'][0]['scope'],primary['content'])

    def test_evidence_from_saved_primary_adds_its_own_source_link(self):
        from newsroom.edition.formatting import source_list,render
        primary={'status':'READ','type':'OFFICIAL','url':'https://example.org/official',
                 'publisher':'Банк','title':'Анонс сервиса','content':'Доступ откроют в декабре.'}
        material={'material_id':1,'url':'https://example.org/announcement','source_name':'Канал',
                  'content':'Анонс сервиса','primary_source_json':store.encoded(primary)}
        candidate=copy.deepcopy(self.candidate);candidate['blocks']=[]
        candidate['headline_evidence']=[{'material_id':1,'quote':'Доступ откроют в декабре.'}]
        candidate['lead_evidence']=copy.deepcopy(candidate['headline_evidence'])
        self.assertEqual(source_list(candidate,[material]),[('Канал',material['url']),('Банк',primary['url'])])
        rendered,_=render(candidate,[material])
        self.assertIn('href="https://example.org/official">Банк</a>',rendered)
        primary['status']='UNREADABLE';material['primary_source_json']=store.encoded(primary)
        self.assertEqual(source_list(candidate,[material]),[('Канал',material['url'])])

    def test_single_digest_fact_keeps_recipient_in_lead_without_other_events(self):
        source=('Курсы криптовалют изменились. 🔸 💲 Tether создает стейблкоин для властей Казахстана. '
                '🔸 💰 Владелец биткоинов перевел монеты.')
        mid=self.material(text=source,title=source);materials=store.materials(self.db,[mid])
        selected={**group(mid),'entities':['Tether'],'focus':[{'material_id':mid,'scope':source,'excerpt':source}]}
        result=short_release.extractive(selected,materials)
        self.assertIsNotNone(result)
        self.assertEqual(result['headline'],'📰 Tether создает стейблкоин')
        self.assertEqual(result['lead'],'Tether создает стейблкоин для властей Казахстана.')
        self.assertEqual(result['blocks'],[])
        self.assertEqual(short_release.problems(result,materials,selected),[])

    def test_last_delivery_preserves_excluded_material_reasons_on_resume(self):
        second=self.material(text='Непрофильная новость о погоде.',title='Погода')
        doc=self.document();excluded=[{'kind':'out_of_scope','material_id':second,'reason':'Нет связи с криптовалютами.'}]
        with patch.object(model,'short_draft',return_value=(self.candidate,receipt())),patch.object(model,'short_check',return_value=(verdict(self.candidate),receipt())):
            short_release.prepare(self.db,doc,self.settings)
        self.db.execute("UPDATE edition_jobs SET state='READY',reasons_json=? WHERE job_id=?",(store.encoded(excluded),doc['job_id']));self.db.commit()
        publish_document(self.db,doc['document_id'],self.config,send=self.send)
        job=self.db.execute('SELECT state,reasons_json FROM edition_jobs WHERE job_id=?',(doc['job_id'],)).fetchone()
        self.assertEqual(job['state'],'PUBLISHED')
        self.assertEqual(store.read(job['reasons_json'],[]),excluded)

    def test_bitcoin_decorative_symbol_is_repaired_without_touching_amounts(self):
        candidate=copy.deepcopy(self.candidate)
        candidate['headline']='₿ Bitcoin восстановился к $83 000 после теста $80 400'
        result=short_release.layout(candidate)
        self.assertEqual(result['headline'],'🪙 Bitcoin восстановился к $83 000 после теста $80 400')
        self.assertEqual(result['lead'],candidate['lead'])
        candidate['headline']='₿ 2 млн перевели на биржу'
        self.assertEqual(short_release.layout(candidate)['headline'],candidate['headline'])

    def test_bad_optional_example_is_omitted_but_conditions_and_material_coverage_survive(self):
        materials=store.materials(self.db,[self.mid]);selected=group(self.mid)
        candidate=copy.deepcopy(self.candidate)
        candidate['blocks'][0]['evidence'][0]['quote']='Такого предложения в источнике нет.'
        candidate['blocks'][0]['text']='В качестве примера показали обычный перевод.'
        result,omitted=short_release.finalized(candidate,verdict(candidate),materials,selected)
        self.assertEqual(result['blocks'],[]);self.assertEqual(len(omitted),1)
        candidate['blocks'][0]['text']='Перевод разрешён только квалифицированным инвесторам.'
        result,omitted=short_release.finalized(candidate,verdict(candidate),materials,selected)
        self.assertEqual(len(result['blocks']),1);self.assertEqual(omitted,[])
        second=self.material(text='Компания Бета запустила криптокошелёк для трёх сетей.');materials=store.materials(self.db,[self.mid,second])
        selected['material_ids'].append(second);candidate['blocks'][0]['text']='Другой материал сообщает дополнительную новость.'
        candidate['blocks'][0]['evidence'][0]['material_id']=second
        result,omitted=short_release.finalized(candidate,verdict(candidate),materials,selected)
        self.assertEqual(len(result['blocks']),1);self.assertEqual(omitted,[])
        result['blocks']=[]
        self.assertIn('coverage',[i['code'] for i in short_release.problems(result,materials,selected)])

    def test_pruned_optional_proof_is_bound_to_the_final_delivery_gate(self):
        candidate=copy.deepcopy(self.candidate)
        candidate['blocks'][0]['text']='В качестве примера показали обычный перевод.'
        candidate['blocks'][0]['evidence'][0]['quote']='Нет такого фрагмента.'
        doc=self.document()
        with patch.object(model,'short_draft',return_value=(candidate,receipt())),patch.object(model,'short_check',return_value=(verdict(candidate),receipt())):
            short_release.prepare(self.db,doc,self.settings)
        row=self.db.execute('SELECT * FROM edition_documents').fetchone()
        self.assertEqual(row['state'],'READY');self.assertEqual(store.read(row['draft_json'],{})['blocks'],[])
        gate(self.db,row['document_id'],row['rendered_text'])
        self.assertEqual(self.db.execute("SELECT count(*) FROM edition_events WHERE stage='short_optional_omitted'").fetchone()[0],1)
