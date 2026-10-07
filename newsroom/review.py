from __future__ import annotations

import difflib
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone

from .db import connect
from .cli import telegram_api
from .runtime import BudgetDeferred


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
        _resolve_agent_correction_item(db, auto_intent["correction_id"], "EDITED")
        capture_source = "AUTOMATED_CORRECTION_CONFIRMED"
        db.commit()
    prior_edit = db.execute(
        "SELECT * FROM telegram_post_edits WHERE post_id=? ORDER BY captured_at DESC, ABS(update_id) DESC LIMIT 1",
        (row["post_id"],),
    ).fetchone()
    if prior_edit and _comparable_post_text(prior_edit['edited_text']) == _comparable_post_text(edited_text):
        if auto_intent and capture_source == "AUTOMATED_CORRECTION_CONFIRMED":
            _record_confirmed_agent_edit(db, auto_intent["correction_id"], previous_text or prior_edit["previous_text"], edited_text)
            db.commit()
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
    if auto_intent and capture_source == "AUTOMATED_CORRECTION_CONFIRMED":
        _record_confirmed_agent_edit(db, auto_intent["correction_id"], previous_text, edited_text)
    else:
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
        notice = (f"Пост «{title}» скорректирован в канале. Сохранил пример; предлагаю общее уточнение:\n"
                  f"{ack['learning_summary']}\n\nДо подтверждения общее правило не меняется. Если вывод верный, ответьте «Верно». Если я понял правку неточно, ответьте на это сообщение и напишите, что именно нужно изменить.")[:3900]
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
        response = "Подтверждение сохранено. Правило будет зарегистрировано; результат записи и действующая версия появятся в кабинете."
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
    _resolve_agent_correction_item(db, correction_id, status)
    db.commit()


def _resolve_agent_correction_item(db, correction_id: int, status: str) -> None:
    feedback = db.execute(
        "SELECT item_id,feedback_type FROM editorial_feedback WHERE feedback_id="
        "(SELECT feedback_id FROM telegram_feedback_corrections WHERE correction_id=?)",
        (correction_id,),
    ).fetchone()
    if (not feedback or feedback["feedback_type"] not in {"AGENT_FACT_UPDATE", "AGENT_STORY_SUPPLEMENT"}
            or not feedback["item_id"]):
        return
    if status == "EDITED":
        disposition = "STORE_ONLY"
    elif status in {"NO_CHANGE", "REJECTED"}:
        disposition = "WAITING_CONFIRMATION"
    else:
        return
    if feedback["feedback_type"] == "AGENT_STORY_SUPPLEMENT" and status == "NO_CHANGE":
        disposition = "STORE_ONLY"
    db.execute("UPDATE items SET disposition=?,processed_at=? WHERE item_id=? AND disposition='AGENT_CORRECTION_QUEUED'",
               (disposition, datetime.now(timezone.utc).isoformat(timespec="seconds"), feedback["item_id"]))


