"""Apply entries to the ledger: resolve the item, work out the quantity, record, replay, reply (PLAN.md §4.2).

An entry is one thing that happened to one item. It comes from the format
parser (structured.py) or, through ai.ground_free_text, from the AI free-text fallback.
"""
import logging
import re
from collections import Counter
from datetime import datetime, timedelta, timezone

import ai
import claude_api
import config
import matcher
import model
import replies
import store
import structured

logger = logging.getLogger()

MAX_SLUG = 40          # keeps "a:<action>:<item_id>" button data under Telegram's 64 bytes
MAX_RAW_TEXT = 300
MAX_ALIASES = 30
MAX_ALIAS_LENGTH = 40
MAX_BRANDS = 20
MAX_BUTTON_NAME = 30
SGT = timezone(timedelta(hours=8))  # Singapore has no daylight saving

# How an entry's item was found: "exact" or "fuzzy" by the matcher, "ai" for an
# existing item the AI picked, "new" for an item the AI had us create.
AI_PATHS = ("ai", "new")
GUESSES = ("fuzzy", "ai")   # recorded straight away, then confirmed with a button
NO_AMOUNT = {"qty": None, "packs": None, "units_per_pack": None}


def apply_entries(entries, *, items, sender, update_id, at, brands=()) -> tuple:
    """Record each entry. Returns (reply lines, STATS counts, button rows).

    items is the tracked-item list; items created here are added to it. brands
    are the names taught with /brand. at is the event time as an ISO UTC
    string. Nothing is recorded for an entry whose phrase isn't a household
    product (it gets a reply only if the line began with "+", "-", "?" or an entry command), or
    whose quantity can't be worked out without guessing (the reply asks).
    """
    lines, buttons, counts = [], [], Counter()
    for index, entry in enumerate(entries):
        entry = dict(entry)  # resolving may take a brand off the phrase
        free_text = entry.get("free_text", False)
        try:
            item, how = (_resolve_named if free_text else _resolve_item)(entry, items, brands, at)
        except claude_api.ClaudeError:
            # The other lines of the message are still recorded.
            logger.exception("update %s: item match failed", update_id)
            lines.append(replies.lookup_failed(entry["phrase"]))
            continue
        if how in AI_PATHS and not free_text:
            counts["ai_match_calls"] += 1
        if item is None:
            counts["lines_rejected"] += 1
            # "have you eaten" is chat and gets no reply, but "+ ..." or "/add ..." was meant as an entry.
            if entry.get("explicit"):
                lines.append(replies.not_a_product(entry["phrase"]))
            continue
        guessed = entry["phrase"] if how in GUESSES else None
        amount = _amount(entry, item)
        if amount == structured.NEED_PACK_SIZE:
            counts["lines_rejected"] += 1
            lines.append(replies.ask_pack_size(item, entry, guessed))
            continue
        if amount == structured.UNCLEAR_COUNT:
            counts["lines_rejected"] += 1
            lines.append(replies.ask_packs_or_units(item, entry, guessed))
            continue
        if amount == structured.WRONG_UNIT:
            counts["lines_rejected"] += 1
            lines.append(replies.wrong_unit(item))
            continue
        if not free_text:
            counts["lines_structured"] += 1
        record = _event_record(entry, amount, sender, at, how)
        # The index keeps two events for one item in one message from overwriting each other.
        sk = f"{at}#{update_id}#{index}"
        store.put_event(item["item_id"], sk, record)
        remember_for_undo(item, record, sk)
        if entry.get("store_is_new"):
            store.save_store(slugify(entry["store"]), entry["store"], at)
        refresh_item(item)
        lines.append(replies.event_line(
            item, record, created=how == "new", read_from=guessed, missed=entry.get("missed", ())
        ))
        if guessed:
            # A wrong guess is put right with one tap: see confirm_same and split_into_new_item.
            store.put_pending(update_id, index, {
                "phrase": entry["phrase"],
                "matched_item_id": item["item_id"],
                "event_pk": store.event_pk(item["item_id"]),
                "event_sk": sk,
                "base_unit": _typed_unit(entry) or item["base_unit"],
            })
            buttons.append(_confirm_buttons(update_id, index, entry["phrase"]))
    return lines, dict(counts), buttons


