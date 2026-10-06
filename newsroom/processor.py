"""Independent material processing, sharing API admission and delivery gates."""
from __future__ import annotations

import threading
import time
from datetime import datetime, timezone

from .agent_control import enabled, require_enabled
from .db import connect
from .locking import acquire_named_lock
from .workflow import Coordinator
from .runtime import stamp


def prepare(config, db=None):
    from .runtime import attach
    from .editorial_registry import attach_cached
    attach(config)
    attach_cached(config)
    config['_continuous_processing'] = True
    config['ai'] = {k: v for k, v in config['ai'].items()
                    if k not in {'_analysis_budget', '_triage_budget', '_disabled_for_cycle', '_triage_disabled'}}
    config['ai']['_retry_cycle_delay_seconds'] = 30
    config['ai']['_research_agent_budget'] = 1
    config['ai']['_recovery_search_budget'] = 2
    if db is not None:
        from .core import _WebSearchQuota
        config['ai']['_web_search_quota'] = _WebSearchQuota(db, 3)
    owner_ids = config.get('telegram', {}).get('interest_owner_user_ids') or []
    if owner_ids:
        config['ai']['_correction_owner_chat_id'] = str(owner_ids[0])
    return config


def seed_held(db, config):
    from .core import _retry_ai_held_items, _close_exhausted_retries, _reconcile_legacy_retry_loops
    from .material_flow import migrate
    migrate(db)
    _reconcile_legacy_retry_loops(db)
    _close_exhausted_retries(db)
    sources = {r['source_id']: r for r in db.execute("SELECT * FROM sources WHERE active=1 OR type='manual' OR url LIKE 'story-watch://%'")}
    settings = {**config, 'newsroom': {**config.get('newsroom', {}), 'retry_items_per_cycle': 1000, 'triage_per_cycle': 1000}}
    # Migration only seeds missing jobs; it never clears attempts or reopens terminal work.
    _retry_ai_held_items(db, sources, settings, limit=1000, coordinator=True)
    db.commit()


def run(config_path, stop=None):
    from .cli import load_config, auto_publish_since
    from .diagnostics import error_location
    import json
    stop = stop or threading.Event()
    db = lock = coordinator = delivery_thread = None
    delivery_stop = delivery_ready = None
    try:
        config = prepare(load_config(config_path))
        require_enabled(config)
        path = config['newsroom']['database']
        lock = acquire_named_lock(path, 'processing')
        if lock is None:
            return
        db = connect(path)
        config = prepare(config, db)
        seed_held(db, config)
        coordinator = Coordinator(db, config, {}, categories=('fresh', 'retry', 'watch'))
        delivery_ready = threading.Event()
        delivery_stop = threading.Event()
        def deliver_pending():
            while not delivery_stop.is_set() and enabled(coordinator.config):
                try:
                    auto_publish_since(path, coordinator.config)
                except Exception as exc:
                    from .core import _log_timing
                    _log_timing('material_delivery_error', code=type(exc).__name__)
                delivery_ready.wait(5)
                delivery_ready.clear()
        delivery_thread = threading.Thread(target=deliver_pending, name='material-delivery', daemon=True)
        delivery_thread.start()
        def publish_ready(post_ids):
            delivery_ready.set()
        coordinator.config['_publish_ready_callback'] = publish_ready
        refreshed = published = seeded = 0.0
        while not stop.is_set() and enabled(coordinator.config):
            now = time.monotonic()
            if now - refreshed >= 10:
                updated = prepare(load_config(config_path), db)
                if updated['newsroom']['database'] != path:
                    raise RuntimeError('PROCESSING_DATABASE_CHANGED')
                updated['_publish_ready_callback'] = publish_ready
                coordinator.config = updated
                coordinator.runtime = updated['ai']['_runtime']
                coordinator._resume_expired()
                db.execute("INSERT INTO app_state(key,value) VALUES('material_processor_heartbeat',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (stamp(),))
                db.commit()
                refreshed = now
            if now - seeded >= 30:
                seed_held(db, coordinator.config)
                coordinator._seed_pending()
                seeded = now
            coordinator.tick()
            stop.wait(.25)
    except Exception as exc:
        if db:
            db.rollback()
            db.execute("INSERT INTO app_state(key,value) VALUES('material_processor_error',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                       (json.dumps({'at': stamp(), 'code': type(exc).__name__, 'location': error_location(exc)}),))
            db.commit()
        raise
    finally:
        if delivery_stop:
            delivery_stop.set()
            delivery_ready.set()
            delivery_thread.join(timeout=60)
        if coordinator:
            coordinator.abort()
        if db:
            db.close()
        if lock:
            lock.close()


def start(config_path):
    def supervise():
        from .cli import load_config
        while enabled(load_config(config_path)):
            try:
                run(config_path)
            except Exception:
                # The preceding run persisted safe diagnostics and released its lease.
                time.sleep(5)
            else:
                break
    thread = threading.Thread(target=supervise, name='material-processor', daemon=True)
    thread.start()
    return thread
