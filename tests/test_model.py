"""Replay tests for the Phase 1 part of the model: stock and status, in base units."""
import random
import unittest

import fakes  # noqa: F401  (puts src/ on sys.path)

import model


def ev(action, qty=None, at="2026-10-09T10:00:00Z", **fields):
    return {"action": action, "qty": qty, "at": at, **fields}


class ReplayTest(unittest.TestCase):
    def test_no_events_means_nothing_is_known(self):
        self.assertEqual(model.replay([]), {"status": None, "stock_est": None, "last_event_at": None})

    def test_bought_adds_the_quantity_and_marks_ok(self):
        state = model.replay([ev("bought", 20), ev("bought", 30)])
        self.assertEqual((state["status"], state["stock_est"]), ("ok", 50))

    def test_mixed_quantities_on_one_item_add_up(self):
        # "5pcs", then "2x5pcs"
        events = [ev("bought", 5), ev("bought", 10, packs=2, units_per_pack=5)]
        self.assertEqual(model.replay(events, pack_size=5)["stock_est"], 15)

    def test_bought_with_an_unknown_quantity_marks_ok_without_guessing_stock(self):
        state = model.replay([ev("bought")])
        self.assertEqual((state["status"], state["stock_est"]), ("ok", None))

    def test_finished_marks_out_and_empties_stock(self):
        state = model.replay([ev("bought", 20), ev("finished")])
        self.assertEqual((state["status"], state["stock_est"]), ("out", 0))

    def test_buying_after_running_out_restocks(self):
        state = model.replay([ev("finished"), ev("bought", 10)])
        self.assertEqual((state["status"], state["stock_est"]), ("ok", 10))

    def test_used_takes_the_amount_off_the_stock(self):
        # 180 packets of 6, then 90 packets used.
        state = model.replay([ev("bought", 540), ev("bought", 540), ev("used", 540)], pack_size=6)
        self.assertEqual((state["status"], state["stock_est"]), ("ok", 540))

    def test_used_down_to_a_pack_or_less_is_low_and_down_to_nothing_is_out(self):
        self.assertEqual(model.replay([ev("bought", 30), ev("used", 20)], pack_size=10)["status"], "low")
        self.assertEqual(model.replay([ev("bought", 30), ev("used", 25)], pack_size=10)["stock_est"], 5)
        state = model.replay([ev("bought", 30), ev("used", 30)], pack_size=10)
        self.assertEqual((state["status"], state["stock_est"]), ("out", 0))

    def test_using_more_than_there_is_stops_at_nothing_left(self):
        state = model.replay([ev("bought", 30), ev("used", 50)])
        self.assertEqual((state["status"], state["stock_est"]), ("out", 0))

    def test_used_with_no_stock_figure_changes_nothing(self):
        self.assertEqual(model.replay([ev("used", 5)]), {"status": None, "stock_est": None, "last_event_at": "2026-10-09T10:00:00Z"})
        state = model.replay([ev("bought", 30), ev("used")])
        self.assertEqual((state["status"], state["stock_est"]), ("ok", 30))

    def test_buying_after_using_adds_to_what_was_left(self):
        self.assertEqual(model.replay([ev("bought", 30), ev("used", 20), ev("bought", 30)])["stock_est"], 40)

    def test_opened_last_leaves_one_pack_marked_low(self):
        state = model.replay([ev("bought", 40), ev("opened_last")], pack_size=10)
        self.assertEqual((state["status"], state["stock_est"]), ("low", 10))

    def test_opened_last_without_a_pack_size_keeps_the_stock_figure(self):
        state = model.replay([ev("bought", 40), ev("opened_last")])
        self.assertEqual((state["status"], state["stock_est"]), ("low", 40))

    def test_stock_count_sets_stock_directly(self):
        self.assertEqual(model.replay([ev("bought", 90), ev("stock_count", 30)])["stock_est"], 30)

    def test_stock_count_of_one_pack_or_less_is_low(self):
        self.assertEqual(model.replay([ev("stock_count", 30)], pack_size=10)["status"], "ok")
        self.assertEqual(model.replay([ev("stock_count", 10)], pack_size=10)["status"], "low")
        self.assertEqual(model.replay([ev("stock_count", 3)])["status"], "ok")
        self.assertEqual(model.replay([ev("stock_count", 1)])["status"], "low")

    def test_stock_count_without_a_number_changes_nothing(self):
        state = model.replay([ev("bought", 20), ev("stock_count")])
        self.assertEqual((state["status"], state["stock_est"]), ("ok", 20))

    def test_still_have_and_price_check_do_not_change_stock(self):
        state = model.replay([ev("finished"), ev("still_have"), ev("price_check", 3000)])
        self.assertEqual((state["status"], state["stock_est"]), ("out", 0))

    def test_price_check_alone_leaves_status_unknown(self):
        self.assertIsNone(model.replay([ev("price_check", 3000)])["status"])

    def test_last_event_time_is_the_newest_event(self):
        state = model.replay([ev("bought", 1, "2026-10-01T00:00:00Z"), ev("finished", at="2026-10-09T00:00:00Z")])
        self.assertEqual(state["last_event_at"], "2026-10-09T00:00:00Z")

    def test_dropping_the_last_event_restores_the_earlier_state(self):
        events = [ev("bought", 20), ev("finished")]
        self.assertEqual(model.replay(events)["status"], "out")
        undone = model.replay(events[:-1])
        self.assertEqual((undone["status"], undone["stock_est"]), ("ok", 20))


