"""One-off URL intake that feeds a read article through the normal newsroom gates."""
from __future__ import annotations

import json
import time
import urllib.parse
from datetime import datetime, timezone

from .core import NOW, _registrable_domain, canonicalize, fetch_publisher_article
from .db import connect
from .locking import acquire_cycle_lock


class IntakeError(ValueError):
    """A user-facing, safe failure while submitting a single article URL."""



class IntakeBusyError(IntakeError):
    """The scheduled collection or another intake currently owns the cycle lock."""



def submit_article_url(config: dict, url: str, *, retry_after_credits_restored: bool = False,
                       retry_after_validation_fix: bool = False) -> dict:
    from .agent_control import require_enabled
    require_enabled(config)
    from .runtime import attach
    attach(config)
    url = str(url or "").strip()
    if not url or len(url) > 2000:
        raise IntakeError("Вставьте ссылку на статью длиной до 2000 символов.")
    lock = acquire_cycle_lock(config["newsroom"]["database"])
    if lock is None:
        raise IntakeBusyError("Сейчас выполняется другой цикл редакции. Повторите отправку позже.")
    try:
        return _submit_locked(config, url, retry_after_credits_restored,
                              retry_after_validation_fix)
    finally:
        lock.close()



def _submit_locked(config: dict, url: str, retry_after_credits_restored: bool = False,
                   retry_after_validation_fix: bool = False) -> dict:
    from .core import _validate_public_http_url

    try:
        _validate_public_http_url(url)
    except (ValueError, OSError) as exc:
        raise IntakeError("Нужна общедоступная HTTPS-ссылка на статью.") from exc

    host = urllib.parse.urlsplit(url).hostname or ""
    try:
        article = fetch_publisher_article(url, host, None, discover_primary=True,
                                         timeout=20, public_only=True)
    except Exception as exc:
        raise IntakeError(f"Не удалось прочитать статью ({type(exc).__name__}).") from exc
    if not article.get("title") or article.get("material_read") is not True or not str(article.get("content") or "").strip():
        raise IntakeError("На странице не удалось прочитать полный текст статьи.")

    article_url = article.get("url") or url
    article_host = urllib.parse.urlsplit(article_url).hostname or host
    publisher = str(article.get("publisher_name") or article_host)[:160]
    db = connect(config["newsroom"]["database"])
    try:
        source_url = article_url
        source = db.execute("SELECT * FROM sources WHERE url=?", (source_url,)).fetchone()
        if source is None:
            domain = _registrable_domain(article_host)
            name = f"Ссылка редакции · {domain}"[:100]
            cur = db.execute(
                "INSERT INTO sources(name,type,url,active,priority,reputation,source_role) "
                "VALUES(?,?,?,0,2,'unknown','publisher')",
                (name, "manual", source_url),
            )
            db.commit()
            source = db.execute("SELECT * FROM sources WHERE source_id=?", (cur.lastrowid,)).fetchone()
        from .workflow import enqueue
        from .topic_registry import attach_cached
        attach_cached(config)
        item_id = enqueue(db, None, {**article, 'url': article_url}, source, {'settings': config.get('ai', {})})
        return {'item_id': item_id, 'outcome': 'STORED' if item_id else 'DUPLICATE',
                'posts': [], 'published': 0, 'message': 'Материал сохранён. Редактор удалён; новая версия ещё не реализована.'}
    finally:
        db.close()
