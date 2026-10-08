"""Three newsroom roles, bounded external workers and a single state coordinator.

Generators execute database logic on the coordinator's thread and yield only
external work. Workers never receive the coordinator's SQLite connection and
cannot update stories, create posts, or send Telegram messages.
"""
from __future__ import annotations

import copy
import math
import inspect
import json
import time
import uuid
from contextlib import nullcontext
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from .runtime import SCOPE, cache_key, stamp

SCHEMA = """
CREATE TABLE IF NOT EXISTS processing_jobs (
 job_id INTEGER PRIMARY KEY, item_id INTEGER NOT NULL REFERENCES items(item_id),
 revision TEXT NOT NULL, category TEXT NOT NULL DEFAULT 'fresh',
 role TEXT NOT NULL DEFAULT 'collector', status TEXT NOT NULL DEFAULT 'PENDING'
 CHECK(status IN ('PENDING','RUNNING','WAITING','DONE','SUPERSEDED')),
 payload_json TEXT NOT NULL, priority INTEGER NOT NULL DEFAULT 1,
 owner TEXT, lease_until TEXT, attempts INTEGER NOT NULL DEFAULT 0,
 created_at TEXT NOT NULL, started_at TEXT, next_at TEXT NOT NULL,
 finished_at TEXT, outcome TEXT, error_code TEXT,
 UNIQUE(item_id,revision,category)
);
CREATE INDEX IF NOT EXISTS processing_jobs_due_idx ON processing_jobs(status,next_at);
CREATE UNIQUE INDEX IF NOT EXISTS processing_jobs_active_item ON processing_jobs(item_id) WHERE status='RUNNING';
CREATE TABLE IF NOT EXISTS processing_job_events (
 event_id INTEGER PRIMARY KEY, job_id INTEGER NOT NULL REFERENCES processing_jobs(job_id),
 role TEXT NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS processing_job_events_job_idx ON processing_job_events(job_id,event_id DESC);
CREATE TRIGGER IF NOT EXISTS processing_job_events_no_update BEFORE UPDATE ON processing_job_events
BEGIN SELECT RAISE(ABORT, 'processing history is append only'); END;
CREATE TRIGGER IF NOT EXISTS processing_job_events_no_delete BEFORE DELETE ON processing_job_events
BEGIN SELECT RAISE(ABORT, 'processing history is append only'); END;
"""


