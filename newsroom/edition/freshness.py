"""Source age admission; missing dates remain publishable, archives remain searchable."""
from datetime import datetime, timedelta, timezone
from . import store

MAX_AGE_HOURS = 48
SCHEMA = """
CREATE TABLE IF NOT EXISTS edition_date_admissions (
 material_id INTEGER PRIMARY KEY REFERENCES edition_materials(material_id),
 decision TEXT NOT NULL, details_json TEXT NOT NULL, created_at TEXT NOT NULL
);
"""


def date(value):
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        # A timezone-free timestamp cannot establish a precise age.
        return parsed.astimezone(timezone.utc) if parsed.tzinfo else None
    except (ValueError, TypeError, AttributeError, OverflowError):
        return None


def assess(material):
    published = date(material.get('published_at'))
    received = date(material.get('received_at'))
    decision = ('STALE' if received - published > timedelta(hours=MAX_AGE_HOURS) else 'FRESH') if published and received else 'UNKNOWN'
    return {'decision': decision, 'published_at': material.get('published_at'),
            'received_at': material.get('received_at'), 'max_age_hours': MAX_AGE_HOURS,
            'reason': {'STALE': 'Дата источника старше 48 часов на момент получения; материал сохранён только для контекста.',
                       'FRESH': 'Дата источника в пределах 48 часов на момент получения.',
                       'UNKNOWN': 'Дата источника неизвестна или не распознана; выпуск разрешён.'}[decision]}


def record(db, material):
    result = assess(material)
    db.execute('INSERT OR IGNORE INTO edition_date_admissions VALUES(?,?,?,?)',
               (material['material_id'], result['decision'], store.encoded(result), store.stamp()))
    return result


def admit(db, materials):
    allowed, excluded = [], []
    for material in materials:
        result = record(db, material)
        if result['decision'] == 'STALE':
            excluded.append({'material_id': material['material_id'], 'kind': 'out_of_scope',
                             'code': 'source_age', **result})
        else:
            allowed.append(material)
    return allowed, excluded


def queued(db):
    rows = db.execute("SELECT material_id FROM edition_materials WHERE queue_state='QUEUED'").fetchall()
    for material in store.materials(db, [r[0] for r in rows]):
        if record(db, material)['decision'] == 'STALE':
            db.execute("UPDATE edition_materials SET queue_state='ARCHIVE',eligible=0 WHERE material_id=?", (material['material_id'],))


def protect_document(db, document):
    """Retire never-sent stale drafts; fresh origins of mixed drafts are replanned."""
    if document['state'] in ('PUBLISHED', 'SENDING', 'UNKNOWN', 'FILTERED', 'DUPLICATE', 'COVERED'):
        return document['state'] != 'FILTERED'
    intent = db.execute("SELECT 1 FROM publication_attempts WHERE post_id=? OR delivery_key LIKE '%:edition:'||?",
                        (document['post_id'], document['document_id'])).fetchone()
    if intent:
        return True  # Preserve every durable delivery intent; gate still checks age.
    ids = set(store.read(document['group_json'], {}).get('material_ids', []))
    origins = [m for m in store.read(document['materials_json'], []) if m['material_id'] in ids]
    allowed, excluded = admit(db, origins)
    if not excluded:
        return True
    reasons = store.read(document['reasons_json'], []) + excluded
    db.execute("UPDATE edition_documents SET state='FILTERED',reasons_json=?,updated_at=? WHERE document_id=?",
               (store.encoded(reasons), store.stamp(), document['document_id']))
    for material in allowed:
        # The discarded mixed draft must not swallow its fresh news.
        db.execute("UPDATE edition_materials SET queue_state='QUEUED' WHERE material_id=?", (material['material_id'],))
    for entry in excluded:
        db.execute("UPDATE edition_materials SET queue_state='ARCHIVE',eligible=0 WHERE material_id=?", (entry['material_id'],))
    store.event(db, document['job_id'], 'source_age', {'excluded': excluded, 'requeued': [m['material_id'] for m in allowed]}, document['document_id'])
    return False
