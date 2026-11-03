"""The price book: an item's price history and how a new price compares with it (PLAN.md §8.2).

Pure, no I/O. Unit prices are per base unit, in the event's own currency. The
stock-up suggestion needs the usage rate, so it comes with Phase 3.
"""
import statistics
from datetime import datetime, timedelta, timezone

import config

SGT = timezone(timedelta(hours=8))  # Singapore has no daylight saving
PRICED_ACTIONS = ("bought", "price_check")  # paid and seen; a price on any other entry says nothing
HISTORY_DAYS = 365
NEAR_BEST = 1.03           # this close to the best price still counts as the best
ABOVE_USUAL = 1.15         # this far over the median is above usual
MIN_POINTS_FOR_GOOD = 4    # a quartile of fewer prices says little

FIRST, BEST, GOOD, ABOVE, TYPICAL = "first", "best", "good", "above", "typical"


def price_points(events, now, currency=None) -> list:
    """The item's prices in one currency (the home currency by default), oldest first. now is epoch seconds.

    Every entry is a true log, and each one counts towards stock. But a price
    says the same thing however many times it is written, so entries on one
    day with the same unit price and store are one point, and "entries" says
    how many. A point is {"unit_price", "day", "store", "paid", "entries"}.
    """
    currency = currency or config.home_currency()
    oldest = now - HISTORY_DAYS * 24 * 60 * 60
    points = []
    for event in events:
        if not _has_price(event) or event_currency(event) != currency:
            continue
        when = datetime.fromisoformat(event["at"].replace("Z", "+00:00"))
        if when.timestamp() < oldest:
            continue
        point = {
            "unit_price": event["unit_price"],
            "day": when.astimezone(SGT).date(),
            "store": event.get("store"),
            "paid": event["action"] == "bought",
            "entries": 1,
        }
        repeat = next((known for known in points if _same_price(known, point)), None)
        if repeat is None:
            points.append(point)
        else:
            repeat["entries"] += 1
            repeat["paid"] = repeat["paid"] or point["paid"]
            repeat["store"] = repeat["store"] or point["store"]
    return points


def event_currency(event) -> str:
    """A price is in the home currency unless its event says otherwise.

    The purchases logged before events stored a currency have none.
    """
    return event.get("currency") or config.home_currency()


def other_currencies(events) -> list:
    """The currencies other than the home one that the item has prices in."""
    return sorted({event_currency(event) for event in events if _has_price(event)} - {config.home_currency()})


def best_point(points) -> dict:
    """The cheapest point, and the most recent one if the price has been seen more than once."""
    return min(reversed(points), key=lambda point: point["unit_price"])


def usual_price(points) -> float:
    return statistics.median(point["unit_price"] for point in points)


def verdict(price, points) -> dict:
    """How a new unit price compares with the earlier points: {"kind", "price", "best", "usual"}."""
    if not points:
        return {"kind": FIRST, "price": price}
    prices = [point["unit_price"] for point in points]
    best = best_point(points)
    usual = usual_price(points)
    if price <= best["unit_price"] * NEAR_BEST:
        kind = BEST
    elif len(points) >= MIN_POINTS_FOR_GOOD and price <= statistics.quantiles(prices, n=4, method="inclusive")[0]:
        kind = GOOD
    elif price >= usual * ABOVE_USUAL:
        kind = ABOVE
    else:
        kind = TYPICAL
    return {"kind": kind, "price": price, "best": best, "usual": usual}


def _has_price(event) -> bool:
    return event["action"] in PRICED_ACTIONS and bool(event.get("unit_price"))


def _same_price(known, point) -> bool:
    """One day, one price, one store. An entry that names no store could be any of them."""
    stores = known["store"], point["store"]
    return (
        known["day"] == point["day"]
        and known["unit_price"] == point["unit_price"]
        and (not all(stores) or stores[0].lower() == stores[1].lower())
    )