@dataclass
class Work:
    role: str
    function: object
    args: tuple = ()
    kwargs: dict = field(default_factory=dict)
    key: str | None = None
    ttl: int = 0
    stage: str | None = None

    def execute(self, runtime=None, scope=None):
        if runtime:
            from .agent_control import require_enabled
            control = runtime.settings.get('_agent_control_config') or {'newsroom': {'database': runtime.database}}
            require_enabled(control)
        from .resources import FUNCTION_STAGES
        stage = self.stage or FUNCTION_STAGES.get(getattr(self.function, '__name__', ''), 'unattributed')
        measurement = runtime.measure(stage, self.role, scope) if runtime else nullcontext({})
        with measurement as measured:
            return self._execute(runtime, scope, measured)

    def _execute(self, runtime, scope, measured):
        token = SCOPE.set({**SCOPE.get(), **(scope or {}), "role": self.role, "runtime": runtime})
        try:
            stage_name = 'drafting' if getattr(self.function, '__name__', '') in {'draft_post', 'validate_draft'} else 'screening' if self.role == 'filter' else 'analysis' if self.role == 'editor' else 'reading'
            if runtime and self.key:
                cached = runtime.cached(self.key, self.role)
                if cached is None and SCOPE.get().get('item_id') is not None:
                    from .material_flow import get
                    with runtime.db() as db:
                        if db.execute("SELECT 1 FROM sqlite_master WHERE name='material_stage_results'").fetchone():
                            cached = get(db, SCOPE.get()['item_id'], stage_name, self.key)
                if cached is not None and (self.role != 'collector' or readable_result(cached)):
                    measured['status'] = 'CACHED'
                    return cached
            result = self.function(*self.args, **self.kwargs)
            # Only completed and validated stage outputs reach this point;
            # raised errors, incomplete API responses and missing results do not.
            readable = self.role != 'collector' or readable_result(result)
            structured = self.role != 'editor' or (isinstance(result, dict) and
                result.get('action') in {'NEW_STORY', 'UPDATE', 'DUPLICATE', 'NOISE'}
                and result.get('publication_recommendation') != 'WAIT_FOR_AUTOMATION')
            if getattr(self.function, '__name__', '') == 'validate_draft':
                structured = isinstance(result, dict) and isinstance(result.get('issues'), list)
            if self.role == 'filter':
                structured = isinstance(result, dict) and result.get('decision') in {'KEEP','NOISE','DUPLICATE'}
            if getattr(self.function, '__name__', '') == 'draft_post':
                structured = isinstance(result, dict) and set(result) == {'headline_ru','summary_ru','what_is_new','editorial_check'}
            if runtime and self.key and result is not None and readable and structured:
                runtime.store(self.key, self.role, result, self.ttl)
                if SCOPE.get().get('item_id') is not None:
                    from .material_flow import put, revision
                    with runtime.db() as db:
                        if (db.execute("SELECT 1 FROM sqlite_master WHERE name='material_stage_results'").fetchone()
                                and (not SCOPE.get().get('revision') or revision(db, SCOPE.get()['item_id']) == SCOPE.get()['revision'])):
                            put(db, SCOPE.get()['item_id'], stage_name, self.key, result)
            return result
        finally:
            SCOPE.reset(token)


def readable_result(result):
    if not isinstance(result, dict):
        return False
    material = result.get('item') or result
    if not isinstance(material, dict):
        return False
    text = material.get('content') or result.get('body') or material.get('primary_source_content') or ''
    return (bool(str(text).strip()) and
            (material.get('material_read') is True or material.get('primary_source_status') == 'READ'))


def drive(generator, runtime=None, scope=None):
    """The same role contracts serve synchronous manual intake and retries."""
    value = None
    error = None
    while True:
        token = SCOPE.set({**(scope or {}), "runtime": runtime})
        try:
            work = generator.throw(error) if error else generator.send(value)
        except StopIteration as done:
            return done.value
        finally:
            SCOPE.reset(token)
        error = None
        try:
            value = work.execute(runtime, scope)
        except Exception as exc:
            error = exc


def resolve_steps(value):
    """Allow a role adapter to return a direct decision or external work steps."""
    if inspect.isgenerator(value):
        return (yield from value)
    return value


def enqueue(db, item_id, item, source, options, *, category="fresh"):
    from .core import _save_item
    if item_id is None:
        item_id = _save_item(db, source, item)
        if item_id is None:
            return None
    revision = cache_key("material-version", {key: item.get(key) for key in (
        "url", "title", "description", "content", "author", "published_at")})
    if item_id is not None:
        db.execute("UPDATE items SET ingest_revision=? WHERE item_id=? AND ingest_revision=''", (revision, item_id))
        revision = db.execute('SELECT ingest_revision FROM items WHERE item_id=?', (item_id,)).fetchone()[0]
    payload = {"item": item, "source": dict(source), "options": options}
    db.execute("UPDATE processing_jobs SET status='SUPERSEDED',finished_at=? "
               "WHERE item_id=? AND revision<>? AND status IN ('PENDING','WAITING','RUNNING')", (stamp(), item_id, revision))
    db.execute("INSERT INTO processing_jobs(item_id,revision,category,payload_json,priority,created_at,next_at) "
               "VALUES(?,?,?,?,?,?,?) ON CONFLICT(item_id,revision,category) DO UPDATE SET status='PENDING',"
               "payload_json=excluded.payload_json,next_at=excluded.next_at,finished_at=NULL,outcome=NULL "
               "WHERE processing_jobs.status='DONE'",
               (item_id, revision, category, json.dumps(payload, ensure_ascii=False),
                source["priority"], stamp(), stamp()))
    from .material_flow import mark
    mark(db, item_id, 'intake', 'DONE', 'Входной материал сохранён.')
    db.commit()
    return item_id


