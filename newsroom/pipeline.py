"""Read-only editorial intake view. Counts represent saved items, not feed polls."""
import json
from datetime import datetime, timedelta, timezone
from .triage import MAX_AUTOMATIC_RETRIES

STAGES = [
    ('received', 'Ждут обработки'), ('primary', 'Чтение источника'),
    ('ai', 'Ждут анализа'), ('confirmation', 'Автоматическая перепроверка'),
    ('correction', 'Правки опубликованных постов'),
    ('review', 'Автоматическая публикация'), ('published', 'Опубликованы'),
    ('filtered', 'Отфильтрованы'), ('processed', 'Завершены без публикации'),
]
REASONS = {
    'PENDING': 'Материал получен. Результат обработки ещё не сохранён.',
    'PRIMARY_RETRY': 'Пока нет прочитанного пригодного материала. Агент повторит чтение; отдельный первоисточник не обязателен.',
    'AI_RETRY': 'Разбор ИИ не завершён. Материал оставлен на повторную обработку.',
    'WAITING_CONFIRMATION': 'Проверки не пройдены; агент повторит редакционный разбор до трёх раз и затем автоматически решит судьбу материала.',
    'AGENT_CORRECTION_QUEUED': 'Новый прочитанный материал может исправить или существенно дополнить свежий опубликованный пост; редактор проверяет правку прежнего сообщения.',
    'NOISE': 'Не прошёл тематический или редакционный отбор.',
    'DUPLICATE': 'Повтор уже известного сюжета; отдельный пост не создан.',
    'STALE': 'Возраст материала превысил допустимое окно свежести.',
    'BASELINE_SKIPPED': 'Материал старше окна загрузки при подключении источника.',
    'UNDATED': 'Не удалось определить дату публикации.',
    'EDITOR_REJECTED': 'ИИ не рекомендовал материал к публикации.',
    'REJECTED': 'Автоматическая обработка завершена по лимиту повторных проверок.',
    'STORE_ONLY': 'Материал сохранён в памяти, но существенного нового повода для отдельного поста не добавил.',
}
DECISION_LABELS = {
    'PENDING': 'Ожидает обработки', 'PRIMARY_RETRY': 'Ожидает чтения материала',
    'AI_RETRY': 'Ожидает ИИ-разбора', 'WAITING_CONFIRMATION': 'Ожидает повторной проверки',
    'AGENT_CORRECTION_QUEUED': 'Проверяется самостоятельная правка опубликованного поста',
    'NEW_STORY': 'Новый сюжет передан на допуск', 'UPDATE_CANDIDATE': 'Обновление сюжета передано на допуск',
    'NOISE': 'Отсеян', 'DUPLICATE': 'Дубликат', 'STALE': 'Устарел',
    'BASELINE_SKIPPED': 'Старше окна первичной загрузки', 'UNDATED': 'Дата не определена',
    'EDITOR_REJECTED': 'Редакторский допуск не пройден', 'REJECTED': 'Закрыт после повторных проверок',
    'STORE_ONLY': 'Сохранён в памяти без отдельного поста', 'PUBLICATION_CONFIRMED': 'Публикация подтверждена',
    'PUBLICATION_BLOCKED': 'Публикация остановлена проверкой', 'ERROR': 'Ошибка обработки',
}
PRIMARY_LABELS = {'READ': 'Прочитан', 'NO_LINK': 'Ссылка не найдена',
                  'ARTICLE_UNREADABLE': 'Статья не прочитана', 'UNREADABLE': 'Не удалось прочитать',
                  'NOT_FOUND': 'Не найден', 'OCR_REVIEW': 'Текст извлечён через OCR; нужна сверка'}
DELIVERY_LABELS = {
    'PREPARED': 'Отправка подготовлена', 'SENDING': 'Telegram обрабатывает отправку',
    'SENT': 'Ответ Telegram получен', 'CONFIRMED': 'Публикация подтверждена',
    'FAILED': 'Telegram отклонил отправку', 'UNKNOWN': 'Ответ Telegram пока не подтверждён',
}


