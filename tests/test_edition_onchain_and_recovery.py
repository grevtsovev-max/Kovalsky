import copy
from unittest.mock import Mock,patch
from edition_helpers import EditionCase,draft,group,receipt,review_result,TEXT,LEAD
from newsroom.edition import model,store
from newsroom.edition.pipeline import process
from newsroom.edition.semantic_filter import screen
from newsroom.edition.fallback import source_draft
from newsroom.edition.formatting import render,validate
from newsroom.edition.publication import publish_document
from newsroom.edition.worker import recover
from newsroom.delivery import DeliveryUncertain,TelegramReceipt


class OnchainAndRecoveryTests(EditionCase):
    def rejected(self,mid,quote):
        return {'decisions':[{'material_id':mid,'decision':'reject','reason':'Наблюдение за кошельками без самостоятельного события.',
                             'evidence':[{'material_id':mid,'quote':quote}]}]},receipt()

    def test_wallet_and_whale_rejection_happens_before_planner_writer_and_delivery(self):
        for content in ('Около 100 BTC впервые переместили за 16 лет.',
                        'Киты двигают рынок: кит открыл лонг на ETH, MARA предположительно продала BTC.'):
            with self.subTest(content=content):
                mid=self.material(text=content,title=content);job=self.job()
                with patch.object(model,'semantic_screen',return_value=self.rejected(mid,content)),patch.object(model,'plan') as planner,patch.object(model,'draft') as writer:
                    send=Mock();result=process(self.db,job,self.settings,send=send)
                self.assertEqual(result['state'],'FILTERED');planner.assert_not_called();writer.assert_not_called();send.assert_not_called()
                saved=store.read(self.db.execute('SELECT result_json FROM edition_semantic_decisions WHERE material_id=?',(mid,)).fetchone()[0],{})
                self.assertEqual(saved['decision'],'reject')

    def test_rejected_material_is_not_resurrected_after_restart_or_planner_failure(self):
        content='Спящий кошелёк перевёл 100 BTC на два адреса.'
        mid=self.material(text=content);job=self.job()
        with patch.object(model,'semantic_screen',return_value=self.rejected(mid,content)):
            allowed,excluded=screen(self.db,job,store.materials(self.db,[mid]),self.settings)
        self.assertEqual(allowed,[])
        recover(self.db,self.config)
        self.assertEqual(self.db.execute('SELECT state FROM edition_jobs WHERE job_id=?',(job,)).fetchone()[0],'FILTERED')
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM edition_documents').fetchone()[0],0)
        with patch.object(model,'semantic_screen') as classify:
            screen(self.db,job,store.materials(self.db,[mid]),self.settings)
        classify.assert_not_called()

    def test_unproven_rejection_is_not_allowed_to_close_material(self):
        mid=self.material();job=self.job()
        with patch.object(model,'semantic_screen',return_value=self.rejected(mid,'Фрагмент, которого нет в источнике')):
            allowed,excluded=screen(self.db,job,store.materials(self.db,[mid]),self.settings)
        self.assertEqual([m['material_id'] for m in allowed],[mid]);self.assertEqual(excluded,[])
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM edition_events WHERE stage='semantic_error'").fetchone()[0],1)

    def test_restart_before_semantic_admission_resumes_filter_without_source_release(self):
        content='Спящий кошелёк перевёл 100 BTC.'
        mid=self.material(text=content);job=store.start(self.db,model.bundle()[2])
        recover(self.db,self.config)
        self.assertEqual(self.db.execute('SELECT state FROM edition_jobs WHERE job_id=?',(job,)).fetchone()[0],'SCREENING')
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM edition_documents').fetchone()[0],0)
        from newsroom.edition.worker import run_once
        with patch.object(model,'semantic_screen',return_value=self.rejected(mid,content)),patch.object(model,'plan') as planner:
            send=Mock();result=run_once(self.config,send=send)
        self.assertEqual(result['state'],'FILTERED');planner.assert_not_called();send.assert_not_called()

    def test_crash_after_saved_rejection_before_job_update_does_not_publish_source(self):
        content='Кошелёк перевёл 100 BTC на другой адрес.'
        mid=self.material(text=content);job=self.job();entry=self.rejected(mid,content)[0]['decisions'][0]
        self.db.execute('INSERT INTO edition_semantic_decisions VALUES(?,?,?,?,?)',(mid,model.bundle()[2],store.encoded(entry),store.encoded(receipt()),store.stamp()));self.db.commit()
        recover(self.db,self.config)
        self.assertEqual(self.db.execute('SELECT state FROM edition_jobs WHERE job_id=?',(job,)).fetchone()[0],'FILTERED')
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM edition_documents').fetchone()[0],0)

    def test_mixed_material_fallback_uses_only_admitted_event(self):
        good='Биржа вернула украденные 100 BTC клиентам.'
        noise='Кит переместил 200 BTC на неизвестный адрес.'
        mid=self.material(text=good+' '+noise,title='Сводка');job=self.job()
        result={'decisions':[{'material_id':mid,'decision':'allow','reason':'Подтверждённый возврат украденного.',
                             'evidence':[{'material_id':mid,'quote':good}]}]}
        send=Mock(return_value=TelegramReceipt({'message_id':700}))
        with patch.object(model,'semantic_screen',return_value=(result,receipt())),patch.object(model,'plan',side_effect=TimeoutError()):
            process(self.db,job,self.settings,send=send)
        send.assert_called_once();self.assertIn(good,send.call_args.args[1]);self.assertNotIn(noise,send.call_args.args[1])

    def test_excluded_digest_neighbour_cannot_revoke_an_admitted_event(self):
        mid=self.material();job=self.job()
        plan={'groups':[group(mid)],'excluded':[{'material_id':mid,'kind':'out_of_scope','reason':'Соседний пункт о переводе кита исключён.'}]}
        with patch.object(model,'plan',return_value=(plan,receipt())),patch.object(model,'draft',side_effect=TimeoutError()):
            result=process(self.db,job,self.settings,publish=False)
        self.assertEqual(result['state'],'READY')
        self.assertEqual(store.read(result['reasons_json'],[])[0]['kind'],'event_out_of_scope')

    def two_groups(self):
        a=self.material();b=self.material(text=TEXT+' Новое условие.',title='Другой проект')
        ga=group(a);gb=group(b);gb['entities']=['Другой проект'];gb['event_key']='Другое событие';gb['subject']='Другой проект'
        return a,b,{'groups':[ga,gb],'excluded':[]}

    def test_checker_failure_is_isolated_and_next_document_is_checked(self):
        a,b,plan=self.two_groups();job=self.job();send=Mock(return_value=TelegramReceipt({'message_id':701}))
        with patch.object(model,'plan',return_value=(plan,receipt())),patch.object(model,'draft',side_effect=[(draft(a),receipt()),(draft(b),receipt())]) as writer,patch.object(model,'check',side_effect=[TimeoutError(),(review_result(),receipt())]) as checker:
            result=process(self.db,job,self.settings,send=send)
        self.assertEqual(result['state'],'PUBLISHED');self.assertEqual(send.call_count,2);self.assertEqual(writer.call_count,2);self.assertEqual(checker.call_count,2)
        modes=[store.read(r[0],{}).get('release_mode','CHECKED') for r in self.db.execute('SELECT review_json FROM edition_checks ORDER BY check_id')]
        self.assertEqual(modes,['SOURCE','CHECKED'])

    def test_writer_failure_does_not_stop_other_drafts(self):
        a,b,plan=self.two_groups();job=self.job()
        with patch.object(model,'plan',return_value=(plan,receipt())),patch.object(model,'draft',side_effect=[TimeoutError(),(draft(b),receipt())]) as writer,patch.object(model,'check',return_value=(review_result(),receipt())) as checker:
            result=process(self.db,job,self.settings,publish=False)
        self.assertEqual(result['state'],'READY');self.assertEqual(writer.call_count,2);checker.assert_called_once()

    def four_fallbacks(self,send):
        mid=self.material();job=self.job();groups=[]
        for n in range(4):
            g=group(mid);g.update(subject='Сюжет '+str(n),entities=['Участник '+str(n)],event_key='Событие '+str(n));groups.append(g)
        with patch.object(model,'plan',return_value=({'groups':groups,'excluded':[]},receipt())),patch.object(model,'draft',side_effect=TimeoutError()):
            result=process(self.db,job,self.settings,send=send)
        return job,result

    def test_four_identical_source_fallbacks_have_one_delivery_and_durable_coverage(self):
        send=Mock(return_value=TelegramReceipt({'message_id':702}));job,result=self.four_fallbacks(send)
        send.assert_called_once();self.assertEqual(result['state'],'PUBLISHED')
        states=[r[0] for r in self.db.execute('SELECT state FROM edition_documents ORDER BY created_at')]
        self.assertEqual(states,['PUBLISHED','COVERED','COVERED','COVERED'])
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM edition_source_coverage').fetchone()[0],3)
        recover(self.db,self.config)
        for r in self.db.execute('SELECT document_id FROM edition_documents').fetchall():
            self.assertEqual(publish_document(self.db,r[0],self.config,send=send),'702')
        send.assert_called_once()

    def test_unknown_delivery_cannot_be_repeated_via_identical_fallback(self):
        send=Mock(side_effect=DeliveryUncertain('UNKNOWN'));job,result=self.four_fallbacks(send)
        send.assert_called_once();self.assertEqual(result['state'],'INCOMPLETE')
        self.assertEqual(sorted(store.document_states(self.db,job)),['UNKNOWN']*4)
        recover(self.db,self.config)
        for r in self.db.execute('SELECT document_id FROM edition_documents').fetchall():
            self.assertIsNone(publish_document(self.db,r[0],self.config,send=send))
        send.assert_called_once()

    def test_different_story_fragments_from_one_article_still_have_separate_releases(self):
        a='Биржа открыла переводы BTC.';b='Регулятор утвердил правила выпуска ЦФА.'
        mid=self.material(text=a+' '+b);job=self.job();groups=[]
        for n,scope in enumerate((a,b)):
            g=group(mid);g.update(subject=scope,entities=['Участник '+str(n)],event_key=str(n),focus=[{'material_id':mid,'excerpt':scope,'scope':scope,'summary':scope}]);groups.append(g)
        send=Mock(return_value=TelegramReceipt({'message_id':703}))
        with patch.object(model,'plan',return_value=({'groups':groups,'excluded':[]},receipt())),patch.object(model,'draft',side_effect=TimeoutError()):
            result=process(self.db,job,self.settings,send=send)
        self.assertEqual(result['state'],'PUBLISHED');self.assertEqual(send.call_count,2)
        self.assertNotEqual(send.call_args_list[0].args[1],send.call_args_list[1].args[1])

    def test_original_contradictory_assessment_is_repaired_once_without_replacing_other_posts(self):
        a,b,plan=self.two_groups();job=self.job();out=draft(a)
        issue={'code':'facts','post_fragment':LEAD,'source_fragment':LEAD,'material_id':a,'reason':'Уточнить участника.','main_fact':False}
        raw=review_result(False,[issue]);raw['assessments']['lead']={'passed':False,'explanation':'В лиде та же неточность участника.'}
        with patch.object(model,'plan',return_value=(plan,receipt())),patch.object(model,'draft',side_effect=[(out,receipt()),(draft(b),receipt()),(out,receipt())]) as writer,patch.object(model,'check',side_effect=[(raw,receipt()),(review_result(),receipt()),(review_result(),receipt())]):
            result=process(self.db,job,self.settings,publish=False)
        self.assertEqual(result['state'],'READY');self.assertEqual(writer.call_count,3)
        self.assertEqual({i['code'] for i in writer.call_args.kwargs['feedback']},{'facts','lead'})
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM edition_events WHERE stage='release'").fetchone()[0],0)
        event=store.read(self.db.execute("SELECT payload_json FROM edition_events WHERE stage='review_response' ORDER BY event_id LIMIT 1").fetchone()[0],{})
        self.assertEqual(event['verdict'],raw)

    def test_missing_factual_objection_cannot_be_normalized_into_approval(self):
        mid=self.material();raw=review_result();raw['assessments']['facts']['passed']=False
        with self.assertRaises(model.AIResponseError):model.reconcile_review(raw,draft(mid),store.materials(self.db,[mid]))

    def test_fallback_strips_middle_promotions_but_keeps_following_news_and_marks_excerpt(self):
        content='Биржа открыла переводы BTC. Хотите еще эксклюзивных новостей? Подписывайтесь на наш канал! Комиссия составляет 0,1%. The post Новость appeared first on Publisher.'
        mid=self.material(text=content,title='Биржа открыла переводы BTC');materials=store.materials(self.db,[mid])
        output=source_draft(group(mid),materials);markup,plain=render(output,materials)
        self.assertNotIn('Подписывайтесь',plain);self.assertNotIn('Хотите',plain);self.assertNotIn('The post',plain)
        self.assertIn('Комиссия составляет 0,1%.',plain);self.assertIn('…',plain)
        self.assertTrue(all(len(b['text'])<=210 for b in output['blocks']));self.assertLessEqual(len(output['lead']),210)
        self.assertFalse({i['code'] for i in validate(output,materials)}&{'paragraph','evidence','length','sources','headline_length','headline_emoji'})
