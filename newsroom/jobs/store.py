"""Durable recruitment queue over read-only collector inputs."""
import hashlib
import json
import re
from datetime import datetime, timezone

VERSION = 'jobs-v1'
SCHEMA = '''
CREATE TABLE IF NOT EXISTS recruitment_queue (
 id INTEGER PRIMARY KEY, item_id INTEGER NOT NULL, revision TEXT NOT NULL,
 source_json TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'PENDING',
 attempts INTEGER NOT NULL DEFAULT 0, next_at TEXT NOT NULL,
 updated_at TEXT NOT NULL, error_code TEXT, UNIQUE(item_id,revision)
);
CREATE INDEX IF NOT EXISTS recruitment_due ON recruitment_queue(state,next_at);
CREATE TABLE IF NOT EXISTS recruitment_cards (
 queue_id INTEGER NOT NULL REFERENCES recruitment_queue(id), ordinal INTEGER NOT NULL,
 card_json TEXT NOT NULL, PRIMARY KEY(queue_id,ordinal)
);
CREATE TABLE IF NOT EXISTS recruitment_attempts (
 id INTEGER PRIMARY KEY, queue_id INTEGER NOT NULL, created_at TEXT NOT NULL,
 status TEXT NOT NULL, result_json TEXT, receipt_json TEXT, error_code TEXT
);
CREATE TABLE IF NOT EXISTS recruitment_cursor (id INTEGER PRIMARY KEY CHECK(id=1),item_id INTEGER NOT NULL);
'''

def stamp():
    return datetime.now(timezone.utc).isoformat(timespec='microseconds')

def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True)

def candidate(source):
    if re.search(r'https?://t\.me/(?:workingincrypto|job_crypto_ru|cryptoheadhunter)(?:/|$)',source.get('url',''),re.I):
        return True
    text='\n'.join(str(source.get(k) or '') for k in ('title','content','description'))
    return bool(re.search(r'#(?:ваканси\w*|vacancy|резюме|resume)|\b(?:вакансии?|we are hiring|we.re hiring|ищем\s+(?:в\s+команду|разработчика|аналитика|менеджера)|требуется\s+(?:разработчик|аналитик|менеджер|инженер))\b',text,re.I))

def initialize(db):
    db.executescript(SCHEMA)

def discover(db,limit=500,ceiling=None):
    """Independent cursor; news eligibility and disposition never affect admission."""
    initialize(db)
    cursor=db.execute('SELECT item_id FROM recruitment_cursor WHERE id=1').fetchone()
    if cursor is None:
        start=db.execute("SELECT MIN(item_id) FROM items WHERE julianday(discovered_at)>=julianday('now')-7").fetchone()[0]
        cursor_id=(start-1) if start else db.execute('SELECT COALESCE(MAX(item_id),0) FROM items').fetchone()[0]
    else:cursor_id=cursor[0]
    rows=db.execute('SELECT i.*,s.name AS source_name FROM items i JOIN sources s USING(source_id) WHERE i.item_id>? AND i.item_id<=? ORDER BY i.item_id LIMIT ?', (cursor_id,ceiling if ceiling is not None else 9223372036854775807,limit)).fetchall()
    # Re-read already admitted items: linked-source reading may finish after intake.
    revisions=db.execute('SELECT DISTINCT i.*,s.name AS source_name FROM items i JOIN sources s USING(source_id) JOIN recruitment_queue q ON q.item_id=i.item_id WHERE i.item_id<=? ORDER BY i.item_id DESC LIMIT ?', (cursor_id,limit)).fetchall()
    added=0
    for row in rows+revisions:
        source={k:row[k] for k in ('item_id','url','title','content','description','published_at','source_name','discovered_at')}
        if candidate(source):
            revision=hashlib.sha256(encoded(source).encode()).hexdigest()
            now=stamp()
            inserted=db.execute('INSERT OR IGNORE INTO recruitment_queue(item_id,revision,source_json,next_at,updated_at) VALUES(?,?,?,?,?)',(row['item_id'],revision,encoded(source),now,now)).rowcount
            added+=inserted
            if inserted:
                db.execute("UPDATE recruitment_queue SET state='SUPERSEDED' WHERE item_id=? AND revision<>? AND state<>'RUNNING'",(row['item_id'],revision))
    if rows:cursor_id=rows[-1]['item_id']
    db.execute('INSERT INTO recruitment_cursor VALUES(1,?) ON CONFLICT(id) DO UPDATE SET item_id=excluded.item_id',(cursor_id,))
    db.commit()
    return added
