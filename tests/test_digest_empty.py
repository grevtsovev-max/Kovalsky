import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch
from newsroom.db import connect
from newsroom.cli import publish_digest

class DigestEmptyTests(unittest.TestCase):
    def check_digest(self, filtered):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / 'test.db')
            db = connect(path)
            if filtered:
                db.execute("INSERT INTO stories(story_id,canonical_topic,headline,first_seen_at,last_updated_at) VALUES(1,'test','test','2026-09-26','2026-09-26')")
                db.execute("INSERT INTO posts(story_id,text,status,created_at,published_at,version,post_hash,fact_check_result) VALUES(1,'Тест','PUBLISHED',?,?,1,'hash',?)", ('2026-09-26T16:00:00+00:00', '2026-09-26T16:00:00+00:00', '{"test_publication":true}'))
                db.commit()
            db.close()
            config = {'newsroom': {'digest_time': '19:30', 'digest_timezone': 'Europe/Moscow'}}
            with patch('newsroom.cli.datetime') as clock, patch('newsroom.cli.telegram_send', return_value='501') as send:
                clock.now.return_value = datetime(2026,9,26,17,0,tzinfo=timezone.utc)
                clock.combine.side_effect = datetime.combine
                self.assertEqual(publish_digest(path, dict(config, telegram={'chat_id': '@test_channel'})), (True, 0))
                message = send.call_args.args[1]
                if filtered:
                    self.assertIn('нет публикаций, подходящих', message)
                else:
                    self.assertIn('в канале новых публикаций не было', message)
                self.assertNotIn('последние сутки', message)
                self.assertEqual(publish_digest(path, dict(config, telegram={'chat_id': '@test_channel'})), (False, 0))
                send.assert_called_once()

    def test_filtered_posts_are_not_reported_as_no_posts(self):
        self.check_digest(True)

    def test_empty_period_and_no_duplicate_send(self):
        self.check_digest(False)
