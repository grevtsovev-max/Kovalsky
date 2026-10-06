"""Private credentials and durable, conservative admission for cloud Reader."""
from __future__ import annotations

import os
from contextlib import closing
import sqlite3
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo


def api_key(settings):
    key = os.environ.get(settings.get('jina_api_key_env', 'JINA_API_KEY'), '')
    if not key and settings.get('jina_api_key_file'):
        try:
            path = Path(settings['jina_api_key_file']).expanduser()
            if path.stat().st_mode & 0o007:
                raise ValueError('JINA_KEY_FILE_PUBLIC')
            key = path.read_text().strip()
        except OSError:
            raise ValueError('JINA_CREDENTIALS_MISSING') from None
    if not key:
        raise ValueError('JINA_CREDENTIALS_MISSING')
    if len(key) > 1024 or any(char.isspace() for char in key):
        raise ValueError('JINA_CREDENTIALS_INVALID')
    return key


def reserve(settings):
    path = settings.get('jina_cloud_ledger')
    if not path:
        raise ValueError('JINA_CLOUD_LEDGER_MISSING')
    budget = max(500, min(20000, int(settings.get('jina_cloud_request_tokens', 20000))))
    daily_calls = max(0, min(100, int(settings.get('jina_cloud_daily_requests', 10))))
    day = datetime.now(ZoneInfo('Europe/Moscow')).date().isoformat()
    Path(path).expanduser().parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(str(Path(path).expanduser()), timeout=5)) as db, db:
        db.execute('CREATE TABLE IF NOT EXISTS reader_attempts (id INTEGER PRIMARY KEY, day TEXT NOT NULL, reserved_tokens INTEGER NOT NULL, status TEXT NOT NULL, actual_tokens INTEGER)')
        db.execute('BEGIN IMMEDIATE')
        calls = db.execute('SELECT COUNT(*) FROM reader_attempts WHERE day=?', (day,)).fetchone()[0]
        if calls >= daily_calls:
            raise ValueError('JINA_CLOUD_DAILY_LIMIT')
        cursor = db.execute('INSERT INTO reader_attempts(day,reserved_tokens,status) VALUES(?,?,?)', (day, budget, 'UNKNOWN'))
        attempt = cursor.lastrowid
    return attempt, budget


def finish(settings, attempt, status, tokens=None):
    # A failed/unknown attempt keeps its reservation; it is never counted as free.
    with closing(sqlite3.connect(settings['jina_cloud_ledger'], timeout=5)) as db, db:
        db.execute('UPDATE reader_attempts SET status=?,actual_tokens=? WHERE id=?', (status, tokens, attempt))
