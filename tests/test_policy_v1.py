import copy
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from newsroom import policy
from newsroom.core import process_item
from newsroom.db import connect
from newsroom.knowledge import validate_post_bindings
from newsroom.material_flow import transport_retry
from newsroom.triage import schedule_retry


class PolicyV1Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = str(Path(self.tmp.name) / 'db')
        self.db = connect(self.path)
        self.addCleanup(self.db.close)
        self.db.execute("INSERT INTO sources(source_id,name,type,url) VALUES(1,'Банк','rss','https://example.org/feed')")
        self.source = dict(self.db.execute('SELECT * FROM sources').fetchone())
        self.now = datetime.now(timezone.utc)

    def stored(self, discovered=None):
        self.db.execute("INSERT INTO items(item_id,source_id,url,canonical_url,title,discovered_at,content_hash,title_hash,ingest_revision) "
                        "VALUES(1,1,'https://example.org/1','https://example.org/1','Банк',( ? ),'hash','title','v1')",
                        ((discovered or self.now).isoformat(),))
        self.db.commit()

    def test_recovery_cannot_reopen_editorial_rejection(self):
        from newsroom.material_flow import recover_technical
        self.stored()
        self.db.execute("UPDATE items SET disposition='REJECTED' WHERE item_id=1")
        self.db.commit()
        with self.assertRaisesRegex(ValueError, 'RECOVERY_REQUIRES_TECHNICAL_ERROR'):
            recover_technical(self.db, {}, 1, 'Provider health probe is now successful')
        self.assertEqual(self.db.execute('SELECT disposition FROM items WHERE item_id=1').fetchone()[0], 'REJECTED')
        self.assertEqual(self.db.execute('SELECT count(*) FROM processing_jobs').fetchone()[0], 0)
        self.assertEqual(self.db.execute("SELECT count(*) FROM app_state WHERE key LIKE 'technical_recovery:%'").fetchone()[0], 0)

    def test_technical_recovery_preserves_editorial_counts_and_failure_history(self):
        from newsroom.material_flow import recover_technical, transport_retry, mark
        self.stored()
        mark(self.db, 1, 'reading', 'ERROR', 'Provider unavailable', block_kind='technical')
        for _ in range(4):
            exhausted, _ = transport_retry(self.db, 1, 'HTTP_503')
        self.assertTrue(exhausted)
        self.db.execute("INSERT INTO app_state VALUES('selection_retry:1',?)", (json.dumps({'attempts': 2}),))
        self.db.execute("UPDATE items SET disposition='TECHNICAL_ERROR',processed_at='2026-10-07T11:00:00+00:00' WHERE item_id=1")
        self.db.commit()
        with self.assertRaisesRegex(ValueError, 'RECOVERY_EVIDENCE_REQUIRED'):
            recover_technical(self.db, {}, 1, '')
        record = recover_technical(self.db, {}, 1, 'Provider restored; successful health probe recorded')
        self.assertEqual(record['transport_baselines']['reading'], 4)
        self.assertEqual(record['previous_processed_at'], '2026-10-07T11:00:00+00:00')
        self.assertIsNone(self.db.execute('SELECT processed_at FROM items WHERE item_id=1').fetchone()[0])
        self.assertEqual(json.loads(self.db.execute("SELECT value FROM app_state WHERE key='selection_retry:1'").fetchone()[0])['attempts'], 2)
        self.assertEqual(self.db.execute('SELECT disposition FROM items WHERE item_id=1').fetchone()[0], 'PENDING')
        self.assertEqual(self.db.execute("SELECT count(*) FROM processing_jobs WHERE status='PENDING'").fetchone()[0], 1)
        with self.assertRaisesRegex(ValueError, 'RECOVERY_REQUIRES_TECHNICAL_ERROR'):
            recover_technical(self.db, {}, 1, 'Repeat without a new confirmed failure')
        mark(self.db, 1, 'reading', 'RUNNING')
        for index in range(4):
            exhausted, delay = transport_retry(self.db, 1, 'HTTP_503')
            self.assertEqual(exhausted, index == 3)
            self.assertEqual(delay, (30, 120, 300, 300)[index])
        history = json.loads(self.db.execute("SELECT value FROM app_state WHERE key='transport_retry:1:v1:reading'").fetchone()[0])
        self.assertEqual(history['failures'], 8)
        self.assertEqual(history['episode_failures'], 4)

    def test_writer_date_context_preserves_day_and_cross_year_timezone(self):
        item = {'url': 'https://example.org/1', 'published_at': '2025-12-31T22:30:00+00:00',
                'discovered_at': '2026-01-03T12:00:00+00:00'}
        context = policy.date_context(item, {'url': item['url']}, {})
        self.assertEqual(context['source_calendar_day'], '2026-01-01')
        self.assertEqual(context['source_date_precision'], 'timestamp')
        context = policy.date_context(item, {'url': item['url'], 'published_at': '2025-12-31'}, {})
        self.assertEqual(context['source_calendar_day'], '2025-12-31')
        self.assertEqual(context['source_published_at'], '2025-12-31')
        self.assertEqual(context['source_date_precision'], 'day')
        other = policy.date_context(item, {'url': 'https://another.example/primary'}, {})
        self.assertIsNone(other['source_calendar_day'])
        self.assertIsNone(other['source_published_at'])
        local = policy.date_context(item, {'url': item['url'], 'source_timezone': 'America/New_York'}, {})
        self.assertEqual(local['source_calendar_day'], '2025-12-31')

    def test_malformed_final_check_is_a_technical_error(self):
        from newsroom.ai import AIResponseError, SCHEMA, validate_draft
        valid = {'issues': [], 'covered_claims': [], 'editorial_check': {
            key: '' if key == 'history_note' else False
            for key in SCHEMA['properties']['editorial_check']['required']}}
        invalid = [dict(valid, issues='none'), dict(valid, covered_claims=[{'fact_id': True, 'post_quote': 'text'}]),
                   dict(valid, editorial_check={'source_matches_event': 'yes'}),
                   dict(valid, covered_claims=[{'fact_id': 1, 'post_quote': None}])]
        for result in invalid:
            with self.subTest(result=result), patch('newsroom.ai.request_response', return_value={
                    'output': [{'content': [{'type': 'output_text', 'text': json.dumps(result)}]}]}):
                with self.assertRaisesRegex(AIResponseError, 'INVALID_TEXT_CHECK'):
                    validate_draft({}, {}, {}, {})

    def test_historic_table_rules_are_not_a_hidden_authority(self):
        settings = {'_editorial_registry': {'rules': [['Лид', 'Старый запрет', '']]}}
        self.assertNotIn('Старый запрет', policy.prompt('drafting', settings))
        settings['_policy_baseline'] = [['Лид', 'Старый запрет', '']]
        settings['_editorial_registry']['rules'].append(['Лид', 'Новое явное правило', ''])
        self.assertNotIn('Старый запрет', policy.prompt('drafting', settings))
        self.assertIn('Новое явное правило', policy.prompt('drafting', settings))
        self.assertNotEqual(policy.snapshot()['sha256'], policy.snapshot(settings)['sha256'])

    def test_owner_can_explicitly_reinstate_a_historical_rule(self):
        rule = ['Лид', 'Начинай с события', '']
        settings = {'_policy_baseline': [rule], '_editorial_registry': {'rules': [rule]}}
        self.assertEqual(policy.amendments(settings), [])
        settings['_policy_confirmed_rules'] = [rule[:2]]
        self.assertEqual(policy.amendments(settings), [rule])

    def test_final_text_seal_detects_mutation_and_changed_rules(self):
        import hashlib
        text = '🇷🇺 Банк открыл сервис\n\nПодтверждённые условия.'
        facts = {'policy': policy.snapshot(), 'final_text_check': {
            'text_sha256': 'checked-content',
            'assembled_sha256': hashlib.sha256(text.encode()).hexdigest()}}
        self.assertEqual(policy.publication_issues(text, facts), [])
        self.assertIn('FINAL_TEXT_CHANGED', policy.publication_issues(text + ' Новое утверждение.', facts))
        settings = {'_policy_baseline': [], '_editorial_registry': {'rules': [['Лид', 'Новое правило', '']]}}
        self.assertIn('POLICY_CHANGED', policy.publication_issues(text, facts, settings))
        self.assertIn('FINAL_TEXT_CHECK_MISSING', policy.publication_issues(text, {}))

    def test_cutover_preserves_evidence_and_closes_old_learning_only_once(self):
        self.stored()
        self.db.execute("INSERT INTO app_state VALUES('editorial_learning:1',?)",
                        (json.dumps({'status': 'READY', 'signal': {'reason': 'Старое замечание'}}),))
        self.assertTrue(policy.cutover(self.db))
        state = json.loads(self.db.execute("SELECT value FROM app_state WHERE key='editorial_learning:1'").fetchone()[0])
        self.assertEqual(state['status'], 'ARCHIVED_POLICY_CUTOVER')
        self.assertEqual(state['signal']['reason'], 'Старое замечание')
        self.assertFalse(policy.cutover(self.db))
        self.assertEqual(self.db.execute('SELECT disposition FROM items').fetchone()[0], 'PENDING')

    def test_queue_age_does_not_expire_a_material_accepted_on_receipt(self):
        self.stored(self.now - timedelta(hours=47))
        item = {'published_at': (self.now - timedelta(hours=48)).isoformat()}
        self.assertIsNone(policy.admission(self.db, 1, item, self.source, 24))
        self.assertIsNone(policy.admission(self.db, 1, item, self.source, 24, after_read=True))

    def test_delayed_job_creation_does_not_replace_revision_receipt(self):
        self.stored(self.now - timedelta(days=30))
        receipt = self.now - timedelta(hours=47)
        self.db.execute('INSERT INTO app_state VALUES(?,?)',
                        ('material_received:1:v1', json.dumps({'at': receipt.isoformat()})))
        item = {'published_at': (receipt - timedelta(hours=1)).isoformat()}
        self.assertIsNone(policy.admission(self.db, 1, item, self.source, 24))
        saved = json.loads(self.db.execute("SELECT value FROM app_state WHERE key='policy_admission:1:v1'").fetchone()[0])
        self.assertEqual(saved['at'], receipt.isoformat())

    def test_activation_backs_up_and_never_reopens_work(self):
        import stat
        import sqlite3
        self.stored()
        self.db.execute("UPDATE items SET disposition='STORE_ONLY'")
        self.db.commit()
        self.assertTrue(policy.activate(self.path))
        self.assertFalse(policy.activate(self.path))
        backups = list((Path(self.path).parent / 'policy-backups').glob('*.sqlite3'))
        self.assertEqual(len(backups), 1)
        self.assertEqual(stat.S_IMODE(backups[0].stat().st_mode), 0o600)
        with sqlite3.connect(backups[0]) as backup:
            self.assertEqual(backup.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
            self.assertEqual(backup.execute('SELECT disposition FROM items').fetchone()[0], 'STORE_ONLY')
            self.assertIsNone(backup.execute("SELECT value FROM app_state WHERE key='policy_v1_cutover'").fetchone())
        self.assertEqual(self.db.execute('SELECT disposition FROM items').fetchone()[0], 'STORE_ONLY')

    def test_modification_time_does_not_become_publication_date(self):
        self.stored()
        item = {'url': 'https://example.org/1', 'updated_at': self.now.isoformat()}
        self.assertIsNone(policy.admission(self.db, 1, item, self.source, 24))
        outcome = policy.admission(self.db, 1, item, self.source, 24, after_read=True)
        self.assertEqual(outcome[0], 'STORE_ONLY')
        self.assertIsNone(self.db.execute("SELECT value FROM app_state WHERE key='policy_admission:1:v1'").fetchone())
        from newsroom.core import _read_material_report
        report = _read_material_report(self.source, {**item, 'material_read': True}, 'Банк сообщил об изменении.')
        self.assertIsNone(report['published_at'])
        self.assertEqual(report['updated_at'], item['updated_at'])
        item['freshness_evidence'] = {'kind': 'dated_feed_sequence', 'description': 'Current dated feed position'}
        self.assertIsNone(policy.admission(self.db, 1, item, self.source, 24, after_read=True))
        record = json.loads(self.db.execute("SELECT value FROM app_state WHERE key='policy_admission:1:v1'").fetchone()[0])
        self.assertIsNone(record['source_date'])
        self.assertEqual(record['freshness_evidence'], item['freshness_evidence'])

    def test_policy_backup_precedes_schema_migration(self):
        import sqlite3
        self.stored()
        with patch('newsroom.db.connect', side_effect=RuntimeError('migration failed')):
            with self.assertRaisesRegex(RuntimeError, 'migration failed'):
                policy.activate(self.path)
        backups = list((Path(self.path).parent / 'policy-backups').glob('*.sqlite3'))
        self.assertEqual(len(backups), 1)
        with sqlite3.connect(backups[0]) as backup:
            self.assertEqual(backup.execute('SELECT count(*) FROM items').fetchone()[0], 1)
            self.assertIsNone(backup.execute("SELECT value FROM app_state WHERE key='policy_v1_cutover'").fetchone())
        self.assertIsNone(self.db.execute("SELECT value FROM app_state WHERE key='policy_v1_cutover'").fetchone())

    def test_activation_does_not_create_a_database_at_a_wrong_path(self):
        import sqlite3
        missing = Path(self.tmp.name) / 'missing.sqlite3'
        with self.assertRaises(sqlite3.OperationalError):
            policy.activate(missing)
        self.assertFalse(missing.exists())

    def test_style_amendment_redrafts_without_repeating_reading_or_analysis(self):
        from newsroom.material_flow import save_draft_context, load_draft_context
        self.stored()
        item = {'title': 'Банк', 'url': 'https://example.org/1'}
        settings = {'_policy_baseline': [], '_editorial_registry': {'rules': []}}
        context = {'ai_result': {'_needs_post_draft': False, 'what_is_new': 'Новое событие',
                                 'final_text_check': {'text_sha256': 'old-proof'}}}
        save_draft_context(self.db, 1, item, settings, context)
        changed = {**settings, '_editorial_registry': {'rules': [['Заголовок', 'Пиши короче', '']]}}
        resumed = load_draft_context(self.db, 1, item, changed)
        self.assertIsNotNone(resumed)
        self.assertEqual(resumed['ai_result']['what_is_new'], 'Новое событие')
        self.assertTrue(resumed['ai_result']['_needs_post_draft'])
        self.assertNotIn('final_text_check', resumed['ai_result'])
        changed['_editorial_registry']['rules'].append(['Отбор', 'Новый тематический критерий', ''])
        self.assertIsNone(load_draft_context(self.db, 1, item, changed))

    def test_changed_style_requeues_ready_post_and_reuses_its_analysis(self):
        from test_workflow import WorkflowTests
        from newsroom.cli import publish
        from newsroom.source_recheck import SourceUpdateRequired
        from newsroom.workflow import Coordinator
        fixture = WorkflowTests(methodName='runTest'); fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        item = fixture.item(77)
        decision = fixture.publish_result(item)
        with patch('newsroom.core.get_api_key', return_value='test'), patch('newsroom.core.analyze_with_ai', return_value=decision):
            self.assertEqual(process_item(fixture.db, fixture.source, item, .35, 3500, 24, ai_settings=fixture.config['ai']), 'NEW_STORY')
        fixture.config['newsroom']['auto_publish_since'] = fixture.now
        fixture.config['telegram'] = {'chat_id': '@test', 'chat_id_env': 'UNUSED_POLICY_TEST'}
        fixture.config['ai'].update(_policy_baseline=[], _editorial_registry={'rules': [['Заголовок', 'Пиши короче', '']]})
        with patch('newsroom.cli.telegram_send') as send:
            with self.assertRaises(SourceUpdateRequired):
                publish(fixture.db, fixture.config, 1, automatic=True)
        send.assert_not_called()
        draft = {key: decision.get(key, '') for key in ('headline_ru', 'summary_ru', 'what_is_new', 'editorial_check')}
        with patch('newsroom.core.analyze_with_ai') as analyze, patch('newsroom.ai.draft_post', return_value=draft) as writer:
            Coordinator(fixture.db, fixture.config, {}, categories=('retry',)).close()
        analyze.assert_not_called(); writer.assert_called_once()
        self.assertEqual([row[0] for row in fixture.db.execute('SELECT status FROM posts ORDER BY post_id')], ['SUPERSEDED', 'PENDING'])

    def test_old_automatic_material_is_context_but_owner_link_is_read(self):
        self.stored()
        item = {'published_at': (self.now - timedelta(days=20)).isoformat()}
        self.assertEqual(policy.admission(self.db, 1, item, self.source, 24)[0], 'STORE_ONLY')
        self.assertIsNone(policy.admission(self.db, 1, item, {**self.source, 'type': 'manual'}, 24))

    def test_unknown_date_is_read_before_context_decision_and_never_invented(self):
        self.stored()
        item = {}
        self.assertIsNone(policy.admission(self.db, 1, item, self.source, 24))
        self.assertEqual(policy.admission(self.db, 1, item, self.source, 24, after_read=True)[0], 'STORE_ONLY')
        self.assertNotIn('published_at', item)
        item['freshness_evidence'] = 'Новая запись в сохранённой последовательности ленты'
        self.assertIsNone(policy.admission(self.db, 1, item, self.source, 24, after_read=True))

    def test_owner_link_does_not_make_future_metadata_valid(self):
        self.stored()
        item = {'published_at': (self.now + timedelta(days=1)).isoformat()}
        self.assertEqual(policy.admission(self.db, 1, item, {**self.source, 'type': 'manual'}, 24)[0], 'TECHNICAL_ERROR')

    def test_calendar_day_is_preserved_without_inventing_midnight(self):
        from newsroom.core import parse_date
        self.assertEqual(parse_date('2026-10-07'), '2026-10-07')
        self.assertIsNone(parse_date('2026-02-30'))
        self.assertEqual(parse_date('2026-10-07T10:00:00+03:00'), '2026-10-07T07:00:00+00:00')

    def test_day_precision_uses_known_source_timezone(self):
        receipt = datetime(2026, 10, 7, 22, 30, tzinfo=timezone.utc)
        self.stored(receipt)
        item = {'published_at': '2026-10-06', 'source_timezone': 'America/New_York'}
        self.assertIsNone(policy.admission(self.db, 1, item, self.source, 24))

    def test_technical_failures_do_not_consume_editorial_attempts(self):
        self.stored()
        for n, delay in enumerate((30, 120, 300, 300), 1):
            exhausted, actual = transport_retry(self.db, 1, 'NETWORK_TIMEOUT', now=self.now)
            self.assertEqual(actual, delay)
            self.assertEqual(exhausted, n == 4)
        self.assertIsNone(self.db.execute("SELECT 1 FROM app_state WHERE key='selection_retry:1'").fetchone())

    def test_editorial_delays_and_terminal_history_are_preserved(self):
        for index, minutes in enumerate((1, 3, 10)):
            count = schedule_retry(self.db, 1, 'PRIMARY_RETRY', self.now, retry=index > 0)
            state = json.loads(self.db.execute("SELECT value FROM app_state WHERE key='selection_retry:1'").fetchone()[0])
            self.assertEqual(count, index)
            self.assertEqual(datetime.fromisoformat(state['next_at']) - self.now, timedelta(minutes=minutes))
        schedule_retry(self.db, 1, 'STORE_ONLY', self.now)
        state = json.loads(self.db.execute("SELECT value FROM app_state WHERE key='selection_retry:1'").fetchone()[0])
        self.assertEqual(state['attempts'], 2)
        self.assertNotIn('next_at', state)

    def test_rendered_binding_cannot_name_unoffered_or_absent_fact(self):
        for binding in ({'fact_id': 99, 'post_quote': 'Банк получил разрешение'},
                        {'fact_id': 1, 'post_quote': 'Несуществующий фрагмент'}):
            with self.assertRaises(ValueError):
                validate_post_bindings({'issues': [], 'covered_claims': [binding]}, {1}, 'Банк получил разрешение')

    def test_full_pipeline_accepts_a_checked_paraphrase_without_forced_quotes_or_confidence_gate(self):
        quote = 'Банк России включил Сбербанк в реестр цифровых депозитариев.'
        paraphrase = 'Сбербанк получил допуск к работе цифровым депозитарием после включения в реестр Банка России.'
        audit = {key: True for key in ('source_matches_event', 'attribution_preserved', 'stage_preserved',
                'headline_main_event', 'lead_event_first', 'paragraphs_concise_distinct', 'no_editorial_process_notes')}
        audit.update(history_required=False, history_explained=False, history_note='')
        decision = {'action': 'NEW_STORY', 'is_relevant': True, 'publication_recommendation': 'AUTO_PUBLISH',
            'confidence': .01, 'geographic_scope': 'RUSSIA', 'russia_cis_impact': 'DIRECT', 'impact_evidence': quote,
            'independent_check': 'NOT_ASSESSED', 'development_date': '', 'development_date_evidence': '',
            'facts': [{'text': quote, 'claim_type': 'FACT'}], 'editorial_check': {**audit, 'attribution_preserved': False},
            'memory': {'match_status': 'CERTAIN', 'existing_event_id': '', 'event': {
                'subject': 'Банк России', 'action': 'включил', 'object': 'Сбербанк', 'jurisdiction': 'RU',
                'stage': 'APPROVED', 'event_date': '', 'statement_date': '', 'effective_date': '', 'document_id': ''},
                'claims': [{'subject': 'Сбербанк', 'predicate': 'включён в реестр', 'scope': 'цифровые депозитарии RU',
                    'value': 'включён', 'statement': quote, 'claim_type': 'FACT', 'source_quote': quote, 'post_quote': '',
                    'valid_from': '', 'valid_to': '', 'previous_fact_id': '', 'relation': 'NEW',
                    'change_type': 'PARTICIPANT_CHANGE', 'material': True,
                    'material_reason': 'Появился новый участник инфраструктуры цифровых активов.'}]}}
        item = {'url': 'https://example.org/1', 'title': 'Сбербанк в реестре', 'content': quote,
                'published_at': self.now.isoformat(), 'primary_source_status': 'READ',
                'primary_source_url': 'https://www.cbr.ru/1', 'primary_source_content': quote,
                'primary_source_type': 'OFFICIAL', 'material_read': True}
        stages = []
        def provider(payload, settings):
            name = payload['text']['format']['name']; stages.append(name)
            if name == 'newsroom_editor_decision':
                result = copy.deepcopy(decision)
            elif name == 'newsroom_post_draft':
                result = {'headline_ru': '🇷🇺 Сбербанк получил допуск к работе цифровым депозитарием',
                          'summary_ru': paraphrase, 'what_is_new': paraphrase, 'editorial_check': audit}
                self.assertNotIn('required_fact_quotes', payload['instructions'])
            elif name == 'newsroom_final_text_check':
                data = json.loads(payload['input'])
                fact_id = data['draft_contract']['material_facts'][0]['fact_id']
                self.assertEqual(data['read_source']['content'], quote)
                result = {'issues': [], 'editorial_check': audit,
                          'covered_claims': [{'fact_id': fact_id, 'post_quote': paraphrase}]}
            else:
                self.fail(name)
            return {'output': [{'content': [{'type': 'output_text', 'text': json.dumps(result)}]}]}
        with patch('newsroom.ai.get_api_key', return_value='test'), \
             patch('newsroom.core.get_api_key', return_value='test'), \
             patch('newsroom.ai.request_response', side_effect=provider):
            outcome = process_item(self.db, self.source, item, .35, 3500, 24,
                                   ai_settings={'model': 'test', 'memory_mode': 'enforce'})
        self.assertEqual(outcome, 'NEW_STORY')
        post = self.db.execute('SELECT * FROM posts').fetchone()
        self.assertIn(paraphrase, post['text'])
        self.assertNotIn(quote, post['text'])
        self.assertEqual(stages, ['newsroom_editor_decision', 'newsroom_post_draft', 'newsroom_final_text_check'])
        binding = self.db.execute('SELECT post_quote FROM post_facts').fetchone()[0]
        self.assertEqual(binding, paraphrase)
        facts = json.loads(post['fact_check_result'])
        self.assertEqual(facts['policy']['version'], '1.0')
        self.assertTrue(facts['final_text_check']['text_sha256'])
