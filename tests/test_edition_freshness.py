from datetime import datetime, timedelta, timezone
from unittest.mock import Mock, patch
from edition_helpers import EditionCase, TEXT
from newsroom.core import _save_item
from newsroom.edition import freshness, store, model
from newsroom.edition.fallback import ensure_job, release
from newsroom.edition.publication import publish_document, gate
from newsroom.edition.worker import recover
from newsroom.delivery import DeliveryRejected, TelegramReceipt


class FreshnessTests(EditionCase):
    def dated(self, value, queue=True):
        source=dict(self.db.execute('SELECT * FROM sources WHERE source_id=?',(self.source,)).fetchone())
        content=TEXT+' Номер сообщения '+str(self.db.execute('SELECT COUNT(*) FROM items').fetchone()[0])+'.'
        item_id=_save_item(self.db,source,{'url':'https://example.org/'+str(self.db.execute('SELECT COUNT(*) FROM items').fetchone()[0]),
                                         'title':'Компания запустила сервис', 'content':content, 'description':content, 'published_at':value})
        mid=store.capture(self.db,item_id,eligible=queue,queue=queue);self.db.commit()
        return mid

    def old(self):
        return self.dated('2026-01-27T10:01:00+00:00')

    def document(self, ids, state='DRAFTING'):
        now=store.stamp();job='job-'+str(self.db.execute('SELECT COUNT(*) FROM edition_jobs').fetchone()[0]);doc='doc-'+job
        self.db.execute('INSERT INTO edition_jobs(job_id,state,cutoff,received_at,material_ids_json,bundle_hash,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)',
                        (job,'DRAFTING',now,now,store.encoded(ids),model.bundle()[2],now,now))
        group={'subject':'Компания','entities':[], 'material_ids':ids, 'focus':[], 'lookup_query':'', 'event_key':''}
        self.db.execute('INSERT INTO edition_documents(document_id,job_id,group_json,materials_json,state,created_at,updated_at) VALUES(?,?,?,?,?,?,?)',
                        (doc,job,store.encoded(group),store.encoded(store.materials(self.db,ids)),state,now,now));self.db.commit()
        return self.db.execute('SELECT * FROM edition_documents WHERE document_id=?',(doc,)).fetchone()

    def test_exact_48_hour_boundary_timezones_and_queue_delay(self):
        m={'received_at':'2026-10-10T12:00:00+03:00','published_at':'2026-10-08T09:00:00Z'}
        self.assertEqual(freshness.assess(m)['decision'],'FRESH')
        m['published_at']='2026-10-08T08:59:59.999999Z'
        self.assertEqual(freshness.assess(m)['decision'],'STALE')
        m['published_at']='2026-10-10T08:00:00Z'
        with patch.object(store,'stamp',return_value='2027-01-01T00:00:00Z'):
            self.assertEqual(freshness.assess(m)['decision'],'FRESH')

    def test_old_source_is_archived_and_searchable_not_queued(self):
        mid=self.old()
        self.assertIsNone(self.job())
        self.assertEqual(self.db.execute('SELECT queue_state FROM edition_materials WHERE material_id=?',(mid,)).fetchone()[0],'ARCHIVE')
        self.assertEqual(self.db.execute('SELECT decision FROM edition_date_admissions WHERE material_id=?',(mid,)).fetchone()[0],'STALE')
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM edition_search WHERE edition_search MATCH 'Компания'").fetchone()[0],1)

    def test_unknown_invalid_and_ambiguous_dates_publish_on_planner_failure(self):
        for value in (None, '', 'not-a-date', '2026-01-27T10:01:00'):
            with self.subTest(value=value):
                mid=self.dated(value);job=self.job();send=Mock(return_value=TelegramReceipt({'message_id':123}))
                from newsroom.edition.pipeline import process
                with patch.object(model,'plan',side_effect=TimeoutError()):
                    process(self.db,job,self.settings,send=send)
                send.assert_called_once()
                self.assertEqual(self.db.execute('SELECT decision FROM edition_date_admissions WHERE material_id=?',(mid,)).fetchone()[0],'UNKNOWN')

    def test_existing_queue_and_screening_cannot_bypass_date_filter(self):
        mid=self.old()
        self.db.execute("UPDATE edition_materials SET queue_state='QUEUED' WHERE material_id=?",(mid,));self.db.commit()
        self.assertIsNone(self.job())
        doc=self.document([mid]);from newsroom.edition.semantic_filter import screen
        with patch.object(model,'semantic_screen') as classify:
            allowed,excluded=screen(self.db,doc['job_id'],store.materials(self.db,[mid]),self.settings)
        classify.assert_not_called();self.assertEqual(allowed,[]);self.assertEqual(excluded[0]['code'],'source_age')

    def test_restart_and_source_release_filter_old_origins(self):
        mid=self.old();doc=self.document([mid])
        with patch.object(model,'draft') as writing:
            recover(self.db,self.config)
        writing.assert_not_called()
        self.assertEqual(self.db.execute('SELECT state FROM edition_documents WHERE document_id=?',(doc['document_id'],)).fetchone()[0],'FILTERED')
        send=Mock();publish_document(self.db,doc['document_id'],self.config,send=send);send.assert_not_called()
        ensure_job(self.db,doc['job_id'],{'code':'test','reason':'test'},settings=self.settings)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM edition_documents WHERE state='READY'").fetchone()[0],0)

    def test_old_context_does_not_block_fresh_origin(self):
        old=self.old();fresh=self.material();doc=self.document([fresh]);materials=store.materials(self.db,[fresh,old])
        self.db.execute('UPDATE edition_documents SET materials_json=? WHERE document_id=?',(store.encoded(materials),doc['document_id']));self.db.commit()
        doc=self.db.execute('SELECT * FROM edition_documents WHERE document_id=?',(doc['document_id'],)).fetchone()
        self.assertTrue(freshness.protect_document(self.db,doc))

    def test_mixed_draft_requeues_fresh_origin_and_filters_stale(self):
        old=self.old();fresh=self.material();doc=self.document([old,fresh],state='READY');send=Mock()
        publish_document(self.db,doc['document_id'],self.config,send=send);send.assert_not_called()
        job=self.job();self.assertEqual(store.read(self.db.execute('SELECT material_ids_json FROM edition_jobs WHERE job_id=?',(job,)).fetchone()[0],[]),[fresh])

    def test_gate_blocks_old_origins_including_source_mode(self):
        doc=self.document([self.old()],state='READY')
        with self.assertRaisesRegex(DeliveryRejected,'EDITION_SOURCE_TOO_OLD'):
            gate(self.db,doc['document_id'],'')

    def test_published_and_unknown_delivery_are_preserved(self):
        for state in ('PUBLISHED','UNKNOWN','SENDING'):
            doc=self.document([self.old()],state=state)
            self.assertTrue(freshness.protect_document(self.db,doc))
            self.assertEqual(self.db.execute('SELECT state FROM edition_documents WHERE document_id=?',(doc['document_id'],)).fetchone()[0],state)
