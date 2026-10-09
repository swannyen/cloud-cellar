"""Apply parsed events to the ledger: resolve the item, record, replay, reply (PLAN.md §4.2)."""
import re
from datetime import datetime, timezone

import model
import replies
import store

MAX_SLUG = 40          # keeps "a:<action>:<item_id>" button data under Telegram's 64 bytes
MAX_RAW_TEXT = 300
MAX_ALIASES = 8
MAX_ALIAS_LENGTH = 30


def apply_events(events, *, items, sender, update_id, at, raw_text, source="chat") -> list:
    """Record each parsed event and return one reply line per event.

    items is the known-item list the parser saw; items created here are added to it.
    at is the event time as an ISO UTC string.
    """
    lines = []
    for index, event in enumerate(events):
        item, created = _resolve_item(event, items, at)
        record = _event_record(event, item, sender, at, raw_text, source)
        # The index keeps two events for one item in one message from overwriting each other.
        sk = f"{at}#{update_id}#{index}"
        store.put_event(item["item_id"], sk, record)
        store.set_last_event(sender["id"], store.event_pk(item["item_id"]), sk, at)
        refresh_item(item)
        lines.append(replies.event_line(item, record, created))
    return lines


def refresh_item(item) -> bool:
    """Recompute the item's cached state from its events. An item with no events left is removed."""
    events = store.list_events(item["item_id"])
    if not events:
        store.delete_item(item["item_id"])
        return False
    item.update(model.replay(events))
    store.save_item(item)
    return True


def iso_utc(epoch_seconds) -> str:
    return datetime.fromtimestamp(epoch_seconds, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def epoch(iso) -> float:
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()


def unit_price(event):
    packs, units, total = event.get("packs"), event.get("units_per_pack"), event.get("total_price")
    if packs and units and total:
        return round(total / (packs * units), 6)
    return None


def slugify(name) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug[:MAX_SLUG].strip("-") or "item"


def _resolve_item(event, items, at):
    """Return (item, created). Reuses a known item where possible."""
    item = _find_item(event, items)
    if item is not None:
        _learn_alias(item, event)
        return item, False
    item = {
        "name": event["item_name"],
        "aliases": [],
        "base_unit": event["base_unit"],
        "created_at": at,
    }
    base = slugify(event["item_name"])
    taken = {known["item_id"] for known in items}
    number = 1
    while True:
        suffix = "" if number == 1 else f"-{number}"
        item["item_id"] = base[: MAX_SLUG - len(suffix)] + suffix
        if item["item_id"] not in taken and store.create_item(item):
            break
        number += 1
    items.append(item)
    return item, True


def _find_item(event, items):
    wording = event["item_name"].lower()
    for item in items:
        if item["item_id"] == event["item_id"]:
            return item
    # The model sometimes returns no ID for an item it has named exactly as we know it.
    for item in items:
        if wording == item["name"].lower() or wording in _lowered(item["aliases"]):
            return item
    return None


def _learn_alias(item, event):
    """Remember the sender's wording so later messages map to this item more easily."""
    wording = event["item_name"].lower()
    known = _lowered(item["aliases"]) | {item["name"].lower(), (event.get("brand") or "").lower()}
    if (
        wording not in known
        and 2 <= len(wording) <= MAX_ALIAS_LENGTH
        and len(item["aliases"]) < MAX_ALIASES
    ):
        item["aliases"].append(wording)


def _event_record(event, item, sender, at, raw_text, source) -> dict:
    record = {
        "action": event["action"],
        "packs": event["packs"],
        "units_per_pack": _units_for(event, item),
        "total_price": round(event["total_price"], 2) if event["total_price"] else None,
        "store": event["store"],
        "brand": event["brand"],
        "user_id": sender["id"],
        "user_name": sender["name"],
        "raw_text": raw_text[:MAX_RAW_TEXT],
        "source": source,
        "at": at,
    }
    record["unit_price"] = unit_price(record)
    return record


def _units_for(event, item):
    """A pack size is only comparable if it is in the item's own base unit."""
    units = {event["base_unit"], item["base_unit"]}
    # Household liquids are sold by weight or volume interchangeably; 1 ml is close to 1 g.
    if len(units) == 1 or units == {"g", "ml"}:
        return event["units_per_pack"]
    return None


def _lowered(values) -> set:
    return {value.lower() for value in values}
