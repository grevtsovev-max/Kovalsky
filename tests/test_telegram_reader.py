import asyncio
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace, ModuleType
from unittest.mock import patch
from newsroom import telegram_reader as reader


class ReaderTests(unittest.TestCase):
    def test_missing_credentials_and_repository_session_rejected(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(ValueError, 'CONFIGURATION_REQUIRED'):
                reader._settings({})
        with patch.dict(os.environ, {'TELEGRAM_API_ID': '123', 'TELEGRAM_API_HASH': 'secret'}):
            with self.assertRaisesRegex(ValueError, 'OUTSIDE_REPOSITORY'):
                reader._settings({'session_file': str(Path(reader.__file__).resolve().parent / 'bad.session')})

    def test_read_pagination_links_and_disconnect(self):
        sessions = ModuleType('telethon.sessions')
        class Session:
            dc_id, server_address, port, auth_key = 1, 'server', 443, 'key'
            def __init__(self, *args): pass
            def set_dc(self, *args): pass
            def close(self): pass
        sessions.SQLiteSession = sessions.MemorySession = Session
        class Client:
            disconnected = False
            offsets = []
            async def connect(self): pass
            async def disconnect(self): self.disconnected = True
            async def is_user_authorized(self): return True
            async def get_me(self): return SimpleNamespace(bot=False)
            async def get_entity(self, channel): return SimpleNamespace(broadcast=True, username='rian_ru')
            async def iter_messages(self, entity, limit, offset_id):
                self.offsets.append(offset_id)
                if offset_id: return
                yield SimpleNamespace(id=22, date=datetime(2026,10,10,tzinfo=timezone.utc),
                    message='😀 https://example.com', entities=[SimpleNamespace(url='https://example.com')],
                    edit_date=None, fwd_from=None)
        client = Client()
        with tempfile.TemporaryDirectory() as directory:
            session = Path(directory)/'reader.session'; session.touch()
            with patch.dict(os.environ, {'TELEGRAM_API_ID': '123', 'TELEGRAM_API_HASH': 'secret'}), patch.dict('sys.modules', {'telethon.sessions': sessions}), patch.object(reader, '_client', return_value=client):
                batch = asyncio.run(reader._read('rian_ru', {'session_file':str(session)}, '2026-10-09T00:00:00+00:00', None, 2))
        self.assertEqual(client.offsets, [0,22])
        self.assertTrue(client.disconnected)
        self.assertIsNone(batch.recovery_before)
        self.assertEqual(batch[0]['url'], 'https://t.me/rian_ru/22')
        self.assertEqual(batch[0]['discovery_links'][0]['href'], 'https://example.com')

    def test_no_session_never_prompts_for_login(self):
        with patch.dict(os.environ, {'TELEGRAM_API_ID': '123', 'TELEGRAM_API_HASH': 'secret'}):
            with self.assertRaisesRegex(ValueError, 'AUTHORIZATION_REQUIRED'):
                reader.fetch('https://t.me/rian_ru', {'session_file':'/tmp/nonexistent-telegram-test.session'})

    def test_errors_do_not_expose_credentials(self):
        async def failing(*args): raise RuntimeError('secret account info')
        with patch.object(reader, '_read', failing):
            with self.assertRaisesRegex(ValueError, '^TELEGRAM_API_READ_FAILED$'):
                reader.fetch('https://t.me/rian_ru', {})

    def test_timeout_is_bounded(self):
        async def slow(*args): await asyncio.sleep(1)
        with patch.object(reader, '_read', slow):
            with self.assertRaises(TimeoutError):
                reader.fetch('https://t.me/rian_ru', {'timeout_seconds': .001})

    def test_api_errors_keep_diagnostic_codes(self):
        from newsroom.core import _safe_source_error
        self.assertEqual(_safe_source_error(ValueError('TELEGRAM_API_AUTHORIZATION_REQUIRED')),
                         'TELEGRAM_API_AUTHORIZATION_REQUIRED')
