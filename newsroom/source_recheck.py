"""One source refresh for an accepted post that waited more than a day."""
from __future__ import annotations
import json
from datetime import datetime, timedelta, timezone
from .delivery import DeliveryRejected


class SourceUpdateRequired(DeliveryRejected):
    pass


def _save(db, key, value):
    db.execute('INSERT INTO app_state(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
               (key, json.dumps(value, ensure_ascii=False)))
    db.commit()


def _claim_read(db, key, now):
    from .runtime import BudgetDeferred
    db.commit()
    db.execute('BEGIN IMMEDIATE')
    prior = db.execute('SELECT value FROM app_state WHERE key=?', (key,)).fetchone()
    state = json.loads(prior[0]) if prior else {}
    if state.get('current') or state.get('status') in {'UNCHANGED', 'SUPPORTED', 'UNAVAILABLE'}:
        db.commit()
        return state
    if state.get('status') == 'READING' and datetime.fromisoformat(state['lease_until']) > now:
        db.commit()
        raise BudgetDeferred('concurrency', 30)
    _save(db, key, {'status': 'READING', 'at': now.isoformat(),
                    'lease_until': (now + timedelta(seconds=30)).isoformat()})
    return None


def _replace_material(db, config, row, current, citation, post_id):
    db.commit()
    db.execute('BEGIN IMMEDIATE')
    try:
        latest = db.execute('SELECT ingest_revision FROM items WHERE item_id=?', (row['item_id'],)).fetchone()
        if not latest or latest[0] != row['ingest_revision']:
            raise DeliveryRejected('SOURCE_REFRESH_VERSION_RACE')
        _replace_material_locked(db, config, row, current, citation, post_id)
    except Exception:
        db.rollback()
        raise


def _replace_material_locked(db, config, row, current, citation, post_id):
    from .core import _saved_material, _save_item, NOW
    from .workflow import enqueue
    source = db.execute('SELECT * FROM sources WHERE source_id=?', (row['source_id'],)).fetchone()
    item = _saved_material(row, source)
    item.update(content=current['content'], material_read=True,
                material_url=current.get('material_url') or current.get('url') or citation['url'],
                publisher_name=citation.get('publisher') or source['name'])
    if current.get('title'):
        item['title'] = current['title']
    if current.get('published_at'):
        item['published_at'] = current['published_at']
    if current.get('updated_at'):
        item['updated_at'] = current['updated_at']
    if citation.get('type') != 'ATTRIBUTED_REPORT':
        item.update(primary_source_url=citation['url'], primary_source_content=current['content'],
                    primary_source_title=current.get('title') or item['title'],
                    primary_source_type=citation.get('type'), primary_source_publisher=citation.get('publisher'),
                    primary_source_status='READ')
    item_id = _save_item(db, source, item)
    if item_id is None:
        raise DeliveryRejected('SOURCE_UPDATE_COULD_NOT_BE_QUEUED')
    revision = db.execute('SELECT ingest_revision FROM items WHERE item_id=?', (item_id,)).fetchone()[0]
    # This continues a previously admitted material; it is not a late first
    # discovery. Keep the reason instead of inventing a publication timestamp.
    db.execute('INSERT INTO app_state(key,value) VALUES(?,?) ON CONFLICT(key) DO NOTHING',
               (f'policy_admission:{item_id}:{revision}', json.dumps({'accepted': True, 'at': NOW(),
                   'source_date': item.get('published_at'), 'reason': 'SOURCE_REFRESH_OF_ACCEPTED_MATERIAL'})))
    db.execute("UPDATE posts SET status='SUPERSEDED',editor_decision='SOURCE_CHANGED',auto_last_error='SOURCE_UPDATE_REQUIRED' WHERE post_id=? AND status='PENDING'", (post_id,))
    settings = config.get('newsroom', {})
    enqueue(db, item_id, item, source, {'threshold': settings.get('similarity_threshold', .35),
        'max_length': settings.get('max_post_length', 3500), 'freshness_hours': settings.get('freshness_window_hours', 24),
        'initial_backfill_minutes': None, 'relevance_terms': []}, category='retry')


