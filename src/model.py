"""Item state computed from the item's event history (pure, no I/O).

Everything is in the item's base unit (pieces, rolls, sheets, g, ml), using each
event's qty. Phase 1 covers stock, status and which brands the stock is.
Usage rates, run-out predictions and snoozing (PLAN.md §8.1) are added in Phase 2.
"""
import matcher


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


def brands_left(events, pack_size=None) -> list:
    """What should still be at home, split by brand: [(brand or None, amount)], oldest purchase first.

    This is an estimate. An entry seldom says which pack was used, so the
    oldest is taken to go first, unless the entry names a brand. Whatever a
    count finds missing went the same way. The amounts add up to replay's
    stock_est; what has no brand comes last.
    """
    lots = []        # [brand, amount] for each purchase with something left, oldest first
    spellings = {}   # brand without capitals or punctuation -> the brand as first written
    for event in events:
        action = event["action"]
        qty = event_qty(event, pack_size)
        brand = matcher.plain(event.get("brand") or "") or None
        if brand:
            spellings.setdefault(brand, event["brand"])
        if action == "bought":
            if qty:
                lots.append([brand, qty])
        elif action == "stock_count":
            if qty is not None:
                _set_total(lots, qty, brand)
        elif action == "opened_last":
            if pack_size:
                _set_total(lots, pack_size, brand)
        elif action == "used":
            if qty is not None:
                _take(lots, qty, brand)
        elif action == "finished":
            lots.clear()
    left = {}
    for brand, amount in lots:
        left[brand] = _rounded(left.get(brand, 0) + amount)
    unbranded = left.pop(None, 0)
    return [(spellings[brand], amount) for brand, amount in left.items()] + ([(None, unbranded)] if unbranded else [])


def _take(lots, amount, brand=None):
    """Use up an amount: the named brand's first, then the oldest."""
    for lot in [lot for lot in lots if brand and lot[0] == brand] + lots:
        taken = min(lot[1], amount)
        lot[1] = _rounded(lot[1] - taken)
        amount = _rounded(amount - taken)
    lots[:] = [lot for lot in lots if lot[1] > 0]


def _set_total(lots, total, brand):
    """Make the lots add up to a counted amount. What is missing was used; anything extra is the brand named, if any."""
    known = sum(amount for _, amount in lots)
    if total < known:
        _take(lots, _rounded(known - total))
    elif total > known:
        lots.append([brand, _rounded(total - known)])


def _rounded(value):
    value = round(value, 6)
    return int(value) if value == int(value) else value


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
