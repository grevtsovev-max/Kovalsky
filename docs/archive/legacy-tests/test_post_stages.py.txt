import json
import unittest
from unittest.mock import patch
from newsroom import ai


def response(value):
    return {'output': [{'content': [{'type': 'output_text', 'text': json.dumps(value)}]}]}


class PostStagesTests(unittest.TestCase):
    def test_final_check_receives_only_the_assembled_publication_text(self):
        post = '🇷🇺 Банк изменил условия\n\nНовые условия.\n\nИсточник: [Банк](https://example.org)'
        draft = {'post_text': post, 'headline_ru': 'Другой заголовок',
                 'summary_ru': 'Не публикуемое резюме', 'what_is_new': 'Другой вариант'}
        decision = dict(draft, facts=[{'text': 'Новые условия.'}])
        audit = {key: '' if key == 'history_note' else False
                 for key in ai.SCHEMA['properties']['editorial_check']['required']}
        result = {'issues': [], 'editorial_check': audit, 'covered_claims': []}
        with patch.object(ai, 'request_response', return_value=response(result)) as request:
            self.assertEqual(ai.validate_draft(decision, {'content': 'Источник'}, draft, {}), result)
        data = json.loads(request.call_args.args[0]['input'])
        self.assertEqual(data['post_text'], post)
        self.assertNotIn('draft', data)
        self.assertNotIn('summary_ru', data['decision'])
        self.assertNotIn('what_is_new', data['decision'])
        self.assertNotIn('post_text', data['decision'])
        self.assertEqual(data['decision']['facts'], decision['facts'])

    def test_final_check_requires_assembled_text(self):
        with patch.object(ai, 'request_response') as request:
            with self.assertRaisesRegex(ai.AIResponseError, 'ASSEMBLED_POST_MISSING'):
                ai.validate_draft({}, {}, {'headline_ru': 'Заголовок', 'summary_ru': 'Текст'}, {})
        request.assert_not_called()

    def test_analysis_marks_unwritten_decision_for_later_drafting(self):
        with patch.object(ai, 'get_api_key', return_value='test'), patch.object(ai, 'request_response', return_value=response({'action': 'NEW_STORY'})) as request:
            result = ai.analyze({'title': 'Событие'}, {'name': 'Источник', 'priority': 1, 'reputation': 'unknown'}, [], {'_analysis_only': True})
        self.assertTrue(result['_needs_post_draft'])
        self.assertIn('только анализ до написания поста', request.call_args.args[0]['instructions'])

    def test_drafting_cannot_replace_factual_decision(self):
        draft = {'headline_ru': 'Заголовок', 'summary_ru': 'Текст', 'what_is_new': 'Изменение', 'editorial_check': {key: '' if key == 'history_note' else False for key in ai.SCHEMA['properties']['editorial_check']['required']}}
        decision = {'facts': [{'text': 'Подтверждённый факт', 'claim_type': 'REPORT'}], 'publication_recommendation': 'AUTO_PUBLISH'}
        with patch.object(ai, 'request_response', return_value=response(draft)) as request:
            self.assertEqual(ai.draft_post(decision, {'content': 'Прочитанный текст'}, {}), draft)
        fields = request.call_args.args[0]['text']['format']['schema']['properties']
        self.assertNotIn('facts', fields)
        self.assertNotIn('publication_recommendation', fields)
        with patch.object(ai, 'request_response', return_value=response({**draft, 'facts': []})):
            with self.assertRaises(ai.AIResponseError):
                ai.draft_post(decision, {'content': 'Прочитанный текст'}, {})
        self.assertEqual(decision['facts'][0]['claim_type'], 'REPORT')

    def test_nullable_nonmessage_content_does_not_break_analysis(self):
        r = response({'action': 'NEW_STORY'})
        r['output'].insert(0, {'type': 'reasoning', 'content': None})
        with patch.object(ai, 'get_api_key', return_value='test'), patch.object(ai, 'request_response', return_value=r):
            result = ai.analyze({'title': 'Событие'}, {'name': 'Источник', 'priority': 1, 'reputation': 'unknown'}, [], {'_analysis_only': True})
        self.assertEqual(result['action'], 'NEW_STORY')

    def test_nullable_nonmessage_content_does_not_break_drafting(self):
        draft = {'headline_ru': 'Заголовок', 'summary_ru': 'Текст', 'what_is_new': 'Изменение', 'editorial_check': {key: '' if key == 'history_note' else False for key in ai.SCHEMA['properties']['editorial_check']['required']}}
        r = response(draft)
        r['output'].insert(0, {'type': 'reasoning', 'content': None})
        with patch.object(ai, 'request_response', return_value=r):
            self.assertEqual(ai.draft_post({'facts': []}, {'content': 'Источник'}, {}), draft)
