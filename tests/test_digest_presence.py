import json
from pathlib import Path
import tempfile
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from newsroom.channel_presence import ChannelPresenceUnavailable, inspect_channel_posts
from newsroom.cli import publish_digest
from newsroom.db import connect
from newsroom.delivery import TelegramReceipt, DeliveryUncertain, deliver, confirm
from newsroom.digest_corrections import repair_digest


class ChannelPresenceTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = str(Path(directory.name) / 'news.sqlite3')
        self.db = connect(self.path)
        self.addCleanup(self.db.close)
        self.config = {'newsroom': {}, 'telegram': {'chat_id': '@testchannel', 'chat_id_env': 'PRESENCE_TEST_UNUSED'}}
        self.chat = {'id': -1001, 'username': 'testchannel'}

    def fetch(self, markup):
        with patch('newsroom.cli.telegram_api', return_value=self.chat), \
                patch('newsroom.core._request_with_url', return_value=(markup.encode(), 'https://t.me/testchannel/1?embed=1', 'text/html')) as request:
            result = inspect_channel_posts(self.config, ['1'], db=self.db)
        return result, request

    def test_explicit_missing_post_is_recorded_as_deleted(self):
        result, request = self.fetch('<div class="tgme_widget_message_error">Post not found</div>')
        self.assertEqual(result['1']['status'], 'DELETED')
        row = self.db.execute('SELECT * FROM channel_post_observations').fetchone()
        self.assertEqual(row['text'], 'Post not found')
        self.assertIn('?embed=1', row['source_url'])
        request.assert_called_once()

    def test_missing_marker_is_unknown_and_retried_not_deleted(self):
        with patch('newsroom.cli.telegram_api', return_value=self.chat), \
                patch('newsroom.core._request_with_url', return_value=(b'<html>Unavailable</html>', 'https://t.me/testchannel/1?embed=1', 'text/html')) as request:
            with self.assertRaises(ChannelPresenceUnavailable):
                inspect_channel_posts(self.config, ['1'], db=self.db)
        self.assertEqual(request.call_count, 3)
        self.assertEqual(self.db.execute('SELECT status FROM channel_post_observations').fetchone()[0], 'UNKNOWN')

    def test_live_message_requires_the_exact_requested_id(self):
        result, _ = self.fetch('<div data-post="testchannel/1"><div class="tgme_widget_message_text">ЦБ объявил решение</div></div>')
        self.assertEqual(result['1']['status'], 'PRESENT')
        self.assertEqual(result['1']['text'], 'ЦБ объявил решение')
        with self.assertRaises(ChannelPresenceUnavailable):
            self.fetch('<div data-post="testchannel/2"><div class="tgme_widget_message_text">Другая новость</div></div>')

    def test_private_channel_is_blocked_instead_of_assuming_presence(self):
        with patch('newsroom.cli.telegram_api', return_value={'id': -1001}), \
                patch('newsroom.core._request_with_url') as request:
            with self.assertRaises(ChannelPresenceUnavailable):
                inspect_channel_posts(self.config, ['1'], db=self.db)
            request.assert_not_called()


