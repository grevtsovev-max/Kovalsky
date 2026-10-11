"""Opt-in MTProto collection; credentials and sessions stay outside the release."""
import asyncio
import getpass
import re
import os
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit


def _settings(settings):
    api_id = os.environ.get(settings.get('api_id_env', 'TELEGRAM_API_ID'), '')
    api_hash = os.environ.get(settings.get('api_hash_env', 'TELEGRAM_API_HASH'), '')
    session = Path(settings.get('session_file', '')).expanduser()
    if not api_id.isdigit() or not api_hash or not session.is_absolute():
        raise ValueError('TELEGRAM_API_CONFIGURATION_REQUIRED')
    if session.suffix != '.session':
        raise ValueError('TELEGRAM_API_SESSION_EXTENSION_REQUIRED')
    repo = Path(__file__).resolve().parent.parent
    if session == repo or repo in session.resolve().parents:
        raise ValueError('TELEGRAM_SESSION_MUST_BE_OUTSIDE_REPOSITORY')
    return int(api_id), api_hash, session


def _client(settings, session):
    try:
        from telethon import TelegramClient
    except ImportError:
        raise ValueError('TELEGRAM_API_DEPENDENCY_REQUIRED') from None
    api_id, api_hash, _ = _settings(settings)
    return TelegramClient(str(session) if isinstance(session, Path) else session, api_id, api_hash, receive_updates=False,
                          request_retries=0, connection_retries=0, flood_sleep_threshold=0)


async def _read(channel, settings, since, before, max_pages):
    from .core import TelegramBatch
    _, _, session = _settings(settings)
    if not session.is_file():
        raise ValueError('TELEGRAM_API_AUTHORIZATION_REQUIRED')
    # Copy authorization into memory: concurrent readers never write the session database.
    from telethon.sessions import SQLiteSession, MemorySession
    stored = SQLiteSession(str(session))
    memory = MemorySession()
    try:
        memory.set_dc(stored.dc_id, stored.server_address, stored.port)
        memory.auth_key = stored.auth_key
    finally:
        stored.close()
    client = _client(settings, memory)
    batch = TelegramBatch()
    cutoff = datetime.fromisoformat(since.replace('Z', '+00:00')) if since else None
    if cutoff and cutoff.tzinfo is None:
        cutoff = cutoff.replace(tzinfo=timezone.utc)
    try:
        await client.connect()
        if not await client.is_user_authorized():
            raise ValueError('TELEGRAM_API_AUTHORIZATION_REQUIRED')
        account = await client.get_me()
        if getattr(account, 'bot', False):
            raise ValueError('TELEGRAM_API_USER_ACCOUNT_REQUIRED')
        entity = await client.get_entity(channel)
        if not getattr(entity, 'broadcast', False) or not getattr(entity, 'username', None):
            raise ValueError('TELEGRAM_API_PUBLIC_CHANNEL_REQUIRED')
        async def page(offset):
            oldest = None
            reached = False
            async for message in client.iter_messages(entity, limit=100, offset_id=offset):
                oldest = message.id
                if cutoff and message.date < cutoff:
                    reached = True
                    break
                text = message.message or ''
                if not text:
                    continue
                url = f'https://t.me/{entity.username}/{message.id}'
                links = []
                for item in message.entities or []:
                    href = getattr(item, 'url', None)
                    if type(item).__name__ == 'MessageEntityUrl':
                        raw = text.encode('utf-16-le')
                        href = raw[item.offset*2:(item.offset+item.length)*2].decode('utf-16-le')
                    if href:
                        links.append({'href': href, 'text': ''})
                batch.append({'url': url, 'title': text.splitlines()[0][:160],
                              'description': text, 'content': text,
                              'published_at': message.date.isoformat(timespec='seconds'),
                              'updated_at': message.edit_date.isoformat(timespec='seconds') if message.edit_date else None,
                              'discovery_links': links, 'telegram_forwarded': bool(message.fwd_from),
                              'material_read': True, 'material_url': url})
            return oldest, reached
        cursor = 0
        for index in range(max(2, int(max_pages))):
            try:
                oldest, reached = await page(cursor)
            except Exception:
                if not cursor:
                    raise
                batch.recovery_before = cursor
                batch.recovery_error = 'TELEGRAM_API_READ_FAILED'
                return batch
            if index == 0 and before:
                cursor = int(before)
            elif reached or oldest is None or oldest <= 1 or since is None:
                batch.recovery_before = None
                return batch
            else:
                cursor = oldest
            batch.recovery_before = cursor
        return batch
    finally:
        await client.disconnect()


def fetch(channel_url, settings, since=None, before=None, max_pages=5):
    channel = urlsplit(channel_url).path.strip('/').split('/')[-1]
    try:
        return asyncio.run(asyncio.wait_for(_read(channel, settings, since, before, max_pages),
                                          timeout=float(settings.get('timeout_seconds', 45))))
    except (ValueError, TimeoutError):
        raise
    except Exception:
        raise ValueError('TELEGRAM_API_READ_FAILED') from None


def login(settings):
    """Interactive login only; the background collector never asks for a code."""
    _, _, session = _settings(settings)
    session.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    async def authorize():
        client = _client(settings, session)
        try:
            from telethon.errors import SessionPasswordNeededError
            await client.connect()
            if not await client.is_user_authorized():
                phone = getpass.getpass('Номер пользовательского аккаунта Telegram (+код страны, ввод скрыт): ').strip()
                if not re.fullmatch(r'\+[0-9]{7,15}', phone):
                    raise ValueError('TELEGRAM_API_PHONE_NUMBER_REQUIRED')
                await client.send_code_request(phone)
                code = getpass.getpass('Код входа из Telegram (ввод скрыт): ').strip()
                try:
                    await client.sign_in(phone=phone, code=code)
                except SessionPasswordNeededError:
                    await client.sign_in(password=getpass.getpass('Пароль двухэтапной защиты (ввод скрыт): '))
            account = await client.get_me()
            if not account or getattr(account, 'bot', False):
                raise ValueError('TELEGRAM_API_USER_ACCOUNT_REQUIRED')
        finally:
            await client.disconnect()
    old_mask = os.umask(0o077)
    try:
        asyncio.run(authorize())
    finally:
        os.umask(old_mask)
        if session.exists():
            session.chmod(0o600)
