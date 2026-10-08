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
        resul…78011 tokens truncated…0435кущей остановки.${loc?.type?' Тип: '+esc(loc.type)+'.':''}</p>${loc?.frames?.length?`<div class="muted">Место ошибки: ${loc.frames.map(f=>`${esc(f.file)} · ${esc(f.function)} · строка ${esc(f.line)}`).join(' → ')}</div>`:''}<p>Следующий шаг: проверить актуальное состояние и причину в карточке материала.</p></details>`}).join('')+(!errors.length&&!blocked?'<div class="muted">Сохранённых общих ошибок нет. Причины отдельных отказов и повторов — в карточках обработки.</div>':'');
 }catch(e){document.getElementById('account-alert').textContent='Не удалось обновить состояние ограничений ИИ.';document.getElementById('overview-errors').textContent='Состояние сбоев не проверено.'}
}

async function reloadAll(){
 loadOperations();loadEditorialRegistry();loadTopicRegistry();reloadRegistry();
 if(active==='pipeline')loadPipeline();
 try{
  const [summary,p,src,an]=await Promise.all([api('/api/summary'),api('/api/posts'),api('/api/sources'),api('/api/analysis-drafts')]);
  posts=p.items;sources=src.items;analysisDrafts=an.items;
  document.getElementById('updated').textContent='обновлено '+new Date().toLocaleTimeString('ru-RU',{hour:'2-digit',minute:'2-digit',timeZone:'Europe/Moscow'});
  document.getElementById('agent-state').innerHTML=pill(summary.agent_enabled?'Агент включён':'Агент выключен',summary.agent_enabled?'green':'amber')+'<span>'+(summary.auto_publish_enabled?'Автопубликация включена':'Автопубликация выключена')+'</span><span>Источники проверены '+date(summary.last_check)+'</span>';
  document.getElementById('home-source-health').textContent=`Последняя проверка источников: ${date(summary.last_check)} · Следующий дайджест: ${date(summary.digest_next)}`;
  document.getElementById('workload-status').textContent=(summary.workload_lines||[]).join('\n');
  const advice=document.getElementById('latency-advice');advice.textContent=summary.latency_recommendation||'';advice.style.display=summary.latency_recommendation?'block':'none';
  const improvement=document.getElementById('improvement-advice');improvement.textContent=(summary.improvement_recommendations||[]).join(' · ');improvement.style.display=summary.improvement_recommendations?.length?'block':'none';
  if(active==='resources')loadResources();if(active==='regulatory')loadRegulatory();if(active==='published')renderPublished();if(active==='analysis')renderAnalysis();if(active==='sources')renderSources();
 }catch(e){document.getElementById('agent-state').textContent='Не удалось обновить состояние агента';showNotice('Не удалось загрузить данные: '+e.message)}
}



function correctionStatusLabel(v){return ({QUEUED:'В очереди',PROCESSING:'Проверяет и готовит правку',EDITED:'Исправлено',NO_CHANGE:'Изменений не потребовалось',REJECTED:'Правка отклонена проверкой',UNKNOWN:'Результат требует сверки'})[v]||v}
async function loadCorrections(){try{let r=await api('/api/corrections'),items=r.items||[],el=document.getElementById('correction-status-list');if(!el)return;el.innerHTML=items.length?items.map(function(c){return '<article class=\"row-card\"><div class=\"row-title\">Пост #'+esc(c.external_id||c.post_id)+' · '+esc(c.headline||'')+'</div><div class=\"meta\">'+pill(correctionStatusLabel(c.status),c.status==='EDITED'?'green':c.status==='QUEUED'||c.status==='PROCESSING'?'amber':c.status==='REJECTED'||c.status==='UNKNOWN'?'red':'')+'<span>Обновлено: '+date(c.updated_at)+'</span>'+(c.attempt_count?'<span>Проверок: '+esc(c.attempt_count)+'</span>':'')+'</div>'+(c.result_summary?'<div class=\"queue-reason\">'+esc(c.result_summary)+'</div>':'')+(c.post_url?'<div class=\"actions\"><a class=\"btn\" href=\"'+esc(safeUrl(c.post_url))+'\" target=\"_blank\" rel=\"noopener\">Открыть пост ↗</a></div>':'')+'</article>'}).join(''):'<div class=\"empty\">Нет запросов на исправление опубликованных постов</div>'}catch(e){let el=document.getElementById('correction-status-list');if(el)el.innerHTML='<div class=\"error\">Не удалось загрузить статусы правок</div>'}}
if(active==='policy')loadPolicy();reloadAll();refreshCollectionStatus();loadCorrections();setInterval(loadCorrections,30000);if(location.hash==='#regulatory')showView('regulatory');setInterval(reloadAll,60000);
</script></body></html>'''


def serve(config: dict, host: str = "127.0.0.1", port: int = 8765, config_path: str = "config.toml") -> None:
    if host not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("Кабинет доступен только на этом компьютере.")
    db_path = config["newsroom"]["database"]
    config_file = Path(config_path).resolve()
    token = secrets.token_urlsafe(32)
    tasks_file = Path(__file__).resolve().parent.parent / "KANBAN.json"
    tasks_lock = threading.Lock()
    collection_state_lock = threading.Lock()
    collection_state = {"state": "idle", "started_at": None, "finished_at": None,
                        "outcomes": {}, "error": None}

    class Handler(BaseHTTPRequestHandler):
        server_version = "KovalskyDashboard"

        def log_message(self, fmt, *args):
            return

        def _json(self, payload: dict, status: int = 200):
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                return

        def _db(self):
            return connect(db_path)

        def _read_db(self):
            return connect_readonly(db_path)

        def _source_freshness_minutes(self, source_type):
            if source_type == "web_search":
                interval = min(3, max(1, int(config.get("web_search", {}).get("min_interval_minutes", 3))))
                return interval + 2
            if source_type == "x":
                return int(config.get("x", {}).get("min_interval_minutes", 15)) + 10
            return 10

        def do_GET(self):
            parsed = urlparse(self.path)
            if parsed.path == "/":
                view_names = { "published", "corrections", "sources", "pipeline", "regulatory", "analysis", "resources", "policy"}
                requested_view = parse_qs(parsed.query).get("view", ["pipeline"])[0]
                view = requested_view if requested_view in view_names else "pipeline"
                headings = {
                    "policy": ("Правила редакции", "Единый стандарт и зарегистрированные уточнения владельца"),
                    "resources": ("Расход ресурсов", "Деньги и расходы по задачам"),
                    "published": ("Публикации", "Посты, отправленные в канал"),
                    "sources": ("Источники", "Подключённые новостные ленты"),
                    "pipeline": ("Материалы", "Что получено, где находится и что будет дальше"),
                    "corrections": ("Правки постов", "Проверки и результаты исправлений"),
                    "regulatory": ("Нормативные документы", "Материалы регуляторов и ход рассмотрения"),
                    "analysis": ("Аналитика", "Авторские аналитические материалы"),
                }
                body_text = PAGE.replace("__TOKEN__", token).replace("__INITIAL_VIEW__", view)
                title, subtitle = headings[view]
                body_text = body_text.replace('<h1 id="heading">Материалы</h1>', f'<h1 id="heading">{title}</h1>')
                body_text = body_text.replace('<div class="sub" id="subtitle">Что получено, где находится и что будет дальше</div>', f'<div class="sub" id="subtitle">{subtitle}</div>')
                for name in view_names:
                    body_text = body_text.replace(f'id="view-{name}" class="view active"', f'id="view-{name}" class="view"')
                    body_text = body_text.replace(f'id="view-{name}" class="view"', f'id="view-{name}" class="view active"' if name == view else f'id="view-{name}" class="view"')
                    body_text = body_text.replace(f'<a class="active" href="/?view={name}"', f'<a href="/?view={name}"')
                    if name == view:
                        body_text = body_text.replace(f'<a href="/?view={name}"', f'<a class="active" href="/?view={name}"')
                body = body_text.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'")
                self.end_headers()
                try:
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError):
                    return
                return
            try:
                from .topic_registry import attach_cached
                attach_cached(config)
                if parsed.path == "/api/topic-registry":
                    from .topic_registry import report
                    db = self._read_db()
                    try: self._json(report(db))
                    finally: db.close()
                elif parsed.path == "/api/editorial-registry":
                    from .editorial_registry import report
                    db = self._read_db()
                    try: self._json(report(db))
                    finally: db.close()
                elif parsed.path == "/api/policy":
                    from .policy import document, snapshot, attach
                    from .editorial_registry import attach_cached
                    policy_config = {**config, 'ai': dict(config.get('ai', {}))}
                    attach_cached(policy_config)
                    attach(policy_config)
                    from .editorial_registry import learning_report
                    db = self._read_db()
                    try:
                        self._json({**snapshot(policy_config['ai']), 'document': document(), **learning_report(db)})
                    finally:
                        db.close()
                elif parsed.path == "/api/topic-registry/script":
                    from .google_apps_script import SCRIPT_PATH
                    self._json({"code": SCRIPT_PATH.read_text(encoding="utf-8")})
                elif parsed.path == "/api/topic-registry/learned":
                    from .topic_registry import learned_topics
                    db = self._read_db()
                    try: self._json({"topics": learned_topics(db)})
                    finally: db.close()
                elif parsed.path == "/api/source-registry":
                    from .source_registry import report
                    db = self._read_db()
                    try:
                        self._json(report(db, config))
                    finally:
                        db.close()
                elif parsed.path == "/api/summary":
                    self._json(self._summary())
                elif parsed.path == "/api/diagnostics":
                    from .diagnostics import snapshot
                    db = self._read_db()
                    try:
                        self._json(snapshot(db, config))
                    finally:
                        db.close()
                elif parsed.path == '/api/spending':
                    from .billing import spending
                    query = parse_qs(parsed.query)
                    db = self._read_db()
                    try:
                        self._json(spending(db, config, query.get('period', ['month'])[0],
                                            query.get('start', [None])[0], query.get('end', [None])[0]))
                    except ValueError as exc:
                        self._json({'error': str(exc)}, 400)
                    finally:
                        db.close()
                elif parsed.path == '/api/resources':
                    from .resources import snapshot, counter_start
                    query = parse_qs(parsed.query)
                    try:
                        new_counter = query.get('hours', ['24'])[0] == 'counter'
                        hours = 24 if new_counter else int(query.get('hours', ['24'])[0])
                        item_id = int(query['item_id'][0]) if query.get('item_id') else None
                    except (ValueError, TypeError):
                        self._json({'error': 'Некорректный период или материал'}, 400)
                        return
                    db = self._read_db()
                    try:
                        self._json(snapshot(db, config, hours=hours, item_id=item_id,
                                            start=counter_start(db) if new_counter else None))
                    except ValueError as exc:
                        self._json({'error': str(exc)}, 400)
                    finally:
                        db.close()
                elif parsed.path == "/api/pipeline":
                    from .pipeline import pipeline_snapshot
                    db = self._read_db()
                    try:
                        self._json(pipeline_snapshot(db, config, parse_qs(parsed.query), self._pipeline_posts(db)))
                    finally:
                        db.close()
                elif parsed.path == "/api/regulatory":
                    from .regulatory import snapshot
                    self._json(snapshot(config))
                elif parsed.path == "/api/news":
                    self._json(self._news(parse_qs(parsed.query)))
                elif parsed.path == "/api/posts":
                    self._json(self._posts())
                elif parsed.path == "/api/corrections":
                    self._json(self._corrections())
                elif parsed.path == "/api/analysis-drafts":
                    self._json(self._analysis_drafts())
                elif parsed.path == "/api/sources":
                    self._json(self._sources())
                elif parsed.path == "/api/errors":
                    self._json(self._errors())
                elif parsed.path == "/api/tasks":
                    self._json(self._tasks())
                elif parsed.path == "/api/collection":
                    with collection_state_lock:
                        self._json(dict(collection_state))
                else:
                    self._json({"error": "Не найдено"}, 404)
            except Exception as exc:
                from .diagnostics import error_location
                location = error_location(exc)
                # Server logs contain code locations only, never query arguments
                # or SQLite messages that can include private data.
                print(json.dumps({"event": "dashboard_read_error", "location": location}), flush=True)
                self._json({"error": f"Не удалось прочитать базу ({type(exc).__name__})",
                            "diagnostic": location}, 500)

        def _tasks(self):
            with tasks_lock:
                try:
                    payload = json.loads(tasks_file.read_text(encoding="utf-8"))
                    tasks = payload.get("tasks", [])
                except FileNotFoundError:
                    tasks = []
            return {"tasks": tasks}

        def _summary(self):
            db = self._read_db()
            try:
                now = datetime.now(timezone.utc)
                cut = (now - timedelta(hours=24)).isoformat(timespec="seconds")
                sources = db.execute("SELECT active,type,last_checked_at,last_error FROM sources WHERE active=1").fetchall()
                healthy = 0; stale = 0; failing = 0
                for source in sources:
                    if source["last_error"]:
                        failing += 1
                    elif not source["last_checked_at"]:
                        stale += 1
                    else:
                        try:
                            stamp = datetime.fromisoformat(source["last_checked_at"].replace("Z", "+00:00"))
                            if stamp.tzinfo and stamp >= now - timedelta(minutes=self._source_freshness_minutes(source["type"])): healthy += 1
                            else: stale += 1
                        except ValueError:
                            stale += 1
                pending = db.execute("SELECT * FROM posts WHERE status='PENDING'").fetchall()
                pending_ai = pending_rule = 0
                for p in pending:
                    try: mode = json.loads(p["fact_check_result"] or "{}").get("mode")
                    except (TypeError, json.JSONDecodeError): mode = None
                    if mode == "AI": pending_ai += 1
                    else: pending_rule += 1
                published24 = db.execute("SELECT COUNT(*) FROM posts WHERE status='PUBLISHED' AND published_at>=?", (cut,)).fetchone()[0]
                published_total = db.execute("SELECT COUNT(*) FROM posts WHERE status='PUBLISHED'").fetchone()[0]
                news24 = db.execute("SELECT COUNT(*) FROM items WHERE discovered_at>=?", (cut,)).fetchone()[0]
                ai24 = db.execute("SELECT COUNT(*) FROM item_analysis WHERE created_at>=?", (cut,)).fetchone()[0]
                last_check = db.execute("SELECT MAX(last_checked_at) FROM sources WHERE active=1").fetchone()[0]
                digest_state = db.execute("SELECT value FROM app_state WHERE key='digest_next_at'").fetchone()
                if digest_state:
                    digest_next = digest_state["value"]
                else:
                    from zoneinfo import ZoneInfo
                    local = datetime.now(ZoneInfo("Europe/Moscow"))
                    due = local.replace(hour=20, minute=5, second=0, microsecond=0)
                    if local >= due:
                        due += timedelta(days=1)
                    digest_next = due.astimezone(timezone.utc).isoformat(timespec="seconds")
                weekly_state = db.execute("SELECT value FROM app_state WHERE key='weekly_digest_next_at'").fetchone()
                if weekly_state:
                    weekly_digest_next = weekly_state["value"]
                else:
                    from zoneinfo import ZoneInfo
                    local = datetime.now(ZoneInfo("Europe/Moscow"))
                    days_to_saturday = (5 - local.weekday()) % 7
                    due = local.replace(hour=19, minute=0, second=0, microsecond=0) + timedelta(days=days_to_saturday)
                    if due <= local:
                        due += timedelta(days=7)
                    weekly_digest_next = due.astimezone(timezone.utc).isoformat(timespec="seconds")
                cutoff=config.get("newsroom",{}).get("auto_publish_since")
                auto_eligible=0
                for p in pending:
                    try:
                        from .cli import is_eligible_for_auto_publish
                        facts=json.loads(p["fact_check_result"] or "{}")
                        primary=facts.get("primary_source") or {}
                        eligible=(is_eligible_for_auto_publish(p,cutoff,config.get("ai",{}).get("_topic_registry"))
                                  and publication_source_ready(facts,p['text'])
                                  and facts.get("source_review_required") is not True
                                  and facts.get("independent_check")!="CONFLICT")
                    except (TypeError, KeyError, ValueError, json.JSONDecodeError):
                        eligible=False
                    auto_eligible += int(eligible)
                from .cli import _performance_summary
                latency_advice = next((line for line in _performance_summary(config, now)
                                       if line.startswith("Рекомендация по задержке:")), None)
                from .cli import _improvement_recommendations, _safe_error_label
                recent_errors = Counter()
                for error_row in db.execute("SELECT message,COUNT(*) AS n FROM errors WHERE timestamp>=? GROUP BY message", (cut,)):
                    recent_errors[_safe_error_label(error_row["message"])] += error_row["n"]
                improvement_advice = _improvement_recommendations(recent_errors, failing, stale)
                held = {r["disposition"]:r["n"] for r in db.execute("SELECT disposition,count(*) n FROM items WHERE disposition IN ('AI_RETRY','PRIMARY_RETRY','WAITING_CONFIRMATION','AGENT_CORRECTION_QUEUED') GROUP BY disposition")}
                ai_state = {r["key"]:r["value"] for r in db.execute("SELECT key,value FROM app_state WHERE key IN ('ai_last_success','ai_last_error')")}
                last_ai_error = json.loads(ai_state.get("ai_last_error", "{}"))
                last_ai_ok = ai_state.get("ai_last_success", "")
                ai_current_error = last_ai_error.get("code") if last_ai_error.get("at", "") > last_ai_ok else None
                from .runtime import snapshot as usage_snapshot, health_lines
                from .workflow import snapshot as queue_snapshot
                return {"pending": len(pending), "pending_ai": pending_ai, "pending_rule": pending_rule,
                        "held":held,"ai_current_error":ai_current_error,"ai_last_success":last_ai_ok,
                        "agent_enabled": agent_enabled(config),
                        "auto_publish_enabled": (agent_enabled(config) and config.get("newsroom", {}).get("auto_publish", True) is True), "auto_eligible":auto_eligible,
                        "published24": published24, "published_total": published_total,
                        "source_ok": healthy, "source_error": failing, "source_stale": stale,
                        "source_total": len(sources), "news24": news24, "ai24": ai24,
                        "last_check": last_check, "digest_next": digest_next, "weekly_digest_next": weekly_digest_next,
                        "latency_recommendation": latency_advice,
                        "queue": queue_snapshot(db, now), "api_usage": usage_snapshot(db, now),
                        "workload_lines": health_lines(db, now),
                        "improvement_recommendations": improvement_advice}
            finally: db.close()

        def _news(self, query):
            limit = 500
            sort = query.get("sort", ["discovered"])[0]
            if sort not in {"discovered", "newest", "oldest"}:
                sort = "discovered"
            ordering = ("i.discovered_at DESC,i.item_id DESC" if sort == "discovered" else
                        "CASE WHEN julianday(i.published_at) IS NULL OR julianday(i.published_at)>julianday('now','+5 minutes') THEN 1 ELSE 0 END,"
                        + ("julianday(i.published_at) ASC," if sort == "oldest" else "julianday(i.published_at) DESC,")
                        + "i.discovered_at DESC,i.item_id DESC")
            db = self._read_db()
            try:
                rows = db.execute(
                    "SELECT i.item_id,i.title,i.description,i.url,i.canonical_url,i.published_at,i.discovered_at,i.disposition,i.primary_source_json,"
                    "(SELECT COUNT(*) FROM item_revisions ir WHERE ir.item_id=i.item_id) AS revision_count,"
                    "i.story_id,s.source_id,s.type AS source_type,s.name AS source_name,st.headline AS story_headline,a.result_json,t.value AS triage_json,r.value AS retry_json "
                    "FROM items i JOIN sources s USING(source_id) LEFT JOIN stories st USING(story_id) "
                    "LEFT JOIN item_analysis a USING(item_id) LEFT JOIN app_state t ON t.key='triage:'||i.item_id "
                    "LEFT JOIN app_state r ON r.key='selection_retry:'||i.item_id ORDER BY " + ordering + " LIMIT ?", (limit,)
                ).fetchall()
                items=[]
                for r in rows:
                    try: analysis=json.loads(r["result_json"] or "{}")
                    except (TypeError,json.JSONDecodeError): analysis={}
                    try: selection=json.loads(r["triage_json"] or "{}"); retry=json.loads(r["retry_json"] or "{}")
                    except (TypeError,json.JSONDecodeError): selection={}; retry={}
                    try: primary=json.loads(r["primary_source_json"] or "{}")
                    except (TypeError,json.JSONDecodeError): primary={}
                    processing_reason=None
                    status=r["disposition"]
                    diff=analysis.get("story_diff") or {}
                    if status in {"PRIMARY_RETRY", "AI_RETRY", "WAITING_CONFIRMATION"}:
                        processing_reason=(retry.get("reason") or {
                            "PRIMARY_RETRY":"Ожидает повторного чтения материала.",
                            "AI_RETRY":"ИИ-разбор не завершён; материал ожидает повтора.",
                            "WAITING_CONFIRMATION":"Автоматическая перепроверка ожидает повтора.",
                        }.get(status))
                    elif status == "AGENT_CORRECTION_QUEUED":
                        processing_reason = "Новый прочитанный первоисточник противоречит факту опубликованного поста; редактор проверяет доказательство и правит то же сообщение только после допуска."
                    elif status == "STORE_ONLY":
                        processing_reason=("Повторяет уже сохранённые сведения; нового существенного изменения нет. Сведения оставлены в памяти."
                                           if diff.get("repeated_facts") else
                                           "Подходит по теме, но агент не нашёл существенного нового повода для отдельного поста. Сведения оставлены в памяти.")
                    elif status == "REJECTED":
                        reasons=[]
                        if retry.get("outcome") == "PRIMARY_RETRY":
                            attempts=int(retry.get("attempts") or 0)
                            source_state=primary.get("status") or "не прочитан"
                            reasons.append(f"Первоисточник не удалось подтвердить (статус: {source_state}) после {attempts} автоматических попыток.")
                        for issue in analysis.get("memory_issues") or []:
                            reasons.append("Модель не связала повторное утверждение с уже сохранённым фактом; проверка памяти остановила публикацию." if issue == "PREVIOUS_FACT_REQUIRED" else f"Проверка памяти остановлена: {issue}.")
                        if analysis.get("source_review_required"):
                            original=analysis.get("original_reporting_check") or {}
                            if original.get("central_claim_supported") is False:
                                reasons.append("Центральное утверждение не подтверждено цитатой в сохранённом материале источника.")
                            elif analysis.get("independent_check") == "NOT_ASSESSED":
                                reasons.append("Для самостоятельного сообщения канала не было независимого подтверждения.")
                        reasons.extend(analysis.get("editorial_issues") or [])
                        processing_reason=" ".join(reasons) or "Материал закрыт после исчерпания автоматических проверок. Подробности сохранены в журнале решения."
                    items.append({"item_id":r["item_id"],"title":r["title"],"description":r["description"],
                                  "url":r["canonical_url"] or r["url"],"published_at":r["published_at"],
                                  "discovered_at":r["discovered_at"],"disposition":r["disposition"],
                                  "revision_count":r["revision_count"],
                                  "story_id":r["story_id"],"story_headline":r["story_headline"],
                                  "source_id":r["source_id"],"source_type":r["source_type"],"source_name":r["source_name"],"summary":analysis.get("summary_ru"),
                                  "selection_reason":selection.get("reason"),"processing_reason":processing_reason,"retry_at":retry.get("next_at"),
                                  "importance":analysis.get("importance"),"geographic_scope":analysis.get("geographic_scope"),
                                  "is_relevant":False if r["disposition"] == "NOISE" else analysis.get("is_relevant", True if selection.get("decision") == "KEEP" else None)})
                from .pipeline import retry_queue_positions
                queue_positions, queue_total, queue_batch = retry_queue_positions(db, config, datetime.now(timezone.utc))
                for item in items:
                    item["retry_queue_position"] = queue_positions.get(item["item_id"])
                    item["retry_queue_total"] = queue_total if item["retry_queue_position"] else 0
                    item["retry_queue_batch"] = queue_batch if item["retry_queue_position"] else 0
                    item["retry_queue_cycles"] = ((item["retry_queue_position"] + queue_batch - 1) // queue_batch
                                                   if item["retry_queue_position"] and queue_batch else 0)
                return {"items":items}
            finally: db.close()

        def _corrections(self):
            db = self._read_db()
            try:
                rows = db.execute(
                    "SELECT c.correction_id,c.post_id,c.status,c.result_code,c.result_summary,"
                    "c.created_at,c.updated_at,c.attempt_count,p.external_id,s.headline "
                    "FROM telegram_feedback_corrections c "
                    "JOIN posts p USING(post_id) JOIN stories s USING(story_id) "
                    "ORDER BY c.correction_id DESC LIMIT 100"
                ).fetchall()
                from .cli import _telegram_message_url
                return {"items": [{
                    "correction_id": r["correction_id"], "post_id": r["post_id"],
                    "external_id": r["external_id"], "headline": r["headline"],
                    "status": r["status"], "result_code": r["result_code"],
                    "result_summary": r["result_summary"], "created_at": r["created_at"],
                    "updated_at": r["updated_at"], "attempt_count": r["attempt_count"],
                    "post_url": _telegram_message_url(config, str(r["external_id"])) if r["external_id"] else None,
                } for r in rows]}
            finally:
                db.close()

        def _posts(self, all_rows=False):
            db=self._read_db()
            try:
                rows=db.execute(
                    "SELECT p.*,s.headline,s.first_seen_at FROM posts p JOIN stories s USING(story_id) "
                    "ORDER BY CASE p.status WHEN 'PENDING' THEN 0 WHEN 'PUBLISHED' THEN 1 ELSE 2 END, "
                    "COALESCE(p.published_at,p.created_at) DESC" + ("" if all_rows else " LIMIT 150")
                ).fetchall()
                items=[]
                for r in rows:
                    try: facts=json.loads(r["fact_check_result"] or "{}")
                    except (TypeError,json.JSONDecodeError): facts={}
                    source_ids=[]
                    try: source_ids=json.loads(r["source_ids"] or "[]")
                    except (TypeError,json.JSONDecodeError): pass
                    source_name=None; url=None
                    if source_ids:
                        marks=','.join('?' for _ in source_ids)
                        source=db.execute(f"SELECT name,url FROM sources WHERE source_id IN ({marks}) LIMIT 1",source_ids).fetchone()
                        if source: source_name=source["name"]; url=source["url"]
                    telegram_url=None
                    if r['external_id']:
                        from .cli import _telegram_message_url
                        telegram_url=_telegram_message_url(config,str(r['external_id']))
                    auto_reason=None
                    if r["status"] == "PENDING":
                        settings=config.get("newsroom",{})
                        primary=facts.get("primary_source") or {}
                        if not settings.get("auto_publish_since"):
                            auto_reason="Не задана дата, с которой разрешена автопубликация."
                        elif facts.get("mode") != "AI":
                            auto_reason="Нужен основной AI-разбор; материал автоматически перепроверяется."
                        elif facts.get("_filter_version") != __import__("newsroom.ai",fromlist=["FILTER_VERSION"]).FILTER_VERSION:
                            auto_reason="Нужна автоматическая перепроверка по актуальным правилам."
                        elif facts.get("geographic_scope") not in {"RUSSIA","CIS","RUSSIA_CIS"}:
                            auto_reason="Материал не прошёл географический допуск автопубликации."
                        elif facts.get("russia_cis_impact") != "DIRECT" or not str(facts.get("impact_evidence") or "").strip():
                            auto_reason="Не подтверждено прямое влияние на крипторынок России/СНГ."
                        elif not publication_source_ready(facts,r['text']):
                            auto_reason="Нужен прочитанный материал с точной атрибуцией и ссылкой."
                        elif facts.get("source_review_required") is True:
                            auto_reason="Источник автоматически перепроверяется."
                        elif facts.get("independent_check") == "CONFLICT":
                            auto_reason="Источники расходятся; выполняется автоматическая перепроверка."
                        else:
                            from .cli import is_eligible_for_auto_publish
                            auto_reason=("Подходит для автопубликации; ожидает ближайшего цикла агента."
                                         if is_eligible_for_auto_publish(r,settings.get("auto_publish_since"),config.get("ai",{}).get("_topic_registry"))
                                         else "Не прошёл одно из условий автопубликации; проверьте материал вручную.")
                    latest_item=db.execute("SELECT item_id FROM items WHERE story_id=? ORDER BY discovered_at DESC LIMIT 1",(r["story_id"],)).fetchone()
                    latest_edit=db.execute("SELECT edited_text FROM telegram_post_edits WHERE post_id=? ORDER BY captured_at DESC, ABS(update_id) DESC LIMIT 1",(r["post_id"],)).fetchone()
                    display_text=latest_edit["edited_text"] if latest_edit else r["text"]
                    origin_item_id = r["origin_item_id"] if "origin_item_id" in r.keys() else None
                    items.append({"post_id":r["post_id"],"story_id":r["story_id"],"headline":r["headline"],
                                  "text":display_text,"status":r["status"],"created_at":r["created_at"],
                                  "auto_reason":auto_reason,"auto_attempts":r["auto_attempts"],
                                  "auto_last_error":r["auto_last_error"],"source_ids":source_ids,
                                  "published_at":r["published_at"],"external_id":r["external_id"],
                                  "mode":facts.get("mode"),"facts":facts,"source_name":source_name,"url":url,
                                  "telegram_url":telegram_url,"external_id":r["external_id"],
                                  "origin_item_id":origin_item_id,
                                  "item_id":origin_item_id or (latest_item["item_id"] if latest_item else None)})
                return {"items":items}
            finally: db.close()

        def _pipeline_posts(self, db):
            rows = db.execute(
                "SELECT p.post_id,p.story_id,p.origin_item_id,p.text,p.status,p.created_at,p.published_at,"
                "p.external_id,p.source_ids,p.fact_check_result,p.auto_attempts,p.auto_last_error,"
                "(SELECT edited_text FROM telegram_post_edits e WHERE e.post_id=p.post_id "
                "ORDER BY e.captured_at DESC,ABS(e.update_id) DESC LIMIT 1) AS edited_text "
                "FROM posts p"
            ).fetchall()
            from .cli import _telegram_message_url
            items = []
            for row in rows:
                try:
                    facts = json.loads(row["fact_check_result"] or "{}")
                except (TypeError, json.JSONDecodeError):
                    facts = {}
                try:
                    source_ids = json.loads(row["source_ids"] or "[]")
                except (TypeError, json.JSONDecodeError):
                    source_ids = []
                external_id = row["external_id"]
                text = row["edited_text"] if row["edited_text"] else row["text"]
                items.append({
                    "post_id": row["post_id"], "story_id": row["story_id"],
                    "origin_item_id": row["origin_item_id"], "status": row["status"],
                    "created_at": row["created_at"], "published_at": row["published_at"],
                    "external_id": external_id, "source_ids": source_ids,
                    "text": text, "saved_text": row["text"], "facts": facts, "auto_reason": None,
                    "auto_attempts": row["auto_attempts"],
                    "auto_last_error": row["auto_last_error"],
                    "telegram_url": _telegram_message_url(config, str(external_id)) if external_id else None,
                })
            return items

        def _analysis_drafts(self):
            db=self._read_db()
            try:
                rows=db.execute("SELECT draft_id,created_at,period_start,period_end,title,thesis,body,source_json,status FROM weekly_analysis_drafts ORDER BY draft_id DESC LIMIT 10").fetchall()
                items=[]
                for row in rows:
                    try: sources=json.loads(row["source_json"] or "[]")
                    except (TypeError,json.JSONDecodeError): sources=[]
                    items.append({"draft_id":row["draft_id"],"created_at":row["created_at"],
                                  "period_start":row["period_start"],"period_end":row["period_end"],
                                  "period_start":row["period_start"],"period_end":row["period_end"],
                                  "title":row["title"],"thesis":row["thesis"],"body":row["body"],
                                  "status":row["status"],
                                  "sources":[{k:item.get(k) for k in ("publisher","title","url")} for item in sources]})
                return {"items":items}
            finally: db.close()

        def _sources(self):
            from .cli import _safe_error_label
            db=self._read_db()
            try:
                rows=db.execute("SELECT source_id,name,type,url,active,last_checked_at,last_seen_published_at,last_error FROM sources WHERE active=1 OR NOT EXISTS (SELECT 1 FROM app_state WHERE key='source_archived:'||sources.source_id) ORDER BY active DESC,name").fetchall()
                cutoff=(datetime.now(timezone.utc)-timedelta(hours=24)).isoformat(timespec="seconds")
                intake={r["source_id"]:r for r in db.execute(
                    "SELECT source_id,COUNT(*) AS found,"
                    "SUM(CASE WHEN disposition IN ('NEW_STORY','UPDATE_CANDIDATE') THEN 1 ELSE 0 END) AS useful "
                    "FROM items WHERE discovered_at>=? GROUP BY source_id",(cutoff,)).fetchall()}
                now=datetime.now(timezone.utc); items=[]
                for r in rows:
                    try: checked=datetime.fromisoformat((r["last_checked_at"] or "").replace("Z", "+00:00"))
                    except ValueError: checked=None
                    if r["last_error"]: health="error"
                    elif checked and checked.tzinfo and checked >= now-timedelta(minutes=self._source_freshness_minutes(r["type"])): health="ok"
                    else: health="stale"
                    items.append({"source_id":r["source_id"],"name":r["name"],"type":r["type"],"url":r["url"],
                                  "active":bool(r["active"]),"last_checked_at":r["last_checked_at"],
                                  "last_seen_published_at":r["last_seen_published_at"],
                                  "found24":int(intake.get(r["source_id"])["found"]) if r["source_id"] in intake else 0,
                                  "useful24":int(intake.get(r["source_id"])["useful"] or 0) if r["source_id"] in intake else 0,"health":health,
                                  "error":_safe_error_label(r["last_error"]) if r["last_error"] else None})
                return {"items":items}
            finally: db.close()

        def _errors(self):
            from .cli import _safe_error_label
            db=self._read_db()
            try:
                rows=db.execute("SELECT e.timestamp,e.message,s.name AS source_name FROM errors e LEFT JOIN sources s USING(source_id) ORDER BY e.timestamp DESC LIMIT 100").fetchall()
                return {"items":[{"timestamp":r["timestamp"],"source_name":r["source_name"],"label":_safe_error_label(r["message"])} for r in rows]}
            finally: db.close()

        def do_POST(self):
            if self.client_address[0] not in {"127.0.0.1", "::1"} or not secrets.compare_digest(self.headers.get("X-Dashboard-Token", ""), token):
                self._json({"error": "Нет доступа"}, 403); return
            try:
                size=int(self.headers.get("Content-Length", "0"))
                if size > 8192: self._json({"error":"Слишком большой запрос"},413); return
                parsed=urlparse(self.path); parts=[unquote(x) for x in parsed.path.strip('/').split('/')]
                from .agent_control import enabled
                if parts in (["api", "collect"], ["api", "intake-url"], ["api", "post-corrections"]) and not enabled(config):
                    self._json({"error": "Агент отключён владельцем"}, 409)
                    return
                if parts in (["api", "billing", "report"], ["api", "billing", "sync"]):
                    from .billing import save_owner_report, sync
                    payload = json.loads(self.rfile.read(size).decode('utf-8'))
                    if not isinstance(payload, dict):
                        self._json({'error': 'Некорректные данные'}, 400)
                        return
                    db = self._db()
                    try:
                        if parts[-1] == 'report':
                            self._json({'ok': True, 'report': save_owner_report(db, payload)})
                        else:
                            query = parse_qs(parsed.query)
                            self._json(sync(db, config, query.get('period', ['month'])[0],
                                            query.get('start', [None])[0], query.get('end', [None])[0]))
                    except ValueError as exc:
                        self._json({'error': str(exc)}, 400)
                    finally:
                        db.close()
                    return
                if parts == ["api", "editorial-registry"]:
                    from .editorial_registry import configure
                    payload = json.loads(self.rfile.read(size).decode("utf-8"))
                    db = self._db()
                    try: self._json(configure(db, payload), 201)
                    except ValueError as exc: self._json({"error": str(exc)}, 400)
                    except Exception: self._json({"error": "Не удалось прочитать редакторские вкладки"}, 503)
                    finally: db.close()
                    return
                if parts == ['api', 'policy', 'clarify']:
                    from .editorial_registry import clarify
                    payload = json.loads(self.rfile.read(size).decode('utf-8'))
                    db = self._db()
                    try:
                        feedback_id = clarify(db, payload.get('key'), payload.get('answer'))
                        self._json({'feedback_id': feedback_id}, 201)
                    except ValueError as exc:
                        self._json({'error': str(exc)}, 400)
                    finally:
                        db.close()
                    return
                if parts == ["api", "topic-registry", "apps-script"]:
                    from .google_apps_script import configure as configure_script
                    payload = json.loads(self.rfile.read(size).decode("utf-8"))
                    db = self._db()
                    try: self._json(configure_script(db, payload), 201)
                    except ValueError as exc: self._json({"error": str(exc)}, 400)
                    except RuntimeError as exc:
                        code = str(exc)
                        if not code.startswith('APPS_SCRIPT_'): code = 'APPS_SCRIPT_REQUEST_FAILED'
                        self._json({"error": "Google не подтвердил подключение (" + code + ").", "code": code}, 503)
                    except Exception: self._json({"error": "Не удалось проверить запись. Проверьте адрес /exec, код доступа, сервис Google Sheets и настройки развертывания: от вашего имени, доступ — Все."}, 503)
                    finally: db.close()
                    return
                if parts == ["api", "topic-registry", "credentials"]:
                    from .google_sheets_auth import configure as configure_credentials
                    payload = json.loads(self.rfile.read(size).decode("utf-8"))
                    db = self._db()
                    try: self._json(configure_credentials(db, payload), 201)
                    except ValueError as exc: self._json({"error": str(exc)}, 400)
                    except Exception: self._json({"error": "Google не подтвердил доступ на запись. Проверьте ключ, включение Google Sheets API и право редактора таблицы."}, 503)
                    finally: db.close()
                    return
                if parts == ["api", "topic-registry"]:
                    from .topic_registry import configure
                    payload = json.loads(self.rfile.read(size).decode("utf-8"))
                    db = self._db()
                    try: self._json(configure(db, payload), 201)
                    except ValueError as exc: self._json({"error": str(exc)}, 400)
                    except Exception as exc: self._json({"error": "Не удалось прочитать темник ("+type(exc).__name__+")"}, 503)
                    finally: db.close()
                    return
                if parts == ["api", "source-registry"]:
                    from .source_registry import configure
                    payload = json.loads(self.rfile.read(size).decode("utf-8"))
                    db = self._db()
                    try:
                        self._json(configure(db, payload), 201)
                    except ValueError as exc:
                        self._json({"error": str(exc)}, 400)
                    except Exception as exc:
                        self._json({"error": f"Не удалось прочитать таблицу ({type(exc).__name__})"}, 503)
                    finally:
                        db.close()
                    return
                if parts==["api","interest-feedback"]:
                    payload=json.loads(self.rfile.read(size).decode("utf-8"))
                    try: item_id=int(payload.get("item_id"))
                    except (ValueError,TypeError): self._json({"error":"Материал не указан"},400); return
                    rating=payload.get("is_interesting")
                    if not isinstance(rating,bool): self._json({"error":"Укажите оценку интереса"},400); return
                    db=self._db()
                    try:
                        from .interests import save_item_feedback
                        topics=save_item_feedback(db,item_id,rating,config.get("ai",{}))
                        self._json({"ok":True,"topics":topics,"is_interesting":rating})
                    except ValueError as exc: self._json({"error":str(exc)},404)
                    except Exception as exc: self._json({"error":f"Не удалось сохранить оценку ({type(exc).__name__})"},503)
                    finally: db.close()
                    return
                if parts==["api","post-corrections"]:
                    payload=json.loads(self.rfile.read(size).decode("utf-8"))
                    try: post_id=int(payload.get("post_id")) if payload.get("post_id") else None
                    except (ValueError,TypeError): self._json({"error":"Некорректный пост"},400); return
                    message_id=str(payload.get("telegram_message_id") or "").strip() or None
                    if message_id and not message_id.isdigit():
                        self._json({"error":"Некорректный ID сообщения"},400); return
                    try: reference_message_id=int(payload.get("reference_message_id")) if payload.get("reference_message_id") else None
                    except (ValueError,TypeError): self._json({"error":"Некорректный ID подробной публикации"},400); return
                    if post_id is None and message_id is None:
                        self._json({"error":"Не указан опубликованный пост"},400); return
                    db=self._db()
                    try:
                        result,status=_queue_post_correction(
                            db,config,post_id,str(payload.get("reason") or ""),message_id,reference_message_id)
                        self._json(result,status)
                    except Exception as exc:
                        db.rollback()
                        self._json({"error":f"Не удалось поставить правку в очередь ({type(exc).__name__})"},503)
                    finally: db.close()
                    return
                if parts==["api","editorial-feedback"]:
                    payload=json.loads(self.rfile.read(size).decode("utf-8"))
                    categories={"POSITIVE","CORRECTION","TELEGRAM_EDIT","NOT_RELEVANT","NOT_IMPORTANT","DUPLICATE","INACCURATE","POOR_STYLE","OTHER"}
                    feedback_type=str(payload.get("feedback_type") or "")
                    reason=str(payload.get("reason") or "").strip()
                    try: item_id=int(payload.get("item_id")) if payload.get("item_id") else None
                    except (ValueError,TypeError): self._json({"error":"Некорректный материал"},400); return
                    try: post_id=int(payload.get("post_id")) if payload.get("post_id") else None
                    except (ValueError,TypeError): self._json({"error":"Некорректный пост"},400); return
                    if feedback_type not in categories or len(reason)<5 or len(reason)>2000 or not (item_id or post_id):
                        self._json({"error":"Выберите тип обратной связи, добавьте комментарий и укажите материал"},400); return
                    db=self._db()
                    try:
                        item=db.execute("SELECT item_id,story_id,title FROM items WHERE item_id=?",(item_id,)).fetchone() if item_id else None
                        post=db.execute("SELECT post_id,story_id,text FROM posts WHERE post_id=?",(post_id,)).fetchone() if post_id else None
                        if item_id and item is None: self._json({"error":"Новость не найдена"},404); return
                        if post_id and post is None: self._json({"error":"Пост не найден"},404); return
                        if item and post and item["story_id"]!=post["story_id"]:
                            self._json({"error":"Новость и пост относятся к разным сюжетам"},400); return
                        story_id=(post["story_id"] if post else item["story_id"])
                        item_title=item["title"] if item else ""
                        if post:
                            latest_edit=db.execute("SELECT edited_text FROM telegram_post_edits WHERE post_id=? ORDER BY captured_at DESC, ABS(update_id) DESC LIMIT 1",(post_id,)).fetchone()
                            post_text=latest_edit["edited_text"] if latest_edit else post["text"]
                        else:
                            post_text=""
                        db.execute("INSERT INTO editorial_feedback(created_at,item_id,story_id,post_id,feedback_type,reason,item_title,post_text) VALUES(?,?,?,?,?,?,?,?)",
                                   (datetime.now(timezone.utc).isoformat(timespec="seconds"),item_id,story_id,post_id,feedback_type,reason,item_title,post_text[:5000]))
                        db.commit()
                        self._json({"ok":True})
                    except Exception as exc: self._json({"error":f"Не удалось сохранить отзыв ({type(exc).__name__})"},503)
                    finally: db.close()
                    return
                if parts==["api","intake-url"]:
                    payload=json.loads(self.rfile.read(size).decode("utf-8"))
                    from .manual_intake import IntakeBusyError, IntakeError, submit_article_url
                    try:
                        result=submit_article_url(config,str(payload.get("url") or ""))
                    except IntakeBusyError as exc:
                        self._json({"error":str(exc)},409); return
                    except IntakeError as exc:
                        self._json({"error":str(exc)},422); return
                    self._json(result,200); return
                if parts==["api","collect"]:
                    from .locking import acquire_cycle_lock
                    lock=acquire_cycle_lock(db_path)
                    if lock is None:
                        self._json({"error":"Сбор уже выполняется планировщиком или другим запуском."},409); return
                    with collection_state_lock:
                        if collection_state["state"]=="running":
                            lock.close()
                            self._json({"error":"Сбор уже выполняется."},409); return
                        collection_state.update({"state":"running","started_at":datetime.now(timezone.utc).isoformat(timespec="seconds"),"finished_at":None,"outcomes":{},"error":None})
                    def collect_in_background():
                        try:
                            from .cli import load_config, run_one_cycle
                            current_config=load_config(str(config_file))
                            if current_config['newsroom']['database'] != db_path:
                                raise RuntimeError('Database changed; restart dashboard')
                            outcomes=run_one_cycle(current_config,db_path)
                            with collection_state_lock:
                                collection_state.update({"state":"done","finished_at":datetime.now(timezone.utc).isoformat(timespec="seconds"),"outcomes":outcomes,"error":None})
                        except Exception as exc:
                            with collection_state_lock:
                                collection_state.update({"state":"error","finished_at":datetime.now(timezone.utc).isoformat(timespec="seconds"),"error":type(exc).__name__})
                        finally:
                            lock.close()
                    threading.Thread(target=collect_in_background,daemon=True,name="newsroom-manual-cycle").start()
                    self._json({"ok":True,"state":"running"},202); return
                if len(parts)==3 and parts[:2]==["api","tasks"]:
                    task_id=parts[2]
                    payload=json.loads(self.rfile.read(size).decode("utf-8"))
                    status=payload.get("status")
                    if status not in {"todo","in_progress","waiting","backlog","done"}:
                        self._json({"error":"Неизвестный статус задачи"},400); return
                    with tasks_lock:
                        try:
                            board=json.loads(tasks_file.read_text(encoding="utf-8"))
                            tasks=board.get("tasks",[])
                        except FileNotFoundError:
                            tasks=[]
                        task=next((entry for entry in tasks if entry.get("id")==task_id),None)
                        if task is None:
                            self._json({"error":"Задача не найдена"},404); return
                        task["status"]=status
                        temporary=tasks_file.with_name(f".{tasks_file.name}.{os.getpid()}.tmp")
                        temporary.write_text(json.dumps({"tasks":tasks},ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
                        os.replace(temporary,tasks_file)
                    self._json({"ok":True,"tasks":tasks}); return
                if parts==["api","sources"]:
                    from .source_registry import state, SETTINGS
                    db = self._read_db()
                    try:
                        registry_settings = state(db, SETTINGS)
                    finally:
                        db.close()
                    if registry_settings:
                        self._json({"error":"Источники меняются в общей Google Таблице",
                                    "registry_url":registry_settings['url']},409); return
                    payload=json.loads(self.rfile.read(size).decode("utf-8"))
                    name=str(payload.get("name","")).strip()
                    source_type=payload.get("type")
                    source_url=str(payload.get("url","")).strip()
                    reputation=payload.get("reputation","unknown")
                    if not name or len(name)>100:
                        self._json({"error":"Укажите название источника (до 100 символов)"},400); return
                    if source_type not in {"rss","web","telegram","google_news"}:
                        self._json({"error":"Неизвестный тип источника"},400); return
                    if reputation not in {"unknown","reputable_media"}:
                        self._json({"error":"Неизвестная оценка источника"},400); return
                    parsed_source=urlparse(source_url)
                    if (parsed_source.scheme not in {"http","https"} or not parsed_source.hostname
                            or parsed_source.username or parsed_source.password):
                        self._json({"error":"Укажите полный HTTP или HTTPS адрес источника"},400); return
                    if source_type=="telegram" and parsed_source.hostname.lower().removeprefix("www.")!="t.me":
                        self._json({"error":"Для Telegram нужен адрес публичного канала t.me"},400); return
                    if source_type=="google_news" and parsed_source.hostname.lower().removeprefix("www.")!="news.google.com":
                        self._json({"error":"Для Google News укажите RSS-адрес news.google.com"},400); return
                    if len(source_url)>2000:
                        self._json({"error":"Адрес источника слишком длинный"},400); return
                    if any(str(entry.get("url","")).rstrip("/")==source_url.rstrip("/") for entry in config.get("sources",[])):
                        self._json({"error":"Этот адрес уже есть в настройках источников"},409); return
                    priority=3
                    block=("\n[[sources]]\n"
                           f"name = {json.dumps(name,ensure_ascii=False)}\n"
                           f"type = {json.dumps(source_type)}\n"
                           f"url = {json.dumps(source_url,ensure_ascii=False)}\n"
                           "active = true\n"
                           f"priority = {priority}\n"
                           f"reputation = {json.dumps(reputation)}\n")
                    temporary=None
                    try:
                        original=config_file.read_text(encoding="utf-8")
                        mode=config_file.stat().st_mode & 0o777
                        temporary=config_file.with_name(f".{config_file.name}.{os.getpid()}.tmp")
                        temporary.write_text(original.rstrip()+block,encoding="utf-8")
                        os.chmod(temporary,mode)
                        os.replace(temporary,config_file)
                        db=self._db()
                        try:
                            db.execute("INSERT INTO sources(name,type,url,active,priority,reputation) VALUES(?,?,?,?,?,?) "
                                       "ON CONFLICT(url) DO UPDATE SET name=excluded.name,type=excluded.type,active=1, "
                                       "priority=excluded.priority,reputation=excluded.reputation",
                                       (name,source_type,source_url,1,priority,reputation))
                            db.commit()
                        finally: db.close()
                    except Exception as exc:
                        if temporary:
                            try: temporary.unlink()
                            except OSError: pass
                        if "UNIQUE" in str(exc):
                            self._json({"error":"Этот адрес уже есть в базе источников"},409); return
                        self._json({"error":f"Не удалось сохранить источник ({type(exc).__name__})"},500); return
                    config.setdefault("sources",[]).append({"name":name,"type":source_type,"url":source_url,
                                                             "active":True,"priority":priority,"reputation":reputation})
                    self._json({"ok":True,"source":name},201); return
                if parts[:2]==["api","posts"]:
                    self._json({"error":"Ручное управление публикациями отключено; решение принимает автоматический редакционный допуск."},410)
                    return
                self._json({"error":"Не найдено"},404)
            except Exception as exc:
                self._json({"error":f"Действие не выполнено ({type(exc).__name__})"},500)

    server=ThreadingHTTPServer((host,port),Handler)
    server.daemon_threads=True
    print(f"Кабинет Kovalsky доступен только на этом компьютере: http://127.0.0.1:{port}",flush=True)
    try: server.serve_forever()
    except KeyboardInterrupt: pass
    finally: server.server_close()