class BrandsLeftTest(unittest.TestCase):
    """What is left, split by brand: an estimate that takes the oldest purchase to be used first."""

    KLEENEX_THEN_SCOTT = [ev("bought", 20, brand="Kleenex"), ev("bought", 30, brand="Scott")]

    def left(self, *later, pack_size=None):
        return model.brands_left(self.KLEENEX_THEN_SCOTT + list(later), pack_size)

    def test_nothing_bought_means_nothing_left(self):
        self.assertEqual(model.brands_left([]), [])
        self.assertEqual(model.brands_left([ev("used", 5), ev("still_have"), ev("bought")]), [])

    def test_purchases_add_up_per_brand_oldest_first_with_no_brand_last(self):
        events = [ev("bought", 800), ev("bought", 1600, brand="Shokubutsu"), ev("bought", 800, brand="半亩花田"),
                  ev("bought", 800, brand="SHOKUBUTSU")]
        self.assertEqual(model.brands_left(events), [("Shokubutsu", 2400), ("半亩花田", 800), (None, 800)])

    def test_what_is_used_comes_off_the_oldest_purchase(self):
        self.assertEqual(self.left(ev("used", 5)), [("Kleenex", 15), ("Scott", 30)])
        self.assertEqual(self.left(ev("used", 25)), [("Scott", 25)])
        self.assertEqual(self.left(ev("used", 80)), [])

    def test_a_used_entry_that_names_a_brand_comes_off_that_brand_first(self):
        self.assertEqual(self.left(ev("used", 25, brand="scott")), [("Kleenex", 20), ("Scott", 5)])
        self.assertEqual(self.left(ev("used", 40, brand="Scott")), [("Kleenex", 10)])
        # A brand that isn't in stock changes nothing about the order.
        self.assertEqual(self.left(ev("used", 5, brand="Vinda")), [("Kleenex", 15), ("Scott", 30)])

    def test_running_out_clears_every_brand(self):
        self.assertEqual(self.left(ev("finished")), [])
        self.assertEqual(self.left(ev("finished"), ev("bought", 10, brand="Vinda")), [("Vinda", 10)])

    def test_a_count_that_finds_less_takes_the_difference_off_the_oldest(self):
        self.assertEqual(self.left(ev("stock_count", 35)), [("Kleenex", 5), ("Scott", 30)])
        self.assertEqual(self.left(ev("stock_count", 0)), [])

    def test_a_count_that_finds_more_adds_the_difference_with_no_brand(self):
        self.assertEqual(self.left(ev("stock_count", 60)), [("Kleenex", 20), ("Scott", 30), (None, 10)])
        self.assertEqual(model.brands_left([ev("stock_count", 12)]), [(None, 12)])

    def test_a_count_that_names_a_brand_is_all_that_brand(self):
        self.assertEqual(self.left(ev("stock_count", 60, brand="Vinda")), [("Vinda", 60)])

    def test_opening_the_last_pack_leaves_one_pack_of_the_newest(self):
        self.assertEqual(self.left(ev("opened_last"), pack_size=10), [("Scott", 10)])
        self.assertEqual(self.left(ev("opened_last")), [("Kleenex", 20), ("Scott", 30)])

    def test_older_entries_without_a_stored_total_count_too(self):
        old = {"action": "bought", "packs": 2, "units_per_pack": 10, "brand": "Kleenex", "at": "2026-10-01T00:00:00Z"}
        self.assertEqual(model.brands_left([old, ev("bought", brand="Scott", packs=1)], pack_size=10),
                         [("Kleenex", 20), ("Scott", 10)])

    def test_the_brands_always_add_up_to_the_stock_figure(self):
        chooser = random.Random(7)
        actions = ("bought", "bought", "used", "used", "stock_count", "opened_last", "finished", "still_have", "price_check")
        for _ in range(2000):
            pack_size = chooser.choice([None, 10, 0.5, 800])
            events = [
                ev(chooser.choice(actions), chooser.choice([None, 0, 1, 5, 10, 25, 0.3, 0.1, 1200]),
                   brand=chooser.choice([None, None, "Lux", "lux", "Dettol"]))
                for _ in range(chooser.randint(0, 12))
            ]
            left = model.brands_left(events, pack_size)
            stock = model.replay(events, pack_size)["stock_est"]
            self.assertAlmostEqual(sum(amount for _, amount in left), stock or 0, places=6, msg=events)
            self.assertTrue(all(amount > 0 for _, amount in left), events)


