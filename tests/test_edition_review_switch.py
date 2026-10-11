import copy
import unittest
from newsroom.edition import model


class ReviewSwitchTests(unittest.TestCase):
    def test_disabled_rule_is_absent_from_both_reviewers_and_issue_codes(self):
        policy, _, _ = model.bundle()
        original = copy.deepcopy(policy)
        for role, schema in [('checker', model.CHECK), ('short_checker', model.SHORT_CHECK)]:
            with self.subTest(role=role):
                rules, output, instruction = model.review_context(role, policy, schema, '')
                self.assertNotIn('exchange_regional_changes', [r['id'] for r in rules['rules']])
                self.assertIn('достоверность географии', instruction)
                if role == 'checker':
                    self.assertNotIn('exchange_regional_changes', output['properties']['issues']['items']['properties']['code']['enum'])
                    self.assertIn('facts', output['properties']['assessments']['properties'])
        self.assertEqual(policy, original)
        self.assertIn('exchange_regional_changes', model.CHECK['properties']['issues']['items']['properties']['code']['enum'])
        for role in ('semantic_filter', 'planner', 'writer', 'short_writer'):
            rules, _, _ = model.review_context(role, policy, model.CHECK, '')
            self.assertIn('exchange_regional_changes', [r['id'] for r in rules['rules']])

    def test_enabling_switch_restores_rule_and_issue_code(self):
        policy, _, _ = model.bundle()
        for rule in policy['rules']:
            if rule['id'] in ('exchange_regional_changes', 'audience_relevance'):
                rule['final_review_enabled'] = True
        for role in ('checker', 'short_checker'):
            rules, schema, instruction = model.review_context(role, policy, model.CHECK, 'original')
            self.assertIn('exchange_regional_changes', [r['id'] for r in rules['rules']])
            self.assertIn('exchange_regional_changes', schema['properties']['issues']['items']['properties']['code']['enum'])
            self.assertEqual(instruction, 'original')
