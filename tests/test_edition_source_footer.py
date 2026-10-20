import copy
from edition_helpers import EditionCase,draft
from newsroom.edition import store
from newsroom.edition.formatting import render,source_list


class SourceFooterTests(EditionCase):
    def fixture(self):
        mid=self.material(url='https://t.me/zenitlegal/626')
        materials=store.materials(self.db,[mid]);value=draft(mid)
        label,url=source_list(value,materials)[0]
        return value,materials,label,url

    def test_source_metadata_is_not_repeated_in_details_or_plain_text(self):
        value,materials,label,url=self.fixture();before=copy.deepcopy(value)
        value['blocks'].append({'kind':'details','text':'Источник: '+label+' (Telegram) — '+url,
                                'evidence':value['lead_evidence'],'quote_text':'','quote_author':''})
        snapshot=copy.deepcopy(value)
        html,plain=render(value,materials)
        self.assertEqual(html.count('Источник:'),1)
        self.assertEqual(plain.count('Источник:'),1)
        self.assertNotIn('<blockquote expandable>',html)
        self.assertIn(before['blocks'][0]['text'],plain)
        self.assertEqual(value,snapshot)
        self.assertIn('href="'+url+'"',html)

    def test_facts_quotes_and_other_sources_are_preserved(self):
        value,materials,label,url=self.fixture()
        texts=['Источник: '+label+' — '+url+' сообщил о запуске.',
               'Источник: Другой источник — https://example.org/other']
        value['blocks']=[{'kind':'details','text':t} for t in texts]
        value['blocks'].append({'kind':'quote','text':'Источник: '+label+' — '+url,'quote_text':'Прямая цитата'})
        html,plain=render(value,materials)
        for text in texts:self.assertIn(text,plain)
        self.assertEqual(plain.count('Источник:'),4)
        self.assertIn('<blockquote>',html)
