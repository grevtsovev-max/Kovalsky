"""Immutable inputs and decisions for subsequent independent intake evaluation."""
import hashlib
import json

from .runtime import stamp

SCHEMA = """
CREATE TABLE IF NOT EXISTS filter_audit (
 audit_id INTEGER PRIMARY KEY,
 item_id INTEGER NOT NULL REFERENCES items(item_id),
 fingerprint TEXT NOT NULL UNIQUE,
 created_at TEXT NOT NULL,
 passed INTEGER NOT NULL CHECK(passed IN (0,1)),
 snapshot_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS filter_audit_created_idx ON filter_audit(created_at,audit_id);
CREATE TRIGGER IF NOT EXISTS filter_audit_no_update BEFORE UPDATE ON filter_audit
BEGIN SELECT RAISE(ABORT, 'filter audit is immutable'); END;
CREATE TRIGGER IF NOT EXISTS filter_audit_no_delete BEFORE DELETE ON filter_audit
BEGIN SELECT RAISE(ABORT, 'filter audit is immutable'); END;
"""


def record(db, item_id, item, spec, result):
    stored = db.execute('SELECT source_id,discovered_at,ingest_revision FROM items WHERE item_id=?',
                        (item_id,)).fetchone()
    snapshot = {'schema_version': 1, 'item_id': item_id,
                'material': {k: item.get(k) for k in
                             ('url', 'title', 'description', 'content', 'published_at', 'updated_at')},
                'intake': dict(stored), 'rules': spec, 'decision': result}
    encoded = json.dumps(snapshot, ensure_ascii=False, sort_keys=True)
    fingerprint = hashlib.sha256(encoded.encode('utf-8')).hexdigest()
    db.execute('INSERT OR IGNORE INTO filter_audit(item_id,fingerprint,created_at,passed,snapshot_json) '
               'VALUES(?,?,?,?,?)', (item_id, fingerprint, stamp(), int(result['passed']), encoded))


def export(db, output):
    """Stream all observations, including rejected inputs; do not infer quality."""
    for row in db.execute('SELECT * FROM filter_audit ORDER BY audit_id'):
        record = json.loads(row['snapshot_json'])
        record.update(audit_id=row['audit_id'], recorded_at=row['created_at'])
        record['current_disposition'] = db.execute(
            'SELECT disposition FROM items WHERE item_id=?', (row['item_id'],)).fetchone()[0]
        output.write(json.dumps(record, ensure_ascii=False) + '\n')
