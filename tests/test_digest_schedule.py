from newsroom.delivery import DeliveryRejected
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch
from newsroom.db import connect
from newsroom.cli import publish_digest, _link_digest_action
from tests.digest_support import present_posts

class DigestScheduleTests(unittest.TestCase):
    def test_digest_links_proposed_and_prepared_action_verbs(self):
        self.assertEqual(
            _link_digest_action('Совфед предложил использовать криптовалюту в качестве залога', 'https://t.me/test_channel/21'),
            'Совфед [предложил](https://t.me/test_channel/21) использовать криптовалюту в качестве залога',
        )
        self.assertEqual(
            _link_digest_action('АСРОС: ЦБ подготовил поправки по кредитным ЦФА', 'https://t.me/test_channel/22'),
            'АСРОС: ЦБ [подготовил](https://t.me/test_channel/22) поправки по кредитным ЦФА',
        )

    def test_digest_links_registration_action_verb(self):
        self.assertEqual(
            _link_digest_action('В Беларуси зарегистрировали первые два криптобанка', 'https://t.me/test_channel/24'),
            'В Беларуси [зарегистрировали](https://t.me/test_channel/24) первые два криптобанка',
        )
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.path=str(Path(self.tmp.name)/'test.db')
        self.db=connect(self.path);self.addCleanup(self.db.close)
        presence=patch('newsroom.channel_presence.inspect_channel_posts',side_effect=lambda config,ids,**kw:present_posts(self.db,'test_channel',ids,**kw))
        presence.start();self.addCleanup(presence.stop)
        self.db.execute("INSERT INTO stories(story_id,canonical_topic,headline,first_seen_at,last_updated_at) VALUES(1,'topic','title','2026-09-25','2026-09-25')")
        self.config={'newsroom':{'digest_time':'19:30','digest_timezone':'Europe/Moscow'},'telegram':{'chat_id':'@test_channel','chat_id_env':'KOVALSKY_TEST_UNUSED_ENV'}}
    def post(self,i,importance='MEDIUM',geo='RUSSIA',test=False,status='PUBLISHED',date='2026-09-26T16:00:00+00:00'):
        facts={'importance':importance,'geographic_scope':geo,'test_publication':test}
        self.db.execute('INSERT INTO posts(post_id,story_id,text,status,created_at,published_at,external_id,version,post_hash,fact_check_result) VALUES(?,1,?,?,?,?,?,1,?,?)',(i,f'Регулятор объявил решение номер {i}\n\nПодробности новых правил для участников рынка.',status,date,date,str(i),str(i),json.dumps(facts)))
        self.db.commit()
    def run_at(self,hour,minute=0,fail=False):
        with patch('newsroom.cli.datetime') as clock,patch('newsroom.cli.telegram_send',return_value='501', side_effect=DeliveryRejected('test') if fail else None) as send:
            clock.now.return_value=datetime(2026,9,26,hour,minute,tzinfo=timezone.utc)
            clock.combine.side_effect=datetime.combine
            if fail:
                with self.assertRaises(RuntimeError):publish_digest(self.path,self.config)
                return None,send
            return publish_digest(self.path,self.config),send
    def test_schedule_selection_order_links_and_fixed_window(self):
        self.db.execute("INSERT INTO app_state VALUES('digest_last_sent_at','2026-09-26T12:00:00+00:00')");self.db.commit()
        self.post(1);self.post(2,importance='HIGH');self.post(3,geo='GLOBAL');self.post(4,test=True);self.post(5,status='PENDING');self.post(6,date='2026-09-25T16:00:00+00:00')
        result,send=self.run_at(17,4);self.assertEqual(result,(False,0));send.assert_not_called()
        result,send=self.run_at(17,5);self.assertEqual(result,(True,3))
        text=send.call_args.args[1]
        self.assertIn('https://t.me/test_channel/1',text);self.assertIn('https://t.me/test_channel/2',text)
        self.assertLess(text.index('/2)'),text.index('/1)'))
        self.assertNotIn('Подробности новых правил', text)
        for i in (4,5,6):self.assertNotIn(f'https://t.me/test_channel/{i}',text)
        result,send=self.run_at(18);self.assertEqual(result,(False,0));send.assert_not_called()
    def test_published_russian_post_is_not_refiltered_by_foreign_word(self):
        self.post(1)
        self.db.execute("UPDATE posts SET text='🇷🇺 Банк пояснил влияние санкций США\n\nПодтверждённая публикация.' WHERE post_id=1")
        self.db.commit()
        result, send = self.run_at(18)
        self.assertEqual(result, (True, 1))
        self.assertIn('США', send.call_args.args[1])
        self.assertIn('https://t.me/test_channel/1', send.call_args.args[1])

    def test_unknown_delivery_cannot_enter_digest_despite_published_label(self):
        self.post(1)
        self.db.execute("INSERT INTO publication_attempts(delivery_key,channel_id,post_id,text,content_hash,status,attempt_count,created_at,updated_at) VALUES('test:post:1','@test_channel',1,'text','hash','UNKNOWN',1,'2026','2026')")
        self.db.commit()
        result, send = self.run_at(18)
        self.assertEqual(result, (False, 0))
        send.assert_not_called()
        self.assertEqual(self.db.execute("SELECT status FROM publication_attempts").fetchone()[0], 'UNKNOWN')

    def test_empty_digest_does_not_block_a_later_publication(self):
        self.db.commit()
        result, send = self.run_at(18)
        self.assertEqual(result, (False, 0))
        send.assert_not_called()
        self.post(1)
        result, send = self.run_at(18)
        self.assertEqual(result, (True, 1))
        send.assert_called_once()

    def test_first_digest_uses_last_24_hours(self):
        self.post(1);self.post(2,date='2026-09-25T16:00:00+00:00')
        result,send=self.run_at(18);self.assertEqual(result,(True,1));self.assertNotIn('/2)',send.call_args.args[1])
    def test_failed_send_does_not_mark_day_complete(self):
        self.post(1);self.run_at(18,fail=True)
        self.assertIsNone(self.db.execute("SELECT value FROM app_state WHERE key='digest_last_local_date'").fetchone())
        self.db.execute("UPDATE publication_attempts SET updated_at='2020-01-01T00:00:00+00:00' WHERE status='FAILED'")
        self.db.commit()
        result,send=self.run_at(18);self.assertEqual(result,(True,1));send.assert_called_once()

    def test_unknown_delivery_blocks_next_cycle_and_freezes_digest(self):
        from newsroom.delivery import DeliveryUncertain
        self.post(1)
        with patch('newsroom.cli.datetime') as clock, patch('newsroom.cli.telegram_send', side_effect=TimeoutError()) as send:
            clock.now.return_value=datetime(2026,9,26,18,tzinfo=timezone.utc)
            clock.combine.side_effect=datetime.combine
            with self.assertRaises(DeliveryUncertain): publish_digest(self.path,self.config)
            self.post(2)
            with self.assertRaises(DeliveryUncertain): publish_digest(self.path,self.config)
            send.assert_called_once()
        batch=self.db.execute('SELECT * FROM digest_batches').fetchone()
        self.assertEqual(batch['news_count'],1)
        self.assertNotIn('/2)',batch['messages_json'])

    def test_saved_receipt_finishes_digest_without_second_send(self):
        self.post(1)
        with patch('newsroom.cli.datetime') as clock, patch('newsroom.cli.telegram_send', return_value='501') as send:
            clock.now.return_value=datetime(2026,9,26,18,tzinfo=timezone.utc)
            clock.combine.side_effect=datetime.combine
            with patch('newsroom.cli.confirm', side_effect=SystemExit()):
                with self.assertRaises(SystemExit): publish_digest(self.path,self.config)
            self.assertEqual(publish_digest(self.path,self.config),(True,1))
            send.assert_called_once()