def _record_agent_edit_learning(db, correction, previous_text: str, corrected_text: str,
                               summary: str, evidence: list | None, source_url: str) -> None:
    """Keep a confirmed agent edit as a deduplicated before/after editorial example."""
    marker = f"[AGENT_CORRECTION_ID:{correction['correction_id']}]"
    if db.execute("SELECT 1 FROM editorial_feedback WHERE post_id=? AND feedback_type='TELEGRAM_EDIT' AND instr(reason,?)>0 LIMIT 1",
                  (correction["post_id"], marker)).fetchone():
        return
    owner_feedback = db.execute("SELECT reason,item_id,story_id,item_title,feedback_type FROM editorial_feedback WHERE feedback_id=?",
                                (correction["feedback_id"],)).fetchone()
    if not owner_feedback:
        return
    diff = "\n".join(difflib.unified_diff(
        previous_text.splitlines(), corrected_text.splitlines(),
        fromfile="до", tofile="после", lineterm="",
    ))[:1400]
    evidence_lines = []
    for entry in evidence or []:
        if isinstance(entry, dict) and str(entry.get("quote") or "").strip():
            evidence_lines.append(f"Подтверждение: {str(entry['quote']).strip()[:350]}")
    agent_origin = owner_feedback["feedback_type"] in {"AGENT_FACT_UPDATE", "AGENT_STORY_SUPPLEMENT"}
    lesson = (
        ("Подтверждённая самостоятельная правка агента по новому источнику.\n"
         if agent_origin else "Подтверждённая правка агента по сигналу владельца.\n")
        + (f"Основание: {str(owner_feedback['reason'] or '')[:700]}\n"
           if agent_origin else f"Замечание владельца: {str(owner_feedback['reason'] or '')[:700]}\n")
        + f"Вывод проверки: {summary[:500]}\n"
        f"Источник: {source_url}\n"
        f"Изменение:\n{diff or 'Текст исправлен.'}\n"
        + "\n".join(evidence_lines)
        + f"\n{marker}"
    )
    db.execute(
        "INSERT INTO editorial_feedback(created_at,item_id,story_id,post_id,feedback_type,reason,item_title,post_text) "
        "VALUES(?,?,?,?,?,?,?,?)",
        (datetime.now(timezone.utc).isoformat(timespec="seconds"), owner_feedback["item_id"],
         owner_feedback["story_id"], correction["post_id"], "TELEGRAM_EDIT",
         lesson[:5000], owner_feedback["item_title"] or "", previous_text[:5000]),
    )


def _bind_corrected_post_facts(db, correction, corrected_text: str,
                               evidence: list | None) -> None:
    """Mark memory facts as covered only after an agent edit was confirmed."""
    feedback = db.execute("SELECT item_id,story_id,feedback_type FROM editorial_feedback WHERE feedback_id=?",
                          (correction["feedback_id"],)).fetchone()
    if not feedback or feedback["feedback_type"] not in {"AGENT_FACT_UPDATE", "AGENT_STORY_SUPPLEMENT"}:
        return
    if not feedback["item_id"]:
        return
    rows = db.execute(
        "SELECT f.fact_id,fe.quote FROM fact_evidence fe "
        "JOIN source_snapshots ss USING(snapshot_id) JOIN story_facts f USING(fact_id) "
        "WHERE ss.item_id=? AND f.story_id=?",
        (feedback["item_id"], feedback["story_id"]),
    ).fetchall()
    if not rows:
        return
    norm_text = _normalised_source_quote(corrected_text)
    for entry in evidence or []:
        if not isinstance(entry, dict):
            continue
        claim = str(entry.get("claim") or "").strip()
        quote = _normalised_source_quote(entry.get("quote") or "")
        if not claim or _normalised_source_quote(claim) not in norm_text or not quote:
            continue
        for fact in rows:
            if _normalised_source_quote(fact["quote"]) == quote:
                db.execute("INSERT OR IGNORE INTO post_facts(post_id,fact_id,post_quote) VALUES(?,?,?)",
                           (correction["post_id"], fact["fact_id"], claim))


def _record_confirmed_agent_edit(db, correction_id: int, previous_text: str,
                                 corrected_text: str) -> None:
    correction = db.execute("SELECT * FROM telegram_feedback_corrections WHERE correction_id=?",
                            (correction_id,)).fetchone()
    if not correction:
        return
    try:
        evidence = json.loads(correction["evidence_json"] or "[]")
    except (TypeError, json.JSONDecodeError):
        evidence = []
    learning_source_url = (evidence[0].get("source_url") if evidence and isinstance(evidence[0], dict)
                           else ((_post_source_urls(corrected_text) or [""])[-1]))
    db.execute("SAVEPOINT confirmed_agent_edit_learning")
    try:
        _bind_corrected_post_facts(db, correction, corrected_text, evidence)
        _record_agent_edit_learning(
            db, correction, previous_text, corrected_text,
            correction["result_summary"] or "Правка подтверждена сверкой с каналом.",
            evidence, learning_source_url)
        db.execute("RELEASE confirmed_agent_edit_learning")
    except Exception:
        db.execute("ROLLBACK TO confirmed_agent_edit_learning")
        db.execute("RELEASE confirmed_agent_edit_learning")


