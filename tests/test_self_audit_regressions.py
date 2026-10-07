import json
import hashlib
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from newsroom.core import (_WebSearchQuota, _requeue_social_quote_repairs,
                           _restore_exact_social_headline_evidence,
                           process_item, require_primary_source_review, run_cycle)
from newsroom.db import connect, connect_readonly
from newsroom.quality import editorial_issues


class SelfAuditRegressionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = str(Path(self.temp.name) / "newsroom.sqlite3")
        self.db = connect(self.path)
        self.addCleanup(self.db.close)
        self.db.execute("INSERT INTO sources(name,type,url,source_role) VALUES(?,?,?,?)",
                        ("ПрофCFA", "telegram", "https://t.me/procfapro", "publisher"))
        self.db.commit()
        self.source = self.db.execute("SELECT * FROM sources").fetchone()
        self.now = datetime.now(timezone.utc).isoformat(timespec="seconds")

    def test_changed_content_at_same_url_is_archived_and_reprocessed(self):
        item = {
            "url": "https://t.me/procfapro/909", "title": "НАУФОР подала заявку на статус СРО",
            "description": "НАУФОР отправила документы в Банк России.",
            "content": "НАУФОР отправила документы в Банк России для получения статуса СРО.",
            "published_at": self.now, "updated_at": self.now,
            "primary_source_status": "READ", "primary_source_url": "https://t.me/procfapro/909",
            "primary_source_title": "НАУФОР подала заявку на статус СРО",
            "primary_source_content": "НАУФОР подала заявку на статус СРО. "
                                       "НАУФОР отправила документы в Банк России для получения статуса СРО.",
            "primary_source_type": "ORIGINAL_SOCIAL_PUBLISHER",
            "primary_source_publisher": "ПрофCFA",
        }
        settings = {"_disabled_for_cycle": True}
        first = process_item(self.db, self.source, dict(item), .35, 3500, 48,
                             ai_settings=settings)
        self.assertEqual(first, "AI_RETRY")

        revised = dict(item, title="НАУФОР подала документы на создание СРО",
                       content=item["content"] + " Президент сообщил о нескольких участниках реестра.",
                       updated_at=self.now)
        outcome = process_item(self.db, self.source, revised, .35, 3500, 48,
                               ai_settings={"_disabled_for_cycle": True})
        self.assertNotEqual(outcome, "DUPLICATE")
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM items").fetchone()[0], 1)
        current = self.db.execute("SELECT title,content,disposition FROM items").fetchone()
        self.assertEqual(current["title"], revised["title"])
        self.assertIn("нескольких участниках", current["content"])
        self.assertEqual(current["disposition"], "AI_RETRY")
        revision = self.db.execute("SELECT * FROM item_revisions").fetchone()
        self.assertEqual(json.loads(revision["source_snapshot_json"])["title"], item["title"])
        self.assertTrue(json.loads(revision["decision_snapshot_json"])["analysis"] is None)

    def test_exact_read_social_headline_repairs_missing_model_quote(self):
        title = "НАУФОР подала заявку на статус СРО в криптосфере"
        source = {"type": "ORIGINAL_SOCIAL_PUBLISHER", "url": "https://t.me/procfapro/909",
                  "content": title + ". Ассоциация отправила пакет документов в Банк России."}
        result = {"action": "NEW_STORY", "publication_recommendation": "AUTO_PUBLISH",
                  "facts": [{"claim_type": "REPORT"}],
                  "original_reporting_check": {"central_claim_supported": False,
                                                "attribution_preserved": True, "evidence": ""}}
        repaired = _restore_exact_social_headline_evidence(result, {"title": title}, source)
        checked = require_primary_source_review(repaired, "READ", source)
        self.assertEqual(checked["original_reporting_check"]["evidence"], title)
        self.assertEqual(checked["_evidence_repair"], "EXACT_READ_SOCIAL_HEADLINE")
        self.assertEqual(checked["publication_recommendation"], "AUTO_PUBLISH")

    def test_old_fresh_exact_social_rejection_is_queued_once_for_new_filter(self):
        title = "НАУФОР подала заявку на статус СРО в криптосфере"
        impact = "При одобрении НАУФОР сможет регулировать криптообменники и цифровые депозитарии."
        primary = {"type": "ORIGINAL_SOCIAL_PUBLISHER", "status": "READ",
                   "url": "https://t.me/procfapro/909", "content": title + ". " + impact}
        cursor = self.db.execute(
            "INSERT INTO items(source_id,url,canonical_url,title,content,published_at,updated_at,discovered_at,"
            "content_hash,title_hash,disposition,primary_source_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (self.source["source_id"], primary["url"], primary["url"], title, primary["content"],
             self.now, self.now, self.now, "old-content", "old-title", "REJECTED",
             json.dumps(primary, ensure_ascii=False)))
        analysis = {"_filter_version": 20, "original_reporting_check": {
            "central_claim_supported": False, "attribution_preserved": True, "evidence": ""},
            "is_relevant": True, "geographic_scope": "RUSSIA", "russia_cis_impact": "DIRECT",
            "impact_evidence": impact, "independent_check": "NOT_ASSESSED",
            "facts": [{"claim_type": "REPORT"}]}
        self.db.execute("INSERT INTO item_analysis(item_id,model,created_at,result_json) VALUES(?,?,?,?)",
                        (cursor.lastrowid, "test", self.now, json.dumps(analysis)))
        self.db.commit()
        with patch("newsroom.core.get_api_key", return_value="test-key"):
            self.assertEqual(_requeue_social_quote_repairs(self.db, {'_policy_baseline': []}, 48), 0)
            self.assertEqual(self.db.execute("SELECT disposition FROM items WHERE item_id=?", (cursor.lastrowid,)).fetchone()[0], 'REJECTED')
            self.assertEqual(_requeue_social_quote_repairs(self.db, {}, 48), 1)
            self.assertEqual(_requeue_social_quote_repairs(self.db, {}, 48), 0)
        self.assertEqual(self.db.execute("SELECT disposition FROM items WHERE item_id=?",
                                         (cursor.lastrowid,)).fetchone()[0], "AI_RETRY")

    def test_search_cooldown_is_persistent_and_independent_per_purpose(self):
        quota = _WebSearchQuota(self.db, 15)
        self.assertEqual(quota.interval_minutes, 3)
        self.assertTrue(quota.reserve("https://search.example/one"))
        self.assertFalse(quota.reserve("https://search.example/two"))
        self.assertTrue(quota.reserve_primary_recovery())
        self.assertFalse(quota.reserve_primary_recovery())
        self.assertTrue(quota.reserve_story_watch())
        self.assertFalse(_WebSearchQuota(self.db, 15).reserve_story_watch())
        earlier = (datetime.now(timezone.utc) - timedelta(minutes=4)).isoformat(timespec="seconds")
        self.db.execute("UPDATE app_state SET value=? WHERE key='web_search_last_call_at:feeds'", (earlier,))
        self.db.commit()
        self.assertTrue(quota.reserve("https://search.example/two"))
        self.assertFalse(quota.reserve_primary_recovery())

    def test_cycle_searches_all_topics_in_one_request_per_window(self):
        config = {"newsroom": {"database": self.path}, "ai": {},
                  "web_search": {"enabled": True, "min_interval_minutes": 15},
                  "sources": [{"name": f"Тема {n}", "type": "web_search",
                               "url": f"web-search://{n}", "query": f"topic {n}"}
                              for n in range(4)]}
        with patch("newsroom.core.fetch_web_search", return_value=[]) as search:
            run_cycle(config)
            self.assertEqual(search.call_count, 1)
            self.assertEqual(len(search.call_args.args[0]), 4)
            search.reset_mock()
            run_cycle(config)
            search.assert_not_called()

    def test_read_only_dashboard_connection_cannot_migrate_or_write(self):
        readonly = connect_readonly(self.path)
        self.addCleanup(readonly.close)
        self.assertEqual(readonly.execute("SELECT COUNT(*) FROM sources").fetchone()[0], 1)
        with self.assertRaises(Exception):
            readonly.execute("INSERT INTO app_state(key,value) VALUES('dashboard-test','1')")

    def test_service_search_note_is_detected_in_finished_post(self):
        flags = {key: True for key in ("source_matches_event", "attribution_preserved", "stage_preserved",
                                       "headline_main_event", "lead_event_first", "paragraphs_concise_distinct",
                                       "no_editorial_process_notes")}
        facts = {"geographic_scope": "RUSSIA", "editorial_check": flags}
        issues = editorial_issues("🇷🇺 НАУФОР подала заявку на статус СРО",
                                  "Сопоставимого материала найти не удалось.", facts)
        self.assertIn("EDITORIAL_PROCESS_NOTE", issues)


if __name__ == "__main__":
    unittest.main()
