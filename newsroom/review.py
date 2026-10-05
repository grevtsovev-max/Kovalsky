from __future__ import annotations

import difflib
import json
import os
import re
import sys
import time
from datetime import datetime, timezone

from .db import connect
from .cli import telegram_api


def _handle_callback(config: dict, db, update: dict) -> None:
    callback = update.get("callback_query") or {}
    callback_id = callback.get("id", "")
    if callback_id:
        telegram_api(config, "answerCallbackQuery", {
            "callback_query_id": callback_id,
            "text": "Ручное одобрение отключено. Публикация проходит автоматические проверки.",
            "show_alert": True,
        })


REVIEW_ALLOWED_UPDATES = ["message", "callback_query", "channel_post", "edited_channel_post"]


def _handle_message(config: dict, db, update: dict) -> None:
    message = dict(update.get("message") or {})
    message["_update_id"] = update.get("update_id")
    _handle_interest_message(config, db, message)


def _utf16_index(text: str, offset: int) -> int:
    units = 0
    for index, char in enumerate(text):
        if units >= offset:
            return index
        units += 2 if ord(char) > 0xFFFF else 1
    return len(text)


def _edited_message_text(message: dict) -> str:
    text = str(message.get("text") or message.get("caption") or "")
    entities = message.get("entities") or message.get("caption_entities") or []
    links = [entity for entity in entities if entity.get("type") == "text_link" and entity.get("url")]
    for entity in sorted(links, key=lambda value: int(value.get("offset", 0)), reverse=True):
        start = _utf16_index(text, int(entity.get("offset", 0)))
        end = _utf16_index(text, int(entity.get("offset", 0)) + int(entity.get("length", 0)))
        label = text[start:end]
        text = text[:start] + f"[{label}]({entity['url']})" + text[end:]
    return text


def _comparable_post_text(text: str) -> str:
    text = re.sub(r"\*\*(.*?)\*\*", r"\1", text, flags=re.DOTALL)
    text = re.sub(r'(?m)^(Источник|Источники|Ранее): (.+?) [—–] (https?://\S+)\s*$', r'\1: [\2](\3)', text)
    return re.sub(r"\s+", " ", text).strip()


def _handle_edited_channel_post(config: dict, db, update: dict) -> None:
    message = update.get("edited_channel_post") or {}
    try:
        update_id = int(update.get('update_id'))
    except (TypeError, ValueError):
        return
    capture_channel_edit(config, db, chat_id=(message.get('chat') or {}).get('id'),
                         message_id=message.get('message_id'), edited_text=_edited_message_text(message),
                         edit_date=message.get('edit_date'), update_id=update_id)


def capture_channel_edit(config, db, *, chat_id, message_id, edited_text,
                         edit_date=None, update_id=None, source_url=None, previous_text=None):
    """Store a real Telegram edit or an explicitly labelled public snapshot.

    Negative keys are local snapshot IDs, never claimed to be Telegram updates.
    """
    settings = config.get("telegram", {})
    target_chat = os.getenv(settings.get("chat_id_env", "TELEGRAM_CHAT_ID")) or settings.get("chat_id")
    capture_source = 'TELEGRAM_UPDATE' if update_id is not None else 'PUBLIC_CHANNEL_SNAPSHOT'
    if update_id is None:
        if not source_url:
            return
        update_id = min(0, db.execute('SELECT COALESCE(MIN(update_id),0) FROM telegram_post_edits').fetchone()[0]) - 1
    try:
        message_id = str(int(message_id))
    except (TypeError, ValueError):
        return
    if not target_chat or str(chat_id) != str(target_chat):
        return
    row = db.execute(
        "SELECT p.post_id,p.story_id,p.text,s.headline FROM posts p "
        "JOIN stories s USING(story_id) WHERE p.status='PUBLISHED' AND p.external_id=?",
        (message_id,),
    ).fetchone()
    if not row or db.execute("SELECT 1 FROM telegram_post_edits WHERE update_id=?", (update_id,)).fetchone():
        return
    if not edited_text:
        return
    auto_intent = db.execute(
        "SELECT * FROM telegram_message_edit_intents WHERE post_id=? AND status IN ('SENDING','UNKNOWN') ORDER BY created_at DESC LIMIT 1",
        (row["post_id"],),
    ).fetchone()
    if auto_intent and _comparable_post_text(auto_intent["new_text"]) == _comparable_post_text(edited_text):
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        db.execute("UPDATE telegram_message_edit_intents SET status='CONFIRMED',response_json=?,updated_at=? WHERE correction_id=?",
                   (json.dumps({"confirmation": "observed_channel_edit", "message_id": message_id}, ensure_ascii=False), now, auto_intent["correction_id"]))
        db.execute("UPDATE telegram_feedback_corrections SET status='EDITED',result_code='CORRECTED',result_summary='Правка подтверждена сверкой с каналом.',corrected_text=?,notice_status='PENDING',updated_at=? WHERE correction_id=?",
                   (edited_text, now, auto_intent["correction_id"]))
        capture_source = "AUTOMATED_CORRECTION_CONFIRMED"
        db.commit()
    prior_edit = db.execute(
        "SELECT * FROM telegram_post_edits WHERE post_id=? ORDER BY captured_at DESC, ABS(update_id) DESC LIMIT 1",
        (row["post_id"],),
    ).fetchone()
    if prior_edit and _comparable_post_text(prior_edit['edited_text']) == _comparable_post_text(edited_text):
        return  # Bot event and public snapshot may arrive in either order.
    if previous_text is None:
        previous_text = prior_edit["edited_text"] if prior_edit else row["text"]
    if prior_edit and prior_edit['capture_source'] == 'PUBLIC_CHANNEL_SNAPSHOT' and edit_date:
        try:
            if datetime.fromtimestamp(int(edit_date),timezone.utc) <= datetime.fromisoformat(prior_edit['captured_at']):
                return  # A delayed event must not revert a more recent observed snapshot.
        except (TypeError,ValueError,OverflowError):
            pass
    if _comparable_post_text(previous_text) == _comparable_post_text(edited_text):
        return
    now = datetime.now(timezone.utc).isoformat()
    try:
        edit_date = datetime.fromtimestamp(int(edit_date), timezone.utc).isoformat(timespec="seconds")
    except (TypeError, ValueError, OSError, OverflowError):
        edit_date = ''  # Public snapshots reveal observation time, not Telegram's edit time.
    diff = "\n".join(difflib.unified_diff(
        previous_text.splitlines(), edited_text.splitlines(),
        fromfile="прежняя версия", tofile="исправленная версия", lineterm="",
    ))
    if auto_intent and capture_source == "AUTOMATED_CORRECTION_CONFIRMED":
        learning_summary = ""
        reason = ""
    else:
        try:
            from .interests import summarize_editorial_edit
            lessons = summarize_editorial_edit(previous_text, edited_text, config.get("ai", {}))
        except Exception:
            lessons = []
        if not lessons:
            lessons = ["Правка сохранена как редакторский пример; ответьте на сообщение, если нужно уточнить конкретное правило."]
        learning_summary = "\n".join(f"{index}. {lesson}" for index, lesson in enumerate(lessons, 1))
        reason = ("Редактор изменил опубликованный пост в Telegram. Возможный урок, автоматически выведенный из правки "
                  "(может быть уточнён владельцем):\n" + learning_summary + "\n\nИзменения:\n" + (diff or "Текст обновлён."))
    item = db.execute("SELECT item_id FROM items WHERE story_id=? ORDER BY discovered_at DESC LIMIT 1",
                      (row["story_id"],)).fetchone()
    db.execute(
        "INSERT INTO telegram_post_edits(update_id,post_id,telegram_chat_id,telegram_message_id,edit_date,previous_text,edited_text,captured_at,capture_source,source_url) "
        "VALUES(?,?,?,?,?,?,?,?,?,?)",
        (update_id, row["post_id"], str(chat_id), message_id, edit_date, previous_text, edited_text, now,capture_source,source_url),
    )
    if not (auto_intent and capture_source == "AUTOMATED_CORRECTION_CONFIRMED"):
        db.execute(
            "INSERT INTO editorial_feedback(created_at,item_id,story_id,post_id,feedback_type,reason,item_title,post_text) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (now, item["item_id"] if item else None, row["story_id"], row["post_id"],
             "TELEGRAM_EDIT", reason[:5000], row["headline"] or "", previous_text[:5000]),
        )
        owners = {str(value) for value in config.get("telegram", {}).get("interest_owner_user_ids", [])}
        for owner_id in owners:
            db.execute(
                "INSERT OR IGNORE INTO telegram_edit_acknowledgements(update_id,telegram_user_id,learning_summary,status,created_at) "
                "VALUES(?,?,?,'PENDING',?)",
                (update_id, owner_id, learning_summary, now),
            )
    db.commit()  # Learning survives interruption or notification failure.
    flush_edit_acknowledgements(config, db, update_id=update_id)
    return update_id


