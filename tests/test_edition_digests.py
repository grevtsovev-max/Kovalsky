from datetime import datetime,timedelta,timezone
from unittest.mock import Mock
from edition_helpers import EditionCase
from newsroom.edition import digests
from newsroom.agent_control import set_enabled,AgentDisabled
from newsroom.delivery import TelegramReceipt

NOW=datetime(2026,10,12,17,5,tzinfo=timezone.utc)


class DigestTests(EditionCase):
    def setUp(self):
        super().setUp()
        self.config['digests']={'enabled':True}
        self.db.executescript(digests.SCHEMA)
        digests.state(self.db,'digest_v2_activation',(NOW-timedelta(days=1)).isoformat());self.db.commit()

    def post(self,headline='Новость опубликована',timestamp=None,target=None):
        stamp=(timestamp or NOW-timedelta(hours=1)).isoformat()
        story=self.db.execute("INSERT INTO stories(canonical_topic,headline,first_seen_at,last_updated_at) VALUES('topic',?,?,?)",(headline,stamp,stamp)).lastrowid
        pid=self.db.execute("INSERT INTO posts(story_id,text,status,created_at,published_at,external_id,version,post_hash) VALUES(?,?,'PUBLISHED',?,?,?,1,?)",(story,'<b>'+headline+'</b>',stamp,stamp,str(story),str(story))).lastrowid
        self.db.execute("INSERT INTO publication_attempts(delivery_key,channel_id,post_id,text,content_hash,status,telegram_message_id,created_at,updated_at) VALUES(?,?,?,?,?,'CONFIRMED',?,?,?)",('post:'+str(pid),target or '@test',pid,headline,str(pid),str(story),stamp,stamp));self.db.commit()
        return pid

    def sender(self):return Mock(return_value=TelegramReceipt({'message_id':1000}))

    def test_daily_only_published_target_posts_and_one_delivery_after_restart(self):
        self.post();self.post('Старый пост',NOW-timedelta(days=2));self.post('Другой канал',target='@other')
        sender=self.sender();digests.tick(self.db,self.config,now=NOW,send=sender)
        digests.tick(self.db,self.config,now=NOW+timedelta(minutes=1),send=sender)
        sender.assert_called_once();text=sender.call_args.args[1]
        self.assertIn('Новость опубликована',text);self.assertNotIn('Старый пост',text);self.assertNotIn('Другой канал',text)
        self.assertIn('https://t.me/test/',text)
        self.assertEqual(self.db.execute('SELECT state FROM edition_digest_runs').fetchone()[0],'SENT')

    def test_unknown_delivery_never_resends(self):
        self.post();sender=Mock(side_effect=TimeoutError())
        digests.tick(self.db,self.config,now=NOW,send=sender)
        digests.tick(self.db,self.config,now=NOW+timedelta(minutes=2),send=sender)
        sender.assert_called_once();self.assertEqual(self.db.execute('SELECT state FROM edition_digest_runs').fetchone()[0],'UNKNOWN')

    def test_activation_does_not_backfill_and_empty_period_is_silent(self):
        self.db.execute("DELETE FROM app_state WHERE key='digest_v2_activation'");self.db.commit()
        sender=self.sender();digests.tick(self.db,self.config,now=NOW+timedelta(minutes=2),send=sender)
        self.assertEqual(self.db.execute('SELECT count(*) FROM edition_digest_runs').fetchone()[0],0)
        digests.tick(self.db,self.config,now=NOW+timedelta(days=1),send=sender)
        sender.assert_not_called();self.assertEqual(self.db.execute('SELECT state FROM edition_digest_runs').fetchone()[0],'EMPTY')

    def test_owner_stop_prevents_even_preparation(self):
        set_enabled(self.config,False)
        with self.assertRaises(AgentDisabled):digests.tick(self.db,self.config,now=NOW,send=self.sender())
        self.assertEqual(self.db.execute('SELECT count(*) FROM edition_digest_runs').fetchone()[0],0)

    def test_weekly_schedule_and_long_batches(self):
        weekly=datetime(2026,10,17,16,tzinfo=timezone.utc)
        self.assertEqual(digests.slots(self.config,weekly)[1][2].isoformat(),'2026-10-17T19:00:00+03:00')
        posts=[{'headline':'Новость '+str(i)+' '+('текст '*20),'url':'https://t.me/test/'+str(i)} for i in range(80)]
        messages=digests.compose('weekly',weekly-timedelta(days=7),weekly,posts)
        self.assertGreater(len(messages),1)
        self.assertTrue(all(len(m)<3600 for m in messages))
        self.assertEqual(sum(m.count('<a href=') for m in messages),80)

    def test_failed_part_can_resume_on_next_day_without_resending_confirmed_part(self):
        self.post();sender=Mock(side_effect=[TelegramReceipt({'message_id':1000}),TimeoutError()])
        digests.tick(self.db,self.config,now=NOW,send=sender)
        # Replaying a persisted pending batch reuses confirmed receipt even across dates.
        self.db.execute("UPDATE edition_digest_runs SET state='PENDING'");self.db.commit()
        digests.tick(self.db,self.config,now=NOW+timedelta(days=1),send=sender)
        sender.assert_called_once()

    def test_partial_unknown_batch_keeps_successful_part_and_blocks_rest(self):
        for i in range(40):self.post('Новость '+str(i)+' '+('текст '*20))
        sender=Mock(side_effect=[TelegramReceipt({'message_id':1000}),TimeoutError()])
        digests.tick(self.db,self.config,now=NOW,send=sender)
        digests.tick(self.db,self.config,now=NOW+timedelta(minutes=1),send=sender)
        self.assertEqual(sender.call_count,2)
        self.assertEqual([r[0] for r in self.db.execute('SELECT status FROM publication_attempts WHERE post_id IS NULL ORDER BY attempt_id')],['CONFIRMED','UNKNOWN'])

    def test_same_title_with_updated_facts_is_not_discarded(self):
        self.post('Одинаковый заголовок');second=self.post('Одинаковый заголовок')
        self.db.execute('UPDATE posts SET text=text||? WHERE post_id=?',(' Новые факты.',second));self.db.commit()
        selected=digests.published(self.db,self.config,NOW-timedelta(days=1),NOW)
        self.assertEqual(len(selected),2)
