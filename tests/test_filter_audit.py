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


class CompactFilterAuditTests(unittest.TestCase):
    setUp = FilterAuditTests.setUp
    run_screen = FilterAuditTests.run_screen
    def legacy(self, *, valid=True):
        import hashlib
        value = {'schema_version': 1, 'item_id': 1,
                 'material': {k: self.item.get(k) for k in
                              ('url','title','description','content','published_at','updated_at')},
                 'intake': dict(self.db.execute('SELECT source_id,discovered_at,ingest_revision FROM items WHERE item_id=1').fetchone()),
                 'rules': self.spec, 'decision': {'passed': False}}
        text = json.dumps(value, ensure_ascii=False, sort_keys=True)
        fingerprint = hashlib.sha256(text.encode()).hexdigest() if valid else 'invalid'
        self.db.execute('INSERT INTO filter_audit(item_id,fingerprint,created_at,passed,snapshot_json) VALUES(1,?,?,0,?)',
                        (fingerprint, '2026-10-10', text))
        self.db.commit()
        return value

    def test_rules_shared_and_changed_versions_remain_immutable(self):
        from newsroom.filter_audit import record, snapshot
        self.spec['large'] = 'Правила ' * 50000
        record(self.db, 1, self.item, self.spec, {'passed': False})
        self.item['title'] = 'Другая новость'
        record(self.db, 1, self.item, self.spec, {'passed': True})
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM filter_rule_snapshots').fetchone()[0], 1)
        self.assertLess(self.db.execute('SELECT MAX(length(snapshot_json)) FROM filter_audit').fetchone()[0], 2000)
        self.spec['version'] = 'v2'
        record(self.db, 1, self.item, self.spec, {'passed': False})
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM filter_rule_snapshots').fetchone()[0], 2)
        self.assertEqual(snapshot(self.db, self.db.execute('SELECT * FROM filter_audit ORDER BY audit_id').fetchone())['rules']['version'], 'v1')
        for sql in ('DELETE FROM filter_rule_snapshots', "UPDATE filter_rule_snapshots SET content_json='{}'"):
            with self.assertRaises(sqlite3.IntegrityError): self.db.execute(sql)

    def test_migration_preserves_export_identity_and_is_resumable(self):
        from ops.compact_filter_audit import compact_batch, verify
        from newsroom.filter_audit import record
        original = self.legacy()
        before = io.StringIO(); export(self.db, before)
        identity = tuple(self.db.execute('SELECT audit_id,item_id,fingerprint,created_at,passed FROM filter_audit').fetchone())
        batch = compact_batch(self.db, batch_size=1)
        self.assertEqual(batch['count'], 1)
        self.assertEqual(compact_batch(self.db)['count'], 0)
        after = io.StringIO(); export(self.db, after)
        self.assertEqual(json.loads(before.getvalue()), json.loads(after.getvalue()))
        self.assertEqual(identity, tuple(self.db.execute('SELECT audit_id,item_id,fingerprint,created_at,passed FROM filter_audit').fetchone()))
        record(self.db, 1, self.item, self.spec, original['decision'])
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM filter_audit').fetchone()[0], 1)
        self.assertEqual(verify(self.db)['verified'], 1)
        with self.assertRaises(sqlite3.IntegrityError): self.db.execute('UPDATE filter_audit SET passed=1')

    def test_bad_history_rolls_back_whole_batch_and_guard(self):
        from ops.compact_filter_audit import compact_batch
        self.legacy()
        self.spec['version'] = 'corrupt'; self.legacy(valid=False)
        with self.assertRaisesRegex(ValueError, 'FINGERPRINT_MISMATCH'): compact_batch(self.db)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM filter_audit WHERE rules_sha256 IS NOT NULL').fetchone()[0], 0)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM filter_rule_snapshots').fetchone()[0], 0)
        with self.assertRaises(sqlite3.IntegrityError): self.db.execute('UPDATE filter_audit SET passed=1')

    def test_legacy_schema_upgrade_preserves_history_without_bulk_migration(self):
        from newsroom.filter_audit import initialize
        self.legacy()
        self.db.execute('ALTER TABLE filter_audit DROP COLUMN rules_sha256'); self.db.commit()
        initialize(self.db)
        output = io.StringIO(); export(self.db, output)
        self.assertEqual(json.loads(output.getvalue())['rules']['version'], 'v1')
        self.assertIsNone(self.db.execute('SELECT rules_sha256 FROM filter_audit').fetchone()[0])
        self.run_screen(True)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM filter_audit WHERE rules_sha256 IS NOT NULL').fetchone()[0], 1)

    def test_retrospective_sees_full_rules_in_mixed_history(self):
        from newsroom import retrospective
        from newsroom.filter_audit import record
        self.legacy()
        self.spec['version'] = 'v2'
        record(self.db, 1, self.item, self.spec, {'passed': True}); self.db.commit()
        from unittest.mock import patch
        with patch('newsroom.retrospective.store.stamp', return_value='2026-10-08T12:00:00+00:00'):
            report = retrospective.collect(self.db)
        versions = [r['rules']['version'] for r in report['materials'][0]['filter_history']]
        self.assertEqual(versions, ['v1', 'v2'])

    def test_streamed_fingerprint_matches_full_snapshots_for_shared_and_changed_rules(self):
        from ops.compact_filter_audit import compact_batch, reconstructed_fingerprint, verify
        from newsroom.filter_audit import snapshot, checksum, encoded
        self.spec.update(nested={'цитата': '"Текст"\n\\строка', 'число': 1.0}, keywords=['Правило']*3000)
        for i in range(12):
            self.item['title'] = 'Материал '+str(i)
            if i == 8: self.spec['nested']['число'] = 2.0
            self.legacy()
        before=io.StringIO();export(self.db,before)
        self.assertEqual(compact_batch(self.db)['count'],12)
        after=io.StringIO();export(self.db,after)
        self.assertEqual([json.loads(x) for x in before.getvalue().splitlines()],
                         [json.loads(x) for x in after.getvalue().splitlines()])
        cache={}
        for row in self.db.execute('SELECT * FROM filter_audit ORDER BY audit_id'):
            self.assertEqual(reconstructed_fingerprint(self.db,row,cache),checksum(encoded(snapshot(self.db,row))))
        self.assertEqual(verify(self.db)['verified'],12)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM filter_rule_snapshots').fetchone()[0],2)
