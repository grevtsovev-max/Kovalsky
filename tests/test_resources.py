import io
import json
import sqlite3
import tempfile
import threading
import time
import unittest
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from http.server import ThreadingHTTPServer

from newsroom.ai import request_response
from newsroom.db import connect, connect_readonly
from newsroom.resources import price_receipt, snapshot
from newsroom.runtime import Runtime, SCOPE
from newsroom.workflow import Work


class Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


class ResourceTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = str(Path(directory.name) / 'news.db')
        self.db = connect(self.path)
        self.addCleanup(self.db.close)
        self.config = {'newsroom': {'database': self.path}, 'ai': {}}
        self.runtime = Runtime(self.path, {})
        self.receipt = {'status': 'completed', 'service_tier': 'default',
                        'usage': {'input_tokens': 1000, 'output_tokens': 200,
                                  'input_tokens_details': {'cached_tokens': 400},
                                  'output_tokens_details': {'reasoning_tokens': 150}}, 'output': []}

    def call(self, stage='editorial', receipt=None, item_id=None):
        token = SCOPE.set({'item_id': item_id, 'stage': stage, 'role': 'editor'})
        try:
            call_id = self.runtime.reserve({'model': 'gpt-6-luna'}, {})
            self.runtime.finish(call_id, receipt or self.receipt, 2, response_bytes=1200)
        finally:
            SCOPE.reset(token)
        return call_id

    def test_tokens_cost_time_and_hourly_totals_reconcile_by_stage_and_model(self):
        self.call('triage')
        self.call('editorial')
        result = snapshot(self.db, self.config)
        total = result['total']
        self.assertEqual(total['calls'], 2)
        self.assertEqual(total['input_tokens'], 2000)
        self.assertEqual(total['cached_input_tokens'], 800)
        self.assertEqual(total['output_tokens'], 400)
        self.assertEqual(total['reasoning_tokens'], 300)
        # Reasoning is a subset of output, and cached input a subset of input.
        self.assertAlmostEqual(total['estimated_usd'], .000328)
        self.assertEqual(total['api_seconds'], 4)
        for grouping in ('stages', 'models', 'hourly', 'categories'):
            self.assertEqual(sum(r['calls'] for r in result[grouping]), total['calls'])
            self.assertAlmostEqual(sum(r['known_estimated_usd'] for r in result[grouping]), total['estimated_usd'])

    def test_multiple_billable_search_actions_are_counted_separately_from_open_and_find(self):
        response = {**self.receipt, 'output': [
            {'type': 'web_search_call', 'action': {'type': action}}
            for action in ('search', 'open_page', 'find_in_page', 'search')]}
        call = self.runtime.reserve({'model': 'gpt-6-luna', 'tools': [{'type': 'web_search'}]},
                                    {'_work_stage': 'discovery_search'})
        self.runtime.finish(call, response, 1)
        row = self.db.execute('SELECT search_calls,search_actions,estimated_usd FROM api_usage').fetchone()
        self.assertEqual(tuple(row)[:2], (4, 2))
        self.assertAlmostEqual(row[2], .020164)

    def test_legacy_rows_stay_unattributed_and_reference_price_is_not_total(self):
        self.db.execute("INSERT INTO api_usage(call_id,role,category,model,status,created_at,input_tokens,cached_input_tokens,output_tokens) VALUES('old','filter','fresh','gpt-6-luna','SUCCEEDED',?,1000,400,200)",
                        (datetime.now(timezone.utc).isoformat(),))
        self.db.commit()
        report = snapshot(self.db, self.config)
        self.assertEqual(report['stages'][0]['stage'], 'unattributed')
        self.assertIsNone(report['total']['estimated_usd'])
        self.assertAlmostEqual(report['total']['reference_usd'], .000164)
        self.assertEqual(report['total']['retry_attribution_unknown'], 1)
        self.assertEqual(report['unknown_cost_reasons'], {'TIER_UNKNOWN': 1})

    def test_invalid_usage_breakdown_remains_unknown_without_losing_receipt(self):
        malformed = {**self.receipt, 'model': [], 'service_tier': {}, 'status': {},
                     'output': None, 'usage': {'input_tokens': 100, 'output_tokens': 10,
                     'input_tokens_details': {'cached_tokens': 101},
                     'output_tokens_details': {'reasoning_tokens': 11}}}
        self.call(receipt=malformed)
        self.call(receipt={**self.receipt, 'usage': []})
        report = snapshot(self.db, self.config)
        self.assertEqual(report['total']['calls'], 2)
        self.assertEqual(report['total']['unknown_usage'], 2)
        self.assertEqual(report['total']['unknown_reasoning'], 2)
        self.assertEqual(report['total']['cached_input_tokens'], 0)
        self.assertIsNone(report['total']['estimated_usd'])

    def test_failed_http_attempt_and_transport_retry_have_distinct_receipts(self):
        error = HTTPError('https://api.openai.com', 500, 'secret', {}, io.BytesIO(b'{"error":{"code":"server_error"}}'))
        with patch('newsroom.ai.get_api_key', return_value='private-key'), \
             patch('newsroom.ai.urllib.request.urlopen', side_effect=[error, Response(json.dumps(self.receipt).encode())]), \
             patch('newsroom.ai.time.sleep'):
            request_response({'model': 'gpt-6-luna', 'input': 'private-text'},
                             {'_runtime': self.runtime, '_work_stage': 'triage'})
        report = snapshot(self.db, self.config)
        self.assertEqual(report['total']['calls'], 2)
        self.assertEqual(report['total']['retries'], 1)
        self.assertEqual(report['total']['errors'], 1)
        self.assertEqual(report['total']['unknown_usage'], 1)
        self.assertIsNone(report['total']['estimated_usd'])
        self.assertAlmostEqual(report['total']['known_estimated_usd'], .000164)
        self.assertGreater(report['total']['request_bytes'], 0)
        encoded = json.dumps(report)
        for secret in ('private-key', 'private-text', self.path):
            self.assertNotIn(secret, encoded)

    def test_work_scope_supplies_runtime_stage_and_item_to_requests(self):
        with patch('newsroom.ai.get_api_key', return_value='test'), \
             patch('newsroom.ai.urllib.request.urlopen', return_value=Response(json.dumps(self.receipt).encode())):
            Work('editor', request_response, ({'model': 'gpt-6-luna'}, {}), stage='editorial').execute(
                self.runtime, {'item_id': 123, 'category': 'retry'})
        report = snapshot(self.db, self.config, item_id=123)
        self.assertEqual(report['total']['calls'], 1)
        self.assertEqual(report['total']['operations'], 1)
        self.assertEqual(report['stages'][0]['stage'], 'editorial')
        self.assertEqual(report['categories'][0]['category'], 'retry')

    def test_background_receipt_and_operation_use_same_category(self):
        with patch('newsroom.ai.get_api_key', return_value='test'), \
             patch('newsroom.ai.urllib.request.urlopen', return_value=Response(json.dumps(self.receipt).encode())):
            request_response({'model': 'gpt-6-luna'}, {'_runtime': self.runtime,
                             '_work_stage': 'archive_memory', '_work_category': 'background'})
        report = snapshot(self.db, self.config)
        self.assertEqual(len(report['categories']), 1)
        self.assertEqual(report['categories'][0]['category'], 'background')
        self.assertEqual(report['categories'][0]['calls'], 1)
        self.assertEqual(report['categories'][0]['operations'], 1)
        self.assertEqual(SCOPE.get(), {})

    def test_cache_hits_are_operations_without_an_extra_api_call(self):
        calls = []
        def read():
            calls.append(True)
            return {'material_read': True, 'content': 'Прочитанный текст сообщения источника.'}
        work = Work('collector', read, key='read-key', ttl=60, stage='source_read')
        work.execute(self.runtime)
        work.execute(self.runtime)
        result = snapshot(self.db, self.config)['total']
        self.assertEqual(len(calls), 1)
        self.assertEqual(result['calls'], 0)
        self.assertEqual(result['operations'], 2)
        self.assertEqual(result['cache_hits'], 1)
        self.assertGreater(result['operation_seconds'], 0)
        self.assertGreaterEqual(result['cpu_seconds'], 0)

    def test_nested_operations_do_not_double_count_worker_time(self):
        with self.runtime.measure('digest', 'editor'):
            with self.runtime.measure('telegram_delivery', 'editor'):
                time.sleep(.02)
        result = snapshot(self.db, self.config)
        rows = {r['stage']: r for r in result['stages']}
        self.assertGreater(rows['telegram_delivery']['operation_seconds'], .015)
        self.assertLess(rows['digest']['operation_seconds'], rows['telegram_delivery']['operation_seconds'])
        self.assertEqual(result['total']['operations'], 2)

    def test_period_filter_and_readonly_reports_never_migrate(self):
        call = self.call()
        earlier = (datetime.now(timezone.utc)-timedelta(hours=2)).isoformat()
        self.db.execute('UPDATE api_usage SET created_at=? WHERE call_id=?', (earlier, call))
        self.db.commit()
        readonly = connect_readonly(self.path)
        self.addCleanup(readonly.close)
        version = readonly.execute('PRAGMA schema_version').fetchone()[0]
        self.assertEqual(snapshot(readonly, self.config, hours=1)['total']['calls'], 0)
        self.assertEqual(snapshot(readonly, self.config, hours=24)['total']['calls'], 1)
        self.assertEqual(readonly.execute('PRAGMA schema_version').fetchone()[0], version)

    def test_missing_prices_and_search_receipts_are_never_zero_cost(self):
        row = {'model': 'unknown-model', 'service_tier': 'default', 'input_tokens': 0,
               'cached_input_tokens': 0, 'output_tokens': 0}
        self.assertIsNone(price_receipt(row, {})['total_usd'])
        row.update(model='gpt-6-luna', search_requested=1, search_tool='web_search')
        self.assertEqual(price_receipt(row, {})['reason'], 'SEARCH_USAGE_UNKNOWN')

    def test_actual_fast_tier_long_context_and_configured_tariffs(self):
        row = {'model': 'gpt-6-luna', 'service_tier': 'priority', 'input_tokens': 300000,
               'cached_input_tokens': 100000, 'output_tokens': 10000}
        self.assertAlmostEqual(price_receipt(row, {})['total_usd'], .099)
        row.update(model='private-model', input_tokens=1000, cached_input_tokens=0, output_tokens=0)
        configured = {'model_prices': {'private-model': {'input_per_million': 3,
                      'cached_input_per_million': 0, 'output_per_million': 0}}}
        self.assertAlmostEqual(price_receipt(row, configured)['total_usd'], .003)

    def test_operation_failure_preserves_exception_and_never_stores_error_text(self):
        with self.assertRaisesRegex(ValueError, 'private-details'):
            with self.runtime.measure('source_read', 'collector'):
                raise ValueError('private-details')
        self.assertEqual(SCOPE.get(), {})
        report = snapshot(self.db, self.config)
        self.assertEqual(report['total']['operation_errors'], 1)
        self.assertNotIn('private-details', json.dumps(report))

    def test_existing_cache_hits_are_preserved_without_counting_new_hits_twice(self):
        old = (datetime.now(timezone.utc)-timedelta(minutes=10)).isoformat()
        self.db.execute("INSERT INTO cache_events(stage,created_at) VALUES('collector',?)", (old,))
        self.db.commit()
        work = Work('collector', lambda: {'material_read': True, 'content': 'Прочитанный текст сообщения источника.'}, key='cached', ttl=60, stage='source_read')
        work.execute(self.runtime)
        work.execute(self.runtime)
        report = snapshot(self.db, self.config)
        self.assertEqual(report['total']['cache_hits'], 2)
        self.assertEqual(report['total']['legacy_cache_hits'], 1)
        self.assertEqual(sum(r['cache_hits'] for r in report['stages']), 2)

    def test_runtime_migration_preserves_old_receipt_and_leaves_stage_unknown(self):
        old_path = str(Path(self.path).with_name('old.db'))
        old_db = sqlite3.connect(old_path)
        old_db.execute('CREATE TABLE api_usage(call_id TEXT PRIMARY KEY,role TEXT,category TEXT,item_id INTEGER,job_id INTEGER,model TEXT,status TEXT,created_at TEXT,finished_at TEXT,elapsed_seconds REAL,input_tokens INTEGER,cached_input_tokens INTEGER,output_tokens INTEGER,search_calls INTEGER,estimated_usd REAL,error_code TEXT)')
        old_db.execute("INSERT INTO api_usage(call_id,role,category,model,status,created_at) VALUES('legacy','filter','fresh','test','ERROR',?)", (datetime.now(timezone.utc).isoformat(),))
        old_db.commit(); old_db.close()
        migrated = connect(old_path)
        self.addCleanup(migrated.close)
        row = migrated.execute('SELECT call_id,stage,transport_attempt FROM api_usage').fetchone()
        self.assertEqual(tuple(row), ('legacy', None, None))
        self.assertEqual(snapshot(migrated, self.config)['total']['unknown_usage'], 1)

    def test_dashboard_serves_resource_page_and_readonly_report(self):
        from newsroom import dashboard
        self.call()
        ready = threading.Event()
        servers = []
        def make_server(address, handler):
            server = ThreadingHTTPServer(('127.0.0.1', 0), handler)
            servers.append(server); ready.set()
            return server
        with patch('newsroom.dashboard.ThreadingHTTPServer', side_effect=make_server):
            thread = threading.Thread(target=dashboard.serve, args=(self.config,), daemon=True)
            thread.start()
            self.assertTrue(ready.wait(3))
            server = servers[0]
            try:
                base = 'http://127.0.0.1:'+str(server.server_port)
                with urllib.request.urlopen(base+'/?view=resources', timeout=3) as response:
                    page = response.read().decode()
                self.assertIn('id="view-resources" class="view active"', page)
                self.assertIn('<h1 id="heading">Расход ресурсов</h1>', page)
                version = self.db.execute('PRAGMA schema_version').fetchone()[0]
                with urllib.request.urlopen(base+'/api/resources?hours=1', timeout=3) as response:
                    report = json.load(response)
                self.assertEqual(report['hours'], 1)
                self.assertEqual(report['total']['calls'], 1)
                self.assertEqual(version, self.db.execute('PRAGMA schema_version').fetchone()[0])
                with self.assertRaises(HTTPError) as invalid:
                    urllib.request.urlopen(base+'/api/resources?hours=bad', timeout=3)
                self.assertEqual(invalid.exception.code, 400)
                invalid.exception.close()
            finally:
                server.shutdown(); server.server_close(); thread.join(3)


if __name__ == '__main__':
    unittest.main()