def flush_edit_acknowledgements(config, db, update_id=None):
    from .delivery import deliver, TelegramReceipt, DeliveryUncertain
    rows = db.execute("SELECT a.*,e.edited_text FROM telegram_edit_acknowledgements a JOIN telegram_post_edits e USING(update_id) "
                      "WHERE a.status IN ('PENDING','SEND_FAILED','SENDING') AND (? IS NULL OR a.update_id=?)",
                      (update_id,update_id)).fetchall()
    for ack in rows:
        owner_id = ack['telegram_user_id']
        title = ack['edited_text'].splitlines()[0][:140]
        notice = (f"Пост «{title}» скорректирован в канале — учёл эту правку для будущих публикаций:\n"
                  f"{ack['learning_summary']}\n\nЕсли вывод верный, ответьте «Верно». Если я понял правку неточно, ответьте на это сообщение и напишите, что именно нужно изменить.")[:3900]
        delivery_config = {**config, 'telegram': {**config.get('telegram',{}),
                           'chat_id': owner_id, 'chat_id_env': '_KOVALSKY_EDIT_ACK_TARGET'}}
        def send_notice(send_config, text):
            return TelegramReceipt(telegram_api(send_config,'sendMessage',{'chat_id':owner_id,'text':text}))
        try:
            sent_message_id = int(deliver(db,delivery_config,f"edit-ack:{ack['acknowledgement_id']}",notice,send_notice))
        except DeliveryUncertain:
            db.execute("UPDATE telegram_edit_acknowledgements SET status='SEND_UNKNOWN' WHERE acknowledgement_id=?",(ack['acknowledgement_id'],))
        except Exception:
            db.execute("UPDATE telegram_edit_acknowledgements SET status='SEND_FAILED' WHERE acknowledgement_id=?",
                       (ack["acknowledgement_id"],))
        else:
            db.execute("UPDATE telegram_edit_acknowledgements SET telegram_message_id=?,status='SENT' WHERE acknowledgement_id=?",
                       (sent_message_id, ack["acknowledgement_id"]))
        db.commit()


def _handle_edit_ack_reply(config: dict, db, update: dict, message: dict, user_id: str) -> bool:
    reply_to = message.get("reply_to_message") or {}
    try:
        update_id = int(update.get("update_id"))
        bot_message_id = int(reply_to.get("message_id"))
    except (TypeError, ValueError):
        return False
    ack = db.execute(
        "SELECT a.acknowledgement_id,a.learning_summary,e.post_id,p.story_id,e.edited_text,s.headline "
        "FROM telegram_edit_acknowledgements a JOIN telegram_post_edits e USING(update_id) "
        "JOIN posts p USING(post_id) JOIN stories s USING(story_id) "
        "WHERE a.telegram_user_id=? AND a.telegram_message_id=? AND a.status='SENT'",
        (user_id, bot_message_id),
    ).fetchone()
    if not ack:
        return False
    if message.get("forward_origin"):
        return False
    if db.execute("SELECT 1 FROM telegram_edit_replies WHERE update_id=?", (update_id,)).fetchone():
        return True
    reply_text = str(message.get("text") or message.get("caption") or "").strip()[:2000]
    if not reply_text:
        return False
    normalized = re.sub(r"[^\wё]+", " ", reply_text.lower(), flags=re.UNICODE).strip()
    is_confirmation = reply_text.strip() in {"👍", "✅"} or normalized in {"верно", "все верно", "всё верно", "да верно", "правильно", "именно", "подтверждаю", "да"}
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    db.execute("INSERT INTO telegram_edit_replies(update_id,acknowledgement_id,reply_text,is_confirmation,created_at) VALUES(?,?,?,?,?)",
               (update_id, ack["acknowledgement_id"], reply_text, int(is_confirmation), now))
    db.execute("UPDATE telegram_edit_acknowledgements SET status=? WHERE acknowledgement_id=?",
               ("CONFIRMED" if is_confirmation else "REFINED", ack["acknowledgement_id"]))
    item = db.execute("SELECT item_id FROM items WHERE story_id=? ORDER BY discovered_at DESC LIMIT 1", (ack["story_id"],)).fetchone()
    if is_confirmation:
        feedback_type = "TELEGRAM_EDIT_CONFIRMATION"
        reason = "Владелец подтвердил вывод бота о редакторской правке:\n" + ack["learning_summary"]
        response = "Спасибо, отметил, что понял правку верно. Буду использовать это правило в следующих подходящих разборах."
    else:
        feedback_type = "TELEGRAM_EDIT_REFINEMENT"
        reason = ("Владелец уточнил вывод бота о редакторской правке. Вывод бота:\n" + ack["learning_summary"] +
                  "\n\nУточнение владельца (приоритетное):\n" + reply_text)
        response = "Уточнение сохранено. В будущих разборах буду учитывать вашу формулировку; если нужно, можете ответить на исходное сообщение ещё раз с дополнительной деталью."
    db.execute(
        "INSERT INTO editorial_feedback(created_at,item_id,story_id,post_id,feedback_type,reason,item_title,post_text) "
        "VALUES(?,?,?,?,?,?,?,?)",
        (now, item["item_id"] if item else None, ack["story_id"], ack["post_id"], feedback_type,
         reason[:5000], ack["headline"] or "", ack["edited_text"][:5000]),
    )
    try:
        telegram_api(config, "sendMessage", {"chat_id": str(message.get("chat", {}).get("id")), "text": response})
    except Exception:
        pass
    return True


_TELEGRAM_POST_URL = re.compile(
    r"https?://(?:www\.)?(?:t\.me|telegram\.me)/(?:s/)?"
    r"(?P<channel>c/\d+|[A-Za-z0-9_]+)/(?P<message_id>\d+)(?:\?[^\s]*)?",
    re.IGNORECASE,
)


