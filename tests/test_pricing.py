"""Price book tests (PLAN.md §8.2): price history, repeated logs, currency and verdicts."""
import unittest
from datetime import date

import fakes  # noqa: F401  (puts src/ on sys.path)

import apply
import pricing

NOW = apply.epoch("2026-11-03T02:00:00Z")


def ev(price=1.0, at="2026-10-11T04:00:00Z", action="bought", store="Taobao", **fields):
    return {"action": action, "unit_price": price, "at": at, "store": store, "currency": "SGD", **fields}


def on_separate_days(*prices):
    return pricing.price_points([ev(price, at=f"2026-10-{day:02d}T04:00:00Z") for day, price in enumerate(prices, 1)], NOW)


class PricePointsTest(unittest.TestCase):
    def test_a_point_says_what_was_paid_where_and_when(self):
        self.assertEqual(pricing.price_points([ev(0.65)], NOW), [
            {"unit_price": 0.65, "day": date(2026, 10, 11), "store": "Taobao", "paid": True, "entries": 1},
        ])

    def test_a_price_seen_counts_but_is_not_called_paid(self):
        points = pricing.price_points([ev(0.65, action="price_check")], NOW)
        self.assertEqual([point["paid"] for point in points], [False])

    def test_entries_with_no_unit_price_are_left_out(self):
        self.assertEqual(pricing.price_points([ev(None), {"action": "bought", "at": "2026-10-11T04:00:00Z"}], NOW), [])

    def test_a_price_on_any_other_entry_is_left_out(self):
        for action in ("stock_count", "used", "finished"):
            self.assertEqual(pricing.price_points([ev(0.65, action=action)], NOW), [])

    def test_only_the_last_year_counts(self):
        points = pricing.price_points([ev(1.0, at="2025-11-02T04:00:00Z"), ev(2.0, at="2025-11-04T04:00:00Z")], NOW)
        self.assertEqual([point["unit_price"] for point in points], [2.0])

    def test_the_day_is_singapore_time(self):
        late_evening_utc = pricing.price_points([ev(at="2026-10-09T16:30:00Z")], NOW)
        self.assertEqual(late_evening_utc[0]["day"], date(2026, 10, 10))


class RepeatedLogsTest(unittest.TestCase):
    """Every entry is a true log. Repeats of one price on one day make one price point, not fewer entries."""

    def test_same_day_price_and_store_is_one_point_that_knows_how_many_entries(self):
        points = pricing.price_points([ev(0.145, at="2026-10-09T14:41:17Z"), ev(0.145, at="2026-10-09T15:14:28Z")], NOW)
        self.assertEqual([(point["unit_price"], point["entries"]) for point in points], [(0.145, 2)])

    def test_capitals_in_the_store_do_not_make_it_another_store(self):
        self.assertEqual(len(pricing.price_points([ev(store="Taobao"), ev(store="taobao")], NOW)), 1)

    def test_an_entry_with_no_store_joins_a_named_one_and_the_name_is_kept(self):
        for first, second in (("Taobao", None), (None, "Taobao")):
            points = pricing.price_points([ev(store=first), ev(store=second)], NOW)
            self.assertEqual([(point["store"], point["entries"]) for point in points], [("Taobao", 2)])

    def test_a_different_price_stays_separate(self):
        self.assertEqual(len(pricing.price_points([ev(0.00692), ev(0.00696)], NOW)), 2)

    def test_a_different_store_stays_separate(self):
        self.assertEqual(len(pricing.price_points([ev(store="Taobao"), ev(store="Shopee")], NOW)), 2)

    def test_a_different_day_stays_separate(self):
        points = pricing.price_points([ev(at="2026-10-09T04:00:00Z"), ev(at="2026-10-11T04:00:00Z")], NOW)
        self.assertEqual(len(points), 2)

    def test_days_split_at_midnight_singapore_time_not_utc(self):
        before, after = ev(at="2026-10-09T15:59:00Z"), ev(at="2026-10-09T16:01:00Z")
        self.assertEqual(len(pricing.price_points([before, after], NOW)), 2)
        same_day = ev(at="2026-10-09T16:01:00Z"), ev(at="2026-10-10T15:59:00Z")
        self.assertEqual(len(pricing.price_points(same_day, NOW)), 1)

    def test_checking_a_price_and_then_paying_it_is_called_paid(self):
        for actions in (("price_check", "bought"), ("bought", "price_check")):
            points = pricing.price_points([ev(action=actions[0]), ev(action=actions[1])], NOW)
            self.assertEqual([(point["paid"], point["entries"]) for point in points], [(True, 2)])

    def test_repeats_do_not_pull_the_usual_price_towards_them(self):
        events = [ev(1.0, at="2026-10-01T04:00:00Z")] * 3 + [
            ev(2.0, at="2026-10-02T04:00:00Z"), ev(3.0, at="2026-10-03T04:00:00Z"),
        ]
        self.assertEqual(pricing.usual_price(pricing.price_points(events, NOW)), 2.0)

    def test_repeats_do_not_reach_the_minimum_for_a_good_price(self):
        events = [ev(1.0, at="2026-10-01T04:00:00Z")] * 3 + [ev(2.0, at="2026-10-02T04:00:00Z")] * 3
        points = pricing.price_points(events, NOW)
        self.assertEqual(len(points), 2)
        self.assertEqual(pricing.verdict(1.2, points)["kind"], pricing.TYPICAL)


