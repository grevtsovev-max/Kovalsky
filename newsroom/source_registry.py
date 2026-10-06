"""Read-only Google Sheets registry; publication rules remain in the pipeline."""
from __future__ import annotations

import csv
import io
import json
import re
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlsplit, urlencode

SETTINGS = 'source_registry_settings'
SNAPSHOT = 'source_registry_snapshot'
ATTEMPT = 'source_registry_attempt'
ERROR = 'source_registry_error'
CURSOR = 'source_registry_entity_cursor'


def entity_sources(rows):
    """Every row is also an observation target, including publishers."""
    names = {}
    for row in rows:
        name = re.sub(r'\s*\((?:RSS|Telegram)\)\s*$', '', row['name'], flags=re.I)
        # Search syntax comes from code; cells supply literal words only.
        for alias in re.split(r'\s+/\s+', name):
            alias = ' '.join(re.findall(r'[\w.-]+', alias, re.UNICODE))
            if alias:
                names.setdefault(alias.casefold(), alias)
    groups, group = [], []
    def source(batch):
        query = '(' + ' OR '.join('"' + name + '"' for name in batch) + ') (криптовалюта OR крипто OR блокчейн OR ЦФА OR bitcoin OR crypto) when:2d'
        url = 'https://news.google.com/rss/search?' + urlencode({'q': query, 'hl': 'ru', 'gl': 'RU', 'ceid': 'RU:ru'})
        return {'name': 'Упоминания: ' + ', '.join(batch)[:85], 'url': url,
                'type': 'google_news', 'active': True, 'priority': 1,
                'reputation': 'unknown', 'source_role': 'discovery',
                'registry_entities': list(batch)}
    for name in names.values():
        candidate = group + [name]
        if group and (len(candidate) > 8 or len(source(candidate)['url']) > 1800):
            groups.append(source(group))
            group = []
        group.append(name)
    if group:
        groups.append(source(group))
    return groups


def schedule_entities(db, config, rows):
    groups = entity_sources(rows)
    if not groups:
        return
    cursor = state(db, CURSOR, 0) % len(groups)
    count = min(4, len(groups))
    selected = [groups[(cursor + index) % len(groups)] for index in range(count)]
    existing = {s['url']: s for s in config.get('sources', [])}
    for source in selected:
        # Preserve an explicitly disabled matching discovery source.
        existing.setdefault(source['url'], source)
    config['sources'] = list(existing.values())
    save(db, CURSOR, (cursor + count) % len(groups))
    db.commit()


def state(db, key, default=None):
    row = db.execute('SELECT value FROM app_state WHERE key=?', (key,)).fetchone()
    return json.loads(row[0]) if row else default


def save(db, key, value):
    db.execute('INSERT INTO app_state(key,value) VALUES(?,?) '
               'ON CONFLICT(key) DO UPDATE SET value=excluded.value',
               (key, json.dumps(value, ensure_ascii=False)))


def validate_settings(payload):
    url = str(payload.get('url', ''))
    match = re.fullmatch(r'https://docs\.google\.com/spreadsheets/d/([A-Za-z0-9_-]{20,100})(?:/[^\s]*)?', url)
    tabs = payload.get('tabs')
    if not match or not isinstance(tabs, list) or not 1 <= len(tabs) <= 20:
        raise ValueError('Укажите ссылку Google Таблицы и список вкладок')
    checked = []
    for tab in tabs:
        name = str(tab.get('name', '')).strip()
        gid = str(tab.get('gid', ''))
        if not name or len(name) > 100 or not re.fullmatch(r'\d{1,12}', gid):
            raise ValueError('Некорректная вкладка')
        checked.append({'name': name, 'gid': gid})
    if len({t['gid'] for t in checked}) != len(checked):
        raise ValueError('Вкладка указана дважды')
    return {'url': f'https://docs.google.com/spreadsheets/d/{match[1]}/edit',
            'spreadsheet_id': match[1], 'tabs': checked}


def parse_tab(body, name):
    rows = list(csv.reader(io.StringIO(body.decode('utf-8-sig'))))
    if not rows or len(rows) > 2001:
        raise ValueError('REGISTRY_SIZE_OR_HEADER')
    headers = [v.strip().casefold() for v in rows[0]]
    if not headers or headers[0] not in {'название', 'имя', 'название или имя'} or 'ссылка' not in headers:
        raise ValueError('REGISTRY_HEADER')
    link_index = headers.index('ссылка')
    task_index = headers.index('что отслеживать') if 'что отслеживать' in headers else None
    result = []
    for number, row in enumerate(rows[1:], 2):
        if not any(v.strip() for v in row):
            continue
        title = row[0].strip()
        if not title or len(title) > 250:
            raise ValueError('REGISTRY_NAME')
        link = row[link_index].strip() if len(row) > link_index else ''
        task = row[task_index].strip() if task_index is not None and len(row) > task_index else ''
        result.append({'section': name, 'row': number, 'name': title,
                       'url': link, 'task': task[:1000]})
    return result


