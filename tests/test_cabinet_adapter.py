import unittest
from tests import test_pipeline as fixtures
from newsroom.cabinet_pipeline import pipeline_snapshot


class CabinetAdapterTests(unittest.TestCase):
    setUp = fixtures.PipelineTests.setUp
    tearDown = fixtures.PipelineTests.tearDown
    add = fixtures.PipelineTests.add
    post = fixtures.PipelineTests.post
    screening = fixtures.PipelineTests.screening
    test_progress_link_returns_materials_that_passed_step_even_if_later_rejected = fixtures.PipelineTests.test_progress_link_returns_materials_that_passed_step_even_if_later_rejected

    def snapshot(self, **params):
        return pipeline_snapshot(self.db, {}, {k:[str(v)] for k,v in params.items()}, self.posts, self.now)

    def test_read_text_survives_topic_rejection_and_rule_changes(self):
        for i in range(1, 6):
            self.add(i, 'NOISE' if i == 2 else 'PENDING',
                     primary={'_material_read': True})
        self.add(6)
        self.screening(2, False)
        self.screening(3, revision='old')
        self.screening(4)
        self.screening(5)
        self.screening(5, False, dependency='new-rules', at='2026-09-28T11:01:00+00:00')
        report = self.snapshot(milestone='primary_read')
        self.assertEqual(report['totals']['first_filter'], 1)
        self.assertEqual(report['totals']['primary_read'], 1)
        self.assertEqual(report['evidence_totals']['primary_read'], 5)
        self.assertEqual({i['item_id'] for i in report['items']}, {4})
        self.assertEqual(report['total'], report['totals']['primary_read'])

    def test_independent_metrics_do_not_invent_missing_read_or_check(self):
        self.add(1, 'NEW_STORY')
        self.post()
        self.posts[0].update(origin_item_id=1)
        report = self.snapshot()
        self.assertEqual(report['totals']['published'], 0)
        self.assertEqual(report['evidence_totals']['published'], 1)
        self.assertEqual(report['totals']['primary_read'], 0)
        self.assertEqual(report['totals']['checked'], 0)
        for card in report['funnel']:
            self.assertEqual(self.snapshot(milestone=card['key'])['total'], card['count'])

    test_counter_reset_keeps_history_and_limits_milestone_lists = fixtures.PipelineTests.test_counter_reset_keeps_history_and_limits_milestone_lists

    def test_current_edition_uses_saved_checks_and_all_linked_materials(self):
        import json, hashlib
        from newsroom.cabinet_pipeline import edition_progress
        self.add(1)
        self.add(2)
        self.screening(1)
        self.db.executescript('''
            CREATE TABLE posts(post_id INTEGER,status TEXT,external_id TEXT);
            CREATE TABLE edition_materials(material_id INTEGER,item_id INTEGER,revision TEXT,queue_state TEXT,eligible INTEGER,content TEXT,received_at TEXT DEFAULT '2026-09-28');
            CREATE TABLE edition_jobs(job_id TEXT,state TEXT,material_ids_json TEXT,updated_at TEXT);
            CREATE TABLE edition_documents(document_id TEXT,group_json TEXT,draft_json TEXT,rendered_text TEXT,state TEXT,post_id INTEGER,updated_at TEXT);
            CREATE TABLE edition_checks(document_id TEXT,rendered_hash TEXT);
            INSERT INTO edition_materials(material_id,item_id,revision,queue_state,eligible,content) VALUES(1,1,'r1','QUEUED',1,'Source one'),(2,2,'r1','QUEUED',1,'Source two');
            INSERT INTO edition_jobs VALUES('job','CHECKING','[1,2]','2026-09-28');
        ''')
        self.db.execute('INSERT INTO edition_documents VALUES(?,?,?,?,?,?,?)',
                        ('doc',json.dumps({'material_ids':[1,2]}),'{}','Current text','CHECKING',None,'2026-09-28'))
        self.db.execute('INSERT INTO edition_checks VALUES(?,?)',('doc',hashlib.sha256(b'Old text').hexdigest()))
        tables={r[0] for r in self.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        result=edition_progress(self.db,tables)
        self.assertEqual(set(result),{1,2})
        self.assertFalse(result[1]['progress']['checked'])
        self.db.execute('INSERT INTO edition_checks VALUES(?,?)',('doc',hashlib.sha256(b'Current text').hexdigest()))
        self.assertTrue(edition_progress(self.db,tables)[2]['progress']['checked'])

    def test_edition_exclusions_and_duplicates_are_terminal(self):
        import json
        from newsroom.cabinet_pipeline import edition_progress
        self.add(1)
        self.add(2)
        self.add(3)
        self.screening(1)
        self.db.executescript("""
            CREATE TABLE posts(post_id INTEGER,status TEXT,external_id TEXT);
            CREATE TABLE edition_materials(material_id INTEGER,item_id INTEGER,revision TEXT,queue_state TEXT,eligible INTEGER,content TEXT,received_at TEXT DEFAULT '2026-09-28');
            CREATE TABLE edition_jobs(job_id TEXT,state TEXT,material_ids_json TEXT,reasons_json TEXT,updated_at TEXT);
            CREATE TABLE edition_documents(document_id TEXT,group_json TEXT,draft_json TEXT,rendered_text TEXT,state TEXT,post_id INTEGER,reasons_json TEXT,updated_at TEXT);
            CREATE TABLE edition_checks(document_id TEXT,rendered_hash TEXT);
            CREATE TABLE edition_admissions(material_id INTEGER,reason TEXT);
            INSERT INTO edition_materials(material_id,item_id,revision,queue_state,eligible,content) VALUES(1,1,'r1','DONE',1,''),(2,2,'r1','DUPLICATE',1,''),(3,3,'r1','CLAIMED',1,'');
            INSERT INTO edition_admissions VALUES(2,'Тот же текст, новых фактов нет.');
            INSERT INTO edition_jobs VALUES('active','PLANNING','[3]','[]','2026-09-28');
        """)
        reason = 'Наблюдение за выводами ETH и резервами биржи.'
        self.db.execute('INSERT INTO edition_jobs VALUES(?,?,?,?,?)',
                        ('mixed','PUBLISHED','[1]',json.dumps([{'material_id':1,'reason':reason}]),'2026-09-28'))
        tables={r[0] for r in self.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        result=edition_progress(self.db,tables)
        self.assertEqual(result[1]['category'],'filtered')
        self.assertEqual(result[1]['reason'],reason)
        self.assertTrue(result[1]['progress']['analyzed'])
        self.assertFalse(result[1]['progress']['primary_read'])
        self.db.execute("UPDATE edition_materials SET eligible=0,content='Saved text' WHERE material_id=2")
        duplicate = edition_progress(self.db,tables)[2]['progress']
        self.assertFalse(duplicate['first_filter'])
        self.assertTrue(duplicate['primary_read'])
        self.assertEqual(result[2]['category'],'filtered')
        self.assertEqual(result[2]['reason'],'Тот же текст, новых фактов нет.')
        self.assertEqual(result[3]['category'],'ai')
        self.db.execute("UPDATE edition_jobs SET state='FILTERED',reasons_json='[]' WHERE job_id='mixed'")
        self.assertEqual(edition_progress(self.db,tables)[1]['category'],'filtered')
        self.db.execute("UPDATE edition_jobs SET state='PUBLISHED' WHERE job_id='mixed'")
        self.assertEqual(edition_progress(self.db,tables)[1]['category'],'processed')

    def test_archive_before_activation_is_not_a_live_queue(self):
        from newsroom.cabinet_pipeline import edition_progress
        self.add(1)
        self.screening(1)
        self.db.executescript("""
            CREATE TABLE posts(post_id INTEGER,status TEXT,external_id TEXT);
            CREATE TABLE edition_materials(material_id INTEGER,item_id INTEGER,revision TEXT,queue_state TEXT,eligible INTEGER,content TEXT,received_at TEXT);
            CREATE TABLE edition_jobs(job_id TEXT,state TEXT,material_ids_json TEXT,updated_at TEXT);
            CREATE TABLE edition_documents(document_id TEXT,group_json TEXT,updated_at TEXT);
            CREATE TABLE edition_checks(document_id TEXT,rendered_hash TEXT);
            INSERT INTO edition_materials VALUES(1,1,'r1','ARCHIVE',0,'','2026-09-27');
            CREATE TABLE app_state(key TEXT,value TEXT);
            INSERT INTO app_state(key,value) VALUES('edition_v2_activation','2026-09-28');
        """)
        tables={r[0] for r in self.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertEqual(edition_progress(self.db,tables)[1]['category'],'processed')
        self.assertIn('до запуска',edition_progress(self.db,tables)[1]['reason'])
        self.db.execute("UPDATE items SET disposition='PRIMARY_RETRY'")
        self.assertEqual(edition_progress(self.db,tables)[1]['category'],'processed')

    def test_original_checked_text_survives_display_edit_in_evidence_summary(self):
        import hashlib
        self.add(1,'NEW_STORY')
        self.post()
        self.posts[0].update(saved_text='Original',text='Display edit')
        self.posts[0]['facts']['final_text_check']={'assembled_sha256':hashlib.sha256(b'Original').hexdigest()}
        report=self.snapshot()
        self.assertEqual(report['evidence_totals']['checked'],1)
        self.assertEqual(report['totals']['checked'],0)
