"""Read-only editorial intake view. Counts represent saved items, not feed polls."""
import json
from datetime import datetime, timedelta, timezone
from .triage import MAX_AUTOMATIC_RETRIES

STAGES = [
    ('received', 'Ждут обработки'), ('primary', 'Чтение источника'),
    ('ai', 'Ждут анализа'), ('confirmation', 'Автоматическая перепроверка'),
    ('review', 'Автоматическая публикация'), ('published', 'Опубликованы'),
    ('filtered', 'Отфильтрованы'), ('processed', 'Завершены без публикации'),
]
REASONS = {
    'PENDING': 'Материал получен. Результат обработки ещё не сохранён.',
    'PRIMARY_RETRY': 'Пока нет прочитанного пригодного материала. Агент повторит чтение; отдельный первоисточник не обязателен.',
    'AI_RETRY': 'Разбор ИИ не завершён. Материал оставлен на повторную обработку.',
    'WAITING_CONFIRMATION': 'Проверки не пройдены; агент повторит редакционный разбор до трёх раз и затем автоматически решит судьбу материала.',
    'NOISE': 'Не прошёл тематический или редакционный отбор.',
    'DUPLICATE': 'Повтор уже известного сюжета; отдельный пост не создан.',
    'STALE': 'Возраст материала превысил допустимое окно свежести.',
    'BASELINE_SKIPPED': 'Материал старше окна загрузки при подключении источника.',
    'UNDATED': 'Не удалось определить дату публикации.',
    'EDITOR_REJECTED': 'ИИ не рекомендовал материал к публикации.',
    'REJECTED': 'Материал автоматически отклонён после трёх безуспешных повторных проверок.',
    'STORE_ONLY': 'Материал сохранён в памяти, но существенного нового повода для отдельного поста не добавил.',
}
PRIMARY_LABELS = {'READ': 'Прочитан', 'NO_LINK': 'Ссылка не найдена',
                  'ARTICLE_UNREADABLE': 'Статья не прочитана', 'UNREADABLE': 'Не удалось прочитать',
                  'NOT_FOUND': 'Не найден', 'OCR_REVIEW': 'Текст извлечён через OCR; нужна сверка'}


def obj(raw):
    try:
        result = json.loads(raw or '{}')
        return result if isinstance(result, dict) else {}
    except (ValueError, TypeError):
        return {}


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
    story_expr = 'st.headline' if 'stories' in tables else 'NULL'
    retry_expr = 't.value' if 'app_state' in tables else 'NULL'
    rows = db.execute('SELECT i.item_id,i.source_id,i.title,i.url,i.canonical_url,i.published_at,'
        'i.discovered_at,i.processed_at,i.disposition,i.story_id,i.primary_source_json,'
        's.name AS source_name,'+story_expr+' AS story_headline,a.created_at AS analyzed_at,a.result_json,'
        +retry_expr+' AS retry_json,f.is_interesting AS interest_vote '
        'FROM items i JOIN sources s USING(source_id) LEFT JOIN item_analysis a USING(item_id) '
        +story_join+' '+retry_join+' '
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
        disposition = item['disposition']
        # The legacy schema has no post.item_id. Only associate accepted items with
        # a post with matching primary URL or legacy processing metadata; never promote duplicate/held items.
        urls = {u for u in [item['url'], item['canonical_url'], primary.get('url')] if u}
        candidates = [p for p in by_story.get(item['story_id'], [])
                      if ((p['facts'].get('primary_source') or {}).get('url') in urls or same_processing_run(item, p))
                      and p['created_at'] >= item['discovered_at']]
        post = min(candidates, key=lambda p: p['created_at']) if candidates and disposition in {'NEW_STORY','UPDATE_CANDIDATE'} else None
        category = {'PRIMARY_RETRY':'primary', 'AI_RETRY':'ai',
                    'WAITING_CONFIRMATION':'confirmation', 'PENDING':'received'}.get(disposition, 'processed')
        reason = retry.get('reason') or REASONS.get(disposition, 'Обработка завершена; связанный пост не найден.')
        if disposition in {'NOISE','DUPLICATE','STALE','BASELINE_SKIPPED','UNDATED','EDITOR_REJECTED','REJECTED'}:
            category = 'filtered'
        if post:
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
            last_attempt_at=item.get('processed_at'),
            retry_attempts=int(retry.get('attempts') or 0),
            retry_limit=(MAX_AUTOMATIC_RETRIES if disposition in {'AI_RETRY', 'PRIMARY_RETRY', 'WAITING_CONFIRMATION'} else 0),
            retry_reason=retry.get('reason'),
            what_is_new=analysis.get('what_is_new'), issues=analysis.get('editorial_issues') or [],
            independent_note=analysis.get('independent_check_note'),
            interest_vote=item.get('interest_vote'),
            post=({k:post.get(k) for k in ('post_id','status','created_at','published_at','telegram_url','text','auto_reason','auto_attempts','auto_last_error')} if post else None))
        if stage == 'all' or stage == category:
            output.append(item)
    return {'stages':[{'key':k,'label':v,'count':counts[k]} for k,v in STAGES],
            'totals':totals,'total':len(output),'items':output[offset:offset+30],
            'offset':offset,'limit':30,'period':period,'updated_at':now.isoformat()}
