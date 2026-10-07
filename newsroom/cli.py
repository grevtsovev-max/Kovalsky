from __future__ import annotations

import argparse
import getpass
import hashlib
from collections import Counter
import html
import json
import os
import re
import subprocess
import sys
import threading
import time
try:
    import tomllib
except ModuleNotFoundError:  # Python 3.9 and 3.10
    try:
        import tomli as tomllib
    except ModuleNotFoundError as exc:
        raise SystemExit("Для Python 3.9/3.10 установите TOML-парсер: python3 -m pip install tomli") from exc
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from .agent_control import AgentDisabled, enabled as agent_enabled, require_enabled, set_enabled
from .quality import editorial_issues, digest_issues, attributed_report_supported
from .ai import FILTER_VERSION
from .core import NOW, _log_timing, _registrable_domain, is_non_news_telegram_format, is_relevant, run_cycle, terms
from .core import NOW, _log_timing, _registrable_domain, configure_runtime_log, is_non_news_telegram_format, is_relevant, run_cycle, terms
from .db import connect
from .delivery import (DeliveryRejected, DeliveryUncertain, TelegramReceipt,
                       deliver, confirm, reconcile_posts, channel, replace_unsent_digest_batch)


def load_config(path: str) -> dict:
    with open(path, "rb") as f:
        config = tomllib.load(f)
    config.setdefault("newsroom", {}).setdefault("auto_publish", True)
    from .runtime import attach
    attach(config)
    return config


def telegram_token(config: dict) -> str:
    settings = config.get("telegram", {})
    token = os.getenv(settings.get("bot_token_env", "TELEGRAM_BOT_TOKEN"))
    if not token:
        service = settings.get("keychain_service")
        security = "/usr/bin/security"
        if service and os.path.exists(security):
            try:
                result = subprocess.run(
                    [security, "find-generic-password", "-a", getpass.getuser(), "-s", service, "-w"],
                    capture_output=True, text=True, timeout=5, check=False,
                )
                if result.returncode == 0 and result.stdout.strip():
                    token = result.stdout.strip()
            except (OSError, subprocess.TimeoutExpired):
                pass
    if not token:
        raise DeliveryRejected("Telegram credentials are missing.")
    return token


def telegram_api(config: dict, method: str, payload: dict, timeout: int = 20) -> dict:
    runtime = config.get('ai', {}).get('_runtime')
    if runtime:
        with runtime.measure('telegram_delivery', 'editor'):
            return _telegram_api(config, method, payload, timeout)
    return _telegram_api(config, method, payload, timeout)


def _telegram_api(config: dict, method: str, payload: dict, timeout: int = 20) -> dict:
    require_enabled(config)
    token = telegram_token(config)
    url = f"https://api.telegram.org/bot{token}/{method}"
    req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"),
                                 headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            result = json.loads(response.read())
    except urllib.error.HTTPError as exc:
        try:
            rejection = json.loads(exc.read())
        except Exception:
            rejection = {}
        if isinstance(rejection, dict) and rejection.get("ok") is False and 400 <= exc.code < 500 and exc.code != 408:
            raise DeliveryRejected(f"Telegram API rejected request ({exc.code})") from None
        raise DeliveryUncertain(f"Telegram API HTTP {exc.code}") from None
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise DeliveryUncertain(f"Telegram API network error ({type(exc).__name__})") from None
    if not isinstance(result, dict) or result.get("ok") not in (True, False):
        raise DeliveryUncertain("Telegram API malformed response")
    if result.get("ok") is False:
        code = result.get("error_code")
        if isinstance(code, int) and 400 <= code < 500 and code != 408:
            raise DeliveryRejected(f"Telegram API rejected request ({code})")
        raise DeliveryUncertain("Telegram API uncertain rejection")
    return result.get("result")


def telegram_format_text(text: str) -> str:
    lines = text.splitlines()
    formatted_lines = []
    link_pattern = re.compile(r"\[([^\]]+)\]\((https?://[^\s)]+)\)")
    def format_line(line: str) -> str:
        result = []
        position = 0
        for match in link_pattern.finditer(line):
            result.append(html.escape(line[position:match.start()]))
            result.append(f'<a href="{html.escape(match.group(2), quote=True)}">{html.escape(match.group(1))}</a>')
            position = match.end()
        result.append(html.escape(line[position:]))
        return "".join(result)

    def is_list_item(line: str) -> bool:
        return line.lstrip().startswith(("➠ ", "• ", "▪ ", "- "))

    def is_section_heading(line: str) -> bool:
        stripped = line.strip()
        if stripped.startswith("Источник:"):
            return False
        return (len(stripped) > 4 and stripped.startswith("**") and stripped.endswith("**")) or stripped.endswith(":")

    def format_regular_line(index: int, line: str) -> str:
        if index == 0:
            return f"<b>{format_line(line)}</b>"
        if len(line) > 4 and line.startswith("**") and line.endswith("**"):
            return f"<b>{format_line(line[2:-2])}</b>"
        return format_line(line)

    index = 0
    while index < len(lines):
        line = lines[index]
        if index > 0 and is_section_heading(line):
            first_item = index + 1
            while first_item < len(lines) and not lines[first_item].strip():
                first_item += 1
            if first_item < len(lines) and is_list_item(lines[first_item]):
                end = first_item
                while end < len(lines):
                    if is_list_item(lines[end]):
                        end += 1
                    elif (not lines[end].strip() and end + 1 < len(lines)
                          and is_list_item(lines[end + 1])):
                        end += 1
                    else:
                        break
                formatted_lines.append(format_regular_line(index, line))
                formatted_lines.append("")
                block = "\n".join(format_line(part) for part in lines[first_item:end])
                formatted_lines.append(f"<blockquote expandable>{block}</blockquote>")
                index = end
                continue
        formatted_lines.append(format_regular_line(index, line))
        index += 1
    return "\n".join(formatted_lines)


def telegram_send(config: dict, text: str) -> str:
    settings = config.get("telegram", {})
    chat_id = os.getenv(settings.get("chat_id_env", "TELEGRAM_CHAT_ID")) or settings.get("chat_id")
    if not chat_id:
        raise RuntimeError("Telegram destination is missing.")
    result = telegram_api(config, "sendMessage", {
        "chat_id": chat_id, "text": telegram_format_text(text), "parse_mode": "HTML",
        "disable_web_page_preview": True,
    })
    return TelegramReceipt(result)


def _link_digest_action(headline: str, url: str) -> str:
    """Link only a short action verb in a digest headline."""
    from .digest_language import digest_action_span
    span = digest_action_span(headline)
    if span:
        start, end = span
        return headline[:start] + f"[{headline[start:end]}]({url})" + headline[end:]
    raise ValueError(f"Для заголовка дайджеста не найден глагол события: {headline}")


def publish_digest(db_path: str, config: dict, kind: str = "daily", *, rebuild_unsent=False) -> tuple[bool, int]:
    runtime = config.get('ai', {}).get('_runtime')
    if runtime:
        with runtime.measure('digest', 'editor', {'category': 'digest'}):
            return _publish_digest_measured(db_path, config, kind, rebuild_unsent=rebuild_unsent)
    return _publish_digest_measured(db_path, config, kind, rebuild_unsent=rebuild_unsent)


def _publish_digest_measured(db_path: str, config: dict, kind: str = "daily", *, rebuild_unsent=False) -> tuple[bool, int]:
    """Publish the scheduled daily or Saturday weekly digest of published channel posts."""
    db = connect(db_path)
    try:
        return _publish_digest(db, config, kind, rebuild_unsent=rebuild_unsent)
    finally:
        db.close()


