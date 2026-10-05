"""Correct an existing digest through evidence checks and durable edit delivery."""
from datetime import date, datetime, timezone
import hashlib
import json
import re
from zoneinfo import ZoneInfo

from .channel_presence import (ChannelPresenceUnavailable, digest_links,
                               inspect_channel_posts, inspect_digest_messages)
from .db import connect
from .delivery import (DeliveryRejected, DeliveryUncertain, TelegramReceipt,
                       channel, confirm, deliver)
from .quality import digest_issues


def repair_digest(db_path, config, kind='daily', local_date=None):
    from .cli import _link_digest_action, _telegram_message_url, telegram_api, telegram_format_text
    from .core import _limit_headline
    if kind not in {'daily', 'weekly'}:
        raise ValueError('Invalid digest kind')
    day = date.fromisoformat(local_date) if local_date else datetime.now(ZoneInfo('Europe/Moscow')).date()
    prefix = 'digest' if kind == 'daily' else 'weekly_digest'
    batch_key = channel(config) + ':' + prefix + ':' + day.isoformat()
    db = connect(db_path)
    results = []
    try:
        batch = db.execute('SELECT * FROM digest_batches WHERE batch_key=?', (batch_key,)).fetchone()
        if not batch:
            raise DeliveryRejected('Digest batch not found')
        for part, _ in enumerate(json.loads(batch['messages_json'])):
            receipt = db.execute('SELECT * FROM publication_attempts WHERE delivery_key=?',
                                 (batch_key + ':' + str(part),)).fetchone()
            if not receipt or receipt['status'] not in {'SENT', 'CONFIRMED'} or not receipt['telegram_message_id']:
                raise DeliveryUncertain('Digest has no confirmed original message to edit')
            message_id = receipt['telegram_message_id']
            for _ in range(3):
                current = inspect_channel_posts(config, [message_id], db=db)[message_id]
                if current['status'] == 'DELETED':
                    results.append({'part': part, 'status': 'DELETED', 'message_id': message_id})
                    break
                original = current['text']
                if digest_issues(original):
                    raise ChannelPresenceUnavailable('CURRENT_DIGEST_FORMAT_INVALID')
                entries = digest_links([original])
                sources = inspect_channel_posts(config, [entry[2] for entry in entries], db=db)
                lines = [original.splitlines()[0]]
                removed = 0
                for line, url, post_id in entries:
                    source = sources[post_id]
                    if url not in {source['url'], _telegram_message_url(config, post_id)}:
                        raise ChannelPresenceUnavailable('DIGEST_LINK_CHANNEL_MISMATCH')
                    if source['status'] == 'DELETED':
                        removed += 1
                        continue
                    headline = re.sub(r'\*\*(.*?)\*\*', r'\1', source['text'].splitlines()[0].strip())
                    emoji = line.split(' ', 1)[0]
                    lines.append(emoji + ' ' + _link_digest_action(_limit_headline(headline), url))
                if len(lines) == 1:
                    lines.append('За период дайджеста нет публикаций, подходящих для включения в подборку.')
                edited = '\n\n'.join(lines)
                if digest_issues(edited):
                    raise ChannelPresenceUnavailable('CORRECTED_DIGEST_FORMAT_INVALID')
                if not inspect_digest_messages(config, [edited], db=db):
                    continue
                before_edit = inspect_channel_posts(config, [message_id], db=db)[message_id]
                if before_edit['status'] != 'PRESENT' or before_edit['text'] != original:
                    continue
                if edited == original:
                    results.append({'part': part, 'status': 'UNCHANGED', 'message_id': message_id})
                    break
                operation = f'{prefix}-edit:{day.isoformat()}:{part}:' + hashlib.sha256(edited.encode()).hexdigest()[:16]
                stamp = datetime.now(timezone.utc).isoformat(timespec='seconds')
                db.execute('INSERT OR IGNORE INTO digest_edits('
                    'batch_key,part,operation,original_text,edited_text,telegram_message_id,status,created_at,updated_at) '
                    "VALUES(?,?,?,?,?,?,'PREPARED',?,?)", (batch_key, part, operation, original, edited, message_id, stamp, stamp))
                db.commit()

                def send_edit(settings, text):
                    result = telegram_api(settings, 'editMessageText', {
                        'chat_id': channel(settings), 'message_id': int(message_id),
                        'text': telegram_format_text(text), 'parse_mode': 'HTML',
                        'disable_web_page_preview': True})
                    if not isinstance(result, dict) or str(result.get('message_id')) != message_id:
                        raise DeliveryUncertain('Digest edit receipt has a different message ID')
                    return TelegramReceipt(result)

                try:
                    deliver(db, config, operation, edited, send_edit)
                    confirm(db, config, operation)
                    status = 'CONFIRMED'
                except DeliveryRejected:
                    status = 'FAILED'
                except DeliveryUncertain:
                    status = 'UNKNOWN'
                db.execute('UPDATE digest_edits SET status=?,updated_at=? WHERE operation=?',
                           (status, datetime.now(timezone.utc).isoformat(timespec='seconds'), operation))
                db.commit()
                results.append({'part': part, 'status': status, 'message_id': message_id, 'removed': removed})
                break
            else:
                raise ChannelPresenceUnavailable('DIGEST_CHANGED_DURING_CORRECTION')
        return {'batch_key': batch_key, 'parts': results}
    finally:
        db.close()
