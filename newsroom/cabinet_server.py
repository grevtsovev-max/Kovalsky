from __future__ import annotations

import json
import os
import secrets
import threading
from collections import Counter
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from .agent_control import enabled as agent_enabled
from .db import connect, connect_readonly



PAGE = r'''
<!doctype html><html lang="ru"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Kovalsky</title><style>body{margin:32px auto;padding:20px;max-width:1100px;font:16px/1.5 system-ui;color:#172236;background:#f4f6fa}button{font:inherit;padding:9px 14px;cursor:pointer;margin:4px;border:1px solid #ddd;border-radius:8px;background:white}.row-card{padding:18px;margin:12px 0;background:white;border-radius:12px;overflow-wrap:anywhere}a{color:#365cf5}
</style><body><main style="max-width:1100px;margin:30px auto;padding:20px"><h1>Kovalsky</h1><p>Новая редакция: подготовка, проверка и автоматическая публикация. Цель — до 10 минут от получения материала до канала.</p><div id="summary"></div><p><button class="btn" onclick="collect()">Собрать материалы</button> <button class="btn" onclick="show('news')">Материалы</button> <button class="btn" onclick="show('sources')">Источники</button> <button class="btn" onclick="show('posts')">Опубликованное</button> <button class="btn" onclick="show('resources')">Расход ресурсов</button></p><p><button class="btn" onclick="showEdition()">Редакция</button></p><div id="notice"></div><div id="registry"></div><div id="content"></div></main><script>
const token='__TOKEN__';
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const safe=u=>{try{const x=new URL(u);return ['https:','http:'].includes(x.protocol)?x.href:'#'}catch{return '#'}};
async function api(p,opts={}){let r=await fetch('/api/'+p,{...opts,headers:{'Content-Type':'application/json','X-Dashboard-Token':token}});let d=await r.json();if(!r.ok)throw Error(d.error||'Не удалось выполнить запрос');return d}
async function show(kind){try{let d=await api(kind);let rows=Array.isArray(d)?d:d.items||d.sources||[];document.getElementById('content').innerHTML=kind==='resources'?'<article class="row-card"><h3>Расходы за последние сутки</h3><p>Обращений к ИИ: '+esc(d.total.calls)+'</p><p>Задач: '+esc(d.total.operations)+'</p><p>Оценка расходов: '+(d.total.estimated_usd==null?'нет полной оценки':'$'+Number(d.total.estimated_usd).toFixed(4))+'</p></article>':rows.map(x=>'<article class="row-card"><h3>'+esc(x.title||x.headline||x.name||'')+'</h3><p>'+esc(x.source_name||x.type||x.status||x.disposition||'')+'</p>'+(x.url||x.telegram_url?'<a href="'+esc(safe(x.url||x.telegram_url))+'" target="_blank" rel="noopener">Открыть ↗</a>':'')+(kind==='posts'?'<p style="white-space:pre-wrap">'+esc(x.text)+'</p>':'')+'</article>').join('')||'<p>Пока нет материалов.</p>'}catch(e){document.getElementById('notice').textContent=e.message}}
async function retryEdition(id){try{await api('edition/retry',{method:'POST',body:JSON.stringify({job_id:id})});await showEdition()}catch(e){document.getElementById('notice').textContent=e.message}}
async function editionDetails(id){try{let d=await api('edition/job?job_id='+encodeURIComponent(id));document.getElementById('content').innerHTML='<button onclick="showEdition()">Назад</button><h2>Исходные материалы</h2>'+d.materials.map(m=>'<article class="row-card"><h3>'+esc(m.title)+'</h3><a href="'+esc(safe(m.url))+'" target="_blank" rel="noopener">'+esc(m.source_name)+'</a><p style="white-space:pre-wrap">'+esc(m.content||m.description)+'</p></article>').join('')+'<h2>Версии и проверки</h2>'+d.events.filter(e=>['draft','repair','check','published','INCOMPLETE'].includes(e.stage)).map(e=>'<article class="row-card"><h3>'+esc(({draft:'Первая версия',repair:'Доработка',check:'Проверка',published:'Отправлено',INCOMPLETE:'Не завершено'})[e.stage])+'</h3><p style="white-space:pre-wrap">'+esc(e.payload.draft?[e.payload.draft.headline,e.payload.draft.lead,...e.payload.draft.blocks.map(b=>b.text)].join('\n\n'):(e.payload.issues||e.payload.reasons||[]).map(x=>x.reason).join('\n'))+'</p></article>').join('')}catch(e){document.getElementById('notice').textContent=e.message}}
async function showEdition(){try{let d=await api('edition');document.getElementById('content').innerHTML='<h2>Редакция</h2><p>'+(d.enabled?'Включена':'Отключена')+' · Сохранено референсов: '+esc(d.references)+'</p>'+d.jobs.map(j=>'<article class="row-card"><h3>'+esc(j.documents.map(x=>x.subject).join('; ')||'Подготовка материалов')+'</h3><p>'+esc(j.state_label)+(j.delayed?' · Задержка более 10 минут':'')+'</p>'+j.documents.map(x=>'<p>'+esc(x.state_label)+'</p><p style="white-space:pre-wrap">'+esc(x.text||x.draft.lead||'')+'</p>'+x.reasons.map(r=>'<p>'+esc(r.reason)+'</p>').join('')).join('')+j.reasons.map(r=>'<p>'+esc(r.reason)+'</p>').join('')+'<button onclick="editionDetails(\''+j.job_id+'\')">Исходники и проверки</button>'+(j.retryable?' <button onclick="retryEdition(\''+j.job_id+'\')">Повторить подготовку</button>':'')+'</article>').join('')+(d.jobs.length?'':'<p>Пока нет подготовок.</p>')}catch(e){document.getElementById('notice').textContent=e.message}}
async function collect(){try{await api('collect',{method:'POST',body:'{}'});document.getElementById('notice').textContent='Сбор запущен'}catch(e){document.getElementById('notice').textContent=e.message}}
async function load(){let d=await api('summary');document.getElementById('summary').textContent='Материалов: '+d.materials+' · Источников: '+d.source_total+' · Опубликовано ранее: '+d.published_total;try{let r=await api('topic-registry');if(r.url)document.getElementById('registry').innerHTML='<a href="'+esc(safe(r.url))+'" target="_blank" rel="noopener">Открыть темник ↗</a>'}catch{}show('news')}load();
</script></body></html>
'''