def _publish_digest(db, config: dict, kind: str, *, rebuild_unsent=False, _visibility_attempt=0) -> tuple[bool, int]:
    if not agent_enabled(config) or config["newsroom"].get("auto_publish", True) is not True:
        return False, 0
    from .channel_presence import inspect_channel_posts, inspect_digest_messages, ChannelPresenceUnavailable
    settings = config["newsroom"]
    now = datetime.now(timezone.utc)
    weekly = kind == "weekly"
    if kind not in {"daily", "weekly"}:
        raise ValueError("kind must be daily or weekly")
    local_zone = ZoneInfo("Europe/Moscow")
    hour, minute = (19, 0) if weekly else (20, 5)
    local_now = now.astimezone(local_zone)
    today_due = local_now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    prefix = "weekly_digest" if weekly else "digest"
    last_date_key = f"{prefix}_last_local_date"
    last_sent_key = f"{prefix}_last_sent_at"
    next_key = f"{prefix}_next_at"
    if weekly:
        days_to_saturday = (5 - local_now.weekday()) % 7
        next_local = today_due + timedelta(days=days_to_saturday)
        if next_local <= local_now:
            next_local += timedelta(days=7)
        due_today = local_now.weekday() == 5 and local_now >= today_due
    else:
        next_local = today_due if local_now < today_due else today_due + timedelta(days=1)
        due_today = local_now >= today_due
    next_at = next_local.astimezone(timezone.utc).isoformat(timespec="seconds")
    last_date_row = db.execute("SELECT value FROM app_state WHERE key=?", (last_date_key,)).fetchone()
    last_date = last_date_row["value"] if last_date_row else ""
    if not due_today or last_date == local_now.date().isoformat():
        db.execute("INSERT OR REPLACE INTO app_state(key,value) VALUES(?,?)", (next_key, next_at))
        db.commit()
        return False, 0

    batch_key = channel(config) + ':' + prefix + ':' + local_now.date().isoformat()
    batch = db.execute('SELECT * FROM digest_batches WHERE batch_key=?', (batch_key,)).fetchone()
    if batch and not rebuild_unsent:
        messages = json.loads(batch['messages_json'])
        news_count = batch['news_count']
        period_end = batch['period_end']
    else:
        lookback = 168 if weekly else 24
        cutoff = (now - timedelta(hours=lookback)).isoformat(timespec="seconds")
        rows = db.execute(
            "SELECT p.post_id,p.text,p.external_id,p.published_at, "
            "COALESCE(json_extract(p.fact_check_result,'$.importance'), "
            "(SELECT json_extract(a.result_json,'$.importance') FROM items i "
            "JOIN item_analysis a USING(item_id) WHERE i.story_id=p.story_id "
            "ORDER BY a.created_at DESC LIMIT 1), s.importance, 'MEDIUM') AS importance, "
            "COALESCE(json_extract(p.fact_check_result,'$.topic_category'), "
            "(SELECT json_extract(a.result_json,'$.topic_category') FROM items i "
            "JOIN item_analysis a USING(item_id) WHERE i.story_id=p.story_id "
            "ORDER BY a.created_at DESC LIMIT 1), '') AS topic_category, "
            "COALESCE(json_extract(p.fact_check_result,'$.geographic_scope'), "
            "(SELECT json_extract(a.result_json,'$.geographic_scope') FROM items i "
            "JOIN item_analysis a USING(item_id) WHERE i.story_id=p.story_id "
            "ORDER BY a.created_at DESC LIMIT 1), '') AS geographic_scope, "
            "COALESCE(json_extract(p.fact_check_result,'$.test_publication'),0) AS test_publication "
            "FROM posts p LEFT JOIN stories s USING(story_id) "
            "WHERE p.status='PUBLISHED' AND p.published_at>? AND p.published_at<=?",
            (cutoff, now.isoformat(timespec="seconds")),
        ).fetchall()
        rank = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3}
        rows = sorted(rows, key=lambda row: (rank.get(row["importance"], 2), row["published_at"] or ""))

        if weekly:
            week_start = (local_now.date() - timedelta(days=6)).strftime("%d.%m")
            day_label = local_now.strftime("%d.%m.%Y")
            title = f"📣 {settings.get('weekly_digest_title', 'Крипторынок: главное за неделю')} · {week_start}–{day_label}"
        else:
            day_label = local_now.strftime("%d.%m.%Y")
            title = f"📣 {settings.get('digest_title', 'Крипторынок: главное за день')} · {day_label}"
        selected = []
        for row in rows:
            post_text = row["text"] or ""
            headline = post_text.splitlines()[0].strip() if post_text.splitlines() else "Новость"
            if row["test_publication"] or ("_topic_registry" not in config.get("ai",{}) and row["geographic_scope"] in {"OTHER", "GLOBAL"}):
                continue
            if "_topic_registry" not in config.get("ai",{}) and re.search(r"(?i)\b(США|американ\w*|ФРС|SEC|Евросоюз|ЕС|Великобритани\w*|британск\w*)\b", headline):
                continue
            headline = re.sub(r"\*\*(.*?)\*\*", r"\1", headline)
            from .core import _limit_headline
            headline = _limit_headline(headline)
            post_url = _telegram_message_url(config, row["external_id"]) if row["external_id"] else None
            category = (row["topic_category"] or "").upper()
            if any(word in category for word in ("REGULATION", "POLICY", "LEGAL", "LAW")):
                emoji = "🏛"
            elif any(word in category for word in ("SECURITY", "HACK", "EXPLOIT")):
                emoji = "🔐"
            elif any(word in category for word in ("TAX", "TAXATION")):
                emoji = "🧾"
            elif any(word in category for word in ("PRODUCT", "TECHNICAL", "INFRASTRUCTURE")):
                emoji = "⚙️"
            elif any(word in category for word in ("MARKET", "FINANCE", "INVESTMENT")):
                emoji = "📈"
            else:
                emoji = "📌"
            selected.append((emoji, headline, row["importance"] or "MEDIUM", post_url))

        if any(not entry[3] for entry in selected):
            raise ChannelPresenceUnavailable('DIGEST_POST_LINK_MISSING')
        observed = inspect_channel_posts(config, [entry[3].rsplit('/', 1)[1] for entry in selected], db=db)
        visible = []
        for emoji, headline, importance, post_url in selected:
            observation = observed[post_url.rsplit('/', 1)[1]]
            if observation['status'] == 'DELETED':
                continue
            headline = re.sub(r"\*\*(.*?)\*\*", r"\1", observation['text'].splitlines()[0].strip())
            visible.append((emoji, _limit_headline(headline), importance, observation['url']))
        selected = visible

        messages = []
        current = title
        if not selected:
            if rows:
                current += "\n\nЗа период дайджеста нет публикаций, подходящих для включения в подборку."
            else:
                current += "\n\nЗа период дайджеста в канале новых публикаций не было."
        else:
            # Keep the approved 25 September digest format: linked headlines only, no body summaries.
            for emoji, headline, importance, post_url in selected:
                linked_headline = _link_digest_action(headline, post_url) if post_url else headline
                item = f"{emoji} {linked_headline}"
                candidate = current + "\n\n" + item
                if len(candidate) > 3900 and current != title:
                    messages.append(current)
                    current = title + " (продолжение)\n\n" + item
                else:
                    current = candidate
        if current:
            messages.append(current)
        # Validate the entire batch before persisting or sending its first part.
        for message in messages:
            issues = digest_issues(message)
            if issues:
                raise RuntimeError("Дайджест не прошёл редакционную проверку: " + ", ".join(issues))
        news_count = len(selected)
        period_end = now.isoformat(timespec="seconds")
        if batch:
            replace_unsent_digest_batch(db, batch_key, messages, news_count, period_end)
        else:
            db.execute('INSERT OR IGNORE INTO digest_batches(batch_key,messages_json,news_count,period_end,created_at) VALUES(?,?,?,?,?)',
                       (batch_key, json.dumps(messages, ensure_ascii=False), news_count, period_end, period_end))
            db.commit()
        batch = db.execute('SELECT * FROM digest_batches WHERE batch_key=?', (batch_key,)).fetchone()
        messages, news_count, period_end = json.loads(batch['messages_json']), batch['news_count'], batch['period_end']
    if not inspect_digest_messages(config, messages, db=db):
        if _visibility_attempt >= 2:
            raise ChannelPresenceUnavailable('DIGEST_CHANNEL_CHANGED_DURING_PREPARATION')
        # Only a known unsent batch can be amended. A successful/unknown part
        # causes replace_unsent_digest_batch to block this rebuild.
        return _publish_digest(db, config, kind, rebuild_unsent=True,
                               _visibility_attempt=_visibility_attempt + 1)
    try:
        for part, message in enumerate(messages):
            issues = digest_issues(message)
            if issues:
                raise RuntimeError("Дайджест не прошёл редакционную проверку: " + ", ".join(issues))
            if part and not inspect_digest_messages(config, [message], db=db):
                raise ChannelPresenceUnavailable('DIGEST_CHANNEL_CHANGED_BETWEEN_PARTS')
            operation = f"{prefix}:{local_now.date().isoformat()}:{part}"
            deliver(db, config, operation, message, telegram_send)
            confirm(db, config, operation)
            db.commit()
    except Exception:
        raise
    next_date = local_now.date() + timedelta(days=7 if weekly else 1)
    next_due = datetime.combine(next_date, today_due.timetz().replace(tzinfo=None), tzinfo=local_zone)
    db.execute("INSERT OR REPLACE INTO app_state(key,value) VALUES(?,?)", (last_date_key, local_now.date().isoformat()))
    db.execute("INSERT OR REPLACE INTO app_state(key,value) VALUES(?,?)", (last_sent_key, period_end))
    db.execute("INSERT OR REPLACE INTO app_state(key,value) VALUES(?,?)", (next_key, next_due.astimezone(timezone.utc).isoformat(timespec="seconds")))
    db.commit()
    print("Опубликован " + ("еженедельный" if weekly else "ежедневный") + f" дайджест опубликованных новостей: {news_count}")
    return True, news_count


def is_eligible_for_auto_publish(post, cutoff: str, thematic=None) -> bool:
    """Recheck publication eligibility at the send boundary, not only in SQL."""
    if not cutoff:
        return False
    try:
        facts = json.loads(post["fact_check_result"] or "{}")
        created_at = datetime.fromisoformat(post["created_at"].replace("Z", "+00:00"))
        cutoff_at = datetime.fromisoformat(cutoff.replace("Z", "+00:00"))
        if created_at.tzinfo is None or cutoff_at.tzinfo is None:
            return False
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return False
    if thematic is not None:
        proof = facts.get('topic_registry') or {}
        match = proof.get('match') or {}
        topical = (proof.get('checked') is True and proof.get('version') == thematic.get('version')
                   and match.get('name') in {t['name'] for t in thematic.get('topics',[])}
                   and len(str(match.get('evidence') or '').strip()) >= 24)
    else:
        topical = (facts.get('geographic_scope') in {'RUSSIA','CIS','RUSSIA_CIS'}
                   and facts.get('russia_cis_impact') == 'DIRECT'
                   and bool(str(facts.get('impact_evidence') or '').strip()))
    return (facts.get("mode") == "AI" and facts.get("_filter_version") == FILTER_VERSION
            and topical
            and (facts.get("publisher_report_exception") is True or facts.get("primary_source_status") not in {"UNREADABLE", "ARTICLE_UNREADABLE", "OCR_REVIEW"})
            and facts.get("source_review_required") is not True
            and facts.get("independent_check") != "CONFLICT"
            and created_at >= cutoff_at)