def counted(qty, update_id, line=0, brand=None, at="2026-11-03T04:00:00Z", **fields):
    """A count as it comes from the table: its sort key says which message and line it was."""
    return ev("stock_count", qty, at, brand=brand, sk=f"{at}#{update_id}#{line}", **fields)


class StocktakeTest(unittest.TestCase):
    """A day's counts of one item are one stocktake (PLAN.md §8.1)."""

    BOUGHT_LAST_MONTH = ev("bought", 400, "2026-10-11T04:00:00Z", sk="2026-10-11T04:00:00Z#1#0")

    def stock(self, *events, pack_size=None):
        return model.replay(list(events), pack_size)["stock_est"]

    def test_the_lines_of_one_message_add_up_brand_by_brand(self):
        events = [self.BOUGHT_LAST_MONTH, counted(1000, 9, 0, "Dove"), counted(400, 9, 1, "半亩花田"),
                  counted(475, 9, 2), counted(750, 9, 3, "半亩花田")]
        self.assertEqual(self.stock(*events), 2625)
        self.assertEqual(model.brands_left(events), [("Dove", 1000), ("半亩花田", 1150), (None, 475)])

    def test_messages_that_day_about_other_brands_add_to_the_count(self):
        events = [self.BOUGHT_LAST_MONTH,
                  counted(2750, 7, brand="na", at="2026-11-03T04:02:20Z"),
                  counted(500, 8, brand="nard", at="2026-11-03T04:02:57Z"),
                  counted(500, 9, brand="ScalpScience", at="2026-11-03T04:03:54Z"),
                  counted(400, 10, brand="Fairo", at="2026-11-03T04:11:02Z")]
        self.assertEqual(self.stock(*events), 4150)
        self.assertEqual(model.brands_left(events),
                         [("na", 2750), ("nard", 500), ("ScalpScience", 500), ("Fairo", 400)])

    def test_a_brand_counted_again_that_day_is_replaced(self):
        first = counted(1600, 7, brand="Lux", at="2026-11-03T03:58:59Z")
        again = [counted(qty, 8, line, "lux", at="2026-11-03T04:06:57Z") for line, qty in enumerate([1600, 1600, 1600, 400])]
        self.assertEqual(self.stock(first, *again), 5200)
        self.assertEqual(model.brands_left([first, *again]), [("Lux", 5200)])

    def test_another_few_add_to_what_is_there(self):
        first = counted(1600, 7, brand="Lux", at="2026-11-03T03:58:59Z")
        more = counted(1200, 8, brand="Lux", at="2026-11-03T03:59:38Z", adds=True)
        self.assertEqual(self.stock(first, more), 2800)
        self.assertEqual(self.stock(self.BOUGHT_LAST_MONTH, more), 1600)
        # A full recount later that day still replaces both.
        self.assertEqual(self.stock(first, more, counted(1600, 9, brand="Lux", at="2026-11-03T04:06:57Z")), 1600)

    def test_a_recount_with_no_brand_starts_again(self):
        self.assertEqual(self.stock(counted(3, 7), counted(4, 8)), 4)
        self.assertEqual(self.stock(counted(3, 7, 0), counted(4, 7, 1)), 7)   # one message: both are there

    def test_a_count_with_no_brand_after_branded_ones_is_counted_as_its_own_brand(self):
        events = [counted(1000, 7, brand="Dove"), counted(500, 8), counted(800, 9, brand="qise"), counted(300, 10)]
        self.assertEqual(self.stock(*events), 2100)
        self.assertEqual(model.brands_left(events), [("Dove", 1000), ("qise", 800), (None, 300)])

    def test_the_first_count_of_a_new_day_replaces_everything(self):
        yesterday = counted(2750, 7, brand="na", at="2026-11-02T04:00:00Z")
        self.assertEqual(self.stock(yesterday, counted(500, 8, brand="nard")), 500)

    def test_the_day_is_singapore_time(self):
        before_midnight = counted(2750, 7, brand="na", at="2026-11-02T15:30:00Z")
        after_midnight = counted(500, 8, brand="nard", at="2026-11-02T16:30:00Z")
        same_evening = counted(500, 8, brand="nard", at="2026-11-02T15:50:00Z")
        self.assertEqual(self.stock(before_midnight, after_midnight), 500)
        self.assertEqual(self.stock(before_midnight, same_evening), 3250)

    def test_a_purchase_in_between_keeps_the_stocktake_going(self):
        events = [counted(2750, 7, brand="na", at="2026-11-03T04:00:00Z"),
                  ev("bought", 500, "2026-11-03T05:00:00Z", brand="nard", sk="2026-11-03T05:00:00Z#8#0"),
                  counted(400, 9, brand="Fairo", at="2026-11-03T06:00:00Z")]
        self.assertEqual(self.stock(*events), 3650)

    def test_status_follows_the_whole_count(self):
        first = counted(400, 7, 0, "Lux")
        self.assertEqual(model.replay([first], pack_size=400)["status"], "low")
        self.assertEqual(model.replay([first, counted(400, 7, 1, "Lux")], pack_size=400)["status"], "ok")

    def test_the_brands_always_add_up_to_the_stock_figure(self):
        chooser = random.Random(11)
        actions = ("bought", "used", "stock_count", "stock_count", "stock_count", "finished", "still_have")
        for _ in range(2000):
            events = []
            for update_id in range(chooser.randint(0, 10)):
                at = chooser.choice(["2026-11-02T04:00:00Z", "2026-11-03T04:00:00Z", "2026-11-03T04:00:00Z"])
                for line in range(chooser.randint(1, 3)):
                    events.append(ev(chooser.choice(actions), chooser.choice([None, 0, 5, 10, 400, 0.3]), at,
                                     brand=chooser.choice([None, None, "Lux", "lux", "Dettol"]),
                                     adds=chooser.random() < 0.2, sk=f"{at}#{update_id}#{line}"))
            events.sort(key=lambda event: event["sk"])
            left = model.brands_left(events, 10)
            stock = model.replay(events, 10)["stock_est"]
            self.assertAlmostEqual(sum(amount for _, amount in left), stock or 0, places=6, msg=events)
            self.assertTrue(all(amount > 0 for _, amount in left), events)


