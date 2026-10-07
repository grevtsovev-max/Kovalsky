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
from .quality import publication_source_ready


def _queue_post_correction(db, config: dict, post_id: int | None, reason: str,
                           telegram_message_id: str | None = None,
                           reference_message_id: int | None = None) -> tuple[dict, int]:
    """Queue a source-checked edit of an existing published post through the backend."""
    reason = str(reason or "").strip()
    if len(reason) < 5 or len(reason) > 2000:
        return {"error": "Комментарий должен содержать от 5 до 2000 символов"}, 400
    if post_id is not None and telegram_message_id is not None:
        post = db.execute(
            "SELECT p.post_id,p.story_id,p.origin_item_id,p.text,p.status,p.external_id,s.headline "
            "FROM posts p JOIN stories s USING(story_id) WHERE p.post_id=? AND p.external_id=?",
            (post_id, str(telegram_message_id)),
        ).fetchone()
    elif telegram_message_id is not None:
        post = db.execute(
            "SELECT p.post_id,p.story_id,p.origin_item_id,p.text,p.status,p.external_id,s.headline "
            "FROM posts p JOIN stories s USING(story_id) WHERE p.external_id=? AND p.status='PUBLISHED' "
            "ORDER BY p.post_id DESC LIMIT 1",
            (str(telegram_message_id),),
        ).fetchone()
    else:
        post = db.execute(
            "SELECT p.post_id,p.story_id,p.origin_item_id,p.text,p.status,p.external_id,s.headline "
            "FROM posts p JOIN stories s USING(story_id) WHERE p.post_id=?",
            (post_id,),
        ).fetchone() if post_id is not None else None
    if not post:
        return {"error": "Пост не найден"}, 404
    if post["status"] != "PUBLISHED" or not post["external_id"]:
        return {"error": "Исправлять можно только подтверждённую публикацию"}, 409
    if reference_message_id is not None:
        reference = db.execute(
            "SELECT post_id FROM posts WHERE external_id=? AND status='PUBLISHED' LIMIT 1",
            (str(reference_message_id),),
        ).fetchone()
        if not reference:
            return {"error": "Подробная публикация для ссылки не найдена"}, 404
        if int(reference["post_id"]) == int(post["post_id"]):
            return {"error": "Нельзя ссылаться на тот же пост"}, 400
    owners = config.get("telegram", {}).get("interest_owner_user_ids") or []
    if not owners:
        return {"error": "Не настроен получатель результата редакторской проверки"}, 503
    owner_chat_id = str(owners[0])
    feedback_reason = reason
    if reference_message_id is not None:
        feedback_reason += f"\n[BACKEND_REFERENCE_MESSAGE_ID:{int(reference_message_id)}]"
    active = db.execute(
        "SELECT c.correction_id,c.status,f.reason FROM telegram_feedback_corrections c "
        "JOIN editorial_feedback f USING(feedback_id) "
        "WHERE c.post_id=? AND c.status IN ('QUEUED','PROCESSING') ORDER BY c.correction_id DESC LIMIT 1",
        (post["post_id"],),
    ).fetchone()
    if active:
        if active["reason"] == feedback_reason:
            return {"ok": True, "queued": True, "reused": True,
                    "correction_id": active["correction_id"], "status": active["status"]}, 200
        return {"error": "Для этого поста уже выполняется другая правка",
                "correction_id": active["correction_id"], "status": active["status"]}, 409
    unresolved = db.execute(
        "SELECT status FROM telegram_message_edit_intents WHERE post_id=? "
        "AND status IN ('SENDING','UNKNOWN') LIMIT 1",
        (post["post_id"],),
    ).fetchone()
    if unresolved:
        return {"error": "Предыдущая правка не подтверждена; новую не ставил"}, 409
    latest_edit = db.execute(
        "SELECT edited_text FROM telegram_post_edits WHERE post_id=? "
        "ORDER BY captured_at DESC,ABS(update_id) DESC LIMIT 1",
        (post["post_id"],),
    ).fetchone()
    post_text = latest_edit["edited_text"] if latest_edit else post["text"]
    item = db.execute("SELECT title FROM items WHERE item_id=?",
                      (post["origin_item_id"],)).fetchone() if post["origin_item_id"] else None
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    feedback = db.execute(
        "INSERT INTO editorial_feedback(created_at,item_id,story_id,post_id,feedback_type,reason,item_title,post_text) "
        "VALUES(?,?,?,?,?,?,?,?)",
        (now, post["origin_item_id"], post["story_id"], post["post_id"], "TELEGRAM_EDIT",
         feedback_reason, (item["title"] if item else post["headline"] or "")[:1000], post_text[:5000]),
    )
    correction = db.execute(
        "INSERT INTO telegram_feedback_corrections(feedback_id,post_id,owner_chat_id,created_at,updated_at) "
        "VALUES(?,?,?,?,?)",
        (feedback.lastrowid, post["post_id"], owner_chat_id, now, now),
    )
    db.commit()
    return {"ok": True, "queued": True, "reused": False,
            "correction_id": correction.lastrowid, "status": "QUEUED"}, 202

