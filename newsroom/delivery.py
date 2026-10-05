"""Durable delivery intents. An ambiguous network result is never retried blindly."""
from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone

SCHEMA = """
CREATE TABLE IF NOT EXISTS publication_attempts (
 attempt_id INTEGER PRIMARY KEY,
 delivery_key TEXT NOT NULL UNIQUE,
 channel_id TEXT NOT NULL,
 post_id INTEGER REFERENCES posts(post_id),
 text TEXT NOT NULL,
 content_hash TEXT NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('PREPARED','SENDING','SENT','CONFIRMED','FAILED','UNKNOWN')),
 attempt_count INTEGER NOT NULL DEFAULT 0,
 telegram_message_id TEXT,
 telegram_response_json TEXT,
 error_code TEXT,
 created_at TEXT NOT NULL,
 updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS publication_attempts_post_idx ON publication_attempts(post_id);
CREATE TABLE IF NOT EXISTS delivery_events (
 event_id INTEGER PRIMARY KEY,
 attempt_id INTEGER NOT NULL REFERENCES publication_attempts(attempt_id),
 status TEXT NOT NULL, detail_json TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS delivery_events_no_update BEFORE UPDATE ON delivery_events
BEGIN SELECT RAISE(ABORT, 'delivery history is append only'); END;
CREATE TRIGGER IF NOT EXISTS delivery_events_no_delete BEFORE DELETE ON delivery_events
BEGIN SELECT RAISE(ABORT, 'delivery history is append only'); END;
CREATE TABLE IF NOT EXISTS digest_batches (
 batch_key TEXT PRIMARY KEY, messages_json TEXT NOT NULL, news_count INTEGER NOT NULL,
 period_end TEXT NOT NULL, created_at TEXT NOT NULL
);
"""


class DeliveryUncertain(RuntimeError):
    pass


class DeliveryRejected(RuntimeError):
    """Only a definitive rejection, or a failure before any request, is retryable."""


class TelegramReceipt(str):
    def __new__(cls, response):
        obj = super().__new__(cls, str(response['message_id']))
        obj.response = response
        return obj


def now():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def channel(config):
    settings = config.get('telegram', {})
    target = os.getenv(settings.get('chat_id_env', 'TELEGRAM_CHAT_ID')) or settings.get('chat_id')
    if not target:
        raise DeliveryRejected('Telegram destination is missing')
    return str(target)


def event(db, attempt_id, status, detail=None):
    db.execute('INSERT INTO delivery_events(attempt_id,status,detail_json,created_at) VALUES(?,?,?,?)',
               (attempt_id, status, json.dumps(detail or {}, ensure_ascii=False), now()))


def deliver(db, config, operation, text, send, post_id=None):
    """Called only AFTER the editorial gate. Commits intent before invoking send."""
    target = channel(config)
    key = target + ':' + operation
    db.commit()
    db.execute('BEGIN IMMEDIATE')
    row = db.execute('SELECT * FROM publication_attempts WHERE delivery_key=?', (key,)).fetchone()
    if row and row['status'] in {'SENT', 'CONFIRMED'}:
        db.commit()
        return row['telegram_message_id']
    if row and row['status'] in {'SENDING', 'UNKNOWN'}:
        db.commit()
        raise DeliveryUncertain('Delivery outcome requires reconciliation; resend blocked')
    if row and row['attempt_count'] >= 3:
        db.commit()
        raise DeliveryRejected('Delivery retry limit reached')
    if row and row['text'] != text:
        db.commit()
        raise DeliveryRejected('Prepared publication changed; original intent retained')
    if post_id is not None and db.execute('SELECT 1 FROM post_memory WHERE post_id=?', (post_id,)).fetchone():
        # Recheck under the same write lock that reserves the send: two different
        # drafts with the same fact cannot both pass an earlier unlocked check.
        from .knowledge import publication_issues
        issues = publication_issues(db, post_id, text)
        if issues:
            db.commit()
            raise DeliveryRejected('Publication memory gate: ' + ', '.join(issues))
        diff_row = db.execute('SELECT d.payload_json FROM post_memory m JOIN story_diffs d USING(diff_id) WHERE m.post_id=?', (post_id,)).fetchone()
        material_ids = json.loads(diff_row[0]).get('material_unpublished_facts', [])
        placeholders = ','.join('?' for _ in material_ids) or 'NULL'
        competing = db.execute(f"""SELECT a.attempt_id FROM post_facts ours
            JOIN post_facts theirs ON theirs.fact_id=ours.fact_id AND theirs.post_id!=ours.post_id
            JOIN publication_attempts a ON a.post_id=theirs.post_id
            WHERE ours.post_id=? AND a.channel_id=? AND ours.fact_id IN ({placeholders}) AND a.status IN ('PREPARED','SENDING','SENT','CONFIRMED','UNKNOWN') LIMIT 1""",
            (post_id,target,*material_ids)).fetchone()
        if competing:
            db.commit()
            raise DeliveryUncertain('Another publication already reserved this fact; resend blocked')
    if not row:
        stamp = now()
        cur = db.execute('INSERT INTO publication_attempts(delivery_key,channel_id,post_id,text,content_hash,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)',
                         (key, target, post_id, text, hashlib.sha256(text.encode()).hexdigest(), 'PREPARED', stamp, stamp))
        attempt_id = cur.lastrowid
        event(db, attempt_id, 'PREPARED')
    else:
        attempt_id = row['attempt_id']
    db.execute("UPDATE publication_attempts SET status='SENDING',attempt_count=attempt_count+1,updated_at=? WHERE attempt_id=?", (now(), attempt_id))
    event(db, attempt_id, 'SENDING')
    db.commit()
    try:
        receipt = send(config, text)
        # The production transport always returns a message ID and its actual response.
        if not isinstance(receipt, (str, int)) or not str(receipt).isdigit():
            raise DeliveryUncertain('Telegram response has no valid message ID')
    except Exception as exc:
        status = 'FAILED' if isinstance(exc, DeliveryRejected) else 'UNKNOWN'
        changed = db.execute("UPDATE publication_attempts SET status=?,error_code=?,updated_at=? WHERE attempt_id=? AND status='SENDING'",
                   (status, type(exc).__name__, now(), attempt_id)).rowcount
        if not changed:
            observed = db.execute('SELECT status,telegram_message_id FROM publication_attempts WHERE attempt_id=?', (attempt_id,)).fetchone()
            db.commit()
            if observed['status'] in {'SENT','CONFIRMED'}:
                return observed['telegram_message_id']
        event(db, attempt_id, status, {'error_code': type(exc).__name__})
        db.commit()
        if status == 'UNKNOWN':
            raise DeliveryUncertain('Telegram outcome unknown; automatic resend blocked') from None
        raise
    response = getattr(receipt, 'response', None)
    db.execute("UPDATE publication_attempts SET status=CASE WHEN status='CONFIRMED' THEN status ELSE 'SENT' END,telegram_message_id=?,telegram_response_json=?,updated_at=? WHERE attempt_id=?",
               (str(receipt), json.dumps(response, ensure_ascii=False) if response is not None else None, now(), attempt_id))
    event(db, attempt_id, 'SENT', {'message_id': str(receipt)})
    db.commit()
    return str(receipt)


