"""
Slash commands (PLAN.md §8.4).
Phase 1a: /listitems, /list, /items, /undo, /alias, /rename, /set, /unit, /brand, /listbrands, /forget, /stats,
/help, /ping.
(/add, /bought, /used and /stocktake are entries, not commands: handler.py sends them through the format parser.)
"""
import re

import ai
import apply
import matcher
import model
import replies
import store
import structured

UNDO_WINDOW_SECONDS = 24 * 60 * 60
MAX_BRAND_LENGTH = 30
LATER_PHASES = ("/status", "/price", "/fx", "/expiring")


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
    if command == "/listitems":
        return replies.cellar(store.list_items())
    if command == "/list":
        return _list(args[0] if args else "")
    if command == "/undo":
        return _undo(sender, now)
    if command == "/alias":
        return _alias(args[0] if args else "")
    if command == "/rename":
        return _rename(args[0] if args else "")
    if command == "/set":
        return _set(args[0] if args else "", now)
    if command == "/unit":
        return _unit(args[0] if args else "", sender)
    if command == "/brand":
        return _brand(args[0] if args else "", sender)
    if command == "/listbrands":
        return _list_brands()
    if command == "/forget":
        return _forget(args[0] if args else "")
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
        # The item may have been renamed since the entry was logged.
        return replies.undone(item["name"] if item else record["item_name"], record["summary"])
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


def _find_item(target, items, close=False):
    """The item a person means by its ID, its name or one of its aliases.

    With close, a near spelling counts too: good enough for looking, not for changing things.
    """
    wanted = target.strip().lower()
    for item in items:
        if item["item_id"] == wanted:
            return item
    found = matcher.match(target, items)
    if found and (close or found[1] == "exact"):
        return next(item for item in items if item["item_id"] == found[0])
    return None


def _list(args) -> str:
    """/list <item>: how much of one item is left, and of which brands."""
    target = " ".join(args.split())
    if not target:
        return replies.LIST_USAGE  # /list on its own is kept for the shopping list (Phase 2)
    item = _find_item(target, store.list_items(), close=True)
    if item is None:
        return replies.unknown_item(target)
    # Worked out from the entries here rather than read from the item, so the total and the brands always agree.
    events = store.list_events(item["item_id"])
    apply.recompute(item, events)
    return replies.item_detail(item, model.brands_left(events, item.get("pack_size")))


def _rename(args) -> str:
    """/rename <item> = <new name>: change what an item is called. Its ID and its entries stay as they are."""
    target, equals, new = args.partition("=")
    name = " ".join(new.split())
    name = name[:1].upper() + name[1:]
    wanted = matcher.normalise(name)
    if not equals or not target.strip() or not wanted or "=" in name or len(name) > ai.MAX_NAME_LENGTH:
        return replies.RENAME_USAGE
    items = store.list_items()
    item = _find_item(target, items)
    if item is None:
        return replies.unknown_item(target.strip())
    if name == item["name"]:
        return replies.already_named(item)
    for other in items:
        if other is not item and wanted in matcher.names(other):
            return replies.name_taken(name, other)
    old_name = item["name"]
    item["name"] = name
    # The name covers this spelling now, so it needn't be kept as another name as well.
    item["aliases"] = [known for known in item["aliases"] if matcher.normalise(known) != wanted]
    store.save_item(item)
    # The old name isn't kept: an item is mostly renamed because the old name was wrong for it.
    return replies.renamed(old_name, item, same_words=matcher.normalise(old_name) == wanted)


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
        return _list_brands() if known else replies.BRAND_USAGE
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


def _forget(args) -> str:
    """/forget <alias|brand|unit|store> <name>: take back something that was taught. Entries are never removed."""
    kind, _, name = " ".join(args.split()).partition(" ")
    forget = {"alias": _forget_alias, "brand": _forget_brand, "unit": _forget_unit, "store": _forget_store}
    if kind.lower() not in forget or not name:
        return replies.FORGET_USAGE
    return forget[kind.lower()](name)


def _forget_alias(name) -> str:
    alias = matcher.normalise(name)
    items = store.list_items()
    holders = []
    for item in items:
        kept = [known for known in item["aliases"] if matcher.normalise(known) != alias]
        if len(kept) != len(item["aliases"]):
            item["aliases"] = kept
            store.save_item(item)
            holders.append(item)
    if holders:
        return replies.forgot_alias(alias, holders)
    named = next((item for item in items if alias and matcher.normalise(item["name"]) == alias), None)
    return replies.alias_is_the_name(named) if named else replies.alias_not_known(name)


def _forget_brand(name) -> str:
    """Forget a brand everywhere: as taught with /brand, and on the entries it was noted on.

    The matcher also knows a brand from the entries that carry it, so leaving it
    there would bring it straight back.
    """
    wanted = matcher.plain(name)
    taught = wanted in {matcher.plain(brand) for brand in store.list_brands()}
    if taught:
        store.delete_brand(wanted)
    entries, names = 0, []
    for item in store.list_items():
        if wanted not in {matcher.plain(brand) for brand in item.get("brands", [])}:
            continue
        for event in store.list_events(item["item_id"]):
            if matcher.plain(event.get("brand") or "") == wanted:
                store.put_event(item["item_id"], event["sk"], {**event, "brand": None})
                entries += 1
        apply.refresh_item(item)
        names.append(item["name"])
    if not taught and not entries:
        return replies.brand_not_known(name)
    return replies.forgot_brand(name, entries, names)


def _forget_unit(name) -> str:
    word = name.lower()
    meant = store.list_units().get(word)
    if meant is None:
        return replies.built_in(word) if word in structured.UNIT_WORDS else replies.unit_not_known(word)
    store.delete_unit(word)
    return replies.forgot_unit(word, meant)


def _forget_store(name) -> str:
    """Forget a store saved by "@", so "from <store>" is no longer read as one. Entries keep the store they name."""
    wanted = structured.squash(name)
    for saved in store.list_stores():
        if wanted in {structured.squash(spelling) for spelling in (saved["name"], *saved.get("aliases", []))}:
            store.delete_store(saved["store_id"])
            return replies.forgot_store(saved["name"])
    built_in = structured.store_names().get(wanted)
    return replies.built_in(built_in) if built_in else replies.store_not_known(name)


def _list_brands() -> str:
    """/listbrands: every brand on something we have logged, and every brand taught with /brand."""
    brands = {}  # plain spelling -> [the brand as written, the items it is on]
    for item in store.list_items():
        for brand in item.get("brands", []):
            brands.setdefault(matcher.plain(brand), [brand, []])[1].append(item["name"])
    for brand in store.list_brands():
        brands.setdefault(matcher.plain(brand), [brand, []])
    return replies.brands_list(brands.values())