def _feedback_link_and_text(message: dict) -> tuple[str | None, str]:
    text = str(message.get("text") or message.get("caption") or "").strip()
    entities = message.get("entities") or message.get("caption_entities") or []
    spans: list[tuple[int, int, str]] = []
    for entity in entities:
        kind = entity.get("type")
        if kind == "text_link" and entity.get("url"):
            start = _utf16_index(text, int(entity.get("offset", 0)))
            end = _utf16_index(text, int(entity.get("offset", 0)) + int(entity.get("length", 0)))
            spans.append((start, end, str(entity["url"])))
        elif kind == "url":
            start = _utf16_index(text, int(entity.get("offset", 0)))
            end = _utf16_index(text, int(entity.get("offset", 0)) + int(entity.get("length", 0)))
            spans.append((start, end, text[start:end]))
    spans.extend((match.start(), match.end(), match.group(0)) for match in _TELEGRAM_POST_URL.finditer(text))
    unique: dict[str, tuple[int, int]] = {}
    for start, end, raw_url in spans:
        url = raw_url.strip().rstrip(".,);]")
        if _TELEGRAM_POST_URL.fullmatch(url):
            unique[url] = (start, end)
    if len(unique) != 1:
        return None, ""
    url = next(iter(unique))
    clean_text = text
    for start, end in sorted(unique.values(), reverse=True):
        clean_text = clean_text[:start] + " " + clean_text[end:]
    clean_text = re.sub(r"(?im)^\s*(?:/feedback\s+)?(?:фидбэк|фидбек|отзыв|комментарий)\s*:\s*", "", clean_text)
    clean_text = re.sub(r"(?im)^\s*/feedback\s*", "", clean_text)
    clean_text = re.sub(r"\s+", " ", clean_text).strip(" \t\r\n—–-:;,.!")
    return url, clean_text


def _handle_link_feedback(config: dict, db, message: dict) -> bool:
    url, reason = _feedback_link_and_text(message)
    if not url:
        return False
    chat_id = str((message.get("chat") or {}).get("id", ""))

    def reply(text: str) -> None:
        telegram_api(config, "sendMessage", {"chat_id": chat_id, "text": text})

    try:
        update_id = int(message.get("_update_id"))
    except (TypeError, ValueError):
        update_id = None
    if update_id is not None and db.execute(
        "SELECT 1 FROM telegram_link_feedback WHERE update_id=?", (update_id,)
    ).fetchone():
        reply("Спасибо, отзыв уже сохранён и будет учитываться в следующих подходящих разборах.")
        return True

    match = _TELEGRAM_POST_URL.fullmatch(url)
    if not match:
        return False
    if len(reason) < 5 or len(reason) > 2000:
        reply("Добавьте отзыв длиной от 5 до 2000 символов рядом со ссылкой на пост.")
        return True
    settings = config.get("telegram", {})
    target_chat = os.getenv(settings.get("chat_id_env", "TELEGRAM_CHAT_ID")) or settings.get("chat_id")
    if not target_chat:
        reply("Не удалось определить канал публикаций, отзыв не сохранён.")
        return True
    try:
        target = telegram_api(config, "getChat", {"chat_id": target_chat})
    except Exception:
        reply("Не удалось проверить канал по ссылке. Отзыв пока не сохранён; попробуйте позже.")
        return True
    channel = match.group("channel")
    target_id = str(target.get("id", target_chat))
    if channel.casefold().startswith("c/"):
        if channel.split("/", 1)[1] != target_id.removeprefix("-100"):
            reply("Эта ссылка ведёт не на канал Kovalsky. Отзыв не сохранён.")
            return True
    elif channel.casefold() != str(target.get("username", "")).casefold():
        reply("Эта ссылка ведёт не на канал Kovalsky. Отзыв не сохранён.")
        return True
    post = db.execute(
        "SELECT p.post_id,p.story_id,p.origin_item_id,p.text,s.headline FROM posts p "
        "JOIN stories s USING(story_id) WHERE p.status='PUBLISHED' AND p.external_id=? "
        "ORDER BY p.post_id DESC LIMIT 1", (match.group("message_id"),)
    ).fetchone()
    if not post:
        reply("Не нашёл эту публикацию среди отправленных постов. Проверьте ссылку.")
        return True
    latest_edit = db.execute(
        "SELECT edited_text FROM telegram_post_edits WHERE post_id=? "
        "ORDER BY captured_at DESC, ABS(update_id) DESC LIMIT 1", (post["post_id"],)
    ).fetchone()
    item = db.execute("SELECT title FROM items WHERE item_id=?", (post["origin_item_id"],)).fetchone() if post["origin_item_id"] else None
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    cursor = db.execute(
        "INSERT INTO editorial_feedback(created_at,item_id,story_id,post_id,feedback_type,reason,item_title,post_text) "
        "VALUES(?,?,?,?,?,?,?,?)",
        (now, post["origin_item_id"], post["story_id"], post["post_id"], "TELEGRAM_LINK_FEEDBACK",
         f"Обратная связь владельца к опубликованному посту ({url}):\n{reason}",
         (item["title"] if item else post["headline"] or "")[:1000],
         (latest_edit["edited_text"] if latest_edit else post["text"])[:5000]),
    )
    if update_id is not None:
        db.execute("INSERT INTO telegram_link_feedback(update_id,feedback_id,telegram_user_id,created_at) VALUES(?,?,?,?)",
                   (update_id, cursor.lastrowid, str((message.get("from") or {}).get("id", "")), now))
    db.execute(
        "INSERT INTO telegram_feedback_corrections(feedback_id,post_id,owner_chat_id,created_at,updated_at) "
        "VALUES(?,?,?,?,?)", (cursor.lastrowid, post["post_id"], chat_id, now, now)
    )
    db.commit()
    reply("Принял отзыв. Сверю его с прочитанным материалом и проверю точечную правку. Если ошибка подтвердится, автоматически исправлю этот же пост и пришлю результат.")
    return True


def _finish_correction(db, correction_id: int, status: str, code: str,
                       summary: str, *, previous_text: str | None = None,
                       corrected_text: str | None = None, evidence: list | None = None) -> None:
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    db.execute(
        "UPDATE telegram_feedback_corrections SET status=?,result_code=?,result_summary=?,previous_text=COALESCE(?,previous_text),"
        "corrected_text=COALESCE(?,corrected_text),evidence_json=?,notice_status='PENDING',updated_at=? WHERE correction_id=?",
        (status, code, summary[:1000], previous_text, corrected_text,
         json.dumps(evidence or [], ensure_ascii=False), now, correction_id),
    )
    db.commit()


def _normalised_source_quote(value: str) -> str:
    return " ".join(str(value or "").casefold().split())


