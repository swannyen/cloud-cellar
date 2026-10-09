"""Webhook entry point: verify secret -> dedupe -> allowed-chat filter -> route.

Flow is PLAN.md §4.1. Phase 1 routes commands and chat messages; button taps
(callback queries) arrive in Phase 2.
"""
import base64
import hmac
import json
import logging
import time
from datetime import datetime, timedelta, timezone

import apply
import commands
import config
import parser
import store
import telegram_api

logger = logging.getLogger()
logger.setLevel(logging.INFO)

SECRET_HEADER = "x-telegram-bot-api-secret-token"  # function URLs lowercase header names
ERROR_REPLY = "Sorry, I couldn't process that — please try again"
SGT = timezone(timedelta(hours=8))  # Singapore has no daylight saving


def lambda_handler(event, context):
    # If the secret can't be loaded this raises and the caller gets a 5xx:
    # nothing is processed without authentication.
    if not _secret_ok(event):
        logger.warning("rejected: missing or wrong secret header")
        return _response(403, {"ok": False})

    reply_chat = None  # set only once the chat has passed the allow-list
    update = {}
    try:
        update = _parse_update(event)
        update_id = update["update_id"]
        if not store.first_delivery(update_id):
            logger.info("update %s: duplicate, skipped", update_id)
            return _response(200, {"ok": True})
        chat_id = _chat_id(update)
        if chat_id not in config.allowed_chat_ids():
            logger.info("update %s: chat %s not allowed, ignored", update_id, chat_id)
            return _response(200, {"ok": True})
        reply_chat = chat_id
        _route(update, chat_id)
    except Exception:
        # Always 200 from here on, so Telegram doesn't retry in a loop.
        logger.exception("failed to process update")
        if reply_chat is not None:
            _send_error_reply(reply_chat, _message_id(update))
    return _response(200, {"ok": True})


def _secret_ok(event) -> bool:
    supplied = (event.get("headers") or {}).get(SECRET_HEADER)
    expected = config.secret("webhook-secret")
    if not supplied or not expected:
        return False
    return hmac.compare_digest(supplied.encode(), expected.encode())


def _parse_update(event) -> dict:
    body = event.get("body") or ""
    if event.get("isBase64Encoded"):
        body = base64.b64decode(body).decode("utf-8")
    return json.loads(body)


def _chat_id(update):
    message = update.get("message") or (update.get("callback_query") or {}).get("message") or {}
    return (message.get("chat") or {}).get("id")


def _message_id(update):
    return (update.get("message") or {}).get("message_id") if isinstance(update, dict) else None


def _route(update, chat_id):
    message = update.get("message") or {}
    sender = message.get("from") or {}
    text = (message.get("text") or message.get("caption") or "").strip()
    if not text or sender.get("is_bot"):
        return
    update_id = update["update_id"]
    sender = {"id": sender.get("id", 0), "name": sender.get("first_name") or "Someone"}
    if text.startswith("/"):
        logger.info("update %s: command %s", update_id, text.split()[0].split("@")[0][:20])
        reply = commands.handle(text, sender, now=int(time.time()))
    else:
        reply = _handle_chat(update_id, text, sender, message.get("date") or int(time.time()))
    if reply:
        telegram_api.send_message(chat_id, reply, reply_to=message.get("message_id"))


def _handle_chat(update_id, text, sender, sent_at):
    """Parse a chat message with Claude, record its events, and return the reply (or None)."""
    items = store.list_items()
    today = datetime.fromtimestamp(sent_at, SGT).date()
    parsed = parser.parse(text, sender["name"], items, today)
    lines = apply.apply_events(
        parsed.events,
        items=items,
        sender=sender,
        update_id=update_id,
        at=apply.iso_utc(sent_at),
        raw_text=text,
    )
    logger.info(
        "update %s: %s event(s), clarify=%s, tokens in/out %s/%s",
        update_id,
        [event["action"] for event in parsed.events],
        bool(parsed.clarify),
        parsed.usage.get("input_tokens"),
        parsed.usage.get("output_tokens"),
    )
    if parsed.clarify:
        lines.append(parsed.clarify)
    return "\n".join(lines) or None


def _send_error_reply(chat_id, message_id):
    try:
        telegram_api.send_message(chat_id, ERROR_REPLY, reply_to=message_id)
    except Exception:
        logger.exception("failed to send the error reply")


def _response(status: int, body: dict) -> dict:
    return {
        "statusCode": status,
        "headers": {"content-type": "application/json"},
        "body": json.dumps(body),
    }
