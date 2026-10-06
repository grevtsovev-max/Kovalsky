"""Provider costs and owner-reported totals, separate from request estimates."""
from __future__ import annotations

import json
import os
import threading
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import urlencode

from .resources import snapshot

_LOCK = threading.Lock()
COSTS_URL = 'https://api.openai.com/v1/organization/costs'
TASKS = {
    'discovery_search': 'Поиск новостей и источников', 'recovery_search': 'Поиск новостей и источников',
    'research_agent': 'Поиск новостей и источников', 'triage': 'Отбор новостей',
    'editorial': 'Подготовка публикаций', 'correction': 'Исправление публикаций',
    'interest_learning': 'Обучение на редакторских сигналах',
    'archive_memory': 'Разбор архива', 'weekly_analysis': 'Недельная аналитика',
    'regulatory_search': 'Нормативные документы', 'regulatory_relations': 'Нормативные документы',
    'regulatory_analysis': 'Нормативные документы', 'regulatory_repair': 'Нормативные документы',
    'regulatory_review': 'Нормативные документы', 'unattributed': 'Старые запросы без указанной задачи',
}


def _now():
    return datetime.now(timezone.utc)


def _state(db, key):
    row = db.execute('SELECT value FROM app_state WHERE key=?', (key,)).fetchone()
    return json.loads(row[0]) if row else None


def _save(db, key, value):
    db.execute('INSERT INTO app_state(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
               (key, json.dumps(value, ensure_ascii=False)))
    db.commit()


def period_range(period='month', now=None, start=None, end=None):
    now = now or _now()
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if period == 'today':
        begin = midnight
    elif period == 'week':
        begin = midnight - timedelta(days=6)
    elif period == 'month':
        begin = midnight.replace(day=1)
    elif period == 'custom':
        try:
            begin = datetime.strptime(start, '%Y-%m-%d').replace(tzinfo=timezone.utc)
            stop = datetime.strptime(end, '%Y-%m-%d').replace(tzinfo=timezone.utc) + timedelta(days=1)
        except (ValueError, TypeError):
            raise ValueError('Укажите начало и конец периода') from None
        if begin >= stop or begin > now or (stop-begin).days > 180:
            raise ValueError('Период должен быть от 1 до 180 дней и не начинаться в будущем')
        return begin, min(stop, now)
    else:
        raise ValueError('Неизвестный период')
    return begin, now


def save_owner_report(db, payload, now=None):
    now = now or _now()
    try:
        amount = Decimal(str(payload.get('amount_usd', '')).replace(',', '.'))
    except InvalidOperation:
        raise ValueError('Укажите сумму в долларах') from None
    if not amount.is_finite() or amount < 0 or amount > Decimal('1000000000'):
        raise ValueError('Укажите неотрицательную сумму в долларах')
    scope = payload.get('scope', 'unknown')
    if scope not in ('unknown', 'account', 'kovalsky'):
        raise ValueError('Неизвестная область расходов')
    start, end = payload.get('start'), payload.get('end')
    begin, stop = period_range('custom', now, start, end) if start and end else (None, None)
    if bool(start) != bool(end):
        raise ValueError('Укажите обе даты или оставьте обе пустыми')
    report = {'source': 'owner_report', 'amount_usd': float(amount), 'scope': scope,
              'start': begin.isoformat() if begin else None, 'end': stop.isoformat() if stop else None,
              'recorded_at': now.isoformat(), 'line_items': []}
    _save(db, 'billing_owner_report', report)
    return report


def _admin_key(settings):
    key = os.getenv(settings.get('admin_api_key_env', 'OPENAI_ADMIN_KEY'))
    if not key and settings.get('admin_api_key_file'):
        try:
            key = Path(settings['admin_api_key_file']).expanduser().read_text().strip()
        except OSError:
            pass
    return key


def _connection(settings):
    return {'configured': bool(_admin_key(settings)),
            'scope': 'kovalsky' if settings.get('dedicated_project') is True and settings.get('project_ids') else 'account'}


def _cache_key(begin, stop, settings):
    # Project selection is private runtime configuration, never an API credential.
    import hashlib
    identity = json.dumps([begin.date().isoformat(), stop.date().isoformat(),
                           settings.get('project_ids', []), settings.get('dedicated_project', False)], sort_keys=True)
    return 'billing_costs:' + hashlib.sha256(identity.encode()).hexdigest()