def refresh_item(item) -> bool:
    """Recompute the item's cached state from its events. An item with no events left is removed."""
    events = store.list_events(item["item_id"])
    if not events:
        store.delete_item(item["item_id"])
        return False
    recompute(item, events)
    store.save_item(item)
    return True


def recompute(item, events):
    """Work out everything cached on an item from its events: pack size, brands, stock and status."""
    _update_pack_size(item, events)
    _update_brands(item, events)
    item.update(model.replay(events, item.get("pack_size")))


def learn_alias(item, phrase) -> bool:
    """Add the phrase to the item's aliases, so next time it matches without AI. False if it wasn't added."""
    alias = matcher.normalise(phrase)
    if (
        not alias
        or alias in matcher.names(item)
        or len(alias) > MAX_ALIAS_LENGTH
        or len(item["aliases"]) >= MAX_ALIASES
    ):
        return False
    item["aliases"].append(alias)
    return True


def confirm_same(pending):
    """"Same item" was tapped: the guessed phrase becomes an alias. Returns the item, or None if it is gone."""
    item = store.get_item(pending["matched_item_id"])
    if item is None:
        return None
    if learn_alias(item, pending["phrase"]):
        store.save_item(item)
    return item


def split_into_new_item(pending):
    """"New item" was tapped: make an item from the phrase and move the guessed event to it.

    Returns the new item, or None if the event has been undone in the meantime.
    """
    event = store.get_event(pending["event_pk"], pending["event_sk"])
    if event is None:
        return None
    items = store.list_items()
    old = next((item for item in items if item["item_id"] == pending["matched_item_id"]), None)
    # Someone may have made the item already, by tapping the same button under another entry.
    found = matcher.match(pending["phrase"], items)
    if found and found[1] == "exact" and found[0] != pending["matched_item_id"]:
        new = _by_id(items, found[0])
    else:
        new = _create_item(new_item_name(pending["phrase"]), pending["base_unit"], items, event["at"])
    sk = pending["event_sk"]
    store.put_event(new["item_id"], sk, event)
    store.delete_event(pending["event_pk"], sk)
    refresh_item(new)
    if old:
        refresh_item(old)
    # /undo has to find the event where it lives now.
    if any(recent["sk"] == sk for recent in store.list_recent_events(event["user_id"])):
        remember_for_undo(new, event, sk)
    return new


def new_item_name(phrase) -> str:
    """The name an item made from a sender's phrase gets: "HAND masks" -> "Hand masks"."""
    return " ".join(phrase.split()).capitalize()