from .dashboard_shell import green_shell

from .cabinet_page import PAGE


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
                view_names = { "vacancies", "published", "sources", "pipeline", "resources", "edition"}
                requested_view = parse_qs(parsed.query).get("view", ["pipeline"])[0]
                view = requested_view if requested_view in view_names else "pipeline"
                headings = {
                    "vacancies": ("Вакансии", "Объявления о работе и условия"),
                    "resources": ("Расход ресурсов", "Деньги и расходы по задачам"),
                    "published": ("Публикации", "Посты, отправленные в канал"),
                    "sources": ("Источники", "Подключённые новостные ленты"),
                    "edition": ("Редакция", "Подготовки, исходники и проверки"),
                    "pipeline": ("Материалы", "Что получено, где находится и что будет дальше"),
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
                if parsed.path == "/api/vacancies":
                    from .vacancy_view import inbox
                    db = self._read_db()
                    try: self._json(inbox(db))
                    finally: db.close()
                elif parsed.path == "/api/topic-registry":
                    from .topic_registry import report
                    db = self._read_db()
                    try: self._json(report(db))
                    finally: db.close()
                elif parsed.path == "/api/editorial-registry":
                    self._json({'error':'Редактор удалён'},410)
                elif parsed.path == "/api/policy":
                    self._json({'error':'Редактор удалён'},410)
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
                elif parsed.path in ('/api/edition','/api/edition/job','/api/edition/rules'):
                    from .edition.views import overview,details
                    from .edition.model import bundle
                    db=self._read_db()
                    try:
                        if parsed.path.endswith('/job'):
                            self._json(details(db,parse_qs(parsed.query).get('job_id',[''])[0]))
                        elif parsed.path.endswith('/rules'):
                            policy,refs,digest=bundle();self._json({'policy':policy,'references':refs,'bundle_hash':digest})
                        else:self._json(overview(db,config))
                    except ValueError as exc:self._json({'error':str(exc)},400)
                    finally:db.close()
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
                    from .cabinet_pipeline import pipeline_snapshot, cabinet_posts
                    db = self._read_db()
                    try: self._json(pipeline_snapshot(db, config, parse_qs(parsed.query), cabinet_posts(db, config)))
                    finally: db.close()
                elif parsed.path == "/api/regulatory":
                    self._json({'error':'Редактор удалён'},410)
                elif parsed.path == "/api/news":
                    self._json(self._news(parse_qs(parsed.query)))
                elif parsed.path == "/api/posts":
                    self._json(self._posts())
                elif parsed.path == "/api/corrections":
                    self._json({'error':'Редактор удалён'},410)
                elif parsed.path == "/api/analysis-drafts":
                    self._json({'error':'Редактор удалён'},410)
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
            db=self._read_db()
            try:
                return {'editorial':'v2','auto_publish_enabled':config.get('editorial',{}).get('enabled') is True,'agent_enabled':agent_enabled(config),
                        'materials':db.execute('SELECT COUNT(*) FROM items').fetchone()[0],
                        'published_total':db.execute("SELECT COUNT(*) FROM posts WHERE status='PUBLISHED'").fetchone()[0],
                        'source_total':db.execute('SELECT COUNT(*) FROM sources WHERE active=1').fetchone()[0]}
            finally: db.close()

        def _news(self, query):
            db=self._read_db()
            try:
                return [dict(r) for r in db.execute('SELECT i.item_id,i.title,i.url,i.content,i.disposition,i.discovered_at,s.name AS source_name FROM items i JOIN sources s USING(source_id) ORDER BY i.item_id DESC LIMIT 200')]
            finally: db.close()


        def _posts(self, all_rows=False):
            db=self._read_db()
            try:
                from .cli import _telegram_message_url
                rows=db.execute("SELECT post_id,COALESCE((SELECT plain_text FROM edition_documents d WHERE d.post_id=posts.post_id),text) AS text,status,external_id,published_at FROM posts WHERE status='PUBLISHED' ORDER BY published_at DESC LIMIT 200").fetchall()
                from .post_metrics import for_posts
                metrics = for_posts(db, [r['post_id'] for r in rows])
                return [{**dict(r),'ai_metrics':metrics[r['post_id']], 'headline':r['text'].splitlines()[0] if r['text'] else '',
                         'telegram_url':_telegram_message_url(config,r['external_id']) if r['external_id'] else None} for r in rows]
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
                if parts==['api','edition','retry']:
                    from .edition.views import retry
                    from .cli import load_config
                    payload=json.loads(self.rfile.read(size).decode('utf-8'))
                    current=load_config(str(config_file))
                    if current['newsroom']['database']!=db_path:
                        self._json({'error':'Настройки изменились; перезапустите кабинет'},409);return
                    db=self._db()
                    try:self._json(retry(db,current,str(payload.get('job_id') or '')),202)
                    except ValueError as exc:self._json({'error':str(exc)},409)
                    finally:db.close()
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
                    self._json({'error':'Редактор удалён'},410)
                    return
                if parts == ['api', 'policy', 'clarify']:
                    self._json({'error':'Редактор удалён'},410)
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
                    self._json({'error':'Редактор удалён'},410)
                    return
                if parts==["api","post-corrections"]:
                    self._json({'error':'Редактор удалён'},410)
                    return
                if parts==["api","editorial-feedback"]:
                    self._json({'error':'Редактор удалён'},410)
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
                    self._json({'error':'Редактор удалён'},410)
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
