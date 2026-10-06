"""Shared API admission, durable usage receipts and validated stage caches.

Only hashes and safe counters are stored for API calls; credentials and prompts
never enter this ledger. Every HTTP attempt, including a transport retry, must
reserve capacity before it can reach the network.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timedelta, timezone

SCOPE = ContextVar("newsroom_work_scope", default={})

SCHEMA = """
CREATE TABLE IF NOT EXISTS api_usage (
 call_id TEXT PRIMARY KEY, role TEXT NOT NULL, category TEXT NOT NULL,
 item_id INTEGER, job_id INTEGER, model TEXT NOT NULL,
 status TEXT NOT NULL, created_at TEXT NOT NULL, finished_at TEXT, lease_until TEXT,
 elapsed_seconds REAL, input_tokens INTEGER, cached_input_tokens INTEGER,
 output_tokens INTEGER, search_requested INTEGER NOT NULL DEFAULT 0,
 search_calls INTEGER, estimated_usd REAL, error_code TEXT
);
CREATE INDEX IF NOT EXISTS api_usage_time_idx ON api_usage(created_at);
CREATE TABLE IF NOT EXISTS stage_cache (
 cache_key TEXT PRIMARY KEY, stage TEXT NOT NULL, result_json TEXT NOT NULL,
 created_at TEXT NOT NULL, expires_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS cache_events (
 event_id INTEGER PRIMARY KEY, stage TEXT NOT NULL, item_id INTEGER,
 created_at TEXT NOT NULL
);
"""


def stamp():
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def account_unavailable(code):
    return code in {'HTTP_429:credit_balance_exhausted', 'HTTP_429:insufficient_quota',
                    'HTTP_429:billing_hard_limit_reached', 'HTTP_401:invalid_api_key'}


class BudgetDeferred(RuntimeError):
    code = "SHARED_BUDGET_DEFERRED"

    def __init__(self, reason='requests', delay_seconds=180):
        self.reason = reason
        self.delay_seconds = delay_seconds
        super().__init__("Shared request capacity unavailable")


class Runtime:
    def __init__(self, database, settings):
        self.database = database
        self.settings = dict(settings)
        self.account_cooldown_seconds = max(60, min(3600, int(settings.get('api_account_cooldown_seconds', 900))))
        self.max_calls = max(1, int(settings.get("api_requests_per_window", 50)))
        self.window = max(30, int(settings.get("api_budget_window_seconds", 180)))
        self.max_active = max(1, min(8, int(settings.get("api_concurrency", 2))))
        self.search_limit = max(0, int(settings.get("api_searches_per_window", 3)))
        self.retry_reserve = min(self.max_calls, max(0, int(settings.get("api_retry_reserve", 4))))
        self.correction_reserve = min(self.max_calls, max(0, int(settings.get("api_correction_reserve", 2))))
        self._lock = threading.Lock()

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.database, timeout=30)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA temp_store=MEMORY")
            with db:
                yield db
        finally:
            db.close()

    def reserve(self, payload, settings):
        scope = SCOPE.get()
        category = settings.get("_work_category", scope.get("category", "fresh"))
        role = settings.get("_work_role", scope.get("role", "collector"))
        cutoff = (datetime.now(timezone.utc) - timedelta(seconds=self.window)).isoformat(timespec="microseconds")
        # Expiry covers the request timeout and its network overhead. Expired
        # calls retain unknown usage, and still count in their request window.
        lease_until = (datetime.now(timezone.utc) + timedelta(
            seconds=max(90, int(settings.get("timeout_seconds", 45)) + 30))).isoformat(timespec="microseconds")
        is_search = any(str(tool.get("type", "")).startswith("web_search")
                        for tool in payload.get("tools", []) if isinstance(tool, dict))
        with self._lock, self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            blocked = db.execute("SELECT value FROM app_state WHERE key='api_account_blocked_until'").fetchone()
            if blocked:
                delay = (datetime.fromisoformat(blocked[0]) - datetime.now(timezone.utc)).total_seconds()
                if delay > 0:
                    raise BudgetDeferred('account', max(1, int(delay) + 1))
            db.execute("UPDATE api_usage SET status='UNKNOWN',error_code='PROCESS_INTERRUPTED' "
                       "WHERE status='RESERVED' AND lease_until<?", (stamp(),))
            total = db.execute("SELECT COUNT(*) FROM api_usage WHERE created_at>=?", (cutoff,)).fetchone()[0]
            active = db.execute("SELECT COUNT(*) FROM api_usage WHERE status='RESERVED'").fetchone()[0]
            used = {row[0]: row[1] for row in db.execute(
                "SELECT category,COUNT(*) FROM api_usage WHERE created_at>=? GROUP BY category", (cutoff,))}
            held = max(0, self.correction_reserve - used.get("correction", 0))
            if category not in {"retry", "correction"}:
                held += max(0, self.retry_reserve - used.get("retry", 0))
            if category == "correction":
                held = 0
            search_used = db.execute("SELECT COUNT(*) FROM api_usage WHERE created_at>=? AND search_requested=1",
                                     (cutoff,)).fetchone()[0]
            if active >= self.max_active:
                raise BudgetDeferred('concurrency', 1)
            if total >= max(0, self.max_calls - held) or (is_search and search_used >= self.search_limit):
                earliest = db.execute('SELECT MIN(created_at) FROM api_usage WHERE created_at>=?', (cutoff,)).fetchone()[0]
                delay = max(1, int((datetime.fromisoformat(earliest) + timedelta(seconds=self.window)
                                  - datetime.now(timezone.utc)).total_seconds()) + 1) if earliest else self.window
                raise BudgetDeferred('requests', delay)
            if category == 'background':
                busy = db.execute("SELECT COUNT(*) FROM processing_jobs WHERE status IN ('PENDING','RUNNING','WAITING') AND category='fresh'").fetchone()[0]
                if busy or used.get('background', 0) >= max(0, int(self.settings.get('api_background_per_window', 2))):
                    raise BudgetDeferred('background', self.window)
            call_id = uuid.uuid4().hex
            db.execute("INSERT INTO api_usage(call_id,role,category,item_id,job_id,model,status,created_at,search_requested,lease_until) "
                       "VALUES(?,?,?,?,?,?,'RESERVED',?,?,?)",
                       (call_id, role, category, scope.get("item_id"), scope.get("job_id"),
                        payload.get("model", "unknown"), stamp(), int(is_search), lease_until))
        return call_id

    def finish(self, call_id, response, elapsed, error=None):
        usage = (response or {}).get("usage") or {}
        def integer(value):
            return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None
        input_tokens = integer(usage.get("input_tokens"))
        output_tokens = integer(usage.get("output_tokens"))
        cached = integer((usage.get("input_tokens_details") or {}).get("cached_tokens"))
        searches = sum(entry.get("type") == "web_search_call" for entry in (response or {}).get("output", []) if isinstance(entry, dict))
        with self.db() as db:
            row = db.execute("SELECT model,search_requested FROM api_usage WHERE call_id=?", (call_id,)).fetchone()
            rates = (self.settings.get("model_prices") or {}).get(row["model"], {})
            estimate = None
            if input_tokens is not None and output_tokens is not None and cached is not None and cached <= input_tokens:
                names = ("input_per_million", "cached_input_per_million", "output_per_million")
                if all(isinstance(rates.get(name), (int, float)) and rates[name] >= 0 for name in names):
                    estimate = ((input_tokens - cached) * rates[names[0]] + cached * rates[names[1]]
                                + output_tokens * rates[names[2]]) / 1_000_000
                    if row["search_requested"]:
                        search_price = self.settings.get("search_price_per_call")
                        estimate = estimate + searches * search_price if isinstance(search_price, (int, float)) and search_price >= 0 else None
            code = getattr(error, "code", type(error).__name__) if error else None
            if account_unavailable(code):
                until = (datetime.now(timezone.utc) + timedelta(seconds=self.account_cooldown_seconds)).isoformat(timespec='microseconds')
                db.execute("INSERT INTO app_state(key,value) VALUES('api_account_blocked_until',?) "
                           "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (until,))
                db.execute("INSERT INTO app_state(key,value) VALUES('ai_last_error',?) "
                           "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                           (json.dumps({'at': stamp(), 'code': code}),))
            status = "ERROR" if error else "SUCCEEDED"
            if error and (str(code).startswith("NETWORK_") or code in {"INVALID_RESPONSE_JSON", "PROCESS_INTERRUPTED"}):
                status = "UNKNOWN"
            db.execute("UPDATE api_usage SET status=?,finished_at=?,elapsed_seconds=?,input_tokens=?,"
                       "cached_input_tokens=?,output_tokens=?,search_calls=?,estimated_usd=?,error_code=? WHERE call_id=?",
                       (status, stamp(), round(elapsed, 4), input_tokens, cached, output_tokens,
                        searches if response is not None else None,
                        estimate, code, call_id))

    def cached(self, key, stage):
        with self.db() as db:
            row = db.execute("SELECT result_json FROM stage_cache WHERE cache_key=? AND expires_at>?", (key, stamp())).fetchone()
            if not row:
                return None
            db.execute("INSERT INTO cache_events(stage,item_id,created_at) VALUES(?,?,?)", (stage, SCOPE.get().get("item_id"), stamp()))
        return json.loads(row[0])

    def store(self, key, stage, result, ttl):
        with self.db() as db:
            db.execute("INSERT OR REPLACE INTO stage_cache VALUES(?,?,?,?,?)", (key, stage, json.dumps(result, ensure_ascii=False),
                       stamp(), (datetime.now(timezone.utc) + timedelta(seconds=ttl)).isoformat(timespec="microseconds")))
            db.execute("DELETE FROM stage_cache WHERE expires_at<?", (stamp(),))


def attach(config):
    """Share one ledger across the main cycle, manual intake and review process."""
    settings = config.setdefault("ai", {})
    path = config.get("newsroom", {}).get("database")
    if path and not settings.get("_runtime"):
        settings["_runtime"] = Runtime(path, settings)
    return settings.get("_runtime")


def cache_key(stage, value):
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256((stage + ":" + encoded).encode()).hexdigest()


def snapshot(db, now=None):
    now = now or datetime.now(timezone.utc)
    cutoff = (now - timedelta(hours=24)).isoformat()
    tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    result = {'calls': 0, 'unknown_usage': 0, 'unpriced_calls': 0,
              'estimated_usd': None, 'cost_per_publication_usd': None, 'roles': [], 'cache_hits': 0}
    if 'api_usage' not in tables:
        return result
    rows = db.execute("SELECT role,COUNT(*) AS calls,SUM(input_tokens) AS input_tokens,SUM(cached_input_tokens) AS cached_input_tokens,"
                      "SUM(output_tokens) AS output_tokens,SUM(estimated_usd) AS estimated_usd,"
                      "SUM(CASE WHEN input_tokens IS NULL OR output_tokens IS NULL THEN 1 ELSE 0 END) AS unknown_usage,"
                      "SUM(CASE WHEN estimated_usd IS NULL THEN 1 ELSE 0 END) AS unpriced_calls "
                      "FROM api_usage WHERE created_at>=? GROUP BY role", (cutoff,)).fetchall()
    result['roles'] = [dict(row) for row in rows]
    for key in ('calls', 'unknown_usage', 'unpriced_calls'):
        result[key] = sum(row[key] or 0 for row in rows)
    estimates = [row['estimated_usd'] for row in rows if row['estimated_usd'] is not None]
    # A partial subtotal is labelled separately; never present unknown expenses as zero.
    result['known_estimated_usd'] = sum(estimates) if estimates else None
    if result['calls'] and not result['unpriced_calls']:
        result['estimated_usd'] = sum(estimates)
        publications = db.execute("SELECT COUNT(*) FROM posts WHERE status='PUBLISHED' AND published_at>=?", (cutoff,)).fetchone()[0]
        if publications:
            result['cost_per_publication_usd'] = result['estimated_usd'] / publications
    if 'cache_events' in tables:
        result['cache_hits'] = db.execute('SELECT COUNT(*) FROM cache_events WHERE created_at>=?', (cutoff,)).fetchone()[0]
    return result


def health_lines(db, now=None):
    usage = snapshot(db, now)
    from .workflow import snapshot as queue_snapshot
    queue = queue_snapshot(db, now)
    wait = queue['wait_p95_seconds']
    oldest = queue['oldest_seconds']
    lines = [f"Очередь: незавершённых {queue['unfinished']} · ожидают {queue['pending']} · выполняются {queue['running']} · отложены {queue['waiting']}.",
             "Ожидание задания: " + (f"p95 {wait:.1f} с" if wait is not None else "нет замеров")
             + (f" · самое старое незавершённое {oldest:.1f} с." if oldest is not None else "."),
             f"API за 24 ч: {usage['calls']} запросов · неизвестный расход {usage['unknown_usage']} · попаданий в кеш {usage['cache_hits']}."]
    if usage['estimated_usd'] is not None:
        lines.append(f"Оценка расходов за 24 ч: ${usage['estimated_usd']:.4f}.")
        if usage['cost_per_publication_usd'] is not None:
            lines.append(f"Расходы на подтверждённую публикацию с учётом отсевов: ${usage['cost_per_publication_usd']:.4f}.")
    else:
        lines.append(f"Полная денежная оценка недоступна: {usage['unpriced_calls']} запросов без тарифа или подтверждённого расхода; это не нулевые расходы.")
    names = {'collector': 'Сборщик', 'filter': 'Фильтровщик', 'editor': 'Редактор'}
    for role in usage['roles']:
        price = (f"${role['estimated_usd']:.4f}" if not role['unpriced_calls'] else 'денежная оценка неполная')
        lines.append(f"{names.get(role['role'], role['role'])}: {role['calls']} запросов · {price} · "
                     f"входных токенов {role['input_tokens'] if role['input_tokens'] is not None else 'неизвестно'} · "
                     f"выходных токенов {role['output_tokens'] if role['output_tokens'] is not None else 'неизвестно'}.")
    return lines
