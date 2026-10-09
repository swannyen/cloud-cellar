"""Slash commands (PLAN.md §8.4). Phase 1: /items, /undo, /help, plus /ping."""
import apply
import replies
import store

UNDO_WINDOW_SECONDS = 24 * 60 * 60
LATER_PHASES = ("/list", "/status", "/price")


def handle(text, sender, now):
    """Return the reply for a command, or None to stay silent. now is epoch seconds."""
    command = text.split()[0].split("@")[0].lower()  # "/items@BotName" -> "/items"
    if command == "/ping":
        return "pong"
    if command in ("/help", "/start"):
        return replies.HELP
    if command == "/items":
        return replies.items_list(store.list_items())
    if command == "/undo":
        return _undo(sender, now)
    if command in LATER_PHASES:
        return replies.NOT_AVAILABLE_YET
    return None


def _undo(sender, now) -> str:
    last = store.get_last_event(sender["id"])
    if not last:
        return replies.NOTHING_TO_UNDO
    if now - apply.epoch(last["at"]) > UNDO_WINDOW_SECONDS:
        return replies.UNDO_TOO_OLD
    pk, sk = last["event_pk"], last["event_sk"]
    event = store.get_event(pk, sk)
    store.clear_last_event(sender["id"])
    if not event:
        return replies.NOTHING_TO_UNDO
    store.delete_event(pk, sk)
    item = store.get_item(pk.split("#", 1)[1])
    if item is None:
        return "Undone."
    apply.refresh_item(item)
    return replies.undone(item, event)
