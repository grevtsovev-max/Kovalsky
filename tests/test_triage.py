import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from newsroom.db import connect
from newsroom.core import process_item, _retry_ai_held_items
from newsroom.triage import classify, screen, schedule_retry, save_state


def decision(kind='KEEP', **changes):
    return dict({'decision':kind,'reason':'Конкретное изменение правил крипторынка','evidence':'В России изменят правила крипторынка',
                 'story_id':'','what_is_new':'Новые требования','confidence':.99}, **changes)


class TriageTests(unittest.TestCase):
    def test_unknown_selection_is_checked_again_with_unchanged_input(self):
        with patch('newsroom.triage.classify', side_effect=[decision('UNKNOWN'), decision()]) as classify_mock:
            self.assertEqual(screen(self.db, 1, self.item, self.settings)['decision'], 'UNKNOWN')
            self.assertEqual(screen(self.db, 1, self.item, self.settings)['decision'], 'KEEP')
            self.assertEqual(screen(self.db, 1, self.item, self.settings)['decision'], 'KEEP')
        self.assertEqual(classify_mock.call_count, 2)

    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.db=connect(str(Path(self.tmp.name)/'test.db'));self.addCleanup(self.db.close)
        self.db.execute("INSERT INTO sources(name,type,url) VALUES('News','rss','https://example.org/rss')")
        self.source=self.db.execute('SELECT * FROM sources').fetchone()
        self.item={'title':'В России изменят правила крипторынка','url':'https://example.org/new',
                   'published_at':datetime.now(timezone.utc).isoformat()}
        self.settings={'triage_enabled':True}

    def process(self):
        return process_item(self.db,self.source,dict(self.item),.35,3500,48,ai_settings=self.settings)

    def test_rejection_happens_before_source_and_editor(self):
        with patch('newsroom.triage.classify',return_value=decision('NOISE',what_is_new='')),patch('newsroom.core._read_feed_article') as read,patch('newsroom.core.analyze_with_ai') as editor:
            self.assertEqual(self.process(),'NOISE');read.assert_not_called();editor.assert_not_called()

    def test_interesting_missing_source_is_held_without_generating_post(self):
        with patch('newsroom.triage.classify',return_value=decision()),patch('newsroom.core.fetch_publisher_article',side_effect=TimeoutError),patch('newsroom.core._recover_primary',return_value=None),patch('newsroom.core.analyze_with_ai') as editor:
            self.assertEqual(self.process(),'PRIMARY_RETRY');editor.assert_not_called()
        self.assertEqual(self.db.execute('select count(*) from posts').fetchone()[0],0)
        row=self.db.execute("select value from app_state where key='selection_retry:1'").fetchone()
        self.assertEqual(json.loads(row[0])['attempts'],0)

    def test_unknown_missing_source_does_not_repeat_forever(self):
        with patch('newsroom.triage.classify',return_value=decision('UNKNOWN')),patch('newsroom.core.fetch_publisher_article',side_effect=TimeoutError),patch('newsroom.core._recover_primary') as recover:
            self.assertEqual(self.process(),'WAITING_CONFIRMATION');recover.assert_not_called()
        with patch('newsroom.core.process_item') as process:
            self.assertEqual(_retry_ai_held_items(self.db,{1:self.source},{'newsroom':{},'ai':self.settings}),{})
            process.assert_not_called()

    def test_budget_or_service_failure_defers_without_reading(self):
        for settings in ({'triage_enabled':True,'_triage_budget':0},{'triage_enabled':True}):
            self.item['url']+='x';self.item['title']+='x';self.settings=settings
            with patch('newsroom.triage.classify',side_effect=TimeoutError),patch('newsroom.core._read_feed_article') as read:
                self.assertEqual(self.process(),'AI_RETRY');read.assert_not_called()

    def test_due_filter_is_before_limit(self):
        for i in (1,2):
            self.db.execute("INSERT INTO items(item_id,source_id,url,canonical_url,title,discovered_at,content_hash,title_hash,disposition) VALUES(?,1,?,?,? ,?,?,?,'PRIMARY_RETRY')",(i,str(i),str(i),'title',datetime.now(timezone.utc).isoformat(),str(i),str(i)))
        schedule_retry(self.db,1,'PRIMARY_RETRY');self.db.commit()
        with patch('newsroom.core.process_item',return_value='NOISE') as process:
            _retry_ai_held_items(self.db,{1:self.source},{'newsroom':{},'ai':self.settings},limit=1)
        self.assertEqual(process.call_args.kwargs['existing_item_id'],2)

    def test_backoff_grows_and_caps(self):
        now=datetime.now(timezone.utc)
        for expected in (1,2,3,3,3,3):
            schedule_retry(self.db,1,'PRIMARY_RETRY',now)
            data=json.loads(self.db.execute("select value from app_state where key='selection_retry:1'").fetchone()[0])
            self.assertEqual(datetime.fromisoformat(data['next_at'])-now,timedelta(minutes=expected))

    def test_unresolved_source_is_rejected_after_three_retries(self):
        with patch('newsroom.triage.classify',return_value=decision()),patch('newsroom.core.fetch_publisher_article',side_effect=TimeoutError),patch('newsroom.core._recover_primary',return_value=None):
            self.assertEqual(self.process(),'PRIMARY_RETRY')
        for expected in ('PRIMARY_RETRY','PRIMARY_RETRY','REJECTED'):
            with patch('newsroom.core.process_item', wraps=process_item):
                row=self.db.execute('select * from items').fetchone()
                self.db.execute("UPDATE app_state SET value=json_set(value,'$.next_at','2000-01-01T00:00:00+00:00') WHERE key='selection_retry:1'")
                self.db.commit()
                with patch('newsroom.triage.classify',return_value=decision()),patch('newsroom.core.fetch_publisher_article',side_effect=TimeoutError),patch('newsroom.core._recover_primary',return_value=None):
                    outcome=__import__('newsroom.core',fromlist=['_retry_ai_held_items'])._retry_ai_held_items(self.db,{1:self.source},{'newsroom':{},'ai':self.settings},limit=1)
            self.assertEqual(self.db.execute('select disposition from items').fetchone()[0],expected)

    def test_feedback_is_exact_not_topic_ban_and_changed_content_is_reassessed(self):
        save_state(self.db,'editor_feedback:9:2026',{'previous_item':dict(self.item,item_id=9,content='Старое предложение'), 'decision':'NOISE','reason':'Не интересно'})
        item=dict(self.item,content='Старое предложение')
        with patch('newsroom.triage.classify',return_value=decision()) as classify_mock:
            self.assertEqual(screen(self.db,1,item,self.settings)['decision'],'NOISE');classify_mock.assert_not_called()
            item['content']='Принят новый закон с условиями для операторов'
            self.assertEqual(screen(self.db,1,item,self.settings)['decision'],'KEEP');classify_mock.assert_called_once()

    def test_cached_selection_avoids_repeated_api_requests(self):
        with patch('newsroom.triage.classify',return_value=decision()) as classify_mock:
            screen(self.db,1,self.item,self.settings);screen(self.db,1,self.item,self.settings)
        classify_mock.assert_called_once()

    def test_grounded_quote_ignores_punctuation_but_requires_verbatim_words(self):
        self.item['description']='В России изменят правила крипторынка; условия пока неизвестны.'
        quoted=decision('NOISE',evidence='«В России изменят правила крипторынка»',what_is_new='',confidence=.99)
        response={'output':[{'content':[{'type':'output_text','text':json.dumps(quoted)}]}]}
        with patch('newsroom.triage.request_response',return_value=response):
            self.assertEqual(classify(self.item,[],[],{})['decision'],'NOISE')

    def test_low_confidence_ungrounded_or_invalid_duplicate_stays_unknown(self):
        examples=[decision('NOISE',confidence=.7),decision('NOISE',evidence='Вымышленный фрагмент текста'),
                  decision('DUPLICATE',story_id='999',what_is_new=''),decision('DUPLICATE',story_id='1',what_is_new='Утверждены новые сроки')]
        for result in examples:
            response={'output':[{'content':[{'type':'output_text','text':json.dumps(result)}]}]}
            with patch('newsroom.triage.request_response',return_value=response):
                self.assertEqual(classify(self.item,[{'story_id':'1'}],[],{})['decision'],'UNKNOWN')

    def test_read_source_still_passes_full_editor_and_publication_guards(self):
        self.item.update(primary_source_status='READ',primary_source_url='https://cbr.ru/new',primary_source_content='В России изменят правила крипторынка и доступ организаций к торгам.')
        result={'action':'NOISE','is_relevant':False,'geographic_scope':'RUSSIA','publication_recommendation':'DO_NOT_PUBLISH'}
        with patch('newsroom.triage.classify',return_value=decision()),patch('newsroom.core.get_api_key',return_value='test'),patch('newsroom.core.analyze_with_ai',return_value=result) as editor:
            self.assertEqual(self.process(),'NOISE');editor.assert_called_once()
        self.assertEqual(self.db.execute('select count(*) from posts').fetchone()[0],0)

    def test_seven_real_editor_decisions_replay_without_network(self):
        fixtures=json.loads((Path(__file__).parent/'fixtures'/'triage_editor_decisions.json').read_text())
        now=datetime.now(timezone.utc).isoformat()
        self.db.execute("INSERT INTO stories(story_id,canonical_topic,headline,first_seen_at,last_updated_at) VALUES(55,'Криптоаналитика','Минфин готовит требования',?,?)",(now,now))
        self.db.execute("INSERT INTO posts(post_id,story_id,text,status,created_at,version,post_hash) VALUES(78,55,'Минфин готовит требования','PUBLISHED',?,1,'test')",(now,))
        for index,fixture in enumerate(fixtures):
            save_state(self.db,'editor_feedback:'+str(index),fixture['feedback'])
        with patch('newsroom.triage.request_response',side_effect=AssertionError('network forbidden')):
            for index,fixture in enumerate(fixtures):
                result=screen(self.db,index,fixture['item'],self.settings)
                self.assertEqual(result['decision'],fixture['feedback']['decision'])
                self.assertEqual(result['origin'],'editor')

    def test_rediscovered_existing_item_does_not_reset_retry_schedule(self):
        with patch('newsroom.triage.classify',return_value=decision()),patch('newsroom.core.fetch_publisher_article',side_effect=TimeoutError),patch('newsroom.core._recover_primary',return_value=None):
            self.assertEqual(self.process(),'PRIMARY_RETRY')
        before=self.db.execute("select value from app_state where key='selection_retry:1'").fetchone()[0]
        self.assertEqual(self.process(),'DUPLICATE')
        self.assertEqual(self.db.execute("select value from app_state where key='selection_retry:1'").fetchone()[0],before)

    def test_valid_duplicate_stops_before_source_read(self):
        self.db.execute("INSERT INTO stories(story_id,canonical_topic,headline,first_seen_at,last_updated_at) VALUES(55,'topic','headline','now','now')")
        with patch('newsroom.triage.classify',return_value=decision('DUPLICATE',story_id='55',what_is_new='')),patch('newsroom.core._read_feed_article') as read:
            self.assertEqual(self.process(),'DUPLICATE');read.assert_not_called()
        self.assertEqual(self.db.execute('select story_id from items').fetchone()[0],55)
