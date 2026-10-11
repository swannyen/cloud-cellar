"""Webhook entry point: verify secret -> dedupe -> allowed-chat filter -> route.

Flow is PLAN.md §4.1. Phase 1 routes commands, chat messages (the pipeline in
PLAN.md §5: format first, AI last) and the match-confirmation buttons; the
nudge buttons arrive in Phase 2.
"""
import base64
import hmac
import json
import logging
import time
from collections import Counter
from datetime import datetime

import ai
import apply
import buttons
import claude_api
import commands
import config
import matcher
import replies
import store
import structured
import telegram_api

logger = logging.getLogger()
logger.setLevel(logging.INFO)

SECRET_HEADER = "x-telegram-bot-api-secret-token"  # function URLs lowercase header names
ERROR_REPLY = "Sorry, I couldn't process that — please try again"


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
            _send_error_reply(reply_chat, update)
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
    callback = update.get("callback_query")
    if callback:
        buttons.handle(callback, chat_id)
        return
    message = update.get("message") or {}
    sender = message.get("from") or {}
    text = (message.get("text") or message.get("caption") or "").strip()
    if not text or sender.get("is_bot"):
        return
    update_id = update["update_id"]
    sender = {"id": sender.get("id", 0), "name": sender.get("first_name") or "Someone"}
    rows = []
    if text.startswith("/") and not structured.is_entry_command(text):
        logger.info("update %s: command %s", update_id, text.split()[0].split("@")[0][:20])
        reply = commands.handle(text, sender, now=int(time.time()))
    else:
        # Chat, or /add, /bought and /used, which are the format written as commands.
        reply, rows = _handle_chat(update_id, text, sender, message.get("date") or int(time.time()))
        if not reply and text.startswith("/"):
            reply = replies.ENTRY_COMMAND_USAGE  # "/add" with nothing after it
    if reply:
        with_buttons = {"buttons": rows} if rows else {}
        telegram_api.send_message(chat_id, reply, reply_to=message.get("message_id"), **with_buttons)


class _Lookups:
    """Saved stores and taught unit words and brands, read from the table only if a line needs them."""

    def __init__(self):
        self._stores = self._units = self._brands = None

    @property
    def brands(self):
        if self._brands is None:
            self._brands = store.list_brands()
        return self._brands

    @property
    def stores(self):
        if self._stores is None:
            self._stores = store.list_stores()
        return self._stores

    @property
    def units(self):
        if self._units is None:
            self._units = store.list_units()
        return self._units


def _handle_chat(update_id, text, sender, sent_at):
    """Run each line through the pipeline in PLAN.md §5. Returns (reply or None, button rows)."""
    items = store.list_items()
    lookups = _Lookups()
    entries = []     # lines read by code
    free_text = []   # lines for the AI fallback
    attempts = 0     # how many of those started with a format keyword
    rejected = 0     # lines that get the format hint
    for line in filter(None, (line.strip() for line in text.splitlines())):
        kind, entry = _read_line(line, items, lookups)
        if kind == "entry":
            entries.append(entry)
        elif kind == "hint":
            rejected += 1
        elif kind == "attempt":
            free_text.append(line)
            attempts += 1
        elif kind == "free text":
            free_text.append(line)
    if not entries and not free_text and not rejected:
        return None, []  # ordinary chat

    counts = Counter()
    notes = []
    if free_text and not config.free_text_ai():
        rejected += len(free_text)
    elif free_text:
        block = "\n".join(free_text)
        found, question = _read_free_text(update_id, block, sender, items, lookups, sent_at, counts)
        entries += found
        if question:
            notes.append(question)
        elif not found:
            rejected += attempts  # tried the format, and the AI couldn't read it either

    lines, rows = [], []
    if entries:
        lines, applied, rows = apply.apply_entries(
            entries, items=items, brands=lookups.brands, sender=sender, update_id=update_id,
            at=apply.iso_utc(sent_at),
        )
        counts.update(applied)
    counts["lines_rejected"] += rejected
    logger.info("update %s: %s, %s", update_id, [entry["action"] for entry in entries], dict(counts))
    _count(sent_at, counts)
    if rejected:
        notes.append(replies.FORMAT_HINT)
    return "\n".join(lines + notes) or None, rows


def _read_line(line, items, lookups):
    """Sort one line: ("entry", the entry), or ("hint" / "attempt" / "free text" / None, None).

    "attempt" is a line that starts with a keyword but can't be read as the format;
    like "free text", it goes to the AI fallback.
    """
    if structured.has_keyword(line):
        result = structured.parse_line(line, lookups.stores, lookups.units)
        if isinstance(result, dict):
            return "entry", result
        if result is None:
            return None, None  # a keyword with nothing after it: ordinary chat
        # "+ 2 3" has no words for the AI to work with either.
        return ("attempt" if structured.has_words(line) else "hint"), None
    if any(character.isdigit() for character in line):
        # No keyword, but clearly a purchase: a price, and an item we know by exactly that name.
        guess = structured.parse_bought(line, lookups.stores, lookups.units)
        if isinstance(guess, dict) and guess["total_price"] is not None:
            found = matcher.match_phrase(guess["phrase"], items, lookups.brands)[0]
            if found and found[1] == "exact":
                return "entry", guess
    return ("free text" if ai.looks_like_log(line, items) else None), None


def _read_free_text(update_id, text, sender, items, lookups, sent_at, counts):
    """Ask the AI fallback what the free-text lines say. Returns (entries, a question to ask or None)."""
    today = datetime.fromtimestamp(sent_at, apply.SGT).date()
    try:
        parsed = ai.parse_free_text(text, sender["name"], items, today)
    except claude_api.ClaudeError:
        # Lines in the format are still recorded.
        logger.exception("update %s: free-text fallback failed", update_id)
        return [], replies.FREE_TEXT_FAILED
    counts["ai_full_calls"] += 1
    logger.info(
        "update %s: free text, tokens in/out %s/%s",
        update_id, parsed.usage.get("input_tokens"), parsed.usage.get("output_tokens"),
    )
    # Only what is written in the message survives: see ai.ground_free_text.
    return ai.ground_free_text(parsed.events, text, lookups.stores, lookups.units), parsed.clarify


def _count(sent_at, counts):
    """Add to the month's /stats counters. The entries are already recorded, so a failure here is only logged."""
    try:
        store.add_stats(apply.month_key(sent_at), counts)
    except Exception:
        logger.exception("failed to update the stats counters")


def _send_error_reply(chat_id, update):
    try:
        callback = update.get("callback_query") if isinstance(update, dict) else None
        if callback:
            telegram_api.answer_callback_query(callback["id"], replies.BUTTON_FAILED)
        else:
            telegram_api.send_message(chat_id, ERROR_REPLY, reply_to=_message_id(update))
    except Exception:
        logger.exception("failed to send the error reply")


def _response(status: int, body: dict) -> dict:
    return {
        "statusCode": status,
        "headers": {"content-type": "application/json"},
        "body": json.dumps(body),
    }
