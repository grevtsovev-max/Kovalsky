import json
import unittest
import threading
import urllib.request
from urllib.error import URLError
from http.server import ThreadingHTTPServer
from unittest.mock import patch

import test_workflow as workflow_tests
from newsroom.core import process_item, _save_item, _safe_source_error
from newsroom.diagnostics import snapshot, error_location


class RuntimeRepairTests(unittest.TestCase):
    setUp = workflow_tests.WorkflowTests.setUp
    item = workflow_tests.WorkflowTests.item
    noise = workflow_tests.WorkflowTests.noise

    def test_null_previous_feedback_does_not_stop_editor_or_consume_error_retry(self):
        item = self.item()
        item_id = _save_item(self.db, self.source, item)
        self.db.execute('INSERT INTO item_analysis(item_id,model,created_at,result_json) VALUES(?,?,?,?)',
                        (item_id, 'test', self.now, json.dumps({'editorial_issues': None, 'memory_issues': None})))
        self.db.commit()
        with patch('newsroom.core.get_api_key', return_value='test'), \
             patch('newsroom.core.analyze_with_ai', side_effect=self.noise) as editor:
            result = process_item(self.db, self.source, item, **self.options,
                                  existing_item_id=item_id, ai_settings=self.config['ai'])
        self.assertEqual(result, 'NOISE')
        self.assertEqual(editor.call_args.args[0]['editorial_feedback'], [])
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM errors').fetchone()[0], 0)

    def test_diagnostics_read_existing_schema_and_hide_private_settings(self):
        self.config['ai']['api_key'] = 'private-key'
        self.config['telegram'] = {'bot_token': 'private-token'}
        schema_before = self.db.execute('PRAGMA schema_version').fetchone()[0]
        result = snapshot(self.db, self.config)
        encoded = json.dumps(result)
        self.assertNotIn('private-key', encoded)
        self.assertNotIn('private-token', encoded)
        self.assertNotIn(self.path, encoded)
        self.assertEqual(result['processing_workers'], 2)
        self.assertIn('job_id', result['schemas']['processing_jobs'])
        self.assertEqual(schema_before, self.db.execute('PRAGMA schema_version').fetchone()[0])

    def test_error_locations_never_include_exception_text_or_arguments(self):
        try:
            raise TypeError('private-key secret article text')
        except TypeError as exc:
            result = error_location(exc)
        self.assertEqual(result['type'], 'TypeError')
        self.assertNotIn('private-key', json.dumps(result))

    def test_tls_handshake_timeout_is_a_timeout_not_a_certificate_failure(self):
        error = URLError(TimeoutError('_ssl.c: handshake operation timed out'))
        self.assertEqual(_safe_source_error(error), 'NETWORK_TIMEOUT')
        self.assertEqual(_safe_source_error(RuntimeError('certificate verify failed')), 'TLS_CERTIFICATE_ERROR')
        self.assertEqual(_safe_source_error(RuntimeError('TLS connection aborted')), 'TLS_CONNECTION_ERROR')

    def test_live_dashboard_reads_queue_and_diagnostics_without_schema_writes(self):
        from newsroom.dashboard import serve
        _save_item(self.db, self.source, self.item())
        self.db.commit()
        ready = threading.Event()
        servers = []
        errors = []
        def factory(address, handler):
            server = ThreadingHTTPServer(('127.0.0.1', 0), handler)
            servers.append(server)
            ready.set()
            return server
        def run():
            try:
                serve(self.config)
            except Exception as exc:
                errors.append(exc)
        schema = self.db.execute('PRAGMA schema_version').fetchone()[0]
        with patch('newsroom.dashboard.ThreadingHTTPServer', side_effect=factory):
            thread = threading.Thread(target=run)
            thread.start()
            try:
                self.assertTrue(ready.wait(3))
                base = 'http://127.0.0.1:' + str(servers[0].server_port)
                for path in ('news', 'pipeline', 'diagnostics'):
                    with urllib.request.urlopen(base+'/api/'+path, timeout=3) as response:
                        data = json.load(response)
                    self.assertNotIn('error', data)
            finally:
                if servers:
                    servers[0].shutdown()
                    servers[0].server_close()
                thread.join(4)
        self.assertEqual(errors, [])
        self.assertEqual(schema, self.db.execute('PRAGMA schema_version').fetchone()[0])


if __name__ == '__main__':
    unittest.main()
