from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from .ai import get_api_key
from .db import connect

SCHEMA = {
    "type": "object", "additionalProperties": False,
    "properties": {
        "title": {"type": "string"},
        "thesis": {"type": "string"},
        "sections": {"type": "array", "items": {"type": "object", "additionalProperties": False,
            "properties": {"heading": {"type": "string"}, "text": {"type": "string"},
                           "evidence_ids": {"type": "array", "items": {"type": "string"}}},
            "required": ["heading", "text", "evidence_ids"]}},
        "alternative_explanations": {"type": "array", "items": {"type": "object", "additionalProperties": False,
            "properties": {"explanation": {"type": "string"}, "evidence_ids": {"type": "array", "items": {"type": "string"}}},
            "required": ["explanation", "evidence_ids"]}},
        "what_would_change_mind": {"type": "string"},
        "open_questions": {"type": "array", "items": {"type": "string"}},
        "conclusion": {"type": "string"}
    },
    "required": ["title", "thesis", "sections", "alternative_explanations", "what_would_change_mind", "open_questions", "conclusion"]
}


def _response_text(response: dict) -> dict:
    for item in response.get("output", []):
        for block in item.get("content", []):
            if block.get("type") == "output_text":
                return json.loads(block["text"])
    raise ValueError("ANALYSIS_RESPONSE_EMPTY")


def _collect_evidence(db, story_ids: set[int], max_sources: int = 18) -> list[dict]:
    evidence, seen = [], set()
    for story_id in story_ids:
        rows = db.execute(
            "SELECT i.item_id,i.title,i.published_at,i.primary_source_json,a.result_json "
            "FROM items i JOIN item_analysis a USING(item_id) WHERE i.story_id=? "
            "ORDER BY COALESCE(i.published_at,i.discovered_at) DESC LIMIT 12", (story_id,)
        ).fetchall()
        for row in rows:
            try:
                primary = json.loads(row["primary_source_json"] or "{}")
                analysis = json.loads(row["result_json"] or "{}")
            except (TypeError, json.JSONDecodeError):
                continue
            url = primary.get("url")
            content = re.sub(r"\s+", " ", str(primary.get("content") or "")).strip()
            if primary.get("status") != "READ" or not url or not content or url in seen:
                continue
            seen.add(url)
            host = (urlsplit(url).hostname or "").lower().removeprefix("www.")
            evidence.append({"evidence_id": f"E{len(evidence)+1}", "story_id": story_id,
                             "item_id": row["item_id"], "title": row["title"],
                             "published_at": row["published_at"], "publisher": primary.get("publisher") or host,
                             "url": url, "host": host, "content": content[:3000],
                             "reported_facts": analysis.get("facts", []),
                             "summary": analysis.get("summary_ru", ""),
                             "independent_check": analysis.get("independent_check", "NOT_ASSESSED")})
            if len(evidence) >= max_sources:
                return evidence
    return evidence


def _format_body(document: dict) -> str:
    lines = [document["title"].strip(), "", f"**Главный тезис:** {document['thesis'].strip()}"]
    for section in document["sections"]:
        refs = ", ".join(section["evidence_ids"])
        lines.extend(["", f"**{section['heading'].strip()}**", section["text"].strip() + (f" [{refs}]" if refs else "")])
    if document["alternative_explanations"]:
        lines.extend(["", "**Другие объяснения**"])
        for alternative in document["alternative_explanations"]:
            refs = ", ".join(alternative["evidence_ids"])
            lines.append(f"• {alternative['explanation'].strip()}" + (f" [{refs}]" if refs else ""))
    lines.extend(["", f"**Что могло бы изменить вывод:** {document['what_would_change_mind'].strip()}"])
    if document["open_questions"]:
        lines.extend(["", "**Что пока неизвестно**"])
        lines.extend(f"• {question.strip()}" for question in document["open_questions"])
    lines.extend(["", f"**Вывод:** {document['conclusion'].strip()}"])
    return "\n".join(lines)


