import json
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
import test_workflow as workflow
from newsroom.core import process_item
from newsroom.source_recheck import verify, SourceUpdateRequired
from newsroom.delivery import DeliveryRejected
from newsroom.runtime import BudgetDeferred
from newsroom.ai import AIResponseError


class SourceRecheckTests(unittest.TestCase):
    def setUp(self):
        self.fixture = workflow.WorkflowTests(methodName='runTest'); self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.db = self.fixture.db
        self.config = self.fixture.config
        self.item = self.fixture.item(991)
        self.decision = self.fixture.publish_result(self.item)
        with patch('newsroom.core.get_api_key', return_value='test'), patch('newsroom.core.analyze_with_ai', return_value=self.decision):
            self.assertEqual(process_item(self.db, self.fixture.source, self.item, .35, 3500, 24,
                                         ai_settings=self.config['ai']), 'NEW_STORY')
        self.post = self.db.execute('SELECT * FROM posts').fetchone()
        self.facts = json.loads(self.post['fact_check_result'])
        self.row = self.db.execute('SELECT * FROM items').fetchone()
        self.original = json.loads(self.row['primary_source_json'])['content']
        self.key = f"policy_admission:{self.row['item_id']}:{self.row['ingest_revision']}"
        self.current = {'material_read': True, 'content': self.original, 'title': self.item['title'], 'url': self.item['primary_source_url']}

    def age(self):
        state = json.loads(self.db.execute('SELECT value FROM app_state WHERE key=?', (self.key,)).fetchone()[0])
        state['at'] = (datetime.now(timezone.utc)-timedelta(hours=25)).isoformat()
        self.db.execute('UPDATE app_state SET value=? WHERE key=?', (json.dumps(state), self.key)); self.db.commit()

    def test_fresh_post_does_not_repeat_source_reading(self):
        with patch('newsroom.core.fetch_publisher_article') as read:
            verify(self.db, self.config, self.post, self.facts)
        read.assert_not_called()

    def test_unchanged_source_is_checked_once_without_reanalysis(self):
        self.age()
        with patch('newsroom.core.fetch_publisher_article', return_value=self.current) as read, patch('newsroom.ai.validate_draft') as check:
            verify(self.db, self.config, self.post, self.facts)
            verify(self.db, self.config, self.post, self.facts)
        read.assert_called_once(); check.assert_not_called()
        self.assertEqual(self.db.execute('SELECT status FROM posts').fetchone()[0], 'PENDING')

    def test_format_revision_reuses_unchanged_source_refresh(self):
        self.age()
        changed_facts = {**self.facts, 'final_text_check': {
            **self.facts['final_text_check'], 'assembled_sha256': 'another-draft-seal'}}
        with patch('newsroom.core.fetch_publisher_article', return_value=self.current) as read, patch('newsroom.ai.validate_draft') as check:
            verify(self.db, self.config, self.post, self.facts)
            verify(self.db, self.config, self.post, changed_facts)
        read.assert_called_once()
        check.assert_not_called()

    def test_new_draft_rechecks_changed_source_from_saved_refresh(self):
        self.age()
        current = {**self.current, 'content': self.original + '\nСправочное уточнение.'}
        checked = {'issues': [], 'covered_claims': [], 'editorial_check': self.facts['editorial_check']}
        changed_facts = {**self.facts, 'final_text_check': {
            **self.facts['final_text_check'], 'assembled_sha256': 'another-draft-seal'}}
        with patch('newsroom.core.fetch_publisher_article', return_value=current) as read, patch('newsroom.ai.validate_draft', return_value=checked) as check:
            verify(self.db, self.config, self.post, self.facts)
            verify(self.db, self.config, self.post, changed_facts)
        read.assert_called_once()
        self.assertEqual(check.call_count, 2)

    def test_second_executor_waits_for_the_same_source_check(self):
        from newsroom.db import connect
        self.age()
        other = connect(self.fixture.path)
        self.addCleanup(other.close)
        def read(*args, **kwargs):
            with self.assertRaises(BudgetDeferred):
                verify(other, self.config, self.post, self.facts)
            return self.current
        with patch('newsroom.core.fetch_publisher_article', side_effect=read) as fetch:
            verify(self.db, self.config, self.post, self.facts)
        fetch.assert_called_once()

    def test_unavailable_page_does_not_cancel_saved_read_evidence(self):
        self.age()
        with patch('newsroom.core.fetch_publisher_article', side_effect=TimeoutError) as read:
            verify(self.db, self.config, self.post, self.facts)
            verify(self.db, self.config, self.post, self.facts)
        read.assert_called_once()
        self.assertEqual(self.db.execute('SELECT primary_source_json FROM items').fetchone()[0], self.row['primary_source_json'])

    def test_source_retraction_closes_old_draft_and_queues_saved_new_version(self):
        self.age()
        current = {**self.current, 'content': 'Банк России отозвал прежнее решение. Допуск участников отменён.'}
        checked = {'issues': ['Прежнее решение отозвано.'], 'covered_claims': [], 'editorial_check': self.facts['editorial_check']}
        with patch('newsroom.core.fetch_publisher_article', return_value=current), patch('newsroom.ai.validate_draft', return_value=checked):
            with self.assertRaises(SourceUpdateRequired):
                verify(self.db, self.config, self.post, self.facts)
        self.assertEqual(self.db.execute('SELECT status FROM posts').fetchone()[0], 'SUPERSEDED')
        updated = self.db.execute('SELECT * FROM items').fetchone()
        self.assertNotEqual(updated['ingest_revision'], self.row['ingest_revision'])
        self.assertIn('отозвал', updated['content'])
        self.assertEqual(self.db.execute("SELECT count(*) FROM processing_jobs WHERE status='PENDING'").fetchone()[0], 1)
        self.assertGreater(self.db.execute('SELECT count(*) FROM item_revisions').fetchone()[0], 0)
        self.assertEqual(self.db.execute('SELECT count(*) FROM publication_attempts').fetchone()[0], 0)

    def test_change_check_transport_retries_are_bounded_and_do_not_reread(self):
        self.age()
        current = {**self.current, 'content': self.original + '\nНовое уточнение источника.'}
        now = datetime.now(timezone.utc)
        before = self.db.execute("SELECT value FROM app_state WHERE key='selection_retry:1'").fetchone()
        with patch('newsroom.core.fetch_publisher_article', return_value=current) as read, patch('newsroom.ai.validate_draft', side_effect=AIResponseError('NETWORK_TIMEOUT')) as check:
            for index in range(5):
                expected = BudgetDeferred if index < 3 else DeliveryRejected
                with self.assertRaises(expected):
                    verify(self.db, self.config, self.post, self.facts, now=now+timedelta(seconds=index*400))
        read.assert_called_once()
        self.assertEqual(check.call_count, 4)
        after = self.db.execute("SELECT value FROM app_state WHERE key='selection_retry:1'").fetchone()
        self.assertEqual(tuple(before) if before else None, tuple(after) if after else None)
        self.assertEqual(self.db.execute('SELECT status FROM posts').fetchone()[0], 'PENDING')

    def test_new_source_details_that_do_not_invalidate_post_need_no_full_analysis(self):
        self.age()
        current = {**self.current, 'content': self.original + '\nДополнительная справочная информация.'}
        checked = {'issues': [], 'covered_claims': [], 'editorial_check': self.facts['editorial_check']}
        with patch('newsroom.core.fetch_publisher_article', return_value=current) as read, patch('newsroom.ai.validate_draft', return_value=checked) as check, patch('newsroom.core.analyze_with_ai') as analyze:
            verify(self.db, self.config, self.post, self.facts)
            verify(self.db, self.config, self.post, self.facts)
        read.assert_called_once(); check.assert_called_once(); analyze.assert_not_called()
        self.assertEqual(self.db.execute('SELECT status FROM posts').fetchone()[0], 'PENDING')