def obj(raw):
    try:
        result = json.loads(raw or '{}')
        return result if isinstance(result, dict) else {}
    except (ValueError, TypeError):
        return {}


def decision_history(db, item_id, available=True):
    """Return a short explanation of saved processing passes without exposing raw prompts."""
    if not available:
        return []
    rows = db.execute(
        'SELECT decision,payload_json,created_at FROM agent_decisions '
        'WHERE item_id=? ORDER BY decision_id DESC LIMIT 8', (item_id,)
    ).fetchall()
    history = []
    for row in reversed(rows):
        payload = obj(row['payload_json'])
        retry = obj(payload.get('retry'))
        input_item = payload.get('input') or {}
        triage = payload.get('audit_triage') or input_item.get('_audit_triage') or {}
        analysis_row = payload.get('analysis') or {}
        analysis = obj(analysis_row.get('result_json')) if isinstance(analysis_row, dict) else {}
        trace = payload.get('audit_trace') or input_item.get('_audit_trace') or []
        steps = [
            f"{event.get('stage')}: {DECISION_LABELS.get(event.get('outcome'), event.get('outcome', ''))}"
            for event in trace if isinstance(event, dict) and event.get('stage')
        ]
        reason = (payload.get('reason') or retry.get('reason') or triage.get('reason')
                  or (trace[-1].get('reason') if trace and isinstance(trace[-1], dict) else None))
        if not reason and payload.get('error_code'):
            reason = f"Ошибка обработки: {payload['error_code']}"
        if not reason and row['decision'] == 'NOISE' and analysis:
            if analysis.get('action') == 'NOISE' or analysis.get('is_relevant') is False:
                reason = 'ИИ-редактор не подтвердил тематическую значимость материала.'
            elif analysis.get('russia_cis_impact') != 'DIRECT':
                reason = 'Не подтверждено прямое влияние на Россию или СНГ.'
        if not reason:
            reason = 'Подробная причина в этой исторической записи не сохранена.'
        history.append({
            'at': row['created_at'],
            'decision': DECISION_LABELS.get(row['decision'], row['decision']),
            'reason': str(reason)[:400],
            'steps': steps[-8:],
        })
    return history


def retry_queue_positions(db, config, now):
    """Mirror retry eligibility/order so the dashboard can explain queue delay."""
    tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if not {'items', 'sources', 'app_state'} <= tables:
        return {}, 0, 0
    newsroom = config.get('newsroom', {})
    ai = config.get('ai', {})
    source_ids = {row[0] for row in db.execute('SELECT source_id FROM sources WHERE active=1')}
    if newsroom.get('story_watch_enabled'):
        source_ids.update(row[0] for row in db.execute("SELECT source_id FROM sources WHERE url LIKE 'story-watch://%'"))
    source_ids = sorted(source_ids)
    if not source_ids:
        return {}, 0, 0
    analysis_budget = max(0, int(newsroom.get('analysis_per_cycle', 25)))
    triage_budget = max(0, int(newsroom.get('triage_per_cycle', 12)))
    if newsroom.get('story_watch_enabled'):
        reserve = min(3, analysis_budget, triage_budget)
        analysis_budget -= reserve
        triage_budget -= reserve
    batch = max(0, int(newsroom.get('retry_items_per_cycle', 2)))
    if ai.get('triage_enabled'):
        batch = min(batch, triage_budget)
    batch = min(batch, analysis_budget)
    if batch <= 0:
        return {}, 0, 0
    placeholders = ','.join('?' for _ in source_ids)
    due_filter = ''
    args = [now.isoformat(), MAX_AUTOMATIC_RETRIES, *source_ids, MAX_AUTOMATIC_RETRIES]
    if ai.get('triage_enabled'):
        due_filter = ("AND COALESCE((SELECT CASE WHEN json_valid(value) "
                      "THEN julianday(json_extract(value,'$.next_at')) ELSE 0 END "
                      "FROM app_state WHERE key='selection_retry:'||items.item_id),0)<=julianday(?) ")
        args.append(now.isoformat())
    sql = ("SELECT item_id FROM items WHERE (disposition IN ('AI_RETRY','PRIMARY_RETRY') OR "
           "(disposition='WAITING_CONFIRMATION' "
           "AND julianday(processed_at)<julianday(?,'-5 minutes') "
           "AND COALESCE((SELECT CAST(value AS INTEGER) FROM app_state "
           "WHERE key='editor_retry:'||items.item_id),0)<?)) "
           f"AND source_id IN ({placeholders}) "
           "AND COALESCE((SELECT CASE WHEN json_valid(value) "
           "THEN CAST(json_extract(value,'$.attempts') AS INTEGER) ELSE 0 END "
           "FROM app_state WHERE key='selection_retry:'||items.item_id),0)<? "
           f"{due_filter} ORDER BY COALESCE(processed_at,discovered_at),discovered_at,item_id")
    rows = db.execute(sql, args).fetchall()
    positions = {row['item_id']: index for index, row in enumerate(rows, 1)}
    return positions, len(rows), batch


