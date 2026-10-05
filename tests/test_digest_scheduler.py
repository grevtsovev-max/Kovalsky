import json
from pathlib import Path
import tempfile
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from newsroom.cli import _digest_schedule_loop, _link_digest_action, _publish_due_digests, publish_digest
from newsroom.db import connect
from newsroom.delivery import DeliveryRejected, DeliveryUncertain, deliver
from newsroom.locking import acquire_cycle_lock
from newsroom.quality import digest_issues
from tests.digest_support import present_posts


class IndependentDigestTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = str(Path(directory.name) / 'news.sqlite3')
        self.db = connect(self.path)
        self.addCleanup(self.db.close)
        self.config = {'newsroom': {}, 'telegram': {
            'chat_id': '@digest_test', 'chat_id_env': 'DIGEST_TEST_UNUSED'}}
        self.db.execute("INSERT INTO stories(story_id,canonical_topic,headline,first_seen_at,last_updated_at) "
                        "VALUES(1,'topic','topic','2026-10-01','2026-10-05')")
        self.db.commit()
        self.now = datetime(2026, 10, 5, 18, 0, tzinfo=timezone.utc)
        presence = patch('newsroom.channel_presence.inspect_channel_posts',
                         side_effect=lambda config, ids, **kw: present_posts(self.db, 'digest_test', ids, **kw))
        presence.start()
        self.addCleanup(presence.stop)

    def post(self, number, date, title='Резиденты будут отчитываться о криптовалюте'):
        self.db.execute("INSERT INTO posts(post_id,story_id,text,status,created_at,published_at,external_id,"
                        "version,post_hash,fact_check_result) VALUES(?,1,?,'PUBLISHED',?,?,?,1,?,?)",
                        (number, title + '\n\nТекст опубликованной новости.', date, date, str(number),
                         str(number), json.dumps({'geographic_scope': 'RUSSIA'})))
        self.db.commit()

    def publish(self, **kwargs):
        with patch('newsroom.cli.datetime') as clock, patch('newsroom.cli.telegram_send', return_value='501') as send:
            clock.now.return_value = self.now
            clock.combine.side_effect = datetime.combine
            result = publish_digest(self.path, self.config, **kwargs)
        return result, send

    def test_morphology_links_verbs_outside_the_old_vocabulary(self):
        titles = [
            ('Правила реестра вступили в силу', 'вступили'),
            ('Резиденты будут отчитываться в ФНС', 'отчитываться'),
            ('Компании возобновят торги', 'возобновят'),
            ('Банки переоформили лицензии', 'переоформили'),
            ('Совфед предложил использовать криптовалюту', 'предложил'),
        ]
        for title, verb in titles:
            with self.subTest(title=title):
                linked = _link_digest_action(title, 'https://t.me/digest_test/1')
                self.assertIn('[' + verb + ']', linked)
                self.assertEqual(digest_issues('📣 Дайджест\n\n🏛 ' + linked), [])
        self.assertTrue(digest_issues('📣 Дайджест\n\n🏛 [ЦБ](https://t.me/digest_test/1) объявил решение'))

    def test_digest_dispatches_while_collection_lock_is_held(self):
        self.post(1, '2026-10-05T15:00:00+00:00')
        lock = acquire_cycle_lock(self.path)
        self.assertIsNotNone(lock)
        self.addCleanup(lock.close)
        with patch('newsroom.cli.datetime') as clock, patch('newsroom.cli.telegram_send', return_value='501') as send, \
                patch('newsroom.cli.run_cycle', side_effect=AssertionError('collection must not run')):
            clock.now.return_value = self.now
            clock.combine.side_effect = datetime.combine
            _publish_due_digests(self.config, self.path)
            send.assert_called_once()
        self.assertEqual(self.db.execute("SELECT value FROM app_state WHERE key='digest_last_local_date'").fetchone()[0],
                         '2026-10-05')

    def test_missed_days_do_not_expand_daily_period(self):
        self.db.execute("INSERT INTO app_state VALUES('digest_last_sent_at','2026-09-29T18:00:00+00:00')")
        self.db.commit()
        self.post(1, '2026-10-05T15:00:00+00:00')
        self.post(2, '2026-10-03T15:00:00+00:00')
        result, send = self.publish()
        self.assertEqual(result, (True, 1))
        self.assertNotIn('/2)', send.call_args.args[1])

    def test_scheduler_recovers_config_failure_and_reuses_connection(self):
        self.post(1, '2026-10-05T15:00:00+00:00')
        config = dict(self.config, newsroom={'database': self.path})
        with patch('newsroom.cli.load_config', side_effect=[OSError('temporarily unavailable'), config, config]), \
                patch('newsroom.cli.time.sleep', side_effect=[None, None, SystemExit]) as sleep, \
                patch('newsroom.cli._seconds_until_digest', return_value=5), \
                patch('newsroom.cli.connect', wraps=connect) as connections, \
                patch('newsroom.cli.datetime') as clock, \
                patch('newsroom.cli.telegram_send', return_value='501') as send:
            clock.now.return_value = self.now
            clock.combine.side_effect = datetime.combine
            with self.assertRaises(SystemExit):
                _digest_schedule_loop('config.toml')
            connections.assert_called_once_with(self.path)
            send.assert_called_once()
            self.assertEqual([call.args[0] for call in sleep.call_args_list], [30.0, 5, 5])
        self.assertIsNotNone(self.db.execute("SELECT value FROM app_state WHERE key='digest_scheduler_last_tick_at'").fetchone())

    def unsent_batch(self, unknown=False):
        text = '📣 Дайджест\n\n🏛 Регулятор [объявил](https://t.me/digest_test/2) решение'
        self.db.execute('INSERT INTO digest_batches VALUES(?,?,?,?,?)',
                        ('@digest_test:digest:2026-10-05', json.dumps([text]), 1, self.now.isoformat(), self.now.isoformat()))
        self.db.commit()
        def failed_send(config, body):
            raise TimeoutError() if unknown else DeliveryRejected('credentials missing before request')
        with self.assertRaises(DeliveryUncertain if unknown else DeliveryRejected):
            deliver(self.db, self.config, 'digest:2026-10-05:0', text, failed_send)
        return text

    def test_rebuild_known_unsent_batch_keeps_old_text_and_attempt_count(self):
        original = self.unsent_batch()
        self.post(1, '2026-10-05T15:00:00+00:00')
        result, send = self.publish(rebuild_unsent=True)
        self.assertEqual(result, (True, 1))
        self.assertIn('/1)', send.call_args.args[1])
        revision = self.db.execute('SELECT * FROM digest_batch_revisions').fetchone()
        self.assertEqual(json.loads(revision['previous_messages_json']), [original])
        attempt = self.db.execute('SELECT * FROM publication_attempts').fetchone()
        self.assertEqual(attempt['status'], 'CONFIRMED')
        self.assertEqual(attempt['attempt_count'], 2)

    def test_rebuild_unknown_batch_is_blocked_without_changing_it(self):
        original = self.unsent_batch(unknown=True)
        self.post(1, '2026-10-05T15:00:00+00:00')
        with patch('newsroom.cli.datetime') as clock, patch('newsroom.cli.telegram_send') as send:
            clock.now.return_value = self.now
            clock.combine.side_effect = datetime.combine
            with self.assertRaises(DeliveryUncertain):
                publish_digest(self.path, self.config, rebuild_unsent=True)
            send.assert_not_called()
        self.assertEqual(json.loads(self.db.execute('SELECT messages_json FROM digest_batches').fetchone()[0]), [original])
        self.assertEqual(self.db.execute('SELECT count(*) FROM digest_batch_revisions').fetchone()[0], 0)