class CollectionCoordinator:
    """Collection persists inputs; the independent processor owns all work."""
    running = {}
    def tick(self): pass
    def close(self): pass
    def abort(self): pass


class StageExecutor:
    """Separate bounded queues: blocked article readers cannot occupy editors."""
    def __init__(self, workers):
        import threading
        self.stopping = threading.Event()
        self.pools = {name: ThreadPoolExecutor(max_workers=workers, thread_name_prefix='newsroom-'+name)
                      for name in ('reading', 'screening', 'analysis', 'drafting')}

    def submit(self, function, *args):
        work = function.__self__
        name = getattr(work.function, '__name__', '')
        stage = 'drafting' if name in {'draft_post', 'validate_draft'} else 'screening' if work.role == 'filter' else 'analysis' if name in {'analyze', 'analyze_with_ai'} else 'reading'
        def invoke():
            runtime, scope = args
            def record(status, reason='', **details):
                if runtime and scope.get('item_id') is not None:
                    from .material_flow import mark, revision
                    with runtime.db() as db:
                        if scope.get('revision') is None or revision(db, scope['item_id']) == scope['revision']:
                            mark(db, scope['item_id'], stage, status, reason, **details)
            from .runtime import BudgetDeferred
            if self.stopping.is_set():
                raise BudgetDeferred('shutdown', 1)
            try:
                record('RUNNING')
                return function(*args)
            except BudgetDeferred as exc:
                record('WAITING', exc.user_reason, block_kind=exc.block_kind,
                       next_at=(datetime.now(timezone.utc) + timedelta(seconds=exc.delay_seconds)).isoformat())
                # The coordinator persists the wait. Never hold a worker in a
                # polling loop while another material could use its stage.
                raise
        return self.pools[stage].submit(invoke)

    def shutdown(self, wait=True):
        self.stopping.set()
        for pool in self.pools.values():
            pool.shutdown(wait=wait)


