"""One-off URL intake that feeds a read article through the normal newsroom gates."""
from __future__ import annotations

import json
import time
import urllib.parse
from datetime import datetime, timezone

from .core import NOW, _registrable_domain, canonicalize, fetch_publisher_article, process_item
from .db import connect
from .locking import acquire_cycle_lock
from .triage import MAX_AUTOMATIC_RETRIES


class IntakeError(ValueError):
    """A user-facing, safe failure while submitting a single article URL."""


class IntakeBusyError(IntakeError):
    """The scheduled collection or another intake currently owns the cycle lock."""


def submit_article_url(config: dict, url: str, *, retry_after_credits_restored: bool = False,
                       retry_after_validation_fix: bool = False) -> dict:
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
    if not article.get("title") or len(str(article.get("content") or "").strip()) < 100:
        raise IntakeError("На странице не удалось прочитать полный текст статьи.")

    article_url = article.get("url") or url
    article_host = urllib.parse.urlsplit(article_url).hostname or host
    publisher = str(article.get("publisher_name") or article_host)[:160]
    linked_document_url = article.get("primary_source_url") or ""
    if (article.get("primary_source_status") == "READ"
            and article.get("primary_source_type") == "OFFICIAL"
            and linked_document_url and linked_document_url != article_url):
        # Keep the official release page and its read attachment together so
        # page-level release-date evidence remains available beside the NPRM.
        page_text = str(article.get("content") or "")
        document_text = str(article.get("primary_source_content") or "")
        article["primary_source_document_url"] = linked_document_url
        article["primary_source_url"] = article_url
        article["primary_source_title"] = article.get("title") or article.get("primary_source_title")
        article["primary_source_content"] = (
            f"Official release page text:\n{page_text}\n\n"
            f"Linked official document ({linked_document_url}):\n{document_text}"
        )[:12000]
    primary_excerpt = str(article.get("primary_source_content") or "")
    use_primary_excerpt = (article.get("primary_source_status") == "READ"
                           and article.get("primary_source_type") == "OFFICIAL"
                           and len(str(article.get("content") or "")) < 1000
                           and len(primary_excerpt) >= 100)
    article.update({"url": article_url, "publisher_name": publisher,
                    "material_url": article.get("material_url") or article_url,
                    "material_read": article.get("material_read") is True,
                    "screening_excerpt": primary_excerpt[:12000] if use_primary_excerpt
                    else str(article.get("content") or "")[:2200],
                    "screening_primary_source": use_primary_excerpt})

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
        source_values = dict(source)
        source_values["source_role"] = "publisher"
        prior = db.execute("SELECT item_id FROM items WHERE source_id=? AND canonical_url=?",
                           (source["source_id"], canonicalize(article_url))).fetchone()
        existing_item_id = prior["item_id"] if prior else None
        if existing_item_id is not None:
            previous = db.execute("SELECT disposition,processed_at FROM items WHERE item_id=?", (existing_item_id,)).fetchone()
            disposition = previous["disposition"]
            recovered_date = bool(article.get("published_at") or article.get("updated_at"))
            if disposition == "UNDATED" and recovered_date:
                # Metadata extraction can improve without changing the article body.
                # Re-run the original item through freshness and all later gates once
                # an explicit publisher date is now available.
                pass
            elif disposition == "NOISE" and not db.execute(
                    "SELECT 1 FROM item_analysis WHERE item_id=?", (existing_item_id,)).fetchone():
                marker = f"manual_prefilter_retry:{existing_item_id}"
                if db.execute("SELECT 1 FROM app_state WHERE key=?", (marker,)).fetchone():
                    return {"item_id": existing_item_id, "outcome": disposition, "posts": [],
                            "published": 0, "publication_failed": 0, "publication_rejected": 0,
                            "message": "Эта ссылка уже прошла повторный тематический фильтр."}
                db.execute("INSERT INTO app_state(key,value) VALUES(?,?)", (marker, NOW()))
                db.commit()
            elif disposition == "REJECTED" and retry_after_validation_fix:
                retry_key = f"selection_retry:{existing_item_id}"
                retry_row = db.execute("SELECT value FROM app_state WHERE key=?", (retry_key,)).fetchone()
                analysis_row = db.execute("SELECT result_json FROM item_analysis WHERE item_id=?",
                                          (existing_item_id,)).fetchone()
                source_row = db.execute("SELECT primary_source_json FROM items WHERE item_id=?",
                                        (existing_item_id,)).fetchone()
                try:
                    retry_state = json.loads(retry_row["value"] or "{}") if retry_row else {}
                    analysis_result = json.loads(analysis_row["result_json"] or "{}") if analysis_row else {}
                    primary_source = json.loads(source_row["primary_source_json"] or "{}") if source_row else {}
                except (TypeError, ValueError, json.JSONDecodeError):
                    retry_state, analysis_result, primary_source = {}, {}, {}
                date_check = analysis_result.get("development_date_check") or {}
                fixed_result = dict(analysis_result)
                fixed_result["publication_recommendation"] = "AUTO_PUBLISH"
                from .core import _development_date_issue
                date_now_valid = bool(analysis_result.get("development_date_evidence")
                                      and date_check.get("status") == "UNVERIFIED"
                                      and date_check.get("reason") == "Цитата источника не подтверждает указанную календарную дату события."
                                      and _development_date_issue(
                                          fixed_result,
                                          {"content": primary_source.get("content", "")},
                                          int(config["newsroom"].get("freshness_window_hours", 24))) is None)
                if (int(retry_state.get("attempts", 0)) < MAX_AUTOMATIC_RETRIES
                        or retry_state.get("outcome") != "REJECTED" or not date_now_valid):
                    return {"item_id": existing_item_id, "outcome": disposition, "posts": [],
                            "published": 0, "publication_failed": 0, "publication_rejected": 1,
                            "message": "Новая проверка разрешается только если сохранённое решение проходит исправленную проверку даты."}

                history_key = f"selection_retry_history:{existing_item_id}"
                history_row = db.execute("SELECT value FROM app_state WHERE key=?", (history_key,)).fetchone()
                try:
                    history = json.loads(history_row["value"] or "[]") if history_row else []
                except (TypeError, ValueError, json.JSONDecodeError):
                    history = []
                if not isinstance(history, list):
                    history = []
                history.append({"closed_retry_state": retry_state,
                                "reopened_at": NOW(),
                                "reason": "verified_date_validation_rule_fix"})
                db.execute("INSERT INTO app_state(key,value) VALUES(?,?) "
                           "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                           (history_key, json.dumps(history, ensure_ascii=False)))
                db.execute("DELETE FROM app_state WHERE key=?", (retry_key,))
                db.execute("UPDATE items SET disposition='AI_RETRY',processed_at=NULL WHERE item_id=?",
                           (existing_item_id,))
                db.commit()
                disposition = "AI_RETRY"
            elif disposition == "REJECTED" and retry_after_credits_restored:
                retry_key = f"selection_retry:{existing_item_id}"
                retry_row = db.execute("SELECT value FROM app_state WHERE key=?", (retry_key,)).fetchone()
                try:
                    retry_state = json.loads(retry_row["value"] or "{}") if retry_row else {}
                except (TypeError, ValueError, json.JSONDecodeError):
                    retry_state = {}
                terminal_ai_error = None
                if previous["processed_at"]:
                    terminal_ai_error = db.execute(
                        "SELECT timestamp,message FROM errors WHERE message='TRIAGE:HTTP_429:credit_balance_exhausted' "
                        "AND substr(timestamp,1,19)=substr(?,1,19) ORDER BY timestamp DESC LIMIT 1",
                        (previous["processed_at"],),
                    ).fetchone()
                if (int(retry_state.get("attempts", 0)) < MAX_AUTOMATIC_RETRIES
                        or retry_state.get("outcome") != "REJECTED" or terminal_ai_error is None):
                    return {"item_id": existing_item_id, "outcome": disposition, "posts": [],
                            "published": 0, "publication_failed": 0, "publication_rejected": 1,
                            "message": "Повторное открытие допустимо только после документированного исчерпания AI-баланса и трёх неудачных попыток."}

                history_key = f"selection_retry_history:{existing_item_id}"
                history_row = db.execute("SELECT value FROM app_state WHERE key=?", (history_key,)).fetchone()
                try:
                    history = json.loads(history_row["value"] or "[]") if history_row else []
                except (TypeError, ValueError, json.JSONDecodeError):
                    history = []
                if not isinstance(history, list):
                    history = []
                history.append({"closed_retry_state": retry_state,
                                "terminal_error": dict(terminal_ai_error),
                                "reopened_at": NOW(),
                                "reason": "owner_confirmed_credits_restored"})
                db.execute("INSERT INTO app_state(key,value) VALUES(?,?) "
                           "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                           (history_key, json.dumps(history, ensure_ascii=False)))
                db.execute("DELETE FROM app_state WHERE key=?", (retry_key,))
                db.execute("UPDATE items SET disposition='AI_RETRY',processed_at=NULL WHERE item_id=?",
                           (existing_item_id,))
                db.commit()
                disposition = "AI_RETRY"
            elif disposition in {"AI_RETRY", "PRIMARY_RETRY", "WAITING_CONFIRMATION"}:
                state_row = db.execute("SELECT value FROM app_state WHERE key=?",
                                       (f"selection_retry:{existing_item_id}",)).fetchone()
                try:
                    retry_state = json.loads(state_row["value"] or "{}") if state_row else {}
                    attempts = int(retry_state.get("attempts", 0))
                    next_at = datetime.fromisoformat(str(retry_state.get("next_at") or "1970-01-01T00:00:00+00:00").replace("Z", "+00:00"))
                    if next_at.tzinfo is None:
                        next_at = next_at.replace(tzinfo=timezone.utc)
                except (TypeError, ValueError, json.JSONDecodeError):
                    attempts, next_at = 3, datetime.max.replace(tzinfo=timezone.utc)
                if attempts >= MAX_AUTOMATIC_RETRIES:
                    return {"item_id": existing_item_id, "outcome": "REJECTED", "posts": [],
                            "published": 0, "publication_failed": 0, "publication_rejected": 1,
                            "message": "Для этой версии исчерпаны автоматические повторы."}
                if next_at > datetime.now(timezone.utc):
                    raise IntakeBusyError("Для этой ссылки уже назначен повторный разбор. Подождите следующего цикла.")
            else:
                return {"item_id": existing_item_id, "outcome": disposition, "posts": [],
                        "published": 0, "publication_failed": 0, "publication_rejected": 0,
                        "message": "Эта ссылка уже обработана; второй пост по тому же материалу не создаётся."}

        ai_settings = dict(config.get("ai", {}))
        ai_settings.update({"_analysis_budget": 1, "_triage_budget": 1,
                            "_recovery_search_budget": 0,
                            "_retry_cycle_delay_seconds": max(30, int(config["newsroom"].get("poll_interval_seconds", 180)))})
        started = time.perf_counter()
        outcome = process_item(
            db, source_values, article,
            float(config["newsroom"].get("similarity_threshold", 0.35)),
            int(config["newsroom"].get("max_post_length", 3500)),
            int(config["newsroom"].get("freshness_window_hours", 24)),
            initial_backfill_minutes=None,
            relevance_terms=config["newsroom"].get("relevance_terms", []),
            ai_settings=ai_settings,
            existing_item_id=existing_item_id,
        )
        item = db.execute("SELECT item_id,story_id FROM items WHERE source_id=? AND canonical_url=?",
                          (source["source_id"], canonicalize(article_url))).fetchone()
        if item is None:
            return {"outcome": outcome, "message": "Материал обработан, запись в очереди не создана."}

        post_ids = [row[0] for row in db.execute(
            "SELECT post_id FROM posts WHERE origin_item_id=? AND status='PENDING' ORDER BY post_id",
            (item["item_id"],),
        ).fetchall()]
        published = failed = rejected = 0
        if post_ids:
            from .cli import auto_publish_since
            published, failed, rejected = auto_publish_since(
                config["newsroom"]["database"], config, post_ids=post_ids)
        posts = [dict(row) for row in db.execute(
            "SELECT post_id,status,external_id,auto_last_error FROM posts WHERE origin_item_id=? ORDER BY post_id",
            (item["item_id"],),
        ).fetchall()]
        outcome_message = {
            "AI_RETRY": "Материал принят; автоматическая проверка будет повторена в следующем цикле.",
            "PRIMARY_RETRY": "Материал принят; система повторит чтение первоисточника.",
            "WAITING_CONFIRMATION": "Материал принят; система назначила повторную проверку.",
            "REJECTED": "После ограниченных автоматических попыток материал закрыт без публикации.",
            "NOISE": "Материал исключён тематическим фильтром.",
            "STALE": "Материал старше допустимого окна свежести.",
            "UNDATED": "Для статьи не удалось установить дату публикации.",
            "DUPLICATE": "Новое существенное сведение не найдено.",
        }.get(outcome)
        db.commit()
        return {"item_id": item["item_id"], "outcome": outcome, "posts": posts,
                "published": published, "publication_failed": failed,
                "publication_rejected": rejected,
                "message": outcome_message,
                "elapsed_seconds": round(time.perf_counter() - started, 2)}
    except IntakeError:
        db.rollback()
        raise
    except Exception as exc:
        db.rollback()
        raise IntakeError(f"Материал не удалось обработать ({type(exc).__name__}).") from exc
    finally:
        db.close()
