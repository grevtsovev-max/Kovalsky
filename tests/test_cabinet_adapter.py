import unittest
from tests import test_pipeline as fixtures
from newsroom.cabinet_pipeline import pipeline_snapshot


class CabinetAdapterTests(unittest.TestCase):
    setUp = fixtures.PipelineTests.setUp
    tearDown = fixtures.PipelineTests.tearDown
    add = fixtures.PipelineTests.add
    post = fixtures.PipelineTests.post
    screening = fixtures.PipelineTests.screening
    test_reading_counts_only_admitted_current_versions = fixtures.PipelineTests.test_reading_counts_only_admitted_current_versions
    test_telegram_display_edit_does_not_erase_completed_check = fixtures.PipelineTests.test_telegram_display_edit_does_not_erase_completed_check
    test_progress_link_returns_materials_that_passed_step_even_if_later_rejected = fixtures.PipelineTests.test_progress_link_returns_materials_that_passed_step_even_if_later_rejected

    def snapshot(self, **params):
        return pipeline_snapshot(self.db, {}, {k:[str(v)] for k,v in params.items()}, self.posts, self.now)

    def test_current_edition_uses_saved_checks_and_all_linked_materials(self):
        import json, hashlib
        from newsroom.cabinet_pipeline import edition_progress
        self.add(1)
        self.add(2)
        self.screening(1)
        self.db.executescript('''
            CREATE TABLE posts(post_id INTEGER,status TEXT,external_id TEXT);
            CREATE TABLE edition_materials(material_id INTEGER,item_id INTEGER,revision TEXT,queue_state TEXT,eligible INTEGER,content TEXT);
            CREATE TABLE edition_jobs(job_id TEXT,state TEXT,material_ids_json TEXT,updated_at TEXT);
            CREATE TABLE edition_documents(document_id TEXT,group_json TEXT,draft_json TEXT,rendered_text TEXT,state TEXT,post_id INTEGER,updated_at TEXT);
            CREATE TABLE edition_checks(document_id TEXT,rendered_hash TEXT);
            INSERT INTO edition_materials VALUES(1,1,'r1','QUEUED',1,'Source one'),(2,2,'r1','QUEUED',1,'Source two');
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
