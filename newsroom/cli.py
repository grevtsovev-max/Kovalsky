from __future__ import annotations


import argparse
try:
    import tomllib
except ImportError:
    import tomli as tomllib


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


import urllib.parse


import urllib.request


from datetime import datetime, timedelta, timezone


from pathlib import Path


from zoneinfo import ZoneInfo


from .agent_control import AgentDisabled, enabled as agent_enabled, require_enabled, set_enabled


from .db import connect
from .core import NOW, _log_timing, configure_runtime_log, run_cycle


from .delivery import (DeliveryRejected, DeliveryUncertain, TelegramReceipt,
                       deliver, confirm, reconcile_posts, channel, replace_unsent_digest_batch)


def load_config(path: str) -> dict:
    with open(path, "rb") as f:
        config = tomllib.load(f)
    config.setdefault("newsroom", {}).setdefault("auto_publish", False)
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
        from .delivery import telegram_rate_limit
        rate_limit = telegram_rate_limit(rejection)
        if rate_limit and exc.code == 429:
            raise rate_limit from None
        if isinstance(rejection, dict) and rejection.get("ok") is False and 400 <= exc.code < 500 and exc.code != 408:
            raise DeliveryRejected(f"Telegram API rejected request ({exc.code})") from None
        raise DeliveryUncertain(f"Telegram API HTTP {exc.code}") from None
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise DeliveryUncertain(f"Telegram API network error ({type(exc).__name__})") from None
    if not isinstance(result, dict) or result.get("ok") not in (True, False):
        raise DeliveryUncertain("Telegram API malformed response")
    if result.get("ok") is False:
        from .delivery import telegram_rate_limit
        rate_limit = telegram_rate_limit(result)
        if rate_limit:
            raise rate_limit
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



def _run_collection_cycle(config, db_path, *, persistent=False):
    # Collection and the independent editor share a process. A transient
    # SQLite write collision must not kill in-flight editorial/API work.
    import sqlite3
    try:
        return run_one_cycle(config, db_path)
    except sqlite3.OperationalError as exc:
        code = getattr(exc, 'sqlite_errorcode', None)
        busy = ((code is not None and (code & 255) in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED})
                or str(exc) in {'database is locked', 'database table is locked'})
        if not persistent or not busy:
            raise
        print('Сбор отложен: база временно занята.', flush=True)
        return None


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



def run_one_cycle(config, db_path):
    require_enabled(config)
    from .topic_registry import attach_cached
    attach_cached(config)
    return run_cycle({**config, '_collection_only': True})


def main():
    parser = argparse.ArgumentParser(prog='newsroom', description='Сбор и хранение материалов')
    parser.add_argument('--config', default='config.toml')
    sub = parser.add_subparsers(dest='command', required=True)
    control = sub.add_parser('agent')
    control.add_argument('action', choices=['disable', 'enable', 'status'])
    for name in ('init', 'once', 'run', 'health', 'pending', 'filter-audit-export'):
        sub.add_parser(name)
    dashboard = sub.add_parser('dashboard')
    dashboard.add_argument('--host', choices=['127.0.0.1','localhost'], default='127.0.0.1')
    dashboard.add_argument('--port', type=int, default=8765)
    owner = sub.add_parser('admin-publish')
    owner.add_argument('--request-key', required=True)
    args = parser.parse_args()
    config = load_config(args.config)
    if args.command == 'agent':
        if args.action != 'status': set_enabled(config, args.action == 'enable')
        print('Агент включён' if agent_enabled(config) else 'Агент отключён')
        return
    db_path = config['newsroom']['database']
    configure_runtime_log(Path(db_path).parent / 'newsroom-runtime.log')
    if args.command == 'dashboard':
        from .dashboard import serve
        serve(config,args.host,args.port,args.config)
    elif args.command == 'admin-publish':
        require_enabled(config)
        from .admin_publish import publish_from_codex
        print(json.dumps(publish_from_codex(config,args.request_key,sys.stdin.read()),ensure_ascii=False))
    elif args.command in {'once','run'}:
        from .locking import acquire_cycle_lock
        if args.command=='run':
            from .edition.worker import run as run_editor
            editor_stop=threading.Event()
            threading.Thread(target=run_editor,args=(args.config,editor_stop),daemon=True,name='newsroom-edition').start()
        while True:
            started = time.monotonic()
            config = load_config(args.config)
            require_enabled(config)
            lock = acquire_cycle_lock(db_path)
            if lock:
                try: print(_run_collection_cycle(config,db_path,persistent=args.command=='run'),flush=True)
                finally: lock.close()
            if args.command == 'once': break
            interval = min(180,max(30,int(config['newsroom'].get('poll_interval_seconds',180))))
            time.sleep(max(0,interval-(time.monotonic()-started)))
    else:
        db = connect(db_path)
        try:
            if args.command == 'filter-audit-export':
                from .filter_audit import export
                export(db,sys.stdout)
            elif args.command == 'init': print('База готова')
            elif args.command == 'health':
                print(json.dumps({'editorial':'enabled' if config.get('editorial',{}).get('enabled') is True else 'disabled','materials':db.execute('SELECT COUNT(*) FROM items').fetchone()[0],
                                  'sources':db.execute('SELECT COUNT(*) FROM sources WHERE active=1').fetchone()[0]},ensure_ascii=False))
            else:
                from .edition.views import overview
                print(json.dumps(overview(db,config),ensure_ascii=False))
        finally: db.close()


if __name__ == '__main__': main()
