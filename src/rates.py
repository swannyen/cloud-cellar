"""Exchange rates for entries priced in another currency (PLAN.md §8.5).

A rate is looked up only while an entry that needs one is recorded: a foreign
price written without its SGD price. The first of these that has one is used:
a rate saved for the entry's day, a fetch from Frankfurter for that day (saved
for next time), then the newest saved rate from the week before. Nothing
fetches rates on a schedule, and there is no setting a rate by hand.

A rate here is always fx_to_home: base-currency units for 1 unit of the currency.
"""
import json
import logging
import math
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone

import currency
import store

logger = logging.getLogger()

API_URL = "https://api.frankfurter.dev/v2/rates"
TIMEOUT_SECONDS = 5
USER_AGENT = "cloud-cellar-bot"
FALLBACK_DAYS = 7           # how old a saved rate may be when the day's own can't be had
WRITTEN, FRANKFURTER = "written", "frankfurter"   # the fx_source values


class RateBook:
    """The rates for one message: the saved records are read once, and each day is fetched at most once."""

    def __init__(self, today):
        self.today = today      # the Singapore date the message was sent
        self._records = None    # sk -> saved FX record
        self._tried = set()     # days already asked of Frankfurter

    def rate(self, code, day):
        """{"fx_to_home", "fx_date", "fx_source"} for the currency on a day (a date), or None."""
        records = self._saved()
        found = _day_rate(records.get(day.isoformat()), code)
        if found is None and day not in self._tried:
            self._tried.add(day)
            fetched = fetch_rates(day, self.today)
            if fetched:
                record = {**_merged(records.get(day.isoformat()), fetched), "sk": day.isoformat()}
                store.save_fx(day.isoformat(), {key: value for key, value in record.items() if key != "sk"})
                records[day.isoformat()] = record
                found = _day_rate(record, code)
        return found or _recent_rate(code, day, records)

    def _saved(self) -> dict:
        if self._records is None:
            self._records = {record["sk"]: record for record in store.list_fx()}
        return self._records


def newest_saved(code, records, before=None):
    """The newest fetched rate saved for the currency, from a day before `before` (a date) if given, or None."""
    days = sorted(records, reverse=True)
    for day in days:
        if before is not None and day >= before.isoformat():
            continue
        found = _day_rate(records[day], code)
        if found:
            return found
    return None


def fetch_rates(day, today) -> dict:
    """Frankfurter's rates for every tracked foreign currency on a day: {code: {"rate", "date"}}, or {} if it fails.

    The rate is how many units of the currency 1 base unit buys. For today the newest
    rates are asked for; for a day in the past, that day's. Never raises.
    """
    params = {"base": currency.home(), "quotes": ",".join(currency.foreign_codes())}
    if day < today:
        params["date"] = day.isoformat()
    request = urllib.request.Request(
        f"{API_URL}?{urllib.parse.urlencode(params)}",
        headers={"user-agent": USER_AGENT, "accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            rows = json.load(response)
    except (OSError, ValueError) as err:   # URLError, HTTPError, timeouts, and a body that isn't JSON
        logger.warning("exchange rates for %s: %s", day, type(err).__name__)
        return {}
    rates = {}
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict) or row.get("base") != currency.home():
            continue
        code, rate, on = row.get("quote"), row.get("rate"), row.get("date")
        if code in currency.foreign_codes() and _positive(rate) and isinstance(on, str):
            rates[code] = {"rate": rate, "date": on[:10]}
    if not rates:
        logger.warning("exchange rates for %s: no usable rows", day)
    return rates


def _merged(saved, fetched) -> dict:
    """The day's record with the fetched rates added to what was saved for it before."""
    saved = saved or {}
    return {
        "rates": {**saved.get("rates", {}), **{code: found["rate"] for code, found in fetched.items()}},
        "dates": {**saved.get("dates", {}), **{code: found["date"] for code, found in fetched.items()}},
        "source": FRANKFURTER,
        "fetched_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def _day_rate(record, code):
    """The rate for a currency in one saved day's record, or None."""
    if not record or not _positive((record.get("rates") or {}).get(code)):
        return None
    return {
        "fx_to_home": currency.rate_figure(1 / record["rates"][code]),
        "fx_date": (record.get("dates") or {}).get(code) or record["sk"],
        "fx_source": record.get("source") or FRANKFURTER,
    }


def _recent_rate(code, day, records):
    """The newest saved rate from the FALLBACK_DAYS before the day, or None."""
    found = newest_saved(code, records, before=day)
    if found and date.fromisoformat(found["fx_date"]) >= day - timedelta(days=FALLBACK_DAYS):
        return found
    return None


def _positive(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value > 0
