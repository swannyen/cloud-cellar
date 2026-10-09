"""Replay tests for the Phase 1 part of the model: stock and status."""
import unittest

import fakes  # noqa: F401  (puts src/ on sys.path)

import model


def ev(action, packs=None, at="2026-10-09T10:00:00Z"):
    return {"action": action, "packs": packs, "at": at}


class ReplayTest(unittest.TestCase):
    def test_no_events_means_nothing_is_known(self):
        self.assertEqual(model.replay([]), {"status": None, "stock_est": None, "last_event_at": None})

    def test_bought_adds_packs_and_marks_ok(self):
        state = model.replay([ev("bought", 2), ev("bought", 3)])
        self.assertEqual((state["status"], state["stock_est"]), ("ok", 5))

    def test_bought_without_a_count_is_one_pack(self):
        self.assertEqual(model.replay([ev("bought")])["stock_est"], 1)

    def test_finished_marks_out_and_empties_stock(self):
        state = model.replay([ev("bought", 2), ev("finished")])
        self.assertEqual((state["status"], state["stock_est"]), ("out", 0))

    def test_buying_after_running_out_restocks(self):
        state = model.replay([ev("finished"), ev("bought", 1)])
        self.assertEqual((state["status"], state["stock_est"]), ("ok", 1))

    def test_opened_last_leaves_one_pack_marked_low(self):
        state = model.replay([ev("bought", 4), ev("opened_last")])
        self.assertEqual((state["status"], state["stock_est"]), ("low", 1))

    def test_stock_count_sets_stock_directly(self):
        self.assertEqual(model.replay([ev("bought", 9), ev("stock_count", 3)])["stock_est"], 3)
        self.assertEqual(model.replay([ev("stock_count", 3)])["status"], "ok")
        self.assertEqual(model.replay([ev("stock_count", 1)])["status"], "low")

    def test_stock_count_without_a_number_changes_nothing(self):
        state = model.replay([ev("bought", 2), ev("stock_count")])
        self.assertEqual((state["status"], state["stock_est"]), ("ok", 2))

    def test_still_have_and_price_check_do_not_change_stock(self):
        state = model.replay([ev("finished"), ev("still_have"), ev("price_check")])
        self.assertEqual((state["status"], state["stock_est"]), ("out", 0))

    def test_price_check_alone_leaves_status_unknown(self):
        self.assertIsNone(model.replay([ev("price_check")])["status"])

    def test_last_event_time_is_the_newest_event(self):
        state = model.replay([ev("bought", 1, "2026-10-01T00:00:00Z"), ev("finished", at="2026-10-09T00:00:00Z")])
        self.assertEqual(state["last_event_at"], "2026-10-09T00:00:00Z")

    def test_dropping_the_last_event_restores_the_earlier_state(self):
        events = [ev("bought", 2), ev("finished")]
        self.assertEqual(model.replay(events)["status"], "out")
        undone = model.replay(events[:-1])
        self.assertEqual((undone["status"], undone["stock_est"]), ("ok", 2))


if __name__ == "__main__":
    unittest.main()