class DigestRemovalTests(ChannelPresenceTests):
    def setUp(self):
        super().setUp()
        self.db.execute("INSERT INTO stories(story_id,canonical_topic,headline,first_seen_at,last_updated_at) VALUES(1,'t','t','2026-10-05','2026-10-05')")
        for mid in (1, 2):
            text = f'Регулятор объявил решение {mid}'
            self.db.execute("INSERT INTO posts(story_id,text,status,created_at,published_at,external_id,version,post_hash,fact_check_result) "
                            "VALUES(1,?,'PUBLISHED',?,?,?,1,?,?)", (text, '2026-10-05T15:00:00+00:00', '2026-10-05T15:00:00+00:00', str(mid), str(mid), '{"geographic_scope":"RUSSIA"}'))
        self.original = '📣 Дайджест\n\n🏛 Регулятор [объявил](https://t.me/testchannel/1) решение 1\n\n🏛 Регулятор [объявил](https://t.me/testchannel/2) решение 2'
        self.db.execute('INSERT INTO digest_batches VALUES(?,?,?,?,?)', ('@testchannel:digest:2026-10-05', json.dumps([self.original]), 2, '2026-10-05T17:05:00+00:00', '2026-10-05T17:05:00+00:00'))
        self.db.commit()
        deliver(self.db, self.config, 'digest:2026-10-05:0', self.original, lambda c, t: TelegramReceipt({'message_id': 64}))
        confirm(self.db, self.config, 'digest:2026-10-05:0')
        self.db.commit()

    def snapshot(self, config, ids, **kwargs):
        result = {}
        for mid in ids:
            mid = str(mid)
            text = self.original if mid == '64' else f'Регулятор объявил решение {mid}'
            result[mid] = {'status': 'DELETED' if mid == '2' else 'PRESENT', 'text': text,
                           'url': f'https://t.me/testchannel/{mid}'}
        return result

    def test_repair_edits_same_message_and_retains_original_delivery(self):
        with patch('newsroom.digest_corrections.inspect_channel_posts', side_effect=self.snapshot), \
                patch('newsroom.channel_presence.inspect_channel_posts', side_effect=self.snapshot), \
                patch('newsroom.cli.telegram_api', return_value={'message_id': 64}) as api:
            result = repair_digest(self.path, self.config, local_date='2026-10-05')
        self.assertEqual(result['parts'][0]['status'], 'CONFIRMED')
        self.assertEqual(result['parts'][0]['removed'], 1)
        self.assertEqual(api.call_args.args[1], 'editMessageText')
        self.assertEqual(api.call_args.args[2]['message_id'], 64)
        edited = self.db.execute('SELECT * FROM digest_edits').fetchone()
        self.assertNotIn('/2)', edited['edited_text'])
        original = self.db.execute("SELECT text FROM publication_attempts WHERE delivery_key='@testchannel:digest:2026-10-05:0'").fetchone()[0]
        self.assertEqual(original, self.original)

    def test_unknown_edit_is_not_resent(self):
        with patch('newsroom.digest_corrections.inspect_channel_posts', side_effect=self.snapshot), \
                patch('newsroom.channel_presence.inspect_channel_posts', side_effect=self.snapshot), \
                patch('newsroom.cli.telegram_api', side_effect=DeliveryUncertain('timeout')) as api:
            for _ in range(2):
                result = repair_digest(self.path, self.config, local_date='2026-10-05')
                self.assertEqual(result['parts'][0]['status'], 'UNKNOWN')
        api.assert_called_once()

    def test_deleted_digest_is_not_recreated(self):
        with patch('newsroom.digest_corrections.inspect_channel_posts', return_value={'64': {'status': 'DELETED'}}), \
                patch('newsroom.cli.telegram_api') as api:
            result = repair_digest(self.path, self.config, local_date='2026-10-05')
            api.assert_not_called()
        self.assertEqual(result['parts'][0]['status'], 'DELETED')

    def test_new_digest_excludes_deleted_posts_and_uses_current_titles(self):
        config = dict(self.config, telegram=dict(self.config['telegram'], chat_id='@new_test'))
        def sources(settings, ids, **kwargs):
            result = self.snapshot(settings, ids, **kwargs)
            for mid, item in result.items():
                item['url'] = f'https://t.me/new_test/{mid}'
            return result
        with patch('newsroom.channel_presence.inspect_channel_posts', side_effect=sources), \
                patch('newsroom.cli.datetime') as clock, \
                patch('newsroom.cli.telegram_send', return_value='65') as send:
            clock.now.return_value = datetime(2026, 10, 5, 18, tzinfo=timezone.utc)
            clock.combine.side_effect = datetime.combine
            self.assertEqual(publish_digest(self.path, config), (True, 1))
            self.assertNotIn('/2)', send.call_args.args[1])

    def test_receipts_from_another_channel_are_not_reused_in_new_channel(self):
        for post in self.db.execute('SELECT * FROM posts').fetchall():
            self.db.execute("INSERT INTO publication_attempts(delivery_key,channel_id,post_id,text,content_hash,status,telegram_message_id,telegram_response_json,created_at,updated_at) VALUES(?,?,?,?,?,'CONFIRMED',?,?,?,?)",
                            (f"@old_channel:post:{post['post_id']}", '@old_channel', post['post_id'], post['text'], post['post_hash'], post['external_id'], json.dumps({'message_id': int(post['external_id'])}), post['published_at'], post['published_at']))
        self.db.commit()
        config = dict(self.config, telegram=dict(self.config['telegram'], chat_id='@new_channel'))
        with patch('newsroom.channel_presence.inspect_channel_posts') as inspect, patch('newsroom.cli.datetime') as clock, patch('newsroom.cli.telegram_send') as send:
            clock.now.return_value = datetime(2026, 10, 5, 18, tzinfo=timezone.utc)
            clock.combine.side_effect = datetime.combine
            self.assertEqual(publish_digest(self.path, config), (False, 0))
        inspect.assert_not_called()
        send.assert_not_called()

    def test_deletion_after_batch_creation_is_removed_before_send(self):
        config = dict(self.config, telegram=dict(self.config['telegram'], chat_id='@race_test'))
        calls = []
        def sources(settings, ids, **kwargs):
            calls.append(list(ids))
            result = self.snapshot(settings, ids, **kwargs)
            for mid, item in result.items():
                item['url'] = f'https://t.me/race_test/{mid}'
                if len(calls) == 1:
                    item['status'] = 'PRESENT'
            return result
        with patch('newsroom.channel_presence.inspect_channel_posts', side_effect=sources), \
                patch('newsroom.cli.datetime') as clock, \
                patch('newsroom.cli.telegram_send', return_value='65') as send:
            clock.now.return_value = datetime(2026, 10, 5, 18, tzinfo=timezone.utc)
            clock.combine.side_effect = datetime.combine
            self.assertEqual(publish_digest(self.path, config), (True, 1))
            self.assertNotIn('/2)', send.call_args.args[1])
            send.assert_called_once()
        self.assertEqual(self.db.execute('SELECT previous_news_count FROM digest_batch_revisions').fetchone()[0], 2)

    def test_unknown_source_blocks_correction_before_telegram(self):
        with patch('newsroom.digest_corrections.inspect_channel_posts', side_effect=ChannelPresenceUnavailable('offline')), \
                patch('newsroom.cli.telegram_api') as api:
            with self.assertRaises(ChannelPresenceUnavailable):
                repair_digest(self.path, self.config, local_date='2026-10-05')
            api.assert_not_called()
