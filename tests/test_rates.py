"""Exchange-rate tests (PLAN.md §8.5): which rate an entry gets, and the Frankfurter call. No network."""
import io
import json
import unittest
import urllib.error
from datetime import date
from unittest import mock

from fakes import FakeTable

import rates
import store

TODAY = date(2026, 11, 13)
FETCHED = {"CNY": {"rate": 5.1901, "date": "2026-11-13"}, "MYR": {"rate": 3.1744, "date": "2026-11-13"},
           "USD": {"rate": 0.77888, "date": "2026-11-13"}}


class RateBookTest(unittest.TestCase):
    def setUp(self):
        self.table = FakeTable()
        patches = [mock.patch.object(store, "_table", self.table),
                   mock.patch.object(rates, "fetch_rates", return_value=FETCHED)]
        started = [patch.start() for patch in patches]
        for patch in patches:
            self.addCleanup(patch.stop)
        self.fetch = started[1]

    def save_day(self, day, **rates_by_code):
        store.save_fx(day, {"rates": rates_by_code, "dates": dict.fromkeys(rates_by_code, day),
                            "source": "frankfurter", "fetched_at": f"{day}T01:00:00Z"})

    def test_with_nothing_saved_the_day_is_fetched_and_saved(self):
        rate = rates.RateBook(TODAY).rate("MYR", TODAY)
        self.assertEqual(rate, {"fx_to_home": 0.31502, "fx_date": "2026-11-13", "fx_source": "frankfurter"})
        self.fetch.assert_called_once_with(TODAY, TODAY)
        saved = store.list_fx()
        self.assertEqual([(record["sk"], record["rates"], record["source"]) for record in saved],
                         [("2026-11-13", {"CNY": 5.1901, "MYR": 3.1744, "USD": 0.77888}, "frankfurter")])

    def test_one_fetch_serves_every_currency_and_entry_of_the_message(self):
        book = rates.RateBook(TODAY)
        book.rate("MYR", TODAY)
        book.rate("CNY", TODAY)
        book.rate("MYR", TODAY)
        self.assertEqual(self.fetch.call_count, 1)

    def test_a_saved_day_is_used_without_fetching(self):
        self.save_day("2026-11-13", MYR=3.2)
        self.assertEqual(rates.RateBook(TODAY).rate("MYR", TODAY)["fx_to_home"], 0.3125)
        self.fetch.assert_not_called()

    def test_a_day_in_the_past_gets_that_days_rate(self):
        self.save_day("2026-11-13", MYR=3.1744)
        self.fetch.return_value = {"MYR": {"rate": 3.2091, "date": "2026-09-15"}}
        rate = rates.RateBook(TODAY).rate("MYR", date(2026, 9, 15))
        self.assertEqual((rate["fx_to_home"], rate["fx_date"]), (0.311614, "2026-09-15"))
        self.fetch.assert_called_once_with(date(2026, 9, 15), TODAY)

    def test_when_the_fetch_fails_a_rate_from_the_week_before_will_do(self):
        self.fetch.return_value = {}
        self.save_day("2026-11-08", MYR=3.1687)
        rate = rates.RateBook(TODAY).rate("MYR", TODAY)
        self.assertEqual((rate["fx_to_home"], rate["fx_date"]), (0.315587, "2026-11-08"))

    def test_but_not_an_older_one_and_then_there_is_none(self):
        self.fetch.return_value = {}
        self.save_day("2026-11-05", MYR=3.1687)
        book = rates.RateBook(TODAY)
        self.assertIsNone(book.rate("MYR", TODAY))
        self.assertIsNone(book.rate("MYR", TODAY))
        self.assertEqual(self.fetch.call_count, 1)  # tried once per message, not once per entry

    def test_a_later_saved_rate_is_never_used_for_an_earlier_day(self):
        self.fetch.return_value = {}
        self.save_day("2026-11-13", MYR=3.1744)
        self.assertIsNone(rates.RateBook(TODAY).rate("MYR", date(2026, 9, 15)))

    def test_a_fetch_adds_to_what_was_saved_for_the_day(self):
        self.save_day("2026-11-13", MYR=3.17)
        self.fetch.return_value = {"CNY": {"rate": 5.19, "date": "2026-11-13"}}
        rates.RateBook(TODAY).rate("CNY", TODAY)
        saved, = store.list_fx()
        self.assertEqual(saved["rates"], {"MYR": 3.17, "CNY": 5.19})

class FetchTest(unittest.TestCase):
    def respond(self, body):
        return mock.patch.object(rates.urllib.request, "urlopen", return_value=io.BytesIO(json.dumps(body).encode()))

    def test_asks_for_every_tracked_currency_against_the_base(self):
        rows = [{"date": "2026-11-13", "base": "SGD", "quote": code, "rate": rate}
                for code, rate in (("CNY", 5.1901), ("MYR", 3.1744), ("USD", 0.77888))]
        with self.respond(rows) as urlopen:
            self.assertEqual(rates.fetch_rates(TODAY, TODAY), FETCHED)
        request = urlopen.call_args[0][0]
        self.assertEqual(request.full_url, "https://api.frankfurter.dev/v2/rates?base=SGD&quotes=CNY%2CMYR%2CUSD")
        self.assertEqual(urlopen.call_args[1], {"timeout": rates.TIMEOUT_SECONDS})

    def test_a_day_in_the_past_asks_for_that_date(self):
        with self.respond([]) as urlopen, self.assertLogs(rates.logger, "WARNING"):   # no rows: nothing to use
            rates.fetch_rates(date(2026, 9, 15), TODAY)
        self.assertTrue(urlopen.call_args[0][0].full_url.endswith("&date=2026-09-15"))

    def test_rows_that_dont_make_sense_are_left_out(self):
        rows = [{"date": "2026-11-13", "base": "SGD", "quote": "MYR", "rate": 0},
                {"date": "2026-11-13", "base": "USD", "quote": "CNY", "rate": 7.1},
                {"date": "2026-11-13", "base": "SGD", "quote": "JPY", "rate": 124.3},
                {"date": "2026-11-13", "base": "SGD", "quote": "USD", "rate": "0.78"},
                "nonsense"]
        with self.respond(rows), self.assertLogs(rates.logger, "WARNING"):
            self.assertEqual(rates.fetch_rates(TODAY, TODAY), {})

    def test_failures_return_nothing_and_never_raise(self):
        errors = [urllib.error.URLError("down"), TimeoutError(), urllib.error.HTTPError("u", 422, "x", {}, None)]
        for error in errors:
            with self.subTest(error=type(error).__name__), \
                    mock.patch.object(rates.urllib.request, "urlopen", side_effect=error), \
                    self.assertLogs(rates.logger, "WARNING"):
                self.assertEqual(rates.fetch_rates(TODAY, TODAY), {})
        with mock.patch.object(rates.urllib.request, "urlopen", return_value=io.BytesIO(b"<html>")), \
                self.assertLogs(rates.logger, "WARNING"):
            self.assertEqual(rates.fetch_rates(TODAY, TODAY), {})
        with self.respond({"status": 422, "message": "invalid currency: XYZ"}), self.assertLogs(rates.logger, "WARNING"):
            self.assertEqual(rates.fetch_rates(TODAY, TODAY), {})


if __name__ == "__main__":
    unittest.main()
