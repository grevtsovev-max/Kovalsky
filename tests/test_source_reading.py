import json
from unittest.mock import patch
from edition_helpers import EditionCase
from newsroom.workflow import enqueue


class SourceReadingTests(EditionCase):
    def collect(self, kind='google_news', **fields):
        self.db.execute('UPDATE sources SET type=? WHERE source_id=?', (kind, self.source))
        self.db.commit()
        source=dict(self.db.execute('SELECT * FROM sources WHERE source_id=?', (self.source,)).fetchone())
        item={'url':'https://example.org/discovery', 'title':'Криптовалюты: семинар',
              'description':'Краткий фрагмент о криптовалютах', 'content':'', **fields}
        settings={'_edition_enabled':True, '_keyword_prefilter':{'keywords':['криптовалюты'], 'version':'test'}}
        item_id=enqueue(self.db,None,item,source,{'settings':settings})
        return item_id

    def test_selected_feed_reads_article_before_editorial_snapshot(self):
        for kind in ('google_news','rss','web','web_search'):
            with self.subTest(kind=kind):
                article={'url':'https://publisher.example/article','title':'Криптовалюты: семинар',
                         'content':'Семинар состоится 20 октября. Подробная программа.', 'material_read':True,
                         'material_url':'https://publisher.example/article'}
                with patch('newsroom.core.fetch_publisher_article',return_value=article) as fetch:
                    item_id=self.collect(kind,url='https://example.org/'+kind,description='Криптовалюты: '+kind)
                fetch.assert_called_once()
                material=self.db.execute('SELECT * FROM edition_materials WHERE item_id=?',(item_id,)).fetchone()
                self.assertEqual(material['queue_state'],'QUEUED')
                self.assertIn('20 октября',material['content'])
                self.assertTrue(json.loads(json.loads(material['snapshot_json'])['primary_source_json'])['_material_read'])
                self.assertEqual(self.db.execute('SELECT COUNT(*) FROM item_revisions WHERE item_id=?',(item_id,)).fetchone()[0],1)

    def test_failed_read_is_recorded_and_does_not_block_release(self):
        with patch('newsroom.core.fetch_publisher_article',side_effect=TimeoutError):
            item_id=self.collect()
        self.assertEqual(self.db.execute('SELECT queue_state FROM edition_materials WHERE item_id=?',(item_id,)).fetchone()[0],'QUEUED')
        state=self.db.execute("SELECT status,block_kind FROM material_stage_state WHERE item_id=? AND stage='reading'",(item_id,)).fetchone()
        self.assertEqual(tuple(state),('DONE',None))
        result=json.loads(self.db.execute("SELECT result_json FROM material_stage_results WHERE item_id=? AND stage='reading'",(item_id,)).fetchone()[0])
        self.assertEqual(result['outcome'],'PARTIAL')
        self.assertEqual(result['error'],'TimeoutError')

    def test_access_denied_page_does_not_replace_saved_feed(self):
        from newsroom.core import _fetch_publisher_article_native
        page=('<html><title>Forbidden</title><body><p>Forbidden. If you are not a bot, '
              'please copy the report and send it to our support team. '+('x'*120)+'</p></body></html>').encode()
        with patch('newsroom.core._request_with_url',return_value=(page,'https://example.org/article','text/html')):
            with self.assertRaisesRegex(ValueError,'PUBLISHER_ACCESS_DENIED'):
                _fetch_publisher_article_native('https://example.org/article','Source',None)
        with patch('newsroom.core.fetch_publisher_article',side_effect=ValueError('PUBLISHER_ACCESS_DENIED')):
            item_id=self.collect()
        row=self.db.execute('SELECT title,content,description FROM items WHERE item_id=?',(item_id,)).fetchone()
        self.assertEqual(row['title'],'Криптовалюты: семинар')
        self.assertNotIn('Forbidden',row['content'])

    def test_rejected_discovery_never_downloads_article(self):
        with patch('newsroom.core.fetch_publisher_article') as fetch:
            self.collect(title='Погода',description='Прогноз',content='')
        fetch.assert_not_called()

    def test_already_read_manual_input_is_not_downloaded_again(self):
        with patch('newsroom.core.fetch_publisher_article') as fetch:
            item_id=self.collect('manual',content='Полная статья про криптовалюты.',material_read=True)
        fetch.assert_not_called()
        self.assertIn('Полная статья',self.db.execute('SELECT content FROM edition_materials WHERE item_id=?',(item_id,)).fetchone()[0])

    def test_telegram_keeps_post_and_reads_link_before_capture(self):
        def linked(item,source):
            item.update(primary_source_url='https://publisher.example/article',primary_source_title='Статья',
                        primary_source_content='Дата и полный текст связанной статьи.',
                        primary_source_type='LINKED_DOCUMENT',primary_source_status='READ')
        with patch('newsroom.core._read_telegram_primary',side_effect=linked) as read:
            item_id=self.collect('telegram',content='Пост про криптовалюты.',material_read=True)
        read.assert_called_once()
        material=json.loads(self.db.execute('SELECT snapshot_json FROM edition_materials WHERE item_id=?',(item_id,)).fetchone()[0])
        self.assertEqual(material['content'],'Пост про криптовалюты.')
        self.assertIn('полный текст',json.loads(material['primary_source_json'])['content'])
