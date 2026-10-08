import copy
import json
import unittest
from newsroom.edition.formatting import validate,render,normalize,material_source
from newsroom.edition import store
from edition_helpers import EditionCase,draft,LEAD,BODY


class FormattingTests(EditionCase):
    def setUp(self):
        super().setUp();self.mid=self.material();self.materials=store.materials(self.db,[self.mid]);self.draft=draft(self.mid)

    def codes(self):return {i['code'] for i in validate(self.draft,self.materials)}

    def test_valid_draft_and_safe_html(self):
        self.assertEqual(validate(self.draft,self.materials),[])
        markup,plain=render(self.draft,self.materials)
        self.assertTrue(markup.startswith('<b>🏦 '));self.assertIn('Источник: <a href="https://example.org/news/1">',markup)
        self.assertNotIn('**',markup);self.assertNotIn('https://',plain)

    def test_exact_headline_limits(self):
        for length in (3,29,70,79,80,110,111):
            with self.subTest(length=length):
                self.draft['headline']='🏦 '+('А'*(length-2))
                self.assertEqual('headline_length' in self.codes(),length>110)

    def test_emoji_required_and_only_one(self):
        for prefix in ('','🏦 🚀 '):
            self.draft['headline']=prefix+'Компания Альфа '+('а'*70)
            self.assertIn('headline_emoji',self.codes())

    def test_flag_is_one_emoji_cluster(self):
        self.draft['headline']='🇷🇺 '+('А'*85)
        self.assertNotIn('headline_emoji',self.codes())

    def test_block_limit_includes_bullet(self):
        for length,kind,expected in [(210,'paragraph',False),(211,'paragraph',True),(208,'bullet',False),(209,'bullet',True)]:
            self.draft['blocks'][0].update(text='а'*length,kind=kind)
            self.assertEqual('paragraph' in self.codes(),expected)

    def test_lead_limit_and_date(self):
        self.draft['lead']='а'*211
        self.assertIn('paragraph',self.codes())
        self.draft['lead']='8 октября компания запустила сервис.'
        self.assertIn('lead',self.codes())

    def test_strong_quote_in_lead_is_not_automatically_rejected(self):
        self.draft['lead']='«Мы планируем расширить доступ», — заявил Иван Иванов.'
        self.assertNotIn('lead',self.codes())

    def test_missing_and_invented_evidence_block(self):
        self.draft['lead_evidence']=[];self.assertIn('facts',self.codes())
        self.draft['lead_evidence']=[{'material_id':self.mid,'quote':'выдуманная цитата'}]
        self.assertIn('evidence',self.codes())

    def test_quote_must_be_exact_and_attributed(self):
        b=self.draft['blocks'][0]
        b.update(kind='quote',text='«Мы планируем расширить доступ» — Иван Иванов.',quote_text='Мы планируем расширить доступ',quote_author='Иван Иванов')
        self.assertNotIn('quotes',self.codes())
        b['quote_text']='Мы уже расширили доступ';self.assertIn('quotes',self.codes())

    def test_details_expand_and_html_is_escaped(self):
        self.draft['blocks'][0].update(kind='details',text='<script>alert(1)</script>')
        markup,_=render(self.draft,self.materials)
        self.assertIn('<blockquote expandable>',markup);self.assertIn('&lt;script&gt;',markup);self.assertNotIn('<script>',markup)

    def test_source_attribution_intro_is_blocked(self):
        self.draft['lead']='По данным Проверенный источник, компания запустила сервис.'
        self.assertIn('sources',self.codes())

    def test_actor_statement_is_permitted(self):
        self.draft['lead']='Компания Альфа планирует расширить доступ в декабре.'
        self.assertNotIn('sources',self.codes())

    def test_words_and_telegram_limit(self):
        self.draft['blocks']=[{**copy.deepcopy(self.draft['blocks'][0]),'text':'слово '*30} for _ in range(15)]
        self.assertIn('length',self.codes())
        self.draft['blocks']=[{**copy.deepcopy(self.draft['blocks'][0]),'text':'я'*200} for _ in range(25)]
        self.assertIn('telegram_length',self.codes())

    def test_normalizer_does_not_change_numbers_or_quotes(self):
        self.draft['blocks'][0]['text']='  «Цена  0.10 USD» — Иванов.  '
        self.assertEqual(normalize(self.draft)['blocks'][0]['text'],'«Цена  0.10 USD» — Иванов.')
        self.assertEqual(self.draft['blocks'][0]['text'],'  «Цена  0.10 USD» — Иванов.  ')

    def test_each_publisher_links_to_its_own_material(self):
        second=copy.deepcopy(self.materials[0]);second['material_id']=999
        for material,publisher,url in [(self.materials[0],'CoinDesk','https://coindesk.com/article-one'),(second,'Cointelegraph','https://cointelegraph.com/article-two')]:
            material['source_name']='Упоминания: CoinDesk, Cointelegraph,'
            material['url']='https://news.google.com/rss/articles/discovery'
            material['primary_source_json']=json.dumps({'_material_publisher':publisher,'_material_url':url,'url':'https://unread-primary.example/','publisher':'Unrelated primary source'})
        self.draft['blocks'][0]['evidence'].append({'material_id':999,'quote':BODY})
        markup,_=render(self.draft,self.materials+[second])
        self.assertIn('<a href="https://coindesk.com/article-one">CoinDesk</a>',markup)
        self.assertIn('<a href="https://cointelegraph.com/article-two">Cointelegraph</a>',markup)
        self.assertNotIn('Упоминания:',markup)
        self.assertNotIn('unread-primary',markup)

    def test_publisher_with_only_google_news_url_keeps_article_link(self):
        material=self.materials[0]
        material['url']='https://news.google.com/rss/articles/one'
        material['primary_source_json']=json.dumps({'_material_publisher':'CoinDesk','_material_url':material['url']})
        self.assertEqual(material_source(material),('CoinDesk',material['url']))

    def test_malformed_metadata_and_unsafe_material_url_fall_back(self):
        material=self.materials[0]
        for metadata in ('broken','[]','null',json.dumps({'_material_url':'javascript:alert(1)'})):
            material['primary_source_json']=metadata
            self.assertEqual(material_source(material),('Проверенный источник',material['url']))

    def test_no_minimum_words(self):
        self.assertEqual(validate(self.draft,self.materials),[])


if __name__=='__main__':unittest.main()
