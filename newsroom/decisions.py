"""Historical storage retained after removal of the editorial implementation."""
from __future__ import annotations
import re
import unicodedata

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