def read_registry(settings):
    from .core import _request_with_url, _validate_public_http_url
    def read(tab):
        url = (f"https://docs.google.com/spreadsheets/d/{settings['spreadsheet_id']}/export"
               f"?format=csv&gid={tab['gid']}")
        body, _, _ = _request_with_url(url, timeout=8, public_only=True)
        return parse_tab(body, tab['name'])
    # Publish a snapshot only after every configured tab has been read.
    with ThreadPoolExecutor(max_workers=4) as executor:
        rows = [row for tab in executor.map(read, settings['tabs']) for row in tab]
    allowed = {}
    for row in rows:
        url = row['url']
        if url and url not in allowed:
            try:
                _validate_public_http_url(url)
            except Exception:
                allowed[url] = False
            else:
                allowed[url] = True
        row['url_allowed'] = allowed.get(url, False)
    return {'rows': rows, 'checked_at': time.time()}


def source_for(row, existing):
    url = row['url']
    if not url:
        return None, 'Без ссылки'
    if not row.get('url_allowed'):
        return None, 'Ссылка не прошла проверку публичного HTTPS-адреса'
    try:
        parsed = urlsplit(url)
        if (parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password
                or parsed.port not in (None, 443)):
            return None, 'Нужна публичная HTTPS-ссылка'
    except ValueError:
        return None, 'Некорректная ссылка'
    host = parsed.hostname.lower()
    if host == 't.me' and re.fullmatch(r'/[A-Za-z0-9_]+/?', parsed.path):
        kind = 'telegram'
    elif host == 'news.google.com' and parsed.path.startswith('/rss/'):
        kind = 'google_news'
    elif url in existing:
        kind = existing[url].get('type', 'rss')
    elif re.search(r'(?:\.rss|\.xml|/feed/?|/rss/?)$', parsed.path, re.I):
        kind = 'rss'
    else:
        return None, 'Нужна RSS-лента; сайт сохранён в справочнике'
    # A section is a monitoring hint, never proof of ownership or trust.
    old = existing.get(url, {})
    label = old.get('name') or row['name']
    if not old.get('name') and kind == 'telegram' and row.get('section') == 'Лица':
        label = 'Канал @' + parsed.path.strip('/')
    source = {**old, 'name': label[:100],
              'url': url, 'type': kind}
    source.setdefault('active', True)
    source.setdefault('priority', 1)
    source.setdefault('reputation', 'unknown')
    source.setdefault('source_role', 'aggregator')
    return source, 'В мониторинге' if source['active'] else 'Выключен в текущих настройках'


def apply_snapshot(config, snapshot):
    existing = {s['url']: dict(s) for s in config.get('sources', [])}
    rows = []
    linked_names = {}
    for row in snapshot.get('rows', []):
        if row.get('url'):
            linked_names.setdefault(row['url'], set()).add(row['name'])
    for original in snapshot.get('rows', []):
        row = dict(original)
        row['entity_monitored'] = True
        source_row = dict(row)
        if len(linked_names.get(row['url'], set())) > 1 and row['url'].startswith('https://t.me/'):
            source_row['name'] = 'Канал @' + urlsplit(row['url']).path.strip('/')
        source, row['status'] = source_for(source_row, existing)
        if source:
            existing[source['url']] = source
        rows.append(row)
    config['sources'] = list(existing.values())
    config['_registry_rows'] = rows
    return rows


def sync(db, config, force=False):
    settings = state(db, SETTINGS)
    if not settings:
        return None
    now = time.time()
    snapshot = state(db, SNAPSHOT, {})
    attempt = state(db, ATTEMPT, 0)
    if force or now - attempt >= 180:
        save(db, ATTEMPT, now)
        db.commit()
        try:
            fresh = read_registry(settings)
        except Exception as exc:
            save(db, ERROR, type(exc).__name__)
        else:
            snapshot = fresh
            save(db, SNAPSHOT, snapshot)
            save(db, ERROR, None)
        db.commit()
    rows = apply_snapshot(config, snapshot)
    schedule_entities(db, config, rows)
    return {'url': settings['url'], 'checked_at': snapshot.get('checked_at'),
            'error': state(db, ERROR), 'rows': rows}


def configure(db, payload):
    settings = validate_settings(payload)
    snapshot = read_registry(settings)
    save(db, SETTINGS, settings)
    save(db, SNAPSHOT, snapshot)
    save(db, ATTEMPT, snapshot['checked_at'])
    save(db, ERROR, None)
    db.commit()
    return {'ok': True, 'rows': len(snapshot['rows']), 'url': settings['url']}


def report(db, config):
    settings = state(db, SETTINGS)
    if not settings:
        return {'connected': False}
    snapshot = state(db, SNAPSHOT, {})
    copy = {'sources': config.get('sources', [])}
    rows = apply_snapshot(copy, snapshot)
    return {'connected': True, 'url': settings['url'],
            'checked_at': snapshot.get('checked_at'), 'error': state(db, ERROR),
            'rows': rows}
