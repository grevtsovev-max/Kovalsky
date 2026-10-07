"""Resource reports contain counters only, never request text or credentials."""
from __future__ import annotations

import json
import math
import re
from collections import defaultdict
from datetime import datetime, timedelta, timezone

STAGES = {
    'discovery_search': 'Поиск новых новостей', 'source_rss': 'Сбор RSS',
    'source_web': 'Сбор сайтов', 'source_telegram': 'Сбор Telegram',
    'source_google_news': 'Сбор Google News', 'source_x': 'Сбор X',
    'source_read': 'Чтение статьи и первоисточника',
    'recovery_search': 'Поиск недостающего источника',
    'research_agent': 'Исследователь источника', 'triage': 'Предварительный отбор',
    'editorial': 'Редакторский разбор', 'correction': 'Правки публикаций',
    'interest_learning': 'Учёт редакторских предпочтений',
    'archive_memory': 'Разбор опубликованной истории', 'weekly_analysis': 'Недельная аналитика',
    'regulatory_search': 'Поиск нормативных документов',
    'regulatory_relations': 'Поиск связанных нормативных актов',
    'regulatory_analysis': 'Разбор нормативного документа',
    'regulatory_repair': 'Исправление нормативного разбора',
    'regulatory_review': 'Проверка нормативного разбора',
    'telegram_delivery': 'Обращения к Telegram', 'digest': 'Подготовка и доставка дайджеста',
    'verification': 'Проверка готового текста', 'drafting': 'Написание поста', 'unattributed': 'Этап не записан',
}
FUNCTION_STAGES = {
    '_read_material_work': 'source_read', 'fetch_publisher_article': 'source_read',
    'classify': 'triage', 'analyze': 'editorial', 'draft_post': 'drafting', 'validate_draft': 'verification', 'request_response': 'research_agent',
    'fetch_web_search': 'recovery_search', 'fetch_google_news': 'source_google_news',
}
PRICING_URL = 'https://developers.openai.com/api/docs/pricing'
# Official standard text tariffs checked on 2026-10-06. Unknown models remain
# unpriced; a dated model snapshot uses its family's tariff, not another model.
STANDARD_RATES = {
    'gpt-6-luna': (.10, .01, .50), 'gpt-6-sol': (2., .20, 10.),
    'gpt-6.1-sol': (2., .10, 10.), 'gpt-6-astra': (10., 1., 50.),
    'gpt-5.6-sol': (4., .40, 20.),
}


def safe_stage(value):
    return value if value in STAGES else 'unattributed'


def integer(value):
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def price_receipt(row, settings):
    """Keep unknown costs unknown. Reasoning is already part of output tokens."""
    names = ('input_per_million', 'cached_input_per_million', 'output_per_million')
    family = re.sub(r'-\d{4}-\d{2}-\d{2}$', '', row['model'])
    custom = (settings.get('model_prices') or {}).get(row['model']) or (settings.get('model_prices') or {}).get(family)
    tier = row.get('service_tier')
    source = 'configured' if custom else 'official:2026-10-06'
    rates = [custom.get(n) for n in names] if custom else STANDARD_RATES.get(family)
    if not rates or not all(isinstance(n, (int, float)) and not isinstance(n, bool)
                            and math.isfinite(n) and n >= 0 for n in rates):
        return {'total_usd': None, 'reason': 'MODEL_PRICE_MISSING'}
    values = [integer(row.get(n)) for n in ('input_tokens', 'cached_input_tokens', 'output_tokens')]
    if any(n is None for n in values) or values[1] > values[0]:
        return {'total_usd': None, 'reason': 'USAGE_UNKNOWN'}
    inp, cached, out = values
    multiplier = 1 if custom else {'default': 1, 'flex': .5, 'priority': 2, 'fast': 2}.get(tier)
    # Old receipts do not establish actual processing tier. Show a separate
    # standard-tariff reference, never turn that assumption into a known cost.
    reference_only = multiplier is None
    if reference_only:
        multiplier = 1
    rates = list(rates)
    if not custom and inp > 272000:
        rates = [rates[0] * 2, rates[1] * 2, rates[2] * 1.5]
    token_usd = ((inp - cached) * rates[0] + cached * rates[1] + out * rates[2]) * multiplier / 1e6
    search_usd = 0
    if row.get('search_requested'):
        searches = integer(row.get('search_actions'))
        search_rate = settings.get('search_price_per_call', .01)
        if searches is None or not isinstance(search_rate, (int, float)) or not math.isfinite(search_rate) or search_rate < 0:
            return {'total_usd': None, 'token_usd': token_usd, 'reason': 'SEARCH_USAGE_UNKNOWN'}
        if row.get('search_tool') != 'web_search' and 'search_price_per_call' not in settings:
            return {'total_usd': None, 'token_usd': token_usd, 'reason': 'SEARCH_PRICE_MISSING'}
        search_usd = searches * search_rate
    total = token_usd + search_usd
    return {'total_usd': None if reference_only else total,
            'reference_usd': total if reference_only else None,
            'token_usd': token_usd, 'search_usd': search_usd,
            'source': source, 'rates': dict(zip(names, rates)),
            'tier': tier, 'reason': 'TIER_UNKNOWN' if reference_only else None}


