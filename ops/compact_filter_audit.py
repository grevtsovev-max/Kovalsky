"""Lossless, resumable filter audit normalization; run after a verified database backup.

No publication, source, decision or delivery record is removed. Each bounded write
transaction restores the immutable trigger before committing. A failed batch rolls
back both the data and the trigger change; existing readers keep their WAL snapshot.
"""
import argparse
import hashlib
import json
import sqlite3
import time

from newsroom import filter_audit


def compact_batch(db, *, batch_size=100, after_id=0):
    if not 1 <= batch_size <= 1000:
        raise ValueError('BATCH_SIZE_OUT_OF_RANGE')
    if db.in_transaction:
        raise ValueError('MAINTENANCE_REQUIRES_OWN_TRANSACTION')
    db.execute('BEGIN IMMEDIATE')
    try:
        rows = db.execute('SELECT * FROM filter_audit WHERE audit_id>? AND rules_sha256 IS NULL '
                          'ORDER BY audit_id LIMIT ?', (after_id, batch_size)).fetchall()
        if not rows:
            db.rollback()
            return {'count': 0, 'after_id': after_id, 'saved_payload_bytes': 0}
        trigger = db.execute("SELECT sql FROM sqlite_master WHERE type='trigger' AND name='filter_audit_no_update'").fetchone()
        if not trigger:
            raise ValueError('FILTER_AUDIT_IMMUTABILITY_GUARD_MISSING')
        db.execute('DROP TRIGGER filter_audit_no_update')
        saved_bytes = 0
        versions = []
        canonical_rules = {}
        for row in rows:
            original = row['snapshot_json']
            value = json.loads(original)
            if (filter_audit.checksum(original) != row['fingerprint'] and
                    filter_audit.checksum(filter_audit.encoded(value)) != row['fingerprint']):
                raise ValueError('FILTER_AUDIT_FINGERPRINT_MISMATCH:'+str(row['audit_id']))
            if value['item_id'] != row['item_id'] or int(value['decision']['passed']) != row['passed']:
                raise ValueError('FILTER_AUDIT_METADATA_MISMATCH:'+str(row['audit_id']))
            rules = value.pop('rules')
            cached = next((digest for previous, digest in versions if previous == rules), None)
            if cached is None:
                digest = filter_audit.save_rules(db, rules)
                # Equality reuses only an exactly identical parsed version. Keep
                # a bounded cache, local to this transaction and this database.
                versions.insert(0, (rules, digest))
                del versions[8:]
            else:
                digest = cached
            compact = filter_audit.encoded(value)
            db.execute('UPDATE filter_audit SET snapshot_json=?,rules_sha256=? WHERE audit_id=?',
                       (compact, digest, row['audit_id']))
            stored = db.execute('SELECT * FROM filter_audit WHERE audit_id=?', (row['audit_id'],)).fetchone()
            if reconstructed_fingerprint(db, stored, canonical_rules) != row['fingerprint']:
                raise ValueError('FILTER_AUDIT_RECONSTRUCTION_MISMATCH')
            for key in ('audit_id', 'item_id', 'fingerprint', 'created_at', 'passed'):
                if stored[key] != row[key]:
                    raise ValueError('FILTER_AUDIT_IDENTITY_CHANGED')
            saved_bytes += len(original.encode('utf-8')) - len(compact.encode('utf-8'))
        db.execute(trigger[0])
        db.commit()
        return {'count': len(rows), 'after_id': rows[-1]['audit_id'], 'saved_payload_bytes': saved_bytes}
    except BaseException:
        db.rollback()
        raise


def reconstructed_fingerprint(db, row, rules_cache):
    """Hash the exact canonical full snapshot without reserializing shared rules.

    Dictionary bytes are verified once per invocation of the full verifier (or
    batch). The root object is streamed with the same ordering and separators as
    json.dumps(ensure_ascii=False, sort_keys=True); no history is omitted.
    """
    value = json.loads(row['snapshot_json'])
    digest = row['rules_sha256']
    if not digest:
        return filter_audit.checksum(filter_audit.encoded(value))
    if 'rules' in value:
        raise ValueError('FILTER_AUDIT_AMBIGUOUS_RULES')
    if digest not in rules_cache:
        saved = db.execute('SELECT content_json FROM filter_rule_snapshots WHERE sha256=?', (digest,)).fetchone()
        if saved is None or filter_audit.checksum(saved[0]) != digest:
            raise ValueError('FILTER_RULE_SNAPSHOT_MISSING_OR_CORRUPT')
        rules_cache[digest] = saved[0].encode('utf-8')
    result = hashlib.sha256()
    result.update(b'{')
    for index, key in enumerate(sorted([*value, 'rules'])):
        if index:
            result.update(b', ')
        result.update(filter_audit.encoded(key).encode('utf-8'))
        result.update(b': ')
        result.update(rules_cache[digest] if key == 'rules' else filter_audit.encoded(value[key]).encode('utf-8'))
    result.update(b'}')
    return result.hexdigest()


def verify(db, *, progress=None):
    count = 0
    cache = {}
    for row in db.execute('SELECT * FROM filter_audit ORDER BY audit_id'):
        value = json.loads(row['snapshot_json'])
        if reconstructed_fingerprint(db, row, cache) != row['fingerprint']:
            raise ValueError('FILTER_AUDIT_FINGERPRINT_MISMATCH:'+str(row['audit_id']))
        if value['item_id'] != row['item_id'] or int(value['decision']['passed']) != row['passed']:
            raise ValueError('FILTER_AUDIT_METADATA_MISMATCH:'+str(row['audit_id']))
        count += 1
        if progress and count % 5000 == 0:
            progress({'verified': count})
    for name in ('filter_audit_no_update', 'filter_audit_no_delete',
                 'filter_rule_snapshots_no_update', 'filter_rule_snapshots_no_delete'):
        if not db.execute("SELECT 1 FROM sqlite_master WHERE type='trigger' AND name=?", (name,)).fetchone():
            raise ValueError('IMMUTABILITY_GUARD_MISSING:'+name)
    if db.execute('PRAGMA foreign_key_check(filter_audit)').fetchone():
        raise ValueError('FILTER_AUDIT_REFERENCES_FAILED')
    return {'verified': count, 'rules_versions': len(cache)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['compact', 'verify'])
    parser.add_argument('--database', required=True)
    parser.add_argument('--batch-size', type=int, default=100)
    args = parser.parse_args()
    from pathlib import Path
    db = sqlite3.connect(Path(args.database).resolve().as_uri()+'?mode=rw', uri=True, timeout=30)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA foreign_keys=ON')
    emit = lambda value: print(json.dumps(value), flush=True)
    try:
        if args.action == 'compact':
            total = saved = after = 0
            while True:
                batch = compact_batch(db, batch_size=args.batch_size, after_id=after)
                if not batch['count']:
                    break
                total += batch['count']; saved += batch['saved_payload_bytes']; after = batch['after_id']
                if total % 1000 < args.batch_size:
                    emit({'migrated': total, 'saved_payload_bytes': saved, 'after_id': after})
                db.execute('PRAGMA wal_checkpoint(PASSIVE)')
                time.sleep(0.05)
            emit({'complete': True, 'migrated': total, 'saved_payload_bytes': saved})
        else:
            emit(verify(db, progress=emit))
    finally:
        db.close()


if __name__ == '__main__':
    main()
