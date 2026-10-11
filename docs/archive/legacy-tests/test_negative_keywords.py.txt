import csv
import io
import unittest

from newsroom import topic_registry as topics
from newsroom.keyword_filter import evaluate


class NegativeKeywordsTests(unittest.TestCase):
    def entry(self, word='79thVault', **values):
        return dict(word=word, enabled=True, scope='Заголовок', kind='Название',
                    aliases=['79th Vault'], row=2, **values)

    def spec(self, entry):
        return {'keywords':['криптовалюта'], 'negative_keywords':[entry]}

    def test_names_aliases_boundaries_and_scope(self):
        spec = self.spec(self.entry())
        for title in ['79THVAULT: криптовалюта', '79th Vault: криптовалюта']:
            result = evaluate(title, spec, title)
            self.assertFalse(result['passed'])
            self.assertEqual(result['matched_negative_keyword'], '79thVault')
        for title in ['79thVault2 криптовалюта','my79thVault криптовалюта']:
            self.assertTrue(evaluate(title, spec, title)['passed'])
        self.assertTrue(evaluate('криптовалюта 79thVault', spec, 'криптовалюта')['passed'])
        spec['negative_keywords'][0]['scope'] = 'Весь текст'
        self.assertFalse(evaluate('криптовалюта 79thVault', spec, 'криптовалюта')['passed'])

    def test_disabled_and_inflections_and_html_attributes(self):
        entry = self.entry(); entry['enabled'] = False
        self.assertTrue(evaluate('79thVault криптовалюта', self.spec(entry), '79thVault криптовалюта')['passed'])
        entry = self.entry('торговый сигнал'); entry.update(kind='Слово или фраза',aliases=[])
        self.assertFalse(evaluate('криптовалюта с торговыми сигналами',self.spec(entry), 'криптовалюта с торговыми сигналами')['passed'])
        title = '<a href="https://79thVault.example">криптовалюта</a>'
        self.assertTrue(evaluate(title, self.spec(self.entry()), title)['passed'])

    def test_negative_overrides_person_rule_and_preserves_configuration_error(self):
        spec = {'mode':'intake_rules','entries':[{'role':'Профильный','description':'криптовалюта','title':'Крипто'}],
                'people':['Иван Иванов'],'rules':[{'name':'Лицо','required':['Лицо в заголовке'],'enabled':True}],
                'negative_keywords':[self.entry()]}
        title='Иван Иванов обсудил 79thVault'
        self.assertFalse(evaluate(title,spec,title)['passed'])
        spec['entries']=[]
        result=evaluate(title,spec,title)
        self.assertTrue(result['configuration_error'])
        self.assertNotIn('matched_negative_keyword',result)

    def test_parser_defaults_flags_validation_and_duplicate_rows(self):
        def body(rows):
            stream=io.StringIO();writer=csv.writer(stream)
            writer.writerow(topics.NEGATIVE_HEADERS);writer.writerows(rows)
            return stream.getvalue().encode()
        entries=topics.parse_negative_keywords(body([['Vavada','TRUE','','Название','Вавада',''],['Sui','FALSE']]))
        self.assertEqual(entries[0]['scope'],'Заголовок')
        self.assertEqual(entries[0]['aliases'],['Вавада'])
        self.assertFalse(entries[1]['enabled'])
        for rows in [[['Sui','yes']],[['Sui','TRUE','Описание']],[['Sui','TRUE','','Регекс']], [['Sui','TRUE'],['sui','FALSE']]]:
            with self.assertRaises(ValueError):topics.parse_negative_keywords(body(rows))

    def test_snapshot_attaches_enabled_entries_and_changes_spec(self):
        from test_intake_rules import fixture
        snapshot, entities, _, _ = fixture()
        snapshot['sections'][topics.NEGATIVE_KEYWORDS]=[self.entry(),dict(self.entry('Sui'),enabled=False)]
        config={};topics.apply_snapshot(config,snapshot,entities)
        entries=config['ai']['_keyword_prefilter']['negative_keywords']
        self.assertEqual(len(entries),1)

    def test_rejected_material_never_reaches_reading_or_ai(self):
        from unittest.mock import patch
        from test_keyword_filter import KeywordFilterTests
        runner=KeywordFilterTests();runner.setUp();self.addCleanup(runner.doCleanups)
        runner.settings['_keyword_prefilter']['negative_keywords']=[self.entry('Vavada')]
        runner.item['title']='Vavada создала цифровой депозитарий'
        with patch('newsroom.core.fetch_publisher_article') as read, patch('newsroom.ai.request_response') as ai:
            self.assertEqual(runner.process(),'NOISE')
        read.assert_not_called();ai.assert_not_called()
        import json
        saved=json.loads(runner.db.execute("SELECT result_json FROM material_stage_results WHERE stage='screening'").fetchone()[0])
        self.assertEqual(saved['matched_negative_keyword'],'Vavada')

    def test_registry_version_changes_and_invalid_rows_fail_atomic_read(self):
        from unittest.mock import patch
        from test_topic_registry import snapshot
        saved=snapshot();settings={'spreadsheet_id':'x'*25,'tabs':[
            {'name':n,'gid':str(i)} for i,n in enumerate(topics.HEADERS,1)],'negative_keywords_gid':'99'}
        def response(url,**kwargs):
            stream=io.StringIO();writer=csv.writer(stream)
            if url.endswith('gid=99'):
                writer.writerow(topics.NEGATIVE_HEADERS);writer.writerow(['Vavada','TRUE','Заголовок','Название','',''])
            else:
                tab=next(t for t in settings['tabs'] if url.endswith('gid='+t['gid']))
                writer.writerow(topics.HEADERS[tab['name']])
                for r in saved['sections'][tab['name']]:writer.writerow([r['title'],r['description'],'TRUE' if r['enabled'] else 'FALSE'])
            return stream.getvalue().encode(),{},url
        with patch('newsroom.core._request_with_url',side_effect=response):
            first=topics.read_registry(settings)
            legacy=topics.read_registry({k:v for k,v in settings.items() if k!='negative_keywords_gid'})
        self.assertNotEqual(first['version'],legacy['version'])
        self.assertEqual(first['sections'][topics.NEGATIVE_KEYWORDS][0]['word'],'Vavada')
        with patch('newsroom.core._request_with_url',side_effect=lambda url,**kw: (b'broken',{},url) if url.endswith('gid=99') else response(url,**kw)):
            with self.assertRaises(ValueError):topics.read_registry(settings)


if __name__ == '__main__':unittest.main()
