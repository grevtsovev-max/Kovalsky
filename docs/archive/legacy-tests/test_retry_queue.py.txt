import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from newsroom.db import connect
from newsroom.core import _close_exhausted_retries, _retry_ai_held_items


class RetryQueueTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = connect(str(Path(self.tmp.name) / 'test.db'))
        self.addCleanup(self.db.close)
        for i in (1, 2):
            self.db.execute("INSERT INTO sources(source_id,name,type,url) VALUES(?,?,'rss',?)", (i, str(i), f'https://example.org/{i}'))
        self.sources = {1: self.db.execute('SELECT * FROM sources WHERE source_id=1').fetchone()}

    def add_item(self, i, source=1):
        self.db.execute("INSERT INTO items(item_id,source_id,url,canonical_url,title,discovered_at,content_hash,title_hash,disposition) VALUES(?,?,?,?,?,?,?,?, 'PRIMARY_RETRY')", (i,source,str(i),str(i),'test',f'2026-09-25T00:00:{i:02d}+00:00',str(i),str(i)))
        self.db.commit()

    def test_failures_rotate_beyond_twenty_oldest_items(self):
        for i in range(1, 23):
            self.add_item(i)
        with patch('newsroom.core.process_item', side_effect=TimeoutError), patch('newsroom.core.NOW', return_value='2026-09-26T00:00:00+00:00'):
            # The production contract permits two retries per cycle. Ten
            # cycles must rotate through twenty different failing materials.
            for _ in range(10):
                self.assertEqual(_retry_ai_held_items(self.db, self.sources, {'newsroom': {}}), {'ERROR': 2})
        seen = []
        def recover(*args, **kwargs):
            item_id = kwargs['existing_item_id']
            seen.append(item_id)
            self.db.execute("UPDATE items SET disposition='NOISE' WHERE item_id=?", (item_id,))
            self.db.commit()
            return 'NOISE'
        with patch('newsroom.core.process_item', side_effect=recover):
            _retry_ai_held_items(self.db, self.sources, {'newsroom': {}}, limit=2)
        self.assertEqual(seen, [21, 22])

    def test_inactive_sources_do_not_consume_limit(self):
        self.add_item(1, source=2)
        self.add_item(2)
        with patch('newsroom.core.process_item', return_value='PRIMARY_RETRY') as process:
            _retry_ai_held_items(self.db, self.sources, {'newsroom': {}}, limit=1)
        self.assertEqual(process.call_args.kwargs['existing_item_id'], 2)

    def test_normal_retry_rotates_by_processed_time(self):
        self.add_item(1)
        self.add_item(2)
        seen = []
        def held(*args, **kwargs):
            i = kwargs['existing_item_id']; seen.append(i)
            self.db.execute("UPDATE items SET processed_at='2026-09-26T00:00:00+00:00' WHERE item_id=?", (i,))
            self.db.commit()
            return 'PRIMARY_RETRY'
        with patch('newsroom.core.process_item', side_effect=held):
            for _ in range(2):
                _retry_ai_held_items(self.db, self.sources, {'newsroom': {}}, limit=1)
        self.assertEqual(seen, [1, 2])

    def test_old_exhausted_items_are_closed_instead_of_stranding(self):
        self.add_item(1)
        self.db.execute("INSERT INTO app_state(key,value) VALUES('selection_retry:1',' {\"attempts\":3}')")
        self.db.execute("INSERT INTO items(item_id,source_id,url,canonical_url,title,discovered_at,content_hash,title_hash,disposition,processed_at) VALUES(2,1,'u2','u2','held','2026-09-25','h2','t2','WAITING_CONFIRMATION','2026-09-25')")
        self.db.execute("INSERT INTO app_state(key,value) VALUES('editor_retry:2','3')")
        self.db.commit()
        self.assertEqual(_close_exhausted_retries(self.db),2)
        self.assertEqual(self.db.execute("SELECT count(*) FROM items WHERE disposition IN ('PRIMARY_RETRY','WAITING_CONFIRMATION','AI_RETRY')").fetchone()[0],0)