def auto_publish_since(db_path: str, config: dict, *, post_ids=None,
                      exclude_post_ids=None) -> tuple[int, int, int]:
    """Deliver checked posts; resource waits and source updates are separate states."""
    from .runtime import BudgetDeferred
    from .source_recheck import SourceUpdateRequired
    if not agent_enabled(config) or config["newsroom"].get("auto_publish", True) is not True:
        return 0, 0, 0
    cutoff = config["newsroom"].get("auto_publish_since")
    if not cutoff:
        print("Автопубликация остановлена: не задана дата начала автоматической отправки.", file=sys.stderr)
        return 0, 1, 0
    db = connect(db_path)
    reconcile_posts(db, config)
    rows = db.execute(
        "SELECT * FROM posts WHERE status='PENDING' AND created_at >= ? ORDER BY created_at,post_id",
        (cutoff,),
    ).fetchall()
    published = failed = rejected = 0
    selected_ids = None if post_ids is None else set(post_ids)
    excluded_ids = set(exclude_post_ids or [])
    for row in rows:
        if not agent_enabled(config):
            break
        post_id = row["post_id"]
        if row['auto_last_error'] == 'DELIVERY_RETRY_EXHAUSTED':
            failed += 1
            continue
        if post_id in excluded_ids:
            continue
        if selected_ids is not None and post_id not in selected_ids:
            continue
        delivery = db.execute("SELECT status FROM publication_attempts WHERE post_id=? AND channel_id=?", (post_id, channel(config))).fetchone()
        if delivery and delivery['status'] in {'SENDING', 'UNKNOWN'}:
            failed += 1
            continue
        if not is_eligible_for_auto_publish(row, cutoff, config.get("ai", {}).get("_topic_registry")):
            if row['origin_item_id']:
                from .material_flow import mark
                mark(db, row['origin_item_id'], 'gate', 'CLOSED', 'Условия автоматического допуска не выполнены.')
            db.execute("UPDATE posts SET status='REJECTED',editor_decision='AUTO_REJECTED',auto_last_error='AUTO_PUBLISH_GATE_FAILED' WHERE post_id=?",
                       (post_id,))
            db.commit()
            rejected += 1
            continue
        attempt = int(row["auto_attempts"] or 0) + 1
        db.execute("UPDATE posts SET auto_attempts=? WHERE post_id=?", (attempt, post_id))
        db.commit()
        try:
            publish(db, config, post_id, automatic=True)
            published += 1
        except AgentDisabled:
            db.execute("UPDATE posts SET auto_attempts=? WHERE post_id=?", (attempt - 1, post_id))
            db.commit()
            break
        except SourceUpdateRequired:
            _log_timing('source_update_reprocessing', post_id=post_id)
        except BudgetDeferred:
            db.execute('UPDATE posts SET auto_attempts=?,auto_last_error=? WHERE post_id=?',
                       (attempt - 1, 'RESOURCE_WAIT', post_id))
            db.commit()
        except DeliveryUncertain:
            failed += 1
            db.execute("UPDATE posts SET auto_last_error='DELIVERY_UNKNOWN' WHERE post_id=?", (post_id,))
            db.commit()
        except Exception as exc:
            failed += 1
            error_code = type(exc).__name__
            exhausted = db.execute("SELECT 1 FROM publication_attempts WHERE post_id=? AND status='FAILED' AND attempt_count>=4 LIMIT 1", (post_id,)).fetchone()
            if exhausted:
                db.execute("UPDATE posts SET auto_last_error='DELIVERY_RETRY_EXHAUSTED' WHERE post_id=?", (post_id,))
                if row['origin_item_id']:
                    from .material_flow import mark
                    mark(db, row['origin_item_id'], 'delivery', 'ERROR',
                         'Доставка технически заблокирована: повторы исчерпаны; материал и ответы сохранены.',
                         block_kind='technical')
                print(f'Доставка поста #{post_id} технически заблокирована после исчерпания повторов.', file=sys.stderr)
            else:
                db.execute("UPDATE posts SET auto_last_error=? WHERE post_id=?", (error_code, post_id))
                print(f"Доставка поста #{post_id} не завершена ({error_code}); история попыток сохранена.", file=sys.stderr)
            db.commit()
    db.close()
    return published, failed, rejected


def _percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int((len(ordered) - 1) * fraction))]