def _make_feedback_correction(config: dict, db, row) -> tuple[str, str, str | None, str | None, list]:
    post = db.execute("SELECT * FROM posts WHERE post_id=? AND status='PUBLISHED'", (row["post_id"],)).fetchone()
    if not post or not post["external_id"]:
        return "REJECTED", "POST_NOT_PUBLISHED", "Не нашёл действующую публикацию для исправления.", None, []
    latest_edit = db.execute(
        "SELECT edited_text FROM telegram_post_edits WHERE post_id=? ORDER BY captured_at DESC,ABS(update_id) DESC LIMIT 1",
        (post["post_id"],),
    ).fetchone()
    current_text = latest_edit["edited_text"] if latest_edit else post["text"]
    db.execute("UPDATE telegram_feedback_corrections SET previous_text=COALESCE(previous_text,?),updated_at=? WHERE correction_id=?",
               (current_text, datetime.now(timezone.utc).isoformat(timespec="seconds"), row["correction_id"]))
    db.commit()
    item = db.execute("SELECT * FROM items WHERE item_id=?", (post["origin_item_id"],)).fetchone() if post["origin_item_id"] else None
    if not item:
        return "REJECTED", "SOURCE_ITEM_MISSING", "Не удалось восстановить исходный материал к этому посту.", current_text, []
    try:
        facts = json.loads(post["fact_check_result"] or "{}")
        item_source = json.loads(item["primary_source_json"] or "{}")
    except (TypeError, json.JSONDecodeError):
        facts, item_source = {}, {}
    primary = dict(facts.get("primary_source") or {})
    report = dict(facts.get("publisher_report") or {})
    if not primary.get("content") and item_source.get("content"):
        primary["content"] = item_source["content"]
        primary.setdefault("url", item_source.get("url"))
        primary.setdefault("publisher", item_source.get("publisher") or item_source.get("_material_publisher"))
        primary.setdefault("status", item_source.get("status"))
    source = primary if primary.get("content") and facts.get("primary_source_status") == "READ" else report
    if not source.get("content") or not source.get("url"):
        return "REJECTED", "READ_SOURCE_UNAVAILABLE", "Не исправлял пост: в базе нет прочитанного текста источника, по которому можно подтвердить отзыв.", current_text, []
    if source["url"] not in current_text:
        return "REJECTED", "SOURCE_LINK_MISSING", "Не исправлял пост: не удалось подтвердить, какой прочитанный источник указан в его ссылке.", current_text, []
    if source is report and not (facts.get("publisher_report_exception") is True and report.get("material_read") is True):
        return "REJECTED", "REPORT_NOT_READ", "Не исправлял пост: прочитанный текст источника не подтверждён в истории публикации.", current_text, []
    feedback = db.execute("SELECT reason FROM editorial_feedback WHERE feedback_id=?", (row["feedback_id"],)).fetchone()
    feedback_text = feedback["reason"] if feedback else ""
    reference_match = re.search(r"\n\[BACKEND_REFERENCE_MESSAGE_ID:(\d+)\]$", feedback_text)
    reference_post = None
    if reference_match:
        reference_post = db.execute(
            "SELECT post_id,external_id,text FROM posts WHERE external_id=? AND status='PUBLISHED' LIMIT 1",
            (reference_match.group(1),),
        ).fetchone()
        if not reference_post or str(reference_post["external_id"]) == str(post["external_id"]):
            return "REJECTED", "REFERENCE_POST_UNAVAILABLE", "Не менял пост: подробная публикация для ссылки не подтверждена.", current_text, []
        feedback_text = feedback_text[:reference_match.start()].strip()
    from .ai import correct_published_post
    from .quality import editorial_issues, publication_source_ready
    from .cli import telegram_format_text
    if not publication_source_ready(facts, current_text):
        return "REJECTED", "ORIGINAL_SOURCE_GATE_FAILED", "Не исправлял пост: исходная публикация не проходит проверку сохранённого источника.", current_text, []
    last_summary = ""
    for attempt in range(1, 4):
        db.execute("UPDATE telegram_feedback_corrections SET attempt_count=?,updated_at=? WHERE correction_id=?",
                   (attempt, datetime.now(timezone.utc).isoformat(timespec="seconds"), row["correction_id"]))
        db.commit()
        try:
            proposal = correct_published_post(
                current_text, feedback_text,
                {"title": item["title"], "content": item["content"]},
                source, config.get("ai", {}),
            )
        except Exception as exc:
            last_summary = f"Проверка временно не завершилась ({type(exc).__name__})."
            continue
        decision = proposal.get("decision")
        last_summary = str(proposal.get("summary") or "")
        if decision == "NO_CHANGE":
            return "NO_CHANGE", "NO_CHANGE", last_summary or "Проверил отзыв; ошибка в посте не подтвердилась.", current_text, []
        if decision == "UNSUPPORTED":
            return "REJECTED", "FEEDBACK_UNSUPPORTED", last_summary or "В прочитанном материале нет подтверждения для изменения.", current_text, []
        if decision not in {"EDIT", "RETRY"}:
            continue
        changes = proposal.get("changes") or []
        if not changes or len(changes) > 5:
            last_summary = "Модель не смогла выделить точечные замены."
            continue
        revised = current_text
        evidence_used = []
        valid = True
        spans = []
        for change in changes:
            old = str(change.get("old_text") or "")
            new = str(change.get("new_text") or "")
            if (not old or not new or revised.count(old) != 1 or len(old) > 700 or len(new) > 700
                    or re.search(r"(?i)(?:Источник:|Источники:|Ранее:|https?://|\]\()", old + new)):
                valid = False
                last_summary = "Замена не ограничена одним фрагментом основного текста."
                break
            start = revised.index(old)
            spans.append((start, start + len(old)))
            kind = change.get("edit_type")
            quote = str(change.get("evidence_quote") or "").strip()
            if kind in {"FACTUAL", "STRUCTURAL"}:
                if (len(quote) < 16 or _normalised_source_quote(quote) not in
                        _normalised_source_quote(source["content"])):
                    valid = False
                    last_summary = "Не нашёл точную цитату из прочитанного источника для фактологической правки."
                    break
                evidence_used.append({"claim": new, "quote": quote, "source_url": source["url"]})
                if kind == "STRUCTURAL" and not any(
                        _normalised_source_quote(fragment) in _normalised_source_quote(source["content"])
                        for fragment in re.split(r"(?<=[.!?])\s+", new) if len(fragment.strip()) >= 16):
                    valid = False
                    last_summary = "Структурная правка добавляет формулировку без подтверждения в источнике."
                    break
            elif kind != "COPYEDIT" or quote:
                valid = False
                last_summary = "Тип одной из правок не подтверждён."
                break
            revised = revised.replace(old, new, 1)
        if valid and any(a < d and c < b for index, (a, b) in enumerate(spans)
                         for c, d in spans[index + 1:]):
            valid = False
            last_summary = "Замены пересекаются и не могут быть безопасно применены."
        if not valid:
            continue
        if reference_post:
            from .cli import _previous_story_label, _telegram_message_url
            reference_url = _telegram_message_url(config, str(reference_post["external_id"]))
            if not reference_url:
                last_summary = "Не удалось сформировать проверенную ссылку на подробную публикацию."
                continue
            reference_line = f"Ранее: [{_previous_story_label(reference_post['text'])}]({reference_url})"
            lines = revised.splitlines()
            prior_lines = [index for index, line in enumerate(lines) if line.startswith("Ранее:")]
            source_lines = [index for index, line in enumerate(lines) if line.startswith(("Источник:", "Источники:"))]
            if len(prior_lines) > 1 or len(source_lines) != 1:
                last_summary = "Не удалось однозначно заменить ссылку на предыдущий разбор."
                continue
            if prior_lines:
                lines[prior_lines[0]] = reference_line
            else:
                lines.insert(source_lines[0], reference_line)
            revised = "\n".join(lines)
        checks = proposal.get("editorial_check") or {}
        audit_fields = ("source_matches_event", "attribution_preserved", "stage_preserved",
                        "headline_main_event", "lead_event_first", "paragraphs_concise_distinct",
                        "no_editorial_process_notes")
        if any(checks.get(key) is not True for key in audit_fields):
            last_summary = "Итоговый текст не прошёл редакционные проверки."
            continue
        original_source_lines = [line for line in current_text.splitlines() if line.startswith(("Источник:", "Источники:"))]
        revised_source_lines = [line for line in revised.splitlines() if line.startswith(("Источник:", "Источники:"))]
        if original_source_lines != revised_source_lines:
            last_summary = "Ссылка на использованный источник изменилась."
            continue
        headline, _, body = revised.partition("\n")
        check_facts = dict(facts)
        check_facts["event_status"] = proposal.get("event_status", facts.get("event_status"))
        check_facts["editorial_check"] = checks
        issues = editorial_issues(headline, body, check_facts, final_post=True)
        try:
            rendered = telegram_format_text(revised)
        except Exception:
            rendered = ""
        if issues or not rendered or len(rendered) > 4096 or len(revised) > int(config.get("newsroom", {}).get("max_post_length", 3500)):
            last_summary = "Исправленная версия не прошла структурную проверку готового поста."
            continue
        if revised == current_text:
            return "NO_CHANGE", "NO_CHANGE", last_summary or "Изменение не требуется.", current_text, []
        return "READY", "CORRECTED", last_summary or "Ошибка подтверждена прочитанным источником.", revised, evidence_used
    return "REJECTED", "CORRECTION_CHECK_FAILED", last_summary or "Не удалось подтвердить безопасную правку за три проверки.", current_text, []