def fetch_costs(settings, begin, stop, key):
    """Read all pages; only persist a complete USD result. Never log raw errors."""
    query = [('start_time', int(begin.timestamp())), ('end_time', int(stop.timestamp())),
             ('bucket_width', '1d'), ('limit', 180), ('group_by', 'project_id'), ('group_by', 'line_item')]
    for project in settings.get('project_ids', []):
        query.append(('project_ids', project))
    totals, days, seen, page = {}, {}, set(), None
    for _ in range(50):
        req = urllib.request.Request(COSTS_URL + '?' + urlencode(query + ([('page', page)] if page else [])),
                                     headers={'Authorization': 'Bearer '+key, 'Accept': 'application/json'})
        with urllib.request.urlopen(req, timeout=15) as response:
            data = json.load(response)
        if not isinstance(data, dict) or not isinstance(data.get('data'), list):
            raise ValueError('BILLING_INVALID_RESPONSE')
        for bucket in data['data']:
            if not isinstance(bucket, dict) or not isinstance(bucket.get('results'), list):
                raise ValueError('BILLING_INVALID_RESPONSE')
            day = datetime.fromtimestamp(bucket['start_time'], timezone.utc).date().isoformat()
            for row in bucket['results']:
                money = row.get('amount') or {}
                if money.get('currency') != 'usd':
                    raise ValueError('BILLING_CURRENCY_UNSUPPORTED')
                amount = Decimal(str(money.get('value')))
                if not amount.is_finite():
                    raise ValueError('BILLING_INVALID_AMOUNT')
                label = row.get('line_item') or 'Без указанной статьи расходов'
                totals[label] = totals.get(label, Decimal(0)) + amount
                days[day] = days.get(day, Decimal(0)) + amount
        if data.get('has_more') is False:
            return {'source': 'openai', 'amount_usd': float(sum(totals.values(), Decimal(0))),
                    'scope': _connection(settings)['scope'], 'start': begin.isoformat(), 'end': stop.isoformat(),
                    'recorded_at': _now().isoformat(),
                    'line_items': [{'name': name, 'amount_usd': float(amount)} for name, amount in sorted(totals.items(), key=lambda x:x[1], reverse=True)],
                    'daily': [{'date': day, 'amount_usd': float(amount)} for day, amount in sorted(days.items())]}
        page = data.get('next_page')
        if not isinstance(page, str) or not page or page in seen:
            raise ValueError('BILLING_INCOMPLETE_PAGES')
        seen.add(page)
    raise ValueError('BILLING_TOO_MANY_PAGES')


def sync(db, config, period='month', start=None, end=None, now=None):
    begin, stop = period_range(period, now, start, end)
    settings = config.get('billing', {})
    key = _admin_key(settings)
    if not key:
        return {'ok': False, 'code': 'ADMIN_KEY_MISSING', 'message': 'Автосверка не подключена: на сервере нужен отдельный Admin API key OpenAI.'}
    cache_key = _cache_key(begin, stop, settings)
    if not _LOCK.acquire(blocking=False):
        return {'ok': False, 'code': 'SYNC_BUSY', 'message': 'Сверка уже выполняется'}
    try:
        previous = _state(db, cache_key)
        if previous and 0 <= ((now or _now())-datetime.fromisoformat(previous['recorded_at'])).total_seconds() < 300:
            return {'ok': True, 'cached': True}
        report = fetch_costs(settings, begin, stop, key)
        _save(db, cache_key, report)
        _save(db, 'billing_last_error', None)
        return {'ok': True, 'cached': False}
    except Exception as exc:
        code = 'HTTP_'+str(exc.code) if isinstance(exc, urllib.error.HTTPError) else type(exc).__name__
        _save(db, 'billing_last_error', {'code': code, 'at': _now().isoformat()})
        return {'ok': False, 'code': code, 'message': 'Не удалось получить расходы OpenAI. Прежние данные сохранены.'}
    finally:
        _LOCK.release()


def spending(db, config, period='month', start=None, end=None, now=None):
    begin, stop = period_range(period, now, start, end)
    settings = config.get('billing', {})
    actual = _state(db, _cache_key(begin, stop, settings))
    owner = _state(db, 'billing_owner_report')
    # A reported total with another/unknown period stays separate. It cannot
    # silently become the amount for the selected period or for one material.
    owner_matches = bool(owner and owner.get('start') == begin.isoformat() and owner.get('end')
                         and datetime.fromisoformat(owner['end']).date() == stop.date())
    if not actual and owner_matches:
        actual = owner
    ledger_stop = min(stop, datetime.fromisoformat(actual['end'])) if actual else stop
    local = snapshot(db, config, now=ledger_stop, start=begin)
    tasks = {}
    for row in local['stages']:
        if not row['calls']:
            continue
        label = TASKS.get(row['stage'], row['label'])
        group = tasks.setdefault(label, {'name': label, 'calls': 0, 'priced_calls': 0, 'unpriced_calls': 0, 'amount_usd': 0.})
        for field in ('calls', 'priced_calls', 'unpriced_calls'):
            group[field] += row[field]
        group['amount_usd'] += row['known_estimated_usd']
    total = local['total']
    comparable = bool(actual and actual['scope'] == 'kovalsky')
    difference = actual['amount_usd'] - total['known_estimated_usd'] if comparable else None
    return {'period': period, 'start': begin.isoformat(), 'end': ledger_stop.isoformat(),
            'actual': actual, 'owner_report': owner, 'connection': _connection(settings),
            'last_sync_error': _state(db, 'billing_last_error'),
            'accounting_started_at': local['accounting_started_at'],
            'local': total, 'publications': local['publications'],
            'tasks': sorted(tasks.values(), key=lambda r:r['amount_usd'], reverse=True),
            'unexplained_usd': difference if difference is not None and difference >= 0 else None,
            'reconciliation_conflict': difference is not None and difference < 0,
            'comparable': comparable,
            'unknown_cost_reasons': local['unknown_cost_reasons']}
