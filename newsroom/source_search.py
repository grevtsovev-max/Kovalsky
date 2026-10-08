"""Historical storage retained after removal of the editorial implementation."""
from __future__ import annotations
import re
import unicodedata

SCHEMA = """
CREATE TABLE IF NOT EXISTS source_search_log (
 search_id INTEGER PRIMARY KEY, item_url TEXT NOT NULL, attempt INTEGER NOT NULL,
 strategy TEXT NOT NULL, query TEXT NOT NULL, outcome TEXT NOT NULL,
 revision_hash TEXT NOT NULL DEFAULT '',
 checked_json TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS source_search_item_idx ON source_search_log(item_url,search_id);
CREATE TRIGGER IF NOT EXISTS source_search_no_update BEFORE UPDATE ON source_search_log
BEGIN SELECT RAISE(ABORT,'search history is append only'); END;
CREATE TRIGGER IF NOT EXISTS source_search_no_delete BEFORE DELETE ON source_search_log
BEGIN SELECT RAISE(ABORT,'search history is append only'); END;
"""