def recover_feedback_correction_jobs(db) -> None:
    rows = db.execute("SELECT * FROM telegram_feedback_corrections WHERE status='PROCESSING'").fetchall()
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    for row in rows:
        intent = db.execute("SELECT * FROM telegram_message_edit_intents WHERE correction_id=?", (row["correction_id"],)).fetchone()
        if not intent:
            db.execute("UPDATE telegram_feedback_corrections SET status='QUEUED',updated_at=? WHERE correction_id=?", (now, row["correction_id"]))
        elif intent["status"] == "PREPARED":
            # The send call had not started yet, so this intent can be prepared again safely.
            db.execute("UPDATE telegram_feedback_corrections SET status='QUEUED',updated_at=? WHERE correction_id=?", (now, row["correction_id"]))
        elif intent["status"] in {"SENT", "CONFIRMED"}:
            history = db.execute("SELECT 1 FROM telegram_post_edits WHERE post_id=? AND edited_text=? LIMIT 1",
                                 (intent["post_id"], intent["new_text"])).fetchone()
            if not history:
                local_id = min(0, db.execute("SELECT COALESCE(MIN(update_id),0) FROM telegram_post_edits").fetchone()[0]) - 1
                db.execute("INSERT INTO telegram_post_edits(update_id,post_id,telegram_chat_id,telegram_message_id,edit_date,previous_text,edited_text,captured_at,capture_source,source_url) VALUES(?,?,?,?,?,?,?,?,?,?)",
                           (local_id, intent["post_id"], intent["channel_id"], intent["telegram_message_id"], "",
                            intent["previous_text"], intent["new_text"], now, "AUTOMATED_CORRECTION_RECOVERED",
                            (re.search(r"(?m)^(?:Источник|Источники): \[[^\]]+\]\((https?://[^)]+)\)$", intent["previous_text"]) or [None, ""])[1]))
            db.execute("UPDATE telegram_feedback_corrections SET status='EDITED',result_code='CORRECTED',result_summary='Правка подтверждена ответом Telegram.',corrected_text=?,notice_status='PENDING',updated_at=? WHERE correction_id=?",
                       (intent["new_text"], now, row["correction_id"]))
        elif intent["status"] == "FAILED":
            db.execute("UPDATE telegram_feedback_corrections SET status='REJECTED',result_code='TELEGRAM_EDIT_REJECTED',result_summary='Telegram отклонил правку; сообщение не изменено.',notice_status='PENDING',updated_at=? WHERE correction_id=?",
                       (now, row["correction_id"]))
        else:
            db.execute("UPDATE telegram_feedback_corrections SET status='UNKNOWN',result_code='TELEGRAM_EDIT_UNKNOWN',result_summary='Результат правки не подтверждён; повторно её не отправлял.',notice_status='PENDING',updated_at=? WHERE correction_id=?",
                       (now, row["correction_id"]))
    db.commit()


