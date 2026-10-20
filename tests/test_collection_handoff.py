from unittest.mock import patch
from edition_helpers import EditionCase,TEXT
from newsroom.edition import store
from newsroom.edition.worker import run_once
from newsroom.locking import acquire_cycle_lock


class CollectionHandoffTests(EditionCase):
    def test_completed_cohort_runs_while_next_collection_holds_lock(self):
        from newsroom.cli import _run_collection_cycle
        mid=self.material()
        lock=acquire_cycle_lock(self.database)
        try:
            with patch('newsroom.cli.run_one_cycle',return_value={'collected':1}):
                self.assertEqual(_run_collection_cycle(self.config,self.database,persistent=True),{'collected':1})
            job=self.db.execute("SELECT job_id FROM edition_jobs WHERE state='SCREENING'").fetchone()[0]
            late=self.material(text=TEXT+' Дополнительный самостоятельный факт.',url='https://example.org/late')
            with patch('newsroom.edition.worker.process',return_value='processed') as process:
                self.assertEqual(run_once(self.config,publish=False),'processed')
            self.assertEqual(process.call_args.args[1],job)
            self.assertEqual(store.read(self.db.execute('SELECT material_ids_json FROM edition_jobs WHERE job_id=?',(job,)).fetchone()[0]),[mid])
            self.assertEqual(self.db.execute('SELECT queue_state FROM edition_materials WHERE material_id=?',(late,)).fetchone()[0],'QUEUED')
        finally:lock.close()

    def test_collection_handoff_respects_owner_stop_and_disabled_edition(self):
        from newsroom.cli import _handoff_collection
        from newsroom.agent_control import set_enabled,AgentDisabled
        self.material()
        disabled={**self.config,'editorial':{'enabled':False}}
        self.assertIsNone(_handoff_collection(disabled,self.database))
        set_enabled(self.config,False)
        with self.assertRaises(AgentDisabled):_handoff_collection(self.config,self.database)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM edition_jobs').fetchone()[0],0)

