import io
import json
import sqlite3
import unittest
from unittest.mock import patch

from newsroom.db import connect
from newsroom.filter_audit import export
from newsroom.keyword_filter import screen


class FilterAuditTests(unittest.TestCase):
    def setUp(self):
        self.db = connect(':memory:')
        self.addCleanup(self.db.close)
        self.db.execute("INSERT INTO sources(name,type,url) VALUES('Test','rss','https://example.org/rss')")
        self.db.execute("INSERT INTO items(source_id,url,canonical_url,title,discovered_at,content_hash,title_hash) "
                        "VALUES(1,'https://example.org/1','https://example.org/1','Original','2026-10-08','hash','title')")
        self.item = {'url': 'https://example.org/1', 'title': 'Original', 'content': 'Read text'}
        self.spec = {'version': 'v1', 'keywords': ['test']}

    def run_screen(self, passed):
        result = {'passed': passed, 'matched_keyword': 'test' if passed else None,
                  'matched_brand': None, 'matched_topic_keyword': None, 'brand_only': False}
        with patch('newsroom.keyword_filter.evaluate', return_value=result):
            screen(self.db, 1, self.item, {'_keyword_prefilter': self.spec})

    def test_both_outcomes_and_exact_inputs_survive_later_changes(self):
        self.run_screen(False)
        self.item['title'] = 'Changed'
        self.spec['version'] = 'v2'
        self.run_screen(True)
        output = io.StringIO()
        export(self.db, output)
        rows = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual(len(rows), 2)
        self.assertFalse(rows[0]['decision']['passed'])
        self.assertEqual(rows[0]['material']['title'], 'Original')
        self.assertEqual(rows[0]['material']['content'], 'Read text')
        self.assertEqual(rows[0]['rules']['version'], 'v1')
        self.assertTrue(rows[1]['decision']['passed'])

    def test_retries_are_deduplicated_and_history_is_immutable(self):
        self.run_screen(False)
        self.run_screen(False)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM filter_audit').fetchone()[0], 1)
        for statement in ('DELETE FROM filter_audit', 'UPDATE filter_audit SET passed=1'):
            with self.assertRaises(sqlite3.IntegrityError):
                self.db.execute(statement)
