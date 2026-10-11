import copy
from unittest.mock import Mock,patch
from edition_helpers import EditionCase,draft,group,receipt,review_result,BODY
from newsroom.edition import model,store
from newsroom.edition.layout_review import apply
from newsroom.edition.layout_metrics import summary
from newsroom.edition.pipeline import process
from newsroom.edition.publication import gate


class LayoutReviewTests(EditionCase):
    def test_split_preserves_every_word_number_and_evidence(self):
        original=draft();original['blocks'][0]['text']=('Условия действуют до 31 декабря при комиссии 0,1%. '*6).strip()
        fixed=apply(original)
        self.assertEqual(' '.join(b['text'] for b in fixed['blocks']),original['blocks'][0]['text'])
        self.assertTrue(all(len(b['text'])<=210 for b in fixed['blocks']))
        self.assertTrue(all(b['evidence']==original['blocks'][0]['evidence'] for b in fixed['blocks']))
        self.assertEqual(len(original['blocks']),1)

    def test_quote_and_unbreakable_token_are_not_rewritten(self):
        original=draft();original['blocks'][0].update(text='«'+('слово '*60)+'» — Автор',quote_text='слово '*60,quote_author='Автор',kind='quote')
        self.assertEqual(apply(original)['blocks'],original['blocks'])
        original['blocks'][0].update(text='x'*220,quote_text='',quote_author='',kind='paragraph')
        self.assertEqual(apply(original)['blocks'],original['blocks'])

    def test_lead_keeps_full_first_sentence_and_moves_details_with_evidence(self):
        original=draft();original['lead']='Банк запустил сервис. '+'Подробности доступны клиентам. '*8
        fixed=apply(original)
        self.assertEqual(fixed['lead'],'Банк запустил сервис.')
        self.assertEqual(' '.join([fixed['lead'],*(b['text'] for b in fixed['blocks'][:-1])]),' '.join(original['lead'].split()))
        self.assertTrue(all(b['evidence']==original['lead_evidence'] for b in fixed['blocks'][:-1]))

    def test_matched_pipeline_avoids_two_calls_without_losing_final_admission(self):
        from edition_helpers import TEXT
        def run(enabled):
            fixture=EditionCase();fixture.setUp()
            try:
                mid=fixture.material(text=TEXT+' '+BODY+' '+BODY+' '+BODY)
                job=fixture.job();bad=draft(mid);bad['blocks'][0]['text']=' '.join([BODY]*3)
                fixture.config['editorial']['layout_review_enabled']=enabled
                with patch.object(model,'plan',return_value=({'groups':[group(mid)],'excluded':[]},receipt())),patch.object(model,'draft',side_effect=[(bad,receipt()),(draft(mid),receipt())]) as writer,patch.object(model,'check',side_effect=[(review_result(),receipt())]*2) as checker:
                    process(fixture.db,job,fixture.settings,publish=False)
                doc=fixture.db.execute('SELECT * FROM edition_documents WHERE job_id=?',(job,)).fetchone()
                self.assertEqual(doc['state'],'READY')
                gate(fixture.db,doc['document_id'],doc['rendered_text'])
                return writer.call_count,checker.call_count,dict(doc),summary(fixture.db)
            finally:fixture.doCleanups()
        before=run(False);after=run(True)
        self.assertEqual(before[:2],(2,2));self.assertEqual(after[:2],(1,1))
        self.assertEqual(after[2]['repairs'],0)
        self.assertEqual(after[3]['with_layout']['avoided_format_returns'],1)
        self.assertIsNone(after[3]['with_layout']['median_usd'])

    def test_factual_objection_still_uses_editor_repair(self):
        mid=self.material();job=self.job();candidate=draft(mid)
        issue={'code':'status','post_fragment':candidate['lead'],'source_fragment':candidate['lead'],'material_id':mid,'reason':'Статус требует доработки','main_fact':True}
        with patch.object(model,'plan',return_value=({'groups':[group(mid)],'excluded':[]},receipt())),patch.object(model,'draft',side_effect=[(candidate,receipt())]*2) as writer,patch.object(model,'check',side_effect=[(review_result(False,[issue]),receipt()),(review_result(),receipt())]):
            process(self.db,job,self.settings,publish=False)
        self.assertEqual(writer.call_count,2)
        self.assertEqual(self.db.execute('SELECT repairs FROM edition_documents WHERE job_id=?',(job,)).fetchone()[0],1)

    def test_ai_receipt_records_time_usage_and_unknown_price(self):
        import json
        response={'id':'sample-response','model':'unpriced-test-model','status':'completed',
                  'usage':{'input_tokens':120,'input_tokens_details':{'cached_tokens':20},'output_tokens':30},
                  'output':[{'content':[{'type':'output_text','text':json.dumps({'ok':True})}]}]}
        with patch.object(model,'request_response',return_value=response):
            value,proof=model.ask('test',model.obj({'ok':model.B}),'',{},self.settings)
        self.assertEqual(proof['usage'],response['usage'])
        self.assertGreaterEqual(proof['elapsed_seconds'],0)
        self.assertIsNone(proof['pricing']['total_usd'])

    def test_duplicate_marker_and_emoji_are_fixed_without_changing_claim(self):
        original=draft();original['headline']='Банк запустил сервис 🏦'
        original['blocks'][0].update(kind='bullet',text='➤ ➤ '+BODY)
        fixed=apply(original)
        self.assertEqual(fixed['headline'],'🏦 Банк запустил сервис')
        self.assertEqual(fixed['blocks'][0]['text'],BODY)
        self.assertEqual(fixed['blocks'][0]['evidence'],original['blocks'][0]['evidence'])

    def test_long_single_sentence_body_stays_for_editor_repair(self):
        original=draft()
        original['blocks'][0]['text']='Условие '+('переводов для клиентов банка ' * 9)+'действует только при наличии экспертизы и инструментов.'
        fixed=apply(original)
        self.assertEqual(fixed['blocks'],original['blocks'])
