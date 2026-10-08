"""Historical storage retained after removal of the editorial implementation."""
from __future__ import annotations
import re
import unicodedata

SCHEMA = """
CREATE TABLE IF NOT EXISTS story_monitoring (
 story_id INTEGER PRIMARY KEY REFERENCES stories(story_id), lifecycle TEXT NOT NULL,
 priority INTEGER NOT NULL, interval_minutes INTEGER NOT NULL, dormant_after_days INTEGER NOT NULL,
 last_event_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS story_monitoring_jobs (
 job_id INTEGER PRIMARY KEY, story_id INTEGER NOT NULL REFERENCES stories(story_id),
 target_type TEXT NOT NULL, target TEXT NOT NULL, next_check_at TEXT NOT NULL,
 last_checked_at TEXT, active INTEGER NOT NULL DEFAULT 1, UNIQUE(story_id,target)
);
CREATE INDEX IF NOT EXISTS story_jobs_due_idx ON story_monitoring_jobs(active,next_check_at);
CREATE TABLE IF NOT EXISTS story_watch_log (
 log_id INTEGER PRIMARY KEY, story_id INTEGER NOT NULL REFERENCES stories(story_id),
 job_id INTEGER REFERENCES story_monitoring_jobs(job_id), action TEXT NOT NULL,
 payload_json TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS story_watch_no_update BEFORE UPDATE ON story_watch_log
BEGIN SELECT RAISE(ABORT,'watch history is append only'); END;
CREATE TRIGGER IF NOT EXISTS story_watch_no_delete BEFORE DELETE ON story_watch_log
BEGIN SELECT RAISE(ABORT,'watch history is append only'); END;
"""