class Coordinator:
    def __init__(self, db, config, counts, *, categories=('fresh',), max_jobs=None):
        self.db, self.config, self.counts = db, config, counts
        self.runtime = config.get("ai", {}).get("_runtime")
        lane_workers = max(1, min(8, int(config.get("newsroom", {}).get("processing_workers", 2))))
        self.continuous = bool(config.get('_continuous_processing'))
        self.workers = min(16, lane_workers * 4) if self.continuous else lane_workers
        self.pool = StageExecutor(lane_workers) if self.continuous else ThreadPoolExecutor(max_workers=self.workers, thread_name_prefix="newsroom-stage")
        self.running = {}
        self.owner = uuid.uuid4().hex
        self.started = time.perf_counter()
        self.budget_seconds = max(5, int(config.get("newsroom", {}).get("processing_cycle_seconds", 150)))
        self.stop_admission = False
        self.categories = categories
        self.max_jobs = max_jobs
        self.claimed = 0
        self.closed = False
        self._resume_expired()
        self._seed_pending()

    def _seed_pending(self):
        from .core import _saved_material
        rows = self.db.execute("SELECT i.* FROM items i JOIN sources s USING(source_id) WHERE i.disposition='PENDING' "
                               "AND NOT EXISTS(SELECT 1 FROM processing_jobs j WHERE j.item_id=i.item_id "
                               "AND j.revision=i.ingest_revision AND j.status<>'SUPERSEDED') LIMIT 1000").fetchall()
        for row in rows:
            source = self.db.execute('SELECT * FROM sources WHERE source_id=?', (row['source_id'],)).fetchone()
            settings = self.config.get('newsroom', {})
            enqueue(self.db, row['item_id'], _saved_material(row, source), source, {
                'threshold': settings.get('similarity_threshold', .35), 'max_length': settings.get('max_post_length', 3500),
                'freshness_hours': settings.get('freshness_window_hours', 24), 'initial_backfill_minutes': None,
                'relevance_terms': settings.get('relevance_terms', []),
            })

    def _resume_expired(self):
        self.db.execute("INSERT INTO processing_job_events(job_id,role,status,created_at) "
                        "SELECT job_id,role,'LEASE_EXPIRED',? FROM processing_jobs "
                        "WHERE status='RUNNING' AND lease_until<?", (stamp(), stamp()))
        self.db.execute("UPDATE processing_jobs SET status='PENDING',owner=NULL,lease_until=NULL "
                        "WHERE status='RUNNING' AND lease_until<?", (stamp(),))
        self.db.execute("UPDATE processing_jobs SET status='DONE',finished_at=?,outcome=(SELECT disposition FROM items WHERE items.item_id=processing_jobs.item_id) "
                        "WHERE status IN ('PENDING','WAITING') AND EXISTS(SELECT 1 FROM items i WHERE i.item_id=processing_jobs.item_id "
                        "AND i.disposition IN ('TECHNICAL_ERROR','REJECTED','STALE','UNDATED','BASELINE_SKIPPED','NOISE','DUPLICATE','STORE_ONLY','NEW_STORY','UPDATE_CANDIDATE','AGENT_CORRECTION_QUEUED') "
                        "AND julianday(i.processed_at)>=julianday(processing_jobs.created_at,'-1 second'))", (stamp(),))
        from .material_flow import mark
        terminal = {'REJECTED', 'STALE', 'UNDATED', 'BASELINE_SKIPPED', 'NOISE',
                    'DUPLICATE', 'STORE_ONLY', 'EDITOR_REJECTED', 'TECHNICAL_ERROR'}
        checkpoints = self.db.execute(
            "SELECT s.*,i.disposition FROM material_stage_state s JOIN items i "
            "ON i.item_id=s.item_id AND i.ingest_revision=s.revision "
            "WHERE s.status IN ('RUNNING','READY','WAITING') AND NOT EXISTS "
            "(SELECT 1 FROM processing_jobs j WHERE j.item_id=s.item_id "
            "AND j.revision=s.revision AND j.status='RUNNING')").fetchall()
        for checkpoint in checkpoints:
            item_id, stage = checkpoint['item_id'], checkpoint['stage']
            if checkpoint['disposition'] in terminal:
                technical = checkpoint['disposition'] == 'TECHNICAL_ERROR'
                mark(self.db, item_id, stage, 'ERROR' if technical else 'CLOSED',
                     'Обработка остановлена технической ошибкой.' if technical else 'Материал завершён без публикации.',
                     block_kind='technical' if technical else None)
            elif checkpoint['disposition'] in {'NEW_STORY', 'UPDATE_CANDIDATE'} and stage not in {'gate', 'delivery'}:
                mark(self.db, item_id, stage, 'CLOSED', 'Обработка передана на допуск готового поста.')
            elif checkpoint['status'] == 'RUNNING' and stage not in {'gate', 'delivery'}:
                job = self.db.execute("SELECT status,next_at FROM processing_jobs WHERE item_id=? "
                                      "AND revision=? AND status IN ('PENDING','WAITING') ORDER BY next_at LIMIT 1",
                                      (item_id, checkpoint['revision'])).fetchone()
                mark(self.db, item_id, stage, 'WAITING' if job and job['status'] == 'WAITING' else 'READY',
                     'Предыдущая обработка прервана; ожидает продолжения по сохранённым результатам.',
                     block_kind='recovery', next_at=job['next_at'] if job else None)
        rejected = self.db.execute(
            "SELECT s.item_id,s.stage FROM material_stage_state s JOIN items i "
            "ON i.item_id=s.item_id AND i.ingest_revision=s.revision "
            "JOIN posts p ON p.origin_item_id=i.item_id "
            "WHERE s.stage='gate' AND s.status IN ('READY','WAITING','RUNNING') "
            "AND p.status='REJECTED' AND julianday(p.created_at)>=julianday(i.processed_at,'-1 second') "
            "AND NOT EXISTS (SELECT 1 FROM posts newer WHERE newer.origin_item_id=i.item_id AND newer.post_id>p.post_id)").fetchall()
        for checkpoint in rejected:
            mark(self.db, checkpoint['item_id'], 'gate', 'CLOSED', 'Готовый пост окончательно отклонён проверками допуска.')
        self.db.commit()

    def _event(self, job_id, role, status):
        self.db.execute("INSERT INTO processing_job_events(job_id,role,status,created_at) VALUES(?,?,?,?)",
                        (job_id, role, status, stamp()))

    def _lease(self):
        return (datetime.now(timezone.utc) + timedelta(seconds=120)).isoformat(timespec="microseconds")

    def _claim(self):
        if self.stop_admission or (not self.continuous and time.perf_counter() - self.started >= self.budget_seconds) or (
                self.max_jobs is not None and self.claimed >= self.max_jobs):
            return None
        self.db.commit()
        self.db.execute("BEGIN IMMEDIATE")
        # Aging is a separate FIFO lane, not an arbitrary relevance score.
        marks = ','.join('?' for _ in self.categories)
        preferred = ('WAITING_CONFIRMATION', '', 'PRIMARY_RETRY', '', 'AI_RETRY', '')[self.claimed % 6] if 'retry' in self.categories else ''
        preferred_category = ('fresh', 'fresh', 'retry')[self.claimed % 3] if self.continuous else ''
        if self.continuous and self.claimed % 12 == 10 and 'watch' in self.categories:
            preferred_category = 'watch'
        # Hold a stage preference for a complete category rotation. Using
        # the same modulo for both permanently assigned drafting to fresh
        # slots, so drafting retries never received their intended priority.
        preferred_stage = ('drafting', '', 'analysis', '', 'reading', '', 'screening', '')[(self.claimed // 4) % 8] if self.continuous else ''
        candidates = self.db.execute("SELECT j.*,i.story_id AS current_story_id FROM processing_jobs j JOIN items i ON i.item_id=j.item_id "
                              "JOIN sources s ON s.source_id=i.source_id "
                              "WHERE j.status IN ('PENDING','WAITING') AND j.next_at<=? AND i.disposition<>'TECHNICAL_ERROR' "
                              # Monitoring controls new intake, not accepted work.
                              f"AND j.category IN ({marks}) "
                              "AND NOT EXISTS(SELECT 1 FROM processing_jobs busy WHERE busy.item_id=j.item_id AND busy.status='RUNNING') "
                              # Rotate reading, analysis and confirmation;
                              # every other slot remains oldest-due FIFO.
                              "ORDER BY CASE WHEN j.category=? THEN 0 ELSE 1 END,"
                              "CASE WHEN ?='retry' AND j.category='retry' THEN j.next_at ELSE NULL END,"
                              "CASE WHEN ?='retry' AND j.category='retry' THEN j.created_at ELSE NULL END,"
                              "CASE WHEN ?='retry' AND j.category='retry' THEN j.job_id ELSE NULL END,"
                              "CASE WHEN (SELECT stage FROM material_stage_state st WHERE st.item_id=i.item_id AND st.revision=i.ingest_revision AND st.status IN ('READY','WAITING') ORDER BY updated_at DESC LIMIT 1)=? THEN 0 ELSE 1 END,"
                              "CASE WHEN i.disposition=? THEN 0 ELSE 1 END,"
                              "CASE WHEN julianday(j.created_at)<julianday('now','-10 minutes') THEN 0 ELSE 1 END,"
                              "CASE WHEN julianday(j.created_at)<julianday('now','-10 minutes') THEN j.next_at ELSE NULL END,"
                              "j.priority DESC,j.next_at,j.created_at,j.job_id", (stamp(), *self.categories,
                              preferred_category, preferred_category, preferred_category, preferred_category,
                              preferred_stage, preferred))
        busy = self.db.execute("SELECT j.payload_json,i.story_id FROM processing_jobs j JOIN items i USING(item_id) WHERE j.status='RUNNING'").fetchall()
        def related(candidate):
            from .core import canonicalize, similarity
            item = json.loads(candidate['payload_json'])['item']
            for active in busy:
                other = json.loads(active['payload_json'])['item']
                own_url, other_url = item.get('primary_source_url'), other.get('primary_source_url')
                if own_url and other_url and canonicalize(own_url) == canonicalize(other_url):
                    return True
                own_text = item.get('title', '') + ' ' + item.get('description', '')
                other_text = other.get('title', '') + ' ' + other.get('description', '')
                if own_text and other_text and similarity(own_text, other_text) >= .8:
                    return True
            return False
        row = next((candidate for candidate in candidates if not related(candidate)), None)
        if row:
            self.db.execute("UPDATE processing_jobs SET status='RUNNING',owner=?,lease_until=?,"
                            "started_at=COALESCE(started_at,?),attempts=attempts+1 WHERE job_id=?",
                            (self.owner, self._lease(), stamp(), row["job_id"]))
            self._event(row["job_id"], row["role"], "RUNNING")
            self.claimed += 1
        self.db.commit()
        return dict(row) if row else None

    def _valid(self, job):
        row = self.db.execute("SELECT j.status,j.owner,j.revision,i.ingest_revision FROM processing_jobs j JOIN items i USING(item_id) WHERE job_id=?", (job["job_id"],)).fetchone()
        return row and row["status"] == "RUNNING" and row["owner"] == self.owner and row['revision'] == row['ingest_revision']

    def _finish_job(self, job, status, category, outcome, next_at, finished_at, error_code=None):
        target_id = job['job_id']
        if category != job['category']:
            existing = self.db.execute(
                'SELECT job_id FROM processing_jobs WHERE item_id=? AND revision=? AND category=?',
                (job['item_id'], job['revision'], category)).fetchone()
            if existing:
                # A previous retry lane may already exist for this version.
                # Keep both histories, but only its canonical retry job runnable.
                target_id = existing['job_id']
                self.db.execute("UPDATE processing_jobs SET status='SUPERSEDED',outcome=?,finished_at=?,"
                                "owner=NULL,lease_until=NULL WHERE job_id=?",
                                (outcome, stamp(), job['job_id']))
                self._event(job['job_id'], 'editor', 'SUPERSEDED')
                self.db.execute('UPDATE processing_jobs SET payload_json=? WHERE job_id=?',
                                (job['payload_json'], target_id))
        self.db.execute('UPDATE processing_jobs SET status=?,category=?,outcome=?,next_at=?,finished_at=?,'
                        'error_code=?,owner=NULL,lease_until=NULL WHERE job_id=?',
                        (status, category, outcome, next_at, finished_at, error_code, target_id))
        self._event(target_id, 'editor', status)

    def _advance(self, job, generator, value=None, error=None):
        # Collection has its own connection. Validate under the write lock so
        # another material revision cannot enter midway through a local step.
        self.db.commit()
        self.db.execute('BEGIN IMMEDIATE')
        if not self._valid(job):
            generator.close()
            self.db.execute("UPDATE processing_jobs SET status='SUPERSEDED',finished_at=?,owner=NULL,lease_until=NULL "
                            "WHERE job_id=? AND owner=?", (stamp(), job['job_id'], self.owner))
            self.db.commit()
            return
        try:
            token = SCOPE.set({'item_id': job['item_id'], 'job_id': job['job_id'],
                              'category': job['category'], 'runtime': self.runtime, 'role': 'collector'})
            try:
                work = generator.throw(error) if error else generator.send(value)
            finally:
                SCOPE.reset(token)
        except StopIteration as result:
            outcome = result.value
            self.counts[outcome] = self.counts.get(outcome, 0) + 1
            held = outcome in {"AI_RETRY", "PRIMARY_RETRY", "WAITING_CONFIRMATION"}
            capacity = bool(job.get("_item", {}).get("_retry_without_count"))
            next_at = stamp()
            if held:
                retry = self.db.execute("SELECT value FROM app_state WHERE key=?", (f"selection_retry:{job['item_id']}",)).fetchone()
                if retry:
                    next_at = json.loads(retry[0]).get("next_at") or next_at
                if job.get('_item', {}).get('_history_changed'):
                    next_at = stamp()
                # Ordinary retries keep the established bounded retry lane.
                # Jobs deferred only by capacity are resumed by this queue.
            self._finish_job(job, "WAITING" if held else "DONE",
                             job['category'] if capacity or not held else 'retry',
                             outcome, next_at, None if held else stamp())
            self.db.commit()
            if not self.continuous and held and capacity and job.get('_item', {}).get('_budget_deferred'):
                self.stop_admission = True
            return
        except Exception as exc:
            self.db.rollback()
            self.counts["ERROR"] = self.counts.get("ERROR", 0) + 1
            from .diagnostics import error_location
            self.db.execute("INSERT INTO app_state(key,value) VALUES('diagnostic_last_error',?) "
                            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                            (json.dumps({'at': stamp(), 'code': type(exc).__name__,
                                         'location': error_location(exc)}),))
            # An unclassified exception is a processor failure, not evidence
            # that the story deserves an editorial rejection.
            from .material_flow import mark
            self.counts['ERROR'] -= 1
            if not self.counts['ERROR']:
                del self.counts['ERROR']
            self.counts['TECHNICAL_ERROR'] = self.counts.get('TECHNICAL_ERROR', 0) + 1
            self.db.execute("UPDATE items SET disposition='TECHNICAL_ERROR',processed_at=? WHERE item_id=?",
                            (stamp(), job['item_id']))
            mark(self.db, job['item_id'], job.get('_stage', 'screening'), 'ERROR',
                 'Техническая ошибка обработчика: ' + type(exc).__name__, block_kind='technical')
            self.db.execute("UPDATE processing_jobs SET status='DONE',outcome='TECHNICAL_ERROR',error_code=?,"
                            "finished_at=?,owner=NULL,lease_until=NULL WHERE job_id=?",
                            (type(exc).__name__, stamp(), job['job_id']))
            self.db.commit()
            return
        from .material_flow import mark
        stage = 'gate' if getattr(work.function, '__name__', '') == 'validate_draft' else 'drafting' if getattr(work.function, '__name__', '') == 'draft_post' else 'screening' if work.role == 'filter' else 'analysis' if getattr(work.function, '__name__', '') in {'analyze', 'analyze_with_ai'} else 'reading'
        mark(self.db, job['item_id'], stage, 'READY' if self.continuous else 'RUNNING')
        job['_stage'] = stage
        self.db.execute("UPDATE processing_jobs SET role=?,lease_until=? WHERE job_id=?", (work.role, self._lease(), job["job_id"]))
        self._event(job["job_id"], work.role, "RUNNING")
        self.db.commit()
        scope = {"item_id": job["item_id"], "job_id": job["job_id"], "category": job["category"], "revision": job["revision"]}
        future = self.pool.submit(work.execute, self.runtime, scope)
        self.running[future] = (job, generator)

    def tick(self):
        for future in list(self.running):
            if not future.done():
                continue
            job, generator = self.running.pop(future)
            try:
                value = future.result()
            except Exception as exc:
                self._advance(job, generator, error=exc)
            else:
                from .material_flow import mark
                if job.get('_stage') and self._valid(job):
                    if job['_stage'] == 'reading' and not readable_result(value):
                        mark(self.db, job['item_id'], 'reading', 'WAITING',
                             'Прочитанный пригодный материал пока не получен.', block_kind='evidence')
                    else:
                        mark(self.db, job['item_id'], job['_stage'], 'DONE')
                self._advance(job, generator, value=value)
        self.db.execute("UPDATE processing_jobs SET lease_until=? WHERE owner=? AND status='RUNNING'", (self._lease(), self.owner))
        self.db.commit()
        while len(self.running) < self.workers:
            job = self._claim()
            if not job:
                break
            payload = json.loads(job["payload_json"])
            from .core import process_item_steps
            job["_item"] = copy.deepcopy(payload["item"])
            if job['attempts']:
                from .core import _saved_material
                saved = self.db.execute('SELECT * FROM items WHERE item_id=?', (job['item_id'],)).fetchone()
                job['_item'].update(_saved_material(saved, payload['source']))
            job['_item']['_expected_revision'] = job['revision']
            job['_item']['_workflow_first_attempt'] = job['attempts'] == 0 and job['category'] == 'fresh'
            generator = process_item_steps(self.db, payload["source"], job["_item"],
                **payload["options"], ai_settings=self.config.get("ai", {}), existing_item_id=job["item_id"],
                post_ready_callback=self.config.get("_publish_ready_callback"))
            self._advance(job, generator)

    def close(self):
        try:
            self.tick()
            while self.running:
                wait(self.running, timeout=1, return_when=FIRST_COMPLETED)
                self.tick()
        except BaseException:
            self.abort()
            raise
        else:
            self.pool.shutdown(wait=True)
            self.closed = True

    def abort(self):
        """Keep admitted work resumable if its collection cycle crashes."""
        if self.closed:
            return
        self.pool.shutdown(wait=True)
        try:
            for job, generator in self.running.values():
                generator.close()
        finally:
            self.db.rollback()
            self.db.execute("INSERT INTO processing_job_events(job_id,role,status,created_at) "
                            "SELECT job_id,role,'CYCLE_ABORTED',? FROM processing_jobs "
                            "WHERE status='RUNNING' AND owner=?", (stamp(), self.owner))
            self.db.execute("UPDATE processing_jobs SET status='PENDING',owner=NULL,lease_until=NULL "
                            "WHERE status='RUNNING' AND owner=?", (self.owner,))
            self.db.commit()
            self.running.clear()
            self.closed = True


def snapshot(db, now=None):
    now = now or datetime.now(timezone.utc)
    tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    result = {"pending": 0, "running": 0, "waiting": 0, "unfinished": 0, "oldest_seconds": None, "wait_p95_seconds": None}
    if "processing_jobs" not in tables:
        return result
    for row in db.execute("SELECT status,COUNT(*) FROM processing_jobs WHERE status IN ('PENDING','RUNNING','WAITING') GROUP BY status"):
        result[row[0].lower()] = row[1]
    result["unfinished"] = result["pending"] + result["running"] + result["waiting"]
    oldest = db.execute("SELECT MIN(created_at) FROM processing_jobs WHERE status IN ('PENDING','RUNNING','WAITING')").fetchone()[0]
    if oldest:
        result["oldest_seconds"] = max(0, (now - datetime.fromisoformat(oldest)).total_seconds())
    cutoff = (now - timedelta(hours=24)).isoformat()
    waits = sorted(max(0, (datetime.fromisoformat(row[1]) - datetime.fromisoformat(row[0])).total_seconds())
                   for row in db.execute("SELECT created_at,started_at FROM processing_jobs WHERE started_at>=?", (cutoff,)))
    if waits:
        result["wait_p95_seconds"] = waits[max(0, math.ceil(len(waits) * .95) - 1)]
    if 'material_stage_state' in tables:
        result['stages'] = [dict(row) for row in db.execute(
            "SELECT st.stage,st.status,st.block_kind,COUNT(*) AS count FROM material_stage_state st "
            "JOIN items i ON i.item_id=st.item_id AND i.ingest_revision=st.revision "
            "WHERE st.status IN ('READY','RUNNING','WAITING','ERROR') GROUP BY st.stage,st.status,st.block_kind")]
    return result
