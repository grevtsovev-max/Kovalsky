"""Historical storage retained after removal of the editorial implementation."""
from __future__ import annotations
import re
import unicodedata

SCHEMA="""
CREATE TABLE IF NOT EXISTS publication_coverage (
 coverage_id INTEGER PRIMARY KEY, post_id INTEGER NOT NULL REFERENCES posts(post_id),
 story_id INTEGER NOT NULL REFERENCES stories(story_id), slot_key TEXT NOT NULL,
 subject TEXT NOT NULL,predicate TEXT NOT NULL,scope TEXT NOT NULL,value TEXT NOT NULL,
 claim_type TEXT NOT NULL,post_quote TEXT NOT NULL,post_text TEXT NOT NULL,
 provenance TEXT NOT NULL DEFAULT 'HISTORICAL_POST_ONLY', created_at TEXT NOT NULL,
 UNIQUE(post_id,slot_key,value,claim_type)
);
CREATE INDEX IF NOT EXISTS publication_coverage_slot_idx ON publication_coverage(slot_key);
CREATE TABLE IF NOT EXISTS archive_memory_runs (
 run_id INTEGER PRIMARY KEY, post_id INTEGER NOT NULL REFERENCES posts(post_id),
 status TEXT NOT NULL, model TEXT, payload_json TEXT NOT NULL,created_at TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS archive_runs_no_update BEFORE UPDATE ON archive_memory_runs
BEGIN SELECT RAISE(ABORT,'archive history is immutable'); END;
CREATE TRIGGER IF NOT EXISTS archive_runs_no_delete BEFORE DELETE ON archive_memory_runs
BEGIN SELECT RAISE(ABORT,'archive history is immutable'); END;
CREATE TRIGGER IF NOT EXISTS coverage_no_update BEFORE UPDATE ON publication_coverage
BEGIN SELECT RAISE(ABORT,'coverage history is immutable'); END;
CREATE TRIGGER IF NOT EXISTS coverage_no_delete BEFORE DELETE ON publication_coverage
BEGIN SELECT RAISE(ABORT,'coverage history is immutable'); END;
"""

