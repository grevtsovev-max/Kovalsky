import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from newsroom.db import connect
from newsroom.review import run_review_bot


class ReviewStartupSafetyTests(unittest.TestCase):
    def test_startup_does_not_create_corrections_for_existing_posts(self):
        with tempfile.TemporaryDirectory() as directory:
            database = str(Path(directory) / "test.sqlite3")
            db = connect(database)
            now = "2026-10-05T09:00:00+00:00"
            db.execute(
                "INSERT INTO stories(canonical_topic,headline,first_seen_at,last_updated_at) "
                "VALUES(?,?,?,?)", ("test", "Synthetic story", now, now),
            )
            for number in range(1, 101):
                db.execute(
                    "INSERT INTO posts(story_id,text,status,created_at,version,source_ids,"
                    "post_hash,fact_check_result,external_id) "
                    "VALUES(1,?,'PUBLISHED',?,1,'[]',?,'{}',?)",
                    ("Synthetic publication", now, f"synthetic-{number}", str(number)),
                )
            db.commit()
            db.close()
            config = {"newsroom": {"database": database},
                      "telegram": {"interest_owner_user_ids": [1001]}}

            def telegram_api(_config, method, _payload, **kwargs):
                if method == "getMe":
                    return {}
                if method == "getUpdates":
                    raise KeyboardInterrupt
                raise AssertionError(f"Unexpected Telegram mutation: {method}")

            with patch("newsroom.review.telegram_api", side_effect=telegram_api), \
                 patch("newsroom.interests.backfill_submission_profiles", return_value=0), \
                 patch("newsroom.edit_sync.sync_recent_channel_edits", return_value=0):
                run_review_bot(config)

            db = connect(database)
            try:
                for table in ("editorial_feedback", "telegram_feedback_corrections"):
                    self.assertEqual(db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0], 0)
                self.assertEqual(db.execute("SELECT COUNT(*) FROM posts WHERE status='PUBLISHED'").fetchone()[0], 100)
            finally:
                db.close()


if __name__ == "__main__":
    unittest.main()
