"""Read-only operational evidence without source texts, credentials or paths."""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path


def error_location(exc):
    frames = []
    trace = exc.__traceback__
    while trace:
        path = Path(trace.tb_frame.f_code.co_filename)
        if path.parent.name == 'newsroom':
            frames.append({'file': path.name, 'function': trace.tb_frame.f_code.co_name,
                           'line': trace.tb_lineno})
        trace = trace.tb_next
    return {'type': type(exc).__name__, 'frames': frames[-5:],
            'sqlite_code': getattr(exc, 'sqlite_errorname', None)}


def snapshot(db, config):
    root = Path(__file__).resolve().parent
    release = root.parent.name
    tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    schemas = {}
    for name in ('processing_jobs', 'processing_job_events', 'api_usage', 'stage_cache',
                 'cache_events', 'items', 'sources', 'item_revisions', 'interest_feedback'):
        schemas[name] = [row[1] for row in db.execute('PRAGMA table_info('+name+')')]
    from .runtime import snapshot as usage
    from .workflow import snapshot as queue
    errors = []
    if 'processing_jobs' in tables:
        errors = [dict(row) for row in db.execute(
            'SELECT error_code,COUNT(*) AS count FROM processing_jobs '
            'WHERE error_code IS NOT NULL GROUP BY error_code')]
    state = {}
    if 'app_state' in tables:
        for row in db.execute("SELECT key,value FROM app_state WHERE key IN "
                              "('ai_last_error','ai_last_success','digest_scheduler_last_tick_at','diagnostic_last_error')"):
            if row[0] in {'ai_last_error', 'diagnostic_last_error'}:
                try:
                    value = json.loads(row[1])
                except (TypeError, ValueError):
                    continue
                if isinstance(value, dict):
                    state[row[0]] = {key: value.get(key) for key in ('at','code','location')}
            else:
                state[row[0]] = row[1]
    return {'release': release if re.fullmatch('[0-9a-f]{40}', release) else None,
            'code_hashes': {name: hashlib.sha256((root/name).read_bytes()).hexdigest()
                            for name in ('workflow.py','runtime.py','core.py','cli.py','diagnostics.py')},
            'processing_workers': max(1, min(8, int(config.get('newsroom', {}).get('processing_workers', 2)))),
            'api_concurrency': max(1, min(8, int(config.get('ai', {}).get('api_concurrency', 2)))),
            'schemas': schemas, 'queue': queue(db), 'usage': usage(db),
            'job_errors': errors, 'state': state}
