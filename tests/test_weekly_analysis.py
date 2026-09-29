import io
import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from newsroom.analysis import generate_weekly_analysis
from newsroom.db import connect


class MockResponse(io.BytesIO):
    def __enter__(self): return self
    def __exit__(self, *args): self.close()


class WeeklyAnalysisTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = str(Path(self.tmp.name) / "analysis.db")
        self.db = connect(self.path)
        self.addCleanup(self.db.close)
        for i in range(1, 5):
            self.db.execute("INSERT INTO sources(source_id,name,type,url) VALUES(?,?,'rss',?)", (i, f"Publisher {i}", f"https://publisher{i}.example/feed"))
        for i in range(1, 4):
            self.db.execute("INSERT INTO stories(story_id,canonical_topic,headline,first_seen_at,last_updated_at,latest_information) VALUES(?,?,?,?,?,?)", (i, f"topic{i}", f"Story {i}", "2026-09-25", "2026-09-25", f"Update {i}"))
            facts = {"primary_source_status": "READ", "russia_cis_impact": "DIRECT", "geographic_scope": "RUSSIA", "source_review_required": False, "independent_check": "CORROBORATED"}
            self.db.execute("INSERT INTO posts(post_id,story_id,text,status,created_at,published_at,version,post_hash,fact_check_result) VALUES(?,?,?,'PUBLISHED',?,?,1,?,?)", (i, i, f"Post {i}", "2026-09-25T17:00:00+00:00", "2026-09-25T17:00:00+00:00", f"hash{i}", json.dumps(facts)))
            primary = {"url": f"https://publisher{i}.example/story", "title": f"Evidence {i}", "publisher": f"Publisher {i}", "status": "READ", "content": f"Verified source text for event {i}, with details about digital assets and the decision."}
            self.db.execute("INSERT INTO items(source_id,url,canonical_url,title,published_at,discovered_at,content_hash,title_hash,story_id,primary_source_json) VALUES(?,?,?,?,?,?,?,?,?,?)", (i, primary["url"], primary["url"], f"Evidence {i}", "2026-09-25T16:00:00+00:00", "2026-09-25T16:00:00+00:00", f"content{i}", f"title{i}", i, json.dumps(primary)))
            self.db.execute("INSERT INTO item_analysis(item_id,model,created_at,result_json) VALUES(?,?,?,?)", (i, "test", "2026-09-25", json.dumps({"summary_ru": f"Verified event {i}", "facts": [{"text": f"Evidence fact {i}", "claim_type": "FACT"}], "independent_check": "CORROBORATED"})))
        primary = {"url": "https://publisher4.example/story", "title": "Evidence 4", "publisher": "Publisher 4", "status": "READ", "content": "Verified independent source supports a related event in digital assets."}
        self.db.execute("INSERT INTO items(source_id,url,canonical_url,title,published_at,discovered_at,content_hash,title_hash,story_id,primary_source_json) VALUES(4,?,?,?,?,?,?,?,?,?)", (primary["url"], primary["url"], "Evidence 4", "2026-09-25T15:00:00+00:00", "2026-09-25T15:00:00+00:00", "content4", "title4", 1, json.dumps(primary)))
        self.db.execute("INSERT INTO item_analysis(item_id,model,created_at,result_json) VALUES(4,'test','2026-09-25',?)", (json.dumps({"summary_ru": "Another verified event", "facts": [], "independent_check": "CORROBORATED"}),))
        self.db.commit()
        self.config = {"newsroom": {"weekly_analysis_enabled": True, "weekly_analysis_time": "20:00", "weekly_analysis_lookback_hours": 168}, "ai": {"api_key_env": "WEEKLY_ANALYSIS_TEST_KEY", "model": "test", "timeout_seconds": 5}}

    def test_builds_nonpublishable_draft_with_source_references_once(self):
        output = {"title": "Что меняется", "thesis": "Тезис с доказательствами.",
                  "sections": [{"heading": "Сигналы", "text": "Несколько событий указывают на сдвиг.", "evidence_ids": ["E1", "E2"]}],
                  "alternative_explanations": [{"explanation": "Это могут быть несвязанные решения.", "evidence_ids": ["E3"]}],
                  "what_would_change_mind": "Опровержение участников.", "open_questions": ["Каков масштаб?"], "conclusion": "Вывод осторожный."}
        api_result = {"output": [{"content": [{"type": "output_text", "text": json.dumps(output)}]}]}
        now = datetime(2026, 9, 25, 17, 30, tzinfo=timezone.utc)  # 20:30 Moscow, Friday
        with patch.dict(os.environ, {"WEEKLY_ANALYSIS_TEST_KEY": "test-key"}), \
             patch("newsroom.analysis.urllib.request.urlopen", return_value=MockResponse(json.dumps(api_result).encode())):
            draft_id = generate_weekly_analysis(self.path, self.config, now)
        self.assertEqual(draft_id, 1)
        row = self.db.execute("SELECT * FROM weekly_analysis_drafts WHERE draft_id=?", (draft_id,)).fetchone()
        self.assertEqual(row["status"], "NEEDS_REVIEW")
        self.assertIn("[E1, E2]", row["body"])
        sources = json.loads(row["source_json"])
        self.assertEqual(len(sources), 4)
        self.assertIsNone(generate_weekly_analysis(self.path, self.config, now))

    def test_does_not_generate_outside_friday_schedule(self):
        with patch("newsroom.analysis.urllib.request.urlopen") as request:
            result = generate_weekly_analysis(self.path, self.config, datetime(2026, 9, 26, 18, 0, tzinfo=timezone.utc))
        self.assertIsNone(result)
        request.assert_not_called()


if __name__ == "__main__":
    unittest.main()
