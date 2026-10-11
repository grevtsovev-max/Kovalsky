import json
import tempfile
import unittest
from datetime import datetime,timedelta,timezone
from pathlib import Path
from unittest.mock import Mock
from newsroom.db import connect
from newsroom.watch import touch,advance,run

class StoryWatchTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.db=connect(str(Path(self.tmp.name)/'db'));self.addCleanup(self.db.close)
        self.now=datetime(2026,9,28,tzinfo=timezone.utc)
        self.db.execute("INSERT INTO stories(story_id,canonical_topic,headline,first_seen_at,last_updated_at) VALUES(1,'topic','headline','2026','2026')")
        identity={'subject':'Банк России','object':'цифровые активы','action':'предложил','document_id':'RU ЦБ 123','stage':'PROPOSED'}
        self.db.execute("INSERT INTO events(event_id,story_id,identity_key,canonical_event,identity_json,first_seen_at,last_seen_at) VALUES(1,1,'key','event',?,'2026','2026')",(json.dumps(identity),))
        self.cfg={'newsroom':{'story_watch_enabled':True,'story_watch_per_cycle':2},'ai':{'_analysis_budget':2,'_triage_budget':2}}
        touch(self.db,1,1,self.now);self.db.commit()

    def test_due_job_uses_common_pipeline_and_never_assigns_story_to_result(self):
        article={'url':'https://example.org/1','title':'новость'}
        search=Mock(return_value=[article]);process=Mock(return_value='NEW_STORY')
        self.assertEqual(run(self.db,self.cfg,search,process,self.now),{})
        search.assert_not_called()
        result=run(self.db,self.cfg,search,process,self.now+timedelta(hours=7))
        self.assertEqual(result,{'STORY_WATCH_NEW_STORY':1})
        self.assertEqual(process.call_args.args[2],article)
        self.assertNotIn('story_id',process.call_args.args[2])
        self.assertEqual(process.call_args.args[1]['source_role'],'discovery')
        run(self.db,self.cfg,search,process,self.now+timedelta(hours=7))
        search.assert_called_once()

    def test_dormant_story_reopens_with_append_only_history(self):
        advance(self.db,self.now+timedelta(days=91))
        self.assertEqual(self.db.execute('SELECT lifecycle FROM story_monitoring').fetchone()[0],'DORMANT')
        touch(self.db,1,1,self.now+timedelta(days=92))
        self.assertEqual(self.db.execute('SELECT lifecycle FROM story_monitoring').fetchone()[0],'ACTIVE')
        self.assertTrue(self.db.execute("SELECT 1 FROM story_watch_log WHERE action='REOPENED'").fetchone())
        self.assertEqual(self.db.execute('SELECT priority FROM story_monitoring').fetchone()[0],2)

    def test_error_keeps_schedule_and_logs_failure(self):
        result=run(self.db,self.cfg,Mock(side_effect=TimeoutError()),Mock(),self.now+timedelta(hours=7))
        self.assertEqual(result,{'STORY_WATCH_ERROR':1})
        self.assertTrue(self.db.execute("SELECT 1 FROM story_watch_log WHERE action='SEARCH_ERROR'").fetchone())
        search=Mock();run(self.db,self.cfg,search,Mock(),self.now+timedelta(hours=7));search.assert_not_called()

    def test_disabled_or_exhausted_budget_never_searches(self):
        search=Mock()
        self.cfg['ai']['_analysis_budget']=0
        run(self.db,self.cfg,search,Mock(),self.now+timedelta(hours=7))
        search.assert_not_called()

    def test_shared_capacity_deferral_reschedules_without_recording_search_failure(self):
        from newsroom.runtime import BudgetDeferred
        now = self.now+timedelta(hours=7)
        result=run(self.db,self.cfg,Mock(side_effect=BudgetDeferred(delay_seconds=180)),Mock(),now)
        self.assertEqual(result,{'STORY_WATCH_DEFERRED':1})
        job=self.db.execute('SELECT * FROM story_monitoring_jobs').fetchone()
        self.assertEqual(job['next_check_at'],(now+timedelta(seconds=180)).isoformat(timespec='seconds'))
        self.assertIsNone(job['last_checked_at'])
        self.assertTrue(self.db.execute("SELECT 1 FROM story_watch_log WHERE action='SEARCH_DEFERRED'").fetchone())
        self.assertFalse(self.db.execute("SELECT 1 FROM story_watch_log WHERE action='SEARCH_ERROR'").fetchone())
