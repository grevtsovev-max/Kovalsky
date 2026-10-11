import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from newsroom.db import connect
from newsroom.interests import backfill_submission_profiles, learning_context, save_submission


class InterestLearningTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = connect(str(Path(self.temp.name) / "newsroom.sqlite3"))

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def profile(self):
        return {"topics": [{"topic": "Цифровые активы", "search_terms": ["крипторынок"]}],
                "analysis_depth": "DEEP", "analysis_features": ["context", "causes", "numbers", "market_impact"],
                "analysis_guidance": "Объяснять механизм и последствия для участников."}

    def test_forwarded_post_saves_topics_and_analysis_depth(self):
        with patch("newsroom.interests.analyze_submitted_post", return_value=self.profile()):
            saved, topic_count, depth = save_submission(
                self.db, user_id="1", chat_id="2", message_id=3, forwarded_from="Канал",
                source_url="https://t.me/channel/3", text="Публикация с контекстом", ai_settings={},
            )
        row = self.db.execute("SELECT * FROM interest_submissions").fetchone()
        self.assertTrue(saved)
        self.assertEqual(topic_count, 1)
        self.assertEqual(depth, "DEEP")
        self.assertEqual(row["analysis_depth"], "DEEP")
        self.assertEqual(row["analysis_profile_extracted"], 1)
        self.assertEqual(json.loads(row["analysis_features_json"]), self.profile()["analysis_features"])
        self.assertEqual(self.db.execute("SELECT topic FROM monitoring_topics").fetchone()[0], "Цифровые активы")

    def test_profile_passes_topics_and_depth_examples_to_future_analyses(self):
        with patch("newsroom.interests.analyze_submitted_post", return_value=self.profile()):
            save_submission(self.db, user_id="1", chat_id="2", message_id=3, forwarded_from="Канал",
                            source_url="https://t.me/channel/3", text="Публикация с контекстом", ai_settings={})
        profile = learning_context(self.db)
        self.assertEqual(profile["preferred_analysis_depth"], "DEEP")
        self.assertEqual(profile["topics"][0]["topic"], "Цифровые активы")
        self.assertEqual(profile["analysis_examples"][0]["analysis_features"], self.profile()["analysis_features"])
        self.assertEqual(profile["analysis_examples"][0]["topics"], ["Цифровые активы"])

    def test_older_saved_examples_are_backfilled_once_without_reweighting_topics(self):
        self.db.execute(
            "INSERT INTO interest_submissions(telegram_user_id,chat_id,message_id,forwarded_from,source_url,text,created_at,topics_extracted) "
            "VALUES('1','2',3,'Канал','https://t.me/channel/3','Старый пересланный текст','2026-09-20T10:00:00+00:00',1)"
        )
        self.db.execute(
            "INSERT INTO monitoring_topics(topic,search_terms,examples,updated_at) VALUES(?,?,?,?)",
            ("Цифровые активы", "[]", json.dumps([{"submission_id": 1}]), "2026-09-20T10:00:00+00:00"),
        )
        with patch("newsroom.interests.analyze_submitted_post", return_value=self.profile()) as analyze:
            count = backfill_submission_profiles(self.db, {})
        row = self.db.execute("SELECT * FROM interest_submissions").fetchone()
        self.assertEqual(count, 1)
        self.assertEqual(row["analysis_depth"], "DEEP")
        self.assertEqual(row["analysis_profile_extracted"], 1)
        self.assertEqual(self.db.execute("SELECT weight FROM monitoring_topics").fetchone()[0], 1)
        analyze.assert_called_once()

    def test_old_interest_submission_schema_is_migrated(self):
        old_path = Path(self.temp.name) / "old.sqlite3"
        connection = sqlite3.connect(old_path)
        connection.execute("CREATE TABLE interest_submissions (submission_id INTEGER PRIMARY KEY, topics_extracted INTEGER NOT NULL DEFAULT 0)")
        connection.commit()
        connection.close()
        migrated = connect(str(old_path))
        try:
            columns = {row[1] for row in migrated.execute("PRAGMA table_info(interest_submissions)")}
            self.assertTrue({"analysis_depth", "analysis_features_json", "analysis_guidance", "analysis_profile_extracted"}.issubset(columns))
        finally:
            migrated.close()


if __name__ == "__main__":
    unittest.main()
