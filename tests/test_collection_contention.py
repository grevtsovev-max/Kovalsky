import sqlite3
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
