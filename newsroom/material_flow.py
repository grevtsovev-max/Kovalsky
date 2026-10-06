"""Durable material checkpoints and safe operational stage state.

Artifacts contain read evidence and validated results, never credentials or API
settings. A changed material/rule/history dependency invalidates continuation.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from .runtime import cache_key, stamp

STAGES = ('intake', 'screening', 'reading', 'analysis', 'drafting', 'gate', 'delivery')
LABELS = dict(zip(STAGES, ('Приём', 'Предварительный отбор', 'Чтение материала',
                         'Анализ события', 'Написание', 'Допуск', 'Отправка')))
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


def draft_dependency(db, item_id, item, settings):
    from .ai import _load_editorial_rules
    from .core import _editor_history_revision, FILTER_VERSION
    row = db.execute('SELECT content_hash,primary_source_json,story_id FROM items WHERE item_id=?', (item_id,)).fetchone()
    return cache_key('draft-continuation-v1', {
        'material': dict(row), 'history': _editor_history_revision(db, item),
        'rules': _load_editorial_rules(settings), 'topic': settings.get('_topic_registry'),
        'editorial': settings.get('_editorial_registry'), 'filter': FILTER_VERSION,
        'model': settings.get('model'), 'memory_mode': settings.get('memory_mode'),
    })


def save_draft_context(db, item_id, item, settings, context):
    # Only the explicit post-finalization context enters the artifact; settings
    # are supplied afresh by the caller after dependency validation.
    put(db, item_id, 'analysis', draft_dependency(db, item_id, item, settings), context)
    mark(db, item_id, 'analysis', 'DONE', 'Событие и доказательства проверены.')
    if (context.get('ai_result') or {}).get('_needs_post_draft'):
        mark(db, item_id, 'drafting', 'READY', 'Ожидает написания по сохранённому анализу.')
    else:
        mark(db, item_id, 'drafting', 'DONE', 'Текст сохранён.')
    db.commit()


def load_draft_context(db, item_id, item, settings):
    return get(db, item_id, 'analysis', draft_dependency(db, item_id, item, settings))


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


def technical_error(exc):
    import sqlite3
    return isinstance(exc, (TypeError, AttributeError, KeyError, NameError, sqlite3.DatabaseError))


def finish_attempt(db, item_id, outcome, item):
    rows = snapshot(db, item_id)
    stage = rows[-1]['stage'] if rows else 'screening'
    reason = item.get('_retry_reason') or (item.get('_audit_trace') or [{}])[-1].get('reason', '')
    if outcome == 'TECHNICAL_ERROR':
        mark(db, item_id, stage, 'ERROR', reason, block_kind='technical')
        return
    if outcome in {'AI_RETRY', 'PRIMARY_RETRY', 'WAITING_CONFIRMATION'}:
        kind = item.get('_flow_block_kind') or ('capacity' if item.get('_retry_without_count') or item.get('_source_search_deferred') else 'evidence' if outcome == 'PRIMARY_RETRY' else 'verification')
        retry = db.execute('SELECT value FROM app_state WHERE key=?', (f'selection_retry:{item_id}',)).fetchone()
        state = json.loads(retry[0]) if retry else {}
        mark(db, item_id, stage, 'WAITING', reason, block_kind=kind, next_at=state.get('next_at'))
        db.execute('UPDATE material_stage_state SET checks=? WHERE item_id=? AND revision=? AND stage=?',
                   (int(state.get('attempts', 0)), item_id, revision(db, item_id), stage))
    elif outcome in {'NEW_STORY', 'UPDATE_CANDIDATE'}:
        mark(db, item_id, 'gate', 'READY', 'Ожидает полного допуска непосредственно перед отправкой.')
    else:
        mark(db, item_id, stage, 'CLOSED', reason or 'Материал завершён без публикации.')


def migrate(db):
    """Preserve legacy attempts/evidence; do not invent successful checkpoints."""
    if db.execute("SELECT 1 FROM app_state WHERE key='material_flow_migrated_v1'").fetchone():
        return
    for row in db.execute("SELECT * FROM items WHERE disposition IN ('PENDING','PRIMARY_RETRY','AI_RETRY','WAITING_CONFIRMATION')").fetchall():
        item_id = row['item_id']
        retry_row = db.execute('SELECT value FROM app_state WHERE key=?', (f'selection_retry:{item_id}',)).fetchone()
        state = json.loads(retry_row[0]) if retry_row else {}
        legacy = db.execute('SELECT value FROM app_state WHERE key=?', (f'editor_retry:{item_id}',)).fetchone()
        state['attempts'] = max(int(state.get('attempts', 0)), int(legacy[0]) if legacy else 0)
        if row['disposition'] != 'PENDING':
            state.setdefault('outcome', row['disposition'])
            state.setdefault('next_at', stamp())
            db.execute('INSERT INTO app_state(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                       (f'selection_retry:{item_id}', json.dumps(state, ensure_ascii=False)))
        mark(db, item_id, 'intake', 'DONE', 'Материал перенесён с сохранением истории.')
        try:
            source = json.loads(row['primary_source_json'] or '{}')
        except (TypeError, ValueError):
            source = {}
        read = source.get('status') == 'READ' or source.get('_material_read') is True
        if read:
            mark(db, item_id, 'reading', 'DONE', 'Сохранён ранее прочитанный материал.')
        stage = 'screening' if row['disposition'] == 'PENDING' else 'reading' if not read else 'analysis'
        mark(db, item_id, stage, 'READY', state.get('reason') or 'Продолжение с сохранёнными доказательствами.', next_at=state.get('next_at'))
        db.execute('UPDATE material_stage_state SET checks=? WHERE item_id=? AND revision=? AND stage=?',
                   (state['attempts'], item_id, revision(db, item_id), stage))
    db.execute("INSERT INTO app_state(key,value) VALUES('material_flow_migrated_v1',?)", (stamp(),))
    db.commit()


def verification_questions(result):
    questions = []
    for key in ('source_review_issues', 'memory_issues', 'editorial_issues'):
        values = result.get(key)
        questions.extend(str(x)[:400] for x in (values if isinstance(values, list) else []) if isinstance(x, str))
    date = result.get('development_date_check') or {}
    if date.get('reason'):
        questions.append(str(date['reason'])[:400])
    if result.get('independent_check_required'):
        questions.append('Как разрешается расхождение между сообщениями источников?')
    if result.get('source_review_required') and not questions:
        questions.append('Какая дословная выдержка из прочитанного материала подтверждает центральное событие и его статус?')
    return list(dict.fromkeys(questions))
