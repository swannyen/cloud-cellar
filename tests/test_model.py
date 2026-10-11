"""Replay tests for the Phase 1 part of the model: stock and status, in base units."""
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


if __name__ == "__main__":
    unittest.main()