def _performance_summary(config: dict, now: datetime) -> list[str]:
    database = Path(config["newsroom"]["database"]).expanduser()
    if not database.is_absolute():
        database = Path.cwd() / database
    log_path = database.parent / "newsroom-runtime.log"
    if not log_path.is_file():
        return ["Время этапов: журнал замеров пока недоступен."]
    cutoff = now - timedelta(hours=24)
    events: dict[str, list[dict]] = {
        "source_fetch_timing": [], "primary_source_read_timing": [],
        "news_processing_timing": [], "telegram_publish_timing": [], "cycle_timing": [],
        "collection_stage_timing": [], "item_processing_timing": [],
        "research_agent_action_timing": [],
        "post_ready": [], "post_publish_confirmed": [],
        "web_search_scheduled": [], "web_search_deferred": [], "web_search_slot_reserved": [],
    }
    try:
        with log_path.open("r", encoding="utf-8", errors="replace") as stream:
            # The runtime log is append-only; a bounded tail is enough for recent metrics.
            stream.seek(0, 2)
            size = stream.tell()
            stream.seek(max(0, size - 8_000_000))
            if size > 8_000_000:
                stream.readline()
            for line in stream:
                try:
                    event = json.loads(line)
                    if event.get("event") not in events or not event.get("timestamp"):
                        continue
                    stamp = datetime.fromisoformat(event["timestamp"].replace("Z", "+00:00"))
                    if stamp.tzinfo is None:
                        stamp = stamp.replace(tzinfo=timezone.utc)
                    if stamp >= cutoff:
                        events[event["event"]].append(event)
                except (ValueError, TypeError, json.JSONDecodeError):
                    continue
    except OSError:
        return ["Время этапов: не удалось прочитать журнал замеров."]

    def range_text(values: list[float]) -> str:
        p95 = _percentile(values, 0.95)
        maximum = max(values) if values else 0
        return f"p95 {p95:.1f} с · максимум {maximum:.1f} с" if p95 is not None else "нет замеров"

    lines = ["Время этапов за 24 ч:"]
    slow_fetches = events["source_fetch_timing"]
    if slow_fetches:
        slowest = max(slow_fetches, key=lambda event: event.get("seconds", 0))
        lines.append(f"Долгие/ошибочные ленты: {len(slow_fetches)} событий · максимум {slowest.get('seconds', 0):.1f} с ({slowest.get('source', 'источник')}).")
    else:
        lines.append("Долгих или ошибочных запросов лент не зафиксировано.")
    reads = [event["seconds"] for event in events["primary_source_read_timing"] if isinstance(event.get("seconds"), (int, float))]
    lines.append(f"Чтение первоисточников: {range_text(reads)} · замеров {len(reads)}.")
    processing = events["news_processing_timing"]
    ai = [event["ai_seconds"] for event in processing if isinstance(event.get("ai_seconds"), (int, float)) and event["ai_seconds"] > 0]
    research = [event["research_agent_seconds"] for event in processing
                if isinstance(event.get("research_agent_seconds"), (int, float))
                and event["research_agent_seconds"] > 0]
    totals = [event["total_seconds"] for event in processing if isinstance(event.get("total_seconds"), (int, float))]
    lines.append(f"AI: {range_text(ai)} · разборов {len(ai)}; обработка материалов с AI/чтением/новым событием: {range_text(totals)} · замеров {len(totals)}.")
    if research:
        lines.append(f"Исследовательский агент: {range_text(research)} · запусков {len(research)}.")
    agent_actions = events["research_agent_action_timing"]
    if agent_actions:
        model_times = [event["model_seconds"] for event in agent_actions
                       if isinstance(event.get("model_seconds"), (int, float))]
        tool_times = [event["tool_seconds"] for event in agent_actions
                      if isinstance(event.get("tool_seconds"), (int, float))]
        lines.append(f"Шаги исследователя: модель {range_text(model_times)} · действий {len(model_times)}; инструменты {range_text(tool_times)} · действий {len(tool_times)}.")
        if model_times and _percentile(model_times, 0.95) >= 15:
            lines.append("Рекомендация по исследованию: p95 ответа модели выше 15 с; проверьте задержки API и размер передаваемого контекста.")
        if tool_times and _percentile(tool_times, 0.95) >= 15:
            slowest_tool = max(agent_actions, key=lambda event: event.get("tool_seconds", 0))
            lines.append(f"Рекомендация по исследованию: p95 инструмента выше 15 с; самый долгий шаг — {slowest_tool.get('tool', 'неизвестно')} ({slowest_tool.get('tool_seconds', 0):.1f} с). Проверьте сетевой ответ и тайм-аут этого действия.")
    all_items = [event["total_seconds"] for event in events["item_processing_timing"]
                 if isinstance(event.get("total_seconds"), (int, float))]
    lines.append(f"Вся обработка материалов: {range_text(all_items)} · замеров {len(all_items)}.")
    publishes = [event["seconds"] for event in events["telegram_publish_timing"] if isinstance(event.get("seconds"), (int, float))]
    lines.append(f"Отправка в Telegram: {range_text(publishes)} · попыток {len(publishes)}.")
    cycles = [event["total_seconds"] for event in events["cycle_timing"] if isinstance(event.get("total_seconds"), (int, float))]
    lines.append(f"Полный цикл сбора и публикации: {range_text(cycles)} · циклов {len(cycles)}.")
    stages = events["collection_stage_timing"]
    if stages:
        lines.append(f"Разбивка сбора: {len(stages)} циклов с полным учётом стадий.")
        for key, label in (("retry_seconds", "Повторная обработка"),
                           ("fetch_wait_seconds", "Ожидание лент"),
                           ("matching_seconds", "Подбор независимых источников"),
                           ("processing_seconds", "Обработка материалов и быстрая отправка новых постов"),
                           ("other_seconds", "Прочие стадии сбора")):
            values = [event[key] for event in stages if isinstance(event.get(key), (int, float))]
            lines.append(f"{label}: {range_text(values)}.")
    def seconds_between(start, end):
        try:
            first = datetime.fromisoformat(str(start).replace("Z", "+00:00"))
            last = datetime.fromisoformat(str(end).replace("Z", "+00:00"))
            if first.tzinfo is None or last.tzinfo is None:
                return None
            seconds = (last - first).total_seconds()
            return seconds if seconds >= 0 else None
        except (TypeError, ValueError):
            return None

    confirmed_by_post = {}
    for event in events["post_publish_confirmed"]:
        post_id = event.get("post_id")
        if post_id and (post_id not in confirmed_by_post
                        or str(event.get("timestamp") or "") < str(confirmed_by_post[post_id].get("timestamp") or "")):
            confirmed_by_post[post_id] = event
    ready_by_post = {}
    for event in events["post_ready"]:
        post_id = event.get("post_id")
        if post_id and (post_id not in ready_by_post
                        or str(event.get("timestamp") or "") < str(ready_by_post[post_id].get("timestamp") or "")):
            ready_by_post[post_id] = event
    source_to_detect, detect_to_ready, ready_to_telegram, source_to_telegram = [], [], [], []
    for event in ready_by_post.values():
        detected = event.get("item_discovered_at")
        source_time = event.get("source_published_at")
        source_gap = seconds_between(source_time, detected)
        detect_gap = seconds_between(detected, event.get("timestamp"))
        if source_gap is not None:
            source_to_detect.append(source_gap)
        if detect_gap is not None:
            detect_to_ready.append(detect_gap)
        confirmation = confirmed_by_post.get(event.get("post_id"))
        if confirmation:
            delivery_gap = seconds_between(event.get("timestamp"), confirmation.get("telegram_confirmed_at"))
            full_gap = seconds_between(source_time, confirmation.get("telegram_confirmed_at"))
            if delivery_gap is not None:
                ready_to_telegram.append(delivery_gap)
            if full_gap is not None:
                source_to_telegram.append(full_gap)
    if events["post_ready"] or events["post_publish_confirmed"]:
        lines.append("Сквозная задержка (за 24 ч, подтверждённые события):")
        lines.append(f"Дата публикации материала → обнаружение: {range_text(source_to_detect)} · пар {len(source_to_detect)}.")
        lines.append(f"Обнаружение → пост готов: {range_text(detect_to_ready)} · пар {len(detect_to_ready)}.")
        lines.append(f"Пост готов → подтверждение Telegram: {range_text(ready_to_telegram)} · пар {len(ready_to_telegram)}.")
        lines.append(f"Источник → Telegram: {range_text(source_to_telegram)} · пар {len(source_to_telegram)}.")
        full_p95 = _percentile(source_to_telegram, 0.95)
        stage_values = {
            "обнаружение": _percentile(source_to_detect, 0.95),
            "редакционная обработка": _percentile(detect_to_ready, 0.95),
            "доставка в Telegram": _percentile(ready_to_telegram, 0.95),
        }
        measured_stages = {name: value for name, value in stage_values.items() if value is not None}
        if not source_to_telegram:
            lines.append("Рекомендация по задержке: сквозной SLA пока не подтверждён — нет пар «готовый пост → подтверждение Telegram» с надёжной датой источника. Проверьте, что сборщик и публикация работают и runtime-журнал получает оба события.")
        elif len(source_to_telegram) < 10:
            sample_note = (f"пока только {len(source_to_telegram)} подтверждённых пар; для устойчивой оценки p95 накопите не менее 10")
            if full_p95 is not None and full_p95 > 180 and measured_stages:
                bottleneck = max(measured_stages, key=measured_stages.get)
                suggestions = {
                    "обнаружение": "проверьте свежесть источников и ожидание слота Web Search",
                    "редакционная обработка": "проверьте очередь и задержки чтения источников и AI-разбора",
                    "доставка в Telegram": "проверьте длительность запросов Telegram и повторы доставки",
                }
                sample_note += f"; текущий p95 выше трёх минут, самый медленный участок — {bottleneck}: {suggestions[bottleneck]}"
            lines.append(f"Рекомендация по задержке: SLA пока нельзя надёжно оценить: {sample_note}.")
        elif full_p95 is not None and full_p95 > 180 and measured_stages:
            bottleneck = max(measured_stages, key=measured_stages.get)
            suggestions = {
                "обнаружение": "проверить свежесть и частоту опроса медленных источников, а также ожидание слота Web Search",
                "редакционная обработка": "сократить очередь свежих материалов и проверить задержки чтения источников и AI-разбора",
                "доставка в Telegram": "проверить длительность запросов Telegram и повторы доставки",
            }
            if full_p95 > 300:
                latency_state = "p95 превысил верхнюю границу 5 минут"
            else:
                latency_state = "p95 выше рабочей цели 3 минуты, но не превысил верхнюю границу 5 минут"
            lines.append(f"Рекомендация по задержке: {latency_state}; наибольший участок — {bottleneck}. {suggestions[bottleneck]}.")
    else:
        lines.append("Сквозная задержка: за последние 24 часа нет событий подготовки или подтверждения публикации.")
        lines.append("Рекомендация по задержке: SLA пока не проверен. Убедитесь, что сборщик и публикация работают и runtime-журнал получает события готовности поста и подтверждения Telegram.")
    slots = events["web_search_slot_reserved"]
    if slots:
        intervals = [event["interval_minutes"] for event in slots if isinstance(event.get("interval_minutes"), (int, float))]
        slot_times: dict[str, list[datetime]] = {}
        for event in slots:
            category = str(event.get("category") or "unknown")
            try:
                stamp = datetime.fromisoformat(str(event["timestamp"]).replace("Z", "+00:00"))
                slot_times.setdefault(category, []).append(stamp)
            except (KeyError, ValueError, TypeError):
                continue
        feed_times = sorted(slot_times.get("feeds", []))
        feed_gaps = [(later - earlier).total_seconds() / 60
                     for earlier, later in zip(feed_times, feed_times[1:])]
        category_counts = ", ".join(
            f"{label} {len(slot_times.get(category, []))}"
            for category, label in (("feeds", "новости"), ("primary_recovery", "первоисточник"), ("story_watch", "сюжеты"))
            if slot_times.get(category)
        )
        scheduled_counts = [event["eligible_count"] for event in events["web_search_scheduled"]
                            if isinstance(event.get("eligible_count"), (int, float))]
        query_counts = [event["query_count"] for event in events["web_search_scheduled"]
                        if isinstance(event.get("query_count"), (int, float))]
        largest_feed_gap = f"{max(feed_gaps):.1f} мин" if feed_gaps else "недостаточно замеров"
        lines.append(f"Web Search за 24 ч: {len(slots)} резервирований ({category_counts or 'категории не записаны'}); интервал слота до {max(intervals) if intervals else 'нет данных'} мин; наибольший интервал новостного поиска {largest_feed_gap}; отложенных циклов {len(events['web_search_deferred'])}.")
        if scheduled_counts:
            lines.append(f"В выбранном цикле было готово к запуску поисковых лент: максимум {max(scheduled_counts)}.")
        if query_counts:
            lines.append(f"Поисковых запросов объединено в вызов: {max(query_counts)}.")
    return lines


def _improvement_recommendations(errors: Counter, failing_sources: int, stale_sources: int) -> list[str]:
    """Turn recurring operational signals into bounded, human-reviewable actions."""
    recommendations = []
    for label, count in errors.most_common():
        if count < 3:
            continue
        if label == "AI_EDITOR_FALLBACK":
            recommendations.append(f"AI-редактор переходил в резервный режим {count} раз: проверьте доступность модели, лимиты и сеть; отслеживайте долю материалов, обработанных резервными правилами.")
        elif label == "NETWORK_TIMEOUT":
            recommendations.append(f"Зафиксировано {count} сетевых тайм-аутов: сравните медленные источники и запросы, затем настройте тайм-аут или исключите источник, который стабильно задерживает цикл.")
        elif label == "HTTP_429":
            recommendations.append(f"Получено {count} ответов HTTP 429: проверьте частоту запросов и квоты затронутых источников, затем разнесите обращения по циклам.")
        elif label == "TLS_CONNECTION_ERROR":
            recommendations.append(f"Зафиксировано {count} ошибок TLS: проверьте сертификат и поддержку HTTPS на затронутых источниках.")
        elif label.startswith("HTTP_5"):
            recommendations.append(f"Получено {count} серверных ошибок ({label}): проверьте доступность затронутых источников и оставьте повтор по текущему расписанию.")
        if len(recommendations) >= 3:
            break
    if failing_sources >= 3:
        recommendations.append(f"Сейчас {failing_sources} источников сообщают об ошибках: сгруппируйте их по коду, проверьте общую сетевую причину и пересмотрите недоступные источники.")
    if stale_sources >= 3:
        recommendations.append(f"{stale_sources} источников давно не проверялись: проверьте длительность цикла и не блокируют ли медленные источники опрос остальных.")
    return recommendations[:5]