def process_feedback_corrections(config: dict, db, limit: int = 1) -> int:
    rows = db.execute("SELECT * FROM telegram_feedback_corrections WHERE status='QUEUED' ORDER BY created_at,correction_id LIMIT ?", (limit,)).fetchall()
    processed = 0
    for row in rows:
        db.execute("UPDATE telegram_feedback_corrections SET status='PROCESSING',updated_at=? WHERE correction_id=? AND status='QUEUED'",
                   (datetime.now(timezone.utc).isoformat(timespec="seconds"), row["correction_id"]))
        db.commit()
        try:
            status, code, summary, revised, evidence = _make_feedback_correction(config, db, row)
            if status != "READY":
                _finish_correction(db, row["correction_id"], status, code, summary,
                                   previous_text=revised, evidence=evidence)
                processed += 1
                continue
            post = db.execute("SELECT * FROM posts WHERE post_id=?", (row["post_id"],)).fetchone()
            from .delivery import channel
            from .cli import telegram_api, telegram_format_text
            target = channel(config)
            now = datetime.now(timezone.utc).isoformat(timespec="seconds")
            busy = db.execute("SELECT 1 FROM telegram_message_edit_intents WHERE post_id=? AND status IN ('SENDING','UNKNOWN') LIMIT 1", (post["post_id"],)).fetchone()
            if busy:
                _finish_correction(db, row["correction_id"], "REJECTED", "PRIOR_EDIT_UNRESOLVED",
                                   "Не менял пост: результат предыдущей правки ещё не подтверждён Telegram.", previous_text=post["text"])
                processed += 1
                continue
            latest_edit = db.execute("SELECT edited_text FROM telegram_post_edits WHERE post_id=? ORDER BY captured_at DESC,ABS(update_id) DESC LIMIT 1", (post["post_id"],)).fetchone()
            previous_text = latest_edit["edited_text"] if latest_edit else post["text"]
            prepared = db.execute("SELECT previous_text FROM telegram_feedback_corrections WHERE correction_id=?", (row["correction_id"],)).fetchone()
            if not prepared or prepared["previous_text"] != previous_text:
                _finish_correction(db, row["correction_id"], "REJECTED", "POST_CHANGED_DURING_CORRECTION",
                                   "Не менял пост: его текст изменился во время проверки отзыва.", previous_text=previous_text)
                processed += 1
                continue
            source_match = re.search(r"(?m)^(?:Источник|Источники): \[[^\]]+\]\((https?://[^)]+)\)$", previous_text)
            if not source_match:
                _finish_correction(db, row["correction_id"], "REJECTED", "SOURCE_FOOTER_INVALID",
                                   "Не менял пост: не удалось сохранить точную ссылку на использованный источник.", previous_text=previous_text)
                processed += 1
                continue
            existing_intent = db.execute("SELECT status FROM telegram_message_edit_intents WHERE correction_id=?", (row["correction_id"],)).fetchone()
            if existing_intent:
                if existing_intent["status"] not in {"PREPARED", "FAILED"}:
                    _finish_correction(db, row["correction_id"], "UNKNOWN", "TELEGRAM_EDIT_UNKNOWN",
                                       "Результат прежней попытки не подтверждён; новую правку не отправлял.", previous_text=previous_text)
                    processed += 1
                    continue
                db.execute("UPDATE telegram_message_edit_intents SET channel_id=?,post_id=?,telegram_message_id=?,previous_text=?,new_text=?,status='PREPARED',response_json=NULL,error_code=NULL,updated_at=? WHERE correction_id=?",
                           (target, post["post_id"], str(post["external_id"]), previous_text, revised, now, row["correction_id"]))
            else:
                db.execute("INSERT INTO telegram_message_edit_intents(correction_id,channel_id,post_id,telegram_message_id,previous_text,new_text,status,created_at,updated_at) VALUES(?,?,?,?,?,?,'PREPARED',?,?)",
                           (row["correction_id"], target, post["post_id"], str(post["external_id"]), previous_text, revised, now, now))
            db.commit()
            db.execute("UPDATE telegram_message_edit_intents SET status='SENDING',updated_at=? WHERE correction_id=?", (now, row["correction_id"]))
            db.commit()
            try:
                response = telegram_api(config, "editMessageText", {
                    "chat_id": target, "message_id": int(post["external_id"]),
                    "text": telegram_format_text(revised), "parse_mode": "HTML",
                    "disable_web_page_preview": True,
                })
            except Exception as exc:
                unknown = type(exc).__name__ == "DeliveryUncertain"
                state = "UNKNOWN" if unknown else "FAILED"
                db.execute("UPDATE telegram_message_edit_intents SET status=?,error_code=?,updated_at=? WHERE correction_id=?",
                           (state, type(exc).__name__, datetime.now(timezone.utc).isoformat(timespec="seconds"), row["correction_id"]))
                db.commit()
                _finish_correction(db, row["correction_id"], state, "TELEGRAM_EDIT_UNKNOWN" if unknown else "TELEGRAM_EDIT_REJECTED",
                                   "Telegram не подтвердил результат правки; повторно отправлять её вслепую не буду." if unknown
                                   else "Telegram отклонил правку; опубликованный текст не изменён.", previous_text=revised)
                processed += 1
                continue
            if not isinstance(response, dict) or str(response.get("message_id")) != str(post["external_id"]):
                db.execute("UPDATE telegram_message_edit_intents SET status='UNKNOWN',error_code='INVALID_EDIT_RECEIPT',updated_at=? WHERE correction_id=?",
                           (datetime.now(timezone.utc).isoformat(timespec="seconds"), row["correction_id"]))
                db.commit()
                _finish_correction(db, row["correction_id"], "UNKNOWN", "TELEGRAM_EDIT_UNKNOWN",
                                   "Telegram вернул неожиданный ответ; результат правки требует сверки.", previous_text=revised)
                processed += 1
                continue
            saved_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
            local_id = min(0, db.execute("SELECT COALESCE(MIN(update_id),0) FROM telegram_post_edits").fetchone()[0]) - 1
            db.execute("INSERT INTO telegram_post_edits(update_id,post_id,telegram_chat_id,telegram_message_id,edit_date,previous_text,edited_text,captured_at,capture_source,source_url) VALUES(?,?,?,?,?,?,?,?,?,?)",
                       (local_id, post["post_id"], target, str(post["external_id"]),
                        datetime.fromtimestamp(response["date"], timezone.utc).isoformat(timespec="seconds") if response.get("date") else "",
                        previous_text, revised, saved_at, "AUTOMATED_CORRECTION", source_match.group(1)))
            db.execute("UPDATE telegram_message_edit_intents SET status='SENT',response_json=?,updated_at=? WHERE correction_id=?",
                       (json.dumps({"message_id": response["message_id"], "date": response.get("date")}, ensure_ascii=False), saved_at, row["correction_id"]))
            db.commit()
            _finish_correction(db, row["correction_id"], "EDITED", "CORRECTED", summary,
                               previous_text=previous_text,
                               corrected_text=revised, evidence=evidence)
            processed += 1
        except Exception as exc:
            db.rollback()
            _finish_correction(db, row["correction_id"], "REJECTED", "CORRECTION_PROCESSING_ERROR",
                               f"Пост не изменён: обработка прервалась ({type(exc).__name__}).")
            processed += 1
    return processed


_DEPLOY_CORRECTION_REQUESTS = (
    {
        "key": "editorial-feedback-case-61-v1",
        "message_id": "61",
        "reference_message_id": "5",
        "reason": (
            "Этот пост повторяет уже опубликованное сообщение о тех же правилах. "
            "Оставь короткое сообщение только о том, что правила вступили в силу, "
            "без повторения подробностей. Добавь строку «Ранее» со ссылкой на подробный "
            "разбор, публикация канала №5. Сохрани ссылку на источник внизу."
        ),
    },
    {
        "key": "editorial-feedback-case-62-v1",
        "message_id": "62",
        "reference_message_id": None,
        "reason": (
            "Убери вводное «Обновление:» из заголовка: это дополнение к новости, "
            "а не обновление инфоповода. Удали из текста дублирующую атрибуцию "
            "«сообщает Коммерсантъ», поскольку ссылка на издание уже стоит внизу. "
            "Сохрани главный факт, проверенные детали и ссылку на источник."
        ),
    },
)


def enqueue_deployed_correction_requests(config: dict, db) -> int:
    """Seed explicit editorial corrections once; the normal review worker handles them."""
    owner_ids = config.get("telegram", {}).get("interest_owner_user_ids") or []
    if not owner_ids:
        return 0
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    created = 0
    state_changed = False
    for request in _DEPLOY_CORRECTION_REQUESTS:
        if db.execute("SELECT 1 FROM app_state WHERE key=?", (request["key"],)).fetchone():
            continue
        post = db.execute(
            "SELECT p.post_id,p.origin_item_id,p.story_id,p.text,p.status,p.external_id,s.headline "
            "FROM posts p JOIN stories s USING(story_id) "
            "WHERE p.external_id=? ORDER BY p.post_id DESC LIMIT 1",
            (request["message_id"],),
        ).fetchone()
        if not post or post["status"] != "PUBLISHED" or not post["external_id"]:
            continue
        if request["reference_message_id"]:
            reference = db.execute(
                "SELECT 1 FROM posts WHERE external_id=? AND status='PUBLISHED' LIMIT 1",
                (request["reference_message_id"],),
            ).fetchone()
            if not reference:
                continue
        existing = db.execute(
            "SELECT 1 FROM telegram_feedback_corrections c "
            "JOIN editorial_feedback f USING(feedback_id) "
            "WHERE c.post_id=? AND f.reason=? LIMIT 1",
            (post["post_id"], request["reason"]),
        ).fetchone()
        if existing:
            db.execute("INSERT INTO app_state(key,value) VALUES(?,?)", (request["key"], "already_queued"))
            state_changed = True
            continue
        latest = db.execute(
            "SELECT edited_text FROM telegram_post_edits WHERE post_id=? "
            "ORDER BY captured_at DESC,ABS(update_id) DESC LIMIT 1", (post["post_id"],)
        ).fetchone()
        feedback_reason = request["reason"]
        if request["reference_message_id"]:
            feedback_reason += f"\n[BACKEND_REFERENCE_MESSAGE_ID:{request['reference_message_id']}]"
        feedback = db.execute(
            "INSERT INTO editorial_feedback(created_at,item_id,story_id,post_id,feedback_type,reason,item_title,post_text) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (now, post["origin_item_id"], post["story_id"], post["post_id"], "TELEGRAM_EDIT",
             feedback_reason, post["headline"] or "", (latest["edited_text"] if latest else post["text"])[:5000]),
        )
        db.execute(
            "INSERT INTO telegram_feedback_corrections(feedback_id,post_id,owner_chat_id,created_at,updated_at) "
            "VALUES(?,?,?,?,?)",
            (feedback.lastrowid, post["post_id"], str(owner_ids[0]), now, now),
        )
        db.execute("INSERT INTO app_state(key,value) VALUES(?,?)", (request["key"], "queued"))
        state_changed = True
        created += 1
    if state_changed:
        db.commit()
    return created