def iso_utc(epoch_seconds) -> str:
    return datetime.fromtimestamp(epoch_seconds, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def epoch(iso) -> float:
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()


def month_key(epoch_seconds) -> str:
    """The Singapore-time month a moment falls in, as "yyyy-mm" (the STATS sort key)."""
    return datetime.fromtimestamp(epoch_seconds, SGT).strftime("%Y-%m")


def slugify(name) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug[:MAX_SLUG].strip("-") or "item"


def _resolve_item(entry, items, brands, at):
    """Return (item, how) for a format entry: the matcher first, the AI item match only if it can't decide.

    item is None when the AI says the phrase isn't a household product.
    Items created here are added to items. A known brand at the front of the
    phrase is moved to the entry's "brand".
    """
    found, phrase = _without_brand(entry, items, brands)
    if found:
        return _by_id(items, found[0]), found[1]
    answer = ai.match_item(phrase, items)
    if answer.item_id:
        return _by_id(items, answer.item_id), "ai"
    if not answer.name:
        return None, "ai"
    # The model sometimes returns no ID for an item it has named exactly as we know it.
    known = matcher.match(answer.name, items)
    if known and known[1] == "exact":
        return _by_id(items, known[0]), "ai"
    # A unit typed in the message says more about the product than the model's guess.
    return _create_item(answer.name, _typed_unit(entry) or answer.base_unit, items, at, phrase), "new"


def _resolve_named(entry, items, brands, at):
    """Return (item, how) for a free-text entry. The fallback has already matched
    the item or named a new one, so the small AI match isn't called again."""
    found, phrase = _without_brand(entry, items, brands)
    # Wording we already know beats the model's pick.
    if found and found[1] == "exact":
        return _by_id(items, found[0]), "exact"
    if entry["item_id"]:
        # A brand on its own ("Mama Lemon") is nothing to point out or to learn as a name.
        only_brand = matcher.normalise(phrase) == matcher.normalise(entry.get("brand") or "")
        return _by_id(items, entry["item_id"]), "exact" if only_brand else "ai"
    return _create_item(phrase, _typed_unit(entry) or entry["base_unit"], items, at), "new"


def _without_brand(entry, items, brands):
    """Match the entry's phrase, moving a known brand from its front to entry["brand"].

    Returns (match or None, the phrase that was matched).
    """
    found, phrase, brand = matcher.match_phrase(entry["phrase"], items, brands)
    if brand:
        entry["phrase"] = phrase
        entry["brand"] = entry.get("brand") or brand
    return found, phrase


def _by_id(items, item_id):
    return next(item for item in items if item["item_id"] == item_id)


def _create_item(name, base_unit, items, at, phrase=None):
    """Write a new item, with the sender's phrase as its first alias when that differs from the name."""
    item = {"name": name, "aliases": [], "base_unit": base_unit, "created_at": at}
    if phrase:
        learn_alias(item, phrase)
    base = slugify(name)
    taken = {known["item_id"] for known in items}
    number = 1
    while True:
        suffix = "" if number == 1 else f"-{number}"
        item["item_id"] = base[: MAX_SLUG - len(suffix)] + suffix
        if item["item_id"] not in taken and store.create_item(item):
            break
        number += 1
    items.append(item)
    return item


def _amount(entry, item):
    """The entry's total for this item (see structured.resolve_quantity), or why it can't be known."""
    if entry["action"] not in ("bought", "used", "stock_count", "price_check"):
        return NO_AMOUNT
    return structured.resolve_quantity(entry["quantity"], item["base_unit"], item.get("pack_size"))


def _typed_unit(entry):
    return (entry.get("quantity") or {}).get("unit")


def _event_record(entry, amount, sender, at, how) -> dict:
    total_price = round(entry["total_price"], 2) if entry["total_price"] else None
    return {
        "action": entry["action"],
        **amount,
        "total_price": total_price,
        "unit_price": round(total_price / amount["qty"], 6) if total_price and amount["qty"] else None,
        "currency": config.home_currency(),
        "store": entry["store"],
        "brand": entry.get("brand"),
        "user_id": sender["id"],
        "user_name": sender["name"],
        "raw_text": entry["raw"][:MAX_RAW_TEXT],
        "source": "chat",
        "parse_path": _parse_path(entry, how),
        "at": at,
    }


def _parse_path(entry, how) -> str:
    """What it took to read the entry: the format alone, the small AI match, or the full AI parse."""
    if entry.get("free_text"):
        return "ai_full"
    return "ai_match" if how in AI_PATHS else "structured"


def _update_pack_size(item, events):
    """The pack size follows the newest "2x10" entry or /set, whichever came later.

    Working it out from the events means undoing a mistyped "2x100" also undoes its pack size.
    """
    size, when = model.latest_pack_size(events)
    set_at = item.get("pack_size_set_at")
    if size and (not set_at or when > set_at):
        item["pack_size"] = size
    elif not set_at:
        item.pop("pack_size", None)


def remember_for_undo(item, event, sk):
    store.add_recent_event(event["user_id"], sk, {
        "event_pk": store.event_pk(item["item_id"]),
        "event_sk": sk,
        "item_name": item["name"],
        "summary": replies.summary(item, event),
        "at": event["at"],
    })


def _update_brands(item, events):
    """The brands recorded on the item's entries, oldest first.

    The matcher drops them from the front of a phrase, and /listbrands shows them.
    """
    brands = []
    for event in events:
        brand = event.get("brand")
        if brand and matcher.plain(brand) not in {matcher.plain(known) for known in brands}:
            brands.append(brand)
    if brands:
        item["brands"] = brands[-MAX_BRANDS:]
    else:
        item.pop("brands", None)


def _confirm_buttons(update_id, index, phrase) -> list:
    name = new_item_name(phrase)
    if len(name) > MAX_BUTTON_NAME:
        name = name[: MAX_BUTTON_NAME - 1] + "…"
    return [("Same item", f"m:same:{update_id}:{index}"), (f"New item: {name}", f"m:new:{update_id}:{index}")]
