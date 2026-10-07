import copy
import json
import io
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from newsroom.core import run_cycle, process_item, _save_item
from newsroom.db import connect, connect_readonly
from newsroom.runtime import BudgetDeferred, Runtime, SCOPE, attach, cache_key, snapshot as usage_snapshot
from newsroom.workflow import Coordinator, Work, enqueue, snapshot


class WorkflowTests(unittest.TestCase):
    def test_cycle_failure_keeps_safe_diagnostic_after_cleanup(self):
        with patch('newsroom.core._run_cycle', side_effect=RuntimeError('private credential')):
            with self.assertRaises(RuntimeError):
                run_cycle(self.config)
        stored = self.db.execute("SELECT value FROM app_state WHERE key='diagnostic_last_error'").fetchone()[0]
        self.assertEqual(json.loads(stored)['code'], 'RuntimeError')
        self.assertNotIn('private credential', stored)

    def test_post_writing_runs_only_after_analysis_gates(self):
        for approved in (False, True):
            with self.subTest(approved=approved):
                item = self.item(100 + int(approved))
                result = self.publish_result(item) if approved else self.noise()
                result['_needs_post_draft'] = True
                draft = {key: result.get(key, '') for key in ('headline_ru', 'summary_ru', 'what_is_new', 'editorial_check')}
                if approved:
                    result['headline_ru'] = result['summary_ru'] = ''
                with patch('newsroom.core.get_api_key', return_value='test'), \
                     patch('newsroom.core.analyze_with_ai', return_value=result), \
                     patch('newsroom.ai.draft_post', return_value=draft) as writer:
                    outcome = process_item(self.db, self.source, item, .35, 3500, 24, ai_settings=self.config['ai'])
                self.assertEqual(writer.call_count, int(approved))
                self.assertEqual(outcome, 'NEW_STORY' if approved else 'NOISE')
                if approved:
                    saved = self.db.execute('SELECT text FROM posts ORDER BY post_id DESC LIMIT 1').fetchone()[0]
                    self.assertIn(draft['headline_ru'], saved)

    def test_history_revision_ignores_unrelated_story_updates(self):
        from newsroom.core import _editor_history_revision
        now = datetime.now(timezone.utc).isoformat()
        self.db.execute("INSERT INTO stories(canonical_topic,headline,first_seen_at,last_updated_at,latest_information) VALUES(?,?,?,?,?)",
                        ('Цифровой рубль', 'Банк России открыл счета цифрового рубля', now, now, '220 тысяч счетов цифрового рубля'))
        related_id = self.db.execute('SELECT last_insert_rowid()').fetchone()[0]
        self.db.execute("INSERT INTO stories(canonical_topic,headline,first_seen_at,last_updated_at,latest_information) VALUES(?,?,?,?,?)",
                        ('Футбол', 'Команда выиграла футбольный матч', now, now, 'Спортсмены забили гол'))
        unrelated_id = self.db.execute('SELECT last_insert_rowid()').fetchone()[0]
        item = {'title': 'Банк России открыл 220 тысяч счетов цифрового рубля'}
        before = _editor_history_revision(self.db, item)
        self.db.execute('UPDATE stories SET version=version+1 WHERE story_id=?', (unrelated_id,))
        self.assertEqual(before, _editor_history_revision(self.db, item))
        self.db.execute('UPDATE stories SET version=version+1 WHERE story_id=?', (related_id,))
        self.assertNotEqual(before, _editor_history_revision(self.db, item))

    def test_inconclusive_editor_output_is_not_cached_even_from_legacy_cache(self):
        runtime = self.config['ai']['_runtime']
        key = cache_key('test', 'inconclusive-editor')
        result = {'action': 'NEW_STORY', 'publication_recommendation': 'WAIT_FOR_AUTOMATION'}
        runtime.store(key, 'editor', result, 60)
        with patch('newsroom.core.analyze_with_ai', return_value=result) as analyze:
            work = Work('editor', analyze, key=key, ttl=60)
            self.assertEqual(work.execute(runtime), result)
            self.assertEqual(work.execute(runtime), result)
        self.assertEqual(analyze.call_count, 2)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM cache_events').fetchone()[0], 0)

    def test_fresh_timeout_does_not_disable_reserved_editor_retry(self):
        from newsroom.ai import AIResponseError
        held = self.item(1)
        fresh = self.item(2)
        enqueue(self.db, None, held, self.source, self.options, category='retry')
        enqueue(self.db, None, fresh, self.source, self.options)
        self.db.execute("UPDATE items SET disposition='WAITING_CONFIRMATION' WHERE item_id=1")
        self.db.commit()
        seen = []
        def analyze(item, *args):
            seen.append(item['url'])
            if item['url'] == fresh['url'] and seen.count(fresh['url']) == 1:
                raise AIResponseError('NETWORK_TIMEOUT')
            return self.noise()
        with patch('newsroom.core.get_api_key', return_value='test'), \
             patch('newsroom.core.fetch_rss', return_value=[]), \
             patch('newsroom.core.analyze_with_ai', side_effect=analyze):
            run_cycle(self.config)
        self.assertEqual(seen[0], fresh['url'])
        self.assertIn(held['url'], seen[1:])
        self.assertEqual(self.db.execute('SELECT disposition FROM items WHERE item_id=1').fetchone()[0], 'NOISE')

    def test_budget_deferral_does_not_consume_editor_retry_attempt(self):
        enqueue(self.db, None, self.item(), self.source, self.options, category='retry')
        self.db.execute("UPDATE items SET disposition='WAITING_CONFIRMATION'")
        self.db.execute("INSERT INTO app_state(key,value) VALUES('editor_retry:1','2')")
        self.db.commit()
        self.config['ai']['triage_enabled'] = True
        deferred = {'decision': 'DEFER', 'reason': 'Бюджет исчерпан', 'retry_without_count': True, 'budget_deferred': True}
        with patch('newsroom.core.screen_item', return_value=deferred), \
             patch('newsroom.core.analyze_with_ai') as editor:
            Coordinator(self.db, self.config, {}, categories=('retry',), max_jobs=1).close()
        editor.assert_not_called()
        self.assertEqual(self.db.execute("SELECT value FROM app_state WHERE key='editor_retry:1'").fetchone()[0], '2')
        self.assertEqual(self.db.execute('SELECT disposition FROM items').fetchone()[0], 'AI_RETRY')
        self.assertEqual(self.db.execute("SELECT status FROM processing_jobs").fetchone()[0], 'WAITING')

    def test_rotated_out_entity_material_is_processed_without_fetching_its_feed(self):
        enqueue(self.db, None, self.item(), self.source, self.options)
        self.db.execute('UPDATE sources SET active=0')
        self.db.commit()
        self.config['sources'] = []
        self.config['_registry_processing_urls'] = [self.source['url']]
        with patch('newsroom.core.get_api_key', return_value='test'), \
             patch('newsroom.core.analyze_with_ai', side_effect=self.noise) as editor, \
             patch('newsroom.core.fetch_rss') as fetch:
            counts = run_cycle(self.config)
        self.assertEqual(counts['NOISE'], 1)
        editor.assert_called_once()
        fetch.assert_not_called()
        self.assertEqual(self.db.execute('SELECT status FROM processing_jobs').fetchone()[0], 'DONE')

    def test_retry_lane_alternates_editor_confirmation_with_oldest_work(self):
        first = self.item(1, title='Первичный отбор старого материала')
        second = self.item(2, title='Подтверждение свежего решения регулятора')
        for item in (first, second):
            enqueue(self.db, None, item, self.source, self.options, category='retry')
        self.db.execute("UPDATE items SET disposition=CASE item_id WHEN 1 THEN 'AI_RETRY' ELSE 'WAITING_CONFIRMATION' END")
        self.db.execute("UPDATE processing_jobs SET created_at='2000-01-01T00:00:00+00:00',next_at='2000-01-01T00:00:00+00:00' WHERE item_id=1")
        self.db.commit()
        coordinator = Coordinator(self.db, self.config, {}, categories=('retry',), max_jobs=2)
        try:
            self.assertEqual(coordinator._claim()['item_id'], 2)
            with patch('newsroom.core.similarity', return_value=0):
                self.assertEqual(coordinator._claim()['item_id'], 1)
        finally:
            coordinator.pool.shutdown()

    def test_item_error_does_not_disable_other_editor_jobs(self):
        self.config['newsroom']['processing_workers'] = 1
        for n in (1, 2):
            enqueue(self.db, None, self.item(n), self.source, self.options)
        def analyze(item, *args):
            if item['url'].endswith('/1'):
                raise TypeError('item-specific error')
            return self.noise()
        with patch('newsroom.core.get_api_key', return_value='test'), \
             patch('newsroom.core.analyze_with_ai', side_effect=analyze) as editor:
            Coordinator(self.db, self.config, {}).close()
        self.assertEqual(editor.call_count, 2)
        rows = self.db.execute('SELECT disposition FROM items ORDER BY item_id').fetchall()
        self.assertEqual([r[0] for r in rows], ['TECHNICAL_ERROR', 'NOISE'])

    def test_retry_rotation_serves_reading_analysis_and_confirmation(self):
        dispositions = ['AI_RETRY', 'WAITING_CONFIRMATION', 'PRIMARY_RETRY', 'AI_RETRY']
        for n, disposition in enumerate(dispositions, 1):
            enqueue(self.db, None, self.item(n), self.source, self.options, category='retry')
            self.db.execute('UPDATE items SET disposition=? WHERE item_id=?', (disposition, n))
        self.db.commit()
        coordinator = Coordinator(self.db, self.config, {}, categories=('retry',), max_jobs=4)
        try:
            with patch('newsroom.core.similarity', return_value=0):
                claimed = [coordinator._claim()['item_id'] for _ in range(4)]
            self.assertEqual(claimed, [2, 1, 3, 4])
        finally:
            coordinator.pool.shutdown()

    def test_restart_resumes_drafting_without_reanalysis_or_second_story(self):
        from newsroom.ai import AIResponseError
        item = self.item()
        result = self.publish_result(item)
        result['_needs_post_draft'] = True
        draft = {key: result.get(key, '') for key in ('headline_ru', 'summary_ru', 'what_is_new', 'editorial_check')}
        result['headline_ru'] = result['summary_ru'] = ''
        self.config['ai']['api_key'] = 'must-not-enter-checkpoint'
        with patch('newsroom.core.get_api_key', return_value='test'), \
             patch('newsroom.core.analyze_with_ai', return_value=result) as editor, \
             patch('newsroom.ai.draft_post', side_effect=[AIResponseError('NETWORK_TIMEOUT'), draft]) as writer:
            first = process_item(self.db, self.source, item, .35, 3500, 24, ai_settings=self.config['ai'])
            self.assertEqual(first, 'AI_RETRY')
            self.db.close()
            self.db = connect(self.path)
            from newsroom.core import _saved_material
            saved = _saved_material(self.db.execute('SELECT * FROM items').fetchone(), self.source)
            second = process_item(self.db, self.source, saved, .35, 3500, 24,
                                  ai_settings=self.config['ai'], existing_item_id=1)
        self.assertEqual(second, 'NEW_STORY')
        self.assertEqual(editor.call_count, 1)
        self.assertEqual(writer.call_count, 2)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM stories').fetchone()[0], 1)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM posts').fetchone()[0], 1)
        artifact = self.db.execute('SELECT result_json FROM material_stage_results').fetchone()[0]
        self.assertNotIn('must-not-enter-checkpoint', artifact)
        self.assertNotIn('api_key', artifact)

    def test_checkpoint_invalidation_on_changed_rules_or_material(self):
        from newsroom.material_flow import save_draft_context, load_draft_context
        enqueue(self.db, None, self.item(), self.source, self.options)
        save_draft_context(self.db, 1, self.item(), self.config['ai'], {'approved': True})
        self.assertIsNotNone(load_draft_context(self.db, 1, self.item(), self.config['ai']))
        changed = dict(self.config['ai'], model='changed-model')
        self.assertIsNone(load_draft_context(self.db, 1, self.item(), changed))
        self.db.execute("UPDATE items SET content_hash='new-version' WHERE item_id=1")
        self.assertIsNone(load_draft_context(self.db, 1, self.item(), self.config['ai']))

    def setUp(self):
        from policy_fixtures import final_check
        checker = patch('newsroom.ai.validate_draft', side_effect=final_check)
        checker.start()
        self.addCleanup(checker.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = str(Path(self.temp.name) / 'newsroom.db')
        self.db = connect(self.path)
        self.addCleanup(self.db.close)
        self.db.execute("INSERT INTO sources(name,type,url) VALUES('Test','rss','https://example.org/feed')")
        self.db.commit()
        self.source = dict(self.db.execute('SELECT * FROM sources').fetchone())
        self.now = datetime.now(timezone.utc).isoformat(timespec='seconds')
        self.config = {'newsroom': {'database': self.path, 'processing_workers': 2}, 'ai': {'model': 'test'},
                       'sources': [{'name': 'Test', 'type': 'rss', 'url': self.source['url']}]}
        attach(self.config)
        self.options = {'threshold': .35, 'max_length': 3500, 'freshness_hours': 24,
                        'initial_backfill_minutes': None, 'relevance_terms': []}

    def item(self, number=1, title=None):
        text = f'Банк России установил условия доступа российских участников к цифровым активам {self.now[:10]}. Условие {number}.'
        return {'url': f'https://example.org/{number}', 'title': title or f'Цифровые активы: условие {number}',
                'content': text, 'description': text, 'published_at': self.now, 'material_read': True,
                'primary_source_status': 'READ', 'primary_source_url': f'https://www.cbr.ru/{number}',
                'primary_source_content': text, 'primary_source_title': 'Условия доступа', 'primary_source_type': 'OFFICIAL'}

    def noise(self, *args):
        return {'action': 'NOISE', 'is_relevant': False, 'publication_recommendation': 'DO_NOT_PUBLISH'}

    def publish_result(self, item):
        quote = item['primary_source_content']
        return {'action': 'NEW_STORY', 'is_relevant': True, 'geographic_scope': 'RUSSIA', 'confidence': .9,
                'russia_cis_impact': 'DIRECT', 'impact_evidence': quote, 'topic_category': 'REGULATION_SANCTIONS',
                'importance': 'HIGH', 'development_date': self.now[:10], 'development_date_evidence': quote,
                'editorial_check': {'source_matches_event': True, 'attribution_preserved': True, 'stage_preserved': True,
                    'history_required': False, 'history_explained': False, 'history_note': '', 'headline_main_event': True,
                    'lead_event_first': True, 'paragraphs_concise_distinct': True, 'no_editorial_process_notes': True},
                'headline_ru': '🇷🇺 Банк России установил условия доступа к цифровым активам', 'summary_ru': quote,
                'publication_recommendation': 'AUTO_PUBLISH', 'independent_check': 'NO_MATCH',
                'facts': [{'text': quote, 'claim_type': 'FACT'}]}

    def test_three_roles_reach_the_existing_gate_and_saved_telegram_receipt(self):
        from newsroom.cli import run_one_cycle
        from newsroom.delivery import TelegramReceipt
        article = self.item()
        discovered = {key: value for key, value in article.items() if not key.startswith('primary_source_')}
        discovered.pop('material_read')
        self.config['ai']['triage_enabled'] = True
        self.config['newsroom']['auto_publish_since'] = self.now
        self.config['telegram'] = {'chat_id': '@workflow_test', 'chat_id_env': 'WORKFLOW_TEST_UNUSED'}
        selection = {'decision': 'KEEP', 'reason': 'Значимое новое условие', 'story_id': '', 'confidence': .95}
        with patch('newsroom.core.fetch_rss', return_value=[discovered]), \
             patch('newsroom.triage.classify', return_value=selection), \
             patch('newsroom.core.fetch_publisher_article', return_value=article), \
             patch('newsroom.core.get_api_key', return_value='test'), \
             patch('newsroom.core.analyze_with_ai', return_value=self.publish_result(article)), \
             patch('newsroom.cli.publish_digest', return_value=(False, 0)), \
             patch('newsroom.cli.telegram_send', return_value=TelegramReceipt({'message_id': 100})) as send:
            counts = run_one_cycle(self.config, self.path)
            self.assertEqual(counts['AUTO_PUBLISHED'], 1)
            run_one_cycle(self.config, self.path)
        self.assertEqual(send.call_count, 1)
        self.assertEqual(tuple(self.db.execute('SELECT status,external_id FROM posts').fetchone()), ('PUBLISHED', '100'))
        self.assertEqual(self.db.execute('SELECT status FROM publication_attempts').fetchone()[0], 'CONFIRMED')
        self.assertEqual(json.loads(self.db.execute('SELECT telegram_response_json FROM publication_attempts').fetchone()[0])['message_id'], 100)
        roles = {row[0] for row in self.db.execute('SELECT role FROM processing_job_events')}
        self.assertEqual(roles, {'collector', 'editor'})

    def test_related_documents_wait_for_each_other_before_spending_on_analysis(self):
        first = self.item(1)
        second = self.item(2)
        second['primary_source_url'] = first['primary_source_url']
        enqueue(self.db, None, first, self.source, self.options)
        enqueue(self.db, None, second, self.source, self.options)
        coordinator = Coordinator(self.db, self.config, {})
        try:
            self.assertIsNotNone(coordinator._claim())
            self.assertIsNone(coordinator._claim())
        finally:
            coordinator.pool.shutdown()

    def test_changed_story_history_cannot_apply_a_stale_editor_decision(self):
        enqueue(self.db, None, self.item(), self.source, self.options)
        ready, release = threading.Event(), threading.Event()
        def analyze(*args):
            ready.set()
            self.assertTrue(release.wait(3))
            return self.publish_result(self.item())
        coordinator = Coordinator(self.db, self.config, {}, max_jobs=1)
        with patch('newsroom.core.get_api_key', return_value='test'), patch('newsroom.core.analyze_with_ai', side_effect=analyze):
            coordinator.tick()
            self.assertTrue(ready.wait(3))
            self.db.execute("INSERT INTO stories(canonical_topic,headline,first_seen_at,last_updated_at,latest_information) VALUES(?,?,?,?,?)",
                            (self.item()['title'], self.item()['title'], self.now, self.now, self.item()['content']))
            self.db.commit()
            release.set()
            coordinator.close()
        self.assertEqual(self.db.execute('SELECT disposition FROM items').fetchone()[0], 'AI_RETRY')
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM posts').fetchone()[0], 0)
        state = json.loads(self.db.execute("SELECT value FROM app_state WHERE key='selection_retry:1'").fetchone()[0])
        self.assertEqual(state['attempts'], 0)

    def test_parallel_mode_reduces_wait_without_more_api_calls_or_tokens(self):
        from newsroom.ai import request_response
        samples = []
        headlines = ['Минфин предложил налоговую льготу для майнинга', 'AFSA выдала лицензию криптобирже',
                     'Банк запустил трансграничные платежи', 'Разработчик выпустил аппаратный кошелёк']
        items = []
        for number, title in enumerate(headlines, 1):
            item = self.item(number, title)
            item['description'] = title
            items.append(item)
        class Response(io.BytesIO):
            def __enter__(self):
                return self
            def __exit__(self, *args):
                self.close()
        def transport(*args, **kwargs):
            time.sleep(.04)
            return Response(json.dumps({'usage': {'input_tokens': 100, 'input_tokens_details': {'cached_tokens': 0},
                                                  'output_tokens': 10}, 'output': []}).encode())
        def editor(item, source, candidates, settings):
            request_response({'model': 'test'}, settings)
            return self.noise()
        for workers in (1, 2):
            path = str(Path(self.temp.name) / f'benchmark-{workers}.db')
            database = connect(path)
            database.close()
            config = {'newsroom': {'database': path, 'processing_workers': workers},
                      'sources': self.config['sources'], 'ai': {'model': 'test'}}
            with patch('newsroom.core.fetch_rss', return_value=copy.deepcopy(items)), \
                 patch('newsroom.core.get_api_key', return_value='test'), patch('newsroom.ai.get_api_key', return_value='test'), \
                 patch('newsroom.core.analyze_with_ai', side_effect=editor), \
                 patch('newsroom.ai.urllib.request.urlopen', side_effect=transport):
                counts = run_cycle(config)
            database = connect_readonly(path)
            try:
                self.assertEqual(counts['NOISE'], 4)
                self.assertEqual(snapshot(database)['unfinished'], 0)
                samples.append((snapshot(database)['wait_p95_seconds'], usage_snapshot(database)['calls'],
                                database.execute('SELECT SUM(input_tokens),SUM(output_tokens) FROM api_usage').fetchone()))
            finally:
                database.close()
        self.assertEqual(samples[0][1], samples[1][1])
        self.assertEqual(tuple(samples[0][2]), tuple(samples[1][2]))
        self.assertLess(samples[1][0], samples[0][0] * .85)

    def test_two_editors_run_concurrently_and_capacity_stays_two(self):
        entered = threading.Barrier(2)
        lock = threading.Lock()
        active, maximum = [0], [0]
        def analyze(*args):
            with lock:
                active[0] += 1
                maximum[0] = max(maximum[0], active[0])
            entered.wait(timeout=3)
            with lock:
                active[0] -= 1
            return self.noise()
        second = self.item(2, 'Майнер запустил промышленный комплекс в Казахстане')
        second.update(description='В Караганде введён в эксплуатацию майнинговый комплекс.',
                      content='В Караганде промышленный майнер запустил производство и подключился к электросети.')
        with patch('newsroom.core.fetch_rss', return_value=[self.item(1), second]), \
             patch('newsroom.core.get_api_key', return_value='test'), \
             patch('newsroom.core.analyze_with_ai', side_effect=analyze):
            result = run_cycle(self.config)
        self.assertEqual(result['NOISE'], 2)
        self.assertEqual(maximum[0], 2)
        self.assertEqual(snapshot(self.db)['unfinished'], 0)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM posts').fetchone()[0], 0)

    def test_collection_saves_next_source_while_an_editor_is_waiting(self):
        started, release = threading.Event(), threading.Event()
        self.config['sources'].append({'name': 'Second', 'type': 'rss', 'url': 'https://second.example/feed'})
        def fetch(url):
            if 'second.example' in url:
                self.assertTrue(started.wait(3))
                return [self.item(2)]
            return [self.item(1)]
        def analyze(item, *args):
            if item['url'].endswith('/1'):
                started.set()
                self.assertTrue(release.wait(3))
            return self.noise()
        errors = []
        def run():
            try:
                run_cycle(self.config)
            except Exception as exc:
                errors.append(exc)
        with patch('newsroom.core.fetch_rss', side_effect=fetch), patch('newsroom.core.get_api_key', return_value='test'), \
             patch('newsroom.core.analyze_with_ai', side_effect=analyze):
            worker = threading.Thread(target=run)
            worker.start()
            try:
                self.assertTrue(started.wait(3))
                deadline = time.monotonic() + 2
                saved = False
                while time.monotonic() < deadline:
                    saved = self.db.execute("SELECT COUNT(*) FROM items WHERE url='https://example.org/2'").fetchone()[0] == 1
                    if saved:
                        break
                    time.sleep(.01)
                self.assertTrue(saved, 'Second source must be saved before the first editor returns')
            finally:
                release.set()
                worker.join(4)
        self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])

    def test_recovery_repairs_checkpoints_but_preserves_active_lease_and_old_version(self):
        from newsroom.material_flow import mark, revision
        enqueue(self.db, None, self.item(), self.source, self.options)
        mark(self.db, 1, 'analysis', 'RUNNING')
        self.db.execute("UPDATE processing_jobs SET status='RUNNING',owner='dead',lease_until='2000-01-01'")
        self.db.commit()
        coordinator = Coordinator(self.db, self.config, {}, max_jobs=0)
        self.addCleanup(coordinator.close)
        row = self.db.execute("SELECT status,block_kind FROM material_stage_state WHERE stage='analysis'").fetchone()
        self.assertEqual(tuple(row), ('READY', 'recovery'))
        mark(self.db, 1, 'analysis', 'RUNNING')
        self.db.execute("UPDATE processing_jobs SET status='RUNNING',owner='live',lease_until='2099-01-01'")
        self.db.commit()
        coordinator._resume_expired()
        self.assertEqual(self.db.execute("SELECT status FROM material_stage_state WHERE stage='analysis'").fetchone()[0], 'RUNNING')
        old_revision = revision(self.db, 1)
        self.db.execute("UPDATE items SET ingest_revision='new-version',disposition='REJECTED',processed_at=?", (self.now,))
        mark(self.db, 1, 'drafting', 'WAITING', 'Old wait')
        self.db.commit()
        coordinator._resume_expired()
        self.assertEqual(self.db.execute("SELECT status FROM material_stage_state WHERE revision=? AND stage='analysis'", (old_revision,)).fetchone()[0], 'RUNNING')
        self.assertEqual(self.db.execute("SELECT status FROM material_stage_state WHERE revision='new-version' AND stage='drafting'").fetchone()[0], 'CLOSED')

    def test_rejected_post_closes_only_its_current_gate(self):
        from newsroom.material_flow import mark
        item = self.item(904)
        with patch('newsroom.core.get_api_key', return_value='test'), \
             patch('newsroom.core.analyze_with_ai', return_value=self.publish_result(item)):
            self.assertEqual(process_item(self.db, self.source, item, .35, 3500, 24, ai_settings=self.config['ai']), 'NEW_STORY')
        self.db.execute("UPDATE posts SET status='REJECTED'")
        self.db.commit()
        coordinator = Coordinator(self.db, self.config, {}, max_jobs=0)
        self.addCleanup(coordinator.close)
        self.assertEqual(self.db.execute("SELECT status FROM material_stage_state WHERE stage='gate'").fetchone()[0], 'CLOSED')
        self.db.execute("UPDATE items SET ingest_revision='revised',processed_at='2099-01-01'")
        mark(self.db, 1, 'gate', 'READY')
        self.db.commit()
        coordinator._resume_expired()
        self.assertEqual(self.db.execute("SELECT status FROM material_stage_state WHERE stage='gate' AND revision='revised'").fetchone()[0], 'READY')

    def test_draft_token_limit_retries_only_writing_with_more_room(self):
        from newsroom.ai import AIResponseError
        item = self.item(920)
        decision = self.publish_result(item)
        draft = {key: decision.get(key, '') for key in ('headline_ru','summary_ru','what_is_new','editorial_check')}
        decision['_needs_post_draft'] = True
        decision['headline_ru'] = decision['summary_ru'] = ''
        with patch('newsroom.core.get_api_key', return_value='test'), \
             patch('newsroom.core.analyze_with_ai', return_value=decision) as analyze, \
             patch('newsroom.ai.draft_post', side_effect=[AIResponseError('OUTPUT_TOKEN_LIMIT'), draft]) as writer:
            self.assertEqual(process_item(self.db, self.source, item, .35, 3500, 24, ai_settings=self.config['ai']), 'NEW_STORY')
        self.assertEqual(analyze.call_count, 1)
        self.assertEqual(writer.call_count, 2)
        self.assertGreaterEqual(writer.call_args_list[1].args[2]['max_output_tokens'], 8000)
        self.assertEqual(self.db.execute('SELECT count(*) FROM posts').fetchone()[0], 1)

    def test_draft_incomplete_response_reports_token_limit(self):
        from newsroom.ai import draft_post, AIResponseError
        response = {'status': 'incomplete', 'incomplete_details': {'reason': 'max_output_tokens'}}
        with patch('newsroom.ai.request_response', return_value=response) as request:
            with self.assertRaises(AIResponseError) as caught:
                draft_post({}, {}, {})
        self.assertEqual(caught.exception.code, 'OUTPUT_TOKEN_LIMIT')
        self.assertGreaterEqual(request.call_args.args[0]['max_output_tokens'], 5000)

    def test_draft_request_is_accounted_as_drafting(self):
        from newsroom.ai import draft_post, SCHEMA
        draft = {'headline_ru': 'Банк открыл счета', 'summary_ru': 'Банк открыл счета цифрового рубля.',
                 'what_is_new': '', 'editorial_check': {key: '' if key == 'history_note' else False for key in SCHEMA['properties']['editorial_check']['required']}}
        response = {'output': [{'content': [{'type': 'output_text', 'text': json.dumps(draft)}]}]}
        with patch('newsroom.ai.request_response', return_value=response) as request:
            contract = {'text_field': 'summary_ru', 'has_previous_publication': False,
                        'required_fact_quotes': ['Банк открыл счета цифрового рубля.']}
            old_audit = {'history_required': True, 'history_note': 'No previous link was supplied'}
            self.assertEqual(draft_post({'editorial_check': old_audit, 'summary_ru': 'Old draft', 'facts': []},
                                        {}, {'_draft_contract': contract}), draft)
        self.assertEqual(request.call_args.args[1]['_work_stage'], 'drafting')
        content = json.loads(request.call_args.args[0]['input'])
        self.assertEqual(content['draft_contract'], contract)
        self.assertNotIn('editorial_check', content['checked_decision'])
        self.assertNotIn('summary_ru', content['checked_decision'])
        self.assertEqual(content['previous_draft']['editorial_check'], old_audit)
        self.assertEqual(content['previous_draft']['summary_ru'], 'Old draft')
        self.assertIn('своими словами', request.call_args.args[0]['instructions'])

    def test_existing_retry_job_accepts_fresh_completion_without_unique_conflict(self):
        for fails in (False, True):
            with self.subTest(fails=fails):
                item = self.item(920 + int(fails))
                enqueue(self.db, None, item, self.source, self.options)
                item_id = self.db.execute('SELECT item_id FROM items WHERE url=?', (item['url'],)).fetchone()[0]
                enqueue(self.db, item_id, item, self.source, self.options, category='retry')
                self.db.execute("UPDATE processing_jobs SET status='DONE',attempts=2 WHERE item_id=? AND category='retry'", (item_id,))
                self.db.commit()
                coordinator = Coordinator(self.db, self.config, {}, max_jobs=1)
                self.addCleanup(coordinator.close)
                job = coordinator._claim()
                self.assertEqual(job['category'], 'fresh')
                def finish():
                    if fails:
                        raise RuntimeError('transport failed')
                    return 'WAITING_CONFIRMATION'
                    yield
                coordinator._advance(job, finish())
                rows = self.db.execute('SELECT category,status,attempts FROM processing_jobs WHERE item_id=? ORDER BY category', (item_id,)).fetchall()
                expected = [('fresh', 'DONE'), ('retry', 'DONE')] if fails else [('fresh', 'SUPERSEDED'), ('retry', 'WAITING')]
                self.assertEqual([(r['category'], r['status']) for r in rows], expected)
                self.assertEqual(rows[1]['attempts'], 2)
                self.assertEqual(self.db.execute('SELECT count(*) FROM processing_job_events WHERE job_id=?', (job['job_id'],)).fetchone()[0], 1 if fails else 2)
                self.db.execute("UPDATE processing_jobs SET status='DONE' WHERE item_id=?", (item_id,))
                self.db.commit()

    def test_local_coordinator_type_error_is_terminal_without_retry(self):
        enqueue(self.db, None, self.item(), self.source, self.options)
        counts = {}
        coordinator = Coordinator(self.db, self.config, counts, max_jobs=1)
        self.addCleanup(coordinator.close)
        job = coordinator._claim()
        def broken():
            raise TypeError('private diagnostic detail')
            yield
        coordinator._advance(job, broken())
        self.assertEqual(counts, {'TECHNICAL_ERROR': 1})
        self.assertEqual(self.db.execute('SELECT disposition FROM items').fetchone()[0], 'TECHNICAL_ERROR')
        self.assertEqual(tuple(self.db.execute('SELECT status,outcome FROM processing_jobs').fetchone()), ('DONE', 'TECHNICAL_ERROR'))
        self.assertIsNone(self.db.execute("SELECT value FROM app_state WHERE key='selection_retry:1'").fetchone())
        checkpoint = self.db.execute("SELECT status,reason FROM material_stage_state WHERE stage='screening'").fetchone()
        self.assertEqual(checkpoint['status'], 'ERROR')
        self.assertNotIn('private', checkpoint['reason'])

    def test_drafting_retry_receives_priority_over_older_screening_retry(self):
        from newsroom.material_flow import mark
        enqueue(self.db, None, self.item(910), self.source, self.options, category='retry')
        enqueue(self.db, None, self.item(911), self.source, self.options, category='retry')
        mark(self.db, 1, 'screening', 'WAITING', 'Old screening')
        mark(self.db, 2, 'drafting', 'WAITING', 'Ready to write')
        self.db.execute("UPDATE processing_jobs SET next_at='2000-01-01'")
        self.db.commit()
        coordinator = Coordinator(self.db, {**self.config, '_continuous_processing': True}, {}, categories=('fresh','retry','watch'))
        coordinator.claimed = 1  # The retry slot must also prefer drafting.
        try:
            self.assertEqual(coordinator._claim()['item_id'], 2)
        finally:
            coordinator.max_jobs = 0
            coordinator.close()

    def test_expired_owner_recovers_without_duplicate_job(self):
        enqueue(self.db, None, self.item(), self.source, self.options)
        self.db.execute("UPDATE processing_jobs SET status='RUNNING',owner='dead',lease_until='2000-01-01'")
        self.db.commit()
        counts = {}
        with patch('newsroom.core.get_api_key', return_value='test'), patch('newsroom.core.analyze_with_ai', side_effect=self.noise):
            Coordinator(self.db, self.config, counts).close()
        self.assertEqual(counts, {'NOISE': 1})
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM processing_jobs').fetchone()[0], 1)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM processing_job_events WHERE status='LEASE_EXPIRED'").fetchone()[0], 1)

    def test_an_active_lease_cannot_be_claimed_by_another_coordinator(self):
        enqueue(self.db, None, self.item(), self.source, self.options)
        first = Coordinator(self.db, self.config, {})
        second = Coordinator(self.db, self.config, {})
        try:
            claimed = first._claim()
            self.assertIsNotNone(claimed)
            self.assertIsNone(second._claim())
        finally:
            first.pool.shutdown()
            second.pool.shutdown()

    def test_changed_material_discards_inflight_result(self):
        enqueue(self.db, None, self.item(), self.source, self.options)
        ready, release = threading.Event(), threading.Event()
        def analyze(*args):
            ready.set()
            self.assertTrue(release.wait(3))
            return self.noise()
        counts = {}
        coordinator = Coordinator(self.db, self.config, counts, max_jobs=1)
        with patch('newsroom.core.get_api_key', return_value='test'), patch('newsroom.core.analyze_with_ai', side_effect=analyze):
            coordinator.tick()
            self.assertTrue(ready.wait(3))
            revised = self.item()
            revised['content'] += ' Новый существенный факт.'
            revised['title'] = 'Изменённые условия'
            enqueue(self.db, None, revised, self.source, self.options)
            release.set()
            coordinator.close()
        self.assertEqual(counts, {})
        row = self.db.execute('SELECT title,disposition FROM items').fetchone()
        self.assertEqual(tuple(row), ('Изменённые условия', 'PENDING'))
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM item_analysis').fetchone()[0], 0)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM processing_jobs WHERE status='SUPERSEDED'").fetchone()[0], 1)

    def test_budget_deferral_keeps_job_and_does_not_consume_retry(self):
        enqueue(self.db, None, self.item(), self.source, self.options)
        with patch('newsroom.core.get_api_key', return_value='test'), \
             patch('newsroom.core.analyze_with_ai', side_effect=BudgetDeferred()):
            counts = {}
            Coordinator(self.db, self.config, counts).close()
        self.assertEqual(counts, {'AI_RETRY': 1})
        state = json.loads(self.db.execute("SELECT value FROM app_state WHERE key='selection_retry:1'").fetchone()[0])
        self.assertEqual(state['attempts'], 0)
        self.assertEqual(snapshot(self.db)['waiting'], 1)

    def test_successful_editor_result_is_cached_but_model_change_invalidates_it(self):
        item_id = _save_item(self.db, self.source, self.item())
        self.db.commit()
        with patch('newsroom.core.get_api_key', return_value='test'), patch('newsroom.core.analyze_with_ai', side_effect=self.noise) as analyze:
            for _ in range(2):
                self.assertEqual(process_item(self.db, self.source, self.item(), **self.options,
                    ai_settings=self.config['ai'], existing_item_id=item_id), 'NOISE')
            self.assertEqual(analyze.call_count, 1)
            self.config['ai']['model'] = 'changed-model'
            process_item(self.db, self.source, self.item(), **self.options, ai_settings=self.config['ai'], existing_item_id=item_id)
            self.assertEqual(analyze.call_count, 2)

    def test_errors_are_never_cached(self):
        calls = []
        def fail():
            calls.append(1)
            raise ValueError('bad output')
        work = Work('editor', fail, key=cache_key('test', 1), ttl=60)
        for _ in range(2):
            with self.assertRaises(ValueError):
                work.execute(self.config['ai']['_runtime'])
        self.assertEqual(len(calls), 2)

    def test_read_only_metrics_include_running_and_waiting_jobs(self):
        enqueue(self.db, None, self.item(1), self.source, self.options)
        enqueue(self.db, None, self.item(2), self.source, self.options)
        self.db.execute("UPDATE processing_jobs SET status='RUNNING' WHERE job_id=1")
        self.db.execute("UPDATE processing_jobs SET status='WAITING' WHERE job_id=2")
        self.db.commit()
        readonly = connect_readonly(self.path)
        try:
            report = snapshot(readonly)
            self.assertEqual(report['unfinished'], 2)
            self.assertEqual(report['running'], 1)
            self.assertEqual(report['waiting'], 1)
            self.assertIsNotNone(report['oldest_seconds'])
        finally:
            readonly.close()

    def test_malformed_provider_output_uses_technical_retries_without_rejecting_story(self):
        from newsroom.ai import AIResponseError
        with patch('newsroom.core.fetch_rss', return_value=[self.item()]), \
             patch('newsroom.core.get_api_key', return_value='test'), \
             patch('newsroom.core.analyze_with_ai', side_effect=AIResponseError('INVALID_STRUCTURED_OUTPUT_JSON')):
            for attempt in range(4):
                run_cycle(self.config)
                state = json.loads(self.db.execute("SELECT value FROM app_state WHERE key='selection_retry:1'").fetchone()[0])
                self.assertEqual(state['attempts'], 0)
                if attempt < 3:
                    self.db.execute("UPDATE processing_jobs SET next_at='2000-01-01' WHERE status='WAITING'")
                    self.db.commit()
        self.assertEqual(self.db.execute('SELECT disposition FROM items').fetchone()[0], 'TECHNICAL_ERROR')
        self.assertEqual(snapshot(self.db)['unfinished'], 0)

    def test_legacy_pending_material_is_seeded_into_the_durable_queue(self):
        _save_item(self.db, self.source, self.item())
        self.db.commit()
        counts = {}
        with patch('newsroom.core.get_api_key', return_value='test'), patch('newsroom.core.analyze_with_ai', side_effect=self.noise):
            Coordinator(self.db, self.config, counts).close()
        self.assertEqual(counts, {'NOISE': 1})
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM processing_jobs').fetchone()[0], 1)

    def test_disabling_source_monitoring_does_not_abandon_accepted_material(self):
        _save_item(self.db, self.source, self.item())
        self.db.execute('UPDATE sources SET active=0 WHERE source_id=?', (self.source['source_id'],))
        self.db.commit()
        counts = {}
        with patch('newsroom.core.get_api_key', return_value='test'), patch('newsroom.core.analyze_with_ai', side_effect=self.noise):
            Coordinator(self.db, self.config, counts).close()
        self.assertEqual(counts, {'NOISE': 1})
        self.assertEqual(snapshot(self.db)['unfinished'], 0)
        self.assertEqual(self.db.execute('SELECT active FROM sources').fetchone()[0], 0)

    def test_related_backlog_does_not_hide_independent_work_beyond_first_batch(self):
        for number in range(1, 35):
            item = self.item(number)
            item['primary_source_url'] = 'https://www.cbr.ru/shared'
            enqueue(self.db, None, item, self.source, self.options)
        independent = self.item(35, 'Казахстан запустил площадку майнинга')
        independent['description'] = 'Новые казахстанские предприятия добывают биткоин.'
        enqueue(self.db, None, independent, self.source, self.options)
        coordinator = Coordinator(self.db, self.config, {})
        try:
            self.assertEqual(coordinator._claim()['item_id'], 1)
            self.assertEqual(coordinator._claim()['item_id'], 35)
        finally:
            coordinator.abort()

    def test_aborted_coordinator_resumes_work_without_applying_late_output(self):
        enqueue(self.db, None, self.item(), self.source, self.options)
        ready = threading.Event()
        release = threading.Event()
        def blocked(*args):
            ready.set()
            self.assertTrue(release.wait(3))
            return self.noise()
        with patch('newsroom.core.get_api_key', return_value='test'), \
             patch('newsroom.core.analyze_with_ai', side_effect=blocked) as analyze:
            coordinator = Coordinator(self.db, self.config, {})
            coordinator.tick()
            self.assertTrue(ready.wait(3))
            release.set()
            coordinator.abort()
            self.assertEqual(snapshot(self.db)['pending'], 1)
            self.assertEqual(self.db.execute('SELECT COUNT(*) FROM item_analysis').fetchone()[0], 0)
            self.assertEqual(self.db.execute("SELECT COUNT(*) FROM processing_job_events WHERE status='CYCLE_ABORTED'").fetchone()[0], 1)
            counts = {}
            Coordinator(self.db, self.config, counts).close()
        self.assertEqual(counts, {'NOISE': 1})
        self.assertEqual(analyze.call_count, 1)

    def test_search_capacity_deferral_does_not_mark_source_broken(self):
        self.config['web_search'] = {'enabled': True}
        self.config['sources'] = [{'name': 'Search', 'type': 'web_search',
                                  'url': 'https://example.org/search', 'query': 'digital assets'}]
        with patch('newsroom.core.fetch_web_search', side_effect=BudgetDeferred()):
            counts = run_cycle(self.config)
        self.assertEqual(counts, {'WEB_SEARCH_DEFERRED': 1})
        row = self.db.execute("SELECT last_error,consecutive_failures,last_success_at FROM sources WHERE name='Search'").fetchone()
        self.assertEqual(tuple(row), (None, 0, None))

    def test_expired_pending_work_is_closed_even_when_its_source_is_disabled(self):
        item = self.item()
        item['published_at'] = (datetime.now(timezone.utc)-timedelta(hours=25)).isoformat()
        enqueue(self.db, None, item, self.source, self.options)
        self.config['sources'] = []
        run_cycle(self.config)
        self.assertEqual(self.db.execute('SELECT disposition FROM items').fetchone()[0], 'STORE_ONLY')
        self.assertEqual(snapshot(self.db)['unfinished'], 0)

    def test_correction_budget_deferral_preserves_spent_attempts_and_remaining_limit(self):
        from newsroom.ai import AIResponseError
        from newsroom.review import process_feedback_corrections
        item = self.item()
        with patch('newsroom.core.get_api_key', return_value='test'), \
             patch('newsroom.core.analyze_with_ai', return_value=self.publish_result(item)):
            self.assertEqual(process_item(self.db, self.source, item, **self.options,
                                         ai_settings=self.config['ai']), 'NEW_STORY')
        self.db.execute("UPDATE posts SET status='PUBLISHED',external_id='100',published_at=?", (self.now,))
        self.db.execute("INSERT INTO editorial_feedback(created_at,post_id,feedback_type,reason) VALUES(?,1,'OTHER','Уточнить формулировку')", (self.now,))
        self.db.execute("INSERT INTO telegram_feedback_corrections(feedback_id,post_id,owner_chat_id,created_at,updated_at) "
                        "VALUES(1,1,'test',?,?)", (self.now, self.now))
        self.db.commit()
        with patch('newsroom.ai.correct_published_post', side_effect=[AIResponseError('NETWORK_CONNECTION_ERROR'), BudgetDeferred()]):
            self.assertEqual(process_feedback_corrections(self.config, self.db), 0)
        row = self.db.execute('SELECT status,attempt_count,next_attempt_at FROM telegram_feedback_corrections').fetchone()
        self.assertEqual((row['status'], row['attempt_count']), ('QUEUED', 1))
        self.assertIsNotNone(row['next_attempt_at'])
        self.db.execute('UPDATE telegram_feedback_corrections SET next_attempt_at=NULL')
        self.db.commit()
        with patch('newsroom.ai.correct_published_post', side_effect=AIResponseError('NETWORK_CONNECTION_ERROR')) as correct:
            self.assertEqual(process_feedback_corrections(self.config, self.db), 1)
            self.assertEqual(correct.call_count, 2)
        self.assertEqual(tuple(self.db.execute('SELECT status,attempt_count FROM telegram_feedback_corrections').fetchone()), ('REJECTED', 3))