def build_health_report(db, config: dict, now: datetime | None = None) -> str:
    now = now or datetime.now(timezone.utc)
    cutoff24 = (now - timedelta(hours=24)).isoformat(timespec="seconds")
    sources = db.execute("SELECT name,last_checked_at,last_error,last_success_at,consecutive_failures,recovery_before FROM sources WHERE active=1 ORDER BY name").fetchall()
    pending = db.execute("SELECT * FROM posts WHERE status='PENDING'").fetchall()
    published24 = db.execute("SELECT COUNT(*) FROM posts WHERE status='PUBLISHED' AND published_at>=?", (cutoff24,)).fetchone()[0]
    last_publish = db.execute("SELECT MAX(published_at) FROM posts WHERE status='PUBLISHED'").fetchone()[0]
    story_followups24 = db.execute(
        "SELECT COUNT(*) FROM posts WHERE status='PUBLISHED' AND published_at>=? AND text LIKE '%Ранее:%'",
        (cutoff24,),
    ).fetchone()[0]
    ai24 = db.execute("SELECT COUNT(*) FROM item_analysis WHERE created_at>=?", (cutoff24,)).fetchone()[0]
    independent_checks24 = Counter()
    for row in db.execute("SELECT result_json FROM item_analysis WHERE created_at>=?", (cutoff24,)):
        try:
            result = json.loads(row["result_json"] or "{}")
            if result.get("_filter_version") == FILTER_VERSION:
                if result.get("action") in {"NEW_STORY", "UPDATE"}:
                    independent_checks24[result.get("independent_check", "NOT_ASSESSED")] += 1
                else:
                    independent_checks24["NOT_APPLICABLE"] += 1
            else:
                independent_checks24["LEGACY"] += 1
        except (TypeError, json.JSONDecodeError):
            independent_checks24["LEGACY"] += 1
    last_ai_error = db.execute("SELECT MAX(timestamp) FROM errors WHERE message LIKE 'AI editor unavailable%' AND timestamp>=?", (cutoff24,)).fetchone()[0]
    raw_errors24 = db.execute("SELECT message,COUNT(*) AS n FROM errors WHERE timestamp>=? GROUP BY message", (cutoff24,)).fetchall()
    errors24 = Counter()
    for row in raw_errors24:
        errors24[_safe_error_label(row["message"])] += row["n"]
    primary_failures24 = 0
    ocr_review24 = 0
    for row in db.execute("SELECT primary_source_json FROM items WHERE discovered_at>=?", (cutoff24,)):
        try:
            source_status = json.loads(row["primary_source_json"] or "{}").get("status")
            if source_status in {"UNREADABLE", "ARTICLE_UNREADABLE"}:
                primary_failures24 += 1
            elif source_status == "OCR_REVIEW":
                ocr_review24 += 1
        except (TypeError, json.JSONDecodeError):
            continue
    stale_after = now - timedelta(minutes=10)
    healthy, failing, stale = 0, [], []
    for source in sources:
        if source["last_error"]:
            failing.append((source["name"], source["last_error"]))
        elif not source["last_success_at"]:
            stale.append(source["name"])
        else:
            try:
                checked = datetime.fromisoformat(source["last_success_at"].replace("Z", "+00:00"))
                if checked.tzinfo is None or checked < stale_after:
                    stale.append(source["name"])
                else:
                    healthy += 1
            except ValueError:
                stale.append(source["name"])
    modes = {"AI": 0, "RULE_BASED": 0, "OTHER": 0}
    cutoff = config.get("newsroom", {}).get("auto_publish_since")
    eligible = 0
    for row in pending:
        try:
            mode = json.loads(row["fact_check_result"] or "{}").get("mode", "OTHER")
        except (TypeError, json.JSONDecodeError):
            mode = "OTHER"
        modes[mode if mode in {"AI", "RULE_BASED"} else "OTHER"] += 1
        eligible += int(is_eligible_for_auto_publish(row, cutoff, config.get("ai", {}).get("_topic_registry")))
    held_items = Counter({row["disposition"]: row["n"] for row in db.execute(
        "SELECT disposition,COUNT(*) AS n FROM items WHERE disposition IN ('AI_RETRY','PRIMARY_RETRY','WAITING_CONFIRMATION','AGENT_CORRECTION_QUEUED') GROUP BY disposition")})
    baseline_missed_at_discovery = db.execute(
        "SELECT COUNT(*) FROM items WHERE disposition='BASELINE_SKIPPED' "
        "AND julianday(discovered_at)-julianday(published_at) BETWEEN 0 AND ?",
        (config.get("newsroom", {}).get("freshness_window_hours", 24) / 24,),
    ).fetchone()[0]
    baseline_still_fresh = db.execute(
        "SELECT COUNT(*) FROM items WHERE disposition='BASELINE_SKIPPED' "
        "AND julianday(?) - julianday(published_at) BETWEEN 0 AND ?",
        (now.astimezone(timezone.utc).isoformat(),
         config.get("newsroom", {}).get("freshness_window_hours", 24) / 24),
    ).fetchone()[0]
    fallback24 = sum(row["n"] for row in raw_errors24 if "AI editor unavailable" in row["message"])
    latest = max((row["last_checked_at"] for row in sources if row["last_checked_at"]), default="нет данных")
    memory_counts = {table: db.execute('SELECT COUNT(*) FROM '+table).fetchone()[0] for table in ('events','story_facts','publication_coverage','agent_decisions','story_monitoring_jobs')}
    uncertain_deliveries = db.execute("SELECT COUNT(*) FROM publication_attempts WHERE status IN ('SENDING','UNKNOWN')").fetchone()[0]
    lines = [
        f"Память: событий {memory_counts['events']} · утверждений {memory_counts['story_facts']} · записей о прежних публикациях {memory_counts['publication_coverage']} · решений {memory_counts['agent_decisions']} · заданий наблюдения {memory_counts['story_monitoring_jobs']}.",
        f"Доставка с неизвестным результатом: {uncertain_deliveries}; повторная отправка заблокирована до подтверждения.",
        f"Здоровье Kovalsky Newsroom · {now.astimezone(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}",
        f"Источники: {healthy} работают · {len(failing)} с ошибками · {len(stale)} давно не проверялись (порог 10 минут).",
        f"Цикл источников последний раз отмечен: {latest}.",
        f"Пропущено при подключении в окне свежести: {baseline_missed_at_discovery}; сейчас в этом окне остаются {baseline_still_fresh} записей.",
        f"Очередь: {sum(modes.values())} · AI {modes['AI']} · резервный режим {modes['RULE_BASED']} · без метки {modes['OTHER']}.",
        f"Задержанные материалы: AI {held_items['AI_RETRY']} · чтение первоисточника {held_items['PRIMARY_RETRY']} · автоматическая перепроверка {held_items['WAITING_CONFIRMATION']} · правка опубликованного поста {held_items['AGENT_CORRECTION_QUEUED']}.",
        f"Ожидают автопубликации по всем защитам: {eligible} (автопубликация включена).",
        f"За 24 часа: AI-разборов {ai24} · сбоев AI с отложенным повтором {fallback24} · публикаций {published24} · продолжений сюжетов {story_followups24}.",
        f"Независимая сверка (текущий фильтр): подтверждено {independent_checks24['CORROBORATED']} · расхождения {independent_checks24['CONFLICT']} · нет второго источника {independent_checks24['NO_MATCH'] + independent_checks24['NOT_ASSESSED']} · не требовалась {independent_checks24['NOT_APPLICABLE']} · старые разборы {independent_checks24['LEGACY']}.",
        f"Последний AI-сбой: {last_ai_error or 'за 24 ч не было'} · последняя публикация: {last_publish or 'не было'}.",
        f"Непрочитанные статьи/первоисточники за 24 ч: {primary_failures24} · PDF с OCR на проверке редактора: {ocr_review24}.",
    ]
    grouped_errors = Counter(source["last_error"] for source in sources if source["last_error"])
    if grouped_errors:
        lines.append("Общие сбои доступа: " + "; ".join(f"{code}: {count} источников" for code, count in grouped_errors.items()))
    recovering = [source for source in sources if source["recovery_before"] is not None]
    lines.append(f"Догрузка истории Telegram: {len(recovering)} источников; продолжится в следующем цикле.")
    for source in sources:
        if source["last_error"]:
            lines.append(f"{source['name']}: подряд сбоев {source['consecutive_failures']}; последнее успешное чтение {source['last_success_at'] or 'ещё не зафиксировано'}; источник включён, повтор в следующем цикле.")
    lines.extend(_performance_summary(config, now))
    from .runtime import health_lines
    lines.extend(health_lines(db, now))
    for name, error in failing[:8]:
        lines.append(f"Источник «{name}»: {_safe_error_label(error)}")
    for label, count in errors24.most_common(8):
        if label != "Источник ошибки учтён":
            lines.append(f"Сбой за 24 ч · {label}: {count}")
    recommendations = _improvement_recommendations(errors24, len(failing), len(stale))
    if recommendations:
        lines.append("Предложения по улучшению:")
        lines.extend(f"- {recommendation}" for recommendation in recommendations)
    else:
        lines.append("Предложения по улучшению: повторяющихся операционных ошибок за 24 ч не выявлено.")
    return "\n".join(lines)