def same_processing_run(item, post):
    # Older posts do not store a primary URL. Source + same processing timestamp
    # provide a narrower fallback than simply sharing a story.
    ids = post.get('source_ids') or []
    if isinstance(ids, str):
        try: ids = json.loads(ids)
        except ValueError: return False
    if item['source_id'] not in ids or not item['processed_at']:
        return False
    try:
        delta = datetime.fromisoformat(post['created_at']) - datetime.fromisoformat(item['processed_at'])
        return abs(delta.total_seconds()) <= 2
    except (TypeError, ValueError):
        return False


def publication_trace(db, post_id, tables):
    """Expose the saved delivery receipt without returning raw Telegram payloads."""
    if 'publication_attempts' not in tables:
        return None
    attempt = db.execute(
        'SELECT attempt_id,status,attempt_count,telegram_message_id,error_code,created_at,updated_at '
        'FROM publication_attempts WHERE post_id=? ORDER BY attempt_id DESC LIMIT 1', (post_id,)
    ).fetchone()
    if not attempt:
        return None
    events = []
    if 'delivery_events' in tables:
        events = [
            {'status': DELIVERY_LABELS.get(row['status'], row['status']), 'at': row['created_at']}
            for row in db.execute(
                'SELECT status,created_at FROM delivery_events WHERE attempt_id=? ORDER BY event_id',
                (attempt['attempt_id'],)
            ).fetchall()
        ]
    return {
        'status': attempt['status'],
        'status_label': DELIVERY_LABELS.get(attempt['status'], 'Статус отправки не определён'),
        'attempt_count': attempt['attempt_count'],
        'telegram_message_id': attempt['telegram_message_id'],
        'error_code': attempt['error_code'],
        'created_at': attempt['created_at'],
        'updated_at': attempt['updated_at'],
        'events': events,
    }


