"""Durable material checkpoints and safe operational stage state.

Artifacts contain read evidence and validated results, never credentials or API
settings. A changed material/rule/history dependency invalidates continuation.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from .runtime import cache_key, stamp

STAGES = ('intake', 'screening', 'reading', 'analysis', 'drafting', 'gate', 'delivery')
LABELS = dict(zip(STAGES, ('Приём', 'Тематический фильтр', 'Чтение материала',
                         'Факты и черновик', 'Оформление или исправление поста',
                         'Проверка текста', 'Отправка и квитанция')))
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
    return cache_key('draft-continuation-v2', {
        'material': dict(row), 'history': _editor_history_revision(db, item),
        'rules': __import__('newsroom.policy', fromlist=['stage_signature']).stage_signature('analysis', settings),
        'topic': settings.get('_topic_registry'), 'filter': FILTER_VERSION,
        'model': settings.get('model'), 'memory_mode': settings.get('memory_mode'),
    })


def save_draft_context(db, item_id, item, settings, context):
    # Only the explicit post-finalization context enters the artifact; settings
    # are supplied afresh by the caller after dependency validation.
    from .policy import stage_signature
    context['_draft_policy_signature'] = stage_signature('drafting', settings)
    put(db, item_id, 'analysis', draft_dependency(db, item_id, item, settings), context)
    mark(db, item_id, 'analysis', 'DONE', 'Событие и доказательства проверены.')
    if (context.get('ai_result') or {}).get('_needs_post_draft'):
        mark(db, item_id, 'drafting', 'READY', 'Ожидает написания по сохранённому анализу.')
    elif (context.get('ai_result') or {}).get('_validation_pending'):
        mark(db, item_id, 'drafting', 'DONE', 'Черновик подготовлен вместе с разбором; ожидает проверки текста.')
    else:
        mark(db, item_id, 'drafting', 'DONE', 'Текст сохранён.')
    db.commit()


def load_draft_context(db, item_id, item, settings):
    from .policy import stage_signature
    context = get(db, item_id, 'analysis', draft_dependency(db, item_id, item, settings))
    if context and context.get('_draft_policy_signature') != stage_signature('drafting', settings):
        decision = context['ai_result']
        decision['_needs_post_draft'] = True
        decision['_validation_pending'] = False
        decision.pop('final_text_check', None)
    return context


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


def transport_retry(db, item_id, reason, *, now=None):
    """Bound external failures independently of editorial retries, per revision/stage."""
    from datetime import timedelta
    now = now or datetime.now(timezone.utc)
    rows = snapshot(db, item_id)
    stage = rows[-1]['stage'] if rows else 'reading'
    key = f'transport_retry:{item_id}:{revision(db, item_id)}:{stage}'
    row = db.execute('SELECT value FROM app_state WHERE key=?', (key,)).fetchone()
    prior = json.loads(row[0]) if row else {}
    failures = int(prior.get('failures', 0)) + 1
    recovery = db.execute('SELECT value FROM app_state WHERE key=?',
                          (f'technical_recovery:{item_id}:{revision(db, item_id)}',)).fetchone()
    baseline = json.loads(recovery[0]).get('transport_baselines', {}).get(stage, 0) if recovery else 0
    episode_failures = failures - int(baseline)
    delay = (30, 120, 300)[min(max(episode_failures - 1, 0), 2)]
    state = {'failures': failures, 'episode_failures': episode_failures, 'stage': stage, 'reason': reason,
             'next_at': (now + timedelta(seconds=delay)).isoformat()}
    db.execute('INSERT INTO app_state(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
               (key, json.dumps(state, ensure_ascii=False)))
    return episode_failures > 3, delay


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
    if result.get('_combined_editor') and result.get('reason'):
        questions.append(str(result['reason'])[:400])
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


def recover_technical(db, config, item_id, evidence):
    """Explicit, evidenced recovery; retain editorial counts and old failure totals."""
    from .core import _saved_material
    from .workflow import enqueue
    import uuid
    if not isinstance(evidence, str) or not 10 <= len(evidence.strip()) <= 2000:
        raise ValueError('RECOVERY_EVIDENCE_REQUIRED')
    db.commit()
    db.execute('BEGIN IMMEDIATE')
    try:
        if db.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
            raise ValueError('DATABASE_INTEGRITY_FAILED')
        item = db.execute('SELECT * FROM items WHERE item_id=?', (item_id,)).fetchone()
        if not item or item['disposition'] != 'TECHNICAL_ERROR':
            raise ValueError('RECOVERY_REQUIRES_TECHNICAL_ERROR')
        if db.execute("SELECT 1 FROM processing_jobs WHERE item_id=? AND status='RUNNING'", (item_id,)).fetchone():
            raise ValueError('RECOVERY_JOB_RUNNING')
        if db.execute("SELECT 1 FROM publication_attempts a JOIN posts p USING(post_id) WHERE p.origin_item_id=? AND a.status IN ('SENDING','UNKNOWN','SENT','CONFIRMED')", (item_id,)).fetchone():
            raise ValueError('RECOVERY_REQUIRES_DELIVERY_RECONCILIATION')
        source = db.execute('SELECT * FROM sources WHERE source_id=?', (item['source_id'],)).fetchone()
        record = {'at': stamp(), 'evidence': evidence.strip(), 'item_id': item_id,
                  'revision': item['ingest_revision'], 'previous_processed_at': item['processed_at'],
                  'transport_baselines': {}}
        for stage in STAGES:
            key = f"transport_retry:{item_id}:{item['ingest_revision']}:{stage}"
            old = db.execute('SELECT value FROM app_state WHERE key=?', (key,)).fetchone()
            if old:
                record['transport_baselines'][stage] = json.loads(old[0]).get('failures', 0)
        recovery_key = f"technical_recovery:{item_id}:{item['ingest_revision']}"
        db.execute('INSERT INTO app_state(key,value) VALUES(?,?)',
                   (recovery_key + ':' + uuid.uuid4().hex, json.dumps(record, ensure_ascii=False)))
        db.execute('INSERT INTO app_state(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                   (recovery_key, json.dumps(record, ensure_ascii=False)))
        db.execute("UPDATE items SET disposition='PENDING',processed_at=NULL WHERE item_id=?", (item_id,))
        settings = config.get('newsroom', {})
        enqueue(db, item_id, _saved_material(item, source), source,
                {'threshold': settings.get('similarity_threshold', .35),
                 'max_length': settings.get('max_post_length', 3500),
                 'freshness_hours': settings.get('freshness_window_hours', 24),
                 'initial_backfill_minutes': None, 'relevance_terms': []}, category='retry')
        return record
    except Exception:
        db.rollback()
        raise
