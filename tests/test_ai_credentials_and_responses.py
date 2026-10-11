import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from newsroom import ai


class FakeResponse:
    def __init__(self, body):
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False

    def read(self):
        return self.body


class AICredentialAndResponseTests(unittest.TestCase):
    def test_key_file_is_used_when_environment_key_is_empty(self):
        with tempfile.TemporaryDirectory() as directory:
            key_file = Path(directory) / "api-key"
            key_file.write_text("sk-test-file-key\n", encoding="utf-8")
            with patch.dict(os.environ, {"OPENAI_API_KEY": ""}):
                self.assertEqual(ai.get_api_key({"api_key_file": str(key_file)}), "sk-test-file-key")

    def test_environment_key_takes_precedence_over_key_file(self):
        with tempfile.TemporaryDirectory() as directory:
            key_file = Path(directory) / "api-key"
            key_file.write_text("sk-test-file-key", encoding="utf-8")
            with patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test-environment-key"}):
                self.assertEqual(ai.get_api_key({"api_key_file": str(key_file)}), "sk-test-environment-key")

    def _analyze_with_body(self, body):
        with patch("newsroom.ai.get_api_key", return_value="sk-test-key"), \
             patch("newsroom.ai.urllib.request.urlopen", return_value=FakeResponse(body)):
            return ai.request_response({'model': 'unit-test', 'input': 'Test'}, {})

    def test_malformed_api_response_has_safe_error_code(self):
        with self.assertRaises(ai.AIResponseError) as caught:
            self._analyze_with_body(b"not-json")
        self.assertEqual(caught.exception.code, "INVALID_RESPONSE_JSON")






if __name__ == "__main__":
    unittest.main()