def _safe_error_label(message: str) -> str:
    text = (message or "").lower()
    if text == "telegram_local_law_restriction":
        return "Telegram ограничивает просмотр с текущего подключения по местному законодательству"
    if text == "search_no_fresh_news":
        return "Поиск возвращает старые справочные статьи вместо новостей"
    if "ai editor unavailable" in text:
        return "AI_EDITOR_FALLBACK"
    if text.startswith("partial_article_errors:"):
        codes = text.split(":", 1)[1].replace(",", ", ")
        return "PARTIAL_ARTICLE_ERRORS (" + codes.upper() + ")"
    if re.search(r"http[_ ]?(401|403|404|429|5\d\d)", text):
        match = re.search(r"(401|403|404|429|5\d\d)", text)
        return "HTTP_" + match.group(1)
    if "ssl" in text or "tls" in text or "handshake" in text:
        return "TLS_CONNECTION_ERROR"
    if "timeout" in text or "timed out" in text:
        return "NETWORK_TIMEOUT"
    if "feed_not_found" in text or "не найдена объявленная rss/atom-лента" in text:
        return "FEED_NOT_FOUND"
    if re.fullmatch(r"[a-z_]+", text):
        return text.upper()
    return "Источник ошибки учтён"


def list_pending(db) -> None:
    rows = db.execute("SELECT post_id,story_id,version,text,created_at FROM posts WHERE status='PENDING' ORDER BY created_at").fetchall()
    if not rows:
        print("Очередь подтверждения пуста.")
        return
    for row in rows:
        print(f"\n#{row['post_id']} · история {row['story_id']} · версия {row['version']} · {row['created_at']}\n{row['text']}\n")


def is_russian_post(text: str) -> bool:
    content = text.split("\n\nИсточник:", 1)[0]
    content = re.sub(r"https?://\S+", "", content)
    lines = content.splitlines()
    if not lines:
        return False

    def russian_ratio(value: str) -> float:
        letters = re.findall(r"[A-Za-zА-Яа-яЁё]", value)
        if not letters:
            return 0.0
        cyrillic = sum("А" <= char.upper() <= "Я" or char in "Ёё" for char in letters)
        return cyrillic / len(letters)

    return russian_ratio(lines[0]) >= 0.35 and russian_ratio(content) >= 0.35


def _telegram_message_url(config: dict, message_id: str) -> str | None:
    settings = config.get("telegram", {})
    chat_id = os.getenv(settings.get("chat_id_env", "TELEGRAM_CHAT_ID")) or settings.get("chat_id", "")
    chat_id = str(chat_id)
    if chat_id.startswith("@"):
        return f"https://t.me/{chat_id[1:]}/{message_id}"
    digits = chat_id.removeprefix("-")
    if digits.startswith("100") and digits[3:].isdigit() and str(message_id).isdigit():
        return f"https://t.me/c/{digits[3:]}/{message_id}"
    return None


def _previous_story_label(text: str) -> str:
    headline = text.splitlines()[0] if text else ""
    words = re.findall(r"[A-Za-zА-Яа-яЁё0-9$]+", headline)
    if words and words[0].casefold() in {"обновление", "дополнение"}:
        words = words[1:]
    return " ".join(words[:3]) or "предыдущая новость"


def _with_previous_story_link(text: str, url: str, max_length: int, label: str = "предыдущая новость") -> str:
    if "Ранее:" in text:
        return text
    reference = f"Ранее: [{label}]({url})"
    source_start = text.rfind("\n\nИсточник:")
    if source_start < 0:
        body, source = text, ""
    else:
        body, source = text[:source_start], text[source_start:]
    available = max(0, max_length - len(reference) - len(source) - 4)
    if len(body) > available:
        title, separator, content = body.partition("\n\n")
        body_budget = max(0, available - len(title) - len(separator))
        excerpt = content[:body_budget]
        boundaries = [match.end() for match in re.finditer(r"[.!?…]\s+", excerpt)]
        safe_boundary = max(boundaries, default=0)
        if safe_boundary >= int(body_budget * 0.55):
            excerpt = excerpt[:safe_boundary]
        elif body_budget:
            excerpt = excerpt[:excerpt.rfind(" ") if " " in excerpt else body_budget].rstrip(" ,;:")
        body = title + (separator + excerpt + "…" if separator else "")
    return f"{body}\n\n{reference}{source}"


def _attach_previous_story_link(db, config: dict, post) -> None:
    if int(post["version"]) <= 1:
        return
    rows = db.execute(
        "SELECT story_id,external_id,text FROM posts WHERE status='PUBLISHED' "
        "AND external_id IS NOT NULL ORDER BY published_at DESC,post_id DESC LIMIT 600"
    ).fetchall()
    if not rows:
        return
    current_text = re.sub(r"(?m)^(?:Источник|Источники|Ранее):.*$", "", post["text"])
    current_terms = terms(current_text)
    if len(current_terms) < 3:
        return

    def relevance(previous):
        earlier_text = re.sub(r"(?m)^(?:Источник|Источники|Ранее):.*$", "", previous["text"])
        earlier_terms = terms(earlier_text)
        overlap = current_terms & earlier_terms
        coverage = len(overlap) / len(current_terms)
        same_story = str(previous["story_id"]) == str(post["story_id"])
        if not same_story and (len(overlap) < 3 or coverage < 0.22):
            return -1.0
        union = current_terms | earlier_terms
        similarity = len(overlap) / max(1, len(union))
        detail = min(1.0, len(earlier_text) / 1000)
        return coverage * 0.65 + similarity * 0.10 + detail * 0.20 + (0.05 if same_story else 0.0)

    previous = max(rows, key=relevance)
    if relevance(previous) < 0:
        return
    url = _telegram_message_url(config, previous["external_id"])
    if not url:
        return
    max_length = int(config.get("newsroom", {}).get("max_post_length", 3500))
    linked_text = _with_previous_story_link(post["text"], url, max_length, _previous_story_label(previous["text"]))
    if linked_text != post["text"]:
        db.execute("UPDATE posts SET text=?,post_hash=? WHERE post_id=?",
                   (linked_text, hashlib.sha256(linked_text.encode("utf-8")).hexdigest(), post["post_id"]))
        db.commit()


def publish(db, config, post_id: int, automatic: bool = False) -> None:
    require_enabled(config)
    if config.get("newsroom", {}).get("auto_publish", True) is not True:
        raise DeliveryRejected("Автопубликация отключена")
    if not automatic:
        raise RuntimeError("Ручная публикация отключена; отправка доступна только через автоматический редакционный допуск")
    from .decisions import record_publication
    try:
        _publish(db, config, post_id, automatic=True)
    except Exception as exc:
        db.rollback()
        record_publication(db, post_id, 'PUBLICATION_BLOCKED', type(exc).__name__)
        db.commit()
        raise
    record_publication(db, post_id, 'PUBLICATION_CONFIRMED')
    db.commit()


