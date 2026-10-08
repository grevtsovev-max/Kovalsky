"""Historical storage; no executable editorial logic."""
SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
 event_id INTEGER PRIMARY KEY, story_id INTEGER NOT NULL REFERENCES stories(story_id),
 identity_key TEXT NOT NULL UNIQUE, canonical_event TEXT NOT NULL,
 identity_json TEXT NOT NULL, first_seen_at TEXT NOT NULL, last_seen_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS events_story_idx ON events(story_id);
CREATE TABLE IF NOT EXISTS event_items (
 event_id INTEGER NOT NULL REFERENCES events(event_id), item_id INTEGER NOT NULL REFERENCES items(item_id),
 created_at TEXT NOT NULL, PRIMARY KEY(event_id,item_id)
);
CREATE TABLE IF NOT EXISTS source_snapshots (
 snapshot_id INTEGER PRIMARY KEY, item_id INTEGER NOT NULL REFERENCES items(item_id),
 url TEXT NOT NULL, content TEXT NOT NULL, content_hash TEXT NOT NULL,
 read_status TEXT NOT NULL, origin_status TEXT NOT NULL, source_type TEXT NOT NULL,
 captured_at TEXT NOT NULL, publication_date TEXT, discovery_date TEXT,
 UNIQUE(item_id,url,content_hash,origin_status)
);
CREATE TABLE IF NOT EXISTS story_facts (
 fact_id INTEGER PRIMARY KEY, story_id INTEGER NOT NULL REFERENCES stories(story_id),
 event_id INTEGER NOT NULL REFERENCES events(event_id), identity_key TEXT NOT NULL UNIQUE,
 slot_key TEXT NOT NULL, subject TEXT NOT NULL, predicate TEXT NOT NULL, scope TEXT NOT NULL,
 value TEXT NOT NULL, statement TEXT NOT NULL, fact_type TEXT NOT NULL,
 valid_from TEXT, valid_to TEXT, created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS facts_story_idx ON story_facts(story_id);
CREATE INDEX IF NOT EXISTS facts_slot_idx ON story_facts(slot_key);
CREATE TABLE IF NOT EXISTS fact_evidence (
 fact_id INTEGER NOT NULL REFERENCES story_facts(fact_id),
 snapshot_id INTEGER NOT NULL REFERENCES source_snapshots(snapshot_id),
 quote TEXT NOT NULL, created_at TEXT NOT NULL,
 PRIMARY KEY(fact_id,snapshot_id,quote)
);
CREATE TABLE IF NOT EXISTS fact_relations (
 relation_id INTEGER PRIMARY KEY, old_fact_id INTEGER NOT NULL REFERENCES story_facts(fact_id),
 new_fact_id INTEGER NOT NULL REFERENCES story_facts(fact_id),
 relation TEXT NOT NULL CHECK(relation IN ('SUPERSEDES','CONFIRMS','CONTRADICTS','RETRACTS')),
 snapshot_id INTEGER NOT NULL REFERENCES source_snapshots(snapshot_id),
 quote TEXT NOT NULL, created_at TEXT NOT NULL,
 UNIQUE(old_fact_id,new_fact_id,relation)
);
CREATE TABLE IF NOT EXISTS story_diffs (
 diff_id INTEGER PRIMARY KEY, story_id INTEGER NOT NULL REFERENCES stories(story_id),
 event_id INTEGER NOT NULL REFERENCES events(event_id), item_id INTEGER NOT NULL REFERENCES items(item_id),
 payload_json TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS post_facts (
 post_id INTEGER NOT NULL REFERENCES posts(post_id), fact_id INTEGER NOT NULL REFERENCES story_facts(fact_id),
 post_quote TEXT NOT NULL, PRIMARY KEY(post_id,fact_id)
);
CREATE TABLE IF NOT EXISTS post_memory (
 post_id INTEGER PRIMARY KEY REFERENCES posts(post_id), diff_id INTEGER NOT NULL REFERENCES story_diffs(diff_id),
 item_id INTEGER NOT NULL REFERENCES items(item_id), event_id INTEGER NOT NULL REFERENCES events(event_id),
 text_hash TEXT NOT NULL
);
"""

