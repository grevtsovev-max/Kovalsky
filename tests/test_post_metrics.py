import unittest
from newsroom.post_metrics import aggregate

class PostMetricsTests(unittest.TestCase):
    def event(self, response='r1', seconds=10, usd=.002):
        return ('draft', {'receipt':{'response_id':response,'elapsed_seconds':seconds,'pricing':{'total_usd':usd}}})
    def test_sum_and_duplicate_receipt(self):
        first=self.event()
        result=aggregate([first,first,('review_response',self.event('r2',20,.003)[1])])
        self.assertEqual(result['calls'],2)
        self.assertEqual(result['api_seconds'],30)
        self.assertAlmostEqual(result['usd'],.005)
    def test_unknown_cost_is_not_zero(self):
        result=aggregate([self.event(usd=None)])
        self.assertEqual(result['calls'],1)
        self.assertIsNone(result['usd'])
        self.assertIsNone(aggregate([])['calls'])
    def test_failed_review_does_not_claim_complete_total(self):
        result=aggregate([self.event(),('review_metrics',{'error':True})])
        self.assertIsNone(result['usd'])
        self.assertIsNone(result['api_seconds'])
    def test_planning_is_excluded(self):
        self.assertIsNone(aggregate([('planning',self.event()[1])])['calls'])
    def test_latency_and_stage_durations(self):
        events=[self.event(),('repair',self.event('r2',3,.001)[1]),('published',{'received_to_channel_seconds':90})]
        result=aggregate(events)
        self.assertEqual(result['received_to_post_seconds'],90)
        self.assertEqual(result['stages']['draft']['seconds'],10)
        self.assertEqual(result['stages']['repair']['seconds'],3)
    def test_planning_cost_is_shared_without_multiplying_job_cost(self):
        import sqlite3,json
        from newsroom.post_metrics import for_posts
        db=sqlite3.connect(':memory:')
        db.executescript('CREATE TABLE edition_documents(document_id TEXT,job_id TEXT,post_id INTEGER); CREATE TABLE edition_jobs(job_id TEXT); CREATE TABLE edition_events(event_id INTEGER,document_id TEXT,job_id TEXT,stage TEXT,payload_json TEXT); INSERT INTO edition_jobs VALUES("j"); INSERT INTO edition_documents VALUES("a","j",1),("b","j",2);')
        db.execute('INSERT INTO edition_events VALUES(1,NULL,"j","plan_response",?)',(json.dumps(self.event('plan',20,.01)[1]),))
        for n,doc in [(1,'a'),(2,'b')]:
            db.execute('INSERT INTO edition_events VALUES(?, ?, "j", "draft", ?)',(n+1,doc,json.dumps(self.event(doc,10,.002)[1])))
        result=for_posts(db,[1,2])
        self.assertAlmostEqual(result[1]['accounted_path_usd'],.007)
        self.assertAlmostEqual(sum(v['planning_share_usd'] for v in result.values()),.01)
        db.close()
