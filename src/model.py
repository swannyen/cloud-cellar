"""Item state computed from the item's event history (pure, no I/O).

Everything is in the item's base unit (pieces, rolls, sheets, g, ml), using each
event's qty. Phase 1 covers stock and status only. Usage rates, run-out
predictions and snoozing (PLAN.md §8.1) are added in Phase 2.
"""


def replay(events, pack_size=None) -> dict:
    """Return the cached state for an item, given its events oldest first.

    pack_size is the item's base units per pack, if known. A value of None in
    the result means "not known yet".
    """
    stock = None
    status = None
    last_event_at = None
    for event in events:
        action = event["action"]
        qty = event_qty(event, pack_size)
        if action == "bought":
            if qty is not None:
                stock = (stock or 0) + qty
            status = "ok"
        elif action == "stock_count":
            if qty is not None:
                stock = qty
                status = "ok" if qty > (pack_size or 1) else "low"
        elif action == "opened_last":
            if pack_size:
                stock = pack_size
            status = "low"
        elif action == "used":
            # Only what is known to be there can be counted down.
            if qty is not None and stock is not None:
                stock = max(0, stock - qty)
                status = "out" if stock == 0 else "ok" if stock > (pack_size or 1) else "low"
        elif action == "finished":
            stock = 0
            status = "out"
        # still_have and price_check don't change stock (still_have snoozes nudges in Phase 2).
        last_event_at = event.get("at", last_event_at)
    return {"status": status, "stock_est": stock, "last_event_at": last_event_at}


def event_qty(event, pack_size=None):
    """The event's total in base units, or None if it can't be known.

    Events recorded before quantities were stored have no qty, only the packs
    and pack size they were written with.
    """
    if event.get("qty") is not None:
        return event["qty"]
    packs, size = event.get("packs"), event.get("units_per_pack") or pack_size
    if packs and size:
        return packs * size
    return None


def latest_pack_size(events):
    """(pack size, when) from the newest event written as packs x size, or (None, None)."""
    for event in reversed(events):
        if event.get("units_per_pack"):
            return event["units_per_pack"], event.get("at", "")
    return None, None
