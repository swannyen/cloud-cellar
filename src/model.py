"""Item state computed from the item's event history (pure, no I/O).

Phase 1 covers stock and status only. Usage rates, run-out predictions and
snoozing (PLAN.md §8.1) are added in Phase 2.
"""


def replay(events) -> dict:
    """Return the cached state for an item, given its events oldest first.

    A value of None means "not known yet".
    """
    stock = None
    status = None
    last_event_at = None
    for event in events:
        action = event["action"]
        packs = event.get("packs")
        if action == "bought":
            stock = (stock or 0) + (packs or 1)
            status = "ok"
        elif action == "stock_count":
            if packs is not None:
                stock = packs
                status = "ok" if packs > 1 else "low"
        elif action == "opened_last":
            stock = 1
            status = "low"
        elif action == "finished":
            stock = 0
            status = "out"
        # still_have and price_check don't change stock (still_have snoozes nudges in Phase 2).
        last_event_at = event.get("at", last_event_at)
    return {"status": status, "stock_est": stock, "last_event_at": last_event_at}
