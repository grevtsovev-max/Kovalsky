import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from newsroom.core import fetch_telegram, run_cycle, TelegramBatch
from newsroom.db import connect
from newsroom.cli import build_health_report


def page(ids):
    return ('<div class="tgme_channel_history">' + ''.join(
        '<div class="tgme_widget_message_wrap"><div data-post="news/%d"><div class="tgme_widget_message_text">Crypto %d</div><time datetime="2026-09-28T10:%02d:00+00:00"></time></div></div>' % (i,i,i)
        for i in ids) + '</div>').encode()


class TelegramRecoveryTests(unittest.TestCase):
    def test_reads_older_pages_until_checkpoint(self):
        with patch('newsroom.core._request', side_effect=[page([10,9]),page([8,7])]) as fetch:
            result=fetch_telegram('https://t.me/news',since='2026-09-28T10:07:00+00:00')
        self.assertEqual(len(result),4)
        self.assertIsNone(result.recovery_before)
        self.assertTrue(fetch.call_args.args[0].endswith('?before=9'))

    def test_cap_saves_cursor_and_next_run_also_reads_newest(self):
        with patch('newsroom.core._request',side_effect=[page([10,9]),page([8,7])]):
            first=fetch_telegram('https://t.me/news',since='2026-09-28T10:01:00+00:00',max_pages=2)
        self.assertEqual(first.recovery_before,7)
        with patch('newsroom.core._request',side_effect=[page([12,11]),page([6,5])]) as fetch:
            second=fetch_telegram('https://t.me/news',since='2026-09-28T10:05:00+00:00',before=7,max_pages=2)
        self.assertEqual(fetch.call_args_list[0].args[0],'https://t.me/s/news')
        self.assertTrue(fetch.call_args_list[1].args[0].endswith('?before=7'))
        self.assertIsNone(second.recovery_before)

    def test_failure_preserves_fetched_items_and_resume_point(self):
        with patch('newsroom.core._request',side_effect=[page([10,9]),TimeoutError('timed out')]):
            result=fetch_telegram('https://t.me/news',since='2026-09-28T10:01:00+00:00')
        self.assertEqual(len(result),2)
        self.assertEqual(result.recovery_before,9)
        self.assertEqual(result.recovery_error,'NETWORK_TIMEOUT')

    def test_access_page_is_not_success(self):
        with patch('newsroom.core._request',return_value=b'<html>Access denied</html>'):
            with self.assertRaisesRegex(ValueError,'TELEGRAM_PREVIEW_UNAVAILABLE'):
                fetch_telegram('https://t.me/news')

    def test_repeated_page_is_not_complete(self):
        with patch('newsroom.core._request',return_value=page([10,9])):
            result=fetch_telegram('https://t.me/news',since='2026-09-28T10:01:00+00:00')
        self.assertEqual(result.recovery_error,'TELEGRAM_PAGINATION_STALLED')
        self.assertEqual(result.recovery_before,9)


class SourceStateTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.path=str(Path(self.tmp.name)/'test.db')
        self.cfg={'newsroom':{'database':self.path},'sources':[{'name':'Test','type':'rss','url':'https://example.org/feed'}]}

    def state(self):
        db=connect(self.path)
        try:return dict(db.execute('SELECT * FROM sources').fetchone())
        finally:db.close()

    def test_failures_stay_active_and_success_clears_failure_state(self):
        with patch('newsroom.core.fetch_rss',side_effect=TimeoutError('timed out')):
            run_cycle(self.cfg);run_cycle(self.cfg)
        row=self.state()
        self.assertEqual(row['active'],1)
        self.assertEqual(row['consecutive_failures'],2)
        self.assertIsNone(row['last_success_at'])
        with patch('newsroom.core.fetch_rss',return_value=[]):
            outcome=run_cycle(self.cfg)
        row=self.state()
        self.assertEqual(outcome['SOURCE_RECOVERED'],1)
        self.assertIsNone(row['last_error'])
        self.assertEqual(row['consecutive_failures'],0)
        self.assertIsNotNone(row['last_success_at'])
        with patch('newsroom.core.fetch_rss',side_effect=TimeoutError('timed out')):run_cycle(self.cfg)
        self.assertEqual(self.state()['last_success_at'],row['last_success_at'])

    def test_health_groups_shared_failures(self):
        with patch('newsroom.core.fetch_rss',side_effect=TimeoutError('timed out')):run_cycle(self.cfg)
        db=connect(self.path)
        try:report=build_health_report(db,self.cfg)
        finally:db.close()
        self.assertIn('NETWORK_TIMEOUT: 1',report)
        self.assertIn('подряд сбоев 1',report)

    def test_partial_recovery_does_not_advance_checkpoint(self):
        self.cfg['sources'][0]['type']='telegram'
        self.cfg['sources'][0]['url']='https://t.me/news'
        first=TelegramBatch([{'url':'https://t.me/news/10','title':'new','published_at':'2026-09-28T10:10:00+00:00'}])
        first.recovery_before=9
        with patch('newsroom.core.fetch_telegram',return_value=first),patch('newsroom.core.process_item',return_value='STALE'):
            run_cycle(self.cfg)
        row=self.state()
        self.assertIsNone(row['last_seen_published_at'])
        self.assertEqual(row['recovery_before'],9)
        second=TelegramBatch([{'url':'https://t.me/news/12','title':'newer','published_at':'2026-09-28T10:12:00+00:00'}])
        with patch('newsroom.core.fetch_telegram',return_value=second),patch('newsroom.core.process_item',return_value='STALE'):
            run_cycle(self.cfg)
        row=self.state()
        self.assertEqual(row['last_seen_published_at'],'2026-09-28T10:10:00+00:00')
        self.assertIsNone(row['recovery_before'])