def _publish(db, config, post_id: int, automatic: bool = False) -> None:
    if not automatic:
        raise RuntimeError("Ручная публикация отключена; отправка доступна только через автоматический редакционный допуск")
    post = db.execute("SELECT * FROM posts WHERE post_id=? AND status='PENDING'", (post_id,)).fetchone()
    if not post:
        raise RuntimeError(f"Pending post #{post_id} not found")
    try:
        facts = json.loads(post["fact_check_result"] or "{}")
        primary = facts.get("primary_source") or {}
        report = facts.get("publisher_report") or {}
    except (TypeError, json.JSONDecodeError):
        facts, primary, report = {}, {}, {}
    if not is_eligible_for_auto_publish(post, config["newsroom"].get("auto_publish_since"), config.get("ai", {}).get("_topic_registry")):
        raise RuntimeError("Публикация остановлена: пост не прошёл условия автопубликации")
    report_ok = (facts.get("publisher_report_exception") is True
                 and attributed_report_supported(report, facts, stored=True)
                 and report.get("url") in post["text"])
    primary_ok = (facts.get("primary_source_status") == "READ" and primary.get("url")
                  and primary.get("content_sha256") and primary["url"] in post["text"])
    if not (report_ok or primary_ok):
        raise RuntimeError("Публикация остановлена: нужен прочитанный материал с подтверждённой атрибуцией или первоисточник")
    if report_ok:
        audit = facts.get("original_reporting_check") or {}
        claims = facts.get("facts") or []
        if (facts.get("source_review_required") or audit.get("central_claim_supported") is not True
                or audit.get("attribution_preserved") is not True or not audit.get("evidence", "").strip()
                or report.get("evidence") != audit.get("evidence")
                or not claims or any(f.get("claim_type") not in {"CLAIM", "REPORT", "OPINION"} for f in claims)):
            raise RuntimeError("Публикация остановлена: сообщение источника должно быть точно подтверждено прочитанным текстом и атрибутировано")
    if primary.get("type", "").startswith(("ORIGINAL_MEDIA_", "ORIGINAL_SOCIAL_")):
        audit = facts.get("original_reporting_check") or {}
        claims = facts.get("facts") or []
        if (facts.get("source_review_required") or audit.get("central_claim_supported") is not True
                or audit.get("attribution_preserved") is not True or not audit.get("evidence", "").strip()
                or not claims or any(f.get("claim_type") not in {"CLAIM", "REPORT", "OPINION"} for f in claims)):
            raise RuntimeError("Публикация остановлена: происхождение и атрибуция сообщения СМИ не проверены")
    if automatic and not is_eligible_for_auto_publish(post, config["newsroom"].get("auto_publish_since"), config.get("ai", {}).get("_topic_registry")):
        raise RuntimeError("Публикация остановлена: пост не прошёл условия автопубликации")
    # New-policy text is final: no heuristic links or edits at the send boundary.
    post = db.execute("SELECT * FROM posts WHERE post_id=?", (post_id,)).fetchone()
    if facts.get('policy') or '_policy_baseline' in config.get('ai', {}):
        from .policy import publication_issues
        proof_issues = publication_issues(post['text'], facts, config.get('ai', {}))
        if proof_issues:
            if 'POLICY_CHANGED' in proof_issues and post['origin_item_id']:
                from .policy import requeue_changed_policy
                requeue_changed_policy(db, config, post)
            raise RuntimeError('Публикация остановлена: ' + ', '.join(proof_issues))
    headline, _, body = post["text"].partition("\n")
    issues = editorial_issues(headline, body, facts, final_post=True)
    if issues:
        raise RuntimeError("Публикация остановлена: " + ", ".join(issues))
    if not is_russian_post(post["text"]):
        raise RuntimeError("Публикация остановлена: заголовок и текст должны быть на русском")
    if config.get('ai', {}).get('memory_mode') == 'enforce' or facts.get('memory_mode') == 'enforce':
        from .knowledge import publication_issues
        memory_issues = publication_issues(db, post_id, post['text'])
        if memory_issues:
            raise RuntimeError('Publication memory gate: ' + ', '.join(memory_issues))
    if post['origin_item_id']:
        from .source_recheck import verify
        verify(db, config, post, facts)
        from .material_flow import mark
        mark(db, post['origin_item_id'], 'gate', 'DONE', 'Проверки текста, доказательств и опубликованной истории выполнены.')
        mark(db, post['origin_item_id'], 'delivery', 'RUNNING', 'Отправка через штатный журнал доставки.')
        db.commit()
    publish_started = time.perf_counter()
    try:
        from .news_series import deliver_series
        external_id = deliver_series(db, config, post_id, post['text'], telegram_send,
                                     config['newsroom'].get('max_post_length', 3500))
    except Exception as exc:
        if post['origin_item_id']:
            from .material_flow import mark
            mark(db, post['origin_item_id'], 'delivery', 'WAITING',
                 'Результат отправки неизвестен; требуется сверка.' if isinstance(exc, DeliveryUncertain) else 'Отправка не завершена: '+type(exc).__name__,
                 block_kind='delivery_unknown' if isinstance(exc, DeliveryUncertain) else 'transport')
            db.commit()
        _log_timing("telegram_publish_timing", post_id=post_id,
                    seconds=round(time.perf_counter() - publish_started, 3),
                    result="ERROR", error_type=type(exc).__name__)
        raise
    publish_seconds = time.perf_counter() - publish_started
    if post['origin_item_id']:
        from .material_flow import mark
        mark(db, post['origin_item_id'], 'delivery', 'DONE', 'Ответ Telegram сохранён; публикация подтверждена.')
        db.commit()
    reconcile_posts(db, config)
    print(f"Опубликовано в Telegram, message_id={external_id}")
    _log_timing("telegram_publish_timing", post_id=post_id,
                seconds=round(publish_seconds, 3), result="OK")
    origin = db.execute("SELECT i.item_id,i.published_at,i.updated_at,i.discovered_at FROM items i "
                        "JOIN posts p ON p.origin_item_id=i.item_id WHERE p.post_id=?", (post_id,)).fetchone()
    source_published_at = ((origin["published_at"] if origin else None)
                           or primary.get("published_at") or report.get("published_at")
                           or (origin["updated_at"] if origin else None))
    _log_timing("post_publish_confirmed", post_id=post_id,
                item_id=origin["item_id"] if origin else None,
                source_published_at=source_published_at,
                item_discovered_at=origin["discovered_at"] if origin else None,
                telegram_confirmed_at=NOW())


def run_one_cycle(config: dict, db_path: str) -> dict[str, int]:
    """Run the same complete work cycle used by scheduled and manual collection."""
    require_enabled(config)
    cycle_started = time.perf_counter()
    from .runtime import attach
    attach(config)
    from .interests import expand_search_queries
    expand_search_queries(config)
    inline_attempted_posts: set[int] = set()
    inline_delivery_counts = [0, 0, 0]

    def publish_ready_posts(post_ids):
        selected = [int(post_id) for post_id in post_ids]
        inline_attempted_posts.update(selected)
        result = auto_publish_since(db_path, config, post_ids=selected)
        for index, value in enumerate(result):
            inline_delivery_counts[index] += value

    cycle_config = dict(config)
    cycle_config["_publish_ready_callback"] = publish_ready_posts
    if config.get('newsroom', {}).get('independent_processing'):
        cycle_config['_collection_only'] = True
        cycle_config.pop('_publish_ready_callback', None)
        counts = run_cycle(cycle_config)
        _log_timing('cycle_timing', total_seconds=round(time.perf_counter()-cycle_started, 3), outcomes=counts)
        return counts
    counts = run_cycle(cycle_config)
    published, failed, rejected = auto_publish_since(
        db_path, config, exclude_post_ids=inline_attempted_posts)
    published += inline_delivery_counts[0]
    failed += inline_delivery_counts[1]
    rejected += inline_delivery_counts[2]
    if published:
        counts["AUTO_PUBLISHED"] = published
    if failed:
        counts["AUTO_PUBLISH_ERROR"] = failed
    if rejected:
        counts["AUTO_REJECTED"] = rejected
    try:
        sent, news_count = publish_digest(db_path, config)
        if sent:
            counts["DIGEST_ITEMS"] = news_count
    except Exception as exc:
        print(f"Дайджест не отправлен ({type(exc).__name__}); следующая попытка будет в новом цикле.", file=sys.stderr, flush=True)
    try:
        sent, news_count = publish_digest(db_path, config, kind="weekly")
        if sent:
            counts["WEEKLY_DIGEST_ITEMS"] = news_count
    except Exception as exc:
        print(f"Еженедельный дайджест не отправлен ({type(exc).__name__}); следующая попытка будет в новом цикле.", file=sys.stderr, flush=True)
    if config["newsroom"].get("weekly_analysis_enabled", False):
        try:
            from .analysis import generate_weekly_analysis
            draft_id = generate_weekly_analysis(db_path, config)
            if draft_id:
                counts["WEEKLY_ANALYSIS_DRAFT"] = draft_id
        except Exception as exc:
            print(f"Аналитический черновик не создан ({type(exc).__name__}).", file=sys.stderr, flush=True)
    _log_timing("cycle_timing", total_seconds=round(time.perf_counter() - cycle_started, 3), outcomes=counts)
    print(datetime.now(timezone.utc).isoformat(timespec="seconds"), counts or "Новых материалов нет", flush=True)
    return counts


def _seconds_until_digest(config: dict, now: datetime | None = None) -> float | None:
    """Return time to the next mandatory digest deadline in Moscow time."""
    now = now or datetime.now(timezone.utc)
    local_now = now.astimezone(ZoneInfo("Europe/Moscow"))
    deadlines = []
    daily_due = local_now.replace(hour=20, minute=5, second=0, microsecond=0)
    if daily_due <= local_now:
        daily_due += timedelta(days=1)
    deadlines.append(daily_due)
    days_to_saturday = (5 - local_now.weekday()) % 7
    weekly_due = local_now.replace(hour=19, minute=0, second=0, microsecond=0) + timedelta(days=days_to_saturday)
    if weekly_due <= local_now:
        weekly_due += timedelta(days=7)
    deadlines.append(weekly_due)
    return max(0.0, (min(deadlines) - local_now).total_seconds()) if deadlines else None


def _publish_due_digests(config: dict, db_path: str, *, db=None) -> None:
    for kind, label in (("daily", "Дайджест"), ("weekly", "Еженедельный дайджест")):
        try:
            if db is None:
                sent, news_count = publish_digest(db_path, config, kind=kind)
            else:
                sent, news_count = _publish_digest(db, config, kind)
            if sent:
                print(f"{label}: отправлен, публикаций {news_count}.", flush=True)
            _log_timing("digest_schedule_check", kind=kind, sent=sent, news_count=news_count)
        except Exception as exc:
            if db is not None:
                db.rollback()
            # The next scheduler tick retries known failures. Delivery's durable
            # attempt record blocks a blind resend when Telegram's answer is unknown.
            print(f"{label} не отправлен ({type(exc).__name__}); повторная проверка через 30 секунд.",
                  file=sys.stderr, flush=True)
            _log_timing("digest_schedule_error", kind=kind, error_type=type(exc).__name__)


