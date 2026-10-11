from unittest.mock import Mock, patch
from edition_helpers import EditionCase, receipt
from newsroom.edition import model, store
from newsroom.edition.pipeline import process
from newsroom.edition.worker import recover


class AudienceRelevanceTests(EditionCase):
    def test_foreign_funding_rejection_survives_restart_without_fallback(self):
        text = 'Soda Labs привлекла $3 млн на инфраструктуру приватности в блокчейне.'
        mid = self.material(text=text, title=text)
        job = self.job()
        entry = {'material_id': mid, 'decision': 'reject', 'rule_code': 'audience_relevance',
                 'reason': 'Раунд зарубежного проекта; конкретное изменение для рынка и аудитории РФ/СНГ не установлено.',
                 'evidence': [{'material_id': mid, 'quote': text}]}
        with patch.object(model, 'semantic_screen', return_value=({'decisions': [entry]}, receipt())), patch.object(model, 'plan') as planner:
            send = Mock()
            result = process(self.db, job, self.settings, send=send)
        self.assertEqual(result['state'], 'FILTERED')
        planner.assert_not_called()
        send.assert_not_called()
        self.assertEqual(store.read(result['reasons_json'], [])[0]['code'], 'audience_relevance')
        recover(self.db, self.config)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM edition_documents').fetchone()[0], 0)

    def test_audience_rule_is_disabled_for_both_final_checks(self):
        for role, original_schema in [('checker', model.CHECK), ('short_checker', model.SHORT_CHECK)]:
            policy, schema, instruction = model.review_context(role, model.bundle()[0], original_schema, '')
            self.assertNotIn('audience_relevance', [r['id'] for r in policy['rules']])
            self.assertNotIn('brand_events', [r['id'] for r in policy['rules']])
            if role == 'checker':
                codes = schema['properties']['issues']['items']['properties']['code']['enum']
                self.assertNotIn('audience_relevance', codes)
                self.assertNotIn('brand_events', codes)
            self.assertIn('Не оценивай интерес для аудитории', instruction)
        for role in ('semantic_filter', 'planner', 'writer', 'short_writer'):
            policy, _, _ = model.review_context(role, model.bundle()[0], model.CHECK, '')
            self.assertIn('audience_relevance', [r['id'] for r in policy['rules']])
