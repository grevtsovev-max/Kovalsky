"""Deterministic, lossless news parts backed by the normal delivery journal."""
from __future__ import annotations
import json
import re


def units(text):
    # Telegram counts the displayed text after formatting, in UTF-16 units.
    text = re.sub(r'\[([^\]\n]+)\]\((https?://[^\s)]+)\)', r'\1', text)
    text = text.replace('**', '')
    return len(text.encode('utf-16-le')) // 2


def split(text, limit=4096):
    limit = min(4096, int(limit))
    if units(text) <= limit:
        return [text]
    headline, separator, rest = text.partition('\n\n')
    body, footer_separator, footer = rest.rpartition('\n\n')
    if not separator or not footer_separator or not footer.startswith(('Источник:', 'Источники:')):
        raise ValueError('NEWS_SERIES_FORMAT')
    budget = limit - units(headline + '\n\n' + footer) - 64
    if budget < 32:
        raise ValueError('NEWS_SERIES_HEADER_OR_FOOTER_TOO_LONG')
    chunks = []
    current = ''
    # Links are indivisible tokens; every original non-whitespace character is
    # retained. Prefer paragraphs, then sentences, then whitespace boundaries.
    for paragraph in body.split('\n\n'):
        blocks = [paragraph] if units(paragraph) <= budget else re.split(r'(?<=[.!?])\s+', paragraph)
        for block in blocks:
            tokenized = units(block) > budget
            tokens = re.findall(r'\[[^\]\n]+\]\(https?://[^\s)]+\)|\S+', block) if tokenized else [block]
            for token_index, token in enumerate(tokens):
                if units(token) > budget:
                    raise ValueError('NEWS_SERIES_UNSPLITTABLE_FRAGMENT')
                separator = ' ' if tokenized and token_index else '\n\n'
                candidate = current + (separator if current else '') + token
                if units(candidate) > budget:
                    chunks.append(current)
                    current = token
                else:
                    current = candidate
    if current:
        chunks.append(current)
    parts = [f'{headline} · {i}/{len(chunks)}\n\n{chunk}\n\n{footer}' for i, chunk in enumerate(chunks, 1)]
    if any(units(part) > limit for part in parts):
        raise ValueError('NEWS_SERIES_LIMIT')
    return parts


def deliver_series(db, config, post_id, text, send, limit=4096):
    from .delivery import channel, deliver, DeliveryRejected
    parts = split(text, limit)
    if len(parts) == 1:
        return deliver(db, config, f'post:{post_id}', text, send, post_id=post_id)
    target = channel(config)
    db.commit()
    db.execute('BEGIN IMMEDIATE')
    try:
        existing = db.execute('SELECT * FROM news_series WHERE post_id=?', (post_id,)).fetchone()
        if existing:
            if existing['channel_id'] != target or existing['original_text'] != text:
                raise DeliveryRejected('NEWS_SERIES_CHANGED_OR_DESTINATION_CHANGED')
            parts = json.loads(existing['parts_json'])
        else:
            db.execute('INSERT INTO news_series(post_id,channel_id,original_text,parts_json,created_at) VALUES(?,?,?,?,?)',
                       (post_id, target, text, json.dumps(parts, ensure_ascii=False), __import__('newsroom.delivery', fromlist=['now']).now()))
        db.commit()
    except Exception:
        db.rollback()
        raise
    receipts = []
    for index, part in enumerate(parts):
        receipts.append(deliver(db, config, f'post:{post_id}:part:{index}', part, send,
                                post_id=post_id, verified_text=text))
    return receipts[0]
