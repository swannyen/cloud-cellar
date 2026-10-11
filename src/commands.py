"""
Slash commands (PLAN.md §8.4).
Phase 1a: /items, /undo, /alias, /set, /unit, /brand, /stats, /help, /ping.
"""
import re

import apply
import matcher
import replies
import store
import structured

UNDO_WINDOW_SECONDS = 24 * 60 * 60
MAX_BRAND_LENGTH = 30
LATER_PHASES = ("/list", "/status", "/price", "/fx", "/expiring")


def handle(text, sender, now):
    """Return the reply for a command, or None to stay silent. now is epoch seconds."""
    name, *args = text.split(None, 1)
    command = name.split("@")[0].lower()  # "/items@BotName" -> "/items"
    if command == "/ping":
        return "pong"
    if command in ("/help", "/start"):
        return replies.HELP
    if command == "/items":
        return replies.items_list(store.list_items())
    if command == "/undo":
        return _undo(sender, now)
    if command == "/alias":
        return _alias(args[0] if args else "")
    if command == "/set":
        return _set(args[0] if args else "", now)
    if command == "/unit":
        return _unit(args[0] if args else "", sender)
    if command == "/brand":
        return _brand(args[0] if args else "", sender)
    if command == "/stats":
        month = apply.month_key(now)
        return replies.stats(month, store.get_stats(month))
    if command in LATER_PHASES:
        return replies.NOT_AVAILABLE_YET
    return None


def _undo(sender, now) -> str:
    """Remove the caller's newest entry from the last 24 hours. Sending it again steps back one more."""
    recent = [
        record for record in store.list_recent_events(sender["id"])
        if "at" in record and now - apply.epoch(record["at"]) <= UNDO_WINDOW_SECONDS
    ]
    for record in reversed(recent):
        store.delete_recent_event(sender["id"], record["sk"])
        pk, sk = record["event_pk"], record["event_sk"]
        if store.get_event(pk, sk) is None:
            continue  # already gone, so step back to the one before
        store.delete_event(pk, sk)
        item = store.get_item(pk.split("#", 1)[1])
        if item is not None:
            apply.refresh_item(item)
        return replies.undone(record["item_name"], record["summary"])
    return replies.NOTHING_TO_UNDO


def _alias(args) -> str:
    """/alias <phrase> = <item>: make the phrase mean that item, and no other."""
    phrase, equals, target = args.partition("=")
    alias = matcher.normalise(phrase)
    if not equals or not alias or not target.strip():
        return replies.ALIAS_USAGE
    items = store.list_items()
    item = _find_item(target, items)
    if item is None:
        return replies.unknown_item(target.strip())
    others = [other for other in items if other is not item]
    for other in others:
        if alias == matcher.normalise(other["name"]):
            return replies.alias_is_a_name(alias, other)
    if alias in matcher.names(item):
        return replies.alias_already(alias, item)
    if len(item["aliases"]) >= apply.MAX_ALIASES:
        return replies.alias_full(item, apply.MAX_ALIASES)
    previous = None
    for other in others:
        kept = [known for known in other["aliases"] if matcher.normalise(known) != alias]
        if len(kept) != len(other["aliases"]):
            other["aliases"] = kept
            store.save_item(other)
            previous = other
    item["aliases"].append(alias)
    store.save_item(item)
    return replies.alias_saved(alias, item, previous)


def _find_item(target, items):
    """The item a person means by its ID, its name or one of its aliases."""
    wanted = target.strip().lower()
    for item in items:
        if item["item_id"] == wanted:
            return item
    found = matcher.match(target, items)
    if found and found[1] == "exact":
        return next(item for item in items if item["item_id"] == found[0])
    return None


def _set(args, now) -> str:
    """/set <item> pack <n>: how many base units one pack of the item holds, e.g. "pack 10" or "pack 5kg"."""
    match = re.fullmatch(r"(.+?)\s+pack\s+(\d+(?:\.\d+)?)\s*([^\W\d_]*)", args.strip(), re.IGNORECASE)
    if not match:
        return replies.SET_USAGE
    target, number, word = match.groups()
    item = _find_item(target, store.list_items())
    if item is None:
        return replies.unknown_item(target.strip())
    unit = structured.unit_word(word, store.list_units()) if word else (item["base_unit"], 1)
    if unit is None or unit[0] == structured.PACK or float(number) <= 0:
        return replies.SET_USAGE
    if not structured.unit_fits(unit[0], item["base_unit"]):
        return replies.wrong_unit(item)
    size = float(number) * unit[1]
    item["pack_size"] = int(size) if size.is_integer() else size
    # Later than any "2x10" entry so far, so this is the size that counts until the next one.
    item["pack_size_set_at"] = apply.iso_utc(now)
    store.save_item(item)
    return replies.pack_size_set(item)


def _unit(args, sender) -> str:
    """/unit <word> = <unit word>: teach a word people put after a number, e.g. "sticks = pcs"."""
    word, equals, target = (part.strip().lower() for part in args.partition("="))
    if not equals or not word.isalpha() or not target:
        return replies.UNIT_USAGE
    if structured.unit_word(target) is None:
        return replies.unit_unknown(target)
    if word in structured.UNIT_WORDS or word in structured.WORDS:
        return replies.unit_taken(word)
    store.save_unit(word, target, sender["name"])
    return replies.unit_saved(word, target)


def _brand(args, sender) -> str:
    """/brand <name>: teach a brand, so "kans facial masks" is read as facial masks, brand kans."""
    name = " ".join(args.split())
    known = store.list_brands()
    if not name:
        return replies.brands_list(known) if known else replies.BRAND_USAGE
    brand_id = matcher.plain(name)
    if not brand_id or len(name) > MAX_BRAND_LENGTH:
        return replies.BRAND_USAGE
    # A brand is dropped from the front of item phrases, so it can't also be what an item is called.
    item = _find_item(name, store.list_items())
    if item is not None:
        return replies.brand_is_an_item(name, item)
    if brand_id in {matcher.plain(brand) for brand in known}:
        return replies.brand_already(name)
    store.save_brand(brand_id, name, sender["name"])
    return replies.brand_saved(name)
