import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import urllib.request

from newsroom import google_apps_script as bridge, topic_registry as topics
from newsroom.db import connect
from newsroom.source_registry import save, state


class AppsScriptTests(unittest.TestCase):
    def setUp(self):
        csv_read = patch('newsroom.core._request_with_url', return_value=('Тема,Слово или фраза,Мониторинг\n'.encode(), {}, ''))
        csv_read.start(); self.addCleanup(csv_read.stop)
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.db = connect(str(Path(self.temp.name) / 'state.db')); self.addCleanup(self.db.close)
        self.settings = {'spreadsheet_id': 'table', 'tabs': [{'name': 'Ключевые слова', 'gid': '2'}]}
        save(self.db, topics.SETTINGS, self.settings); self.db.commit()
        self.credentials = {'url': 'https://script.google.com/macros/s/' + 'x'*30 + '/exec', 'secret': 'a'*64}

    def test_flat_header_permission_probe_preserves_first_column(self):
        with patch('newsroom.core._request_with_url', return_value=('Ключевик,Мониторинг,Роль,Уточнение\n'.encode(), {}, '')):
            probe=topics.keyword_header_probe(self.settings)
        self.assertEqual(probe['find'],'Ключевик')
        self.assertEqual(probe['replacement'],'Ключевик')
        self.assertEqual(probe['range']['startColumnIndex'],0)

    def test_reject_untrusted_urls_and_invalid_secrets(self):
        for url in ['http://script.google.com/macros/s/'+'x'*30+'/exec', 'https://example.org/exec',
                    self.credentials['url']+'?secret=x', self.credentials['url'].replace('/exec','/dev')]:
            with self.assertRaises(ValueError): bridge.validate({**self.credentials, 'url': url})
        with self.assertRaises(ValueError): bridge.validate({**self.credentials, 'secret': 'short'})

    def test_activate_only_after_verified_write_store_secret_outside_database(self):
        result = {'replies': [{'findReplace': {'occurrencesChanged': 1}}]}
        with patch.object(bridge, 'request', return_value=result) as request:
            bridge.configure(self.db, self.credentials)
        self.assertEqual(request.call_args.args[3]['requests'][0]['findReplace']['find'], 'Слово или фраза')
        settings = state(self.db, topics.SETTINGS)
        path = Path(settings['apps_script_file'])
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(bridge.read_credentials(path), self.credentials)
        self.assertNotIn(self.credentials['secret'], str(list(self.db.iterdump())))
        self.assertTrue(topics.credentials_available(settings))
        with patch.object(bridge, 'request', return_value={'done': True}) as request:
            self.assertEqual(topics.api(settings, 'GET', '?fields=x'), {'done': True})
            self.assertEqual(request.call_args.args[1:], ('GET', '?fields=x', None))

    def test_unchanged_header_does_not_require_changed_occurrences(self):
        with patch.object(bridge, 'request', return_value={'replies': [{'findReplace': {}}]}):
            self.assertTrue(bridge.configure(self.db, self.credentials)['writing_verified'])

    def test_failed_verification_does_not_activate_or_store(self):
        for result in [{'replies': [{}]}, {'replies': []}]:
            with patch.object(bridge, 'request', return_value=result):
                with self.assertRaises(ValueError): bridge.configure(self.db, self.credentials)
        self.assertNotIn('apps_script_file', state(self.db, topics.SETTINGS))
        self.assertFalse((Path(self.temp.name)/'google-apps-script.json').exists())

    def test_private_file_checks_and_symlink_destination(self):
        path = Path(bridge.store_credentials(self.db, self.credentials))
        os.chmod(path, 0o644)
        with self.assertRaises(ValueError): bridge.read_credentials(path)
        path.unlink(); path.symlink_to(Path(self.temp.name)/'other')
        with self.assertRaises(ValueError): bridge.store_credentials(self.db, self.credentials)
        with self.assertRaises(OSError): bridge.read_credentials(path)

    def test_redirects_are_restricted_to_google_content(self):
        handler = bridge.GoogleRedirects()
        req = urllib.request.Request(self.credentials['url'], data=b'private', method='POST')
        for target in ['http://script.googleusercontent.com/macros/echo', 'https://evil.example/echo',
                       'https://script.google.com/other', 'https://script.googleusercontent.com:444/echo']:
            with self.assertRaises(ValueError): handler.redirect_request(req, None, 302, '', {}, target)
        redirected = handler.redirect_request(req, None, 302, '', {}, 'https://script.googleusercontent.com/macros/echo')
        self.assertIsNone(redirected.data)
        self.assertEqual(redirected.get_method(), 'GET')

    def test_slow_google_requests_have_time_to_finish_and_errors_are_safe(self):
        class Response:
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def read(self, limit): return json.dumps({'ok': False, 'error': 'ACCESS_DENIED', 'private': 'secret'}).encode()
        with patch('urllib.request.build_opener') as opener:
            opener.return_value.open.return_value = Response()
            with self.assertRaisesRegex(RuntimeError, '^APPS_SCRIPT_ACCESS_DENIED$'):
                bridge.request(self.settings, 'GET', '?fields=x', credentials=self.credentials)
            self.assertEqual(opener.return_value.open.call_args.kwargs['timeout'], 90)
            opener.return_value.open.side_effect = TimeoutError('private redirect')
            with self.assertRaisesRegex(RuntimeError, '^APPS_SCRIPT_REQUEST_TIMEOUT$'):
                bridge.request(self.settings, 'GET', '?fields=x', credentials=self.credentials)

    def test_wrong_table_or_failed_bridge_does_not_return_google_error_body(self):
        class Response:
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def read(self, limit): return json.dumps({'ok': True, 'spreadsheet_id': 'wrong', 'result': {'secret': 'private'}}).encode()
        with patch('urllib.request.build_opener') as opener:
            opener.return_value.open.return_value = Response()
            with self.assertRaisesRegex(RuntimeError, '^APPS_SCRIPT_REQUEST_FAILED$'):
                bridge.request(self.settings, 'GET', '?fields=x', credentials=self.credentials)


if __name__ == '__main__': unittest.main()
