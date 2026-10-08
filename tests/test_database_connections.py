import json
import tempfile
import unittest
from pathlib import Path

from newsroom.db import connect


class DatabaseConnectionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = str(Path(self.tmp.name) / 'news.sqlite3')
        self.db = connect(self.path)
        self.addCleanup(self.db.close)

    def test_ready_connection_opens_during_another_writer_without_migration(self):
        self.db.execute('BEGIN IMMEDIATE')
        other = connect(self.path)
        self.addCleanup(other.close)
        self.assertFalse(other.in_transaction)
        self.assertEqual(other.execute('PRAGMA foreign_keys').fetchone()[0], 1)
        self.assertEqual(other.execute('SELECT COUNT(*) FROM items').fetchone()[0], 0)
        self.db.rollback()

    def test_schema_change_invalidates_marker_and_repairs_missing_column(self):
        self.db.execute('ALTER TABLE items DROP COLUMN primary_source_json')
        self.db.commit()
        other = connect(self.path)
        self.addCleanup(other.close)
        self.assertIn('primary_source_json', [r[1] for r in other.execute('PRAGMA table_info(items)')])
        state = json.loads(other.execute("SELECT value FROM app_state WHERE key='database_schema_ready'").fetchone()[0])
        self.assertEqual(state['cookie'], other.execute('PRAGMA schema_version').fetchone()[0])

    def test_bad_marker_is_rebuilt_and_connection_remains_usable(self):
        for value in ('not-json', '[]', '{}'):
            with self.subTest(value=value):
                self.db.execute("UPDATE app_state SET value=? WHERE key='database_schema_ready'", (value,))
                self.db.commit()
                other = connect(self.path)
                try:
                    state = json.loads(other.execute("SELECT value FROM app_state WHERE key='database_schema_ready'").fetchone()[0])
                    self.assertIn('signature', state)
                finally:
                    other.close()
