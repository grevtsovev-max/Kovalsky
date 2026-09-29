import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock
from newsroom.db import connect
from newsroom.core import terms,similarity
from newsroom.source_search import recover

class SearchStrategyTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.db=connect(str(Path(self.tmp.name)/'db'));self.addCleanup(self.db.close)
        self.item={'url':'https://example.org/news','title':'Банк России установил правила доступа к цифровым активам'}

    def test_three_distinct_strategies_and_no_fourth_search(self):
        news,web=Mock(return_value=[]),Mock(return_value=[])
        settings={'_recovery_search_budget':4}
        for _ in range(4):recover(self.db,self.item,settings,news,web,terms,similarity)
        news.assert_called_once();self.assertEqual(web.call_count,2)
        rows=self.db.execute("SELECT strategy,query FROM source_search_log WHERE outcome='STARTED'").fetchall()
        self.assertEqual(len({r['query'] for r in rows}),3)
        self.assertEqual([r['strategy'] for r in rows],['TITLE_AND_QUOTE_SEARCH','OFFICIAL_SOURCE_SEARCH','ALTERNATIVE_SOURCE_SEARCH'])

    def test_budget_wait_does_not_consume_attempt(self):
        search=Mock()
        recover(self.db,self.item,{'_recovery_search_budget':0},search,search,terms,similarity)
        self.assertTrue(self.item['_source_search_deferred'])
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM source_search_log').fetchone()[0],0)
        search.assert_not_called()

    def test_only_read_content_is_returned_and_recorded(self):
        article=dict(self.item,primary_source_status='READ',primary_source_url='https://cbr.ru/1',primary_source_content='Фактически прочитанный документ.')
        search=Mock(return_value=[article])
        found=recover(self.db,self.item,{'_recovery_search_budget':1},search,search,terms,similarity)
        self.assertEqual(found,article)
        row=self.db.execute("SELECT checked_json FROM source_search_log WHERE outcome='FOUND_CANDIDATE'").fetchone()
        self.assertIn('Фактически прочитанный документ.',row[0])

    def test_read_label_without_text_is_not_evidence(self):
        article=dict(self.item,primary_source_status='READ',primary_source_url='https://cbr.ru/1')
        search=Mock(return_value=[article])
        self.assertIsNone(recover(self.db,self.item,{'_recovery_search_budget':1},search,search,terms,similarity))