def pipeline_snapshot(db, config, params, posts, now=None):
    now = now or datetime.now(timezone.utc)
    period = params.get('period', ['48'])[0]
    if period not in {'24', '48', '168', 'all'}:
        period = '48'
    query = params.get('q', [''])[0].strip().casefold()[:200]
    stage = params.get('stage', ['all'])[0]
    try:
        offset = max(0, int(params.get('offset', ['0'])[0]))
    except ValueError:
        offset = 0
    where, args = '', []
    if period != 'all':
        where = 'WHERE julianday(i.discovered_at)>=julianday(?)'
        args = [(now - timedelta(hours=int(period))).isoformat()]
    tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    story_join = 'LEFT JOIN stories st USING(story_id)' if 'stories' in tables else ''
    retry_join = "LEFT JOIN app_state t ON t.key='selection_retry:'||i.item_id" if 'app_state' in tables else ''
    triage_join = "LEFT JOIN app_state tr ON tr.key='triage:'||i.item_id" if 'app_state' in tables else ''
    story_expr = 'st.headline' if 'stories' in tables else 'NULL'
    retry_expr = 't.value' if 'app_state' in tables else 'NULL'
    triage_expr = 'tr.value' if 'app_state' in tables else 'NULL'
    rows = db.execute('SELECT i.item_id,i.source_id,i.title,i.url,i.canonical_url,i.published_at,'
        'i.discovered_at,i.processed_at,i.disposition,i.story_id,i.primary_source_json,'
        's.name AS source_name,'+story_expr+' AS story_headline,a.created_at AS analyzed_at,a.result_json,'
        +retry_expr+' AS retry_json,'+triage_expr+' AS triage_json,f.is_interesting AS interest_vote '
        'FROM items i JOIN sources s USING(source_id) LEFT JOIN item_analysis a USING(item_id) '
        +story_join+' '+retry_join+' '+triage_join+' '
        'LEFT JOIN interest_feedback f USING(item_id) '
        + where + ' ORDER BY i.discovered_at DESC,i.item_id DESC', args).fetchall()
    by_story = {}
    for post in posts:
        by_story.setdefault(post['story_id'], []).append(post)
    counts = dict.fromkeys(dict(STAGES), 0)
    totals = dict(received=0, analyzed=0, primary_read=0, drafted=0, published=0)
    output = []
    for row in rows:
        item = dict(row)
        if query and query not in (item['title'] + ' ' + item['source_name'] + ' ' + item['url']).casefold():
            continue
        analysis = obj(item.pop('result_json'))
        primary = obj(item.pop('primary_source_json'))
        retry = obj(item.pop('retry_json'))
        triage = obj(item.pop('triage_json'))
        disposition = item['disposition']
        # A saved origin link is stronger evidence than the item's current
        # disposition, which may have changed during later story reconciliation.
        # Only inferred legacy matches remain limited to accepted dispositions.
        post = None
        post_link_method = None
        story_posts = by_story.get(item['story_id'], [])
        exact = [p for p in story_posts if p.get('origin_item_id') == item['item_id']]
        if exact:
            post = min(exact, key=lambda p: p['created_at'])
            post_link_method = 'EXPLICIT'
        elif disposition in {'NEW_STORY','UPDATE_CANDIDATE'}:
            urls = {u for u in [item['url'], item['canonical_url'], primary.get('url')] if u}
            legacy_posts = [p for p in story_posts
                            if p.get('origin_item_id') is None
                            and p['created_at'] >= item['discovered_at']]
            url_matches = [p for p in legacy_posts
                           if ((p.get('facts') or {}).get('primary_source') or {}).get('url') in urls]
            time_matches = [p for p in legacy_posts if same_processing_run(item, p)]
            if url_matches:
                post = min(url_matches, key=lambda p: p['created_at'])
                post_link_method = 'LEGACY_SOURCE_URL'
            elif time_matches:
                post = min(time_matches, key=lambda p: p['created_at'])
                post_link_method = 'LEGACY_PROCESSING_TIME'
        category = {'PRIMARY_RETRY':'primary', 'AI_RETRY':'ai',
                    'WAITING_CONFIRMATION':'confirmation', 'AGENT_CORRECTION_QUEUED':'correction',
                    'PENDING':'received'}.get(disposition, 'processed')
        reason = retry.get('reason') or REASONS.get(disposition, 'Обработка завершена; связанный пост не найден.')
        if disposition == 'NOISE' and triage.get('decision') == 'NOISE' and triage.get('reason'):
            reason = f"Предварительный ИИ-отбор: {triage['reason']}"
        elif disposition == 'DUPLICATE' and triage.get('decision') == 'DUPLICATE' and triage.get('reason'):
            reason = f"Предварительный ИИ-отбор: {triage['reason']}"
        elif disposition == 'NOISE' and analysis:
            if analysis.get('action') == 'NOISE' or analysis.get('is_relevant') is False:
                reason = 'Редакторский ИИ-разбор не подтвердил тематическую значимость.'
            elif analysis.get('russia_cis_impact') != 'DIRECT':
                reason = 'Редакторский ИИ-разбор не подтвердил прямое влияние на Россию или СНГ.'
        if disposition in {'NOISE','DUPLICATE','STALE','BASELINE_SKIPPED','UNDATED','EDITOR_REJECTED','REJECTED'}:
            category = 'filtered'
        if post:
            post['publication_trace'] = publication_trace(db, post['post_id'], tables)
            post['link_method'] = post_link_method
            if post['status'] == 'PUBLISHED':
                category, reason = 'published', 'Связанный пост по этому сюжету опубликован в канале.'
            elif post['status'] == 'PENDING':
                category, reason = 'review', post.get('auto_reason') or 'Черновик ожидает редакторского решения.'
            elif post['status'] == 'ON_HOLD':
                category, reason = 'confirmation', 'Черновик снят с публикации и оставлен на автоматическую перепроверку.'
            elif post['status'] == 'REJECTED':
                category, reason = 'filtered', 'Связанный черновик окончательно отклонён проверками допуска.'
        counts[category] += 1
        totals['received'] += 1
        totals['analyzed'] += bool(item['analyzed_at'])
        totals['primary_read'] += primary.get('status') == 'READ'
        totals['drafted'] += post is not None
        totals['published'] += category == 'published'
        item.update(stage=category, reason=reason,
            primary_status=PRIMARY_LABELS.get(primary.get('status'), 'Проверка не завершена' if primary else 'Нет сохранённой проверки'),
            primary_url=primary.get('url'), summary=analysis.get('summary_ru'),
            analyzed_at=item.get('analyzed_at'), retry_at=retry.get('next_at'),
            triage_decision=triage.get('decision'), triage_reason=triage.get('reason'),
            last_attempt_at=item.get('processed_at'),
            retry_attempts=int(retry.get('attempts') or 0),
            retry_limit=(MAX_AUTOMATIC_RETRIES if disposition in {'AI_RETRY', 'PRIMARY_RETRY', 'WAITING_CONFIRMATION'} else 0),
            retry_reason=retry.get('reason'),
            what_is_new=analysis.get('what_is_new'), issues=analysis.get('editorial_issues') or [],
            independent_note=analysis.get('independent_check_note'),
            interest_vote=item.get('interest_vote'),
            post=({k:post.get(k) for k in ('post_id','status','created_at','published_at','telegram_url','external_id',
                                            'text','auto_reason','auto_attempts','auto_last_error',
                                            'publication_trace','link_method')} if post else None))
        if stage == 'all' or stage == category:
            output.append(item)
    visible = output[offset:offset+30]
    queue_positions, queue_total, queue_batch = retry_queue_positions(db, config, now)
    has_decisions = 'agent_decisions' in tables
    for item in visible:
        item['retry_queue_position'] = queue_positions.get(item['item_id'])
        item['retry_queue_total'] = queue_total if item['retry_queue_position'] else 0
        item['retry_queue_batch'] = queue_batch if item['retry_queue_position'] else 0
        item['retry_queue_cycles'] = ((item['retry_queue_position'] + queue_batch - 1) // queue_batch
                                      if item['retry_queue_position'] and queue_batch else 0)
        history = decision_history(db, item['item_id'], has_decisions)
        item['decision_history'] = history
        item['analysis_status'] = (
            'Полный ИИ-разбор сохранён' if item.get('analyzed_at') else
            'Ожидает ИИ-разбора; повтор запланирован' if item['disposition'] == 'AI_RETRY' else
            'Ожидает прочитанного материала; полный разбор ещё не запускался' if item['disposition'] == 'PRIMARY_RETRY' else
            'Предварительный ИИ-отбор завершён; полный разбор не запускался' if item.get('triage_decision') else
            'Не запускался: материал завершил обработку до редакторского разбора' if item['stage'] in {'filtered','processed'} else
            'Ещё не завершён'
        )
        if history and history[-1].get('reason') and item['disposition'] in {'NOISE','DUPLICATE','REJECTED','EDITOR_REJECTED'}:
            if history[-1]['reason'] != 'Подробная причина в этой исторической записи не сохранена.':
                item['reason'] = history[-1]['reason']
    return {'stages':[{'key':k,'label':v,'count':counts[k]} for k,v in STAGES],
            'totals':totals,'total':len(output),'items':visible,
            'offset':offset,'limit':30,'period':period,'updated_at':now.isoformat()}