def flush_feedback_correction_notices(config: dict, db) -> None:
    rows = db.execute("SELECT * FROM telegram_feedback_corrections WHERE notice_status='PENDING' AND status!='QUEUED' ORDER BY correction_id LIMIT 10").fetchall()
    for row in rows:
        status = row["status"]
        if status == "EDITED":
            post = db.execute("SELECT external_id FROM posts WHERE post_id=?", (row["post_id"],)).fetchone()
            try:
                from .delivery import channel
                target = telegram_api(config, "getChat", {"chat_id": channel(config)})
                username = target.get("username")
                message_id = post["external_id"] if post else ""
                link = (f"https://t.me/{username}/{message_id}" if username else
                        f"https://t.me/c/{str(target.get('id','')).removeprefix('-100')}/{message_id}")
            except Exception:
                link = ""
            text = "Проверил отзыв и исправил тот же пост в канале. " + (link + "\n" if link else "") + f"Причина: {row['result_summary']}"
        elif status == "NO_CHANGE":
            text = "Проверил отзыв. Пост не менял: " + (row["result_summary"] or "не нашёл подтверждённой ошибки.")
        elif status == "UNKNOWN":
            text = "Не могу подтвердить, применил ли Telegram правку. Повторно её не отправлял; проверьте пост в канале."
        else:
            text = "Проверил отзыв, но пост не менял: " + (row["result_summary"] or "правка не прошла проверки.")
        try:
            result = telegram_api(config, "sendMessage", {"chat_id": row["owner_chat_id"], "text": text})
            state = "SENT" if isinstance(result, dict) and result.get("message_id") else "UNKNOWN"
            error = None if state == "SENT" else "INVALID_NOTICE_RECEIPT"
        except Exception as exc:
            state = "UNKNOWN" if type(exc).__name__ == "DeliveryUncertain" else "FAILED"
            error = type(exc).__name__
        db.execute("UPDATE telegram_feedback_corrections SET notice_status=?,notice_attempts=notice_attempts+1,notice_error_code=?,updated_at=? WHERE correction_id=?",
                   (state, error, datetime.now(timezone.utc).isoformat(timespec="seconds"), row["correction_id"]))
        db.commit()


def _handle_interest_message(config: dict, db, message: dict) -> None:
    """Accept forwarded examples only in an explicitly allow-listed private chat."""
    chat = message.get("chat") or {}
    sender = message.get("from") or {}
    user_id = str(sender.get("id", ""))
    settings = config.get("telegram", {})
    owners = {str(value) for value in settings.get("interest_owner_user_ids", [])}
    if chat.get("type") != "private" or not user_id:
        return
    text = (message.get("text") or message.get("caption") or "").strip()
    command = text.split(maxsplit=1)[0].lower().split("@", 1)[0] if text else ""
    if command in {"/start", "/myid"}:
        telegram_api(config, "sendMessage", {"chat_id": str(chat.get("id")),
            "text": f"Бот запущен. Ваш Telegram ID: {user_id}.\n\nКоманда владельца для публикации без редакционных проверок:\n/publish Текст поста\n\nТакже можно поставить текст на следующей строке после /publish. Пересылки сохраняются в темник; ссылку на пост канала можно прислать с отзывом для проверяемого исправления."})
        return
    if user_id not in owners:
        return
    if _handle_admin_publish(config, db, message, user_id, text, command):
        return
    if _handle_edit_ack_reply(config, db, {"update_id": message.get("_update_id")}, message, user_id):
        return
    if _handle_link_feedback(config, db, message):
        return
    if command in {"/topics", "/темы"}:
        topics = db.execute("SELECT topic,weight FROM monitoring_topics ORDER BY weight DESC,topic").fetchall()
        body = "Текущий темник:\n" + "\n".join(f"• {r['topic']} ({r['weight']})" for r in topics) if topics else "Темник пока пуст. Перешли мне интересную публикацию."
        telegram_api(config, "sendMessage", {"chat_id": str(chat.get("id")), "text": body[:3900]})
        return
    if not text and message.get("forward_origin"):
        text = "Пересланная публикация без текста; возможно, изображение или видео."
    if not text or not message.get("forward_origin"):
        return
    origin = message.get("forward_origin") or {}
    origin_chat = origin.get("chat") or {}
    forwarded_from = (origin_chat.get("title") or origin_chat.get("username") or
                      origin.get("sender_user_name") or origin.get("type") or "unknown")
    entities = message.get("caption_entities") or message.get("entities") or []
    urls = [entity.get("url") for entity in entities if entity.get("type") == "text_link" and entity.get("url")]
    source_url = urls[0] if urls else ""
    if not source_url and origin_chat.get("username") and origin.get("message_id"):
        source_url = f"https://t.me/{origin_chat['username']}/{origin['message_id']}"
    try:
        from .interests import save_submission
        saved, topic_count, analysis_depth = save_submission(
            db, user_id=user_id, chat_id=str(chat.get("id")), message_id=int(message.get("message_id", 0)),
            forwarded_from=str(forwarded_from)[:250], source_url=source_url,
            text=text, ai_settings=config.get("ai", {}),
        )
        depth_labels = {"BRIEF": "краткий", "CONTEXTUAL": "контекстный", "DEEP": "глубокий"}
        depth_text = depth_labels.get(analysis_depth)
        if not saved and depth_text:
            reply = "Уже сохранил этот пост. Темы и предпочтительную глубину анализа учитываю в следующих материалах."
        elif not saved:
            reply = "Пост уже сохранён, темы мониторинга учитываются. Чтобы разобрать глубину анализа, перешлите публикацию ещё раз."
        elif topic_count and depth_text:
            reply = (f"Сохранил пост и обновил темник: добавлено или уточнено тем — {topic_count}. "
                     f"Отметил {depth_text} уровень анализа и характерные приёмы; буду использовать их как ориентир "
                     "в следующих материалах по подходящим темам.")
        elif topic_count:
            reply = (f"Сохранил пост и обновил темник: добавлено или уточнено тем — {topic_count}. "
                     "Глубину анализа пока не удалось определить; использую темы в поиске.")
        elif depth_text:
            reply = (f"Сохранил пост. Отметил {depth_text} уровень анализа и характерные приёмы; "
                     "буду использовать их как ориентир в следующих материалах.")
        else:
            reply = "Пост сохранил, но пока не удалось разобрать темы и глубину анализа. Попробуйте переслать его позже."
    except Exception as exc:
        # The raw example remains saved even if topic extraction is temporarily unavailable.
        reply = f"Публикацию сохранил. Темы и глубину анализа пока не удалось определить ({type(exc).__name__}); попробую при следующей пересылке."
    telegram_api(config, "sendMessage", {"chat_id": str(chat.get("id")), "text": reply})


