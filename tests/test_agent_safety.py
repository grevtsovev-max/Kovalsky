import json
import unittest
import urllib.request
from unittest.mock import Mock, patch

from newsroom.agent import run_research_agent
from newsroom.ai import AIResponseError
from newsroom.core import (_PublicHttpsConnection, _PublicHttpsRedirectHandler,
                           _request_with_url, _validate_public_http_url)


class AgentSafetyTests(unittest.TestCase):
    def test_private_and_non_https_targets_are_rejected_before_network_request(self):
        urls = ("https://127.0.0.1/", "https://10.1.2.3/", "https://169.254.169.254/",
                "https://[::1]/", "http://example.org/", "https://user:pass@example.org/",
                "https://example.org:8443/")
        with patch("newsroom.core.urllib.request.build_opener") as opener:
            for url in urls:
                with self.subTest(url=url), self.assertRaises(ValueError):
                    _request_with_url(url, public_only=True)
            opener.assert_not_called()

    def test_hostname_resolving_to_private_address_is_rejected(self):
        answer = [(2, 1, 6, "", ("192.168.1.2", 443))]
        with patch("newsroom.core.socket.getaddrinfo", return_value=answer):
            with self.assertRaisesRegex(ValueError, "URL_NOT_PUBLIC"):
                _validate_public_http_url("https://source.example/article")

    def test_redirect_to_private_address_is_rejected(self):
        request = urllib.request.Request("https://source.example/article")
        with self.assertRaisesRegex(ValueError, "URL_NOT_PUBLIC"):
            _PublicHttpsRedirectHandler().redirect_request(
                request, None, 302, "Found", {}, "https://127.0.0.1/internal")

    def test_connection_pins_checked_ip_and_preserves_tls_hostname(self):
        context = Mock()
        connection = _PublicHttpsConnection("source.example", timeout=8, context=context)
        public = [(2, 1, 6, "", ("8.8.8.8", 443))]
        with patch("newsroom.core.socket.getaddrinfo", return_value=public) as dns, \
             patch("newsroom.core.socket.create_connection") as connect_socket:
            connection.connect()
        dns.assert_called_once()
        self.assertEqual(connect_socket.call_args.args[0], ("8.8.8.8", 443))
        self.assertEqual(context.wrap_socket.call_args.kwargs["server_hostname"], "source.example")

    def test_connection_rechecks_dns_before_connecting(self):
        private = [(2, 1, 6, "", ("127.0.0.1", 443))]
        with patch("newsroom.core.socket.getaddrinfo", return_value=private), \
             patch("newsroom.core.socket.create_connection") as connect_socket:
            with self.assertRaisesRegex(ValueError, "URL_NOT_PUBLIC"):
                _PublicHttpsConnection("source.example", timeout=8).connect()
        connect_socket.assert_not_called()

    def test_model_cannot_call_publication_or_arbitrary_tools(self):
        action = {"output": [{"type": "function_call", "call_id": "test-call",
                              "name": "publish", "arguments": "{}"}]}
        handler = Mock()
        with patch("newsroom.agent.request_response", return_value=action):
            with self.assertRaisesRegex(AIResponseError, "AGENT_INVALID_ACTION"):
                run_research_agent({}, {}, {"publish": handler})
        handler.assert_not_called()

    def test_additional_model_arguments_do_not_reach_handler(self):
        action = {"output": [{"type": "function_call", "call_id": "test-call",
                  "name": "search_web", "arguments": json.dumps({
                      "query": "recent public news", "purpose": "NEWS",
                      "rationale": "Find recent evidence", "command": "unexpected"})}]}
        handler = Mock()
        with patch("newsroom.agent.request_response", return_value=action):
            with self.assertRaisesRegex(AIResponseError, "AGENT_INVALID_SEARCH_ARGUMENTS"):
                run_research_agent({}, {}, {"search_web": handler})
        handler.assert_not_called()


if __name__ == "__main__":
    unittest.main()
