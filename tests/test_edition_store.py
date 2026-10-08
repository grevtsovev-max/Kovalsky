from datetime import datetime,timezone,timedelta
from unittest.mock import patch,Mock
from newsroom.edition import store,model
from newsroom.edition.worker import recover,run_once
from newsroom.edition.views import overview
from newsroom.agent_control import AgentDisabled,set_enabled
from newsroom.material_flow import mark
from edition_helpers import EditionCase,draft,group,LEAD,TEXT,receipt


class StoreTests(EditionCase):
    def test_archive_indexing_does_not_enqueue_historical_materials(self):
        self.material(queue=False)
        store.archive_step(self.db)
        self.assertIsNone(store.start(self.db,model.bundle()[2]))
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM edition_materials').fetchone()[0],1)

    def test_archive_capture_does_not_prevent_later_admission(self):
        mid=self.material(queue=False)
        item_id=self.db.execute('SELECT item_id FROM edition_materials WHERE material_id=?',(mid,)).fetchone()[0]
        store.capture(self.db,item_id,eligible=True,queue=True);self.db.commit()
        self.assertIsNotNone(store.start(self.db,model.bundle()[2]))

    def test_only_current_first_filter_passes_are_queued(self):
        mid=self.material(queue=False);row=self.db.execute('SELECT item_id,revision FROM edition_materials WHERE material_id=?',(mid,)).fetchone()
        # The captured test material was already received; activation is moved earlier only in this isolated DB.
        self.db.execute("UPDATE app_state SET value='2000-01-01T00:00:00+00:00' WHERE key='edition_v2_activation'");self.db.commit()
        store.ingest(self.db,row['item_id'],{'_edition_enabled':True})
        self.assertIsNone(store.start(self.db,model.bundle()[2]))
        mark(self.db,row['item_id'],'screening','DONE','Первый фильтр пройден');self.db.commit()
        store.ingest(self.db,row['item_id'],{'_edition_enabled':True})
        self.assertIsNotNone(store.start(self.db,model.bundle()[2]))

    def test_disabled_editor_cannot_queue(self):
        mid=self.material(queue=False);item=self.db.execute('SELECT item_id FROM edition_materials WHERE material_id=?',(mid,)).fetchone()[0]
        store.ingest(self.db,item,{'_edition_enabled':False})
        self.assertIsNone(store.start(self.db,model.bundle()[2]))

    def test_local_search_is_indexed_limited_and_cutoff_bound(self):
        initial=self.material();job=self.job()
        old=[]
        for n in range(7):old.append(self.material(text='Альфа перевод сеть '+str(n),queue=False))
        # These are later arrivals and must not leak into the initial context.
        context=store.lookup(self.db,job,'Альфа')
        self.assertIn(initial,[m['material_id'] for m in context])
        self.assertFalse(set(old)&{m['material_id'] for m in context})
        fresh=store.lookup(self.db,job,'Альфа',late=True)
        self.assertEqual(len(fresh),5);self.assertTrue({m['material_id'] for m in fresh}<=set(old))
        self.assertEqual(store.lookup(self.db,job,'Альфа',late=True),[])
        self.assertEqual(store.lookup(self.db,job,'Альфа'),[])
        row=self.db.execute('SELECT searches,late_checked FROM edition_jobs WHERE job_id=?',(job,)).fetchone()
        self.assertEqual(tuple(row),(2,1))

    def test_arrivals_remain_queued_for_next_preparation(self):
        mid=self.material();job=self.job();later=self.material(text=TEXT+' Новая самостоятельная новость.')
        original=store.read(self.db.execute('SELECT material_ids_json FROM edition_jobs WHERE job_id=?',(job,)).fetchone()[0],[])
        self.assertEqual(original,[mid]);self.assertNotIn(later,original)
        store.lookup(self.db,job,'Альфа',late=True)
        self.assertEqual(self.db.execute('SELECT queue_state FROM edition_materials WHERE material_id=?',(later,)).fetchone()[0],'QUEUED')

    def test_search_budget_survives_reopening(self):
        self.material();job=self.job();store.lookup(self.db,job,'Альфа');store.lookup(self.db,job,'Альфа',late=True)
        from newsroom.db import connect
        other=connect(self.database)
        try:self.assertEqual(store.lookup(other,job,'Альфа'),[])
        finally:other.close()

    def test_events_are_append_only(self):
        self.material();job=self.job()
        with self.assertRaises(Exception):self.db.execute('DELETE FROM edition_events WHERE job_id=?',(job,))
        self.db.rollback()
        self.assertTrue(self.db.execute('SELECT COUNT(*) FROM edition_events').fetchone()[0])

    def test_interrupted_work_releases_source_without_automatic_model_rewrite(self):
        self.material();job=self.job();recover(self.db,self.config)
        self.assertEqual(self.db.execute('SELECT state FROM edition_jobs WHERE job_id=?',(job,)).fetchone()[0],'READY')
        with patch.object(model,'plan') as planning:
            self.assertIsNone(run_once(self.config,publish=False));planning.assert_not_called()

    def test_agent_stop_prevents_all_editor_work(self):
        self.material();set_enabled(self.config,False)
        with patch.object(model,'plan') as planning:
            with self.assertRaises(AgentDisabled):run_once(self.config,publish=False)
            planning.assert_not_called()

    def test_over_target_is_displayed_but_not_rejected(self):
        self.material();job=self.job()
        old=(datetime.now(timezone.utc)-timedelta(minutes=11)).isoformat()
        self.db.execute('UPDATE edition_jobs SET received_at=? WHERE job_id=?',(old,job));self.db.commit()
        data=overview(self.db,self.config)
        self.assertTrue(data['jobs'][0]['delayed']);self.assertEqual(data['jobs'][0]['state'],'PLANNING')