def _empty():
    return dict(calls=0, succeeded=0, errors=0, in_flight=0, unknown_usage=0,
                incomplete=0, unknown_bytes=0, unknown_reasoning=0, unattributed_source=0, legacy_cache_hits=0,
                retries=0, retry_attribution_unknown=0, input_tokens=0,
                cached_input_tokens=0, output_tokens=0, reasoning_tokens=0,
                search_requests=0, search_actions=0, unknown_search_usage=0,
                request_bytes=0, response_bytes=0, api_seconds=0.,
                operations=0, cache_hits=0, deferred=0, operation_errors=0,
                operation_seconds=0., cpu_seconds=0., known_estimated_usd=0.,
                reference_usd=0., priced_calls=0, reference_calls=0, unpriced_calls=0)


def snapshot(db, config, hours=24, now=None, item_id=None, start=None):
    now = now or datetime.now(timezone.utc)
    hours = hours if hours in (1, 24, 168) else 24
    cutoff = (start or (now - timedelta(hours=hours))).isoformat()
    tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    settings = config.get('ai', {})
    started_row = db.execute("SELECT value FROM app_state WHERE key='resource_accounting_started_at'").fetchone() if 'app_state' in tables else None
    started_at = started_row[0] if started_row else None
    total, stages, models, hourly = _empty(), defaultdict(_empty), defaultdict(_empty), defaultdict(_empty)
    categories, sources, roles = defaultdict(_empty), defaultdict(_empty), defaultdict(_empty)
    reasons = defaultdict(int)
    where = 'created_at>=? AND created_at<=?'
    params = [cutoff, now.isoformat()]
    if item_id is not None:
        where += ' AND item_id=?'; params.append(item_id)
    calls = (dict(r) for r in db.execute('SELECT * FROM api_usage WHERE '+where, params)) if 'api_usage' in tables else ()
    for row in calls:
        stage = safe_stage(row.get('stage'))
        hour = row['created_at'][:13] + ':00:00+00:00'
        effective_model = row.get('response_model') or row['model']
        pricing = json.loads(row['pricing_json']) if row.get('pricing_json') else price_receipt({**row, 'model': effective_model}, settings)
        targets = [total, stages[stage], models[effective_model], hourly[hour], categories[row['category']], roles[row['role']]]
        source_id = row.get('source_id')
        if source_id is not None:
            targets.append(sources[source_id])
        for target in targets:
            target['calls'] += 1
            target['succeeded'] += row['status'] == 'SUCCEEDED'
            target['errors'] += row['status'] in ('ERROR', 'UNKNOWN')
            target['in_flight'] += row['status'] == 'RESERVED'
            target['incomplete'] += row.get('response_status') in ('incomplete', 'failed')
            target['unattributed_source'] += source_id is None
            target['unknown_bytes'] += row.get('request_bytes') is None or row.get('response_bytes') is None
            target['unknown_reasoning'] += row.get('reasoning_tokens') is None
            target['unknown_usage'] += any(row.get(n) is None for n in ('input_tokens','cached_input_tokens','output_tokens'))
            target['retries'] += (row.get('transport_attempt') or 0) > 0
            target['retry_attribution_unknown'] += row.get('transport_attempt') is None
            for field in ('input_tokens','cached_input_tokens','output_tokens','reasoning_tokens', 'request_bytes','response_bytes'):
                target[field] += row.get(field) or 0
            target['api_seconds'] += row.get('elapsed_seconds') or 0
            target['search_requests'] += bool(row['search_requested'])
            target['search_actions'] += row.get('search_actions') or 0
            target['unknown_search_usage'] += bool(row['search_requested']) and row.get('search_actions') is None
            if pricing.get('total_usd') is not None:
                target['priced_calls'] += 1
                target['known_estimated_usd'] += pricing['total_usd']
            else:
                target['unpriced_calls'] += 1
            if pricing.get('reference_usd') is not None:
                target['reference_calls'] += 1
                target['reference_usd'] += pricing['reference_usd']
        if pricing.get('reason'):
            reasons[pricing['reason']] += 1
    operations = (dict(r) for r in db.execute('SELECT * FROM resource_operations WHERE '+where, params)) if 'resource_operations' in tables else ()
    for row in operations:
        targets = [total, stages[safe_stage(row['stage'])], hourly[row['created_at'][:13]+':00:00+00:00'], categories[row['category']], roles[row['role']]]
        if row['source_id'] is not None:
            targets.append(sources[row['source_id']])
        for target in targets:
            target['operations'] += 1
            target['cache_hits'] += row['status'] == 'CACHED'
            target['deferred'] += row['status'] == 'DEFERRED'
            target['operation_errors'] += row['status'] == 'ERROR'
            target['operation_seconds'] += row['elapsed_seconds']
            target['cpu_seconds'] += row['cpu_seconds']
    if 'cache_events' in tables:
        legacy_where = where + (' AND created_at<?' if started_at else '')
        legacy_params = params + ([started_at] if started_at else [])
        for row in db.execute('SELECT stage,created_at FROM cache_events WHERE '+legacy_where, legacy_params):
            for target in (total, stages['unattributed'], roles[row['stage']], hourly[row['created_at'][:13]+':00:00+00:00'], categories['unattributed']):
                target['cache_hits'] += 1
                target['legacy_cache_hits'] += 1
    for target in [total, *stages.values(), *models.values(), *hourly.values(), *categories.values(), *sources.values(), *roles.values()]:
        target['estimated_usd'] = target['known_estimated_usd'] if target['calls'] and not target['unpriced_calls'] else None
        # Token counters are subtotals of known receipts. Coverage is explicit.
        target['usage_complete'] = not target['unknown_usage']
    publication_where = "status='PUBLISHED' AND published_at>=? AND published_at<=?"
    publication_params = [cutoff, now.isoformat()]
    if item_id is not None:
        publication_where += ' AND origin_item_id=?'; publication_params.append(item_id)
    publications = db.execute('SELECT COUNT(*) FROM posts WHERE '+publication_where, publication_params).fetchone()[0] if 'posts' in tables else 0
    return {'hours': hours, 'from': cutoff, 'to': now.isoformat(), 'total': total,
            'stages': [dict(stage=k, label=STAGES[k], **v) for k,v in sorted(stages.items())],
            'models': [dict(model=k, **v) for k,v in sorted(models.items())],
            'hourly': [dict(hour=k, **v) for k,v in sorted(hourly.items())],
            'categories': [dict(category=k, **v) for k,v in sorted(categories.items())],
            'roles': [dict(role=k, **v) for k,v in sorted(roles.items())],
            'sources': [dict(source_id=k, name=(db.execute('SELECT name FROM sources WHERE source_id=?',(k,)).fetchone() or ['Источник'])[0], **v)
                        for k,v in sorted(sources.items(), key=lambda x:x[1]['operation_seconds'], reverse=True)[:20]],
            'publications': publications,
            'cost_per_publication_usd': total['estimated_usd'] / publications if publications and total['estimated_usd'] is not None else None,
            'unknown_cost_reasons': dict(reasons), 'pricing_url': PRICING_URL,
            'pricing_checked_at': '2026-10-06',
            'accounting_started_at': started_at,
            'notes': ['Оценка по ответам API; фактические списания проверяются в биллинге.',
                      'Входные токены включают кешированные; выходные включают токены рассуждений.',
                      'Время операций суммируется по исполнителям без повторного учёта вложенных операций; время API входит в него.',
                      'Старые записи без этапа и режима оплаты остаются явно неполными.']}
