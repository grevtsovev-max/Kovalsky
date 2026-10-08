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
    from .material_flow import mark
    mark(db, item_id, 'intake', 'DONE', 'Входной материал сохранён.')
    settings = options.get('settings') or {}
    if settings.get('_keyword_prefilter'):
        from .keyword_filter import screen
        screen(db, item_id, item, settings)
    db.commit()
    from .edition.store import ingest
    ingest(db,item_id,settings)
    return item_id


class CollectionCoordinator:
    """Persist collected inputs without starting editorial processing."""
    running = {}
    def tick(self): pass
    def close(self): pass
    def abort(self): pass


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
            require_enabled(runtime.settings.get('_agent_control_config') or {'newsroom': {'database': runtime.database}})
        measurement = runtime.measure(self.stage or 'unattributed', self.role, scope) if runtime else nullcontext({})
        with measurement:
            return self.function(*self.args, **self.kwargs)
