from __future__ import annotations

import sqlite3
from pathlib import Path


SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS sources (
  source_id INTEGER PRIMARY KEY, name TEXT NOT NULL, type TEXT NOT NULL,
  url TEXT NOT NULL UNIQUE, active INTEGER NOT NULL DEFAULT 1,
  priority INTEGER NOT NULL DEFAULT 1, reputation TEXT NOT NULL DEFAULT 'unknown',
  last_checked_at TEXT, last_seen_published_at TEXT, last_error TEXT
);
CREATE TABLE IF NOT EXISTS items (
  item_id INTEGER PRIMARY KEY, source_id INTEGER NOT NULL REFERENCES sources(source_id),
  url TEXT NOT NULL, canonical_url TEXT NOT NULL, title TEXT NOT NULL,
  description TEXT NOT NULL DEFAULT '', content TEXT NOT NULL DEFAULT '', author TEXT,
  published_at TEXT, updated_at TEXT, discovered_at TEXT NOT NULL, processed_at TEXT,
  content_hash TEXT NOT NULL, title_hash TEXT NOT NULL,
  disposition TEXT NOT NULL DEFAULT 'PENDING', story_id INTEGER,
  primary_source_json TEXT NOT NULL DEFAULT '{}',
  UNIQUE(source_id, canonical_url), UNIQUE(source_id, content_hash)
);
CREATE INDEX IF NOT EXISTS items_story_idx ON items(story_id);
CREATE TABLE IF NOT EXISTS stories (
  story_id INTEGER PRIMARY KEY, canonical_topic TEXT NOT NULL, headline TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'ACTIVE', importance TEXT NOT NULL DEFAULT 'MEDIUM',
  first_seen_at TEXT NOT NULL, last_updated_at TEXT NOT NULL,
  last_published_at TEXT, last_source_published_at TEXT,
  latest_information TEXT NOT NULL DEFAULT '', known_facts TEXT NOT NULL DEFAULT '[]',
  entities TEXT NOT NULL DEFAULT '[]', keywords TEXT NOT NULL DEFAULT '[]',
  source_count INTEGER NOT NULL DEFAULT 0, version INTEGER NOT NULL DEFAULT 1,
  publication_count INTEGER NOT NULL DEFAULT 0, last_content_hash TEXT
);
CREATE TABLE IF NOT EXISTS story_timeline (
  timeline_id INTEGER PRIMARY KEY, story_id INTEGER NOT NULL REFERENCES stories(story_id),
  item_id INTEGER REFERENCES items(item_id), timestamp TEXT NOT NULL,
  source_name TEXT NOT NULL, new_information TEXT NOT NULL, confidence REAL NOT NULL DEFAULT 0.5,
  UNIQUE(story_id, item_id)
);
CREATE TABLE IF NOT EXISTS posts (
  post_id INTEGER PRIMARY KEY, story_id INTEGER NOT NULL REFERENCES stories(story_id),
  text TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'PENDING', created_at TEXT NOT NULL,
  published_at TEXT, external_id TEXT, version INTEGER NOT NULL,
  source_ids TEXT NOT NULL DEFAULT '[]', post_hash TEXT NOT NULL,
  editor_decision TEXT NOT NULL DEFAULT 'PENDING', fact_check_result TEXT NOT NULL DEFAULT '{}',
  auto_attempts INTEGER NOT NULL DEFAULT 0, auto_last_error TEXT
);
CREATE TABLE IF NOT EXISTS errors (
  error_id INTEGER PRIMARY KEY, source_id INTEGER, timestamp TEXT NOT NULL,
  message TEXT NOT NULL, retry_count INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS item_analysis (
  analysis_id INTEGER PRIMARY KEY, item_id INTEGER NOT NULL UNIQUE REFERENCES items(item_id),
  model TEXT NOT NULL, created_at TEXT NOT NULL, result_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS item_revisions (
  revision_id INTEGER PRIMARY KEY,
  item_id INTEGER NOT NULL REFERENCES items(item_id),
  observed_at TEXT NOT NULL,
  revision_hash TEXT NOT NULL,
  source_snapshot_json TEXT NOT NULL,
  decision_snapshot_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS item_revisions_item_idx ON item_revisions(item_id, revision_id DESC);
CREATE TRIGGER IF NOT EXISTS item_revisions_no_update BEFORE UPDATE ON item_revisions
BEGIN SELECT RAISE(ABORT,'item revision history is append only'); END;
CREATE TRIGGER IF NOT EXISTS item_revisions_no_delete BEFORE DELETE ON item_revisions
BEGIN SELECT RAISE(ABORT,'item revision history is append only'); END;
CREATE TABLE IF NOT EXISTS app_state (
  key TEXT PRIMARY KEY, value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS editor_notifications (
  notification_id INTEGER PRIMARY KEY,
  post_id INTEGER NOT NULL REFERENCES posts(post_id),
  chat_id TEXT NOT NULL, message_id INTEGER NOT NULL, created_at TEXT NOT NULL,
  UNIQUE(chat_id, message_id)
);
CREATE TABLE IF NOT EXISTS weekly_analysis_drafts (
  draft_id INTEGER PRIMARY KEY, created_at TEXT NOT NULL, period_start TEXT NOT NULL,
  period_end TEXT NOT NULL, title TEXT NOT NULL, thesis TEXT NOT NULL,
  body TEXT NOT NULL, analysis_json TEXT NOT NULL, source_json TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'NEEDS_REVIEW'
);
CREATE TABLE IF NOT EXISTS interest_submissions (
  submission_id INTEGER PRIMARY KEY, telegram_user_id TEXT NOT NULL,
  chat_id TEXT NOT NULL, message_id INTEGER NOT NULL, forwarded_from TEXT,
  source_url TEXT, text TEXT NOT NULL, created_at TEXT NOT NULL,
  topics_extracted INTEGER NOT NULL DEFAULT 0,
  analysis_depth TEXT, analysis_features_json TEXT NOT NULL DEFAULT '[]',
  analysis_guidance TEXT NOT NULL DEFAULT '', analysis_profile_extracted INTEGER NOT NULL DEFAULT 0,
  UNIQUE(chat_id, message_id)
);
CREATE TABLE IF NOT EXISTS monitoring_topics (
  topic_id INTEGER PRIMARY KEY, topic TEXT NOT NULL UNIQUE,
  search_terms TEXT NOT NULL DEFAULT '[]', examples TEXT NOT NULL DEFAULT '[]',
  weight INTEGER NOT NULL DEFAULT 1, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS editorial_feedback (
  feedback_id INTEGER PRIMARY KEY,
  created_at TEXT NOT NULL,
  item_id INTEGER REFERENCES items(item_id),
  story_id INTEGER REFERENCES stories(story_id),
  post_id INTEGER REFERENCES posts(post_id),
  feedback_type TEXT NOT NULL,
  reason TEXT NOT NULL,
  item_title TEXT NOT NULL DEFAULT '',
  post_text TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS editorial_feedback_recent_idx ON editorial_feedback(created_at DESC);
CREATE TABLE IF NOT EXISTS telegram_post_edits (
  update_id INTEGER PRIMARY KEY,
  post_id INTEGER NOT NULL REFERENCES posts(post_id),
  telegram_chat_id TEXT NOT NULL,
  telegram_message_id TEXT NOT NULL,
  edit_date TEXT NOT NULL,
  previous_text TEXT NOT NULL,
  edited_text TEXT NOT NULL,
  captured_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS telegram_post_edits_post_idx ON telegram_post_edits(post_id,update_id DESC);
CREATE TABLE IF NOT EXISTS telegram_review_inbox (
  update_id INTEGER PRIMARY KEY,
  payload_json TEXT NOT NULL,
  received_at TEXT NOT NULL,
  processed_at TEXT,
  outcome TEXT
);
CREATE TABLE IF NOT EXISTS telegram_public_snapshots (
  post_id INTEGER PRIMARY KEY REFERENCES posts(post_id),
  text TEXT NOT NULL,
  observed_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS telegram_edit_acknowledgements (
  acknowledgement_id INTEGER PRIMARY KEY,
  update_id INTEGER NOT NULL REFERENCES telegram_post_edits(update_id),
  telegram_user_id TEXT NOT NULL,
  telegram_message_id INTEGER,
  learning_summary TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'SENT',
  created_at TEXT NOT NULL,
  UNIQUE(update_id,telegram_user_id),
  UNIQUE(telegram_user_id,telegram_message_id)
);
CREATE TABLE IF NOT EXISTS telegram_edit_replies (
  update_id INTEGER PRIMARY KEY,
  acknowledgement_id INTEGER NOT NULL REFERENCES telegram_edit_acknowledgements(acknowledgement_id),
  reply_text TEXT NOT NULL,
  is_confirmation INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS interest_feedback (
  item_id INTEGER PRIMARY KEY REFERENCES items(item_id),
  is_interesting INTEGER NOT NULL CHECK(is_interesting IN (0,1)),
  note TEXT NOT NULL, topics_json TEXT NOT NULL DEFAULT '[]',
  previous_disposition TEXT, updated_at TEXT NOT NULL
);
"""


def connect(path: str) -> sqlite3.Connection:
    db_path = Path(path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(db_path, timeout=30)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.executescript(SCHEMA)
    from .delivery import SCHEMA as DELIVERY_SCHEMA
    db.executescript(DELIVERY_SCHEMA)
    from .decisions import SCHEMA as DECISION_SCHEMA
    db.executescript(DECISION_SCHEMA)
    from .knowledge import SCHEMA as KNOWLEDGE_SCHEMA
    db.executescript(KNOWLEDGE_SCHEMA)
    from .source_search import SCHEMA as SEARCH_SCHEMA
    db.executescript(SEARCH_SCHEMA)
    search_columns = {row[1] for row in db.execute("PRAGMA table_info(source_search_log)")}
    if "revision_hash" not in search_columns:
        db.execute("ALTER TABLE source_search_log ADD COLUMN revision_hash TEXT NOT NULL DEFAULT ''")
    from .watch import SCHEMA as WATCH_SCHEMA
    db.executescript(WATCH_SCHEMA)
    from .archive_memory import SCHEMA as ARCHIVE_SCHEMA
    db.executescript(ARCHIVE_SCHEMA)
    item_columns = {row[1] for row in db.execute("PRAGMA table_info(items)")}
    edit_columns = {row[1] for row in db.execute("PRAGMA table_info(telegram_post_edits)")}
    for name, declaration in (("capture_source", "TEXT NOT NULL DEFAULT 'TELEGRAM_UPDATE'"), ("source_url", "TEXT")):
        if name not in edit_columns:
            db.execute(f"ALTER TABLE telegram_post_edits ADD COLUMN {name} {declaration}")
    if "primary_source_json" not in item_columns:
        db.execute("ALTER TABLE items ADD COLUMN primary_source_json TEXT NOT NULL DEFAULT '{}' ")
    post_columns = {row[1] for row in db.execute("PRAGMA table_info(posts)")}
    for name, declaration in (("auto_attempts", "INTEGER NOT NULL DEFAULT 0"), ("auto_last_error", "TEXT")):
        if name not in post_columns:
            db.execute(f"ALTER TABLE posts ADD COLUMN {name} {declaration}")
    source_columns = {row[1] for row in db.execute("PRAGMA table_info(sources)")}
    for name, declaration in (
        ("source_role", "TEXT NOT NULL DEFAULT 'aggregator'"),
        ("last_success_at", "TEXT"),
        ("consecutive_failures", "INTEGER NOT NULL DEFAULT 0"),
        ("recovery_before", "INTEGER"),
        ("recovery_since", "TEXT"),
        ("recovery_latest", "TEXT"),
    ):
        if name not in source_columns:
            db.execute(f"ALTER TABLE sources ADD COLUMN {name} {declaration}")
    submission_columns = {row[1] for row in db.execute("PRAGMA table_info(interest_submissions)")}
    for name, declaration in (
        ("analysis_depth", "TEXT"),
        ("analysis_features_json", "TEXT NOT NULL DEFAULT '[]'"),
        ("analysis_guidance", "TEXT NOT NULL DEFAULT ''"),
        ("analysis_profile_extracted", "INTEGER NOT NULL DEFAULT 0"),
    ):
        if name not in submission_columns:
            db.execute(f"ALTER TABLE interest_submissions ADD COLUMN {name} {declaration}")
    feedback_columns = {row[1] for row in db.execute("PRAGMA table_info(interest_feedback)")}
    for name, declaration in (("topics_json", "TEXT NOT NULL DEFAULT '[]'"),
                              ("previous_disposition", "TEXT")):
        if name not in feedback_columns:
            db.execute(f"ALTER TABLE interest_feedback ADD COLUMN {name} {declaration}")
    db.execute("UPDATE interest_feedback SET previous_disposition='WAITING_CONFIRMATION' "
               "WHERE is_interesting=0 AND previous_disposition IS NULL AND note='Пользователь отметил как неинтересное' "
               "AND item_id IN (SELECT item_id FROM items WHERE disposition='NOISE')")
    db.commit()
    return db


def connect_readonly(path: str) -> sqlite3.Connection:
    """Open an existing database for queries without running schema or data migrations."""
    db_path = Path(path).expanduser().resolve()
    db = sqlite3.connect(db_path.as_uri() + "?mode=ro", uri=True, timeout=5)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA query_only=ON")
    db.execute("PRAGMA busy_timeout=5000")
    return db
