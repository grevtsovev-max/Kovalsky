"""Explicit publication requests issued by the owner in the Codex conversation."""
from __future__ import annotations

import re
from datetime import datetime, timezone

from .db import connect
from .delivery import DeliveryRejected, DeliveryUncertain, channel, confirm, deliver


def publish_from_codex(config: dict, request_key: str, text: str) -> dict:
    """Send exact owner-provided text without the editorial pipeline.

    The caller must invoke this only after the owner explicitly instructs the
    assistant to publish the prepared post in the active Codex conversation.
    """
    key = str(request_key or "").strip()
    body = str(text or "")
    if not re.fullmatch(r"[A-Za-z0-9_-]{8,100}", key):
        raise ValueError("request_key must be 8–100 ASCII letters, digits, '_' or '-'.")
    if not body.strip():
        raise ValueError("Publication text is empty.")
    if len(body) > 3900:
        raise ValueError("Publication text exceeds the Telegram technical limit of 3900 characters.")

    destination = channel(config)
    db = connect(config["newsroom"]["database"])
    operation = f"codex-admin:{key}"
    delivery_key = destination + ":" + operation
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    try:
        request = db.execute("SELECT * FROM codex_publication_requests WHERE request_key=?", (key,)).fetchone()
        if request:
            if request["text"] != body:
                raise ValueError("This request key is already bound to different publication text.")
            if request["status"] == "PUBLISHED":
                return {"status": "PUBLISHED", "request_key": key,
                        "telegram_message_id": request["telegram_message_id"], "reused": True}
            if request["status"] in {"SENDING", "UNKNOWN"}:
                attempt = db.execute("SELECT status,telegram_message_id FROM publication_attempts WHERE delivery_key=?",
                                     (delivery_key,)).fetchone()
                if attempt and attempt["status"] in {"SENT", "CONFIRMED"}:
                    message_id = attempt["telegram_message_id"]
                    db.execute("UPDATE codex_publication_requests SET status='PUBLISHED',telegram_message_id=?,error_code=NULL,updated_at=? WHERE request_key=?",
                               (message_id, datetime.now(timezone.utc).isoformat(timespec="seconds"), key))
                    db.commit()
                    from .cli import _telegram_message_url
                    try:
                        url = _telegram_message_url(config, message_id)
                    except Exception:
                        url = None
                    return {"status": "PUBLISHED", "request_key": key,
                            "telegram_message_id": message_id,
                            "telegram_url": url, "reused": True}
                return {"status": "UNKNOWN", "request_key": key,
                        "telegram_message_id": request["telegram_message_id"], "reused": True}
        else:
            db.execute("INSERT INTO codex_publication_requests(request_key,text,status,created_at,updated_at) "
                       "VALUES(?,?,'RECEIVED',?,?)", (key, body, now, now))
            db.commit()

        db.execute("UPDATE codex_publication_requests SET status='SENDING',error_code=NULL,updated_at=? "
                   "WHERE request_key=?", (now, key))
        db.commit()
        from .cli import telegram_send, _telegram_message_url
        try:
            message_id = str(deliver(db, config, operation, body, telegram_send))
        except DeliveryUncertain:
            db.execute("UPDATE codex_publication_requests SET status='UNKNOWN',error_code='DELIVERY_UNCERTAIN',updated_at=? "
                       "WHERE request_key=?", (datetime.now(timezone.utc).isoformat(timespec="seconds"), key))
            db.commit()
            return {"status": "UNKNOWN", "request_key": key, "telegram_message_id": None}
        except DeliveryRejected as exc:
            db.execute("UPDATE codex_publication_requests SET status='FAILED',error_code=?,updated_at=? "
                       "WHERE request_key=?",
                       (type(exc).__name__, datetime.now(timezone.utc).isoformat(timespec="seconds"), key))
            db.commit()
            return {"status": "FAILED", "request_key": key, "error": type(exc).__name__}
        except Exception as exc:
            db.execute("UPDATE codex_publication_requests SET status='UNKNOWN',error_code=?,updated_at=? "
                       "WHERE request_key=?",
                       (type(exc).__name__, datetime.now(timezone.utc).isoformat(timespec="seconds"), key))
            db.commit()
            return {"status": "UNKNOWN", "request_key": key, "telegram_message_id": None}

        confirm(db, config, operation)
        try:
            url = _telegram_message_url(config, message_id)
        except Exception:
            url = None
        db.execute("UPDATE codex_publication_requests SET status='PUBLISHED',telegram_message_id=?,error_code=NULL,updated_at=? "
                   "WHERE request_key=?",
                   (message_id, datetime.now(timezone.utc).isoformat(timespec="seconds"), key))
        db.commit()
        return {"status": "PUBLISHED", "request_key": key,
                "telegram_message_id": message_id, "telegram_url": url, "reused": False}
    finally:
        db.close()
