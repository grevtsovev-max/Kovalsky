import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from newsroom.cli import publish_digest
from newsroom.db import connect
from tests.digest_support import present_posts


class WeeklyDigestTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = str(Path(self.tmp.name) / "weekly.db")
        self.db = connect(self.path)
        self.addCleanup(self.db.close)
        presence = patch('newsroom.channel_presence.inspect_channel_posts',
                         side_effect=lambda config, ids, **kw: present_posts(self.db, 'weekly_test', ids, **kw))
        presence.start()
        self.addCleanup(presence.stop)
        self.db.execute("INSERT INTO stories(story_id,canonical_topic,headline,first_seen_at,last_updated_at) VALUES(1,'topic','topic','2026-09-18','2026-09-18')")
        self.config = {"newsroom": {"weekly_digest_time": "19:30", "digest_timezone": "Europe/Moscow", "weekly_digest_lookback_hours": 168},
                       "telegram": {"chat_id": "@weekly_test", "chat_id_env": "WEEKLY_TEST_UNUSED"}}

    def add_post(self, post_id, published_at):
        facts = {"importance": "HIGH", "geographic_scope": "RUSSIA", "test_publication": False}
        self.db.execute("INSERT INTO posts(post_id,story_id,text,status,created_at,published_at,external_id,version,post_hash,fact_check_result) VALUES(?,1,?,'PUBLISHED',?,?,?,1,?,?)",
                        (post_id, f"Регулятор объявил решение {post_id}\n\nПодтверждённые подробности новости.", published_at, published_at, str(post_id), str(post_id), json.dumps(facts)))
        self.db.commit()

    def run_saturday(self, hour, minute=30):
        with patch("newsroom.cli.datetime") as clock, patch("newsroom.cli.telegram_send", return_value="501") as send:
            clock.now.return_value = datetime(2026, 9, 26, hour, minute, tzinfo=timezone.utc)
            clock.combine.side_effect = datetime.combine
            result = publish_digest(self.path, self.config, kind="weekly")
            return result, send

    def test_saturday_schedule_uses_week_window_and_sends_once(self):
        self.add_post(1, "2026-09-25T15:00:00+00:00")
        self.add_post(2, "2026-09-19T15:59:00+00:00")
        result, send = self.run_saturday(15, 59)
        self.assertEqual(result, (False, 0))
        send.assert_not_called()
        result, send = self.run_saturday(16, 0)
        self.assertEqual(result, (True, 1))
        message = send.call_args.args[1]
        self.assertIn("главное за неделю", message)
        self.assertIn("/1", message)
        self.assertNotIn("/2", message)
        self.assertIsNone(self.db.execute("SELECT value FROM app_state WHERE key='digest_last_local_date'").fetchone())
        result, send = self.run_saturday(17, 0)
        self.assertEqual(result, (False, 0))
        send.assert_not_called()


if __name__ == "__main__":
    unittest.main()
