import json
import tempfile
import unittest
from pathlib import Path
from newsroom.db import connect
from newsroom.decisions import record

class DecisionTests(unittest.TestCase):
    def test_reanalysis_preserves_prior_evidence_and_rule_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = connect(str(Path(tmp) / 'db'))
            try:
                db.execute("INSERT INTO sources(source_id,name,type,url) VALUES(1,'source','rss','https://example.org')")
                db.execute("INSERT INTO items(item_id,source_id,url,canonical_url,title,content,discovered_at,content_hash,title_hash) VALUES(1,1,'url','url','title','old text','2026','hash','title')")
                record(db, 1, 'PRIMARY_RETRY', 'test')
                db.execute("UPDATE items SET content='new text' WHERE item_id=1")
                record(db, 1, 'NEW_STORY', 'test')
                db.commit()
                rows = db.execute('SELECT * FROM agent_decisions ORDER BY decision_id').fetchall()
                self.assertEqual(len(rows), 2)
                self.assertEqual(json.loads(rows[0]['payload_json'])['item']['content'], 'old text')
                self.assertEqual(json.loads(rows[1]['payload_json'])['item']['content'], 'new text')
                self.assertTrue(db.execute('SELECT content FROM rule_snapshots WHERE sha256=?', (rows[0]['editorial_rules_version'],)).fetchone()[0])
                with self.assertRaisesRegex(Exception, 'append only'):
                    db.execute("UPDATE agent_decisions SET decision='NEW_STORY'")
            finally: db.close()
