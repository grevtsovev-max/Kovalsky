# -*- coding: utf-8 -*-
import json
import unittest
from unittest.mock import patch
import test_review as review_tests
from newsroom.review import capture_channel_edit, handle_update, flush_edit_acknowledgements
from newsroom.edit_sync import ChannelTextParser, sync_recent_channel_edits, handle_persisted_update
from newsroom.delivery import DeliveryRejected, DeliveryUncertain


class EditRecoveryTests(unittest.TestCase):
    setUp = review_tests.AutomaticReviewTests.setUp
    tearDown = review_tests.AutomaticReviewTests.tearDown
    _published_edit_update = review_tests.AutomaticReviewTests._published_edit_update

    def capture(self, text='Новый текст'):
        return capture_channel_edit(self.config,self.db,chat_id=-1004316291893,message_id=987,
                                    edited_text=text,source_url='https://t.me/testchannel/987')

    def test_snapshot_has_honest_provenance_and_no_duplicate_notice(self):
        update = self._published_edit_update(text='Новый текст')
        with patch('newsroom.interests.summarize_editorial_edit',return_value=['Писать короче.']), \
             patch('newsroom.review.telegram_api',return_value={'message_id':900}) as api:
            key = self.capture()
            self.assertLess(key,0)
            self.assertIsNone(self.capture())
            handle_update(self.config,self.db,update)
        self.assertEqual(api.call_count,1)
        row=self.db.execute('SELECT * FROM telegram_post_edits').fetchone()
        self.assertEqual(row['capture_source'],'PUBLIC_CHANNEL_SNAPSHOT')
        self.assertEqual(row['edit_date'],'')
        self.assertEqual(self.db.execute('SELECT text FROM posts').fetchone()[0],'Тестовый пост')

    def test_failed_ack_keeps_learning_and_retries_confirmed_failure(self):
        self._published_edit_update()
        with patch('newsroom.interests.summarize_editorial_edit',return_value=['Писать короче.']), \
             patch('newsroom.review.telegram_api',side_effect=DeliveryRejected('503')):
            self.capture()
        self.assertEqual(self.db.execute('SELECT count(*) FROM editorial_feedback').fetchone()[0],1)
        self.assertEqual(self.db.execute('SELECT status FROM telegram_edit_acknowledgements').fetchone()[0],'SEND_FAILED')
        self.db.execute("UPDATE publication_attempts SET updated_at='2020-01-01T00:00:00+00:00' WHERE status='FAILED'")
        self.db.commit()
        with patch('newsroom.review.telegram_api',return_value={'message_id':901}) as api:
            flush_edit_acknowledgements(self.config,self.db)
            flush_edit_acknowledgements(self.config,self.db)
        api.assert_called_once()
        self.assertEqual(self.db.execute('SELECT status FROM telegram_edit_acknowledgements').fetchone()[0],'SENT')

    def test_bot_event_before_snapshot_does_not_repeat_learning(self):
        update=self._published_edit_update(text='Новый текст')
        with patch('newsroom.interests.summarize_editorial_edit',return_value=['Урок']), \
             patch('newsroom.review.telegram_api',return_value={'message_id':903}) as api:
            handle_update(self.config,self.db,update)
            key=capture_channel_edit(self.config,self.db,chat_id=-1004316291893,message_id=987,
                                     edited_text='Новый текст',previous_text='Тестовый пост',
                                     source_url='https://t.me/testchannel/987')
            self.assertIsNone(key)
            api.assert_called_once()

    def test_unknown_ack_is_not_resent(self):
        self._published_edit_update()
        with patch('newsroom.interests.summarize_editorial_edit',return_value=['Писать короче.']), \
             patch('newsroom.review.telegram_api',side_effect=DeliveryUncertain('timeout')) as api:
            self.capture()
            flush_edit_acknowledgements(self.config,self.db)
        api.assert_called_once()
        self.assertEqual(self.db.execute('SELECT status FROM telegram_edit_acknowledgements').fetchone()[0],'SEND_UNKNOWN')

    def test_incoming_event_survives_handler_failure(self):
        update=self._published_edit_update()
        with patch('newsroom.review.handle_update',side_effect=ValueError):
            with self.assertRaises(ValueError):handle_persisted_update(self.config,self.db,update)
        row=self.db.execute('SELECT * FROM telegram_review_inbox').fetchone()
        self.assertEqual(json.loads(row['payload_json']),update)
        self.assertIsNone(row['processed_at'])

    def test_initial_baseline_is_quiet_then_actual_change_notifies(self):
        self._published_edit_update()
        page='<div data-post="testchannel/987"><div class="tgme_widget_message_text">Первая версия</div></div>'
        with patch('newsroom.cli.telegram_api',return_value={'id':-1004316291893,'username':'testchannel'}), \
             patch('newsroom.core._request_with_url',return_value=(page.encode(),'url','text/html')), \
             patch('newsroom.review.telegram_api',return_value={'message_id':902}) as api:
            self.assertEqual(sync_recent_channel_edits(self.config,self.db,force=True),0)
            api.assert_not_called()
        with patch('newsroom.cli.telegram_api',return_value={'id':-1004316291893,'username':'testchannel'}), \
             patch('newsroom.core._request_with_url',return_value=(page.replace('Первая','Новая').encode(),'url','text/html')), \
             patch('newsroom.interests.summarize_editorial_edit',return_value=['Урок']), \
             patch('newsroom.review.telegram_api',return_value={'message_id':902}) as api:
            self.assertEqual(sync_recent_channel_edits(self.config,self.db,force=True),1)
            self.assertEqual(sync_recent_channel_edits(self.config,self.db,force=True),0)
            api.assert_called_once()


class ChannelParserTests(unittest.TestCase):
    def test_preserves_paragraphs_links_and_emoji_without_footer_noise(self):
        parser=ChannelTextParser('testchannel')
        parser.feed('<div data-post="testchannel/987"><div class="tgme_widget_message_text">🇷🇺 <b>Заголовок</b><br><br>Текст.<br><br>Источник: <a href="https://example.org">Издание</a></div><span>100 views</span></div>')
        self.assertEqual(parser.messages['987'],'🇷🇺 Заголовок\n\nТекст.\n\nИсточник: [Издание](https://example.org)')