class OlderEventsTest(unittest.TestCase):
    """Events recorded before qty was stored only have packs and a pack size."""

    def test_quantity_is_packs_times_size(self):
        old = {"action": "bought", "packs": 2, "units_per_pack": 10, "at": "2026-10-09T14:56:00Z"}
        self.assertEqual(model.event_qty(old), 20)
        self.assertEqual(model.replay([old])["stock_est"], 20)

    def test_packs_alone_use_the_items_pack_size_when_there_is_one(self):
        old = {"action": "bought", "packs": 2}
        self.assertIsNone(model.event_qty(old))
        self.assertEqual(model.event_qty(old, pack_size=5), 10)

    def test_stored_qty_wins(self):
        self.assertEqual(model.event_qty({"qty": 7, "packs": 2, "units_per_pack": 10}), 7)


class PackSizeTest(unittest.TestCase):
    def test_newest_event_written_as_packs_times_size_gives_the_pack_size(self):
        events = [
            ev("bought", 20, "2026-10-01T00:00:00Z", packs=2, units_per_pack=10),
            ev("bought", 24, "2026-10-05T00:00:00Z", packs=2, units_per_pack=12),
            ev("bought", 24, "2026-10-08T00:00:00Z", packs=2),   # a bare "2": says nothing about the size
            ev("bought", 5, "2026-10-09T00:00:00Z"),
        ]
        self.assertEqual(model.latest_pack_size(events), (12, "2026-10-05T00:00:00Z"))

    def test_no_such_event(self):
        self.assertEqual(model.latest_pack_size([ev("bought", 5), ev("finished")]), (None, None))



