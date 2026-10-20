"""Immutable inputs and decisions for subsequent independent intake evaluation."""
import hashlib
import json

from .runtime import stamp

SCHEMA = """
CREATE TABLE IF NOT EXISTS filter_rule_snapshots (
 sha256 TEXT PRIMARY KEY,
 content_json TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS filter_rule_snapshots_no_update BEFORE UPDATE ON filter_rule_snapshots
BEGIN SELECT RAISE(ABORT, 'filter rules are immutable'); END;
CREATE TRIGGER IF NOT EXISTS filter_rule_snapshots_no_delete BEFORE DELETE ON filter_rule_snapshots
BEGIN SELECT RAISE(ABORT, 'filter rules are immutable'); END;
CREATE TABLE IF NOT EXISTS filter_audit (
 audit_id INTEGER PRIMARY KEY,
 item_id INTEGER NOT NULL REFERENCES items(item_id),
 fingerprint TEXT NOT NULL UNIQUE,
 created_at TEXT NOT NULL,
 passed INTEGER NOT NULL CHECK(passed IN (0,1)),
 snapshot_json TEXT NOT NULL,
 rules_sha256 TEXT REFERENCES filter_rule_snapshots(sha256)
);
CREATE INDEX IF NOT EXISTS filter_audit_item_idx ON filter_audit(item_id,audit_id);
CREATE INDEX IF NOT EXISTS filter_audit_created_idx ON filter_audit(created_at,audit_id);
CREATE TRIGGER IF NOT EXISTS filter_audit_no_update BEFORE UPDATE ON filter_audit
BEGIN SELECT RAISE(ABORT, 'filter audit is immutable'); END;
CREATE TRIGGER IF NOT EXISTS filter_audit_no_delete BEFORE DELETE ON filter_audit
BEGIN SELECT RAISE(ABORT, 'filter audit is immutable'); END;
"""


def initialize(db):
    """Add the small rule dictionary; historical rows migrate explicitly, not on open."""
    db.executescript(SCHEMA)
    if 'rules_sha256' not in {r[1] for r in db.execute('PRAGMA table_info(filter_audit)')}:
        db.execute('ALTER TABLE filter_audit ADD COLUMN rules_sha256 TEXT REFERENCES filter_rule_snapshots(sha256)')


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def checksum(value):
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


def save_rules(db, rules):
    content = encoded(rules)
    digest = checksum(content)
    db.execute('INSERT OR IGNORE INTO filter_rule_snapshots(sha256,content_json) VALUES(?,?)',
               (digest, content))
    saved = db.execute('SELECT content_json FROM filter_rule_snapshots WHERE sha256=?', (digest,)).fetchone()
    if saved[0] != content:
        raise ValueError('FILTER_RULE_SNAPSHOT_MISMATCH')
    return digest


def snapshot(db, row, rules_cache=None):
    """Reconstruct the exact v1 public snapshot for both historical storage formats."""
    value = json.loads(row['snapshot_json'])
    digest = row['rules_sha256'] if 'rules_sha256' in row.keys() else None
    if digest:
        cache = rules_cache if rules_cache is not None else {}
        if digest not in cache:
            saved = db.execute('SELECT content_json FROM filter_rule_snapshots WHERE sha256=?', (digest,)).fetchone()
            if saved is None or checksum(saved[0]) != digest:
                raise ValueError('FILTER_RULE_SNAPSHOT_MISSING_OR_CORRUPT')
            cache[digest] = saved[0]
        if 'rules' in value:
            raise ValueError('FILTER_AUDIT_AMBIGUOUS_RULES')
        value['rules'] = json.loads(cache[digest])
    return value


def record(db, item_id, item, spec, result):
    stored = db.execute('SELECT source_id,discovered_at,ingest_revision FROM items WHERE item_id=?',
                        (item_id,)).fetchone()
    value = {'schema_version': 1, 'item_id': item_id,
             'material': {k: item.get(k) for k in
                          ('url', 'title', 'description', 'content', 'published_at', 'updated_at')},
             'intake': dict(stored), 'rules': spec, 'decision': result}
    # Keep the original identity: retries deduplicate across old and compact rows.
    fingerprint = checksum(encoded(value))
    digest = save_rules(db, spec)
    del value['rules']
    db.execute('INSERT OR IGNORE INTO filter_audit(item_id,fingerprint,created_at,passed,snapshot_json,rules_sha256) '
               'VALUES(?,?,?,?,?,?)', (item_id, fingerprint, stamp(), int(result['passed']), encoded(value), digest))


def export(db, output):
    """Stream the unchanged public format, including rejected inputs and full rules."""
    cache = {}
    for row in db.execute('SELECT * FROM filter_audit ORDER BY audit_id'):
        value = snapshot(db, row, cache)
        value.update(audit_id=row['audit_id'], recorded_at=row['created_at'])
        value['current_disposition'] = db.execute(
            'SELECT disposition FROM items WHERE item_id=?', (row['item_id'],)).fetchone()[0]
        output.write(json.dumps(value, ensure_ascii=False) + '\n')
