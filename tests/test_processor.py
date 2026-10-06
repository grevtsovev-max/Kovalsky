import copy
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from newsroom.db import connect
from newsroom.workflow import Work, StageExecutor, enqueue, Coordinator
from newsroom.processor import prepare, seed_held, run
try:
    import test_workflow as fixtures
except ModuleNotFoundError:
    import tests.test_workflow as fixtures


class ProcessorTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.WorkflowTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.db, self.config, self.source = self.fixture.db, self.fixture.config, self.fixture.source

    def test_collection_does_not_wait_for_editor(self):
        from newsroom.core import run_cycle
        self.config['_collection_only'] = True
        with patch('newsroom.core.fetch_rss', return_value=[self.fixture.item()]), patch('newsroom.core.analyze_with_ai') as editor:
            run_cycle(self.config)
        editor.assert_not_called()
        self.assertEqual(self.db.execute('SELECT status FROM processing_jobs').fetchone()[0], 'PENDING')

    def test_reader_pool_does_not_block_editor_pool(self):
        release = threading.Event()
        started = threading.Event()
        def read():
            started.set()
            release.wait(2)
        def analyze():
            return {'action': 'NOISE'}
        executor = StageExecutor(1)
        try:
            reader = executor.submit(Work('collector', read).execute, None, {})
            self.assertTrue(started.wait(1))
            editor = executor.submit(Work('editor', analyze).execute, None, {})
            self.assertEqual(editor.result(timeout=1), {'action': 'NOISE'})
            self.assertFalse(reader.done())
        finally:
            release.set()
            executor.shutdown()

    def test_capacity_wait_returns_to_running_without_counting_a_retry(self):
        from newsroom.runtime import BudgetDeferred
        from newsroom.material_flow import revision, snapshot
        enqueue(self.db, None, self.fixture.item(), self.source, self.fixture.options)
        runtime = self.config['ai']['_runtime']
        scope = {'item_id': 1, 'revision': revision(self.db, 1)}
        calls = []
        def analyze():
            calls.append(1)
            if len(calls) == 1:
                raise BudgetDeferred('concurrency', 1)
            with runtime.db() as db:
                state = next(x for x in snapshot(db, 1) if x['stage'] == 'analysis')
            self.assertEqual(state['status'], 'RUNNING')
            self.assertIsNone(state['block_kind'])
            self.assertGreater(state['wait_seconds'], .3)
            return {'action': 'NOISE'}
        executor = StageExecutor(1)
        try:
            future = executor.submit(Work('editor', analyze).execute, runtime, scope)
            self.assertEqual(future.result(timeout=3), {'action': 'NOISE'})
        finally:
            executor.shutdown()
        self.assertEqual(len(calls), 2)
        self.assertIsNone(self.db.execute("SELECT value FROM app_state WHERE key='selection_retry:1'").fetchone())

    def test_first_account_failure_does_not_consume_attempt_at_any_ai_stage(self):
        from newsroom.ai import AIResponseError
        from newsroom.core import process_item
        from newsroom.material_flow import snapshot
        for number, stage in enumerate(('screening', 'analysis', 'drafting'), 200):
            with self.subTest(stage=stage):
                item = self.fixture.item(number)
                settings = dict(self.config['ai'], triage_enabled=stage == 'screening')
                result = self.fixture.publish_result(item)
                result['_needs_post_draft'] = True
                result['headline_ru'] = result['summary_ru'] = ''
                error = AIResponseError('HTTP_429:credit_balance_exhausted')
                with patch('newsroom.core.get_api_key', return_value='test'), \
                     patch('newsroom.triage.classify', side_effect=error), \
                     patch('newsroom.core.analyze_with_ai', side_effect=error if stage == 'analysis' else None, return_value=result), \
                     patch('newsroom.ai.draft_post', side_effect=error):
                    self.assertEqual(process_item(self.db, self.source, item, .35, 3500, 24, ai_settings=settings), 'AI_RETRY')
                item_id = self.db.execute('SELECT item_id FROM items WHERE url=?', (item['url'],)).fetchone()[0]
                retry = json.loads(self.db.execute('SELECT value FROM app_state WHERE key=?', (f'selection_retry:{item_id}',)).fetchone()[0])
                self.assertEqual(retry['attempts'], 0)
                self.assertEqual(snapshot(self.db, item_id)[-1]['block_kind'], 'account')

    def test_failed_article_read_is_not_marked_done(self):
        from newsroom.material_flow import snapshot
        item = {key: value for key, value in self.fixture.item().items() if not key.startswith('primary_source_') and key != 'material_read'}
        config = prepare({**self.config, 'newsroom': dict(self.config['newsroom']), 'ai': dict(self.config['ai'])}, self.db)
        config['ai']['triage_enabled'] = True
        enqueue(self.db, None, item, self.source, self.fixture.options)
        with patch('newsroom.triage.classify', return_value={'decision': 'KEEP', 'reason': 'Нужно прочитать материал'}), \
             patch('newsroom.core.fetch_publisher_article', side_effect=TimeoutError()), \
             patch('newsroom.core._recover_primary', return_value=None), \
             patch('newsroom.core._agent_recover_primary', return_value=None), \
             patch('newsroom.core.analyze_with_ai') as analyze:
            Coordinator(self.db, config, {}, max_jobs=1).close()
        analyze.assert_not_called()
        reading = next(x for x in snapshot(self.db, 1) if x['stage'] == 'reading')
        self.assertEqual(reading['status'], 'WAITING')
        self.assertEqual(reading['block_kind'], 'evidence')

    def test_old_worker_does_not_mark_new_material_revision_running(self):
        from newsroom.material_flow import revision, snapshot
        enqueue(self.db, None, self.fixture.item(), self.source, self.fixture.options)
        old = revision(self.db, 1)
        self.db.execute("UPDATE items SET ingest_revision='new-version' WHERE item_id=1")
        self.db.commit()
        def analyze():
            return {'action': 'NOISE'}
        executor = StageExecutor(1)
        try:
            future = executor.submit(Work('editor', analyze).execute, self.config['ai']['_runtime'], {'item_id': 1, 'revision': old})
            future.result(timeout=3)
        finally:
            executor.shutdown()
        self.assertEqual(snapshot(self.db, 1), [])

    def test_legacy_read_flag_without_text_does_not_prevent_real_reading(self):
        runtime = self.config['ai']['_runtime']
        runtime.store('legacy-read', 'collector', {'material_read': True}, 60)
        text = {'material_read': True, 'content': 'Фактически прочитанный текст источника с описанием события.'}
        with patch('newsroom.core.fetch_publisher_article', return_value=text) as reader:
            work = Work('collector', reader, key='legacy-read', ttl=60)
            self.assertEqual(work.execute(runtime), text)
            self.assertEqual(work.execute(runtime), text)
        reader.assert_called_once()

    def test_unwritten_attribution_is_checked_by_writer_without_repeating_analysis(self):
        from newsroom.core import process_item
        source = dict(self.source, name='РБК')
        item = dict(self.fixture.item(300), primary_source_type='ORIGINAL_MEDIA_REPORT',
                    primary_source_publisher='РБК')
        item['primary_source_url'] = item['url']
        result = self.fixture.publish_result(item)
        result['facts'] = [{**fact, 'claim_type': 'REPORT'} for fact in result['facts']]
        result['original_reporting_check'] = {'central_claim_supported': True,
            'attribution_preserved': False, 'evidence': item['primary_source_content']}
        good = {key: copy.deepcopy(result.get(key, '')) for key in ('headline_ru', 'summary_ru', 'what_is_new', 'editorial_check')}
        bad = copy.deepcopy(good)
        bad['editorial_check']['attribution_preserved'] = False
        result['_needs_post_draft'] = True
        result['headline_ru'] = result['summary_ru'] = ''
        with patch('newsroom.core.get_api_key', return_value='test'), \
             patch('newsroom.core.analyze_with_ai', return_value=result) as analyze, \
             patch('newsroom.ai.draft_post', side_effect=[bad, good]) as writer:
            first = process_item(self.db, source, item, .35, 3500, 24, ai_settings=self.config['ai'])
            self.assertEqual(first, 'WAITING_CONFIRMATION')
            self.assertEqual(self.db.execute('SELECT COUNT(*) FROM posts').fetchone()[0], 0)
            item_id = self.db.execute('SELECT item_id FROM items WHERE url=?', (item['url'],)).fetchone()[0]
            second = process_item(self.db, source, item, .35, 3500, 24, ai_settings=self.config['ai'], existing_item_id=item_id)
        self.assertEqual(second, 'NEW_STORY', self.db.execute('SELECT result_json FROM item_analysis WHERE item_id=?', (item_id,)).fetchone()[0])
        self.assertEqual(analyze.call_count, 1)
        self.assertEqual(writer.call_count, 2)
        facts = json.loads(self.db.execute('SELECT fact_check_result FROM posts').fetchone()[0])
        self.assertTrue(facts['original_reporting_check']['attribution_preserved'])
        self.assertIn('[РБК]('+item['url']+')', self.db.execute('SELECT text FROM posts').fetchone()[0])

    def test_analysis_phase_still_requires_quote_and_safe_claim_type(self):
        from newsroom.core import require_primary_source_review
        source = {'url': 'https://example.org/report', 'publisher': 'РБК',
                  'content': 'Прочитанное сообщение источника о конкретном новом событии.',
                  'type': 'ATTRIBUTED_REPORT', 'material_read': True}
        base = {'action': 'NEW_STORY', 'publication_recommendation': 'AUTO_PUBLISH',
                'facts': [{'claim_type': 'REPORT'}], 'original_reporting_check': {
                    'central_claim_supported': True, 'attribution_preserved': False, 'evidence': source['content']}}
        self.assertEqual(require_primary_source_review(base, 'NO_LINK', publisher_report=source, analysis_only=True)['publication_recommendation'], 'AUTO_PUBLISH')
        self.assertEqual(require_primary_source_review(base, 'NO_LINK', publisher_report=source)['publication_recommendation'], 'WAIT_FOR_AUTOMATION')
        for changed in ({'facts': [{'claim_type': 'FACT'}]}, {'original_reporting_check': {**base['original_reporting_check'], 'evidence': 'Выдуманная цитата, которой нет в источнике.'}}):
            self.assertEqual(require_primary_source_review({**base, **changed}, 'NO_LINK', publisher_report=source, analysis_only=True)['publication_recommendation'], 'WAIT_FOR_AUTOMATION')

    def test_headline_action_accepts_finite_verbs_beyond_the_word_list(self):
        from newsroom.quality import editorial_issues
        facts = self.fixture.publish_result(self.fixture.item())
        for headline in ('🇷🇺 Компании вошли в реестры Банка России',
                         '🇷🇺 Банк России добавил криптопосредников в справочник',
                         '🇷🇺 Форум Ассоциации ФинТех завершился'):
            self.assertNotIn('HEADLINE_NOT_EVENT_LED', editorial_issues(headline, facts['summary_ru'], facts))
        for headline in ('🇷🇺 Новые счета цифрового рубля', '🇷🇺 Новые правила цифровых активов',
                         '🇷🇺 Открыть счёт цифрового рубля', '🇷🇺 Откройте счёт цифрового рубля'):
            self.assertIn('HEADLINE_NOT_EVENT_LED', editorial_issues(headline, facts['summary_ru'], facts))

    def test_legacy_migration_preserves_attempts_and_read_source(self):
        from newsroom.material_flow import migrate, snapshot
        enqueue(self.db, None, self.fixture.item(), self.source, self.fixture.options)
        self.db.execute("UPDATE items SET disposition='WAITING_CONFIRMATION'")
        self.db.execute("INSERT INTO app_state VALUES('selection_retry:1',?)", (json.dumps({'attempts': 1}),))
        self.db.execute("INSERT INTO app_state VALUES('editor_retry:1','2')")
        self.db.commit()
        migrate(self.db)
        migrate(self.db)
        state = json.loads(self.db.execute("SELECT value FROM app_state WHERE key='selection_retry:1'").fetchone()[0])
        self.assertEqual(state['attempts'], 2)
        self.assertTrue(any(x['stage']=='reading' and x['status']=='DONE' for x in snapshot(self.db, 1)))
        self.assertEqual(self.db.execute('SELECT disposition FROM items').fetchone()[0], 'WAITING_CONFIRMATION')

    def test_processor_works_without_collection_cycle(self):
        enqueue(self.db, None, self.fixture.item(), self.source, self.fixture.options)
        path = Path(self.fixture.temp.name) / 'worker.toml'
        path.write_text('[newsroom]\ndatabase = '+json.dumps(self.fixture.path)+'\n[ai]\nmodel = "test"\n')
        stop = threading.Event()
        errors = []
        def worker():
            try:
                run(str(path), stop)
            except Exception as exc:
                errors.append(exc)
        with patch('newsroom.core.get_api_key', return_value='test'), \
             patch('newsroom.core.analyze_with_ai', return_value=self.fixture.noise()), \
             patch('newsroom.cli.auto_publish_since', return_value=(0, 0, 0)):
            thread = threading.Thread(target=worker)
            thread.start()
            try:
                deadline = time.monotonic()+5
                while time.monotonic()<deadline and not errors:
                    if self.db.execute('SELECT disposition FROM items').fetchone()[0] == 'NOISE':
                        break
                    time.sleep(.05)
                self.assertEqual(self.db.execute('SELECT disposition FROM items').fetchone()[0], 'NOISE')
            finally:
                stop.set()
                thread.join(timeout=5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])

    def test_stale_saved_item_cannot_overwrite_new_revision(self):
        from newsroom.core import _save_item
        item = self.fixture.item()
        item_id = _save_item(self.db, self.source, item)
        old_revision = self.db.execute('SELECT ingest_revision FROM items').fetchone()[0]
        changed = dict(item, content=item['content']+' Новое существенное условие.')
        self.assertEqual(_save_item(self.db, self.source, changed), item_id)
        old = dict(item, _expected_revision=old_revision)
        self.assertIsNone(_save_item(self.db, self.source, old, existing_item_id=item_id))
        self.assertEqual(self.db.execute('SELECT content FROM items').fetchone()[0], changed['content'])

    def test_unchanged_feed_does_not_reset_enriched_article_or_attempts(self):
        from newsroom.core import _save_item
        item = self.fixture.item()
        item_id = _save_item(self.db, self.source, item)
        enriched = dict(item, title=item['title']+' Уточнённый заголовок.',
                        description='Описание из страницы издателя.', content=item['content']+' Полный прочитанный текст.')
        _save_item(self.db, self.source, enriched, existing_item_id=item_id)
        self.db.execute("UPDATE items SET disposition='AI_RETRY' WHERE item_id=?", (item_id,))
        self.db.execute("INSERT INTO app_state VALUES('selection_retry:1',?)", (json.dumps({'attempts': 2}),))
        self.db.commit()
        self.assertIsNone(_save_item(self.db, self.source, item))
        row = self.db.execute('SELECT title,content,disposition FROM items WHERE item_id=?', (item_id,)).fetchone()
        self.assertEqual(tuple(row), (enriched['title'], enriched['content'], 'AI_RETRY'))
        self.assertEqual(json.loads(self.db.execute("SELECT value FROM app_state WHERE key='selection_retry:1'").fetchone()[0])['attempts'], 2)
        changed = dict(item, description='Издатель добавил новое условие в ленту.')
        self.assertEqual(_save_item(self.db, self.source, changed), item_id)
        self.assertEqual(self.db.execute('SELECT disposition FROM items WHERE item_id=?', (item_id,)).fetchone()[0], 'PENDING')
        self.assertIsNone(self.db.execute("SELECT value FROM app_state WHERE key='selection_retry:1'").fetchone())

    def test_google_discovery_does_not_read_article_before_selection(self):
        from newsroom.core import fetch_google_news
        xml = b'<rss><channel><item><title>News</title><link>https://news.google.com/rss/articles/example</link><pubDate>Tue, 06 Oct 2026 10:00:00 GMT</pubDate><source>Publisher</source></item></channel></rss>'
        with patch('newsroom.core._request', return_value=xml), patch('newsroom.core.fetch_publisher_article') as reader:
            items = fetch_google_news('https://news.google.com/rss/search?q=test', read_articles=False)
        reader.assert_not_called()
        self.assertEqual(len(items), 1)
        self.assertNotIn('material_read', items[0])

    def test_search_date_is_checked_after_reading_and_before_analysis(self):
        from datetime import datetime, timedelta, timezone
        from newsroom.core import process_item
        source = dict(self.source, type='web_search')
        old = (datetime.now(timezone.utc)-timedelta(hours=48)).isoformat()
        for number, date, expected in ((100, self.fixture.now, 'NOISE'), (101, old, 'STALE'), (102, None, 'UNDATED')):
            with self.subTest(date=date):
                article = dict(self.fixture.item(number), published_at=date)
                item = {key: article[key] for key in ('url', 'title')}
                item.update(description='', content='', published_at=None)
                with patch('newsroom.core.fetch_publisher_article', return_value=article) as read, \
                     patch('newsroom.core.get_api_key', return_value='test'), \
                     patch('newsroom.core.analyze_with_ai', return_value=self.fixture.noise()) as analyze:
                    outcome = process_item(self.db, source, item, .35, 3500, 24, ai_settings=self.config['ai'])
                self.assertEqual(outcome, expected)
                read.assert_called_once()
                self.assertEqual(analyze.call_count, int(expected == 'NOISE'))
                self.assertEqual(self.db.execute('SELECT published_at FROM items WHERE url=?', (item['url'],)).fetchone()[0], date)
