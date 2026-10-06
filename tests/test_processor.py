import copy
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from newsroom.db import connect
from newsroom.workflow import Work, StageExecutor, enqueue, Coordinator
from newsroom.processor import prepare, seed_held, run
try:
    import test_workflow as fixtures
except ModuleNotFoundError:
    import tests.test_workflow as fixtures


class ProcessorTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.WorkflowTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.db, self.config, self.source = self.fixture.db, self.fixture.config, self.fixture.source

    def test_collection_does_not_wait_for_editor(self):
        from newsroom.core import run_cycle
        self.config['_collection_only'] = True
        with patch('newsroom.core.fetch_rss', return_value=[self.fixture.item()]), patch('newsroom.core.analyze_with_ai') as editor:
            run_cycle(self.config)
        editor.assert_not_called()
        self.assertEqual(self.db.execute('SELECT status FROM processing_jobs').fetchone()[0], 'PENDING')

    def test_reader_pool_does_not_block_editor_pool(self):
        release = threading.Event()
        started = threading.Event()
        def read():
            started.set()
            release.wait(2)
        def analyze():
            return {'action': 'NOISE'}
        executor = StageExecutor(1)
        try:
            reader = executor.submit(Work('collector', read).execute, None, {})
            self.assertTrue(started.wait(1))
            editor = executor.submit(Work('editor', analyze).execute, None, {})
            self.assertEqual(editor.result(timeout=1), {'action': 'NOISE'})
            self.assertFalse(reader.done())
        finally:
            release.set()
            executor.shutdown()

    def test_legacy_migration_preserves_attempts_and_read_source(self):
        from newsroom.material_flow import migrate, snapshot
        enqueue(self.db, None, self.fixture.item(), self.source, self.fixture.options)
        self.db.execute("UPDATE items SET disposition='WAITING_CONFIRMATION'")
        self.db.execute("INSERT INTO app_state VALUES('selection_retry:1',?)", (json.dumps({'attempts': 1}),))
        self.db.execute("INSERT INTO app_state VALUES('editor_retry:1','2')")
        self.db.commit()
        migrate(self.db)
        migrate(self.db)
        state = json.loads(self.db.execute("SELECT value FROM app_state WHERE key='selection_retry:1'").fetchone()[0])
        self.assertEqual(state['attempts'], 2)
        self.assertTrue(any(x['stage']=='reading' and x['status']=='DONE' for x in snapshot(self.db, 1)))
        self.assertEqual(self.db.execute('SELECT disposition FROM items').fetchone()[0], 'WAITING_CONFIRMATION')

    def test_processor_works_without_collection_cycle(self):
        enqueue(self.db, None, self.fixture.item(), self.source, self.fixture.options)
        path = Path(self.fixture.temp.name) / 'worker.toml'
        path.write_text('[newsroom]\ndatabase = '+json.dumps(self.fixture.path)+'\n[ai]\nmodel = "test"\n')
        stop = threading.Event()
        errors = []
        def worker():
            try:
                run(str(path), stop)
            except Exception as exc:
                errors.append(exc)
        with patch('newsroom.core.get_api_key', return_value='test'), \
             patch('newsroom.core.analyze_with_ai', return_value=self.fixture.noise()), \
             patch('newsroom.cli.auto_publish_since', return_value=(0, 0, 0)):
            thread = threading.Thread(target=worker)
            thread.start()
            try:
                deadline = time.monotonic()+5
                while time.monotonic()<deadline and not errors:
                    if self.db.execute('SELECT disposition FROM items').fetchone()[0] == 'NOISE':
                        break
                    time.sleep(.05)
                self.assertEqual(self.db.execute('SELECT disposition FROM items').fetchone()[0], 'NOISE')
            finally:
                stop.set()
                thread.join(timeout=5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])

    def test_stale_saved_item_cannot_overwrite_new_revision(self):
        from newsroom.core import _save_item
        item = self.fixture.item()
        item_id = _save_item(self.db, self.source, item)
        old_revision = self.db.execute('SELECT ingest_revision FROM items').fetchone()[0]
        changed = dict(item, content=item['content']+' Новое существенное условие.')
        self.assertEqual(_save_item(self.db, self.source, changed), item_id)
        old = dict(item, _expected_revision=old_revision)
        self.assertIsNone(_save_item(self.db, self.source, old, existing_item_id=item_id))
        self.assertEqual(self.db.execute('SELECT content FROM items').fetchone()[0], changed['content'])

    def test_google_discovery_does_not_read_article_before_selection(self):
        from newsroom.core import fetch_google_news
        xml = b'<rss><channel><item><title>News</title><link>https://news.google.com/rss/articles/example</link><pubDate>Tue, 06 Oct 2026 10:00:00 GMT</pubDate><source>Publisher</source></item></channel></rss>'
        with patch('newsroom.core._request', return_value=xml), patch('newsroom.core.fetch_publisher_article') as reader:
            items = fetch_google_news('https://news.google.com/rss/search?q=test', read_articles=False)
        reader.assert_not_called()
        self.assertEqual(len(items), 1)
        self.assertNotIn('material_read', items[0])