def _handle_admin_publish(config: dict, db, message: dict, user_id: str,
                          text: str, command: str) -> bool:
    """Publish an explicit owner command outside the editorial queue, durably logged."""
    if command not in {"/publish", "/пост"}:
        return False
    match = re.match(r"^/(?:publish|пост)(?:@\w+)?(?:[ \t]+|\r?\n)?", text, re.IGNORECASE)
    body = text[match.end():].strip() if match else ""
    reply_to = str((message.get("chat") or {}).get("id", ""))
    if not body:
        telegram_api(config, "sendMessage", {"chat_id": reply_to,
            "text": "Добавьте текст после команды: /publish Текст поста"})
        return True
    if len(body) > 3900:
        telegram_api(config, "sendMessage", {"chat_id": reply_to,
            "text": "Текст длиннее технического лимита 3900 символов. Разбейте публикацию и отправьте команды отдельно."})
        return True
    try:
        update_id = int(message.get("_update_id"))
    except (TypeError, ValueError):
        telegram_api(config, "sendMessage", {"chat_id": reply_to,
            "text": "Не удалось надёжно зарегистрировать команду; пост не отправлен. Повторите команду."})
        return True

    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    existing = db.execute("SELECT * FROM admin_publication_requests WHERE update_id=?", (update_id,)).fetchone()
    if existing and existing["status"] == "PUBLISHED":
        telegram_api(config, "sendMessage", {"chat_id": reply_to,
            "text": f"Эта команда уже опубликована. Telegram message_id={existing['telegram_message_id']}"})
        return True
    if existing and existing["status"] in {"SENDING", "UNKNOWN"}:
        telegram_api(config, "sendMessage", {"chat_id": reply_to,
            "text": "Результат этой отправки пока не подтверждён. Повторно не отправляю, чтобы не создать дубль."})
        return True
    if not existing:
        db.execute("INSERT INTO admin_publication_requests(update_id,owner_user_id,owner_chat_id,request_message_id,text,status,created_at,updated_at) "
                   "VALUES(?,?,?,?,?,'RECEIVED',?,?)",
                   (update_id, user_id, reply_to, str(message.get("message_id", "")), body, now, now))
        db.commit()
    elif existing["text"] != body or existing["owner_user_id"] != user_id:
        telegram_api(config, "sendMessage", {"chat_id": reply_to,
            "text": "Команда уже зарегистрирована с другим содержимым; новую отправку не создавал."})
        return True

    from .delivery import DeliveryRejected, DeliveryUncertain, deliver
    from .cli import telegram_send
    db.execute("UPDATE admin_publication_requests SET status='SENDING',error_code=NULL,updated_at=? WHERE update_id=?",
               (now, update_id))
    db.commit()
    try:
        message_id = str(deliver(db, config, f"admin-command:{update_id}", body, telegram_send))
    except DeliveryUncertain:
        db.execute("UPDATE admin_publication_requests SET status='UNKNOWN',error_code='DELIVERY_UNCERTAIN',updated_at=? WHERE update_id=?",
                   (datetime.now(timezone.utc).isoformat(timespec="seconds"), update_id))
        db.commit()
        telegram_api(config, "sendMessage", {"chat_id": reply_to,
            "text": "Telegram не подтвердил результат. Повторно не отправляю, чтобы не создать дубль; сверю статус позже."})
        return True
    except DeliveryRejected as exc:
        db.execute("UPDATE admin_publication_requests SET status='FAILED',error_code=?,updated_at=? WHERE update_id=?",
                   (type(exc).__name__, datetime.now(timezone.utc).isoformat(timespec="seconds"), update_id))
        db.commit()
        telegram_api(config, "sendMessage", {"chat_id": reply_to,
            "text": "Telegram отклонил отправку; пост не опубликован. Исправьте техническую проблему и отправьте команду заново."})
        return True
    except Exception as exc:
        db.execute("UPDATE admin_publication_requests SET status='UNKNOWN',error_code=?,updated_at=? WHERE update_id=?",
                   (type(exc).__name__, datetime.now(timezone.utc).isoformat(timespec="seconds"), update_id))
        db.commit()
        telegram_api(config, "sendMessage", {"chat_id": reply_to,
            "text": "Результат отправки не удалось подтвердить. Повторно не отправляю, чтобы не создать дубль."})
        return True
    db.execute("UPDATE admin_publication_requests SET status='PUBLISHED',telegram_message_id=?,error_code=NULL,updated_at=? WHERE update_id=?",
               (message_id, datetime.now(timezone.utc).isoformat(timespec="seconds"), update_id))
    db.commit()
    try:
        from .cli import _telegram_message_url
        link = _telegram_message_url(config, message_id)
    except Exception:
        link = None
    result_text = f"Опубликовано по команде владельца. Telegram message_id={message_id}"
    if link:
        result_text += f"\n{link}"
    telegram_api(config, "sendMessage", {"chat_id": reply_to, "text": result_text})
    return True


def handle_update(config: dict, db, update: dict) -> None:
    if update.get("channel_post"):
        from .delivery import observe_channel_post
        message = update["channel_post"]
        observe_channel_post(db, message, _edited_message_text(message))
    elif update.get("edited_channel_post"):
        _handle_edited_channel_post(config, db, update)
    elif update.get("callback_query"):
        _handle_callback(config, db, update)
    elif update.get("message"):
        _handle_message(config, db, update)


def run_review_bot(config: dict) -> None:
    db_path = config["newsroom"]["database"]
    db = connect(db_path)
    offset_row = db.execute("SELECT value FROM app_state WHERE key='review_bot_offset'").fetchone()
    offset = int(offset_row["value"]) if offset_row else None
    telegram_api(config, "getMe", {})
    from .interests import backfill_submission_profiles
    try:
        learned = backfill_submission_profiles(db, config.get("ai", {}))
        if learned:
            print(f"Дополнено примеров с глубиной анализа: {learned}.", flush=True)
    except Exception as exc:
        print(f"Не удалось дополнить старые примеры ({type(exc).__name__}).", file=sys.stderr, flush=True)
    print("Бот сбора редакционных примеров и интересов запущен.", flush=True)
    from .edit_sync import handle_persisted_update, sync_recent_channel_edits
    while True:
        payload = {"timeout": 30, "allowed_updates": REVIEW_ALLOWED_UPDATES}
        if offset is not None:
            payload["offset"] = offset
        try:
            seeded = enqueue_deployed_correction_requests(config, db)
            if seeded:
                print(f"Поставлено серверных редакторских правок в очередь: {seeded}.", flush=True)
            recover_feedback_correction_jobs(db)
            flush_feedback_correction_notices(config, db)
            process_feedback_corrections(config, db, limit=1)
            flush_feedback_correction_notices(config, db)
            try:
                flush_edit_acknowledgements(config,db)
                recovered = sync_recent_channel_edits(config,db)
                if recovered:
                    print(f"Сверка канала: сохранено правок {recovered}.",flush=True)
            except Exception as exc:
                db.rollback()
                print(f"Сверка правок канала временно недоступна ({type(exc).__name__}).",file=sys.stderr,flush=True)
            updates = telegram_api(config, "getUpdates", payload, timeout=40) or []
            for update in updates:
                handle_persisted_update(config, db, update)
                offset = int(update["update_id"]) + 1
                db.execute("INSERT OR REPLACE INTO app_state(key,value) VALUES('review_bot_offset',?)", (str(offset),))
                db.commit()
        except KeyboardInterrupt:
            break
        except Exception as exc:
            # Exception text may contain the bot token from an API URL: print only a safe class.
            print(f"Ошибка редакторского бота ({type(exc).__name__}); повтор через 10 секунд.", file=sys.stderr, flush=True)
            time.sleep(10)
    db.close()