def _normalised_source_quote(value: str) -> str:
    return " ".join(str(value or "").casefold().split())


def _post_source_urls(text: str) -> list[str]:
    lines = [line for line in str(text or "").splitlines()
             if line.startswith(("Источник:", "Источники:"))]
    if len(lines) != 1:
        return []
    return re.findall(r"\[[^\]]+\]\((https?://[^)]+)\)", lines[0])


def enqueue_agent_fact_correction(db, *, item_id: int, story_id: int, post_id: int,
                                  owner_chat_id: str, supplement: bool = False) -> int | None:
    """Queue a same-message edit grounded in new evidence for a published story."""
    if not owner_chat_id:
        return None
    feedback_type = "AGENT_STORY_SUPPLEMENT" if supplement else "AGENT_FACT_UPDATE"
    existing = db.execute(
        "SELECT c.correction_id,c.status FROM telegram_feedback_corrections c "
        "JOIN editorial_feedback f USING(feedback_id) "
        "WHERE c.post_id=? AND f.item_id=? AND f.feedback_type=? LIMIT 1",
        (post_id, item_id, feedback_type),
    ).fetchone()
    if existing:
        return int(existing["correction_id"]) if existing["status"] in {"QUEUED", "PROCESSING", "UNKNOWN"} else None
    post = db.execute("SELECT text FROM posts WHERE post_id=? AND status='PUBLISHED'", (post_id,)).fetchone()
    item = db.execute("SELECT title,url FROM items WHERE item_id=?", (item_id,)).fetchone()
    if not post or not item:
        return None
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    reason = (("Самостоятельная редакторская проверка: новый прочитанный материал содержит существенное "
               "подтверждённое дополнение к этому свежему опубликованному сюжету. Добавь его в прежний пост "
               "только если это улучшит полноту сообщения и точная опора есть в источнике.") if supplement else
              ("Самостоятельная редакторская перепроверка: новый прочитанный материал по тому же сюжету "
               "содержит факт, который противоречит опубликованному утверждению. Проверь только это "
               "противоречие по новому источнику; исправь прежний пост лишь при прямом подтверждении."))
    cur = db.execute(
        "INSERT INTO editorial_feedback(created_at,item_id,story_id,post_id,feedback_type,reason,item_title,post_text) "
        "VALUES(?,?,?,?,?,?,?,?)",
        (now, item_id, story_id, post_id, feedback_type, reason,
         item["title"] or "", post["text"] or ""),
    )
    feedback_id = cur.lastrowid
    correction_id = db.execute(
        "INSERT INTO telegram_feedback_corrections(feedback_id,post_id,owner_chat_id,created_at,updated_at) "
        "VALUES(?,?,?,?,?)", (feedback_id, post_id, str(owner_chat_id), now, now),
    ).lastrowid
    return int(correction_id)


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
    feedback = db.execute("SELECT * FROM editorial_feedback WHERE feedback_id=?", (row["feedback_id"],)).fetchone()
    feedback_type = feedback["feedback_type"] if feedback else ""
    fact_update = feedback_type == "AGENT_FACT_UPDATE"
    story_supplement = feedback_type == "AGENT_STORY_SUPPLEMENT"
    agent_update = fact_update or story_supplement
    source_item_id = feedback["item_id"] if agent_update else post["origin_item_id"]
    item = db.execute("SELECT * FROM items WHERE item_id=?", (source_item_id,)).fetchone() if source_item_id else None
    if not item:
        return "REJECTED", "SOURCE_ITEM_MISSING", "Не удалось восстановить исходный материал к этому посту.", current_text, []
    try:
        item_source = json.loads(item["primary_source_json"] or "{}")
        if fact_update:
            from .core import digest
            analysis = db.execute("SELECT result_json FROM item_analysis WHERE item_id=?",
                                  (item["item_id"],)).fetchone()
            facts = json.loads((analysis["result_json"] if analysis else "{}") or "{}")
            primary = dict(item_source)
            primary["content_sha256"] = digest(str(primary.get("content") or ""))
            facts["primary_source"] = primary
            facts["primary_source_status"] = primary.get("status")
            facts["publisher_report_exception"] = False
            facts["publisher_report"] = None
        elif story_supplement:
            from .core import digest
            analysis = db.execute("SELECT result_json FROM item_analysis WHERE item_id=?",
                                  (item["item_id"],)).fetchone()
            facts = json.loads((analysis["result_json"] if analysis else "{}") or "{}")
            primary = dict(item_source)
            if primary.get("status") == "READ" and primary.get("content"):
                primary["content_sha256"] = digest(str(primary["content"]))
                facts["primary_source"] = primary
                facts["primary_source_status"] = "READ"
            elif item_source.get("_material_read") is True and len(str(item["content"] or "").strip()) >= 100:
                evidence = str((facts.get("original_reporting_check") or {}).get("evidence") or "")
                report = {"type": "ATTRIBUTED_REPORT", "material_read": True,
                          "url": item_source.get("_material_url") or item["url"],
                          "publisher": item_source.get("_material_publisher") or "Издание",
                          "content": item["content"], "evidence": evidence,
                          "content_sha256": digest(str(item["content"]))}
                facts["publisher_report_exception"] = True
                facts["publisher_report"] = report
        else:
            facts = json.loads(post["fact_check_result"] or "{}")
    except (TypeError, json.JSONDecodeError, IndexError):
        facts, item_source = {}, {}
    primary = dict(facts.get("primary_source") or {})
    report = dict(facts.get("publisher_report") or {})
    if not primary.get("content") and item_source.get("content"):
        primary["content"] = item_source["content"]
        primary.setdefault("url", item_source.get("url"))
        primary.setdefault("publisher", item_source.get("publisher") or item_source.get("_material_publisher"))
        primary.setdefault("status", item_source.get("status"))
    source = (primary if primary.get("content") and facts.get("primary_source_status") == "READ" else
              report if story_supplement and facts.get("publisher_report_exception") is True
              and report.get("material_read") is True else {}) if agent_update else (
        primary if primary.get("content") and facts.get("primary_source_status") == "READ" else report)
    if not source.get("content") or not source.get("url"):
        return "REJECTED", "READ_SOURCE_UNAVAILABLE", "Не исправлял пост: в базе нет прочитанного текста источника, по которому можно подтвердить отзыв.", current_text, []
    if not agent_update and source["url"] not in current_text:
        return "REJECTED", "SOURCE_LINK_MISSING", "Не исправлял пост: не удалось подтвердить, какой прочитанный источник указан в его ссылке.", current_text, []
    if source is report and not (facts.get("publisher_report_exception") is True and report.get("material_read") is True):
        return "REJECTED", "REPORT_NOT_READ", "Не исправлял пост: прочитанный текст источника не подтверждён в истории публикации.", current_text, []
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
    source_gate_text = current_text
    if agent_update and source.get("url"):
        source_gate_text += f"\n\nИсточник: [Подтверждающий материал]({source['url']})"
    if not publication_source_ready(facts, source_gate_text):
        return "REJECTED", "ORIGINAL_SOURCE_GATE_FAILED", "Не исправлял пост: исходная публикация не проходит проверку сохранённого источника.", current_text, []
    last_summary = ""
    for attempt in range(max(0, int(row['attempt_count'])) + 1, 4):
        db.execute("UPDATE telegram_feedback_corrections SET attempt_count=?,updated_at=? WHERE correction_id=?",
                   (attempt, datetime.now(timezone.utc).isoformat(timespec="seconds"), row["correction_id"]))
        db.commit()
        try:
            proposal = correct_published_post(
                current_text, feedback_text,
                {"title": item["title"], "content": item["content"]},
                source, config.get("ai", {}), autonomous=agent_update,
                allow_supplement=story_supplement,
            )
        except BudgetDeferred:
            db.execute('UPDATE telegram_feedback_corrections SET attempt_count=? WHERE correction_id=?',
                       (attempt - 1, row['correction_id']))
            db.commit()
            raise
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
            elif kind == "SUPPLEMENT":
                added = new[len(old):].strip() if new.startswith(old) else ""
                if (not story_supplement or len(added) < 24 or len(quote) < 24
                        or _normalised_source_quote(quote) not in _normalised_source_quote(source["content"])
                        or _normalised_source_quote(quote) not in _normalised_source_quote(added)):
                    valid = False
                    last_summary = "Дополнение должно сохранить прежний фрагмент и дословно добавить подтверждённое предложение из источника."
                    break
                evidence_used.append({"claim": added, "quote": quote, "source_url": source["url"]})
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
        if agent_update:
            lines = revised.splitlines()
            source_line_indexes = [index for index, line in enumerate(lines)
                                   if line.startswith(("Источник:", "Источники:"))]
            prior_urls = _post_source_urls(current_text)
            if len(source_line_indexes) != 1 or not prior_urls:
                last_summary = "Не удалось сохранить исходную ссылку опубликованного поста при дополнении."
                continue
            links = re.findall(r"\[([^\]]+)\]\((https?://[^)]+)\)", lines[source_line_indexes[0]])
            if source["url"] not in {url for _, url in links}:
                links.append((str(source.get("publisher") or "Новый источник"), source["url"]))
            if not set(prior_urls).issubset({url for _, url in links}):
                last_summary = "Не удалось сохранить все прежние ссылки на источники."
                continue
            lines[source_line_indexes[0]] = "Источники: " + ", ".join(
                f"[{label}]({url})" for label, url in links)
            revised = "\n".join(lines)
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
        if (not agent_update and original_source_lines != revised_source_lines) or (
                agent_update and (len(revised_source_lines) != 1 or source["url"] not in revised_source_lines[0])):
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
                            (_post_source_urls(intent["previous_text"]) or [""])[0]))
            db.commit()
            try:
                evidence = json.loads(row["evidence_json"] or "[]")
            except (TypeError, json.JSONDecodeError):
                evidence = []
            try:
                _bind_corrected_post_facts(db, row, intent["new_text"], evidence)
                db.commit()
            except Exception:
                db.rollback()  # Fact coverage is ancillary to the confirmed Telegram edit.
            source_urls = _post_source_urls(intent["previous_text"])
            edit_feedback = db.execute("SELECT feedback_type FROM editorial_feedback WHERE feedback_id=?",
                                       (row["feedback_id"],)).fetchone()
            learning_source_url = (evidence[0].get("source_url") if evidence and isinstance(evidence[0], dict)
                                   else (source_urls[-1] if edit_feedback and edit_feedback["feedback_type"] in {"AGENT_FACT_UPDATE", "AGENT_STORY_SUPPLEMENT"} and source_urls
                                         else (source_urls[0] if source_urls else "")))
            try:
                _record_agent_edit_learning(db, row, intent["previous_text"], intent["new_text"],
                                            row["result_summary"] or "Правка подтверждена ответом Telegram.",
                                            evidence, learning_source_url)
            except Exception:
                db.rollback()  # Learning failure must not change the recovered delivery result.
            db.execute("UPDATE telegram_feedback_corrections SET status='EDITED',result_code='CORRECTED',result_summary='Правка подтверждена ответом Telegram.',corrected_text=?,notice_status='PENDING',updated_at=? WHERE correction_id=?",
                       (intent["new_text"], now, row["correction_id"]))
        elif intent["status"] == "FAILED":
            db.execute("UPDATE telegram_feedback_corrections SET status='REJECTED',result_code='TELEGRAM_EDIT_REJECTED',result_summary='Telegram отклонил правку; сообщение не изменено.',notice_status='PENDING',updated_at=? WHERE correction_id=?",
                       (now, row["correction_id"]))
        else:
            db.execute("UPDATE telegram_feedback_corrections SET status='UNKNOWN',result_code='TELEGRAM_EDIT_UNKNOWN',result_summary='Результат правки не подтверждён; повторно её не отправлял.',notice_status='PENDING',updated_at=? WHERE correction_id=?",
                       (now, row["correction_id"]))
        state = db.execute("SELECT status FROM telegram_feedback_corrections WHERE correction_id=?",
                           (row["correction_id"],)).fetchone()
        if state:
            _resolve_agent_correction_item(db, row["correction_id"], state["status"])
    db.commit()


