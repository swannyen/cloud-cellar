"""Button taps (PLAN.md §4.3). Phase 1a has the match confirmations; nudge buttons arrive in Phase 2.

A guessed match is recorded straight away and shown with two buttons:
"m:same:<update_id>:<line>" keeps it and saves the phrase as an alias, and
"m:new:<update_id>:<line>" moves the entry to a new item made from the phrase.
"""
import logging

import apply
import replies
import store
import telegram_api

logger = logging.getLogger()


def handle(callback, chat_id):
    """Act on one tap, tell the person what happened, and take the tapped buttons away."""
    parts = (callback.get("data") or "").split(":")
    toast = ""
    if len(parts) == 4 and parts[0] == "m" and parts[1] in ("same", "new"):
        toast = _confirm_match(parts[1], parts[2], parts[3])
        _remove_buttons(callback, chat_id, f":{parts[2]}:{parts[3]}")
        logger.info("button %s for update %s line %s", parts[1], parts[2], parts[3])
    telegram_api.answer_callback_query(callback["id"], toast)


def _confirm_match(choice, update_id, line) -> str:
    pending = store.get_pending(update_id, line)
    if pending is None:
        return replies.TOO_OLD_TO_CHANGE
    store.delete_pending(update_id, line)
    if choice == "same":
        item = apply.confirm_same(pending)
        return replies.alias_toast(pending["phrase"], item) if item else replies.ENTRY_WAS_UNDONE
    item = apply.split_into_new_item(pending)
    return replies.new_item_toast(item) if item else replies.ENTRY_WAS_UNDONE


def _remove_buttons(callback, chat_id, suffix):
    """Drop the row that was tapped; rows for other entries in the same reply stay."""
    message = callback.get("message") or {}
    rows = (message.get("reply_markup") or {}).get("inline_keyboard") or []
    kept = [row for row in rows if not any(button.get("callback_data", "").endswith(suffix) for button in row)]
    if "message_id" in message and len(kept) != len(rows):
        telegram_api.edit_buttons(chat_id, message["message_id"], kept)