def confirm(db, config, operation):
    row = db.execute('SELECT * FROM publication_attempts WHERE delivery_key=?', (channel(config) + ':' + operation,)).fetchone()
    if row and row['status'] == 'SENT':
        db.execute("UPDATE publication_attempts SET status='CONFIRMED',updated_at=? WHERE attempt_id=?", (now(), row['attempt_id']))
        event(db, row['attempt_id'], 'CONFIRMED')


def reconcile_posts(db, config):
    """Replay durable successful receipts, atomically, without sending anything."""
    db.commit()
    db.execute('BEGIN IMMEDIATE')
    rows = db.execute("SELECT a.*,p.status AS post_status,p.story_id,p.post_hash FROM publication_attempts a JOIN posts p USING(post_id) WHERE a.channel_id=? AND a.status='SENT'", (channel(config),)).fetchall()
    for row in rows:
        if row['post_status'] != 'PUBLISHED':
            db.execute("UPDATE posts SET status='PUBLISHED',published_at=?,external_id=?,editor_decision='APPROVED' WHERE post_id=?", (row['updated_at'], row['telegram_message_id'], row['post_id']))
            db.execute('UPDATE stories SET last_published_at=?,version=version+1,publication_count=publication_count+1,last_content_hash=? WHERE story_id=?', (row['updated_at'], row['post_hash'], row['story_id']))
        confirm(db, config, 'post:' + str(row['post_id']))
    db.commit()
    return len(rows)


def observe_channel_post(db, message, comparable_text):
    """Resolve an ambiguous send only from an actual, uniquely matching channel update."""
    import re
    chat = message.get('chat') or {}
    targets = {str(chat.get('id', ''))}
    if chat.get('username'):
        targets.add('@' + chat['username'])
    message_id = message.get('message_id')
    if not isinstance(message_id, int) or message_id <= 0:
        return False
    try:
        sent_at = datetime.fromtimestamp(int(message['date']), timezone.utc)
    except (KeyError, TypeError, ValueError, OverflowError):
        return False
    def comparable(text):
        return re.sub(r'\s+', ' ', re.sub(r'\*\*(.*?)\*\*', r'\1', text, flags=re.DOTALL)).strip()
    matches = []
    for row in db.execute("SELECT * FROM publication_attempts WHERE status IN ('SENDING','UNKNOWN')"):
        if row['channel_id'] not in targets or comparable(row['text']) != comparable(comparable_text):
            continue
        if sent_at < datetime.fromisoformat(row['created_at']):
            continue
        matches.append(row)
    if len(matches) != 1:
        return False
    row = matches[0]
    db.execute("UPDATE publication_attempts SET status='SENT',telegram_message_id=?,telegram_response_json=?,updated_at=? WHERE attempt_id=? AND status IN ('SENDING','UNKNOWN')",
               (str(message_id), json.dumps(message, ensure_ascii=False), now(), row['attempt_id']))
    event(db, row['attempt_id'], 'SENT', {'confirmation': 'channel_post', 'message_id': str(message_id)})
    marker = ':codex-admin:'
    if marker in row['delivery_key']:
        request_key = row['delivery_key'].rsplit(marker, 1)[1]
        db.execute("UPDATE codex_publication_requests SET status='PUBLISHED',telegram_message_id=?,error_code=NULL,updated_at=? "
                   "WHERE request_key=? AND status IN ('SENDING','UNKNOWN')",
                   (str(message_id), now(), request_key))
    return True
