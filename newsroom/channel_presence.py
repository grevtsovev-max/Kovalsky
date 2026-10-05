"""Read the actual channel before using a published post in a digest."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from html.parser import HTMLParser
import hashlib
import re
from urllib.parse import urlparse

from .edit_sync import ChannelTextParser


class ChannelPresenceUnavailable(RuntimeError):
    pass


class _WidgetError(HTMLParser):
    def __init__(self):
        super().__init__()
        self.depth = 0
        self.parts = []

    def handle_starttag(self, tag, attrs):
        if tag == 'div':
            if 'tgme_widget_message_error' in dict(attrs).get('class', '').split():
                self.depth = 1
            elif self.depth:
                self.depth += 1

    def handle_data(self, value):
        if self.depth:
            self.parts.append(value)

    def handle_endtag(self, tag):
        if tag == 'div' and self.depth:
            self.depth -= 1


def inspect_channel_posts(config, message_ids, *, db=None):
    from .cli import telegram_api
    from .core import _request_with_url
    from .delivery import channel
    ids = list(dict.fromkeys(str(value) for value in message_ids))
    if not ids:
        return {}
    if any(not value.isdigit() or int(value) <= 0 for value in ids):
        raise ChannelPresenceUnavailable('CHANNEL_MESSAGE_ID_INVALID')
    target = channel(config)
    chat = None
    for _ in range(3):
        try:
            chat = telegram_api(config, 'getChat', {'chat_id': target})
            break
        except Exception:
            pass
    username = (chat or {}).get('username', '')
    if (not re.fullmatch(r'[A-Za-z0-9_]+', username)
            or (target.startswith('@') and target[1:].casefold() != username.casefold())
            or (not target.startswith('@') and str((chat or {}).get('id')) != target)):
        raise ChannelPresenceUnavailable('CHANNEL_PUBLIC_SNAPSHOT_UNAVAILABLE')

    def read(message_id):
        url = f'https://t.me/{username}/{message_id}'
        for _ in range(3):
            try:
                raw, actual_url, _ = _request_with_url(url + '?embed=1', timeout=10, public_only=True)
                if (urlparse(actual_url).hostname != 't.me'
                        or urlparse(actual_url).path.strip('/').casefold() != f'{username}/{message_id}'.casefold()):
                    continue
                parser = ChannelTextParser(username)
                html = raw.decode('utf-8', errors='replace')
                parser.feed(html)
                text = parser.messages.get(message_id)
                error = _WidgetError()
                error.feed(html)
                if text:
                    status = 'PRESENT'
                elif ' '.join(error.parts).strip() in {'Post not found', 'Message not found'}:
                    status, text = 'DELETED', ' '.join(error.parts).strip()
                else:
                    continue
                return {'status': status, 'text': text, 'url': url, 'source_url': actual_url,
                        'content_sha256': hashlib.sha256(raw).hexdigest()}
            except Exception:
                pass
        return {'status': 'UNKNOWN', 'text': '', 'url': url, 'source_url': url + '?embed=1', 'content_sha256': ''}

    with ThreadPoolExecutor(max_workers=min(4, len(ids))) as workers:
        observations = dict(zip(ids, workers.map(read, ids)))
    if db is not None:
        stamp = datetime.now(timezone.utc).isoformat(timespec='seconds')
        for message_id, result in observations.items():
            db.execute('INSERT INTO channel_post_observations('
                'channel_id,message_id,status,source_url,text,content_sha256,observed_at) VALUES(?,?,?,?,?,?,?)',
                (str(chat['id']), message_id, result['status'], result['source_url'], result['text'],
                 result['content_sha256'], stamp))
        db.commit()
    if any(value['status'] == 'UNKNOWN' for value in observations.values()):
        raise ChannelPresenceUnavailable('CHANNEL_POST_PRESENCE_UNRESOLVED')
    return observations


def digest_links(messages):
    result = []
    for message in messages:
        for line in message.splitlines()[1:]:
            match = re.search(r'\[([^\]]+)\]\((https://t\.me/(?:c/\d+|[A-Za-z0-9_]+)/(\d+))\)', line)
            if match:
                result.append((line, match.group(2), match.group(3)))
    return result


def inspect_digest_messages(config, messages, *, db=None):
    """Return false for a deleted or edited entry; never infer deletion from absence."""
    from .cli import _telegram_message_url
    from .core import _limit_headline
    entries = digest_links(messages)
    observations = inspect_channel_posts(config, [entry[2] for entry in entries], db=db)
    for line, url, message_id in entries:
        observation = observations[message_id]
        if observation['status'] == 'DELETED':
            return False
        if url not in {observation['url'], _telegram_message_url(config, message_id)}:
            raise ChannelPresenceUnavailable('DIGEST_LINK_CHANNEL_MISMATCH')
        headline = re.sub(r'\*\*(.*?)\*\*', r'\1', observation['text'].splitlines()[0].strip())
        visible = re.sub(r'\[([^\]]+)\]\([^)]+\)', r'\1', line.split(' ', 1)[1])
        if visible != _limit_headline(headline):
            return False
    return True
