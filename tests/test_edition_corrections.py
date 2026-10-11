from unittest.mock import Mock, patch
from edition_helpers import EditionCase, draft, group, receipt, review_result
from newsroom.edition import model
from newsroom.edition.pipeline import process
from newsroom.edition.corrections import correct
from newsroom.delivery import TelegramReceipt, DeliveryUncertain
from newsroom.runtime import BudgetDeferred


class CorrectionsTests(EditionCase):
    def setUp(self):
        super().setUp()
        self.mid = self.material()
        with patch.object(model, 'plan', return_value=({'groups':[group(self.mid)], 'excluded':[]}, receipt())), patch.object(model, 'draft', return_value=(draft(self.mid),receipt())), patch.object(model,'check',return_value=(review_result(),receipt())):
            process(self.db,self.job(),self.settings,send=Mock(return_value=TelegramReceipt({'message_id':481})))
        self.config['ai'] = self.settings
        self.original = self.db.execute('SELECT text FROM posts').fetchone()[0]

    def run_correction(self, send, checker=None):
        with patch.object(model,'plan',return_value=({'groups':[group(self.mid)],'excluded':[]},receipt())), patch.object(model,'draft',return_value=(draft(self.mid),receipt())), patch.object(model,'check',side_effect=checker,return_value=(review_result(),receipt())):
            return correct(self.db,self.config,481,send=send)

    def test_edits_same_message_preserves_history_and_receipt(self):
        send=Mock(return_value={'message_id':481})
        self.assertEqual(self.run_correction(send)['status'],'APPLIED')
        self.assertEqual(send.call_args.args[0]['message_id'],481)
        row=self.db.execute('SELECT * FROM edition_corrections').fetchone()
        self.assertEqual(row['original_text'],self.original)
        self.assertEqual(row['status'],'APPLIED')
        self.assertEqual(self.db.execute('SELECT version FROM posts').fetchone()[0],2)

    def test_unavailable_checker_keeps_original_and_does_not_edit(self):
        send=Mock()
        with self.assertRaises(BudgetDeferred):self.run_correction(send,BudgetDeferred())
        send.assert_not_called()
        self.assertEqual(self.db.execute('SELECT text FROM posts').fetchone()[0],self.original)
        self.assertEqual(self.db.execute('SELECT status FROM edition_corrections').fetchone()[0],'WAITING')

    def test_unknown_result_blocks_blind_retry(self):
        send=Mock(side_effect=DeliveryUncertain())
        with self.assertRaises(DeliveryUncertain):self.run_correction(send)
        with self.assertRaisesRegex(ValueError,'CORRECTION_DELIVERY_UNKNOWN'):self.run_correction(send)
        self.assertEqual(send.call_count,1)

    def test_owner_stop_blocks_correction(self):
        self.config['newsroom']['agent_enabled']=False
        send=Mock()
        from newsroom.agent_control import AgentDisabled
        with self.assertRaises(AgentDisabled):self.run_correction(send)
        send.assert_not_called()

    def test_owner_empty_as_dot_uses_grounded_filter_and_skips_final_review(self):
        from newsroom.edition import store
        d = self.db.execute('SELECT * FROM edition_documents').fetchone()
        material = store.read(d['materials_json'], [])[0]
        focus = store.read(d['group_json'], {})['focus'][0]
        quote = focus.get('scope') or focus['excerpt']
        entry = {'material_id': material['material_id'], 'decision': 'reject',
                 'reason': 'Нет конкретной значимости для РФ/СНГ.', 'rule_code': 'audience_relevance',
                 'evidence': [{'material_id': material['material_id'], 'quote': quote}]}
        send = Mock(return_value={'message_id': 481})
        with patch.object(model, 'semantic_screen', return_value=({'decisions': [entry]}, receipt())), patch.object(model, 'check') as checker:
            self.assertEqual(correct(self.db, self.config, 481, send=send, empty_as_dot=True)['status'], 'APPLIED')
        self.assertEqual(send.call_args.args[0]['text'], '.')
        checker.assert_not_called()
        self.assertEqual(self.db.execute('SELECT text FROM posts').fetchone()[0], '.')
        self.assertEqual(self.db.execute('SELECT original_text FROM edition_corrections').fetchone()[0], self.original)
