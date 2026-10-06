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
                 'cache_events', 'resource_operations', 'items', 'sources', 'item_revisions', 'interest_feedback'):
        schemas[name] = [row[1] for row in db.execute('PRAGMA table_info('+name+')')]
    from .runtime import snapshot as usage
    from .workflow import snapshot as queue
    from .ai import web_search_enabled
    search = {'enabled': web_search_enabled(config.get('ai', {})),
              'active_sources': db.execute("SELECT COUNT(*) FROM sources WHERE type='web_search' AND active=1").fetchone()[0],
              'last_api_attempt_at': None}
    if 'api_usage' in tables:
        search['last_api_attempt_at'] = db.execute(
            "SELECT MAX(created_at) FROM api_usage WHERE search_requested=1").fetchone()[0]
    errors = []
    if 'processing_jobs' in tables:
        errors = [dict(row) for row in db.execute(
            'SELECT error_code,COUNT(*) AS count FROM processing_jobs '
            'WHERE error_code IS NOT NULL GROUP BY error_code')]
    state = {}
    if 'app_state' in tables:
        for row in db.execute("SELECT key,value FROM app_state WHERE key IN "
                              "('ai_last_error','ai_last_success','digest_scheduler_last_tick_at','diagnostic_last_error','api_account_blocked_until','material_processor_heartbeat','material_processor_error')"):
            if row[0] in {'ai_last_error', 'diagnostic_last_error', 'material_processor_error'}:
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
            'independent_processing': bool(config.get('newsroom', {}).get('independent_processing')),
            'processing_workers': max(1, min(8, int(config.get('newsroom', {}).get('processing_workers', 2)))),
            'api_concurrency': max(1, min(8, int(config.get('ai', {}).get('api_concurrency', 2)))),
            'processing_limits': {key: int(config.get('newsroom', {}).get(key, default))
                                  for key, default in (('analysis_per_cycle', 25), ('triage_per_cycle', 12),
                                                       ('retry_items_per_cycle', 2), ('processing_cycle_seconds', 150))},
            'api_limits': {key: int(config.get('ai', {}).get(key, default))
                           for key, default in (('api_requests_per_window', 50), ('api_budget_window_seconds', 180),
                                                ('api_retry_reserve', 4), ('timeout_seconds', 45))},
            'schemas': schemas, 'queue': queue(db), 'usage': usage(db), 'web_search': search,
            'job_errors': errors, 'state': state}
