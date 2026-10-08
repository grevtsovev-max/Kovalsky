import sqlite3
import tempfile
from pathlib import Path
from newsroom.workflow import enqueue
import unittest
from unittest.mock import patch
from newsroom import cli


class CollectionContentionTests(unittest.TestCase):
    def test_live_collection_busy_does_not_terminate_editor_process(self):
        with patch.object(cli, 'run_one_cycle', side_effect=sqlite3.OperationalError('database is locked')), patch('builtins.print'):
            self.assertIsNone(cli._run_collection_cycle({}, 'unused', persistent=True))

    def test_once_and_non_contention_errors_remain_visible(self):
        for persistent, message in [(False, 'database is locked'), (True, 'no such table: items')]:
            with self.subTest(persistent=persistent, message=message):
                with patch.object(cli, 'run_one_cycle', side_effect=sqlite3.OperationalError(message)):
                    with self.assertRaises(sqlite3.OperationalError):
                        cli._run_collection_cycle({}, 'unused', persistent=persistent)


class DuplicateTransactionTests(unittest.TestCase):
    def test_duplicate_releases_write_lock_for_resource_receipts(self):
        with tempfile.TemporaryDirectory() as directory:
            path=str(Path(directory)/'contention.sqlite')
            db=sqlite3.connect(path)
            observer=sqlite3.connect(path,timeout=.05)
            try:
                db.execute('CREATE TABLE entries(value TEXT UNIQUE)')
                db.execute('CREATE TABLE resource_operations(value TEXT)')
                db.execute("INSERT INTO entries VALUES('existing')");db.commit()
                def duplicate(*args):
                    try:db.execute("INSERT INTO entries VALUES('existing')")
                    except sqlite3.IntegrityError:pass
                    self.assertTrue(db.in_transaction)
                    return None
                with patch('newsroom.core._save_item',side_effect=duplicate):
                    self.assertIsNone(enqueue(db,None,{}, {},{}))
                self.assertFalse(db.in_transaction)
                observer.execute("INSERT INTO resource_operations VALUES('receipt')");observer.commit()
                self.assertEqual(db.execute('SELECT COUNT(*) FROM resource_operations').fetchone()[0],1)
            finally:
                observer.close();db.close()