def _digest_schedule_loop(config_path: str) -> None:
    """Dispatch daily and weekly digests independently of the news collection cycle."""
    db = None
    database = None
    try:
        while True:
            wait = 30.0
            try:
                config = load_config(config_path)
                db_path = config["newsroom"]["database"]
                if db is None or db_path != database:
                    if db is not None:
                        db.close()
                        db = None
                    db = connect(db_path)
                    database = db_path
                db.execute("INSERT OR REPLACE INTO app_state(key,value) VALUES(?,?)",
                           ("digest_scheduler_last_tick_at", NOW()))
                db.commit()
                _publish_due_digests(config, db_path, db=db)
                wait = _seconds_until_digest(config)
            except Exception as exc:
                if db is not None:
                    db.rollback()
                print(f"Планировщик дайджеста: {type(exc).__name__}; повтор через 30 секунд.",
                      file=sys.stderr, flush=True)
            time.sleep(min(30.0, max(1.0, wait or 30.0)))
    finally:
        if db is not None:
            db.close()


def _start_digest_scheduler(config_path: str):
    """Start the digest task inside the existing server process."""
    worker = threading.Thread(target=_digest_schedule_loop, args=(config_path,),
                              name="digest-scheduler", daemon=True)
    worker.start()
    return worker


def main() -> None:
    parser = argparse.ArgumentParser(prog="newsroom", description="Локальный агент мониторинга новостей")
    parser.add_argument("--config", default="config.toml")
    sub = parser.add_subparsers(dest="command", required=True)
    control = sub.add_parser("agent", help="Постоянное включение/отключение всего агента")
    control.add_argument("action", choices=["disable", "enable", "status"])
    policy_command = sub.add_parser('policy', help='Версия правил и безопасная миграция')
    policy_command.add_argument('action', choices=['show', 'activate'])
    recovery = sub.add_parser('recover-material', help='Возобновить технически заблокированный материал после устранения причины')
    recovery.add_argument('--item-id', type=int, required=True)
    recovery.add_argument('--evidence', required=True, help='Подтверждение устранённой причины; сохраняется в истории')
    sub.add_parser("init", help="Создать/обновить локальную базу")
    sub.add_parser("once", help="Проверить все активные RSS-источники один раз")
    sub.add_parser("run", help="Постоянный цикл мониторинга")
    sub.add_parser("digest-run", help="Независимый планировщик ежедневного и недельного дайджестов")
    digest = sub.add_parser("digest", help="Проверить срок и отправить ежедневный или недельный дайджест")
    digest.add_argument("--kind", choices=["daily", "weekly"], default="daily")
    digest.add_argument("--rebuild-unsent", action="store_true",
                        help="Пересобрать только выпуск с заведомо неуспешной доставкой, сохранив историю")
    repair = sub.add_parser("digest-repair", help="Проверить канал и исправить прежний дайджест тем же message_id")
    repair.add_argument("--kind", choices=["daily", "weekly"], default="daily")
    repair.add_argument("--date", help="Дата выпуска в формате ГГГГ-ММ-ДД; по умолчанию сегодня по Москве")
    sub.add_parser("pending", help="Показать посты в автоматической обработке")
    sub.add_parser("health", help="Сводка по источникам, AI, очереди и публикациям")
    sub.add_parser("review-bot", help="Бот сбора редакционных примеров и интересов")
    dashboard = sub.add_parser("dashboard", help="Открыть локальный веб-кабинет редакции")
    dashboard.add_argument("--host", choices=["127.0.0.1", "localhost"], default="127.0.0.1")
    dashboard.add_argument("--port", type=int, default=8765)
    sub.add_parser("recheck", help="Предварительно проверить очередь по текущему фильтру")
    admin_publish = sub.add_parser("admin-publish", help="Опубликовать точный текст по прямой команде владельца из Codex-чата")
    admin_publish.add_argument("--request-key", required=True,
                               help="Устойчивый ключ одной команды; повторное использование защищает от дубля")
    args = parser.parse_args()
    if args.command == "agent":
        with open(args.config, "rb") as stream:
            config = tomllib.load(stream)
        if args.action != "status":
            set_enabled(config, args.action == "enable")
        print("Агент включён" if agent_enabled(config) else "Агент отключён")
        return
    config = load_config(args.config)
    if args.command not in {"dashboard", "health", "pending", "init", "policy"}:
        require_enabled(config)
    db_path = config["newsroom"]["database"]
    database_path = Path(db_path).expanduser()
    if not database_path.is_absolute():
        database_path = Path.cwd() / database_path
    configure_runtime_log(database_path.parent / "newsroom-runtime.log")
    _log_timing("runtime_loaded", build="kovalsky-v2-20260929-r1", command=args.command, pid=os.getpid())
    if args.command == 'policy':
        from . import policy
        if args.action == 'activate':
            policy.activate(db_path)
        from .editorial_registry import attach_cached
        attach_cached(config)
        policy.attach(config)
        print(json.dumps(policy.snapshot(config.get('ai', {})), ensure_ascii=False))
    elif args.command == 'recover-material':
        from .material_flow import recover_technical
        with connect(db_path) as db:
            print(json.dumps(recover_technical(db, config, args.item_id, args.evidence), ensure_ascii=False))
    elif args.command == "init":
        connect(db_path).close()
        print(f"База готова: {db_path}")
    elif args.command == "dashboard":
        from .dashboard import serve
        serve(config, args.host, args.port, args.config)
    elif args.command == "review-bot":
        from .review import run_review_bot
        run_review_bot(config)
    elif args.command == "admin-publish":
        from .admin_publish import publish_from_codex
        result = publish_from_codex(config, args.request_key, sys.stdin.read())
        print(json.dumps(result, ensure_ascii=False))
    elif args.command == "digest":
        sent, news_count = publish_digest(db_path, config, kind=args.kind, rebuild_unsent=args.rebuild_unsent)
        print(("Дайджест отправлен" if sent else "Срок отправки ещё не наступил или выпуск уже отправлен сегодня")
              + f"; публикаций: {news_count}.")
    elif args.command == "digest-run":
        _digest_schedule_loop(args.config)
    elif args.command == "digest-repair":
        from .digest_corrections import repair_digest
        print(json.dumps(repair_digest(db_path, config, args.kind, args.date), ensure_ascii=False))
    elif args.command in {"once", "run"}:
        from .locking import acquire_cycle_lock
        interval = min(180, max(30, int(config["newsroom"].get("poll_interval_seconds", 180))))
        if args.command == "run":
            _start_digest_scheduler(args.config)
            if config['newsroom'].get('independent_processing'):
                connect(db_path).close()
                from .processor import start
                start(args.config)
        while True:
            cycle_started = time.monotonic()
            config = load_config(args.config)
            db_path = config["newsroom"]["database"]
            lock = acquire_cycle_lock(db_path)
            if lock is None:
                print("Сбор пропущен: другой цикл уже выполняется.", flush=True)
            else:
                try:
                    run_one_cycle(config, db_path)
                finally:
                    lock.close()
            if args.command == "once":
                break
            # Keep the cadence start-to-start instead of waiting a full interval
            # after work finishes; an overlong cycle simply starts the next one.
            sleep_for = max(0.0, interval - (time.monotonic() - cycle_started))
            digest_wait = _seconds_until_digest(config)
            if digest_wait is not None:
                sleep_for = min(sleep_for, digest_wait)
            time.sleep(sleep_for)
    else:
        db = connect(db_path)
        try:
            if args.command == "health":
                print(build_health_report(db, config))
            elif args.command == "pending":
                list_pending(db)
            elif args.command == "recheck":
                relevance_terms = config["newsroom"].get("relevance_terms", [])
                rejected = 0
                pending = db.execute("SELECT * FROM posts WHERE status='PENDING'").fetchall()
                keep, remove = [], []
                for post in pending:
                    source_ids = json.loads(post["source_ids"] or "[]")
                    if not source_ids:
                        keep.append((post["post_id"], post["text"].splitlines()[0]))
                        continue
                    placeholders = ",".join("?" for _ in source_ids)
                    matched = db.execute(
                        f"SELECT i.item_id,i.title,i.description,s.type AS source_type,a.result_json FROM items i JOIN sources s USING(source_id) LEFT JOIN item_analysis a USING(item_id) WHERE i.story_id=? AND i.source_id IN ({placeholders})",
                        (post["story_id"], *source_ids),
                    ).fetchall()
                    relevant = False
                    for item in matched:
                        if is_non_news_telegram_format({"type": item["source_type"]},
                                                       {"title": item["title"], "description": item["description"]}):
                            continue
                        if item["result_json"]:
                            try:
                                analysis = json.loads(item["result_json"])
                                if analysis.get("_filter_version") == FILTER_VERSION and isinstance(analysis.get("is_relevant"), bool):
                                    relevant = relevant or analysis["is_relevant"]
                                    continue
                            except (TypeError, json.JSONDecodeError):
                                pass
                        relevant = relevant or is_relevant(f"{item['title']} {item['description']}", relevance_terms)
                    headline = post["text"].splitlines()[0]
                    if relevant:
                        keep.append((post["post_id"], headline))
                        continue
                    remove.append((post["post_id"], headline, post["story_id"], source_ids))
                print(f"Предварительный просмотр. Останутся: {len(keep)}; фильтр предлагает перепроверить: {len(remove)}.")
                if keep:
                    print("\nОстанутся:")
                    for post_id, headline in keep:
                        print(f"  #{post_id} {headline}")
                if remove:
                    print("\nНужна автоматическая перепроверка:")
                    for post_id, headline, _, _ in remove:
                        print(f"  #{post_id} {headline}")
        finally:
            db.close()


if __name__ == "__main__":
    main()