def process_feedback_corrections(config: dict, db, limit: int = 1) -> int:
    from .agent_control import require_enabled
    require_enabled(config)
    from .runtime import attach
    attach(config)
    rows = db.execute("SELECT * FROM telegram_feedback_corrections WHERE status='QUEUED' "
                      "AND (next_attempt_at IS NULL OR next_attempt_at<=?) ORDER BY created_at,correction_id LIMIT ?",
                      (datetime.now(timezone.utc).isoformat(), limit)).fetchall()
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
            source_urls = _post_source_urls(previous_text)
            if not source_urls:
                _finish_correction(db, row["correction_id"], "REJECTED", "SOURCE_FOOTER_INVALID",
                                   "Не менял пост: не удалось сохранить точную ссылку на использованный источник.", previous_text=previous_text)
                processed += 1
                continue
            correction_feedback = db.execute("SELECT feedback_type FROM editorial_feedback WHERE feedback_id=?",
                                             (row["feedback_id"],)).fetchone()
            agent_update = bool(correction_feedback and correction_feedback["feedback_type"] in {"AGENT_FACT_UPDATE", "AGENT_STORY_SUPPLEMENT"})
            if agent_update and (not evidence or not isinstance(evidence[0], dict)
                                 or evidence[0].get("source_url") not in _post_source_urls(revised)):
                _finish_correction(db, row["correction_id"], "REJECTED", "NEW_SOURCE_LINK_MISSING",
                                   "Не менял пост: новый подтверждающий источник не попал в текст исправления.", previous_text=previous_text)
                processed += 1
                continue
            correction_source_url = evidence[0]["source_url"] if agent_update else source_urls[0]
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
                        previous_text, revised, saved_at, "AUTOMATED_CORRECTION", correction_source_url))
            db.execute("UPDATE telegram_message_edit_intents SET status='SENT',response_json=?,updated_at=? WHERE correction_id=?",
                       (json.dumps({"message_id": response["message_id"], "date": response.get("date")}, ensure_ascii=False), saved_at, row["correction_id"]))
            db.commit()
            _finish_correction(db, row["correction_id"], "EDITED", "CORRECTED", summary,
                               previous_text=previous_text,
                               corrected_text=revised, evidence=evidence)
            try:
                _bind_corrected_post_facts(db, row, revised, evidence)
                _record_agent_edit_learning(db, row, previous_text, revised, summary, evidence, correction_source_url)
                db.commit()
            except Exception:
                db.rollback()  # The confirmed edit remains recorded even if its learning example cannot be saved.
            processed += 1
        except BudgetDeferred as exc:
            db.rollback()
            db.execute("UPDATE telegram_feedback_corrections SET status='QUEUED',result_code='BUDGET_DEFERRED',next_attempt_at=? WHERE correction_id=?",
                       ((datetime.now(timezone.utc) + timedelta(seconds=exc.delay_seconds)).isoformat(), row['correction_id']))
            db.commit()
        except Exception as exc:
            db.rollback()
            _finish_correction(db, row["correction_id"], "REJECTED", "CORRECTION_PROCESSING_ERROR",
                               f"Пост не изменён: обработка прервалась ({type(exc).__name__}).")
            processed += 1
    return processed


