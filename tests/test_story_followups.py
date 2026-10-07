import json
import unittest
from unittest.mock import patch
import test_recovery_integration as recovery
from newsroom.core import process_item
from newsroom.cli import publish

class StoryFollowupTests(unittest.TestCase):
    def setUp(self):
        from policy_fixtures import final_check
        checker = patch('newsroom.ai.validate_draft', side_effect=final_check)
        checker.start()
        self.addCleanup(checker.stop)

    setUp = recovery.RecoveryIntegrationTests.setUp
    process = recovery.RecoveryIntegrationTests.process

    def create_first(self):
        with patch('newsroom.core.fetch_publisher_article',return_value=self.article), patch('newsroom.core.get_api_key',return_value='test'), patch('newsroom.core.analyze_with_ai',return_value=self.result):
            self.assertEqual(self.process(),'NEW_STORY')
        self.config['newsroom']['auto_publish_since']=self.now
        self.config['telegram']={'chat_id':'@test_channel','chat_id_env':'KOVALSKY_TEST_UNUSED_ENV'}
        with patch('newsroom.cli.telegram_send',return_value='101') as send:
            publish(self.db,self.config,1,automatic=True)
            self.assertNotIn('Ранее:',send.call_args.args[1])
        return self.db.execute('SELECT story_id FROM posts WHERE post_id=1').fetchone()[0]

    def next_item(self,action,story_id,number):
        item=dict(self.item,url=f'https://example.org/update-{number}',content=f'Новые существенные сведения, выпуск {number}')
        article=dict(self.article,**item)
        result=dict(self.result,action=action,story_id=str(story_id),what_is_new=f'Регулятор уточнил дату вступления требований в силу. Дополнение номер {number}.',headline_ru='🇷🇺 Банк России изменил срок вступления правил в силу')
        with patch('newsroom.core.fetch_publisher_article',return_value=article), patch('newsroom.core.get_api_key',return_value='test'), patch('newsroom.core.analyze_with_ai',return_value=result):
            return process_item(self.db,self.source,item,0.35,3500,48,ai_settings={'model':'test'})

    def test_updates_link_relevant_previous_publication_and_duplicates_create_no_post(self):
        story=self.create_first()
        for number in (2,3):
            self.assertEqual(self.next_item('UPDATE',story,number),'UPDATE_CANDIDATE')
            with patch('newsroom.cli.telegram_send',return_value=str(100+number)) as send:
                publish(self.db,self.config,number,automatic=True)
                text=send.call_args.args[1]
                self.assertIn(f'Дополнение номер {number}',text)
                self.assertNotIn('Ранее:', text)
                self.assertNotIn(f'https://t.me/test_channel/{100+number}',text)
                self.assertEqual(text.count('Ранее:'),0)
                self.assertIn('https://www.cbr.ru/crypto',text)
        self.assertEqual(self.next_item('DUPLICATE',story,4),'DUPLICATE')
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM posts').fetchone()[0],3)

    def test_conflict_and_ocr_are_blocked_at_send_boundary(self):
        self.create_first()
        self.next_item('UPDATE',1,2)
        row=self.db.execute('SELECT * FROM posts WHERE post_id=2').fetchone()
        original=json.loads(row['fact_check_result'])
        for field,value in [('independent_check','CONFLICT'),('primary_source_status','OCR_REVIEW')]:
            with self.subTest(field=field):
                facts=dict(original,**{field:value})
                self.db.execute('UPDATE posts SET fact_check_result=? WHERE post_id=2',(json.dumps(facts),));self.db.commit()
                with patch('newsroom.cli.telegram_send') as send:
                    with self.assertRaises(RuntimeError):publish(self.db,self.config,2,automatic=True)
                    send.assert_not_called()
                self.assertEqual(self.db.execute('SELECT status FROM posts WHERE post_id=2').fetchone()[0],'PENDING')