def verify(db, config, post, facts, *, now=None):
    """Unchanged/unavailable sources retain admission; changed facts reenter work."""
    from .core import fetch_publisher_article
    from .knowledge import norm, validate_post_bindings
    from .ai import validate_draft
    if not post['origin_item_id'] or not facts.get('policy'):
        return
    row = db.execute('SELECT * FROM items WHERE item_id=?', (post['origin_item_id'],)).fetchone()
    if not row:
        raise DeliveryRejected('SOURCE_MATERIAL_MISSING')
    if facts.get('material_revision') and facts['material_revision'] != row['ingest_revision']:
        db.execute("UPDATE posts SET status='SUPERSEDED',editor_decision='SOURCE_CHANGED',auto_last_error='SOURCE_MATERIAL_VERSION_CHANGED' WHERE post_id=? AND status='PENDING'", (post['post_id'],))
        db.commit()
        raise SourceUpdateRequired('SOURCE_MATERIAL_VERSION_CHANGED')
    admission = db.execute('SELECT value FROM app_state WHERE key=?',
                           (f"policy_admission:{row['item_id']}:{row['ingest_revision']}",)).fetchone()
    anchor = json.loads(admission[0])['at'] if admission else row['discovered_at']
    accepted_at = datetime.fromisoformat(anchor.replace('Z', '+00:00'))
    now = now or datetime.now(timezone.utc)
    from .policy import relative_date_words, requeue_changed_policy
    if relative_date_words(post['text']):
        requeue_changed_policy(db, config, post, reason='RELATIVE_DATE_REWRITE')
    if (now - accepted_at).total_seconds() <= 86400:
        return
    key = f"pre_send_source:{post['post_id']}:{facts['final_text_check']['assembled_sha256']}"
    prior = db.execute('SELECT value FROM app_state WHERE key=?', (key,)).fetchone()
    state = json.loads(prior[0]) if prior else {}
    if state.get('status') in {'UNCHANGED', 'SUPPORTED', 'UNAVAILABLE'}:
        return
    from .runtime import BudgetDeferred, account_unavailable
    if state.get('status') == 'TECHNICAL_ERROR':
        raise DeliveryRejected('SOURCE_REFRESH_TECHNICAL_ERROR')
    if state.get('retry_at') and datetime.fromisoformat(state['retry_at']) > now:
        raise BudgetDeferred('requests', max(1, int((datetime.fromisoformat(state['retry_at']) - now).total_seconds())))
    if state.get('status') == 'REPROCESSING':
        raise SourceUpdateRequired('SOURCE_UPDATE_REQUIRED')
    citation = facts.get('publisher_report') if facts.get('publisher_report_exception') else facts.get('primary_source')
    if not citation or not citation.get('url'):
        raise DeliveryRejected('SOURCE_REFRESH_URL_MISSING')
    saved_primary = json.loads(row['primary_source_json'] or '{}')
    original = citation.get('content') or saved_primary.get('content')
    if not original:
        raise DeliveryRejected('SAVED_READ_SOURCE_MISSING')
    import hashlib
    read_key = (f"pre_send_source_read:{row['item_id']}:{row['ingest_revision']}:"
                + hashlib.sha256(citation['url'].encode()).hexdigest())
    if 'current' not in state:
        claimed = _claim_read(db, read_key, now)
        if claimed and claimed.get('status') in {'UNCHANGED', 'UNAVAILABLE'}:
            return
        if claimed and claimed.get('current'):
            state = {**state, 'current': claimed['current']}
            _save(db, key, state)
    if 'current' not in state:
        try:
            current = fetch_publisher_article(citation['url'], citation.get('publisher') or '',
                citation.get('published_at'), discover_primary=False, timeout=20, public_only=True)
        except (TimeoutError, OSError, ValueError) as exc:
            _save(db, read_key, {'status': 'UNAVAILABLE', 'at': now.isoformat(), 'error': type(exc).__name__})
            return
        if current.get('material_read') is not True or not str(current.get('content') or '').strip():
            _save(db, read_key, {'status': 'UNAVAILABLE', 'at': now.isoformat()})
            return
        if norm(current['content']) == norm(original):
            _save(db, read_key, {'status': 'UNCHANGED', 'at': now.isoformat()})
            return
        state = {'status': 'CHECKING_CHANGE', 'at': now.isoformat(), 'current': current}
        _save(db, read_key, {'status': 'READ_CHANGED', 'at': now.isoformat(), 'current': current})
        _save(db, key, state)
    current = state['current']
    headline, _, body = post['text'].partition('\n\n')
    body = body.rpartition('\n\n')[0]
    analysis = db.execute('SELECT result_json FROM item_analysis WHERE item_id=?', (row['item_id'],)).fetchone()
    decision = json.loads(analysis[0]) if analysis else facts
    claims = [dict(fact) for fact in db.execute('SELECT f.fact_id,f.subject,f.predicate,f.value,f.statement,f.fact_type FROM post_facts p JOIN story_facts f USING(fact_id) WHERE p.post_id=?', (post['post_id'],))]
    from .policy import date_context
    # The refreshed text belongs to the refreshed publication clock. An
    # update timestamp must never stand in for its publication timestamp.
    refreshed_citation = {**citation, 'content': current['content']}
    if current.get('published_at'):
        refreshed_citation['published_at'] = current['published_at']
    for field in ('source_timezone', 'timezone'):
        if current.get(field):
            refreshed_citation[field] = current[field]
    source_row = db.execute('SELECT * FROM sources WHERE source_id=?', (row['source_id'],)).fetchone()
    options = {**config.get('ai', {}), '_transport_attempt': int(state.get('failures', 0)),
               '_draft_contract': {'text_field': 'summary_ru', 'material_facts': claims,
                                  'dates': date_context(dict(row), refreshed_citation, dict(source_row) if source_row else {})}}
    try:
        checked = validate_draft(decision, refreshed_citation,
            {'headline_ru': headline, 'summary_ru': body, 'what_is_new': body, 'editorial_check': facts.get('editorial_check')}, options)
        validate_post_bindings(checked, {claim['fact_id'] for claim in claims}, headline + '\n\n' + body)
        from .quality import editorial_issues
        issues = checked['issues'] + editorial_issues(headline, body, {**facts, 'editorial_check': checked['editorial_check']})
    except Exception as exc:
        from .material_flow import mark, technical_error
        account = account_unavailable(getattr(exc, 'code', None))
        failures = int(state.get('failures', 0))
        if isinstance(exc, BudgetDeferred) or account:
            delay = exc.delay_seconds if isinstance(exc, BudgetDeferred) else 900
        else:
            failures += 1
            delay = (30, 120, 300)[min(failures - 1, 2)]
        blocked = failures > 3 or technical_error(exc)
        state.update(status='TECHNICAL_ERROR' if blocked else 'CHECKING_CHANGE', failures=failures,
                     error=getattr(exc, 'code', type(exc).__name__), retry_at=(now + timedelta(seconds=delay)).isoformat())
        _save(db, key, state)
        mark(db, row['item_id'], 'gate', 'ERROR' if blocked else 'WAITING',
             'Проверка изменения источника отложена: ' + state['error'],
             block_kind='technical' if blocked else 'account' if account else 'capacity' if isinstance(exc, BudgetDeferred) else 'transport',
             next_at=state['retry_at'])
        db.commit()
        if blocked:
            raise DeliveryRejected('SOURCE_REFRESH_TECHNICAL_ERROR') from None
        raise BudgetDeferred('account' if account else 'requests', delay) from None
    if issues:
        _replace_material(db, config, row, current, citation, post['post_id'])
        state.update(status='REPROCESSING', issues=issues)
        _save(db, key, state)
        raise SourceUpdateRequired('SOURCE_UPDATE_REQUIRED')
    state.update(status='SUPPORTED', checked=checked)
    _save(db, key, state)