def flush_feedback_correction_notices(config: dict, db) -> None:
    rows = db.execute("SELECT * FROM telegram_feedback_corrections WHERE notice_status='PENDING' AND status!='QUEUED' ORDER BY correction_id LIMIT 10").fetchall()
    for row in rows:
        status = row["status"]
        feedback = db.execute("SELECT feedback_type FROM editorial_feedback WHERE feedback_id=?",
                              (row["feedback_id"],)).fetchone()
        subject = "Самостоятельно проверил опубликованный пост" if feedback and feedback["feedback_type"] in {"AGENT_FACT_UPDATE", "AGENT_STORY_SUPPLEMENT"} else "Проверил отзыв"
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
            text = subject + " и исправил тот же пост в канале. " + (link + "\n" if link else "") + f"Причина: {row['result_summary']}"
        elif status == "NO_CHANGE":
            text = subject + ". Пост не менял: " + (row["result_summary"] or "не нашёл подтверждённой ошибки.")
        elif status == "UNKNOWN":
            text = subject + ": не могу подтвердить, применил ли Telegram правку. Повторно её не отправлял; проверьте пост в канале."
        else:
            text = subject + ", но пост не менял: " + (row["result_summary"] or "правка не прошла проверки.")
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
            "text": f"Бот запущен. Ваш Telegram ID: {user_id}.\n\nКоманда владельца для публикации без редакционных проверок:\n/publish Текст поста\n\nОбщее замечание о темах, отборе или стиле:\n/feedback Ваш комментарий\n\nТакже можно поставить текст на следующей строке после /publish. Пересылки сохраняются в темник; ссылку на пост канала можно прислать с отзывом для проверяемого исправления."})
        return
    if user_id not in owners:
        return
    if _handle_admin_publish(config, db, message, user_id, text, command):
        return
    if _handle_edit_ack_reply(config, db, {"update_id": message.get("_update_id")}, message, user_id):
        return
    if _handle_link_feedback(config, db, message):
        return
    if command == "/feedback":
        match = re.match(r"^/feedback(?:@\w+)?(?:[ \t]+|\r?\n)?", text, re.IGNORECASE)
        comment = text[match.end():].strip() if match else ""
        if not 5 <= len(comment) <= 2000:
            telegram_api(config, "sendMessage", {"chat_id": str(chat.get("id")),
                "text": "Напишите /feedback и следом комментарий длиной от 5 до 2000 символов."})
            return
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        db.execute(
            "INSERT INTO editorial_feedback(created_at,feedback_type,reason,item_title,post_text) "
            "VALUES(?,?,?,?,?)",
            (now, "OTHER", "Общий комментарий владельца из редакторского бота:\n" + comment,
             "Общее редакторское замечание", ""),
        )
        db.commit()
        telegram_api(config, "sendMessage", {"chat_id": str(chat.get("id")),
            "text": "Комментарий сохранён и будет учитываться в следующих подходящих разборах. "
                    "Если речь о конкретном посте и нужна проверяемая правка, пришлите его ссылку и замечание."})
        return
    if command in {"/topics", "/темы"}:
        from .topic_registry import SETTINGS, SNAPSHOT, policy
        from .source_registry import state
        if state(db, SETTINGS):
            names = [t['name'] for t in policy(state(db, SNAPSHOT, {}))['topics']]
            body = 'Темы из Google Таблицы:\n' + '\n'.join('• '+n for n in names) + '\n' + state(db,SETTINGS)['url']
        else:
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
    if '_topic_registry' in config.get('ai', {}):
        from .topic_registry import credentials_available, SETTINGS
        from .source_registry import state
        reply = ('Пример сохранён. Предпочтения подачи будут учтены. Тематические уточнения пройдут разбор '
                 'и будут применены через общую Google Таблицу. ' +
                 ('' if credentials_available(state(db, SETTINGS)) else 'Для автоматической записи в таблицу ещё нужен доступ Google на сервере.'))
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
    from .agent_control import require_enabled
    require_enabled(config)
    db_path = config["newsroom"]["database"]
    db = connect(db_path)
    from .runtime import attach
    attach(config)
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
    policy_checked = 0.0
    while True:
        require_enabled(config)
        if time.monotonic() - policy_checked >= 180:
            try:
                from .editorial_registry import sync as sync_rules, learn_cycle as learn_rules
                sync_rules(db, config)
                from .policy import attach as attach_policy
                attach_policy(config)
                learn_rules(db, config)
            except Exception as exc:
                db.rollback()
                print(f'Обновление правил отложено ({type(exc).__name__}).', file=sys.stderr, flush=True)
            policy_checked = time.monotonic()
        payload = {"timeout": 30, "allowed_updates": REVIEW_ALLOWED_UPDATES}
        if offset is not None:
            payload["offset"] = offset
        try:
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
