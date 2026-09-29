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
    db.execute(
        "INSERT INTO editorial_feedback(created_at,item_id,story_id,post_id,feedback_type,reason,item_title,post_text) "
        "VALUES(?,?,?,?,?,?,?,?)",
        (now, item["item_id"] if item else None, row["story_id"], row["post_id"],
         "TELEGRAM_EDIT", reason[:5000], row["headline"] or "", previous_text[:5000]),
    )
    owners = {str(value) for value in config.get("telegram", {}).get("interest_owner_user_ids", [])}
    for owner_id in owners:
        cursor = db.execute(
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
            "text": f"Бот для сбора публикаций запущен. Ваш Telegram ID: {user_id}. После добавления ID в настройки владельца темника можно пересылать сюда публикации."})
        return
    if user_id not in owners:
        return
    if _handle_edit_ack_reply(config, db, {"update_id": message.get("_update_id")}, message, user_id):
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
