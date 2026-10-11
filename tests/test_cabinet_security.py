import io
import unittest
from email.message import Message
from types import SimpleNamespace
from unittest.mock import Mock, patch

from newsroom import cabinet_server


class CabinetSecurityTests(unittest.TestCase):
    def setUp(self):
        captured = {}

        def server_factory(address, handler):
            captured['handler'] = handler
            return Mock()

        with patch.object(cabinet_server, 'ThreadingHTTPServer', server_factory):
            cabinet_server.serve({'newsroom': {'database': ':memory:'}})
        self.handler = captured['handler'].__new__(captured['handler'])
        self.handler.server = SimpleNamespace(server_port=8765)
        self.handler.client_address = ('127.0.0.1', 12345)
        self.handler.headers = Message()
        self.handler.headers['Host'] = 'localhost:8765'
        self.handler.path = '/api/summary'
        self.handler._json = Mock()
        self.handler._summary = Mock(return_value={'materials': 0})

    def test_read_requires_token_before_touching_data(self):
        self.handler.do_GET()
        self.handler._json.assert_called_once_with({'error': 'Нет доступа'}, 403)
        self.handler._summary.assert_not_called()

    def test_root_issues_token_only_for_local_authority(self):
        self.handler.path = '/'
        self.handler.headers.replace_header('Host', 'attacker.example:8765')
        self.handler.do_GET()
        self.handler._json.assert_called_once_with({'error': 'Нет доступа'}, 403)

    def test_cross_origin_and_duplicate_host_are_rejected(self):
        self.handler.headers['Origin'] = 'https://attacker.example'
        self.assertFalse(self.handler._local_request())
        del self.handler.headers['Origin']
        self.handler.headers['Host'] = 'localhost:8765'
        self.assertFalse(self.handler._local_request())

    def test_normal_page_and_authenticated_read_work(self):
        self.handler.path = '/'
        self.handler.wfile = io.BytesIO()
        self.handler.send_response = Mock()
        self.handler.send_header = Mock()
        self.handler.end_headers = Mock()
        self.handler.do_GET()
        page = self.handler.wfile.getvalue().decode()
        import re
        token = re.search(r"const TOKEN='([^']+)'", page).group(1)
        self.handler.headers['X-Dashboard-Token'] = token
        self.handler.path = '/api/summary'
        with patch('newsroom.topic_registry.attach_cached'):
            self.handler.do_GET()
        self.handler._json.assert_called_once_with({'materials': 0})

    def test_foreign_site_post_is_rejected_before_action(self):
        self.handler.headers['Sec-Fetch-Site'] = 'cross-site'
        self.handler.do_POST()
        self.handler._json.assert_called_once_with({'error': 'Нет доступа'}, 403)
