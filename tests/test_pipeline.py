import json
import sqlite3
import unittest
from datetime import datetime, timezone
from newsroom.pipeline import pipeline_snapshot


class PipelineTests(unittest.TestCase):
    def test_processing_job_exposes_queue_state_without_worker_payload(self):
        self.add(1, 'WAITING_CONFIRMATION')
        self.db.execute('CREATE TABLE processing_jobs(job_id INTEGER,item_id INTEGER,category TEXT,status TEXT,attempts INTEGER,next_at TEXT,outcome TEXT,error_code TEXT,payload_json TEXT)')
        self.db.execute("INSERT INTO processing_jobs VALUES(1,1,'retry','WAITING',2,'2026-09-28T12:02:00+00:00','AI_RETRY',NULL,'private payload')")
        job = self.snapshot()['items'][0]['processing_job']
        self.assertEqual(job['category'], 'retry')
        self.assertEqual(job['attempts'], 2)
        self.assertNotIn('payload_json', job)
        self.db.execute('CREATE TABLE processing_job_events(event_id INTEGER,job_id INTEGER,status TEXT,created_at TEXT)')
        self.db.execute("INSERT INTO processing_job_events VALUES(1,1,'LEASE_EXPIRED','2026-09-28T11:59:00+00:00')")
        recovery = self.snapshot()['items'][0]['processing_job']['last_recovery']
        self.assertEqual(recovery['status'], 'LEASE_EXPIRED')
        self.assertEqual(set(recovery), {'status', 'created_at'})

    def test_saved_gate_reasons_are_visible_for_waiting_material(self):
        self.add(1, 'WAITING_CONFIRMATION')
        check = {'status': 'UNVERIFIED', 'reason': 'Не подтверждена дата события.'}
        self.db.execute('INSERT INTO item_analysis VALUES(?,?,?)',
                        (1, '2026-09-28T11:00:00+00:00', json.dumps({
                            'development_date_check': check, 'memory_issues': ['Нужна сверка факта'],
                            'publication_recommendation': 'WAIT_FOR_AUTOMATION', 'source_review_required': True})))
        item = self.snapshot()['items'][0]
        self.assertEqual(item['date_check'], check)
        self.assertEqual(item['memory_issues'], ['Нужна сверка факта'])
        self.assertTrue(item['source_review_required'])

    def setUp(self):
        self.db = sqlite3.connect(':memory:')
        self.db.row_factory = sqlite3.Row
        self.db.executescript('''
            CREATE TABLE sources(source_id INTEGER, name TEXT);
            INSERT INTO sources VALUES(1,'АСРОС');
            CREATE TABLE items(item_id INTEGER,source_id INTEGER,title TEXT,url TEXT,canonical_url TEXT,
                published_at TEXT,discovered_at TEXT,processed_at TEXT,disposition TEXT,story_id INTEGER,primary_source_json TEXT);
            CREATE TABLE item_analysis(item_id INTEGER,created_at TEXT,result_json TEXT);
            CREATE TABLE interest_feedback(item_id INTEGER,is_interesting INTEGER);
        ''')
        self.now = datetime(2026,9,28,12,tzinfo=timezone.utc)
        self.posts = []

    def tearDown(self):
        self.db.close()

    def add(self, i, disposition='PENDING', discovered='2026-09-28T10:00:00+00:00', primary=None):
        self.db.execute('INSERT INTO items VALUES(?,?,?,?,?,?,?,?,?,?,?)',
            (i,1,'Материал '+str(i),'https://source/'+str(i),'https://source/'+str(i),
             '2026-09-26T18:22:14+00:00',discovered,discovered,disposition,1,json.dumps(primary or {})))

    def snapshot(self, **params):
        return pipeline_snapshot(self.db,{}, {k:[str(v)] for k,v in params.items()},self.posts,self.now)

    def post(self, url='https://source/1', status='PUBLISHED', created='2026-09-28T11:00:00+00:00'):
        self.posts.append(dict(post_id=22,story_id=1,status=status,created_at=created,
            published_at=created if status=='PUBLISHED' else None, facts={'primary_source':{'url':url}},
            auto_reason='Ожидает решения'))

    def test_partition_counts_and_pagination_cover_more_than_100(self):
        for i in range(105): self.add(i, 'NOISE' if i%2 else 'PRIMARY_RETRY')
        first=self.snapshot(period='all');second=self.snapshot(period='all',offset=30)
        self.assertEqual(first['totals']['received'],105)
        self.assertEqual(sum(s['count'] for s in first['stages']),105)
        self.assertEqual(len(first['items']),30)
        self.assertFalse({x['item_id'] for x in first['items']} & {x['item_id'] for x in second['items']})
        filtered=self.snapshot(period='all',stage='filtered')
        self.assertEqual(filtered['total'],52)
        self.assertEqual(filtered['totals']['received'],105)

    def test_period_uses_received_not_article_or_processing_date(self):
        self.add(1,discovered='2026-09-26T19:42:14+00:00')
        self.assertEqual(self.snapshot(period=24)['total'],0)
        self.assertEqual(self.snapshot(period=48)['total'],1)

    def test_duplicate_and_held_never_inherit_story_publication(self):
        for i,status in enumerate(['DUPLICATE','PRIMARY_RETRY','WAITING_CONFIRMATION'],1):
            self.add(i,status,primary={'status':'READ','url':'https://primary'})
        self.post('https://primary')
        result=self.snapshot()
        self.assertEqual(result['totals']['published'],0)
        self.assertTrue(all(x['post'] is None for x in result['items']))

    def test_primary_match_required_and_null_legacy_source_is_safe(self):
        self.add(1,'NEW_STORY')
        self.post('https://unrelated')
        self.posts[0]['facts']['primary_source']=None
        self.assertIsNone(self.snapshot()['items'][0]['post'])

    def test_publication_and_review(self):
        self.add(1,'NEW_STORY', primary={'status':'READ','url':'https://source/1'})
        self.post()
        self.assertEqual(self.snapshot()['items'][0]['stage'],'published')
        self.posts[0]['status']='PENDING'
        self.assertEqual(self.snapshot()['items'][0]['stage'],'review')
        self.posts[0]['status']='REJECTED'
        self.assertEqual(self.snapshot()['items'][0]['stage'],'filtered')

    def test_legacy_post_matches_source_and_processing_time(self):
        self.add(1,'NEW_STORY')
        self.add(2,'UPDATE_CANDIDATE',discovered='2026-09-28T11:01:00+00:00')
        self.post(created='2026-09-28T10:00:01+00:00')
        self.posts[0]['facts']['primary_source']=None
        self.posts[0]['source_ids']=[1]
        result=self.snapshot()
        self.assertEqual(result['totals']['published'],1)
        self.assertEqual(next(x for x in result['items'] if x['item_id']==1)['stage'],'published')
        self.assertIsNone(next(x for x in result['items'] if x['item_id']==2)['post'])

    def test_analysis_search_and_corrupt_metadata(self):
        self.add(1,'PRIMARY_RETRY')
        self.db.execute("UPDATE items SET primary_source_json='broken'")
        self.db.execute('INSERT INTO item_analysis VALUES(1,?,?)',('2026-09-28T10:01:00+00:00',json.dumps({'summary_ru':'Вывод'})))
        result=self.snapshot(q='асрос')
        self.assertEqual(result['totals']['analyzed'],1)
        self.assertEqual(result['items'][0]['summary'],'Вывод')
        self.assertEqual(self.snapshot(q='нет совпадений')['total'],0)

if __name__=='__main__':unittest.main()
