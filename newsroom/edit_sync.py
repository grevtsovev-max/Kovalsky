"""Recover missed edits from the actual public channel; never fabricate bot events."""
from html.parser import HTMLParser
import json
import os
import re
import time
from datetime import datetime, timezone


class ChannelTextParser(HTMLParser):
    def __init__(self, username):
        super().__init__(convert_charrefs=True)
        self.username = username.casefold()
        self.post = None
        self.depth = 0
        self.parts = []
        self.links = []
        self.messages = {}

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if 'data-post' in attrs:
            self.post = attrs['data-post']
        if tag == 'div' and 'tgme_widget_message_text' in attrs.get('class', '').split():
            self.depth = 1
            self.parts, self.links = [], []
        elif self.depth:
            if tag == 'div':
                self.depth += 1
            elif tag == 'br':
                self.parts.append('\n')
            elif tag == 'a':
                self.links.append((len(self.parts), attrs.get('href', '')))

    def handle_data(self, data):
        if self.depth:
            self.parts.append(data)

    def handle_endtag(self, tag):
        if not self.depth:
            return
        if tag == 'a' and self.links:
            start, url = self.links.pop()
            label = ''.join(self.parts[start:])
            # Autolinks/mentions are plain text in Bot API; only text links use Markdown.
            if url.startswith(('https://', 'http://')) and label != url and not label.startswith(('@', '#')):
                self.parts[start:] = [f'[{label}]({url})']
        elif tag == 'div':
            self.depth -= 1
            if not self.depth and self.post:
                channel, _, message_id = self.post.partition('/')
                if channel.casefold() == self.username and message_id.isdigit():
                    self.messages[message_id] = ''.join(self.parts).strip()


def sync_recent_channel_edits(config, db, *, force=False, post_ids=None):
    from .cli import telegram_api
    from .core import _request_with_url
    from .review import capture_channel_edit
    stamp = time.time()
    previous = db.execute("SELECT value FROM app_state WHERE key='channel_edit_sync_at'").fetchone()
    if not force and previous and stamp-float(previous[0]) < 60:
        return 0
    started = db.execute("SELECT value FROM app_state WHERE key='channel_edit_sync_started_at'").fetchone()
    started_at = started[0] if started else datetime.now(timezone.utc).isoformat()
    db.execute("INSERT OR IGNORE INTO app_state(key,value) VALUES('channel_edit_sync_started_at',?)",(started_at,))
    db.commit()
    settings = config.get('telegram', {})
    target = os.getenv(settings.get('chat_id_env','TELEGRAM_CHAT_ID')) or settings.get('chat_id')
    chat = telegram_api(config,'getChat',{'chat_id':target})
    username = chat.get('username','')
    if not re.fullmatch(r'[A-Za-z0-9_]+',username):
        return 0  # Private channels have no public snapshot endpoint.
    rows = db.execute("SELECT post_id,external_id,published_at FROM posts WHERE status='PUBLISHED' AND external_id IS NOT NULL ORDER BY published_at DESC LIMIT 20").fetchall()
    selected = None if post_ids is None else set(post_ids)
    rows = [r for r in rows if selected is None or r['post_id'] in selected]
    if not rows:
        return 0
    url = f'https://t.me/s/{username}'
    raw, _, _ = _request_with_url(url,timeout=10)
    parser = ChannelTextParser(username)
    parser.feed(raw.decode('utf-8',errors='replace'))
    count = 0
    for row in rows:
        message_id = str(row['external_id'])
        text = parser.messages.get(message_id)
        if not text:
            continue
        baseline = db.execute('SELECT text FROM telegram_public_snapshots WHERE post_id=?',(row['post_id'],)).fetchone()
        # Initial baseline is not an edit. Recover historical changes only when
        # explicitly selected, and observe subsequent changes to older posts.
        if baseline or selected is not None or (row['published_at'] or '') >= started_at:
            capture = capture_channel_edit(config,db,chat_id=chat['id'],message_id=message_id,
                                           edited_text=text,source_url=f'https://t.me/{username}/{message_id}',
                                           previous_text=baseline[0] if baseline and selected is None else None)
            count += capture is not None
        db.execute('INSERT OR REPLACE INTO telegram_public_snapshots(post_id,text,observed_at) VALUES(?,?,?)',
                   (row['post_id'],text,datetime.now(timezone.utc).isoformat()))
    db.execute("INSERT OR REPLACE INTO app_state(key,value) VALUES('channel_edit_sync_at',?)",(str(stamp),))
    db.commit()
    return count


def handle_persisted_update(config, db, update):
    from .review import handle_update
    now = datetime.now(timezone.utc).isoformat()
    update_id = int(update['update_id'])
    db.execute('INSERT OR IGNORE INTO telegram_review_inbox(update_id,payload_json,received_at) VALUES(?,?,?)',
               (update_id,json.dumps(update,ensure_ascii=False),now))
    db.commit()
    if db.execute('SELECT processed_at FROM telegram_review_inbox WHERE update_id=?',(update_id,)).fetchone()[0]:
        return
    try:
        handle_update(config,db,update)
    except Exception as exc:
        db.rollback()
        db.execute('UPDATE telegram_review_inbox SET outcome=? WHERE update_id=?',(type(exc).__name__,update_id))
        db.commit()
        raise
    db.execute("UPDATE telegram_review_inbox SET processed_at=?,outcome='HANDLED' WHERE update_id=?",(now,update_id))
    db.commit()
