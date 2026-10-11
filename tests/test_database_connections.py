import json
import sqlite3
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

    def test_legacy_clock_word_does_not_prevent_opening_database(self):
        from newsroom.db import SCHEMA
        path = str(Path(self.tmp.name) / 'legacy.sqlite3')
        old = sqlite3.connect(path)
        old.executescript(SCHEMA.replace(
            'CREATE INDEX IF NOT EXISTS items_discovered_julian_idx ON items(julianday(discovered_at));', ''))
        old.execute("INSERT INTO sources(source_id,name,type,url) VALUES(1,'Fixture','rss','https://example.org/rss')")
        old.execute("INSERT INTO items(source_id,url,canonical_url,title,discovered_at,content_hash,title_hash) "
                    "VALUES(1,'https://example.org/a','https://example.org/a','Fixture','now','fixture','fixture')")
        old.commit()
        old.close()
        migrated = connect(path)
        try:
            self.assertEqual(migrated.execute('SELECT discovered_at FROM items').fetchone()[0], 'now')
            self.assertIsNotNone(migrated.execute(
                "SELECT name FROM sqlite_master WHERE name='filter_audit'").fetchone())
        finally:
            migrated.close()
        reopened = connect(path)
        try:
            self.assertEqual(reopened.execute('SELECT COUNT(*) FROM items').fetchone()[0], 1)
        finally:
            reopened.close()

    def test_filter_audit_schema_is_restored_after_external_change(self):
        self.db.execute('DROP TABLE filter_audit')
        self.db.commit()
        other = connect(self.path)
        try:
            self.assertIsNotNone(other.execute(
                "SELECT name FROM sqlite_master WHERE name='filter_audit'").fetchone())
        finally:
            other.close()
