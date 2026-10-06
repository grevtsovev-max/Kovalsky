import json
import unittest
from unittest.mock import patch
from newsroom import ai


def response(value):
    return {'output': [{'content': [{'type': 'output_text', 'text': json.dumps(value)}]}]}


class PostStagesTests(unittest.TestCase):
    def test_analysis_marks_unwritten_decision_for_later_drafting(self):
        with patch.object(ai, 'get_api_key', return_value='test'), patch.object(ai, 'request_response', return_value=response({'action': 'NEW_STORY'})) as request:
            result = ai.analyze({'title': 'Событие'}, {'name': 'Источник', 'priority': 1, 'reputation': 'unknown'}, [], {'_analysis_only': True})
        self.assertTrue(result['_needs_post_draft'])
        self.assertIn('только анализ до написания поста', request.call_args.args[0]['instructions'])

    def test_drafting_cannot_replace_factual_decision(self):
        draft = {'headline_ru': 'Заголовок', 'summary_ru': 'Текст', 'what_is_new': 'Изменение', 'editorial_check': {}}
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
