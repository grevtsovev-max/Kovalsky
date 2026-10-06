import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from newsroom.db import connect
from newsroom.cli import load_config, publish
from newsroom.review import REVIEW_ALLOWED_UPDATES, handle_update


class AutomaticReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = connect(str(Path(self.temp.name) / "newsroom.sqlite3"))
        self.db.execute(
            "INSERT INTO stories(canonical_topic,headline,first_seen_at,last_updated_at) VALUES(?,?,?,?)",
            ("тест", "Тест", "2026-09-25T12:00:00+00:00", "2026-09-25T12:00:00+00:00"),
        )
        self.db.execute(
            "INSERT INTO posts(story_id,text,status,created_at,version,source_ids,post_hash,fact_check_result) VALUES(1,?,'PENDING',?,1,'[]','hash',?)",
            ("Тестовый пост", "2026-09-25T12:01:00+00:00", json.dumps({"mode": "RULE_BASED"})),
        )
        self.db.commit()
        # A stale setting must not restore the retired human approval flow.
        self.config = {"telegram": {"review_chat_id": "-1004316291893", "chat_id": "-1004316291893", "interest_owner_user_ids": [42]}, "newsroom": {}}

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def test_agent_fact_correction_uses_actual_post_schema_and_is_idempotent(self):
        from newsroom.review import enqueue_agent_fact_correction
        self.db.execute("UPDATE posts SET status='PUBLISHED' WHERE post_id=1")
        self.db.execute("INSERT INTO sources(name,type,url) VALUES('Источник','rss','https://example.org/feed')")
        self.db.execute("INSERT INTO items(source_id,url,canonical_url,title,discovered_at,content_hash,title_hash) VALUES(1,'https://example.org/a','https://example.org/a','Дополнение','2026-10-06','a','a')")
        for supplement in (False, True):
            kwargs = dict(item_id=1, story_id=1, post_id=1, owner_chat_id='42', supplement=supplement)
            first = enqueue_agent_fact_correction(self.db, **kwargs)
            self.assertIsNotNone(first)
            self.assertEqual(first, enqueue_agent_fact_correction(self.db, **kwargs))
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM telegram_feedback_corrections').fetchone()[0], 2)
        self.assertEqual(self.db.execute('SELECT text FROM posts WHERE post_id=1').fetchone()[0], 'Тестовый пост')

    def test_old_approval_buttons_are_acknowledged_but_do_not_change_post(self):
        update = {"callback_query": {
            "id": "callback-1", "data": "approve:1",
            "from": {"id": 42},
            "message": {"message_id": 55, "chat": {"id": -1004316291893}},
        }}
        with patch("newsroom.review.telegram_api") as api, patch("newsroom.cli.publish") as publish:
            handle_update(self.config, self.db, update)
        self.assertEqual(self.db.execute("SELECT status FROM posts WHERE post_id=1").fetchone()[0], "PENDING")
        publish.assert_not_called()
        api.assert_called_once()
        self.assertEqual(api.call_args.args[1], "answerCallbackQuery")

    def test_old_queue_command_does_not_send_manual_review_response(self):
        update = {"message": {
            "chat": {"id": -1004316291893, "type": "group"},
            "from": {"id": 42}, "text": "/queue",
        }}
        with patch("newsroom.review.telegram_api") as api:
            handle_update(self.config, self.db, update)
        api.assert_not_called()
        self.assertEqual(self.db.execute("SELECT status FROM posts WHERE post_id=1").fetchone()[0], "PENDING")

    def _published_edit_update(self, update_id=501, chat_id=-1004316291893, message_id=987, text="Исправленный пост"):
        self.db.execute("UPDATE posts SET status='PUBLISHED',external_id=? WHERE post_id=1", (str(message_id),))
        self.db.commit()
        return {"update_id": update_id, "edited_channel_post": {
            "message_id": message_id, "chat": {"id": chat_id, "type": "channel"},
            "edit_date": 1790630400, "text": text,
        }}

    def test_telegram_edit_is_saved_and_owner_receives_learning_summary(self):
        update = self._published_edit_update(text="Исправленный пост с уточнением")
        with patch("newsroom.interests.summarize_editorial_edit", return_value=["Добавлять подтверждённое последствие."]), \
             patch("newsroom.review.telegram_api", return_value={"message_id": 900}) as api:
            handle_update(self.config, self.db, update)
        feedback = self.db.execute("SELECT * FROM editorial_feedback WHERE feedback_type='TELEGRAM_EDIT'").fetchone()
        ack = self.db.execute("SELECT * FROM telegram_edit_acknowledgements WHERE update_id=501").fetchone()
        self.assertIsNotNone(feedback)
        self.assertEqual(feedback["post_id"], 1)
        self.assertEqual(feedback["post_text"], "Тестовый пост")
        self.assertIn("-Тестовый пост", feedback["reason"])
        self.assertIn("+Исправленный пост с уточнением", feedback["reason"])
        self.assertEqual(ack["telegram_message_id"], 900)
        self.assertIn("Добавлять подтверждённое последствие", api.call_args.args[2]["text"])
        self.assertIn("ответьте на это сообщение", api.call_args.args[2]["text"])

    def test_reply_to_learning_ack_can_refine_bot_interpretation(self):
        edit = self._published_edit_update(text="Исправленный пост с уточнением")
        with patch("newsroom.interests.summarize_editorial_edit", return_value=["Добавлять подтверждённое последствие."]), \
             patch("newsroom.review.telegram_api", side_effect=[{"message_id": 900}, {"message_id": 901}]) as api:
            handle_update(self.config, self.db, edit)
            reply = {"update_id": 502, "message": {
                "message_id": 901, "chat": {"id": 42, "type": "private"}, "from": {"id": 42},
                "text": "Не только последствия, сначала объяснять механизм изменения.",
                "reply_to_message": {"message_id": 900},
            }}
            handle_update(self.config, self.db, reply)
            handle_update(self.config, self.db, reply)
        feedback = self.db.execute("SELECT * FROM editorial_feedback WHERE feedback_type='TELEGRAM_EDIT_REFINEMENT'").fetchone()
        ack = self.db.execute("SELECT status FROM telegram_edit_acknowledgements WHERE update_id=501").fetchone()
        self.assertEqual(ack["status"], "REFINED")
        self.assertIn("сначала объяснять механизм", feedback["reason"])
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM telegram_edit_replies").fetchone()[0], 1)
        self.assertEqual(api.call_count, 2)

    def test_owner_can_confirm_learning_summary(self):
        edit = self._published_edit_update(text="Исправленный пост с уточнением")
        with patch("newsroom.interests.summarize_editorial_edit", return_value=["Уточнять стадию решения."]), \
             patch("newsroom.review.telegram_api", side_effect=[{"message_id": 900}, {"message_id": 901}]):
            handle_update(self.config, self.db, edit)
            handle_update(self.config, self.db, {"update_id": 502, "message": {
                "message_id": 901, "chat": {"id": 42, "type": "private"}, "from": {"id": 42},
                "text": "Верно", "reply_to_message": {"message_id": 900},
            }})
        self.assertEqual(self.db.execute("SELECT status FROM telegram_edit_acknowledgements WHERE update_id=501").fetchone()[0], "CONFIRMED")
        self.assertIsNotNone(self.db.execute("SELECT 1 FROM editorial_feedback WHERE feedback_type='TELEGRAM_EDIT_CONFIRMATION'").fetchone())

    def test_telegram_edit_updates_are_idempotent_and_scoped_to_destination(self):
        update = self._published_edit_update()
        handle_update(self.config, self.db, update)
        handle_update(self.config, self.db, update)
        other_channel = self._published_edit_update(update_id=502, chat_id=-100999, text="Другая версия")
        handle_update(self.config, self.db, other_channel)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM editorial_feedback WHERE feedback_type='TELEGRAM_EDIT'").fetchone()[0], 1)
        self.assertIn("edited_channel_post", REVIEW_ALLOWED_UPDATES)

    def test_formatting_only_telegram_update_is_ignored(self):
        update = self._published_edit_update(text="Тестовый пост")
        handle_update(self.config, self.db, update)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM editorial_feedback WHERE feedback_type='TELEGRAM_EDIT'").fetchone()[0], 0)

    def test_auto_publish_false_is_preserved(self):
        path = Path(self.temp.name) / "config.toml"
        path.write_text('[newsroom]\nauto_publish = false\n', encoding="utf-8")
        self.assertFalse(load_config(str(path))["newsroom"]["auto_publish"])

    def test_direct_manual_publish_call_is_blocked_before_database_access(self):
        with self.assertRaisesRegex(RuntimeError, "Ручная публикация отключена"):
            publish(None, {}, 1)


if __name__ == "__main__":
    unittest.main()