def generate_weekly_analysis(db_path: str, config: dict, now: datetime | None = None) -> int | None:
    """Create one evidence-linked Friday analysis draft; it is never published automatically."""
    settings = config.get("newsroom", {})
    if not settings.get("weekly_analysis_enabled", False):
        return None
    now = now or datetime.now(timezone.utc)
    local_zone = ZoneInfo(settings.get("digest_timezone", "Europe/Moscow"))
    local_now = now.astimezone(local_zone)
    try:
        hour, minute = (int(part) for part in settings.get("weekly_analysis_time", "20:00").split(":"))
    except (TypeError, ValueError):
        hour, minute = 20, 0
    due = local_now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if local_now.weekday() != 4 or local_now < due:
        return None
    local_day = local_now.date().isoformat()
    db = connect(db_path)
    attempt = db.execute("SELECT value FROM app_state WHERE key='weekly_analysis_last_attempt'").fetchone()
    if attempt and attempt["value"] == local_day:
        db.close()
        return None
    db.execute("INSERT OR REPLACE INTO app_state(key,value) VALUES('weekly_analysis_last_attempt',?)", (local_day,))
    db.commit()
    cutoff = (now - timedelta(hours=max(1, int(settings.get("weekly_analysis_lookback_hours", 168))))).isoformat(timespec="seconds")
    published = db.execute(
        "SELECT DISTINCT p.story_id,p.fact_check_result,s.headline,s.latest_information "
        "FROM posts p JOIN stories s USING(story_id) WHERE p.status='PUBLISHED' AND p.published_at>? AND p.published_at<=?",
        (cutoff, now.isoformat(timespec="seconds")),
    ).fetchall()
    story_ids = set()
    for row in published:
        try:
            facts = json.loads(row["fact_check_result"] or "{}")
        except (TypeError, json.JSONDecodeError):
            continue
        if (facts.get("primary_source_status") == "READ" and facts.get("russia_cis_impact") == "DIRECT"
                and facts.get("source_review_required") is not True and facts.get("independent_check") != "CONFLICT"):
            story_ids.add(row["story_id"])
    evidence = _collect_evidence(db, story_ids)
    period_start = cutoff
    period_end = now.isoformat(timespec="seconds")
    if len(story_ids) < 3 or len(evidence) < 4 or len({item["host"] for item in evidence}) < 3:
        db.execute("INSERT OR REPLACE INTO app_state(key,value) VALUES('weekly_analysis_last_status',?)", ("INSUFFICIENT_EVIDENCE",))
        db.commit(); db.close()
        return None
    api_key = get_api_key(config.get("ai", {}))
    if not api_key:
        db.execute("INSERT OR REPLACE INTO app_state(key,value) VALUES('weekly_analysis_last_status',?)", ("AI_CREDENTIALS_MISSING",))
        db.commit(); db.close()
        return None
    published_context = [{"story_id": row["story_id"], "headline": row["headline"],
                          "latest_information": row["latest_information"][:1200]} for row in published
                         if row["story_id"] in story_ids]
    request = {"model": config.get("ai", {}).get("model", "gpt-6-luna"), "store": False,
        "max_output_tokens": int(settings.get("weekly_analysis_max_output_tokens", 2400)),
        "instructions": (
            "Ты автор-аналитик редакции. Создай содержательный русский аналитический черновик, не перечень новостей. "
            "Связывай события только причинными/временными отношениями, которые подтверждаются переданными первоисточниками. "
            "Не выдумывай мотивы, причинность, договорённости, числа или скрытые связи. Гипотезы явно называй гипотезами; "
            "включай конкурирующие объяснения, контраргументы и то, что могло бы опровергнуть главный тезис. "
            "Каждый раздел обязан ссылаться на ID доказательств из набора. Цитаты и факты должны быть дословно/точно "
            "поддержаны текстами первоисточников. Если общего устойчивого вывода нет, прямо скажи, что фактов пока мало. "
            "Текст пригоден для авторского поста, без канцелярита и самоуверенных прогнозов. Это редакторский черновик, не пост к автопубликации."
        ),
        "input": json.dumps({"published_stories": published_context, "read_primary_sources": evidence}, ensure_ascii=False),
        "text": {"format": {"type": "json_schema", "name": "weekly_analysis_draft", "strict": True, "schema": SCHEMA}}}
    req = urllib.request.Request("https://api.openai.com/v1/responses", data=json.dumps(request, ensure_ascii=False).encode(),
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=int(config.get("ai", {}).get("timeout_seconds", 45))) as response:
            result = _response_text(json.loads(response.read()))
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, ValueError, KeyError, json.JSONDecodeError):
        db.execute("INSERT OR REPLACE INTO app_state(key,value) VALUES('weekly_analysis_last_status',?)", ("GENERATION_FAILED",))
        db.commit(); db.close()
        return None
    available = {item["evidence_id"] for item in evidence}
    references = {ref for section in result["sections"] for ref in section["evidence_ids"]}
    references.update(ref for alt in result["alternative_explanations"] for ref in alt["evidence_ids"])
    if (not result["sections"] or not references or not references.issubset(available)
            or any(not section["evidence_ids"] for section in result["sections"])):
        db.execute("INSERT OR REPLACE INTO app_state(key,value) VALUES('weekly_analysis_last_status',?)", ("INVALID_EVIDENCE_LINKS",))
        db.commit(); db.close()
        return None
    body = _format_body(result)
    cur = db.execute("""INSERT INTO weekly_analysis_drafts(created_at,period_start,period_end,title,thesis,body,analysis_json,source_json,status)
                        VALUES(?,?,?,?,?,?,?,?, 'NEEDS_REVIEW')""",
        (now.isoformat(timespec="seconds"), period_start, period_end, result["title"], result["thesis"], body,
         json.dumps(result, ensure_ascii=False), json.dumps(evidence, ensure_ascii=False)))
    db.execute("INSERT OR REPLACE INTO app_state(key,value) VALUES('weekly_analysis_last_status',?)", ("DRAFT_READY",))
    db.commit(); draft_id = cur.lastrowid; db.close()
    return draft_id
