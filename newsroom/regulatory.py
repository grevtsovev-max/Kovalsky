"""Independent regulatory intake. Evidence-backed drafts; never publishes messages."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit, urljoin, urlunsplit, parse_qsl, urlencode

from .ai import request_response, AIResponseError
from .core import (PublisherArticleParser, _request_with_url, _extract_pdf_text,
                   canonicalize, fetch_rss, is_relevant)
from .locking import acquire_cycle_lock

SOURCES = [
    ('cbr', 'Банк России', 'cbr.ru', 'https://cbr.ru/rss/project'),
    ('duma', 'Госдума — законопроекты', 'sozd.duma.gov.ru', ''),
    ('minfin', 'Минфин', 'minfin.gov.ru', ''),
    ('pravo', 'Официальное опубликование', 'publication.pravo.gov.ru', ''),
    ('regulation', 'Проекты нормативных актов', 'regulation.gov.ru', ''),
    ('fns', 'ФНС', 'nalog.gov.ru', ''),
    ('rosfin', 'Росфинмониторинг', 'fedsfm.ru', ''),
]
STAGES = {'PROJECT': 'Проект', 'INTRODUCED': 'Внесён', 'ADOPTED': 'Принят',
          'PUBLISHED': 'Опубликован', 'IN_FORCE': 'Вступил в силу',
          'WITHDRAWN': 'Отклонён / отозван', 'GUIDANCE': 'Разъяснение', 'UNKNOWN': 'Не установлен'}
DDL = '''
CREATE TABLE IF NOT EXISTS reg_sources (
 id TEXT PRIMARY KEY, name TEXT NOT NULL, domain TEXT NOT NULL,
 checked_at TEXT, success_at TEXT, error TEXT, discovered INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS reg_documents (
 id INTEGER PRIMARY KEY, url TEXT UNIQUE NOT NULL, source_id TEXT NOT NULL,
 title TEXT NOT NULL, discovered_at TEXT NOT NULL, checked_at TEXT,
 status TEXT NOT NULL DEFAULT 'QUEUED', error TEXT, current_version INTEGER);
CREATE TABLE IF NOT EXISTS reg_versions (
 id INTEGER PRIMARY KEY, document_id INTEGER NOT NULL REFERENCES reg_documents(id),
 observed_at TEXT NOT NULL, content_hash TEXT NOT NULL, content TEXT NOT NULL,
 final_url TEXT NOT NULL, analysis TEXT NOT NULL,
 UNIQUE(document_id, content_hash));
CREATE TABLE IF NOT EXISTS reg_state (key TEXT PRIMARY KEY, value TEXT NOT NULL);
'''


def now():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def database_path(config):
    return str(Path(config['newsroom']['database']).with_name('regulatory.sqlite3'))


def connect(config):
    path = Path(database_path(config))
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, timeout=30)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA foreign_keys=ON')
    db.execute('PRAGMA journal_mode=WAL')
    db.executescript(DDL)
    for key, name, domain, _ in SOURCES:
        db.execute('INSERT OR IGNORE INTO reg_sources(id,name,domain) VALUES(?,?,?)', (key, name, domain))
    db.commit()
    return db


def connect_readonly(config):
    path = Path(database_path(config)).expanduser().resolve()
    db = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=5)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA query_only=ON')
    db.execute('PRAGMA busy_timeout=5000')
    return db


def official(url, domain=None):
    parsed = urlsplit(url)
    host = (parsed.hostname or '').lower()
    domains = [domain] if domain else [s[2] for s in SOURCES]
    return (parsed.scheme in {'http', 'https'} and not parsed.username and
            parsed.port in {None, 80, 443} and any(host == d or host.endswith('.' + d) for d in domains))


def monitoring_sources(config):
    """Adapters describe how to read; the shared registry decides what to read."""
    from .source_registry import cached_authority
    snapshot = cached_authority(config)
    if snapshot is None:
        return SOURCES
    domains = {(urlsplit(row.get('url', '')).hostname or '').lower()
               for row in snapshot.get('rows', [])
               if row.get('enabled', True) and row.get('url_allowed')}
    return [source for source in SOURCES
            if any(host == source[2] or host.endswith('.' + source[2]) for host in domains)]


def output_text(response):
    if response.get('status') == 'incomplete':
        raise AIResponseError('REG_INCOMPLETE')
    text = ''.join(b.get('text', '') for o in response.get('output', [])
                   for b in o.get('content', []) if b.get('type') == 'output_text')
    if not text:
        raise AIResponseError('REG_EMPTY_RESPONSE')
    return text


def discover(source, config):
    key, name, domain, rss = source
    result = []
    if key == 'cbr':
        from .regulatory_reader import read_source
        for page_url in ['https://cbr.ru/project_na/', 'https://cbr.ru/develop/acts/', 'https://cbr.ru/analytics/na_vr/']:
            page = read_source(page_url, archive_path(config))
            result.extend(page['links'])
    else:
        terms = config['newsroom'].get('relevance_terms', [])
        response = request_response({
            'model': config.get('ai', {}).get('search_model', config['ai'].get('model', 'gpt-6-luna')),
            'store': False, 'max_output_tokens': 2200,
            'tools': [{'type': 'web_search', 'filters': {'allowed_domains': [domain]}}],
            'input': ('Найди на официальном сайте ' + domain +
                      ' актуальные документы и изменения за последние 30 дней, а также ранее принятые'
                      ' акты с будущими сроками. Темы крипторынка России/СНГ: ' + ', '.join(terms) +
                      '. Нужны конкретные карточки законопроектов, тексты актов, проектов и официальные'
                      ' разъяснения, не главные страницы. Верни ссылки с цитированием источников.'),
        }, {**config['ai'],'timeout_seconds':120, '_work_role':'collector', '_work_stage':'regulatory_search'})
        output_text(response)
        for out in response.get('output', []):
            for block in out.get('content', []):
                for annotation in block.get('annotations', []):
                    if annotation.get('type') == 'url_citation':
                        result.append({'url': annotation.get('url', ''), 'title': annotation.get('title', '')})
    return [item for item in result if official(item['url'], None if key=='cbr' else domain)
            and len(urlsplit(item['url']).path.strip('/')) > 2
            and (not rss or is_relevant(item.get('title', '') + ' ' + item.get('content', ''),
                                       config['newsroom'].get('relevance_terms', []))
                 or re.search(r'внесении изменен|признании.+утративш',item.get('title',''),re.I))]


def archive_path(config):
    return str(Path(database_path(config)).parent/'regulatory-sources')


def canonical_document(url):
    parts = urlsplit(canonicalize(url))
    host = parts.netloc.lower().removeprefix('www.')
    query = urlencode([(k,v) for k,v in parse_qsl(parts.query) if k.lower() not in {'ysclid','yclid'}])
    return urlunsplit((parts.scheme,host,parts.path,query,''))


class DocumentText(str):
    def __new__(cls, text, source):
        result = str.__new__(cls,text); result.source=source; return result


def read_document(url, archive=None):
    from .regulatory_reader import read_source, full_text
    source=read_source(url,archive)
    if source['empty_pages']: raise ValueError('DOCUMENT_INCOMPLETE')
    return DocumentText(full_text(source),source), source['final_url'], source['links']


FIELDS = ['document_number', 'summary', 'affected', 'next_step']
PROPERTIES = {k: {'type': 'string'} for k in FIELDS}
PROPERTIES.update({
    'relevant': {'type': 'boolean'},
    'kind': {'type': 'string', 'enum': ['ACT', 'BILL', 'PROJECT', 'GUIDANCE', 'ANNOUNCEMENT', 'INDEX', 'OTHER']},
    'stage': {'type': 'string', 'enum': list(STAGES)},
    'evidence': {'type': 'string'}, 'stage_evidence': {'type': 'string'},
    'deadlines': {'type': 'array', 'items': {'type': 'object', 'additionalProperties': False,
        'properties': {k: {'type': 'string'} for k in ['date', 'meaning', 'evidence']},
        'required': ['date', 'meaning', 'evidence']}},
})
SCHEMA = {'type': 'object', 'additionalProperties': False, 'properties': PROPERTIES, 'required': list(PROPERTIES)}


def analyze_document(text, url, config, title='', previous=None):
    from .regulatory_research import research
    source=getattr(text,'source',None)
    if source is None:
        source=dict(url=url,final_url=url,pages=[str(text)],read_at=now(),raw_hash=hashlib.sha256(text.encode()).hexdigest(),format='html',ocr_pages=[],empty_pages=[],links=[])
    result=research(source,title,config,archive_path(config),previous=previous)
    result['evidence']=result['steps'][0]['evidence'][0]['quote'] if result['steps'] else ''
    result['stage_evidence']=result['stage_basis'][0]['quote'] if result['stage_basis'] else ''
    return result


def normalized(text):
    return re.sub(r'\s+', ' ', text).strip()


def validate_analysis(result, text):
    body = normalized(text)
    if result.get('stage') not in STAGES or type(result.get('relevant')) is not bool:
        raise ValueError('INVALID_ANALYSIS')
    evidence = normalized(result.get('evidence', ''))
    if result['relevant'] and (len(evidence) < 24 or evidence not in body):
        raise ValueError('UNSUPPORTED_RELEVANCE')
    quote = normalized(result.get('stage_evidence', ''))
    if result['stage'] != 'UNKNOWN' and (len(quote) < 12 or quote not in body):
        raise ValueError('UNSUPPORTED_STAGE')
    for deadline in result.get('deadlines', []):
        datetime.strptime(deadline['date'], '%Y-%m-%d')
        quote = normalized(deadline['evidence'])
        if len(quote) < 12 or quote not in body:
            raise ValueError('UNSUPPORTED_DEADLINE')


def enqueue(db, source_id, items):
    count = 0
    for item in items:
        if not official(item['url']):
            continue
        cursor = db.execute('INSERT OR IGNORE INTO reg_documents(url,source_id,title,discovered_at) VALUES(?,?,?,?)',
                            (canonical_document(item['url']), next((s[0] for s in SOURCES if official(item['url'],s[2])),source_id), item.get('title') or item['url'], now()))
        count += cursor.rowcount
    db.commit()
    return count


def process_document(db, document, config):
    text, final_url, links = read_document(document['url'],archive_path(config))
    from .regulatory_research import VERSION
    dependencies = []
    if document['current_version']:
        saved = db.execute('SELECT analysis FROM reg_versions WHERE id=?',(document['current_version'],)).fetchone()
        if saved:
            for key, source in json.loads(saved['analysis']).get('sources',{}).items():
                if key in {'D1','PREVIOUS'}: continue
                related = db.execute('SELECT v.analysis FROM reg_documents d JOIN reg_versions v ON v.id=d.current_version WHERE d.url=?',
                                     (canonical_document(source['url']),)).fetchone()
                if related:
                    current = json.loads(related['analysis']).get('sources',{}).get('D1',{}).get('raw_hash','')
                    dependencies.append((source['url'],current))
    # Revisit dependencies weekly even when the root text has not changed.
    week = datetime.now(timezone.utc).strftime('%G-%V')
    checksum = hashlib.sha256((normalized(text)+json.dumps([VERSION,week,sorted(dependencies)])).encode()).hexdigest()
    previous = db.execute('SELECT * FROM reg_versions WHERE document_id=? AND content_hash=?',
                          (document['id'], checksum)).fetchone()
    if previous:
        version_id, result = previous['id'], json.loads(previous['analysis'])
    else:
        previous_source = None
        if document['current_version']:
            old=db.execute('SELECT content,final_url,observed_at FROM reg_versions WHERE id=?',(document['current_version'],)).fetchone()
            if old:
                pages=re.split(r'\[Страница \d+\]\n',old['content'])[1:] or [old['content']]
                previous_source=dict(url=document['url'],final_url=old['final_url'],read_at=old['observed_at'],raw_hash=hashlib.sha256(old['content'].encode()).hexdigest(),format='stored',pages=pages,ocr_pages=[],empty_pages=[])
                Path(archive_path(config)).mkdir(parents=True,exist_ok=True)
                (Path(archive_path(config))/(previous_source['raw_hash']+'.json')).write_text(json.dumps(previous_source,ensure_ascii=False))
        result = analyze_document(text, final_url, config, title=document['title'], previous=previous_source)
        cursor = db.execute('INSERT INTO reg_versions(document_id,observed_at,content_hash,content,final_url,analysis) VALUES(?,?,?,?,?,?)',
                            (document['id'], now(), checksum, text, final_url, json.dumps(result, ensure_ascii=False)))
        version_id = cursor.lastrowid
    status = 'NEEDS_REVIEW' if result['relevant'] and result['kind'] not in {'INDEX', 'OTHER'} else 'FILTERED'
    db.execute('UPDATE reg_documents SET checked_at=?,status=?,error=NULL,current_version=? WHERE id=?',
               (now(), status, version_id, document['id']))
    db.commit()
    if result['relevant']:
        enqueue(db, document['source_id'], links[:10])
        enqueue(db, document['source_id'], [{'url':s['url'],'title':'Связанный документ: '+s['url']}
                 for k,s in result.get('sources',{}).items() if k not in {'D1','PREVIOUS'}])
    return 'UNCHANGED' if previous and document['current_version'] == version_id else status


def run_cycle(config, discover_limit=None):
    settings = config.get('regulatory', {})
    lock = acquire_cycle_lock(database_path(config))
    if lock is None:
        return {'BUSY': 1}
    db = connect(config)
    counts = {}
    try:
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=6)).isoformat()
        due = {row['id'] for row in db.execute('SELECT id FROM reg_sources WHERE checked_at IS NULL OR checked_at<?', (cutoff,))}
        allowed = monitoring_sources(config)
        sources = [s for s in allowed if s[0] in due]
        if discover_limit is not None:
            sources = sources[:discover_limit]
        for source in sources:
            key = source[0]
            try:
                items = discover(source, config)
                added = enqueue(db, key, items)
                db.execute('UPDATE reg_sources SET checked_at=?,success_at=?,error=NULL,discovered=? WHERE id=?',
                           (now(), now(), len(items), key))
                counts['DISCOVERED'] = counts.get('DISCOVERED', 0) + added
            except Exception as exc:
                db.execute('UPDATE reg_sources SET checked_at=?,error=? WHERE id=?', (now(), exc.code if isinstance(exc,AIResponseError) else type(exc).__name__, key))
                counts['SOURCE_ERROR'] = counts.get('SOURCE_ERROR', 0) + 1
            db.commit()
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=6)).isoformat()
        ids = [s[0] for s in allowed]
        placeholders = ','.join('?' for _ in ids) or 'NULL'
        docs = db.execute(f'SELECT * FROM reg_documents WHERE source_id IN ({placeholders}) AND (checked_at IS NULL OR checked_at<?) '
                          "ORDER BY checked_at IS NOT NULL, COALESCE(checked_at,discovered_at),id LIMIT ?",
                          (*ids, cutoff, int(settings.get('documents_per_cycle', 3)))).fetchall()
        for document in docs:
            try:
                outcome = process_document(db, document, config)
            except Exception as exc:
                db.rollback()
                # Preserve the last successfully read version while displaying the new failure.
                db.execute('UPDATE reg_documents SET checked_at=?,status=?,error=? WHERE id=?',
                           (now(), 'RETRY', exc.code if isinstance(exc,AIResponseError) else type(exc).__name__, document['id']))
                db.commit()
                outcome = 'RETRY'
            counts[outcome] = counts.get(outcome, 0) + 1
        db.execute("INSERT OR REPLACE INTO reg_state VALUES('last_cycle',?)", (now(),))
        db.commit()
        return counts
    finally:
        db.close()
        lock.close()


def snapshot(config):
    if not Path(database_path(config)).exists():
        return {'items': [], 'sources': [], 'last_cycle': None, 'total': 0}
    db = connect_readonly(config)
    try:
        items = []
        for row in db.execute('SELECT d.*,v.analysis,v.observed_at FROM reg_documents d '
                              'LEFT JOIN reg_versions v ON v.id=d.current_version '
                              "WHERE d.status!='FILTERED' ORDER BY d.current_version IS NULL, COALESCE(v.observed_at,d.discovered_at) DESC LIMIT 100"):
            item = dict(row)
            item['analysis'] = json.loads(item['analysis']) if item['analysis'] else {}
            item['title'] = item['analysis'].get('title') or item['title']
            item['history'] = [{'observed_at': r['observed_at'], **json.loads(r['analysis'])}
                               for r in db.execute('SELECT observed_at,analysis FROM reg_versions WHERE document_id=? ORDER BY id DESC LIMIT 10', (row['id'],))]
            items.append(item)
        last = db.execute("SELECT value FROM reg_state WHERE key='last_cycle'").fetchone()
        allowed = {source[0] for source in monitoring_sources(config)}
        return {'items': items, 'sources': [dict(r) for r in db.execute('SELECT * FROM reg_sources') if r['id'] in allowed],
                'last_cycle': last[0] if last else None,
                'researched': db.execute("SELECT count(*) FROM reg_documents WHERE status!='FILTERED' AND current_version IS NOT NULL").fetchone()[0],
                'total': db.execute("SELECT count(*) FROM reg_documents WHERE status!='FILTERED'").fetchone()[0]}
    finally:
        db.close()


def main():
    from .cli import load_config
    parser = argparse.ArgumentParser(description='Отдельный нормативный мониторинг Kovalsky')
    parser.add_argument('--config', default='config.toml')
    parser.add_argument('--discover-limit', type=int)
    parser.add_argument('--snapshot', action='store_true')
    parser.add_argument('--enqueue', action='append', default=[])
    parser.add_argument('--title', default='Документ для исследования')
    args = parser.parse_args()
    config = load_config(args.config)
    if args.enqueue:
        db=connect(config)
        enqueue(db,'cbr',[{'url':url,'title':args.title} for url in args.enqueue])
        db.close()
    result = snapshot(config) if args.snapshot else run_cycle(config, args.discover_limit)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    main()
