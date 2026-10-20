from unittest.mock import patch
from edition_helpers import EditionCase,LEAD,receipt
from newsroom.edition import model,store
from newsroom.edition.semantic_filter import screen


class SemanticIsolationTests(EditionCase):
    def test_bad_neighbour_preserves_valid_allow_and_reject_and_cache(self):
        good=self.material()
        rejected=self.material(text='Непрофильный материал.',url='https://example.org/rejected')
        bad=self.material(text='Третий исходник.',url='https://example.org/bad')
        job=self.job();materials=store.materials(self.db,[good,rejected,bad])
        entries=[{'material_id':good,'decision':'allow','reason':'Профильная новость.',
                  'evidence':[{'material_id':good,'quote':LEAD}]},
                 {'material_id':rejected,'decision':'reject','reason':'Непрофильная новость.',
                  'evidence':[{'material_id':rejected,'quote':'Непрофильный материал.'}]},
                 {'material_id':bad,'decision':'reject','reason':'Отказ с неверной цитатой.',
                  'evidence':[{'material_id':bad,'quote':'Выдуманная цитата.'}]}]
        with patch.object(model,'semantic_screen',return_value=({'decisions':entries},receipt())):
            allowed,excluded=screen(self.db,job,materials,self.settings)
        self.assertEqual([m['material_id'] for m in allowed],[good,bad])
        self.assertEqual([m['material_id'] for m in excluded],[rejected])
        rows={r['material_id']:r for r in self.db.execute('SELECT * FROM edition_semantic_decisions')}
        self.assertEqual(store.read(rows[good]['result_json'])['decision'],'allow')
        self.assertEqual(store.read(rows[rejected]['result_json'])['decision'],'reject')
        self.assertEqual(store.read(rows[bad]['receipt_json'])['kind'],'semantic_unavailable')
        event=store.read(self.db.execute("SELECT payload_json FROM edition_events WHERE stage='semantic_error'").fetchone()[0])
        self.assertEqual(event['material_ids'],[bad])
        def recheck(batch,settings):
            self.assertEqual([m['material_id'] for m in batch],[bad])
            return {'decisions':[{'material_id':bad,'decision':'allow','reason':'Повторная проверка.', 'evidence':[]}]},receipt()
        with patch.object(model,'semantic_screen',side_effect=recheck) as call:
            screen(self.db,job,materials,self.settings)
        call.assert_called_once()

    def test_missing_coverage_keeps_entire_response_unconfirmed(self):
        first=self.material();second=self.material(text='Другой материал.')
        job=self.job()
        with patch.object(model,'semantic_screen',return_value=({'decisions':[]},receipt())):
            screen(self.db,job,store.materials(self.db,[first,second]),self.settings)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM edition_semantic_decisions WHERE receipt_json LIKE '%semantic_unavailable%'").fetchone()[0],2)
