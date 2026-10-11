"""Read selected discoveries before capturing immutable editorial inputs."""
from __future__ import annotations

from .material_flow import mark, put
from .runtime import stamp


def prepare(db, item_id, item, source, settings):
    if settings.get('_edition_enabled') is not True:
        return
    row = db.execute('SELECT disposition,ingest_revision FROM items WHERE item_id=?', (item_id,)).fetchone()
    screened = db.execute("SELECT status FROM material_stage_state WHERE item_id=? AND revision=? AND stage='screening'",
                          (item_id, row['ingest_revision'])).fetchone()
    if row['disposition'] != 'PENDING' or not screened or screened[0] != 'DONE':
        return
    completed = db.execute("SELECT status FROM material_stage_state WHERE item_id=? AND revision=? AND stage='reading'",
                           (item_id, row['ingest_revision'])).fetchone()
    if completed and completed[0] in ('DONE', 'ERROR'):
        return  # One attempt per discovery revision; never retry a failed read blindly.
    from .agent_control import require_enabled
    control = settings.get('_agent_control_config') or {}
    require_enabled(control)
    from .core import (_read_feed_article, _read_telegram_primary,
                       _primary_source_from_item, _stored_primary, _archive_item_revision)
    before = db.execute('SELECT * FROM items WHERE item_id=?', (item_id,)).fetchone()
    mark(db, item_id, 'reading', 'RUNNING', 'Загрузка текста источника перед редакционной подготовкой.')
    db.commit()  # Network requests must not hold a database write lock.
    source_type = source['type']
    if source_type in ('rss', 'web', 'google_news', 'web_search'):
        if item.get('material_read') is not True:
            _read_feed_article(item, item.get('publisher_name') or source['name'])
    elif source_type == 'telegram':
        _read_telegram_primary(item, source)
    elif source_type == 'x':
        item['material_read'] = bool(str(item.get('content') or '').strip())
        item['material_url'] = item['url']
    require_enabled(control)
    status = item.get('primary_source_status') or 'NOT_CHECKED'
    metadata = _stored_primary(item, _primary_source_from_item(item, status), status)
    read = item.get('material_read') is True and bool(str(item.get('content') or '').strip())
    linked_error = status in ('UNREADABLE', 'ARTICLE_UNREADABLE')
    outcome = 'READ' if read and not linked_error else 'PARTIAL' if str(item.get('content') or item.get('description') or '').strip() else 'UNAVAILABLE'
    result = {'outcome': outcome, 'material_read': read, 'primary_status': status,
              'error': item.get('primary_source_error'),
              'characters': len(item.get('content') or ''), 'read_at': stamp()}
    _archive_item_revision(db, item_id, before)
    # Keep URL identity, feed hashes, revision and receipt time intact. A repeated
    # poll must compare against the discovery, not its downloaded article body.
    db.execute('UPDATE items SET title=?,description=?,content=?,primary_source_json=? WHERE item_id=?',
               (item['title'], item.get('description') or '', item.get('content') or '', metadata, item_id))
    put(db, item_id, 'reading', 'source-reading-v1', result)
    mark(db, item_id, 'reading', 'DONE',
         'Текст источника прочитан и сохранён.' if outcome == 'READ' else
         'Загрузка завершена; редакция продолжает выпуск по доступным сохранённым фрагментам.')
    db.commit()