class RuntimeTests(unittest.TestCase):
    setUp = WorkflowTests.setUp

    def runtime(self, **settings):
        return Runtime(self.path, {'api_retry_reserve': 0, 'api_correction_reserve': 0, **settings})

    def test_global_request_budget_survives_new_runtime_instances(self):
        first = self.runtime(api_requests_per_window=1)
        call = first.reserve({'model': 'test'}, {})
        first.finish(call, {'usage': {'input_tokens': 10, 'output_tokens': 2}}, .1)
        with self.assertRaises(BudgetDeferred):
            self.runtime(api_requests_per_window=1).reserve({'model': 'test'}, {})

    def test_parallel_reservations_never_exceed_global_concurrency(self):
        runtimes = [self.runtime(api_concurrency=2) for _ in range(8)]
        barrier = threading.Barrier(8)
        def reserve(runtime):
            barrier.wait(3)
            try:
                return runtime.reserve({'model': 'test'}, {})
            except BudgetDeferred:
                return None
        with ThreadPoolExecutor(max_workers=8) as pool:
            receipts = list(pool.map(reserve, runtimes))
        self.assertEqual(sum(value is not None for value in receipts), 2)

    def test_retries_and_corrections_have_reserved_capacity(self):
        runtime = self.runtime(api_requests_per_window=4, api_retry_reserve=1, api_correction_reserve=1)
        for _ in range(2):
            call = runtime.reserve({'model': 'test'}, {})
            runtime.finish(call, {}, .01)
        with self.assertRaises(BudgetDeferred):
            runtime.reserve({'model': 'test'}, {})
        call = runtime.reserve({'model': 'test'}, {'_work_category': 'retry'})
        runtime.finish(call, {}, .01)
        call = runtime.reserve({'model': 'test'}, {'_work_category': 'correction'})
        runtime.finish(call, {}, .01)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM api_usage').fetchone()[0], 4)

    def test_missing_prices_and_missing_usage_are_not_zero_cost(self):
        runtime = self.runtime()
        call = runtime.reserve({'model': 'test'}, {})
        runtime.finish(call, {}, .01)
        result = usage_snapshot(self.db)
        self.assertEqual(result['unknown_usage'], 1)
        self.assertIsNone(result['estimated_usd'])
        self.assertIsNone(result['cost_per_publication_usd'])

    def test_profile_backfill_releases_database_between_budgeted_model_calls(self):
        from newsroom.interests import backfill_submission_profiles
        for number in range(2):
            self.db.execute("INSERT INTO interest_submissions(telegram_user_id,chat_id,message_id,text,created_at,topics_extracted) "
                            "VALUES('test','test',?,'example',?,1)", (number, self.now))
        self.db.commit()
        runtime = self.config['ai']['_runtime']
        def infer(*args):
            call=runtime.reserve({'model': 'test'}, {'_work_role': 'filter', '_work_category': 'background'})
            runtime.finish(call, {}, .01)
            return {'analysis_depth': 'BRIEF', 'analysis_features': [], 'analysis_guidance': ''}
        with patch('newsroom.interests.analyze_submitted_post', side_effect=infer):
            self.assertEqual(backfill_submission_profiles(self.db, self.config['ai']), 2)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM api_usage').fetchone()[0], 2)

    def test_usage_estimate_uses_cached_token_price_and_synthetic_rates(self):
        runtime = self.runtime(model_prices={'test': {'input_per_million': 2, 'cached_input_per_million': 1, 'output_per_million': 4}})
        call = runtime.reserve({'model': 'test'}, {})
        runtime.finish(call, {'usage': {'input_tokens': 100, 'input_tokens_details': {'cached_tokens': 20}, 'output_tokens': 10}}, .1)
        result = usage_snapshot(self.db)
        self.assertAlmostEqual(result['estimated_usd'], .00022)
        self.assertEqual(result['unknown_usage'], 0)

    def test_unknown_api_outcome_consumes_capacity_and_remains_unpriced(self):
        runtime = self.runtime(api_requests_per_window=1)
        call = runtime.reserve({'model': 'test'}, {})
        from newsroom.ai import AIResponseError
        runtime.finish(call, None, .1, AIResponseError('NETWORK_TIMEOUT'))
        with self.assertRaises(BudgetDeferred):
            runtime.reserve({'model': 'test'}, {})
        self.assertEqual(self.db.execute('SELECT status FROM api_usage').fetchone()[0], 'UNKNOWN')

    def test_http_retry_also_requires_a_new_budget_reservation(self):
        from newsroom.ai import request_response
        from newsroom.ai import AIResponseError
        from urllib.error import URLError
        runtime = self.runtime(api_requests_per_window=1)
        with patch('newsroom.ai.get_api_key', return_value='test'), patch('newsroom.ai.time.sleep'), \
             patch('newsroom.ai.urllib.request.urlopen', side_effect=URLError('connection failed')) as transport:
            with self.assertRaises(AIResponseError):
                request_response({'model': 'test'}, {'_runtime': runtime})
            with self.assertRaises(BudgetDeferred):
                request_response({'model': 'test'}, {'_runtime': runtime})
        self.assertEqual(transport.call_count, 1)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM api_usage').fetchone()[0], 1)

    def test_background_work_waits_while_fresh_queue_is_unfinished(self):
        enqueue(self.db, None, WorkflowTests.item(self), self.source, self.options)
        with self.assertRaises(BudgetDeferred):
            self.runtime().reserve({'model': 'test'}, {'_work_category': 'background'})


if __name__ == '__main__':
    unittest.main()