class ExpiryOrderTest(unittest.TestCase):
    """What expires first is used first (PLAN.md §8.6)."""

    def lux(self, qty, update_id, line, expires_on=None, size=400):
        return counted(qty, update_id, line, "Lux", expires_on=expires_on, units_per_pack=size, packs=qty // size)

    def test_lots_come_back_in_the_order_to_use_them(self):
        events = [self.lux(1600, 8, 0, "2028-07-31"), self.lux(1600, 8, 1, "2028-06-30"), self.lux(400, 8, 2),
                  self.lux(400, 8, 3, "2028-03-31")]
        self.assertEqual(model.batches_left(events), [
            {"brand": "Lux", "amount": 400, "expires_on": "2028-03-31", "size": 400},
            {"brand": "Lux", "amount": 1600, "expires_on": "2028-06-30", "size": 400},
            {"brand": "Lux", "amount": 1600, "expires_on": "2028-07-31", "size": 400},
            {"brand": "Lux", "amount": 400, "expires_on": None, "size": 400},
        ])

    def test_what_is_used_comes_off_what_expires_first(self):
        events = [ev("bought", 1000, brand="Dove", expires_on="2028-01-31", sk="a#1#0"),
                  ev("bought", 500, brand="opos", expires_on="2027-05-31", sk="a#2#0"), ev("used", 600, sk="a#3#0")]
        self.assertEqual(model.brands_left(events), [("Dove", 900)])
        # A used entry that names a brand still takes that brand first.
        events[-1] = ev("used", 600, brand="Dove", sk="a#3#0")
        self.assertEqual(model.brands_left(events), [("Dove", 400), ("opos", 500)])

    def test_the_size_of_a_pack_is_kept_or_worked_out(self):
        events = [ev("bought", 1100, packs=2, brand="na", sk="a#1#0"), ev("bought", 750, sk="a#2#0")]
        self.assertEqual([batch["size"] for batch in model.batches_left(events)], [550, None])


if __name__ == "__main__":
    unittest.main()
