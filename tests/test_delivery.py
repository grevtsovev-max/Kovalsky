import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from newsroom.db import connect
from newsroom.delivery import (deliver, confirm, reconcile_posts, DeliveryRejected,
                               DeliveryUncertain, TelegramReceipt, observe_channel_post)


class DeliveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = str(Path(self.tmp.name) / 'test.db')
        self.db = connect(self.path)
        self.addCleanup(self.db.close)
        self.config = {'telegram': {'chat_id': '@test', 'chat_id_env': 'TEST_UNUSED'}}
        self.send = Mock(return_value=TelegramReceipt({'message_id': 42, 'text': 'body'}))

    def deliver(self, text='body', send=None):
        return deliver(self.db, self.config, 'post:1', text, send or self.send)

    def test_receipt_persisted_and_text_change_cannot_resend(self):
        self.assertEqual(self.deliver(), '42')
        self.assertEqual(self.deliver('edited body'), '42')
        self.send.assert_called_once()
        row = self.db.execute('SELECT * FROM publication_attempts').fetchone()
        self.assertEqual(json.loads(row['telegram_response_json'])['message_id'], 42)
        confirm(self.db, self.config, 'post:1')
        self.db.commit()
        self.assertEqual(self.deliver(), '42')
        self.send.assert_called_once()

    def test_network_timeout_is_never_retried_even_with_edited_text(self):
        self.send.side_effect = TimeoutError()
        with self.assertRaises(DeliveryUncertain): self.deliver()
        with self.assertRaises(DeliveryUncertain): self.deliver('edited')
        self.send.assert_called_once()
        self.assertEqual(self.db.execute('SELECT status FROM publication_attempts').fetchone()[0], 'UNKNOWN')

    def test_process_death_after_remote_success_leaves_sending(self):
        self.send.side_effect = SystemExit()
        with self.assertRaises(SystemExit): self.deliver()
        other = connect(self.path)
        try:
            with self.assertRaises(DeliveryUncertain):
                deliver(other, self.config, 'post:1', 'body', self.send)
        finally: other.close()
        self.send.assert_called_once()

    def test_definitive_rejection_retries_at_most_three_times(self):
        self.send.side_effect = DeliveryRejected('rejected')
        from datetime import datetime, timedelta, timezone
        current = datetime.now(timezone.utc)
        for index in range(5):
            with patch('newsroom.delivery.now', return_value=(current + timedelta(seconds=index*301)).isoformat()):
                with self.assertRaises(DeliveryRejected): self.deliver()
        self.assertEqual(self.send.call_count, 4)

    def test_definitive_failure_waits_without_consuming_attempt(self):
        from newsroom.runtime import BudgetDeferred
        self.send.side_effect = DeliveryRejected('rejected')
        with self.assertRaises(DeliveryRejected):
            self.deliver()
        with self.assertRaises(BudgetDeferred) as waiting:
            self.deliver()
        self.assertGreater(waiting.exception.delay_seconds, 0)
        self.assertLessEqual(waiting.exception.delay_seconds, 30)
        self.send.assert_called_once()
        self.assertEqual(self.db.execute('SELECT attempt_count FROM publication_attempts').fetchone()[0], 1)

    def test_second_connection_cannot_send_while_first_is_in_flight(self):
        def sending(config, text):
            other = connect(self.path)
            try:
                with self.assertRaises(DeliveryUncertain):
                    deliver(other, config, 'post:1', text, self.send)
            finally: other.close()
            return '42'
        self.deliver(send=sending)
        self.send.assert_not_called()

    def test_replay_saved_receipt_updates_post_and_story_once(self):
        self.db.execute("INSERT INTO stories(story_id,canonical_topic,headline,first_seen_at,last_updated_at) VALUES(1,'a','a','2026','2026')")
        self.db.execute("INSERT INTO posts(post_id,story_id,text,created_at,version,post_hash) VALUES(1,1,'body','2026',1,'hash')")
        deliver(self.db, self.config, 'post:1', 'body', self.send, post_id=1)
        self.assertEqual(reconcile_posts(self.db, self.config), 1)
        self.assertEqual(reconcile_posts(self.db, self.config), 0)
        self.assertEqual(self.db.execute('SELECT external_id FROM posts').fetchone()[0], '42')
        self.assertEqual(self.db.execute('SELECT publication_count FROM stories').fetchone()[0], 1)
        self.send.assert_called_once()

    def test_reservation_rechecks_actual_saved_text_before_transport(self):
        self.db.execute("INSERT INTO stories(story_id,canonical_topic,headline,first_seen_at,last_updated_at) VALUES(1,'a','a','2026','2026')")
        self.db.execute("INSERT INTO posts(post_id,story_id,text,created_at,version,post_hash) VALUES(1,1,'new unverified body','2026',1,'hash')")
        with self.assertRaisesRegex(DeliveryRejected, 'changed before reservation'):
            deliver(self.db, self.config, 'post:1', 'previous checked body', self.send, post_id=1)
        self.send.assert_not_called()
        self.assertEqual(self.db.execute('SELECT count(*) FROM publication_attempts').fetchone()[0], 0)

    def test_only_matching_actual_channel_update_resolves_unknown(self):
        self.send.side_effect = TimeoutError()
        with self.assertRaises(DeliveryUncertain): self.deliver()
        message = {'message_id': 42, 'date': 2000000000, 'chat': {'id': -1001, 'username': 'other'}, 'text': 'body'}
        self.assertFalse(observe_channel_post(self.db, message, 'body'))
        message['chat']['username'] = 'test'
        self.assertFalse(observe_channel_post(self.db, message, 'wrong body'))
        self.assertTrue(observe_channel_post(self.db, message, 'body'))
        self.db.commit()
        self.assertEqual(self.deliver(), '42')
        self.send.assert_called_once()

    def test_delivery_history_cannot_be_rewritten(self):
        self.deliver()
        with self.assertRaisesRegex(Exception, 'append only'):
            self.db.execute("DELETE FROM delivery_events")

    def test_observed_receipt_wins_race_against_transport_timeout(self):
        def sending(config, text):
            other = connect(self.path)
            try:
                self.assertTrue(observe_channel_post(other, {'message_id':42,'date':2000000000,'chat':{'username':'test'},'text':'body'}, 'body'))
                other.commit()
            finally: other.close()
            raise TimeoutError()
        self.assertEqual(self.deliver(send=sending), '42')
        self.assertEqual(self.db.execute('SELECT status FROM publication_attempts').fetchone()[0], 'SENT')

    def test_malformed_api_reply_is_not_a_definitive_rejection(self):
        from newsroom.cli import telegram_api
        response=Mock()
        response.__enter__=Mock(return_value=response)
        response.__exit__=Mock(return_value=False)
        for data in ({}, [], {'ok':False,'error_code':500}):
            response.read.return_value=json.dumps(data).encode()
            with patch('newsroom.cli.telegram_token',return_value='test'),patch('newsroom.cli.urllib.request.urlopen',return_value=response):
                with self.assertRaises(DeliveryUncertain):
                    telegram_api({},'sendMessage',{})
