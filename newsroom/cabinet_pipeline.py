"""Read-only editorial intake view. Counts represent saved items, not feed polls."""
import json
import hashlib
from datetime import datetime, timedelta, timezone
MAX_AUTOMATIC_RETRIES = 3  # Historical records only; this view never schedules work.



PATH_KEYS = ('received', 'first_filter', 'primary_read', 'analyzed', 'drafted', 'checked', 'published')

def sequential_progress(evidence):
    result = dict(evidence)
    reached = True
    for key in PATH_KEYS:
        reached = reached and bool(evidence.get(key))
        result[key] = reached
    return result

STAGES = [
    ('received', 'Ожидают обработки'), ('screening', 'Фильтр ключевиков'), ('primary', 'Чтение источника'),
    ('ai', 'Факты и черновик'), ('confirmation', 'Требуют уточнения'),
    ('drafting', 'Оформляются или исправляются посты'), ('technical', 'Остановлены ошибкой'),
    ('correction', 'Правки опубликованных постов'),
    ('review', 'Проверяются перед отправкой'), ('published', 'Опубликованы'),
    ('filtered', 'Отклонены'), ('processed', 'Сохранены без публикации'),
]
REASONS = {
    'TECHNICAL_ERROR': 'Обработка остановлена технической ошибкой; требуется исправление обработчика.',
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
    if newsroom.get('independent_processing'):
        return {}, 0, 0  # Continuous stage queues have no per-collection-cycle position.
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
    series_info = {}
    if 'news_series' in tables:
        series = db.execute('SELECT parts_json FROM news_series WHERE post_id=?', (post_id,)).fetchone()
        if series:
            parts = [dict(row) for row in db.execute(
                'SELECT delivery_key,status,telegram_message_id FROM publication_attempts WHERE post_id=? ORDER BY attempt_id', (post_id,))]
            total = len(json.loads(series['parts_json']))
            delivered = sum(part['status'] in {'SENT', 'CONFIRMED'} for part in parts)
            series_info = {'series_total': total, 'series_delivered': delivered, 'series_parts': parts}
    return {
        'status': attempt['status'],
        'status_label': (f"Доставлено частей: {series_info['series_delivered']}/{series_info['series_total']} · "
                         if series_info else '') + DELIVERY_LABELS.get(attempt['status'], 'Статус отправки не определён'),
        'attempt_count': attempt['attempt_count'],
        'telegram_message_id': attempt['telegram_message_id'],
        'error_code': attempt['error_code'],
        'created_at': attempt['created_at'],
        'updated_at': attempt['updated_at'],
        'events': events,
        **series_info,
    }


def pipeline_snapshot(db, config, params, posts, now=None):
    now = now or datetime.now(timezone.utc)
    period = params.get('period', ['48'])[0]
    if period not in {'24', '48', '168', 'all'}:
        period = '48'
    query = params.get('q', [''])[0].strip().casefold()[:200]
    stage = params.get('stage', ['all'])[0]
    bucket = params.get('bucket', ['all'])[0]
    milestone = params.get('milestone', ['all'])[0]
    try:
        offset = max(0, int(params.get('offset', ['0'])[0]))
    except ValueError:
        offset = 0
    where, args = '', []
    if period != 'all':
        where = 'WHERE julianday(i.discovered_at)>=julianday(?)'
        args = [(now - timedelta(hours=int(period))).isoformat()]
    tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    counter_epoch = None
    if 'app_state' in tables:
        epoch_row = db.execute("SELECT value FROM app_state WHERE key='pipeline_counter_epoch_v2'").fetchone()
        if epoch_row:
            counter_epoch = obj(epoch_row[0])
    story_join = 'LEFT JOIN stories st USING(story_id)' if 'stories' in tables else ''
    retry_join = "LEFT JOIN app_state t ON t.key='selection_retry:'||i.item_id" if 'app_state' in tables else ''
    triage_join = "LEFT JOIN app_state tr ON tr.key='triage:'||i.item_id" if 'app_state' in tables else ''
    story_expr = 'st.headline' if 'stories' in tables else 'NULL'
    retry_expr = 't.value' if 'app_state' in tables else 'NULL'
    triage_expr = 'tr.value' if 'app_state' in tables else 'NULL'
    revision_expr = 'i.ingest_revision' if any(r[1] == 'ingest_revision' for r in db.execute('PRAGMA table_info(items)')) else 'NULL'
    # The cabinet needs read evidence, not the archived article body. Project it
    # before SQLite sorts the rows so large source texts never fill the sorter.
    primary_expr = "CASE WHEN json_valid(i.primary_source_json) THEN json_object(" + ','.join(
        "'" + key + "',i.primary_source_json -> '$." + key + "'"
        for key in ('status', 'url', '_material_read', '_material_url')
    ) + ") ELSE '{}' END"
    rows = db.execute('SELECT '+revision_expr+' AS ingest_revision,i.item_id,i.source_id,i.title,i.url,i.canonical_url,i.published_at,'
        'i.discovered_at,i.processed_at,i.disposition,i.story_id,'+primary_expr+' AS primary_source_json,'
        's.name AS source_name,'+story_expr+' AS story_headline,a.created_at AS analyzed_at,a.result_json,'
        +retry_expr+' AS retry_json,'+triage_expr+' AS triage_json,f.is_interesting AS interest_vote '
        'FROM items i JOIN sources s USING(source_id) LEFT JOIN item_analysis a USING(item_id) '
        +story_join+' '+retry_join+' '+triage_join+' '
        'LEFT JOIN interest_feedback f USING(item_id) '
        + where + ' ORDER BY julianday(i.discovered_at) DESC,i.item_id DESC', args).fetchall()
    by_story = {}
    for post in posts:
        by_story.setdefault(post['story_id'], []).append(post)
    posts_by_id = {p['post_id']: p for p in posts}
    counts = dict.fromkeys(dict(STAGES), 0)
    totals = dict(received=0, first_filter=0, analyzed=0, primary_read=0, selected=0, drafted=0, checked=0, published=0)
    evidence_totals = dict(totals)
    revisions = {row['item_id']: row['ingest_revision'] for row in rows}
    item_marks = 'SELECT value FROM json_each(?)'
    item_ids = (json.dumps(list(revisions)),)
    keyword_passed = set()
    keyword_results = {}
    if 'material_stage_results' in tables:
        for r in db.execute(
            "SELECT r.item_id,r.revision,r.result_json FROM material_stage_results r "
            f"WHERE r.item_id IN ({item_marks}) AND r.stage='screening' AND json_valid(r.result_json) "
            "AND json_extract(r.result_json,'$.kind')='keyword_prefilter' ORDER BY r.created_at", item_ids):
            if r['revision'] != revisions[r['item_id']]:
                continue
            result = obj(r['result_json'])
            keyword_results[r[0]] = result
            if result.get('passed') is True:
                keyword_passed.add(r[0])
            else:
                keyword_passed.discard(r[0])
    flow_by_item = {}
    if 'material_stage_state' in tables:
        from .material_flow import LABELS
        for row in db.execute(
            f'SELECT f.* FROM material_stage_state f WHERE f.item_id IN ({item_marks}) '
            'ORDER BY f.updated_at', item_ids):
            if row['revision'] != revisions[row['item_id']]:
                continue
            point = {key: row[key] for key in row.keys() if key not in {'item_id', 'revision'}}
            point['label'] = LABELS.get(point['stage'], point['stage'])
            elapsed = max(0, (now - datetime.fromisoformat(point['updated_at'])).total_seconds())
            if point['status'] == 'RUNNING':
                point['work_seconds'] += elapsed
            elif point['status'] in {'READY', 'WAITING', 'ERROR'}:
                point['wait_seconds'] += elapsed
            flow_by_item.setdefault(row['item_id'], []).append(point)
    edition = edition_progress(db, tables)
    output = []
    for row in rows:
        item = dict(row)
        item.pop('ingest_revision', None)
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
                    'PENDING':'received', 'TECHNICAL_ERROR':'technical'}.get(disposition, 'processed')
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
        if 'material_stage_state' in tables:
            item['checkpoints'] = flow_by_item.get(item['item_id'], [])
            if post and post.get('publication_trace'):
                trace = post['publication_trace']
                for checkpoint in item['checkpoints']:
                    if checkpoint['stage'] == 'delivery':
                        checkpoint['status'] = 'DONE' if trace['status'] == 'CONFIRMED' and trace.get('telegram_message_id') else 'WAITING'
                        checkpoint['reason'] = trace['status_label']
                        checkpoint['block_kind'] = 'delivery_unknown' if trace['status'] == 'UNKNOWN' else checkpoint['block_kind']
            active_checkpoint = next((x for x in reversed(item['checkpoints']) if x['status'] in {'READY', 'RUNNING', 'WAITING', 'ERROR'}), None)
            if active_checkpoint and category not in {'published', 'filtered'} and (category != 'processed' or disposition in {'NEW_STORY', 'UPDATE_CANDIDATE'}):
                item['current_checkpoint'] = active_checkpoint
                if not post:
                    category = {'screening': 'screening', 'reading': 'primary', 'analysis': 'ai',
                                'drafting': 'drafting', 'gate': 'review', 'delivery': 'review'}.get(active_checkpoint['stage'], category)
                if active_checkpoint['status'] == 'ERROR' or active_checkpoint['block_kind'] == 'technical':
                    category = 'technical'
                if active_checkpoint['reason']:
                    reason = active_checkpoint['reason']
        latest = edition.get(item['item_id'])
        if latest and latest.get('archived') and category in {'published', 'filtered'}:
            latest = None
        if latest:
            category, reason = latest['category'], latest['reason']
            if latest.get('post_id') in posts_by_id:
                post = posts_by_id[latest['post_id']]
                post['link_method'] = 'EXPLICIT'
                post['publication_trace'] = publication_trace(db, post['post_id'], tables)
        counts[category] += 1
        proof = ((post.get('facts') or {}).get('final_text_check') or {}) if post else {}
        # A Telegram edit changes the display, not the completed pre-send check.
        # Drafts still require proof of their current text. Never infer a check
        # merely from PUBLISHED: verify the stored original against its proof.
        checked_text = ((post.get('saved_text', post.get('text')) if category == 'published'
                         else post.get('text')) or '') if post else ''
        progress = {
            'received': True,
            'first_filter': item['item_id'] in keyword_passed,
            'analyzed': bool(item['analyzed_at']),
            # Reading is a saved fact, independent of a later topic decision.
            'primary_read': primary.get('status') == 'READ' or primary.get('_material_read') is True,
            'selected': analysis.get('is_relevant') is True or post is not None,
            'drafted': post is not None,
            'checked': bool(post and proof.get('assembled_sha256') and proof['assembled_sha256'] == hashlib.sha256(checked_text.encode()).hexdigest()),
            'published': category == 'published',
        }
        if latest:
            progress.update(latest['progress'])
        original_progress = dict(progress)
        progress = sequential_progress(progress)
        in_counter_cohort = not counter_epoch or item['item_id'] > counter_epoch['after_item_id']
        for key, passed in original_progress.items():
            evidence_totals[key] += bool(passed and in_counter_cohort)
        for key, passed in progress.items():
            totals[key] += bool(passed and in_counter_cohort)
        terminal = category in {'published', 'filtered', 'processed'}
        try:
            discovered = datetime.fromisoformat(item['discovered_at'].replace('Z', '+00:00'))
            if discovered.tzinfo is None:
                discovered = discovered.replace(tzinfo=timezone.utc)
        except (ValueError, TypeError, AttributeError):
            discovered = None
        slow = bool(not terminal and discovered and (now - discovered).total_seconds() >= 15 * 60)
        trace = (post or {}).get('publication_trace') or {}
        needs_attention = bool(not terminal and (slow or category == 'technical' or trace.get('status') == 'UNKNOWN'))
        item['needs_attention'] = needs_attention
        item['keyword_filter'] = keyword_results.get(item['item_id'])
        item.update(stage=category, reason=reason,
            material_status=('Материал прочитан' if primary.get('status') == 'READ' or primary.get('_material_read') is True else 'Нет прочитанного пригодного материала'),
            material_url=primary.get('url') if primary.get('status') == 'READ' else primary.get('_material_url') if primary.get('_material_read') is True else None,
            primary_status=PRIMARY_LABELS.get(primary.get('status'), 'Проверка не завершена' if primary else 'Нет сохранённой проверки'),
            primary_url=primary.get('url'), summary=analysis.get('summary_ru'),
            analyzed_at=item.get('analyzed_at'), retry_at=retry.get('next_at'),
            triage_decision=triage.get('decision'), triage_reason=triage.get('reason'),
            last_attempt_at=item.get('processed_at'),
            retry_attempts=int(retry.get('attempts') or 0),
            retry_limit=(MAX_AUTOMATIC_RETRIES if disposition in {'AI_RETRY', 'PRIMARY_RETRY', 'WAITING_CONFIRMATION'} else 0),
            retry_reason=retry.get('reason'),
            what_is_new=analysis.get('what_is_new'), issues=analysis.get('editorial_issues') or [],
            date_check=analysis.get('development_date_check'),
            memory_issues=analysis.get('memory_issues') or [],
            publication_recommendation=analysis.get('publication_recommendation'),
            source_review_required=analysis.get('source_review_required') is True,
            source_review_issues=analysis.get('source_review_issues') or [],
            verification_questions=analysis.get('verification_questions') or [],
            independent_note=analysis.get('independent_check_note'),
            interest_vote=item.get('interest_vote'),
            post=({k:post.get(k) for k in ('post_id','status','created_at','published_at','telegram_url','external_id',
                                            'text','auto_reason','auto_attempts','auto_last_error',
                                            'publication_trace','link_method','ai_metrics')} if post else None))
        if latest:
            item.pop('current_checkpoint', None)
            item['archived'] = latest.get('archived', False)
            item['material_status'] = 'Материал прочитан' if latest['progress'].get('primary_read', progress['primary_read']) else 'Текст ещё не получен'
            item['analysis_status'] = 'Разбор сохранён' if latest['progress'].get('analyzed', progress['analyzed']) else latest['reason']
        in_bucket = (bucket == 'all' or bucket == 'work' and not terminal
                     or bucket == 'attention' and needs_attention
                     or bucket == 'published' and category == 'published'
                     or bucket == 'closed' and category in {'filtered', 'processed'})
        if in_bucket and (milestone == 'all' or in_counter_cohort and progress.get(milestone, False)) and (stage == 'all' or stage == category):
            output.append(item)
    visible = output[offset:offset+30]
    queue_positions, queue_total, queue_batch = retry_queue_positions(db, config, now)
    has_decisions = 'agent_decisions' in tables
    for item in visible:
        item['processing_job'] = None
        if 'processing_jobs' in tables:
            job = db.execute("SELECT job_id,category,status,attempts,next_at,outcome,error_code FROM processing_jobs "
                             "WHERE item_id=? ORDER BY job_id DESC LIMIT 1", (item['item_id'],)).fetchone()
            if job:
                item['processing_job'] = {key: job[key] for key in job.keys() if key != 'job_id'}
                if 'processing_job_events' in tables:
                    recovery = db.execute("SELECT status,created_at FROM processing_job_events WHERE job_id=? "
                        "AND status IN ('LEASE_EXPIRED','CYCLE_ABORTED') ORDER BY event_id DESC LIMIT 1",
                        (job['job_id'],)).fetchone()
                    item['processing_job']['last_recovery'] = dict(recovery) if recovery else None
        item['retry_queue_position'] = queue_positions.get(item['item_id'])
        item['retry_queue_total'] = queue_total if item['retry_queue_position'] else 0
        item['retry_queue_batch'] = queue_batch if item['retry_queue_position'] else 0
        item['retry_queue_cycles'] = ((item['retry_queue_position'] + queue_batch - 1) // queue_batch
                                      if item['retry_queue_position'] and queue_batch else 0)
        history = decision_history(db, item['item_id'], has_decisions)
        item['decision_history'] = history
        item['analysis_status'] = edition.get(item['item_id'], {}).get('reason') or (
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
    funnel = [
        ('received', 'Собрано', 'Сборщик',
         'Сохраняет публикации из подключённых источников.',
         'Уникальные материалы, полученные за выбранный период. Повторные обращения к тому же материалу не увеличивают число.'),
        ('first_filter', 'Отобрано по теме', 'Фильтр ключевиков',
         'Проверяет материал по действующим ключевым словам и сохраняет решение.',
         'Материалы с допуском фильтра для текущей версии. Повторная проверка может изменить этот результат; прежний разбор или публикация при этом сохраняются.'),
        ('primary_read', 'Текст получен', 'Сборщик и чтение',
         'Сохраняет доступный текст Telegram или прочитанной статьи, в том числе до тематического отбора.',
         'Материалы с сохранённым текстом независимо от допуска по теме. Отклонённые материалы тоже учитываются. Отсутствующий текст не восстанавливается по факту публикации.'),
        ('analyzed', 'Разобрано ИИ', 'Редакция',
         'Разбирает сохранённые материалы и готовит новости по правилам редакции.',
         'Материалы с сохранённым результатом разбора, включая исключённые сюжеты и дубликаты. Прежние результаты учитываются независимо от текущего допуска фильтра.'),
        ('drafted', 'Пост сохранён', 'Редакция',
         'Сохраняет подготовленные тексты; несколько материалов могут объединяться в пост, а один материал — давать несколько постов.',
         'Материалы, связанные хотя бы с одним сохранённым текстом поста. Это число материалов, а не постов. Резервный текст также учитывается, даже если разбор ИИ не завершился.'),
        ('checked', 'Текст проверен', 'Проверяющий',
         'Сверяет подготовленный текст с сохранёнными источниками и фиксирует результат.',
         'Материалы с сохранённой проверкой связанного текста: текущего черновика или версии перед публикацией. Наличие проверки не означает отсутствие замечаний. При отсутствии сохранённого подтверждения этап не засчитывается.'),
        ('published', 'Опубликовано', 'Telegram',
         'Отправляет подготовленный пост и сохраняет результат доставки.',
         'Материалы, связанные хотя бы с одним опубликованным постом. Это число материалов, а не сообщений в канале. Прежние публикации сохраняются независимо от текущего допуска фильтра.'),
    ]
    return {'evidence_totals':evidence_totals, 'counter_started_at': (counter_epoch or {}).get('started_at'), 'funnel': [
                {'key': key, 'label': label, 'count': totals[key], 'owner': owner, 'action': action, 'description': 'Уникальные материалы с подтверждёнными результатами этого и всех предыдущих этапов для текущего допуска по теме. Независимые исторические результаты показаны в отдельной сводке.'}
                for key, label, owner, action, description in funnel],
            'stages':[{'key':k,'label':v,'count':counts[k]} for k,v in STAGES],
            'totals':totals,'total':len(output),'items':visible,
            'offset':offset,'limit':30,'period':period,'bucket':bucket,'milestone':milestone,'updated_at':now.isoformat()}


def cabinet_posts(db, config):
    """Read historical post evidence without invoking removed editorial modules."""
    from .cli import _telegram_message_url
    output = []
    for row in db.execute('SELECT p.*,s.headline FROM posts p LEFT JOIN stories s USING(story_id) ORDER BY p.created_at DESC'):
        post = dict(row)
        post['facts'] = obj(post.get('fact_check_result'))
        post['saved_text'] = post.get('text') or ''
        post['source_ids'] = obj(post.get('source_ids'))
        post['telegram_url'] = _telegram_message_url(config, post['external_id']) if post.get('external_id') else None
        output.append(post)
    from .post_metrics import for_posts
    metrics = for_posts(db, [p['post_id'] for p in output])
    for post in output:
        post['ai_metrics'] = metrics[post['post_id']]
    return output


def edition_progress(db, tables):
    """Project current edition evidence onto the approved seven material steps."""
    if not {'edition_materials', 'edition_documents', 'edition_jobs', 'edition_checks'} <= tables:
        return {}
    from .edition.views import LABELS
    from .edition.store import read
    result = {}
    documents = {}
    for d in db.execute('SELECT * FROM edition_documents ORDER BY updated_at'):
        for material_id in obj(d['group_json']).get('material_ids', []):
            documents.setdefault(material_id, []).append(dict(d))
    job_map = {}
    for j in db.execute('SELECT * FROM edition_jobs ORDER BY updated_at'):
        for material_id in read(j['material_ids_json'], []):
            job_map.setdefault(material_id, []).append(dict(j))
    admissions = {r['material_id']: dict(r) for r in db.execute('SELECT * FROM edition_admissions')} if 'edition_admissions' in tables else {}
    proofs = {(r[0],r[1]) for r in db.execute('SELECT document_id,rendered_hash FROM edition_checks')}
    sent = {r[0] for r in db.execute("SELECT post_id FROM posts WHERE status='PUBLISHED' AND external_id IS NOT NULL")}
    activation = db.execute("SELECT value FROM app_state WHERE key='edition_v2_activation'").fetchone() if 'app_state' in tables else None
    for row in db.execute('SELECT m.material_id,m.item_id,m.revision,m.queue_state,m.eligible,m.received_at,(length(m.content)>0) AS content,i.ingest_revision,i.disposition FROM edition_materials m JOIN items i USING(item_id) ORDER BY m.material_id'):
        m = dict(row)
        if m['revision'] != m['ingest_revision']:
            continue
        docs = documents.get(m['material_id'], [])
        jobs = job_map.get(m['material_id'], [])
        # Archived snapshots do not supersede legacy processing evidence.
        if not jobs and not docs and m['queue_state'] == 'ARCHIVE':
            if m['disposition'] in {'PENDING', 'PRIMARY_RETRY', 'AI_RETRY', 'WAITING_CONFIRMATION', 'TECHNICAL_ERROR', 'NEW_STORY', 'UPDATE_CANDIDATE', 'AGENT_CORRECTION_QUEUED'} and activation and m['received_at'] < activation[0]:
                result[m['item_id']] = {
                    'category': 'processed',
                    'archived': True,
                    'reason': 'Архивный материал: получен до запуска новой редакции и не включён в её очередь. Результат прежней обработки не сохранён.',
                    'progress': {},
                }
            continue
        checks = False
        published = False
        drafted = False
        analyzed = False
        for d in docs:
            analyzed |= bool(obj(d['draft_json']))
            drafted |= bool(d['rendered_text'])
            checks |= (d['document_id'], hashlib.sha256(d['rendered_text'].encode()).hexdigest()) in proofs
            published |= d['post_id'] in sent
        state = jobs[-1]['state'] if jobs else m['queue_state']
        reason = LABELS.get(state, state)
        excluded = [entry for job in jobs[-1:] for entry in read(job.get('reasons_json'), [])
                    if isinstance(entry, dict) and entry.get('material_id') == m['material_id']]
        admission = admissions.get(m['material_id'])
        states = {d['state'] for d in docs}
        if docs:
            if states <= {'PUBLISHED', 'COVERED', 'DUPLICATE', 'FILTERED', 'CANCELLED'}:
                category = 'published' if published else 'filtered'
                reason = ('Связанный пост опубликован в канале.' if published else
                          'Материал завершён без публикации.')
                saved_reasons = [entry.get('reason') for d in docs
                                 for entry in read(d.get('reasons_json'), [])
                                 if isinstance(entry, dict) and entry.get('reason')]
                if not published and saved_reasons:
                    reason = '; '.join(saved_reasons)
            elif states & {'UNKNOWN', 'INCOMPLETE', 'REJECTED'}:
                category = 'technical'
                problem = 'UNKNOWN' if 'UNKNOWN' in states else sorted(states & {'INCOMPLETE', 'REJECTED'})[0]
                reason = LABELS.get(problem, problem)
            elif 'LANGUAGE_PENDING' in states:
                category, reason = 'confirmation', LABELS.get('LANGUAGE_PENDING', 'Ожидает русского перевода')
            elif states & {'CHECKING', 'READY', 'SENDING'}:
                category = 'review'
            else:
                category = 'ai'
        elif excluded:
            category = 'filtered'
            reason = '; '.join(entry['reason'] for entry in excluded if entry.get('reason')) or 'Исключён редакцией.'
            analyzed = True
        elif m['queue_state'] in {'DUPLICATE', 'SUPERSEDED'}:
            category = 'filtered'
            reason = admission['reason'] if admission else 'Повтор или заменённая версия; отдельная публикация не требуется.'
        elif state in {'FILTERED', 'CANCELLED'}:
            category, reason = 'filtered', LABELS.get(state, 'Завершён без публикации')
        elif state in {'PUBLISHED', 'DONE'}:
            category, reason = 'processed', 'Обработка редакцией завершена; отдельный пост не связан с материалом.'
        elif state in {'UNKNOWN', 'INCOMPLETE', 'REJECTED'}:
            category = 'technical'
        elif state in {'CHECKING', 'READY', 'SENDING'}:
            category = 'review'
        else:
            category = 'ai'
            reason = LABELS.get(state, 'Ожидает подготовки редакцией.')
        result[m['item_id']] = {'category':category, 'reason':reason, 'post_id':next((d['post_id'] for d in reversed(docs) if d.get('post_id')),None),
            'progress': {'first_filter': bool(m['eligible']),
                         'primary_read':bool(m['content']),
                         'analyzed':analyzed, 'drafted':drafted,
                         'checked':checks, 'published':published}}
    return result
