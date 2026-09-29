import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from newsroom import ai
from newsroom.cli import is_eligible_for_auto_publish, publish


class FakeResponse:
    def __init__(self, body):
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False

    def read(self):
        return self.body


class AICredentialAndResponseTests(unittest.TestCase):
    def test_key_file_is_used_when_environment_key_is_empty(self):
        with tempfile.TemporaryDirectory() as directory:
            key_file = Path(directory) / "api-key"
            key_file.write_text("sk-test-file-key\n", encoding="utf-8")
            with patch.dict(os.environ, {"OPENAI_API_KEY": ""}):
                self.assertEqual(ai.get_api_key({"api_key_file": str(key_file)}), "sk-test-file-key")

    def test_environment_key_takes_precedence_over_key_file(self):
        with tempfile.TemporaryDirectory() as directory:
            key_file = Path(directory) / "api-key"
            key_file.write_text("sk-test-file-key", encoding="utf-8")
            with patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test-environment-key"}):
                self.assertEqual(ai.get_api_key({"api_key_file": str(key_file)}), "sk-test-environment-key")

    def _analyze_with_body(self, body):
        with patch("newsroom.ai.get_api_key", return_value="sk-test-key"), \
             patch("newsroom.ai.urllib.request.urlopen", return_value=FakeResponse(body)):
            return ai.analyze(
                {"title": "Test", "description": "Test text", "content": "Test text"},
                {"name": "Test source", "reputation": "unknown", "priority": 1},
                [],
                {"model": "unit-test"},
            )

    def test_malformed_api_response_has_safe_error_code(self):
        with self.assertRaises(ai.AIResponseError) as caught:
            self._analyze_with_body(b"not-json")
        self.assertEqual(caught.exception.code, "INVALID_RESPONSE_JSON")

    def test_malformed_structured_output_has_distinct_safe_error_code(self):
        body = json.dumps({"output": [{"content": [{"type": "output_text", "text": "not-json"}]}]}).encode()
        with self.assertRaises(ai.AIResponseError) as caught:
            self._analyze_with_body(body)
        self.assertEqual(caught.exception.code, "INVALID_STRUCTURED_OUTPUT_JSON")

    def test_token_limited_response_has_actionable_safe_error_code(self):
        body = json.dumps({
            "status": "incomplete",
            "incomplete_details": {"reason": "max_output_tokens"},
            "output": [],
        }).encode()
        with self.assertRaises(ai.AIResponseError) as caught:
            self._analyze_with_body(body)
        self.assertEqual(caught.exception.code, "OUTPUT_TOKEN_LIMIT")


class AutomaticPublicationGuardTests(unittest.TestCase):
    cutoff = "2026-09-25T10:09:12+00:00"

    def post(self, mode="AI", created_at="2026-09-25T10:10:00+00:00", source_status="READ"):
        return {"fact_check_result": json.dumps({"mode": mode, "primary_source_status": source_status, "_filter_version": ai.FILTER_VERSION, "geographic_scope": "RUSSIA", "russia_cis_impact": "DIRECT", "impact_evidence": "Банк России установил требования для российских участников рынка."}),
                "created_at": created_at}

    def test_allows_only_new_ai_posts(self):
        self.assertTrue(is_eligible_for_auto_publish(self.post(), self.cutoff))

    def test_blocks_rule_based_posts(self):
        self.assertFalse(is_eligible_for_auto_publish(self.post(mode="RULE_BASED"), self.cutoff))

    def test_blocks_posts_when_an_explicit_primary_source_is_unreadable(self):
        self.assertFalse(is_eligible_for_auto_publish(
            self.post(source_status="UNREADABLE"), self.cutoff))

    def test_blocks_posts_when_the_publisher_article_itself_was_unreadable(self):
        self.assertFalse(is_eligible_for_auto_publish(
            self.post(source_status="ARTICLE_UNREADABLE"), self.cutoff))

    def test_blocks_posts_before_cutoff(self):
        self.assertFalse(is_eligible_for_auto_publish(
            self.post(created_at="2026-09-25T10:00:00+00:00"), self.cutoff))

    def test_blocks_missing_or_malformed_cutoff_and_metadata(self):
        self.assertFalse(is_eligible_for_auto_publish(self.post(), ""))
        self.assertFalse(is_eligible_for_auto_publish(
            {"fact_check_result": "bad", "created_at": "2026-09-25T10:10:00+00:00"}, self.cutoff))

    def test_automatic_send_boundary_blocks_rule_based_before_telegram(self):
        post = {
            "post_id": 1,
            "fact_check_result": json.dumps({"mode": "RULE_BASED", "primary_source_status": "READ", "primary_source": {"url": "https://www.cbr.ru/test", "content_sha256": "hash"}}),
            "created_at": "2026-09-25T10:10:00+00:00",
            "text": "Заголовок\n\nРусский текст\n\nИсточник: https://www.cbr.ru/test",
            "story_id": 1,
            "post_hash": "hash",
        }

        class Cursor:
            def fetchone(self):
                return post

        class Database:
            def rollback(self): pass
            def commit(self): pass
            def execute(self, *_args):
                return Cursor()

        config = {"newsroom": {"auto_publish_since": self.cutoff}}
        with patch("newsroom.cli.telegram_send") as send, patch("newsroom.decisions.record_publication") as audit:
            with self.assertRaisesRegex(RuntimeError, "условия автопубликации"):
                publish(Database(), config, 1, automatic=True)
            self.assertEqual(audit.call_args.args[2], 'PUBLICATION_BLOCKED')
        send.assert_not_called()


if __name__ == "__main__":
    unittest.main()
