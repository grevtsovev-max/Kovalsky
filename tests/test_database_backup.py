import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from ops.backup_database import snapshot,restore,inspect_database,ROOT


class DatabaseBackupTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.source=self.root/'live.sqlite3'
        self.db=sqlite3.connect(self.source);self.addCleanup(self.db.close)
        self.db.executescript("PRAGMA journal_mode=WAL;CREATE TABLE history(id INTEGER PRIMARY KEY,text TEXT);INSERT INTO history VALUES(1,'Published evidence');")

    def test_snapshot_includes_wal_and_restore_preserves_all_history(self):
        backup=self.root/'backups'/'snapshot.sqlite3';copy=self.root/'restored.sqlite3'
        self.assertEqual(snapshot(self.source,backup),{'history':1})
        self.assertEqual(backup.stat().st_mode&0o777,0o600)
        self.db.execute("INSERT INTO history VALUES(2,'Later evidence')");self.db.commit()
        self.assertEqual(restore(backup,copy),{'history':1})
        self.assertEqual(inspect_database(self.source),{'history':2})
        db=sqlite3.connect(copy)
        try:self.assertEqual(db.execute('SELECT text FROM history').fetchall(),[('Published evidence',)])
        finally:db.close()

    def test_uncommitted_changes_do_not_leak_into_backup(self):
        self.db.execute("INSERT INTO history VALUES(2,'Uncommitted')")
        self.assertEqual(snapshot(self.source,self.root/'snapshot.sqlite3'),{'history':1})
        self.db.rollback()

    def test_live_database_and_existing_snapshot_cannot_be_overwritten(self):
        for target in (self.source,self.root/'copy.sqlite3'):
            if target!=self.source:target.write_bytes(b'existing snapshot')
            with self.assertRaisesRegex(ValueError,'DESTINATION_EXISTS'):restore(self.source,target)
        self.assertEqual((self.root/'copy.sqlite3').read_bytes(),b'existing snapshot')

    def test_missing_source_does_not_create_an_empty_database_or_final_snapshot(self):
        destination=self.root/'snapshot.sqlite3'
        with self.assertRaises(sqlite3.OperationalError):snapshot(self.root/'missing.sqlite3',destination)
        self.assertFalse(destination.exists())
        self.assertEqual(list(self.root.glob('.snapshot-*')),[])

    def test_runtime_data_cannot_be_saved_inside_the_release(self):
        with self.assertRaisesRegex(ValueError,'OUTSIDE_RELEASE'):snapshot(self.source,ROOT/'snapshot.sqlite3')

    def test_live_writes_do_not_restart_or_change_the_pinned_snapshot(self):
        from unittest.mock import patch
        original_connect = sqlite3.connect
        live = self.db
        class Reader(sqlite3.Connection):
            def backup(reader, target, **kwargs):
                self.assertTrue(reader.in_transaction)
                live.execute("INSERT INTO history VALUES(2,'After snapshot started')")
                live.commit()
                return super(Reader, reader).backup(target, **kwargs)
        def open_database(path, *args, **kwargs):
            if isinstance(path,str) and 'mode=ro' in path:
                kwargs['factory'] = Reader
            return original_connect(path, *args, **kwargs)
        with patch('ops.backup_database.sqlite3.connect', side_effect=open_database):
            counts = snapshot(self.source,self.root/'pinned.sqlite3')
        self.assertEqual(counts, {'history':1})
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM history').fetchone()[0],2)
