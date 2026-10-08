from __future__ import annotations


import json


from datetime import datetime, timezone


from .runtime import cache_key, stamp

STAGES = ('intake', 'screening', 'reading')
LABELS = dict(zip(STAGES, ('Приём', 'Тематический фильтр', 'Чтение материала')))


SCHEMA = """
CREATE TABLE IF NOT EXISTS material_stage_results (
 item_id INTEGER NOT NULL REFERENCES items(item_id), revision TEXT NOT NULL,
 stage TEXT NOT NULL, dependency TEXT NOT NULL, result_json TEXT NOT NULL,
 created_at TEXT NOT NULL,
 PRIMARY KEY(item_id,revision,stage,dependency)
);
CREATE TABLE IF NOT EXISTS material_stage_state (
 item_id INTEGER NOT NULL REFERENCES items(item_id), revision TEXT NOT NULL,
 stage TEXT NOT NULL, status TEXT NOT NULL, reason TEXT NOT NULL DEFAULT '',
 block_kind TEXT, checks INTEGER NOT NULL DEFAULT 0, next_at TEXT,
 started_at TEXT, updated_at TEXT NOT NULL, work_seconds REAL NOT NULL DEFAULT 0,
 wait_seconds REAL NOT NULL DEFAULT 0,
 PRIMARY KEY(item_id,revision,stage)
);
"""


def revision(db, item_id):
    row = db.execute('SELECT ingest_revision FROM items WHERE item_id=?', (item_id,)).fetchone()
    return row[0] if row else None



def mark(db, item_id, stage, status, reason='', *, block_kind=None, next_at=None):
    if stage not in STAGES:
        raise ValueError('UNKNOWN_MATERIAL_STAGE')
    rev = revision(db, item_id)
    if rev is None:
        return
    now = stamp()
    old = db.execute('SELECT * FROM material_stage_state WHERE item_id=? AND revision=? AND stage=?',
                     (item_id, rev, stage)).fetchone()
    work = float(old['work_seconds']) if old else 0.0
    wait = float(old['wait_seconds']) if old else 0.0
    if old:
        elapsed = max(0, (datetime.now(timezone.utc) - datetime.fromisoformat(old['updated_at'])).total_seconds())
        if old['status'] == 'RUNNING':
            work += elapsed
        elif old['status'] in {'WAITING', 'READY'}:
            wait += elapsed
    db.execute('INSERT INTO material_stage_state(item_id,revision,stage,status,reason,block_kind,next_at,started_at,updated_at,work_seconds,wait_seconds) '
               'VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(item_id,revision,stage) DO UPDATE SET '
               'status=excluded.status,reason=excluded.reason,block_kind=excluded.block_kind,next_at=excluded.next_at,'
               'started_at=COALESCE(material_stage_state.started_at,excluded.started_at),updated_at=excluded.updated_at,'
               'work_seconds=excluded.work_seconds,wait_seconds=excluded.wait_seconds',
               (item_id, rev, stage, status, reason[:800], block_kind, next_at,
                now if status == 'RUNNING' else None, now, work, wait))



def put(db, item_id, stage, dependency, result):
    rev = revision(db, item_id)
    if rev is None:
        return
    db.execute('INSERT INTO material_stage_results VALUES(?,?,?,?,?,?) ON CONFLICT(item_id,revision,stage,dependency) DO UPDATE SET result_json=excluded.result_json,created_at=excluded.created_at',
               (item_id, rev, stage, dependency, json.dumps(result, ensure_ascii=False), stamp()))



def get(db, item_id, stage, dependency):
    row = db.execute('SELECT result_json FROM material_stage_results WHERE item_id=? AND revision=? AND stage=? AND dependency=?',
                     (item_id, revision(db, item_id), stage, dependency)).fetchone()
    return json.loads(row[0]) if row else None



def snapshot(db, item_id):
    rev = revision(db, item_id)
    rows = [dict(row) | {'label': LABELS[row['stage']]} for row in db.execute(
        'SELECT stage,status,reason,block_kind,checks,next_at,started_at,updated_at,work_seconds,wait_seconds '
        'FROM material_stage_state WHERE item_id=? AND revision=? ORDER BY updated_at', (item_id, rev))]
    for row in rows:
        elapsed = max(0, (datetime.now(timezone.utc)-datetime.fromisoformat(row['updated_at'])).total_seconds())
        if row['status'] == 'RUNNING':
            row['work_seconds'] += elapsed
        elif row['status'] in {'READY','WAITING','ERROR'}:
            row['wait_seconds'] += elapsed
    return rows