PAGE = r'''<!doctype html>
<html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Kovalsky · Newsroom</title><style>
:root{--bg:#f4f6fa;--surface:#fff;--ink:#172236;--muted:#718096;--line:#e5eaf1;--blue:#365cf5;--blue2:#eaf0ff;--green:#158566;--amber:#a46a00;--red:#be424c;--shadow:0 8px 24px #2538580a}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:14px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}button,input{font:inherit}button{cursor:pointer}.app{display:grid;grid-template-columns:232px minmax(0,1fr);min-height:100vh}.side{background:#fff;border-right:1px solid var(--line);padding:24px 14px;display:flex;flex-direction:column}.brand{padding:0 12px 27px;font-size:18px;font-weight:750;letter-spacing:-.4px}.brand span{display:block;color:var(--muted);font-size:11px;font-weight:500;letter-spacing:.08em;text-transform:uppercase;margin-top:3px}.nav{display:grid;gap:5px}.nav a{display:block;border:0;background:transparent;color:#526075;text-align:left;text-decoration:none;padding:11px 12px;border-radius:10px;font-weight:600}.nav a.active,.nav a:hover{background:var(--blue2);color:var(--blue)}.side-foot{margin-top:auto;padding:14px 12px;color:var(--muted);font-size:12px}.dot{display:inline-block;width:8px;height:8px;border-radius:50%;background:#20a677;margin-right:7px}.main{padding:28px clamp(18px,4vw,54px);max-width:1600px;width:100%;margin:0 auto;min-width:0}.top{display:flex;align-items:flex-start;justify-content:space-between;gap:20px;margin-bottom:26px}.eyebrow{color:var(--muted);font-size:12px;margin-bottom:5px}.top h1{font-size:28px;line-height:1.2;margin:0;letter-spacing:-.7px}.sub{margin-top:7px;color:var(--muted)}.refresh{border:1px solid var(--line);background:#fff;padding:9px 13px;border-radius:9px;color:#39465a;font-weight:600}.refresh:hover{border-color:#b9c6dc}.cards{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:14px;margin-bottom:22px}.metric,.panel,.row-card,.table-wrap{background:var(--surface);border:1px solid var(--line);border-radius:13px;box-shadow:var(--shadow)}.metric{padding:17px 18px}.metric label{display:block;color:var(--muted);font-size:12px;font-weight:600}.metric strong{display:block;font-size:26px;margin-top:7px;letter-spacing:-.5px}.metric small{display:block;color:var(--muted);margin-top:2px}.grid{display:grid;grid-template-columns:minmax(0,1.6fr) minmax(280px,.8fr);gap:16px}.grid>*{min-width:0}.panel{padding:19px 20px;margin-bottom:16px;min-width:0}.panel-head{display:flex;align-items:center;justify-content:space-between;gap:12px;margin-bottom:14px}.head-actions{display:flex;align-items:center;gap:10px}.panel h2{font-size:15px;margin:0}.text-btn{border:0;background:none;color:var(--blue);font-weight:650}.row-list{display:grid;gap:9px}.row-card{padding:14px 15px;box-shadow:none;min-width:0;overflow-wrap:anywhere}.row-title{font-weight:700;color:var(--ink);text-decoration:none;overflow-wrap:anywhere}.row-title:hover{color:var(--blue)}.meta{display:flex;flex-wrap:wrap;gap:7px 12px;color:var(--muted);font-size:12px;margin-top:7px}.pill{display:inline-flex;align-items:center;border-radius:99px;padding:3px 8px;background:#f0f3f7;color:#59677a;font-size:11px;font-weight:700}.pill.green{background:#e6f6ef;color:var(--green)}.pill.amber{background:#fff3d9;color:var(--amber)}.pill.red{background:#fdebed;color:var(--red)}.excerpt{color:#526075;margin-top:10px;overflow-wrap:anywhere}.queue-reason{margin-top:10px;padding:9px 11px;border-radius:8px;background:#fff7e5;color:#7b560f;font-size:13px}.queue-reason.ready{background:#eaf7f0;color:#17664f}.post-copy{line-height:1.65;overflow-wrap:anywhere;word-break:normal}.post-copy p{margin:0 0 12px}.post-copy p:last-child{margin-bottom:0}.post-copy strong{color:var(--ink);font-weight:700}.post-copy a{color:var(--blue);text-decoration:underline;text-underline-offset:2px}.post-details{margin:12px 0 2px;border:1px solid var(--line);border-radius:10px;background:#fafbfd}.post-details summary{padding:10px 12px;color:#39465a;font-weight:700;cursor:pointer;list-style:none}.post-details summary::-webkit-details-marker{display:none}.post-details summary:after{content:"＋";float:right;color:var(--muted);font-weight:500}.post-details[open] > summary:after{content:"−"}.post-details-body{padding:0 12px 12px}.post-details-body p{position:relative;padding-left:17px;margin:0 0 9px}.post-details-body p:before{content:"➠";position:absolute;left:0;color:var(--blue)}.post-details-body p:last-child{margin-bottom:0}.actions{display:flex;flex-wrap:wrap;gap:8px;margin-top:12px;min-width:0}.actions .btn{min-width:0;white-space:normal;overflow-wrap:anywhere}.btn{border:1px solid var(--line);background:#fff;border-radius:8px;padding:7px 11px;font-weight:650;color:#3e4b5e}.btn.primary{border-color:var(--blue);background:var(--blue);color:#fff}.btn.danger{color:var(--red)}.btn:disabled{opacity:.55;cursor:wait}.toolbar{display:flex;gap:9px;flex-wrap:wrap;margin-bottom:14px}.editorial-feedback{margin-top:12px;border-top:1px solid var(--line);padding-top:10px}.editorial-feedback summary{color:var(--blue);font-weight:650;cursor:pointer}.editorial-feedback form{display:grid;gap:9px;margin-top:10px;max-width:600px}.editorial-feedback label{display:grid;gap:5px;color:#526075;font-size:12px;font-weight:650}.editorial-feedback select,.editorial-feedback textarea{width:100%;border:1px solid var(--line);border-radius:8px;padding:8px 10px;color:var(--ink);font:inherit}.editorial-feedback textarea{resize:vertical}.toolbar input,.toolbar select{background:#fff;border:1px solid var(--line);border-radius:9px;padding:9px 11px;color:var(--ink)}.toolbar input{min-width:220px;flex:1}.toolbar label{display:flex;align-items:center;gap:6px;padding:0 8px;background:#fff;border:1px solid var(--line);border-radius:9px;color:var(--muted);font-size:12px;font-weight:650}.toolbar label select{border:0;padding:9px 3px;color:var(--ink);outline:none;background:transparent}.table-wrap{overflow:auto}.table{width:100%;border-collapse:collapse;min-width:650px}.table th,.table td{text-align:left;padding:12px 14px;border-bottom:1px solid var(--line);vertical-align:top}.table th{color:var(--muted);font-size:11px;text-transform:uppercase;letter-spacing:.06em;background:#fafbfd}.table tr:last-child td{border-bottom:0}.error{color:var(--red)}.muted{color:var(--muted)}.empty{padding:28px;text-align:center;color:var(--muted);background:#fff;border:1px dashed var(--line);border-radius:12px}.notice{position:fixed;right:22px;bottom:22px;background:#172236;color:#fff;padding:12px 16px;border-radius:10px;box-shadow:var(--shadow);display:none;max-width:360px}.view{display:none}.view.active{display:block}.line-chart{height:6px;border-radius:8px;background:#edf1f6;overflow:hidden;margin-top:12px}.line-chart i{height:100%;display:block;background:var(--blue);border-radius:8px}.source-title{font-weight:650}.source-groups{display:grid;gap:16px}.source-group{background:#fff;border:1px solid var(--line);border-radius:12px;overflow:hidden}.source-group-head{display:flex;justify-content:space-between;align-items:center;padding:13px 15px;background:#fafbfd;border-bottom:1px solid var(--line)}.source-group-head h3{font-size:14px;margin:0}.source-group .table-wrap{border:0;border-radius:0;box-shadow:none}.source-group .table-wrap{overflow-x:hidden}.source-group .table{table-layout:fixed;min-width:0}.source-group th:first-child,.source-group td:first-child{width:38%}.source-group th:nth-child(2),.source-group td:nth-child(2){width:20%}.source-group th:nth-child(3),.source-group td:nth-child(3){width:20%}.source-group th:nth-child(4),.source-group td:nth-child(4){width:22%}.source-group td:first-child .muted{overflow-wrap:anywhere;word-break:break-word;line-break:anywhere}.source-add{margin-bottom:14px;background:#fff;border:1px solid var(--line);border-radius:12px}.source-add summary{padding:13px 16px;color:var(--blue);font-weight:700;cursor:pointer;list-style:none}.source-add summary::-webkit-details-marker{display:none}.source-add-form{padding:0 16px 16px;display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px}.source-add-form label{display:grid;gap:5px;color:var(--muted);font-size:12px;font-weight:650}.source-add-form input,.source-add-form select{width:100%;padding:9px 10px;background:#fff;border:1px solid var(--line);border-radius:8px;color:var(--ink)}.source-add-form .wide{grid-column:1/-1}.source-add-form .form-actions{display:flex;justify-content:flex-end;align-items:center;gap:10px}.source-inactive-toggle{display:flex;align-items:center;gap:7px;margin:4px 0 14px;color:var(--muted);font-size:12px}.source-inactive-toggle input{margin:0}.source-title{font-weight:650}.modal{position:fixed;inset:0;background:#17223670;display:none;align-items:center;justify-content:center;padding:20px}.modal.open{display:flex}.modal-box{background:#fff;border-radius:15px;max-width:620px;width:100%;padding:24px}.modal-box h2{margin:0 0 12px}.modal-box pre{white-space:pre-wrap;max-height:55vh;overflow:auto;font:14px/1.5 -apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif}.modal-close{float:right;border:0;background:none;font-size:20px;color:var(--muted)}
@media(max-width:900px){.app{grid-template-columns:minmax(0,1fr)}.side{position:sticky;top:0;z-index:4;border-right:0;border-bottom:1px solid var(--line);padding:10px 14px}.brand{padding:4px 8px 10px}.brand span{display:none}.nav{display:flex;overflow:auto}.nav a{white-space:nowrap;padding:9px 11px}.side-foot{display:none}.main{padding-top:20px}.grid{grid-template-columns:1fr}.cards{grid-template-columns:repeat(2,minmax(0,1fr))}}
@media(max-width:520px){.top h1{font-size:23px}.cards{gap:8px}.metric{padding:13px}.metric strong{font-size:22px}.main{padding-left:14px;padding-right:14px}.panel{padding:15px}.source-add-form{grid-template-columns:1fr}.source-add-form .wide{grid-column:auto}.source-add-form .form-actions{justify-content:space-between}.head-actions{flex-wrap:wrap;justify-content:flex-end}}
.nav-more{margin-top:8px;border-top:1px solid var(--line);padding:9px 8px 0}.nav-more summary{list-style:none;cursor:pointer;color:#718096;font-size:12px;font-weight:700;padding:6px 4px}.nav-more summary::-webkit-details-marker{display:none}.nav-more summary:after{content:'＋';float:right}.nav-more[open] summary:after{content:'−'}.nav-more a{display:block;width:100%;font-size:13px;padding:9px 10px}.home-collection{padding:22px}.home-collection .panel-head{margin-bottom:16px}.collection-status{padding:10px 12px;margin:4px 0 10px}.home-source-health{font-size:12px;color:var(--muted);border-top:1px solid var(--line);padding-top:13px}.latency-advice{margin:10px 0 0}.home-links{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:12px}.home-link{display:flex;align-items:center;gap:12px;text-align:left;background:#fff;border:1px solid var(--line);border-radius:12px;padding:17px;color:var(--ink)}.home-link:hover{border-color:#b8c7ff;box-shadow:var(--shadow)}.home-link-icon{display:grid;place-items:center;flex:0 0 38px;height:38px;border-radius:10px;background:var(--blue2);color:var(--blue);font-size:20px}.home-link b,.home-link small{display:block}.home-link small{margin-top:4px;color:var(--muted);font-size:11px}.home-link-arrow{margin-left:auto;color:var(--blue);font-size:18px}.news-help{font-size:12px;margin:-3px 0 14px}.news-toolbar{align-items:center}.news-toolbar>label{min-height:39px}@media(max-width:700px){.home-links{grid-template-columns:1fr}.home-link{padding:13px}.nav-more{margin:0;padding:0;border:0;flex:0 0 auto}.nav-more summary{padding:9px 11px;white-space:nowrap}.nav-more[open]{position:absolute;top:52px;right:8px;background:#fff;border:1px solid var(--line);border-radius:10px;padding:8px;box-shadow:var(--shadow);z-index:5}.nav-more[open] summary{display:none}.nav-more[open] a{white-space:nowrap}}

/* Cabinet workspace */
:root{--bg:#eef2f1;--surface:#fff;--ink:#17332f;--muted:#617570;--line:#dce5e1;--blue:#087f73;--blue2:#e0f3ec;--shadow:0 6px 24px #17332f06}
body{font-size:14px;line-height:1.55}.app{grid-template-columns:244px minmax(0,1fr)}.side{background:#142f2b;color:#fff;padding:32px 18px;position:sticky;top:0;height:100vh}.brand{font-size:25px;letter-spacing:-1px}.brand span{color:#96b7ae;letter-spacing:.16em}.nav a{color:#bdd2cb;border-radius:8px;padding:12px 14px}.nav a.active,.nav a:hover{background:#275047;color:#fff}.nav-more{border-color:#36514a}.nav-more summary,.side-foot,.side-foot .muted{color:#96b7ae}.main{padding:36px clamp(18px,3vw,48px);max-width:1680px}.top{margin-bottom:30px}.top h1{font-size:34px;letter-spacing:-1.1px}.eyebrow{font-size:11px;text-transform:uppercase;letter-spacing:.1em}.panel,.metric,.row-card{border-radius:16px}.panel{padding:24px}.panel h2{font-size:18px}.metric{padding:22px}.metric strong{font-size:32px}.row-card{padding:22px}.row-title{font-size:16px}.btn,.refresh{padding:10px 14px;border-radius:9px}.btn:hover,.refresh:hover{border-color:var(--blue);background:var(--blue2)}.btn.primary:hover{background:#06685e;color:white}.queue-reason{line-height:1.65}.toolbar{gap:12px}.toolbar input{min-width:180px}.nav a:focus-visible,button:focus-visible,input:focus-visible,select:focus-visible,summary:focus-visible,a:focus-visible{outline:3px solid #46bca1;outline-offset:3px}.home-links{gap:14px}.home-link{border-radius:14px}.flow-guide{margin-bottom:20px}.flow-guide p{color:var(--muted);margin:8px 0 16px}.flow-chain{display:grid;grid-template-columns:repeat(7,minmax(0,1fr));gap:8px;list-style:none;padding:0;margin:16px 0}.flow-step{border:1px solid var(--line);border-radius:10px;padding:12px 10px;background:#f7f9f8;min-width:0}.flow-step b{display:block;font-size:12px}.flow-step small{display:block;color:var(--muted);font-size:11px;margin-top:5px}.flow-step .step-number{display:inline-grid;place-items:center;width:23px;height:23px;border-radius:50%;background:#e5ebe8;margin-bottom:8px;font-size:11px;font-weight:750}.flow-step.done{border-color:#a9d5c3;background:#edf8f2}.flow-step.current{border:2px solid var(--blue);background:var(--blue2)}.flow-step.failed{border-color:#e5aaa8;background:#fff0ee}.flow-step.current .step-number{background:var(--blue);color:#fff}.flow-facts{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:12px;margin:16px 0}.flow-fact{padding:14px;background:#f5f8f6;border-radius:10px;overflow-wrap:anywhere}.flow-fact label{display:block;font-size:11px;font-weight:750;text-transform:uppercase;letter-spacing:.06em;color:var(--muted);margin-bottom:6px}.flow-fact.problem{background:#fff4e5}.flow-fact strong{font-size:13px}.flow-legend{display:flex;gap:16px;flex-wrap:wrap;font-size:12px;color:var(--muted)}.news-pagination{display:flex;gap:12px;align-items:center;flex-wrap:wrap;padding:18px 0;color:var(--muted)}
@media(max-width:1100px){.flow-chain{grid-template-columns:repeat(4,minmax(0,1fr))}}
@media(max-width:900px){.app{grid-template-columns:minmax(0,1fr)}.side{height:auto;padding:12px;position:sticky}.brand{padding-bottom:8px;font-size:20px}.nav-more[open]{background:#142f2b}.main{padding-top:24px}.flow-facts{grid-template-columns:1fr}.top h1{font-size:28px}}
@media(max-width:520px){.flow-chain{grid-template-columns:repeat(2,minmax(0,1fr))}.panel,.row-card{padding:16px}.top{gap:10px}.top .refresh{font-size:12px}.toolbar>input{width:100%}}

/* One material workspace: compact progress above a readable work list. */
.side{min-width:0}.agent-state{display:flex;gap:10px 20px;align-items:center;flex-wrap:wrap;font-size:12px;color:var(--muted);margin:0 0 18px}.agent-state .pill{font-size:12px}
.material-path{padding:18px 20px;margin-bottom:20px}.material-path h2{font-size:16px}.material-path .panel-head{margin-bottom:14px}.period-control{display:flex;align-items:center;gap:8px;color:var(--muted);font-size:12px}.period-control select{font:inherit;color:var(--ink);border:1px solid var(--line);border-radius:8px;padding:7px;background:#fff}
.path-strip{display:grid;grid-template-columns:repeat(7,minmax(0,1fr));gap:0}.path-step{position:relative;text-align:left;border:0;border-right:1px solid var(--line);padding:8px 14px;background:transparent;color:var(--ink);border-radius:6px;min-width:0}.path-step:first-child{padding-left:0}.path-step:last-child{border-right:0}.path-step span{display:block;color:var(--muted);font-size:11px;line-height:1.5}.path-step strong{display:block;font-size:22px;font-weight:650;margin-top:4px;font-variant-numeric:tabular-nums}.path-step:hover,.path-step[aria-pressed=true]{background:var(--blue2)}.path-help{font-size:11px;color:var(--muted);margin-top:10px}.path-help summary{cursor:pointer}.path-caption{font-size:11px;color:var(--muted);margin:12px 0 0}
.material-tabs{display:flex;gap:6px;flex-wrap:wrap;margin-bottom:16px;border-bottom:1px solid var(--line);padding-bottom:10px}.material-tabs button{border:0;background:transparent;color:var(--muted);padding:9px 12px;border-radius:8px;font-weight:600}.material-tabs button.active{background:var(--blue2);color:var(--blue)}.material-tabs button:hover{background:#e7eeea}.material-toolbar{margin-bottom:10px}.selection-note{display:flex;gap:10px;align-items:center;flex-wrap:wrap;min-height:28px;margin-bottom:12px;color:var(--muted);font-size:12px}.selection-note small{width:100%}.material-card{padding:18px 20px}.material-heading{display:flex;align-items:flex-start;justify-content:space-between;gap:18px}.material-heading .pill{flex-shrink:0;max-width:40%;white-space:normal}.material-summary{margin:14px 0 10px;font-size:13px}.material-summary>div{display:grid;grid-template-columns:85px minmax(0,1fr);gap:10px;margin-top:6px}.material-summary dt{color:var(--muted)}.material-summary dd{margin:0}.material-timing{font-size:11px;color:var(--muted)}.material-details{border-top:1px solid var(--line);margin-top:12px;padding-top:10px;font-size:13px}.material-details>summary,.cabinet-extra>summary{cursor:pointer;font-weight:600;color:var(--blue)}.material-card .editorial-feedback{border:0;margin-top:7px;padding:0;font-size:12px}.cabinet-extra{padding:15px 20px}.cabinet-extra[open]>summary{margin-bottom:14px}.material-card .flow-chain{margin-top:14px}.main{max-width:1480px}.top{margin-bottom:20px}.top h1{font-size:30px}
@media(max-width:1100px){.path-strip{grid-template-columns:repeat(4,minmax(0,1fr));gap:8px}.path-step{border:0;background:#f5f8f6;padding:8px 10px}.path-step:first-child{padding-left:10px}}
@media(max-width:600px){.path-strip{grid-template-columns:repeat(7,minmax(112px,1fr));overflow-x:auto;padding-bottom:7px}.path-step:last-child{grid-column:auto}.path-step strong{font-size:20px}.material-path .panel-head{align-items:flex-start;flex-direction:column}.material-heading{display:block}.material-heading .pill{max-width:100%;margin-top:8px}.material-tabs{gap:3px}.material-tabs button{font-size:12px;padding:8px}.material-summary>div{grid-template-columns:65px minmax(0,1fr)}.material-card{padding:16px}.material-toolbar label{width:100%}.top h1{font-size:25px}}
</style></head><body>
<div class="app"><aside class="side"><div class="brand">Kovalsky<span>Newsroom</span></div><nav class="nav"><a class="active" href="/?view=pipeline" data-view="pipeline">Материалы</a><a href="/?view=published" data-view="published">Публикации</a><a href="/?view=sources" data-view="sources">Источники</a><a href="/?view=policy" data-view="policy">Правила <span id="policy-question-count"></span></a><a href="/?view=resources" data-view="resources">Расходы</a><details class="nav-more"><summary>Дополнительно</summary><a href="/?view=corrections" data-view="corrections">Правки постов</a><a href="/?view=regulatory" data-view="regulatory">Нормативные документы</a><a href="/?view=analysis" data-view="analysis">Аналитика</a></details></nav><div class="side-foot">Кабинет редакции<br><span class="muted">Данные обновляются автоматически</span></div></aside>
<main class="main"><header class="top"><div><div class="eyebrow">Редакция · <span id="updated">загрузка…</span></div><h1 id="heading">Материалы</h1><div class="sub" id="subtitle">Что получено, где находится и что будет дальше</div></div><button class="refresh" onclick="reloadAll()">↻ Обновить</button></header>



<section id="view-corrections" class="view"><div class="panel"><div class="panel-head"><div><h2>Статус правок</h2><div class="muted">Состояние проверок на сервере обновляется автоматически.</div></div><button class="btn" type="button" onclick="loadCorrections()">↻ Обновить</button></div><div id="correction-status-list" class="row-list"><div class="muted">Загрузка статусов…</div></div></div></section>
<section id="view-published" class="view"><div class="toolbar"><input id="published-search" placeholder="Поиск по опубликованным постам…" oninput="renderPublished()"></div><div id="published-list" class="row-list"></div></section>
<section id="view-policy" class="view"><div class="panel"><h2>Действующие правила</h2><p id="policy-version" class="muted"></p><div id="policy-questions"></div><div id="policy-history"></div><div id="policy-amendments"></div><pre id="policy-document" style="white-space:pre-wrap;font:inherit;line-height:1.6"></pre></div></section>
<section id="view-resources" class="view"><div class="panel"><div class="panel-head"><div><h2>Сколько потрачено и на что</h2><div class="muted">Сумма OpenAI и оценка расходов по задачам Kovalsky.</div></div><button id="billing-sync" class="btn" onclick="syncBilling()">Сверить с OpenAI</button></div><div class="toolbar"><label>Период<select id="spending-period" onchange="loadSpending()"><option value="today">Сегодня</option><option value="week">Последние 7 дней</option><option value="month" selected>Этот месяц</option><option value="custom">Свои даты</option></select></label><label id="spending-start-label" hidden>С<input id="spending-start" type="date" onchange="loadSpending()"></label><label id="spending-end-label" hidden>По<input id="spending-end" type="date" onchange="loadSpending()"></label></div><div id="spending-dates" class="muted" style="margin-bottom:14px"></div><div id="spending-summary" class="cards" style="grid-template-columns:repeat(2,minmax(0,1fr))"></div><div id="spending-explanation" class="queue-reason"></div><h3>На какие задачи ушли деньги</h3><div id="spending-tasks" class="table-wrap"></div><div id="spending-provider" style="margin-top:18px"></div><div id="spending-connection" class="muted" style="margin-top:16px"></div><details style="margin-top:18px"><summary style="cursor:pointer;font-weight:650">Внести сумму из кабинета OpenAI</summary><p class="muted">Если автосверка не подключена, можно сохранить сумму вручную. Даты и область расходов нужны для сравнения. Без них сумма показывается отдельно.</p><form id="billing-report-form" class="source-add-form"><label>Потрачено, USD<input id="billing-amount" type="number" min="0" step="0.01" required></label><label>Где потрачено<select id="billing-scope"><option value="unknown">Пока не уточнено</option><option value="account">Весь аккаунт OpenAI</option><option value="kovalsky">Только проект Kovalsky</option></select></label><label>Начало периода<input id="billing-start" type="date"></label><label>Конец периода<input id="billing-end" type="date"></label><div class="form-actions wide"><span class="muted">Значение сохраняется как введённое владельцем.</span><button class="btn primary" type="submit">Сохранить сумму</button></div></form><div id="billing-report-result" class="muted"></div></details></div><details class="panel"><summary style="cursor:pointer;font-weight:700">Подробный журнал: запросы, токены и время</summary><div class="panel"><div class="panel-head"><div><h2>На что уходят ресурсы</h2><div class="muted">Все обращения к модели, включая отбор, ошибки и повторы. Суммы с неполными данными показаны отдельно.</div></div><button class="btn" onclick="loadResources()">↻ Обновить</button></div><div class="toolbar"><label>Период<select id="resource-period" onchange="loadResources()"><option value="1">Час</option><option value="24" selected>Сутки</option><option value="168">Неделя</option></select></label><label>Материал<input id="resource-item" type="number" min="1" placeholder="Все материалы" onchange="loadResources()"></label></div><div id="resource-summary" class="cards"></div><div id="resource-coverage" class="queue-reason"></div><h3>По этапам</h3><div id="resource-stages" class="table-wrap"></div><h3>По часам · Москва</h3><div id="resource-hours" class="table-wrap"></div><h3>По ролям · включая прежний журнал</h3><div id="resource-roles" class="table-wrap"></div><h3>По моделям</h3><div id="resource-models" class="table-wrap"></div><h3>Свежие материалы, повторы и фоновые задачи</h3><div id="resource-categories" class="table-wrap"></div><h3>Наиболее затратные по времени источники</h3><div id="resource-sources" class="table-wrap"></div><div id="resource-notes" class="muted" style="margin-top:18px;white-space:pre-line"></div></div></details></section>
<section id="view-pipeline" class="view active">
<div id="agent-state" class="agent-state" role="status">Проверяю состояние агента…</div>
<div id="account-alert" role="status"></div>
<div class="panel material-path">
<div class="panel-head"><h2>Путь материалов</h2><label class="period-control">Получены за <select id="pipeline-period" onchange="pipelineOffset=0;loadPipeline()"><option value="24">24 часа</option><option value="48">48 часов</option><option value="168">7 дней</option><option value="all" selected>всё время</option></select></label></div>
<div id="pipeline-funnel" class="path-strip" aria-label="Пройденные шаги"></div>
<p id="pipeline-counter-start" class="path-caption"></p><details class="path-help"><summary>Как считаются этапы</summary><p class="path-caption">Сколько материалов из выбранного периода прошло каждый шаг по сохранённым результатам. Нажмите на шаг, чтобы открыть список. Повторы не увеличивают числа. У старых материалов часть результатов могла не сохраняться.</p></details>
</div>
<div class="material-tabs" role="group" aria-label="Состояние материалов"><button class="active" data-bucket="work" aria-pressed="true" onclick="chooseBucket('work')">В работе</button><button data-bucket="attention" aria-pressed="false" onclick="chooseBucket('attention')">Требуют внимания</button><button data-bucket="published" aria-pressed="false" onclick="chooseBucket('published')">Опубликованы</button><button data-bucket="closed" aria-pressed="false" onclick="chooseBucket('closed')">Без публикации</button><button data-bucket="all" aria-pressed="false" onclick="chooseBucket('all')">Все</button></div>
<div class="toolbar material-toolbar"><input id="pipeline-search" aria-label="Поиск материалов" placeholder="Заголовок или источник…" oninput="schedulePipeline()"><label>Сейчас на этапе <select id="pipeline-stage" onchange="chooseCurrentStage()"><option value="all">Любом</option></select></label></div>
<div id="pipeline-selection" class="selection-note" role="status"></div>
<div id="pipeline-list" class="row-list"></div><div id="pipeline-pagination" class="news-pagination"></div>
<details class="panel cabinet-extra"><summary>Добавить материал по ссылке</summary><p class="muted">Статья пройдёт обычную обработку и автоматические проверки перед публикацией.</p><form id="manual-intake-form" class="source-add-form"><label class="wide">Ссылка на статью<input name="url" type="url" maxlength="2000" required placeholder="https://example.com/news/article"></label><div class="form-actions wide"><span class="muted">Принимаются общедоступные HTTPS-страницы с читаемым текстом.</span><button id="manual-intake-submit" class="btn primary" type="submit">Прочитать и проверить</button></div></form><div id="manual-intake-result" class="queue-reason" style="display:none"></div></details>
<details class="panel cabinet-extra"><summary>Состояние сбора и технические подробности</summary><div class="actions"><button id="force-collect-button" class="btn" type="button" onclick="forceCollect()">↻ Собрать сейчас</button></div><div id="collection-status" class="collection-status" style="display:none"></div><p id="home-source-health" class="muted"></p><div id="workload-status" style="white-space:pre-line"></div><div id="overview-errors"></div><div id="latency-advice" class="queue-reason" style="display:none"></div><div id="improvement-advice" class="queue-reason" style="display:none"></div></details>
</section>
<section id="view-regulatory" class="view"><div class="panel"><div class="panel-head"><h2>Нормативный мониторинг</h2><button class="btn" onclick="loadRegulatory()">Обновить</button></div><p class="muted">Предварительный разбор официальных материалов. Стадии и сроки требуют редакторской проверки. Поиск по индексу не гарантирует полноту охвата.</p><div id="reg-status"></div><div class="toolbar" style="margin-top:12px"><label>Показать<select id="reg-filter" onchange="loadRegulatory()"><option value="ready">Исследования</option><option value="queue">Очередь чтения</option><option value="all">Все материалы</option></select></label></div><details><summary>Охват источников</summary><div id="reg-sources"></div></details></div><div id="reg-list" class="row-list"></div></section>
<section id="view-analysis" class="view"><div class="panel"><div class="panel-head"><h2>Авторские аналитические черновики</h2><span class="muted">Только редакторская проверка · автопубликация выключена</span></div><div id="analysis-list" class="row-list"></div></div></section>

<section id="view-sources" class="view"><div id="registry-status" class="muted" style="margin-bottom:14px"></div><div id="topic-registry-status" class="muted" style="margin-bottom:14px"></div><div id="editorial-registry-status" class="muted" style="margin-bottom:14px"></div><details class="source-add"><summary>Обучение через таблицу — простое подключение</summary><div style="padding:0 16px 16px"><ol><li>В таблице тем откройте «Расширения → Apps Script».</li><li>Скопируйте код ниже и замените им содержимое редактора.</li><li>Слева «Сервисы → ＋ → Google Sheets API → Добавить». Сохраните код, выберите функцию setupKovalsky и нажмите «Выполнить». Разрешите доступ Google и сохраните код доступа из окна в таблице.</li><li>«Развернуть → Новое развертывание → Веб-приложение»: выполнять от вашего имени, доступ — «Все». Скопируйте адрес приложения.</li><li>Внесите адрес и код доступа ниже и нажмите «Подключить».</li></ol><button class="btn" onclick="copyGoogleScript()">Скопировать код скрипта</button><textarea id="google-script-code" readonly hidden style="width:100%;height:200px;margin-top:12px"></textarea><form id="google-script-form" class="source-add-form" style="margin-top:16px"><label class="wide">Адрес подключения<input name="url" type="url" required placeholder="https://script.google.com/macros/s/…/exec" autocomplete="off"></label><label class="wide">Код доступа из таблицы<input name="secret" type="password" required autocomplete="off"></label><div class="form-actions wide"><span class="muted">Подключение включится после проверки записи в таблицу.</span><button class="btn primary" type="submit">Подключить</button></div></form></div></details><details class="source-add"><summary>Подключить служебный аккаунт Google для обучения</summary><div style="padding:0 16px 16px"><p>Создайте служебный аккаунт Google, включите Google Sheets API и выдайте его email право редактора таблицы тем. Затем выберите скачанный JSON-ключ здесь. Ключ хранится в закрытом файле на сервере.</p><label class="btn">Выбрать JSON-ключ<input id="google-sheets-key" type="file" accept=".json,application/json" style="display:none" onchange="connectGoogleSheetsKey(this)"></label></div></details><div class="panel"><div class="panel-head"><h2>Источники</h2><span class="muted" id="source-updated"></span></div><div class="muted" style="margin:-6px 0 14px">Источники сгруппированы по типу. «Найдено» — все материалы за 24 часа; «Новые сюжеты» — новые темы и существенные обновления. Источники и объекты наблюдения задаются в общей таблице. Изменения применяются в следующем цикле сбора.</div><details class="source-add"><summary>＋ Добавить источник</summary><form id="source-add-form" class="source-add-form"><label>Название<input name="name" maxlength="100" required placeholder="Например, РБК Крипто"></label><label>Тип источника<select name="type"><option value="rss">RSS-лента</option><option value="web">Сайт с RSS</option><option value="telegram">Telegram-канал</option><option value="google_news">Google News</option></select></label><label class="wide">Адрес<input name="url" type="url" maxlength="2000" required placeholder="https://example.com/feed.xml"></label><label>Доверие к источнику<select name="reputation"><option value="unknown">Обычный источник</option><option value="reputable_media">Проверенное СМИ</option></select></label><div class="form-actions wide"><span class="muted">Для Telegram укажите публичный канал t.me</span><button class="btn primary" type="submit">Добавить</button></div></form></details><label class="source-inactive-toggle"><input id="show-inactive-sources" type="checkbox" checked onchange="renderSources()">Показать отключённые (<span id="inactive-source-count">0</span>)</label><div id="sources-table" class="source-groups"></div></div></section>


</main></div><div id="notice" class="notice"></div><script>
const TOKEN='__TOKEN__';let posts=[],sources=[],analysisDrafts=[],active='__INITIAL_VIEW__',collectionPoll=null,collectionWasRunning=false,pipelineOffset=0,pipelineRequest=0,pipelineBucket='work',pipelineMilestone='all',pipelineTimer=null;
const titles={policy:['Правила редакции','Единый стандарт и зарегистрированные уточнения владельца'],resources:['Расход ресурсов','Деньги и расходы по задачам'],pipeline:['Материалы','Что получено, где находится и что будет дальше'],regulatory:['Нормативные документы','Материалы регуляторов и ход рассмотрения'],published:['Публикации','Посты, отправленные в канал'],corrections:['Правки постов','Очередь серверных исправлений опубликованных постов'],analysis:['Аналитика','Авторские аналитические материалы'],sources:['Источники','Подключённые новостные ленты']};
function safeUrl(v){try{let u=new URL(v,location.origin);return ['http:','https:'].includes(u.protocol)?u.href:'#'}catch(e){return '#'}}function esc(v){return String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}function date(v){if(!v)return '—';let d=new Date(v);return isNaN(d)?v:d.toLocaleString('ru-RU',{day:'2-digit',month:'short',hour:'2-digit',minute:'2-digit',timeZone:'Europe/Moscow'})}function pill(text,cls=''){return `<span class="pill ${cls}">${esc(text)}</span>`}const dispositionLabels={NEW_STORY:'Новый сюжет: на допуске',UPDATE_CANDIDATE:'Обновление: на допуске',DUPLICATE:'Повтор сюжета',STORE_ONLY:'Сохранено в памяти; без поста',AGENT_CORRECTION_QUEUED:'Правка опубликованного поста проверяется',NOISE:'Не по теме',STALE:'Устарела',BASELINE_SKIPPED:'Пропущена при первом запуске',WAITING_CONFIRMATION:'Автоматическая перепроверка',AI_RETRY:'Повторный AI-разбор',PRIMARY_RETRY:'Повторное чтение источника',EDITOR_REJECTED:'Не рекомендована к публикации',REJECTED:'Отклонена после проверок',UNDATED:'Без даты публикации'};function dispositionLabel(v){return dispositionLabels[v]||'Статус не определён'}function showNotice(t){let n=document.getElementById('notice');n.textContent=t;n.style.display='block';setTimeout(()=>n.style.display='none',3500)}
function showView(name){if(['overview','news'].includes(name))name='pipeline';active=name;document.querySelectorAll('.view').forEach(x=>x.classList.remove('active'));document.getElementById('view-'+name).classList.add('active');document.querySelectorAll('.nav [data-view]').forEach(x=>x.classList.toggle('active',x.dataset.view===name));document.getElementById('heading').textContent=titles[name][0];document.getElementById('subtitle').textContent=titles[name][1];if(name==='regulatory'){history.replaceState(null,'','/?view=regulatory#regulatory');loadRegulatory()}else history.replaceState(null,'','/?view='+encodeURIComponent(name));if(name==='policy')loadPolicy();if(name==='resources')loadResources();if(name==='pipeline')loadPipeline();if(name==='published')renderPublished();if(name==='corrections')loadCorrections();if(name==='analysis')renderAnalysis();if(name==='sources')renderSources();}
document.querySelectorAll('.nav [data-view]').forEach(b=>b.onclick=e=>{e.preventDefault();showView(b.dataset.view)});document.getElementById('source-add-form').addEventListener('submit',addSource);document.getElementById('manual-intake-form').addEventListener('submit',submitManualIntake);
async function api(path,opts={}){let r=await fetch(path,{...opts,headers:{'Content-Type':'application/json','X-Dashboard-Token':TOKEN,...(opts.headers||{})}});let d=await r.json();if(!r.ok)throw Error(d.error||'Ошибка запроса');return d}
async function submitManualIntake(event){event.preventDefault();const form=event.currentTarget,button=document.getElementById('manual-intake-submit'),result=document.getElementById('manual-intake-result'),url=new FormData(form).get('url');button.disabled=true;button.textContent='Читаю и проверяю…';result.style.display='block';result.className='queue-reason';result.textContent='Проверяю страницу, дату, новизну и редакционные требования. Отправка возможна только после автоматического допуска.';try{const data=await api('/api/intake-url',{method:'POST',body:JSON.stringify({url})});const posts=data.posts||[],published=posts.find(p=>p.status==='PUBLISHED'),pending=posts.find(p=>p.status==='PENDING'),rejected=posts.find(p=>p.status==='REJECTED');let message;if(published){message='Пост прошёл автоматический допуск и опубликован в Telegram.';result.className='queue-reason ready'}else if(pending){message='Черновик создан и ожидает автоматической повторной проверки.'}else if(rejected){message='Материал обработан, но пост не прошёл автоматический допуск.'}else{const labels={DUPLICATE:'Повтор уже известного сюжета.',NOISE:'Материал исключён тематической проверкой.',STALE:'Материал старше допустимого окна свежести.',UNDATED:'У статьи не удалось установить дату.',BASELINE_SKIPPED:'Материал пропущен по сроку публикации.'};message=data.message||labels[data.outcome]||`Материал проверен. Результат: ${data.outcome||'пост не создан'}.`}result.textContent=message;form.reset();await reloadAll()}catch(e){result.textContent=e.message;result.className='queue-reason error'}finally{button.disabled=false;button.textContent='Прочитать и проверить'}}
function showCollectionStatus(message,tone=''){const box=document.getElementById('collection-status');box.textContent=message;box.className='queue-reason'+(tone==='ready'?' ready':'');box.style.display='block'}
async function refreshCollectionStatus(){try{const s=await api('/api/collection'),button=document.getElementById('force-collect-button');if(s.state==='running'){collectionWasRunning=true;button.disabled=true;button.textContent='Сбор идёт…';showCollectionStatus(`Сбор запущен ${date(s.started_at)}. Панель обновится после завершения.`);if(collectionPoll)clearTimeout(collectionPoll);collectionPoll=setTimeout(refreshCollectionStatus,2500);return}button.disabled=false;button.textContent='↻ Собрать сейчас';if(s.state==='error')showCollectionStatus(`Сбор завершился с ошибкой (${s.error||'ошибка цикла'}).`);else if(s.finished_at){const outcomes=Object.entries(s.outcomes||{}).map(([k,v])=>`${k}: ${v}`).join(' · ');showCollectionStatus(`Последний цикл завершён ${date(s.finished_at)}${outcomes?' · '+outcomes:' · новых материалов нет'}`,'ready')}else document.getElementById('collection-status').style.display='none';if(collectionWasRunning){collectionWasRunning=false;await reloadAll()}}catch(e){showCollectionStatus('Не удалось получить состояние сбора.');}}
async function forceCollect(){if(!confirm('Запустить полный цикл сбора сейчас? Если появятся материалы, подходящие под правила автопубликации, они могут быть отправлены в Telegram.'))return;const button=document.getElementById('force-collect-button');button.disabled=true;button.textContent='Запускаю…';try{await api('/api/collect',{method:'POST',body:'{}'});collectionWasRunning=true;showNotice('Полный цикл сбора запущен');await refreshCollectionStatus()}catch(e){button.disabled=false;button.textContent='↻ Собрать сейчас';showNotice(e.message);await refreshCollectionStatus()}}
function feedbackBox(itemId,postId=''){let correctionOption=postId?'<option value="TELEGRAM_EDIT">Проверить и исправить опубликованный пост</option>':'';let referenceField=postId?'<label>ID подробной публикации для строки «Ранее» (необязательно)<input name="reference_message_id" type="number" min="1" step="1" placeholder="Например, 5"></label>':'';return `<details class="editorial-feedback"><summary>💬 Оставить обратную связь агенту</summary><form onsubmit="submitEditorialFeedback(event,this)" data-item-id="${itemId}" data-post-id="${postId}"><label>Тип обратной связи<select name="feedback_type" required><option value="">Выберите тип обратной связи</option>${correctionOption}<option value="POSITIVE">Понравилось — стоит повторять</option><option value="CORRECTION">Нужно исправить или уточнить</option><option value="NOT_RELEVANT">Не относится к нашей теме</option><option value="NOT_IMPORTANT">Недостаточно важно</option><option value="DUPLICATE">Повтор уже известного сюжета</option><option value="INACCURATE">Фактическая ошибка или слабый источник</option><option value="POOR_STYLE">Не подходит подача или стиль</option><option value="OTHER">Общий комментарий</option></select></label><label>Комментарий, поправка или пожелание<textarea name="reason" required minlength="5" maxlength="2000" rows="2" placeholder="Например: удачно объяснено влияние на рынок; здесь стоит уточнить, что решение пока не вступило в силу"></textarea></label>${referenceField}<button class="btn" type="submit">Сохранить обратную связь</button></form></details>`}
async function submitEditorialFeedback(event,form){event.preventDefault();let button=form.querySelector('button[type=submit]'),data=Object.fromEntries(new FormData(form).entries());data.item_id=Number(form.dataset.itemId)||null;data.post_id=Number(form.dataset.postId)||null;button.disabled=true;try{let result=await api(data.feedback_type==='TELEGRAM_EDIT'?'/api/post-corrections':'/api/editorial-feedback',{method:'POST',body:JSON.stringify(data)});showNotice(result.queued?'Правка поставлена в очередь проверки':'Отзыв сохранён и будет учтён в следующих разборах');form.reset();form.closest('details').open=false}catch(e){showNotice('Не удалось сохранить отзыв: '+e.message)}finally{button.disabled=false}}
function inlineText(s){let out='',i=0,re=/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)|\*\*([^*]+)\*\*/g,m;while((m=re.exec(s))){out+=esc(s.slice(i,m.index));out+=m[1]?`<a href="${esc(safeUrl(m[2]))}" target="_blank" rel="noopener noreferrer">${esc(m[1])}</a>`:`<strong>${esc(m[3])}</strong>`;i=re.lastIndex}return out+esc(s.slice(i))}
function postText(raw){let groups=String(raw||'').trim().split(/\n\s*\n/).filter(Boolean),html='';for(let i=0;i<groups.length;i++){let g=groups[i].trim(),next=groups[i+1]||'';if(/^\*\*.+?:\*\*$/.test(g)&&next.split(/\n/).every(x=>/^➠\s*/.test(x.trim()))){let label=g.replace(/^\*\*|\*\*$/g,'').replace(/:$/,'');html+=`<details class="post-details"><summary>${inlineText(label)}</summary><div class="post-details-body">${next.split(/\n/).map(x=>`<p>${inlineText(x.trim().replace(/^➠\s*/,''))}</p>`).join('')}</div></details>`;i++;continue}html+=`<p>${g.split(/\n/).map(inlineText).join('<br>')}</p>`}return `<div class="excerpt post-copy">${html}</div>`}
function postStatusLabel(v){return ({PENDING:'В обработке',PUBLISHED:'Опубликован',REJECTED:'Не опубликован',ON_HOLD:'На автоматической перепроверке'})[v]||'Статус не определён'}function postCard(p){let facts=p.facts||{},modeLabel=p.mode==='RULE_BASED'?'Резервный режим':p.mode==='AI'?'AI-разбор':p.mode,scopeLabel=({RUSSIA:'Россия',CIS:'СНГ',RUSSIA_CIS:'Россия и СНГ'})[facts.geographic_scope]||facts.geographic_scope,importanceLabel=({HIGH:'Высокая важность',MEDIUM:'Средняя важность',LOW:'Низкая важность'})[facts.importance]||facts.importance;let meta=[modeLabel,scopeLabel,importanceLabel,p.source_name].filter(Boolean).join(' · ');return `<article class="row-card"><div class="row-title">${esc(p.headline||('Пост #'+p.post_id))}</div><div class="meta"><span>${date(p.created_at||p.published_at)}</span>${pill(postStatusLabel(p.status),p.status==='PENDING'?'amber':p.status==='PUBLISHED'?'green':'')}</div>${postText(p.text)}${p.status==='PENDING'&&p.auto_reason?`<div class="queue-reason ${p.auto_reason.startsWith('Подходит')?'ready':''}">${esc(p.auto_reason)}</div>`:''}${meta?`<div class="meta">${esc(meta)}</div>`:''}<div class="actions">${p.url?`<a class="btn" href="${esc(safeUrl(p.url))}" target="_blank" rel="noopener">Источник ↗</a>`:''}${p.telegram_url?`<a class="btn" href="${esc(safeUrl(p.telegram_url))}" target="_blank" rel="noopener">В канале ↗</a>`:''}</div>${p.status==='PUBLISHED'?feedbackBox(p.item_id,p.post_id):''}</article>`}
function pipelinePostHtml(p){let trace=p.publication_trace,linkLabel=({EXPLICIT:'прямая запись',LEGACY_SOURCE_URL:'совпадение URL источника в старой записи',LEGACY_PROCESSING_TIME:'совпадение источника и времени в старой записи'})[p.link_method]||'связь с материалом не подтверждена',telegram=p.telegram_url?`<a href="${esc(safeUrl(p.telegram_url))}" target="_blank" rel="noopener">Открыть сообщение в Telegram ↗</a>`:p.external_id?`ID сообщения Telegram: ${esc(p.external_id)}`:'Ответ Telegram не сохранён';return `<details class="post-details"><summary>Пост #${esc(p.post_id)} · ${esc(postStatusLabel(p.status))}${trace?` · ${esc(trace.status_label)}`:''}</summary><div class="post-details-body"><div class="meta">Связь: ${esc(linkLabel)} · создан ${date(p.created_at)}${p.published_at?` · опубликован ${date(p.published_at)}`:''}</div>${postText(p.text)}<p>${telegram}</p>${trace?`<p>${esc(trace.status_label)} · попыток отправки: ${esc(trace.attempt_count)}${trace.telegram_message_id?` · ID Telegram: ${esc(trace.telegram_message_id)}`:''}${trace.error_code?` · ${esc(processingError(trace.error_code))}`:''}</p>${trace.events?.length?`<ol>${trace.events.map(e=>`<li>${esc(e.status)} · ${date(e.at)}</li>`).join('')}</ol>`:''}`:`<p>${p.status==='PUBLISHED'?'Подробный ответ Telegram отсутствует в старой записи.':'Попытка отправки ещё не сохранена.'}</p>`}</div></details>`}
function processingError(code){return ({DELIVERY_UNKNOWN:'Telegram не подтвердил результат отправки; повтор заблокирован до сверки с каналом.',AUTO_PUBLISH_GATE_FAILED:'Черновик не прошёл обязательный автоматический допуск.',TELEGRAM_REJECTED:'Telegram отклонил отправку; агент повторит её в установленном лимите.'})[code]||`Последняя ошибка: ${code}`}
function ageMinutes(timestamp){let time=Date.parse(timestamp||'');return Number.isFinite(time)?Math.max(0,Math.floor((Date.now()-time)/60000)):0}
function pipelineHistoryHtml(i){let events=i.decision_history||[];if(!events.length)return '';return `<details style="margin-top:10px;border-top:1px solid var(--line);padding-top:8px"><summary style="cursor:pointer;color:#536174;font-weight:650">История обработки · ${events.length}</summary><ol style="margin:8px 0 0;padding-left:20px">${events.map(h=>`<li style="margin:0 0 9px"><b>${esc(h.decision)}</b> · ${date(h.at)}${h.steps?.length?`<div style="color:var(--muted);margin:3px 0">${esc(h.steps.join(' → '))}</div>`:''}<div>${esc(h.reason)}</div></li>`).join('')}</ol></details>`}

const flowStages=[['intake','Приём','Материал сохранён'],['screening','Фильтр ключевиков','Список из таблицы и словоформы; без ИИ'],['reading','Чтение','Текст и атрибуция'],['analysis','Редакционный разбор','Тема, событие и новые факты'],['drafting','Написание','Текст поста'],['gate','Проверка поста','Финальные проверки'],['delivery','Telegram','Подтверждение отправки']];
const flowStatuses={READY:'В очереди',RUNNING:'Выполняется',DONE:'Завершён',WAITING:'Ожидает повтора',ERROR:'Ошибка',CLOSED:'Закрыт'};
function flowChain(i){const points=i?.checkpoints||[];return `<ol class="flow-chain" aria-label="Цепочка обработки">${flowStages.map(([key,label,help],n)=>{const cp=points.find(x=>x.stage===key);const state=cp?.status;return `<li class="flow-step ${state==='DONE'?'done':['ERROR','CLOSED'].includes(state)?'failed':['READY','RUNNING','WAITING'].includes(state)?'current':''}"><span class="step-number">${state==='DONE'?'✓':n+1}</span><details><summary><b>${esc(label)}</b><small>${esc(cp?(flowStatuses[state]||'Статус неизвестен'):i?'Нет записи':help)}</small></summary><p>${esc(cp?.reason||help)}</p>${cp?.updated_at?`<small>Обновлён ${date(cp.updated_at)}</small>`:''}</details></li>`}).join('')}</ol>`}
function chooseBucket(bucket){pipelineBucket=bucket;pipelineMilestone='all';pipelineOffset=0;document.getElementById('pipeline-stage').value='all';loadPipeline()}
function chooseCurrentStage(){pipelineBucket='all';pipelineMilestone='all';pipelineOffset=0;loadPipeline()}
function chooseMilestone(key){pipelineMilestone=key;pipelineBucket='all';pipelineOffset=0;document.getElementById('pipeline-stage').value='all';loadPipeline()}
function schedulePipeline(){clearTimeout(pipelineTimer);pipelineRequest++;pipelineOffset=0;pipelineTimer=setTimeout(loadPipeline,250)}
function materialState(i){
 const terminal=['published','filtered','processed'].includes(i.stage),cp=i.current_checkpoint||(i.checkpoints||[]).slice().reverse().find(x=>['READY','RUNNING','WAITING','ERROR'].includes(x.status));
 const labels={received:'Ожидает первого фильтра',screening:'Фильтр ключевиков',primary:'Чтение источника',ai:'Разбор ИИ',confirmation:'Повторная проверка',drafting:'Подготовка поста',review:'Проверка и отправка',technical:'Техническая ошибка',correction:'Проверка правки',published:'Опубликован',filtered:'Завершён без публикации',processed:'Сохранён без публикации'};
 const stageName=key=>flowStages.find(x=>x[0]===key)?.[1]||'Обработка';
 let current=terminal?labels[i.stage]:cp?stageName(cp.stage)+' · '+(flowStatuses[cp.status]||''):labels[i.stage];
 let next=terminal?(i.stage==='published'?'Пост в канале. Обработка завершена.':'Обработка завершена. Материал сохранён.'):({received:'Проверить ключевики и словоформы, без ИИ.',screening:'Завершить фильтр ключевиков, затем прочитать источник.',primary:'Прочитать источник, затем передать на разбор ИИ.',ai:'Завершить разбор ИИ, затем подготовить пост.',confirmation:'Повторить проверку нерешённых вопросов.',drafting:'Подготовить текст и проверить перед отправкой.',review:'Проверить готовый пост и отправить в канал при успешной проверке.',technical:'Устранить техническую ошибку. Продолжение пока не подтверждено.',correction:'Проверить правку опубликованного сообщения.'})[i.stage];
 if(!terminal&&cp){const n=flowStages.findIndex(x=>x[0]===cp.stage);if(cp.status==='ERROR')next='Устранить техническую ошибку. Продолжение пока не подтверждено.';else if(cp.status==='WAITING')next=`Повторить «${stageName(cp.stage)}»${cp.next_at?' после '+date(cp.next_at):' после устранения причины ожидания'}.`;else if(cp.status==='READY')next=`Дождаться очереди на этап «${stageName(cp.stage)}».`;else next=`Завершить «${stageName(cp.stage)}»${n>=0&&n<flowStages.length-1?'; дальше — '+flowStages[n+1][1]:', затем сохранить ответ Telegram'}.`}
 if(!terminal&&!cp&&i.retry_at)next=`Повтор разрешён после ${date(i.retry_at)}. Запуск зависит от свободного места в очереди.`;
 if(i.post?.publication_trace?.status==='UNKNOWN')next='Сверить результат отправки с каналом. Повторная отправка заблокирована.';
 return {current,next,reason:(terminal?i.reason:cp?.reason||i.retry_reason||i.reason)||'Причина не сохранена.'}
}
function materialCard(i){
 const state=materialState(i),active=!['published','filtered','processed'].includes(i.stage),tone=i.needs_attention?'amber':i.stage==='published'?'green':'';
 const timing=[active?'В обработке '+ageMinutes(i.discovered_at)+' мин':'',i.last_attempt_at?'Последняя обработка '+date(i.last_attempt_at):''].filter(Boolean).join(' · ');
 const filter=i.keyword_filter;
 return `<article class="row-card material-card" data-item-id="${i.item_id}"><div class="material-heading"><a class="row-title" href="${esc(safeUrl(i.canonical_url||i.url))}" target="_blank" rel="noopener">${esc(i.title)}</a>${pill(state.current,tone)}</div><div class="meta">${esc(i.source_name)} · получен ${date(i.discovered_at)}</div><dl class="material-summary"><div><dt>${active?'Состояние':'Результат'}</dt><dd>${esc(state.reason)}</dd></div><div><dt>Дальше</dt><dd>${esc(state.next)}</dd></div></dl>${timing?`<div class="material-timing">${esc(timing)}</div>`:''}<details class="material-details"><summary>Путь и подробности</summary>${flowChain(i)}${!i.checkpoints?.length?'<p class="muted">Подробная история этапов для этого материала не сохранена.</p>':''}${filter?`<p><b>Первый фильтр, без ИИ:</b> ${filter.passed?'совпадение — '+esc(filter.matched_keyword):'совпадений с ключевиками нет'}.</p>`:''}<p>${esc(i.material_status||i.primary_status||'Статус чтения не сохранён')}${i.material_url?` · <a href="${esc(safeUrl(i.material_url))}" target="_blank" rel="noopener">Прочитанный источник ↗</a>`:''}<br>ИИ-разбор: ${esc(i.analysis_status||'Статус не записан')}</p>${i.retry_limit?`<p>Повторов: ${i.retry_attempts||0} из ${i.retry_limit}${i.retry_at?' · повтор разрешён с '+date(i.retry_at):''}. Разрешённое время не гарантирует немедленный запуск.</p>`:''}${i.retry_queue_position?`<p>Место в очереди повторов: ${i.retry_queue_position} из ${i.retry_queue_total}.</p>`:''}${i.processing_job?.error_code?`<p class="error">${esc(processingError(i.processing_job.error_code))}</p>`:''}${i.post?pipelinePostHtml(i.post):''}${pipelineHistoryHtml(i)}${i.summary?`<p>${esc(i.summary)}</p>`:''}${i.issues?.length?`<p>Замечания: ${esc(i.issues.join(' · '))}</p>`:''}</details>${feedbackBox(i.item_id,i.post?.status==='PUBLISHED'?i.post.post_id:'')}</article>`
}
async function loadPipeline(){
 clearTimeout(pipelineTimer);
 const request=++pipelineRequest,root=document.getElementById('pipeline-list');
 if(!root.children.length)root.innerHTML='<div class="empty">Загружаю материалы…</div>';
 root.setAttribute('aria-busy','true');
 try{
  const params=new URLSearchParams({period:document.getElementById('pipeline-period').value,stage:document.getElementById('pipeline-stage').value,bucket:pipelineBucket,milestone:pipelineMilestone,q:document.getElementById('pipeline-search').value,offset:String(pipelineOffset)}),d=await api('/api/pipeline?'+params);
  if(request!==pipelineRequest)return;
  document.getElementById('pipeline-counter-start').textContent=d.counter_started_at?'Новый отсчёт с '+date(d.counter_started_at)+'. Счётчики и нажатия на шаги показывают только новые материалы. Прежние доступны во вкладке «Все».':'';
  const shortLabels={received:'Получено',first_filter:'Прошло фильтр',primary_read:'Прочитано',analyzed:'Разобрано ИИ',drafted:'Подготовлен пост',checked:'Прошло проверку',published:'Опубликовано'};
  document.getElementById('pipeline-funnel').innerHTML=d.funnel.map((s,n)=>`<button class="path-step" aria-pressed="${pipelineMilestone===s.key}" title="${esc(s.description)}" onclick="chooseMilestone('${esc(s.key)}')"><span>${n+1}. ${esc(shortLabels[s.key]||s.label)}</span><strong>${s.count.toLocaleString('ru-RU')}</strong></button>`).join('');
  document.querySelectorAll('[data-bucket]').forEach(b=>{b.classList.toggle('active',b.dataset.bucket===pipelineBucket);b.setAttribute('aria-pressed',String(b.dataset.bucket===pipelineBucket))});
  const select=document.getElementById('pipeline-stage'),chosen=select.value,options='<option value="all">Любом</option>'+d.stages.map(s=>`<option value="${esc(s.key)}">${esc(s.label)}</option>`).join('');
  if(select.innerHTML!==options)select.innerHTML=options;
  select.value=[...select.options].some(o=>o.value===chosen)?chosen:'all';
  const milestone=d.funnel.find(s=>s.key===pipelineMilestone),bucketLabel={work:'В работе',attention:'Требуют внимания',published:'Опубликованы',closed:'Завершены без публикации',all:'Все материалы'}[pipelineBucket];
  document.getElementById('pipeline-selection').innerHTML=`<span>${milestone?esc(milestone.label):bucketLabel} · ${d.total.toLocaleString('ru-RU')}</span>${milestone?'<button class="text-btn" onclick="chooseBucket(&quot;work&quot;)">Вернуться к работе ×</button>':''}${pipelineBucket==='attention'?'<small>Ошибки, неизвестный результат отправки или обработка дольше 15 минут. Долгое ожидание само по себе не означает сбой.</small>':''}`;
  // Preserve expanded cards while the list refreshes automatically.
  const openDetails=new Set([...root.querySelectorAll('article[data-item-id] details[open]')].map(e=>e.closest('article').dataset.itemId+':'+[...e.closest('article').querySelectorAll('details')].indexOf(e)));
  root.innerHTML=d.items.length?d.items.map(materialCard).join(''):'<div class="empty">В этой выборке материалов нет. Выберите «Все» или измените период.</div>';
  root.querySelectorAll('article[data-item-id] details').forEach(e=>{e.open=openDetails.has(e.closest('article').dataset.itemId+':'+[...e.closest('article').querySelectorAll('details')].indexOf(e))});
  document.getElementById('pipeline-pagination').innerHTML=`<span>${d.total?'Показаны '+(d.offset+1)+'–'+(d.offset+d.items.length)+' из '+d.total:'Нет материалов'} · ${date(d.updated_at)}</span> ${d.offset>0?'<button class="btn" onclick="pipelineOffset=Math.max(0,pipelineOffset-30);loadPipeline()">← Назад</button>':''} ${d.offset+d.items.length<d.total?'<button class="btn" onclick="pipelineOffset+=30;loadPipeline()">Дальше →</button>':''}`;
 }catch(e){if(request===pipelineRequest){root.innerHTML=`<div class="empty error">Не удалось загрузить материалы: ${esc(e.message)}</div>`;document.getElementById('pipeline-selection').textContent='Данные списка не обновлены';document.getElementById('pipeline-pagination').innerHTML=''}}
 finally{if(request===pipelineRequest)root.setAttribute('aria-busy','false')}
}
function renderAnalysis(){let root=document.getElementById('analysis-list');if(!analysisDrafts.length){root.innerHTML='<div class="empty">Черновик появится после включения генерации, если за неделю накопится достаточно подтверждённых материалов.</div>';return}root.innerHTML=analysisDrafts.map(d=>`<article class="row-card"><div class="row-title">${esc(d.title)}</div><div class="meta">${date(d.created_at)} · ${pill('Нужна редакторская проверка','amber')} · ${esc(d.period_start)} — ${esc(d.period_end)}</div><p><b>Главный тезис:</b> ${esc(d.thesis)}</p><div class="analysis-body">${esc(d.body).replace(/\n/g,'<br>')}</div><h3>Доказательства и первоисточники</h3><ol>${d.sources.map(x=>`<li>${esc(x.publisher)} · ${esc(x.title)} <a href="${esc(safeUrl(x.url))}" target="_blank" rel="noopener">Источник ↗</a></li>`).join('')}</ol></article>`).join('')}
function renderPublished(){let q=(document.getElementById('published-search').value||'').toLowerCase(),p=posts.filter(x=>x.status==='PUBLISHED'&&(!q||(`${x.headline} ${x.text}`).toLowerCase().includes(q)));document.getElementById('published-list').innerHTML=p.length?p.map(x=>postCard(x)).join(''):'<div class="empty">Публикаций пока нет</div>'}
function renderSources(){let activeSources=sources.filter(x=>x.active),inactiveSources=sources.filter(x=>!x.active),showInactive=document.getElementById('show-inactive-sources').checked,visibleSources=showInactive?sources:activeSources,good=activeSources.filter(x=>x.health==='ok').length;document.getElementById('source-updated').textContent=`${good} из ${activeSources.length} включено`;document.getElementById('inactive-source-count').textContent=inactiveSources.length;const labels={manual:'Ссылки на статьи',rss:'RSS-ленты',web:'Сайты с RSS',telegram:'Telegram-каналы',google_news:'Google News',web_search:'Web Search',x:'X'},order=['manual','rss','web','google_news','web_search','x','telegram'];document.getElementById('sources-table').innerHTML=order.map(type=>{let group=visibleSources.filter(x=>x.type===type);if(!group.length)return '';return `<section class="source-group"><div class="source-group-head"><h3>${labels[type]}</h3><span class="muted">${group.length} · ${group.filter(x=>x.active).length} включено</span></div><div class="table-wrap"><table class="table"><thead><tr><th>Источник</th><th>Состояние</th><th>За 24 часа</th><th>Проверен</th></tr></thead><tbody>${group.map(s=>`<tr><td><div class="source-title">${esc(s.name)}</div><div class="muted">${esc(s.url)}</div></td><td>${!s.active?pill('Только отдельные ссылки'):pill(s.health==='ok'?'Работает':s.health==='stale'?'Давно не проверялся':'Ошибка',s.health==='ok'?'green':s.health==='stale'?'amber':'red')}${s.error?`<div class="error">${esc(s.error)}</div>`:''}</td><td>${s.found24} найдено · ${s.useful24} новых сюжетов</td><td>${date(s.last_checked_at)}</td></tr>`).join('')}</tbody></table></div></section>`}).join('')||'<div class="empty">Источников пока нет</div>'}async function addSource(event){event.preventDefault();const form=event.currentTarget,data=Object.fromEntries(new FormData(form).entries());try{await api('/api/sources',{method:'POST',body:JSON.stringify(data)});form.reset();document.querySelector('.source-add').open=false;showNotice('Источник добавлен; он появится в следующем цикле');await reloadAll()}catch(e){showNotice(e.message)}}
const regStages={PROJECT:'Проект',INTRODUCED:'Внесён',ADOPTED:'Принят',PUBLISHED:'Опубликован',IN_FORCE:'Вступил в силу',WITHDRAWN:'Отклонён / отозван',GUIDANCE:'Разъяснение',UNKNOWN:'Стадия не установлена'};
function regLabel(id){return id==='D1'?'Основной документ':id==='PREVIOUS'?'Прежняя версия':'Связанный документ '+id.replace('D','')}
function regReadable(text){return String(text||'').replace(/\bD1\b/g,'основной документ').replace(/\bD2\b/g,'связанный документ')}
function regEvidence(list,a){return (list||[]).map(e=>{let s=a.sources?.[e.source];return `<details style="margin:8px 0"><summary class="muted">${esc(regLabel(e.source))} · стр. ${esc(e.page)} · ${esc(e.point)}</summary><p style="white-space:pre-wrap">${esc(e.quote)}</p>${s?`<a href="${esc(safeUrl(s.final_url))}#page=${Number(e.page)}" target="_blank" rel="noopener">Открыть основание</a>`:''}</details>`}).join('')}
function regClaims(list,a){const labels={FACT:'По документу',INTERPRETATION:'Вывод',FORECAST:'Сценарий'};return (list||[]).map(c=>`<div style="padding:12px 0;border-bottom:1px solid var(--line)">${pill(labels[c.kind]||'Вывод',c.kind==='FORECAST'?'amber':'')}<p>${esc(regReadable(c.text))}</p>${regEvidence(c.evidence,a)}</div>`).join('')}
async function loadRegulatory(){try{const d=await api('/api/regulatory');const filter=document.getElementById('reg-filter')?.value||'ready';const visible=d.items.filter(x=>filter==='all'||(filter==='ready'?x.current_version!==null:x.current_version===null));
document.getElementById('reg-status').textContent=`Последний цикл: ${date(d.last_cycle)} · Исследований: ${d.researched??d.items.filter(x=>x.current_version!==null).length} · Всего кандидатов: ${d.total}`;
document.getElementById('reg-sources').innerHTML=d.sources.map(s=>`<p><strong>${esc(s.name)}</strong> · ${s.id==='cbr'?'Разделы проектов и актов':'Поиск по официальному домену'} · ${s.error?'Ошибка проверки':s.checked_at?'Проверен':'Ещё не проверен'} · ${date(s.checked_at)} · найдено ${s.discovered}</p>`).join('');
document.getElementById('reg-list').innerHTML=visible.map(x=>{const a=x.analysis||{},status={QUEUED:'Ждёт исследования',RETRY:'Повторная проверка',NEEDS_REVIEW:'На редакторской проверке'}[x.status]||x.status;return `<article class="row-card"><a class="row-title" href="${esc(safeUrl(x.url))}" target="_blank" rel="noopener">${esc(a.title||x.title)}</a><div class="meta">${pill(status,'amber')}${pill(regStages[a.stage]||'Стадия не установлена')}<span>Исследование: ${date(x.observed_at)}</span></div>${x.error?'<p class="error">Последняя проверка не завершена; прежний разбор может быть неактуален.</p>':''}${a.summary?`<p>${esc(a.summary)}</p><p><strong>Кого затрагивает:</strong> ${esc(a.affected)}</p>`:''}${a.steps?`<details class="post-details"><summary>Объяснение по шагам</summary><div class="post-details-body">${regClaims(a.steps,a)}</div></details>`:''}${a.relations?.length?`<details class="post-details"><summary>Связи документов (${a.relations.length})</summary><div class="post-details-body">${a.relations.map(r=>`<div style="padding:10px 0"><strong>${esc(r.reference)}</strong>${r.relationship==='UNRESOLVED'?pill('Источник не проверен','amber'):''}<p>${esc(regReadable(r.explanation))}</p>${regEvidence(r.evidence,a)}</div>`).join('')}</div></details>`:''}${a.angles?.length?`<details class="post-details"><summary>Темы для канала</summary><div class="post-details-body">${regClaims(a.angles,a)}</div></details>`:''}${a.deadlines?.length?`<details class="post-details"><summary>Сроки и условия</summary><div class="post-details-body">${a.deadlines.map(t=>`<div><strong>${esc(t.date)}</strong> — ${esc(t.meaning)}${Array.isArray(t.evidence)?regEvidence(t.evidence,a):''}</div>`).join('')}</div></details>`:''}${a.draft_paragraphs?.length?`<details class="post-details"><summary>Редакционный черновик</summary><div class="post-details-body"><strong>${esc(a.draft_title)}</strong>${a.draft_paragraphs.map(p=>`<p>${esc(p.text)}</p>`).join('')}<p class="muted">Черновик не опубликован. Нужны проверка новизны и редакторский допуск.</p></div></details>`:''}${a.open_questions?.length?`<details class="post-details"><summary>Что ещё проверить (${a.open_questions.length})</summary><div class="post-details-body">${a.open_questions.map(q=>`<p>${esc(regReadable(q))}</p>`).join('')}</div></details>`:''}${a.review?`<p class="muted">Проверка выводов: ${a.review.verdict==='PASS'?'автоматических замечаний нет':'есть замечания'}</p>${(a.review.problems||[]).map(p=>`<p class="queue-reason">${esc(p)}</p>`).join('')}`:''}${a.sources?`<details class="post-details"><summary>Прочитанные источники и версии</summary><div class="post-details-body">${Object.entries(a.sources).map(([id,s])=>`<p><a href="${esc(safeUrl(s.final_url))}" target="_blank" rel="noopener">${esc(regLabel(id))} · ${esc(new URL(s.final_url).hostname)}</a> · ${s.page_count} стр. · ${date(s.read_at)}${s.ocr_pages?.length?' · применён OCR, требуется сверка':''}${s.empty_pages?.length?' · есть непрочитанные страницы':''}</p>`).join('')}</div></details>`:''}<details class="post-details"><summary>История исследований (${x.history.length})</summary><div class="post-details-body">${x.history.map(h=>`<p>${date(h.observed_at)} · ${esc(regStages[h.stage]||'')}<br>${esc(h.summary)}</p>`).join('')}</div></details></article>`}).join('')||'<div class="empty">Документов пока нет. Состояние проверки смотрите в охвате источников.</div>';
}catch(e){document.getElementById('reg-status').textContent='Не удалось загрузить нормативный мониторинг';showNotice(e.message)}}

let resourceRequest=0;document.getElementById('resource-item').value=new URLSearchParams(location.search).get('item_id')||'';
let spendingRequest=0, billingLastAttempt=0;
const spendingMoney=n=>n==null?'Нет данных':Number(n)>0&&Number(n)<.01?'Менее $0,01':'$'+Number(n).toLocaleString('ru-RU',{minimumFractionDigits:2,maximumFractionDigits:2});
function spendingQuery(){const p=document.getElementById('spending-period').value;return '?period='+encodeURIComponent(p)+(p==='custom'?'&start='+document.getElementById('spending-start').value+'&end='+document.getElementById('spending-end').value:'')}
function spendingDate(s){return s?new Date(s).toLocaleDateString('ru-RU',{timeZone:'UTC'}):'не указан'}
function spendingTable(rows){return '<table class="table" style="min-width:420px"><thead><tr><th>Задача</th><th>Учтённая сумма</th><th>Что ещё неизвестно</th></tr></thead><tbody>'+rows.map(r=>'<tr><td>'+esc(r.name)+'<div class="muted">'+resourceNumber(r.calls)+' запросов</div></td><td>'+spendingMoney(r.priced_calls?r.amount_usd:null)+(r.unpriced_calls&&r.priced_calls?'<div class="muted">Это часть расходов</div>':'')+'</td><td>'+(r.unpriced_calls?resourceNumber(r.unpriced_calls)+' запросов без суммы':'Все запросы имеют оценку')+'</td></tr>').join('')+'</tbody></table>'}
async function loadSpending(){const seq=++spendingRequest,p=document.getElementById('spending-period').value,custom=p==='custom';for(const id of ['spending-start-label','spending-end-label'])document.getElementById(id).style.display=custom?'flex':'none';if(custom&&(!document.getElementById('spending-start').value||!document.getElementById('spending-end').value))return;try{const d=await api('/api/spending'+spendingQuery());if(seq!==spendingRequest)return;const t=d.local,b=d.actual||d.owner_report,matched=!!d.actual,scope=b?({account:'Весь аккаунт OpenAI',kovalsky:'Только проект Kovalsky',unknown:'Область расходов не уточнена'})[b.scope]:'';document.getElementById('spending-dates').textContent='Журнал за '+spendingDate(d.start)+' — '+spendingDate(d.end)+'. Дни считаются по времени OpenAI (UTC).';const reported=b?.source==='owner_report';document.getElementById('spending-summary').innerHTML=[[spendingMoney(b?.amount_usd),b?(matched?'Расход в OpenAI':'Последняя указанная сумма OpenAI'):'Расход в OpenAI',b?scope+' · '+(reported?'Введено владельцем':'Получено из OpenAI')+(matched?'':' · Период не совпадает или не указан'):'Фактические списания ещё не подключены'],[spendingMoney(t.priced_calls?t.known_estimated_usd:null),'Учтено по запросам Kovalsky','Оценка '+resourceNumber(t.priced_calls)+' из '+resourceNumber(t.calls)+' запросов за выбранный период']].map(x=>'<div class="metric"><label>'+esc(x[1])+'</label><strong>'+esc(x[0])+'</strong><small>'+esc(x[2])+'</small></div>').join('');let explanation=t.unpriced_calls?resourceNumber(t.unpriced_calls)+' запросов пока без денежной оценки. Учтённая сумма не показывает весь расход.':'Все сохранённые запросы за период имеют денежную оценку.';if(b&&!matched)explanation+=' Последнее значение OpenAI: '+spendingMoney(b.amount_usd)+'. Период: '+spendingDate(b.start)+' — '+spendingDate(b.end)+'. Оно не распределяется по задачам.';if(d.unexplained_usd!=null)explanation+=' Разница с суммой OpenAI, пока не объяснённая журналом: '+spendingMoney(d.unexplained_usd)+'.';else if(d.reconciliation_conflict)explanation+=' Оценка журнала превышает сумму OpenAI: нужна сверка дат и тарифов.';else if(matched)explanation+=' Сумма по аккаунту не позволяет установить расход только Kovalsky.';explanation+=' Подробный учёт начался '+date(d.accounting_started_at)+'.';document.getElementById('spending-explanation').textContent=explanation;document.getElementById('spending-tasks').innerHTML=d.tasks.length?spendingTable(d.tasks):'<div class="empty">За период нет модельных запросов.</div>';document.getElementById('spending-provider').innerHTML=d.actual?.line_items?.length?'<h3>На что списал деньги OpenAI</h3><table class="table" style="min-width:320px"><thead><tr><th>Статья списания</th><th>Сумма</th></tr></thead><tbody>'+d.actual.line_items.map(r=>'<tr><td>'+esc(r.name)+'</td><td>'+spendingMoney(r.amount_usd)+'</td></tr>').join('')+'</tbody></table>':'';document.getElementById('billing-sync').disabled=!d.connection.configured;document.getElementById('spending-connection').textContent=d.connection.configured?'Автосверка подключена. '+(d.actual?'Последнее обновление: '+date(d.actual.recorded_at)+'. ':'')+'OpenAI может уточнять списания с задержкой.':'Автосверка не подключена. На сервере нужен отдельный административный ключ для чтения расходов OpenAI. Пока можно внести сумму вручную.';if(d.connection.configured&&(!d.actual||Date.now()-Date.parse(d.actual.recorded_at)>300000)&&Date.now()-billingLastAttempt>300000)syncBilling()}catch(e){if(seq===spendingRequest)document.getElementById('spending-explanation').textContent='Не удалось загрузить расходы: '+e.message}}
async function syncBilling(){billingLastAttempt=Date.now();const button=document.getElementById('billing-sync');button.disabled=true;button.textContent='Сверяю…';try{const r=await api('/api/billing/sync'+spendingQuery(),{method:'POST',body:JSON.stringify({})});if(!r.ok)showNotice(r.message);await loadSpending()}catch(e){showNotice(e.message)}finally{button.textContent='Сверить с OpenAI'}}
document.getElementById('billing-report-form').addEventListener('submit',async e=>{e.preventDefault();const box=document.getElementById('billing-report-result');try{await api('/api/billing/report',{method:'POST',body:JSON.stringify({amount_usd:document.getElementById('billing-amount').value,scope:document.getElementById('billing-scope').value,start:document.getElementById('billing-start').value,end:document.getElementById('billing-end').value})});box.textContent='Сумма сохранена.';await loadSpending()}catch(err){box.textContent=err.message}});
const resourceNumber=n=>Number(n||0).toLocaleString('ru-RU'), resourceTime=n=>Number(n||0).toFixed(2)+' с', resourceMoney=n=>n==null?'Неизвестно':'$'+Number(n).toFixed(4);
function resourceTable(rows,label){return rows.length?'<table class="table"><thead><tr><th>'+label+'</th><th>Запросы / ошибки / повторы</th><th>Вход / кеш / выход</th><th>Поиск</th><th>Время операций / API / процессор</th><th>Кеш / отсрочки</th><th>Оценка стоимости</th></tr></thead><tbody>'+rows.map(r=>'<tr><td>'+esc(r.label||r.model||r.name||r.category)+'<div class="muted">'+resourceNumber(r.operations)+' операций · '+resourceNumber(r.operation_errors)+' ошибок</div></td><td>'+resourceNumber(r.calls)+' / '+resourceNumber(r.errors)+' / '+resourceNumber(r.retries)+'</td><td>'+resourceNumber(r.input_tokens)+' / '+resourceNumber(r.cached_input_tokens)+' / '+resourceNumber(r.output_tokens)+(r.unknown_usage?'<div class="muted">'+resourceNumber(r.unknown_usage)+' ответов без полного расхода</div>':'')+'</td><td>'+resourceNumber(r.search_actions)+(r.unknown_search_usage?'<div class="muted">'+resourceNumber(r.unknown_search_usage)+' с неизвестным числом поисков</div>':'')+'</td><td>'+resourceTime(r.operation_seconds)+' / '+resourceTime(r.api_seconds)+' / '+resourceTime(r.cpu_seconds)+'</td><td>'+resourceNumber(r.cache_hits)+' / '+resourceNumber(r.deferred)+'</td><td>'+(r.calls?resourceMoney(r.estimated_usd):'Нет запросов ИИ')+(r.unpriced_calls?'<div class="muted">Известная часть: '+resourceMoney(r.priced_calls?r.known_estimated_usd:null)+'</div>':'')+(r.reference_calls?'<div class="muted">По стандартному тарифу, режим неизвестен: '+resourceMoney(r.reference_usd)+'</div>':'')+'</td></tr>').join('')+'</tbody></table>':'<div class="empty">За выбранный период нет записей</div>'}
document.getElementById('policy-questions').addEventListener('submit',async e=>{e.preventDefault();const f=e.target;try{await api('/api/policy/clarify',{method:'POST',body:JSON.stringify({key:f.dataset.key,answer:f.elements.answer.value})});showNotice('Ответ сохранён; изменение пройдёт проверку и запись');loadPolicy()}catch(err){showNotice(err.message)}});
async function loadPolicy(){try{const d=await api('/api/policy');document.getElementById('policy-version').textContent='Версия '+d.version;document.getElementById('policy-document').textContent=d.document;document.getElementById('policy-questions').innerHTML=d.questions.map(q=>'<form class="policy-answer" data-key="'+esc(q.key)+'"><h3>'+esc(q.question)+'</h3><p>'+esc(q.reason)+'</p><textarea name="answer" required maxlength="4000" placeholder="Уточнение области действия"></textarea><button class="btn" type="submit">Сохранить ответ</button></form>').join('');document.getElementById('policy-history').innerHTML=d.applied.length?'<h3>Применённые изменения</h3>'+d.applied.map(j=>'<p class="muted">'+esc(j.at?new Date(j.at*1000).toLocaleString('ru-RU'):'Дата не указана')+' · Версия '+esc(j.version.version)+' · '+esc(j.version.sha256)+'</p>'+j.changes.map(c=>'<p><b>'+esc(c.section)+'</b>: '+esc(c.previous_rule||'Правила не было')+' → '+esc(c.operation==='DISABLE'?'Отключено':c.rule)+'</p>').join('')).join(''):'';document.getElementById('policy-amendments').innerHTML=d.amendments.length?'<h3>Уточнения владельца</h3>'+d.amendments.map(x=>'<p>'+esc(x[1])+'</p>').join(''):''}catch(e){showNotice(e.message)}}
async function loadResources(){loadSpending();let request=++resourceRequest,box=document.getElementById('resource-coverage');try{const item=document.getElementById('resource-item').value;let d=await api('/api/resources?hours='+document.getElementById('resource-period').value+(item?'&item_id='+encodeURIComponent(item):''));if(request!==resourceRequest)return;const t=d.total;document.getElementById('resource-summary').innerHTML=[[resourceNumber(t.calls),'Запросов к модели',resourceNumber(t.errors)+' ошибок · '+resourceNumber(t.retries)+' транспортных повторов'],[t.calls?resourceMoney(t.estimated_usd):'Нет запросов','Оценка расходов',t.unpriced_calls?'Полная сумма пока неизвестна':'По сохранённым ответам API'],[resourceNumber(t.input_tokens+t.output_tokens),'Токенов с известным расходом',resourceNumber(t.cached_input_tokens)+' кешированных входных'],[resourceNumber(t.cache_hits),'Попаданий в кеш',resourceNumber(t.deferred)+' отсрочек']].map(x=>'<div class="metric"><label>'+x[1]+'</label><strong>'+x[0]+'</strong><small>'+x[2]+'</small></div>').join('');box.textContent='Обновлено '+date(d.to)+' · Детальный учёт с '+date(d.accounting_started_at)+' · '+d.publications+' публикаций за период. '+t.unknown_usage+' запросов без полного расхода; '+t.unpriced_calls+' без полной денежной оценки. '+t.retry_attribution_unknown+' старых запросов без отметки транспортного повтора. '+t.unattributed_source+' запросов без привязки к источнику. '+t.legacy_cache_hits+' попаданий в кеш из прежнего журнала. '+t.unknown_bytes+' без полного размера обмена. Вход / ответ по известным размерам: '+resourceNumber(t.request_bytes)+' / '+resourceNumber(t.response_bytes)+' байт. Токены рассуждений: '+resourceNumber(t.reasoning_tokens)+' (у '+t.unknown_reasoning+' ответов разбивка отсутствует). Стоимость на публикацию: '+resourceMoney(d.cost_per_publication_usd)+'.';document.getElementById('resource-stages').innerHTML=resourceTable(d.stages.sort((a,b)=>b.calls-a.calls||b.operation_seconds-a.operation_seconds),'Этап');document.getElementById('resource-roles').innerHTML=resourceTable(d.roles.map(r=>({...r,label:({collector:'Сборщик',filter:'Фильтровщик',editor:'Редактор'})[r.role]||'Другая роль'})),'Роль');document.getElementById('resource-models').innerHTML=resourceTable(d.models,'Модель');const labels={fresh:'Свежие материалы',retry:'Повторная проверка',correction:'Правки',background:'Фоновые задачи',feeds:'Сбор источников',watch:'Наблюдение за сюжетом',digest:'Дайджест'};document.getElementById('resource-categories').innerHTML=resourceTable(d.categories.map(r=>({...r,label:labels[r.category]||'Другая задача'})),'Задача');document.getElementById('resource-sources').innerHTML=resourceTable(d.sources,'Источник');document.getElementById('resource-hours').innerHTML=resourceTable(d.hourly.map(r=>({...r,label:new Date(r.hour).toLocaleString('ru-RU',{timeZone:'Europe/Moscow',day:'2-digit',month:'2-digit',hour:'2-digit',minute:'2-digit'})})),'Час');document.getElementById('resource-notes').textContent=d.notes.join('\n')+'\nТарифы проверены '+d.pricing_checked_at+'.'}catch(e){if(request===resourceRequest)box.textContent='Не удалось загрузить расход: '+e.message}}

async function reloadRegistry(){try{let r=await api('/api/source-registry');let el=document.getElementById('registry-status');if(!r.connected){el.textContent='';return}document.getElementById('source-add-form').closest('.source-add').style.display=r.authoritative?'none':'';let pending=r.rows.filter(x=>x.entity_monitored&&x.status!=='В мониторинге').length;el.innerHTML=`<a href="${esc(r.url)}" target="_blank" rel="noopener">Единая база Google Таблиц</a> · ${r.rows.filter(x=>x.entity_monitored).length} объектов наблюдения · правки и галочка «Мониторинг» — в таблице · ${pending} без прямой ленты · ${r.error?'ошибка чтения, используется сохранённая версия':'проверен '+date(new Date(r.checked_at*1000).toISOString())}`;}catch(e){document.getElementById('registry-status').textContent='Не удалось прочитать состояние справочника'}}
async function copyGoogleScript(){try{let r=await api('/api/topic-registry/script');let box=document.getElementById('google-script-code');box.value=r.code;box.hidden=false;box.focus();box.select();let copied=false;try{if(navigator.clipboard){await navigator.clipboard.writeText(r.code);copied=true}else{copied=document.execCommand('copy')}}catch(e){}showNotice(copied?'Код скопирован. Вставьте его в Apps Script таблицы.':'Выделенный код можно скопировать вручную.')}catch(e){showNotice(e.message)}}
document.getElementById('google-script-form').addEventListener('submit',async e=>{e.preventDefault();let f=e.target,b=f.querySelector('button[type="submit"]');b.disabled=true;try{await api('/api/topic-registry/apps-script',{method:'POST',body:JSON.stringify({url:f.elements.url.value.trim(),secret:f.elements.secret.value.trim()})});f.elements.secret.value='';showNotice('Подключено: запись в таблицу проверена. Обучающие отзывы будут менять темник.');await loadTopicRegistry()}catch(err){showNotice(err.message)}finally{b.disabled=false}});
async function connectGoogleSheetsKey(input){let f=input.files[0];if(!f)return;try{if(f.size>8192)throw Error('Слишком большой файл ключа');let data=JSON.parse(await f.text());let r=await api('/api/topic-registry/credentials',{method:'POST',body:JSON.stringify(data)});showNotice('Служебный аккаунт подключён, запись в таблицу проверена');await loadTopicRegistry()}catch(e){showNotice(e.message)}finally{input.value=''}}
async function loadEditorialRegistry(){try{let r=await api('/api/editorial-registry');document.getElementById('policy-question-count').textContent=r.questions?.length?'('+r.questions.length+' требуют ответа)':'';document.getElementById('editorial-registry-status').textContent=r.connected?'Редактура: '+(r.counts?.['Редакторские правила']||0)+' правил · '+(r.counts?.['Примеры редактуры']||0)+' примеров'+(r.error?' · Используется последняя прочитанная версия':'')+(r.writer?.ready?' · Обучение подключено':' · Правила читаются; для записи обучения обновите Apps Script'):''}catch(e){document.getElementById('editorial-registry-status').textContent='Не удалось проверить редакторскую таблицу'}}
async function loadTopicRegistry(){try{let r=await api('/api/topic-registry');document.getElementById('topic-registry-status').innerHTML=r.connected?`Темник: <a href="${esc(safeUrl(r.url))}" target="_blank" rel="noopener">Google Таблица ↗</a> · ${r.counts?.['Темы']||0} тем · ${r.counts?.['Ключевые слова']||0} ключей${r.error?' · Используется последняя прочитанная версия':''}${r.writing_available?' · Запись из обучающих отзывов доступна':' · Для записи обучающих отзывов нужен доступ Google'}`:''}catch(e){document.getElementById('topic-registry-status').textContent='Не удалось проверить подключение темника'}}

async function loadOperations(){
 try{const d=await api('/api/diagnostics');
 const state=d.state||{},errors=['diagnostic_last_error','material_processor_error','ai_last_error'].map(key=>({key,...state[key]})).filter(x=>x.code||x.location);
 const block=state.api_account_blocked_until,blocked=block&&Date.parse(block)>Date.now();
 const accountCause={'HTTP_429:credit_balance_exhausted':'Баланс поставщика ИИ исчерпан. Следующий шаг: пополнить баланс у поставщика.','HTTP_429:insufficient_quota':'Достигнута квота счёта ИИ. Следующий шаг: проверить квоту и оплату у поставщика.','HTTP_429:billing_hard_limit_reached':'Достигнут платёжный лимит ИИ. Следующий шаг: проверить лимит у поставщика.','HTTP_401:invalid_api_key':'Поставщик ИИ отклонил доступ. Следующий шаг: проверить действительность ключа.'}[state.ai_last_error?.code]||'Счёт ИИ временно недоступен. Следующий шаг: проверить состояние счёта у поставщика.';
 document.getElementById('account-alert').innerHTML=(blocked?`<div class="queue-reason"><b>${esc(accountCause)}</b> Проверка доступности разрешена после ${date(block)}; материалы остаются в очереди, попытки не расходуются.</div>`:'');
 document.getElementById('overview-errors').innerHTML=errors.map(x=>{const labels={diagnostic_last_error:'Последний сохранённый сбой цикла',material_processor_error:'Последний сохранённый сбой обработки',ai_last_error:'Последняя сохранённая ошибка ИИ'};const loc=x.location;return `<details class="queue-reason"><summary><b>${labels[x.key]}</b> · ${date(x.at)}${x.code?' · '+esc(x.code):''}</summary><p>Это запись последнего сбоя, а не подтверждение текущей остановки.${loc?.type?' Тип: '+esc(loc.type)+'.':''}</p>${loc?.frames?.length?`<div class="muted">Место ошибки: ${loc.frames.map(f=>`${esc(f.file)} · ${esc(f.function)} · строка ${esc(f.line)}`).join(' → ')}</div>`:''}<p>Следующий шаг: проверить актуальное состояние и причину в карточке материала.</p></details>`}).join('')+(!errors.length&&!blocked?'<div class="muted">Сохранённых общих ошибок нет. Причины отдельных отказов и повторов — в карточках обработки.</div>':'');
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
                    from .resources import snapshot
                    query = parse_qs(parsed.query)
                    try:
                        hours = int(query.get('hours', ['24'])[0])
                        item_id = int(query['item_id'][0]) if query.get('item_id') else None
                    except (ValueError, TypeError):
                        self._json({'error': 'Некорректный период или материал'}, 400)
                        return
                    db = self._read_db()
                    try:
                        self._json(snapshot(db, config, hours=hours, item_id=item_id))
                    finally:
                        db.close()
                elif parsed.path == "/api/pipeline":
                    from .pipeline import pipeline_snapshot
                    db = self._read_db()
                    try:
                        self._json(pipeline_snapshot(db, config, parse_qs(parsed.query), self._posts(all_rows=True)["items"]))
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
