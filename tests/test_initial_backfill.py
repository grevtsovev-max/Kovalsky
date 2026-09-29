import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from newsroom.core import run_cycle
from newsroom.db import connect


class InitialBackfillTests(unittest.TestCase):
    def run_first_cycle(self, age_hours, settings=None):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "test.db")
            config = {"newsroom": {"database": path, **(settings or {})},
                      "sources": [{"name": "Test", "type": "rss", "url": "https://example.org/feed"}]}
            item = {"url": "https://example.org/story", "title": "Crypto market", "content": "Market event",
                    "published_at": (datetime.now(timezone.utc) - timedelta(hours=age_hours)).isoformat()}
            with patch("newsroom.core.fetch_rss", return_value=[item]), patch("newsroom.core.fetch_publisher_article", side_effect=TimeoutError), patch("newsroom.core.analyze_with_ai") as ai:
                run_cycle(config)
                ai.assert_not_called()
            db = connect(path)
            try:
                row = db.execute("SELECT disposition FROM items").fetchone()
                self.assertEqual(db.execute("SELECT COUNT(*) FROM posts").fetchone()[0], 0)
                return row[0]
            finally:
                db.close()

    def test_eight_hour_old_story_reaches_source_verification(self):
        self.assertEqual(self.run_first_cycle(8), "PRIMARY_RETRY")

    def test_old_story_stays_outside_initial_window(self):
        self.assertEqual(self.run_first_cycle(49), "BASELINE_SKIPPED")

    def test_default_follows_configured_freshness_window(self):
        self.assertEqual(self.run_first_cycle(60, {"freshness_window_hours": 72}), "PRIMARY_RETRY")

    def test_explicit_narrow_window_is_respected(self):
        self.assertEqual(self.run_first_cycle(8, {"initial_backfill_minutes": 15}), "BASELINE_SKIPPED")