class CurrencyTest(unittest.TestCase):
    """Prices are SGD unless the log says otherwise."""

    def test_an_entry_with_no_currency_is_read_as_sgd(self):
        old = {"action": "bought", "unit_price": 0.145, "at": "2026-10-09T14:41:17Z", "store": "Taobao"}
        self.assertEqual(pricing.event_currency(old), "SGD")
        self.assertEqual([point["unit_price"] for point in pricing.price_points([old], NOW, "SGD")], [0.145])

    def test_old_and_new_entries_are_one_history(self):
        old = {"action": "bought", "unit_price": 1.0, "at": "2026-10-09T04:00:00Z"}
        self.assertEqual(len(pricing.price_points([old, ev(2.0)], NOW)), 2)

    def test_a_price_logged_in_another_currency_is_kept_apart(self):
        events = [ev(5.0), ev(1.0, currency="MYR", at="2026-10-12T04:00:00Z")]
        self.assertEqual([point["unit_price"] for point in pricing.price_points(events, NOW)], [5.0])
        self.assertEqual([point["unit_price"] for point in pricing.price_points(events, NOW, "MYR")], [1.0])

    def test_a_cheap_foreign_number_does_not_become_the_best_price(self):
        events = [ev(5.0), ev(1.0, currency="MYR", at="2026-10-12T04:00:00Z")]
        self.assertEqual(pricing.verdict(4.0, pricing.price_points(events, NOW))["best"]["unit_price"], 5.0)

    def test_other_currencies_are_named(self):
        events = [ev(5.0), ev(1.0, currency="MYR"), ev(2.0, currency="JPY"), ev(3.0, currency="MYR"), ev(None, currency="THB")]
        self.assertEqual(pricing.other_currencies(events), ["JPY", "MYR"])
        self.assertEqual(pricing.other_currencies([ev(5.0)]), [])


class VerdictTest(unittest.TestCase):
    FIVE = (1.0, 1.2, 1.4, 1.6, 1.8)  # best 1.0, lower quartile 1.2, median 1.4

    def kind(self, price, earlier=FIVE):
        return pricing.verdict(price, on_separate_days(*earlier))["kind"]

    def test_no_history_is_the_first_price(self):
        self.assertEqual(pricing.verdict(1.0, []), {"kind": pricing.FIRST, "price": 1.0})

    def test_a_new_low_is_the_best_price(self):
        self.assertEqual(self.kind(0.9), pricing.BEST)

    def test_matching_the_best_or_within_three_percent_is_the_best_price(self):
        self.assertEqual(self.kind(1.0), pricing.BEST)
        self.assertEqual(self.kind(1.03), pricing.BEST)
        self.assertNotEqual(self.kind(1.04), pricing.BEST)

    def test_at_or_below_the_lower_quartile_is_a_good_price(self):
        self.assertEqual(self.kind(1.04), pricing.GOOD)
        self.assertEqual(self.kind(1.2), pricing.GOOD)
        self.assertNotEqual(self.kind(1.21), pricing.GOOD)

    def test_a_good_price_needs_four_earlier_prices(self):
        self.assertEqual(self.kind(1.05, (1.0, 1.2, 1.4)), pricing.TYPICAL)
        self.assertEqual(self.kind(1.05, (1.0, 1.2, 1.4, 1.6)), pricing.GOOD)

    def test_well_over_the_median_is_above_usual(self):
        self.assertEqual(self.kind(1.7), pricing.ABOVE)
        self.assertEqual(self.kind(1.5, (1.0, 1.0)), pricing.ABOVE)

    def test_anything_else_is_typical(self):
        self.assertEqual(self.kind(1.3), pricing.TYPICAL)
        self.assertEqual(self.kind(1.5), pricing.TYPICAL)

    def test_the_verdict_carries_what_the_reply_needs(self):
        verdict = pricing.verdict(1.3, on_separate_days(*self.FIVE))
        self.assertEqual(verdict["price"], 1.3)
        self.assertEqual(verdict["best"]["unit_price"], 1.0)
        self.assertEqual(verdict["usual"], 1.4)

    def test_when_the_best_price_is_seen_twice_the_latest_is_named(self):
        points = pricing.price_points([ev(1.0, at="2026-10-01T04:00:00Z", store="Giant"), ev(1.0, store="Taobao")], NOW)
        self.assertEqual(pricing.best_point(points)["store"], "Taobao")


if __name__ == "__main__":
    unittest.main()
