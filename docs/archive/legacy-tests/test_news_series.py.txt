import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock
from unittest.mock import patch
from newsroom.db import connect
from newsroom.delivery import DeliveryRejected, DeliveryUncertain, reconcile_posts, TelegramReceipt
from newsroom.news_series import split, units, deliver_series


class NewsSeriesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.db = connect(str(Path(self.tmp.name) / 'db')); self.addCleanup(self.db.close)
        self.config = {'telegram': {'chat_id': '@test', 'chat_id_env': 'UNUSED_TEST_SERIES'}}
        self.paragraphs = [f'Условие {i}: банк сохраняет подтверждённые права участников. ' * 2 for i in range(12)]
        self.text = '🇷🇺 Банк открыл сервис\n\n' + '\n\n'.join(self.paragraphs) + '\n\nИсточник: [Банк](https://example.org/1)'
        self.db.execute("INSERT INTO stories(story_id,canonical_topic,headline,first_seen_at,last_updated_at) VALUES(1,'a','a','2026','2026')")
        self.db.execute("INSERT INTO posts(post_id,story_id,text,created_at,version,post_hash) VALUES(1,1,?,'2026',1,'hash')", (self.text,))
        self.db.commit()

    def receipt(self, number):
        return TelegramReceipt({'message_id': number, 'text': 'actual response'})

    def test_split_preserves_conditions_and_complete_links(self):
        parts = split(self.text, 400)
        self.assertGreater(len(parts), 1)
        self.assertTrue(all(units(part) <= 400 for part in parts))
        body = ' '.join(part.partition('\n\n')[2].rpartition('\n\n')[0] for part in parts)
        self.assertEqual(' '.join(body.split()), ' '.join(' '.join(self.paragraphs).split()))
        self.assertTrue(all(part.endswith('Источник: [Банк](https://example.org/1)') for part in parts))
        self.assertEqual(split('Коротко'), ['Коротко'])

    def test_partial_failure_resumes_only_unsent_parts_and_confirms_once(self):
        send = Mock(side_effect=[self.receipt(101), DeliveryRejected('definitive failure')])
        with self.assertRaises(DeliveryRejected):
            deliver_series(self.db, self.config, 1, self.text, send, 400)
        self.assertEqual(reconcile_posts(self.db, self.config), 0)
        self.assertEqual(self.db.execute('SELECT status FROM posts').fetchone()[0], 'PENDING')
        count = len(split(self.text, 400))
        self.db.execute("UPDATE publication_attempts SET updated_at='2020-01-01T00:00:00+00:00' WHERE status='FAILED'")
        resumed = Mock(side_effect=[self.receipt(102+i) for i in range(count-1)])
        self.assertEqual(deliver_series(self.db, self.config, 1, self.text, resumed, 400), '101')
        self.assertEqual(resumed.call_count, count-1)
        self.assertEqual(reconcile_posts(self.db, self.config), 1)
        self.assertEqual(reconcile_posts(self.db, self.config), 0)
        self.assertEqual(self.db.execute('SELECT publication_count FROM stories').fetchone()[0], 1)
        self.assertEqual(self.db.execute('SELECT external_id FROM posts').fetchone()[0], '101')
        self.assertEqual(self.db.execute("SELECT count(*) FROM publication_attempts WHERE status='CONFIRMED'").fetchone()[0], count)

    def test_unknown_second_part_is_never_blindly_resent(self):
        send = Mock(side_effect=[self.receipt(101), TimeoutError()])
        with self.assertRaises(DeliveryUncertain):
            deliver_series(self.db, self.config, 1, self.text, send, 400)
        with self.assertRaises(DeliveryUncertain):
            deliver_series(self.db, self.config, 1, self.text, send, 400)
        self.assertEqual(send.call_count, 2)
        self.assertEqual(reconcile_posts(self.db, self.config), 0)

    def test_partial_series_cannot_change_text_or_channel(self):
        send = Mock(side_effect=[self.receipt(101), DeliveryRejected('failed')])
        with self.assertRaises(DeliveryRejected):
            deliver_series(self.db, self.config, 1, self.text, send, 400)
        for target, text in [('@other', self.text), ('@test', self.text.replace('сервис', 'продукт'))]:
            changed = {'telegram': {'chat_id': target, 'chat_id_env': 'UNUSED_TEST_SERIES'}}
            with self.assertRaises(DeliveryRejected):
                deliver_series(self.db, changed, 1, text, send, 400)
        self.assertEqual(send.call_count, 2)

    def test_series_plan_cannot_be_rewritten_after_first_attempt(self):
        send = Mock(side_effect=DeliveryRejected('failed'))
        with self.assertRaises(DeliveryRejected):
            deliver_series(self.db, self.config, 1, self.text, send, 400)
        with self.assertRaisesRegex(Exception, 'immutable'):
            self.db.execute("UPDATE news_series SET parts_json='[]'")

    def test_long_validated_post_uses_series_through_normal_publish(self):
        from test_workflow import WorkflowTests
        from newsroom.core import process_item
        from newsroom.cli import publish
        fixture = WorkflowTests(methodName='runTest')
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        item = fixture.item(77)
        result = fixture.publish_result(item)
        result['summary_ru'] = '\n\n'.join([result['summary_ru']] * 15)
        with patch('newsroom.core.get_api_key', return_value='test'), patch('newsroom.core.analyze_with_ai', return_value=result):
            self.assertEqual(process_item(fixture.db, fixture.source, item, .35, 400, 24,
                                         ai_settings=fixture.config['ai']), 'NEW_STORY')
        fixture.config['newsroom']['max_post_length'] = 400
        fixture.config['newsroom']['auto_publish_since'] = fixture.now
        fixture.config['telegram'] = self.config['telegram']
        send = Mock(side_effect=[self.receipt(200+i) for i in range(50)])
        with patch('newsroom.cli.telegram_send', send):
            publish(fixture.db, fixture.config, 1, automatic=True)
        self.assertGreater(send.call_count, 1)
        self.assertEqual(fixture.db.execute('SELECT status FROM posts').fetchone()[0], 'PUBLISHED')
        self.assertEqual(fixture.db.execute("SELECT count(*) FROM publication_attempts WHERE status='CONFIRMED'").fetchone()[0], send.call_count)
