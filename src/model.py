"""Item state computed from the item's event history (pure, no I/O).

Everything is in the item's base unit (pieces, rolls, sheets, g, ml), using each
event's qty. Built so far: stock, status, which brands the stock is, and what to
use next by expiry date. Usage rates, run-out predictions and snoozing
(PLAN.md §8.1) are added in Phase 3.
"""
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import matcher

SGT = timezone(timedelta(hours=8))  # a stocktake is one day's counts, by the household's clock


def replay(events, pack_size=None) -> dict:
    """Return the cached state for an item, given its events oldest first.

    pack_size is the item's base units per pack, if known. A value of None in
    the result means "not known yet".
    """
    stock = _Stock(pack_size)
    status = None
    last_event_at = None
    for event in events:
        action = event["action"]
        counted = stock.apply(event)
        if action == "bought":
            status = "ok"
        elif action == "stock_count":
            if counted:
                status = _level(stock.total(), pack_size)
        elif action == "opened_last":
            status = "low"
        elif action == "used":
            # Only what is known to be there can be counted down.
            if counted:
                status = "out" if stock.total() == 0 else _level(stock.total(), pack_size)
        elif action == "finished":
            status = "out"
        # still_have and price_check don't change stock (still_have snoozes nudges in Phase 3).
        last_event_at = event.get("at", last_event_at)
    return {"status": status, "stock_est": stock.total(), "last_event_at": last_event_at}


def brands_left(events, pack_size=None) -> list:
    """What should still be at home, split by brand: [(brand or None, amount)], oldest purchase first.

    This is an estimate. An entry seldom says which pack was used, so the one
    that expires first is taken to go first, then the oldest, unless the entry
    names a brand. Whatever a count finds missing went the same way. The
    amounts add up to replay's stock_est; what has no brand comes last.
    """
    stock = _Stock(pack_size)
    for event in events:
        stock.apply(event)
    left = {}
    for lot in stock.lots:
        left[lot.brand] = _rounded(left.get(lot.brand, 0) + lot.amount)
    unbranded = left.pop(None, 0)
    return [(stock.spellings[brand], amount) for brand, amount in left.items()] + ([(None, unbranded)] if unbranded else [])


def batches_left(events, pack_size=None) -> list:
    """What should still be at home, lot by lot, in the order to use it: what expires first, then the oldest.

    A lot is what one purchase or one line of a count brought in. Each comes
    back as {"brand", "amount", "expires_on", "size"}: the brand as first
    written or None, the amount in base units, the expiry as an ISO date or
    None, and the base units in one pack when that is known. Lots with no
    expiry come last. The amounts add up to replay's stock_est.
    """
    stock = _Stock(pack_size)
    for event in events:
        stock.apply(event)
    return [
        {"brand": stock.spellings.get(lot.brand), "amount": lot.amount, "expires_on": lot.expires_on, "size": lot.size}
        for lot in _use_order(stock.lots)
    ]


@dataclass
class _Lot:
    """Some of an item that came in together: one purchase, or one line of a count."""
    brand: str | None              # without capitals or punctuation
    amount: float                  # base units still there
    expires_on: str | None = None  # ISO date
    size: float | None = None      # base units in one pack


class _Stock:
    """What should be at home, as lots (see _Lot), oldest first.

    replay, brands_left and batches_left all read this, so the brands and the
    lots always add up to the stock figure.

    Counts follow PLAN.md §8.1. The first count of an item on a day replaces
    everything known about it. The counts after it that day carry on the same
    stocktake, so they add up brand by brand: a brand already counted that
    day, in an earlier message, is counted again from scratch, and having no
    brand counts as a brand of its own. The lines of one message always add
    up. A count marked "adds" ("I have another 3 bottles") adds to whatever is there.
    """

    def __init__(self, pack_size):
        self.pack_size = pack_size
        self.lots = []
        self.known = False      # no figure until something is bought, counted or runs out
        self.spellings = {}     # brand without capitals or punctuation -> the brand as first written
        self.stocktake = None   # (day, {brand: the message that last counted it}) for today's counts

    def total(self):
        return _rounded(sum(lot.amount for lot in self.lots)) if self.known else None

    def apply(self, event) -> bool:
        """Take one event into account. True if it gave a figure for the stock."""
        action = event["action"]
        qty = event_qty(event, self.pack_size)
        brand = matcher.plain(event.get("brand") or "") or None
        if brand:
            self.spellings.setdefault(brand, event["brand"])
        if action == "bought":
            if qty is None:
                return False
            self._add(brand, qty, event)
        elif action == "stock_count":
            if qty is None:
                return False
            self._count(event, qty, brand)
        elif action == "opened_last":
            if not self.pack_size:
                return False
            _set_total(self.lots, _Lot(brand, self.pack_size, size=self.pack_size))
        elif action == "used":
            if qty is None or not self.known:
                return False
            _take(self.lots, qty, brand)
            return True
        elif action == "finished":
            self.lots.clear()
        else:
            return False
        self.known = True
        return True

    def _count(self, event, qty, brand):
        if event.get("adds"):
            self._add(brand, qty, event)
            return
        day, message = _day(event), _message(event)
        counted = self.stocktake[1] if self.stocktake and self.stocktake[0] == day else None
        # A recount with no brand, when nothing branded has been counted that day, starts the stocktake over.
        if counted is None or (brand is None and set(counted) == {None} and counted[None] != message):
            if brand:
                self.lots = []
                self._add(brand, qty, event)
            else:
                _set_total(self.lots, self._lot(None, qty, event))  # says nothing about brands, so the earlier ones are kept
            self.stocktake = (day, {brand: message})
            return
        if counted.get(brand, message) != message:
            self.lots = [lot for lot in self.lots if lot.brand != brand]   # counted again: the new figure replaces it
        self._add(brand, qty, event)
        counted[brand] = message

    def _add(self, brand, qty, event):
        if qty:
            self.lots.append(self._lot(brand, qty, event))

    def _lot(self, brand, qty, event):
        size = event.get("units_per_pack") or (qty / event["packs"] if event.get("packs") else None)
        return _Lot(brand, qty, event.get("expires_on"), _rounded(size) if size else None)


def _level(stock, pack_size) -> str:
    return "ok" if stock > (pack_size or 1) else "low"


def _day(event):
    at = event.get("at")
    return datetime.fromisoformat(at.replace("Z", "+00:00")).astimezone(SGT).date() if at else None


def _message(event):
    """Which message an event came from. Its sort key is "<at>#<update_id>#<line>"; without one, its time."""
    sk = event.get("sk")
    return sk.rpartition("#")[0] if sk else event.get("at")


def _use_order(lots, brand=None) -> list:
    """The lots in the order to use them: the named brand's first, then what expires first, then the oldest."""
    return sorted(lots, key=lambda lot: (not (brand and lot.brand == brand), lot.expires_on is None, lot.expires_on or ""))


def _take(lots, amount, brand=None):
    """Use up an amount: the named brand's first, then what expires first, then the oldest."""
    for lot in _use_order(lots, brand):
        taken = min(lot.amount, amount)
        lot.amount = _rounded(lot.amount - taken)
        amount = _rounded(amount - taken)
    lots[:] = [lot for lot in lots if lot.amount > 0]


def _set_total(lots, counted):
    """Make the lots add up to a counted lot's amount. What is missing was used; anything extra is the counted lot."""
    known = sum(lot.amount for lot in lots)
    if counted.amount < known:
        _take(lots, _rounded(known - counted.amount))
    elif counted.amount > known:
        counted.amount = _rounded(counted.amount - known)
        lots.append(counted)


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
