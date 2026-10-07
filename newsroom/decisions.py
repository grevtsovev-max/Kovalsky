"""Append-only processing decisions with reproducible rule and evidence snapshots."""
from __future__ import annotations
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS rule_snapshots (
 sha256 TEXT PRIMARY KEY, name TEXT NOT NULL, content TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS agent_decisions (
 decision_id INTEGER PRIMARY KEY,
 item_id INTEGER REFERENCES items(item_id),
 story_id INTEGER REFERENCES stories(story_id),
 decision TEXT NOT NULL,
 model_version TEXT,
 editorial_rules_version TEXT NOT NULL REFERENCES rule_snapshots(sha256),
 agent_logic_version TEXT NOT NULL REFERENCES rule_snapshots(sha256),
 payload_json TEXT NOT NULL,
 created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS agent_decisions_item_idx ON agent_decisions(item_id,decision_id);
CREATE TRIGGER IF NOT EXISTS agent_decisions_no_update BEFORE UPDATE ON agent_decisions
BEGIN SELECT RAISE(ABORT, 'decision history is append only'); END;
CREATE TRIGGER IF NOT EXISTS agent_decisions_no_delete BEFORE DELETE ON agent_decisions
BEGIN SELECT RAISE(ABORT, 'decision history is append only'); END;
CREATE TRIGGER IF NOT EXISTS rule_snapshots_no_update BEFORE UPDATE ON rule_snapshots
BEGIN SELECT RAISE(ABORT, 'rule snapshots are immutable'); END;
CREATE TRIGGER IF NOT EXISTS rule_snapshots_no_delete BEFORE DELETE ON rule_snapshots
BEGIN SELECT RAISE(ABORT, 'rule snapshots are immutable'); END;
"""


def rule_version(db, name):
    if name in {'EDITORIAL_RULES.md', 'AGENT_LOGIC.md'}:
        from .policy import document, amendments
        from .source_registry import state
        from .editorial_registry import policy, SNAPSHOT
        baseline = state(db, 'policy_v1_cutover')
        settings = {'_editorial_registry': policy(state(db, SNAPSHOT, {}))}
        if baseline:
            settings['_policy_baseline'] = baseline['legacy_rules']
            settings['_policy_confirmed_rules'] = state(db, 'policy_v1_confirmed_rules', [])
        name = 'NEWSROOM_RULES_V1.md'
        text = json.dumps([document(), amendments(settings)], ensure_ascii=False)
    else:
        text = (Path(__file__).resolve().parent.parent / name).read_text(encoding='utf-8')
    version = hashlib.sha256(text.encode()).hexdigest()
    db.execute('INSERT OR IGNORE INTO rule_snapshots(sha256,name,content) VALUES(?,?,?)', (version, name, text))
    return version


def record(db, item_id, outcome, model=None, extra=None):
    item = db.execute('SELECT * FROM items WHERE item_id=?', (item_id,)).fetchone()
    if item is None:
        return
    analysis = db.execute('SELECT * FROM item_analysis WHERE item_id=?', (item_id,)).fetchone()
    retry = db.execute("SELECT value FROM app_state WHERE key=?", ('selection_retry:' + str(item_id),)).fetchone()
    # Store actual text, not just hashes pointing at mutable items.
    payload = {'item': dict(item), 'analysis': dict(analysis) if analysis else None,
               'retry': retry[0] if retry else None,
               'source_search': [dict(r) for r in db.execute('SELECT * FROM source_search_log WHERE item_url=? ORDER BY search_id', (item['url'],))],
               **(extra or {})}
    db.execute('INSERT INTO agent_decisions(item_id,story_id,decision,model_version,editorial_rules_version,agent_logic_version,payload_json,created_at) VALUES(?,?,?,?,?,?,?,?)',
               (item_id, item['story_id'], outcome, model,
                rule_version(db, 'EDITORIAL_RULES.md'), rule_version(db, 'AGENT_LOGIC.md'),
                json.dumps(payload, ensure_ascii=False), datetime.now(timezone.utc).isoformat(timespec='seconds')))


def record_failure(db, item, source_id, model, error_code):
    """An exception is a decision outcome too; record it after rolling back partial work."""
    db.execute('INSERT INTO agent_decisions(decision,model_version,editorial_rules_version,agent_logic_version,payload_json,created_at) VALUES(?,?,?,?,?,?)',
               ('ERROR',model,rule_version(db,'EDITORIAL_RULES.md'),rule_version(db,'AGENT_LOGIC.md'),
                json.dumps({'source_id':source_id,'input':item,'error_code':error_code},ensure_ascii=False),
                datetime.now(timezone.utc).isoformat(timespec='seconds')))


def record_publication(db, post_id, outcome, error_code=None):
    post=db.execute('SELECT * FROM posts WHERE post_id=?',(post_id,)).fetchone()
    if not post:return
    binding=db.execute('SELECT * FROM post_memory WHERE post_id=?',(post_id,)).fetchone()
    attempt=db.execute('SELECT * FROM publication_attempts WHERE post_id=? ORDER BY attempt_id DESC LIMIT 1',(post_id,)).fetchone()
    payload={'post':dict(post),'memory':dict(binding) if binding else None,
             'delivery':dict(attempt) if attempt else None,'error_code':error_code}
    db.execute('INSERT INTO agent_decisions(item_id,story_id,decision,model_version,editorial_rules_version,agent_logic_version,payload_json,created_at) VALUES(?,?,?,?,?,?,?,?)',
               (binding['item_id'] if binding else None,post['story_id'],outcome,None,
                rule_version(db,'EDITORIAL_RULES.md'),rule_version(db,'AGENT_LOGIC.md'),
                json.dumps(payload,ensure_ascii=False),datetime.now(timezone.utc).isoformat(timespec='seconds')))
