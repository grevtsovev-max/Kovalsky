import unittest
from newsroom.edition.source_text import editorial_source, promotional_sentence
from newsroom.edition.short_release import extractive
from newsroom.edition.formatting import validate


class PromotionalRecoveryTests(unittest.TestCase):
    def test_reader_solicitation_removed_without_losing_following_news(self):
        news='Национальное рейтинговое агентство выпустило исследование о майнинге.'
        source='🚀 Подключите от 200 TH/s на Bitcoin.\n'+news+'\nЕсли оборудование уже подключено, проверьте, что вы подали заявку.'
        self.assertEqual(editorial_source(source),news)
        self.assertFalse(promotional_sentence('Компания подключила оборудование мощностью 200 TH/s.'))
        self.assertFalse(promotional_sentence('Сервис помогает оценить риски криптовалютных переводов.'))

    def test_multi_event_brand_bundle_cannot_use_unchecked_offline_snippets(self):
        group={'material_ids':[1,2],'_event_keys':['mining_report','pool_giveaway']}
        self.assertIsNone(extractive(group,[]))

    def test_draft_solicitation_is_rejected_even_with_exact_source_proof(self):
        quote='Подключите от 200 TH/s на Bitcoin или от 15 GH/s на Scrypt'
        material={'material_id':1,'title':quote,'content':quote,'url':'https://t.me/example/1','source_name':'Example'}
        proof=[{'material_id':1,'quote':quote}]
        draft={'headline':'📰 '+quote,'headline_evidence':proof,'lead':quote,'lead_evidence':proof,'blocks':[]}
        self.assertTrue(any(i['code']=='body' for i in validate(draft,[material])))
