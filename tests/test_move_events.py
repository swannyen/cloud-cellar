"""Tests for scripts/move_events.py against an in-memory table. Nothing touches AWS."""
import importlib.util
import unittest
from pathlib import Path
from unittest import mock

from fakes import FakeTable

import store

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("move_events", ROOT / "scripts" / "move_events.py")
move_events = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(move_events)

MEI = 42


class MoveEventsTest(unittest.TestCase):
    """Face masks holding entries for three other products, as the first version of the bot left it."""

    def setUp(self):
        self.table = FakeTable()
        patch = mock.patch.object(store, "_table", self.table)
        patch.start()
        self.addCleanup(patch.stop)
        store.create_item({"item_id": "face-masks", "name": "Face masks", "base_unit": "piece", "aliases": [],
                           "status": "ok", "stock_est": 8})
        store.create_item({"item_id": "kn95-masks", "name": "KN95 masks", "base_unit": "piece", "aliases": []})
        self.event("kn95-masks", "2026-10-09T15:41:26Z#78#0", "KN95 masks 100 pieces for 3.29 from Taobao", 100)
        for sk, text, size in [
            ("2026-10-09T15:39:19Z#75#0", "Face masks 1000 pieces for 17.13 from Taobao", 1000),
            ("2026-10-09T15:40:49Z#77#0", "KN95 masks 200 pieces for 6.91", 200),
            ("2026-10-09T22:41:17Z#89#0", "Hand masks 10 pcs $1.45 from Taobao", 10),
            ("2026-10-09T23:14:28Z#94#0", "HAND masks 10 pcs $1.45 from Taobao", 10),
            ("2026-10-09T23:15:33Z#96#0", "Facial masks 5 pcs 1.86 from Taobao", 5),
        ]:
            self.event("face-masks", sk, text, size)

    def event(self, item_id, sk, text, size):
        # Entries from before quantities were stored: one pack of the size that was written.
        store.put_event(item_id, sk, {"action": "bought", "packs": 1, "units_per_pack": size, "total_price": 1.45,
                                      "store": "Taobao", "user_id": MEI, "raw_text": text, "at": sk[:20]})

    def texts(self, item_id):
        return [event["raw_text"] for event in store.list_events(item_id)]

    def test_plan_shows_the_move_and_changes_nothing(self):
        before = dict(self.table.rows)
        source, target, entries, is_new = move_events.plan("face-masks", "hand-masks", "hand masks", "Hand masks")
        self.assertEqual([event["raw_text"][:10] for event in entries], ["Hand masks", "HAND masks"])
        self.assertEqual((source["name"], target["name"], target["base_unit"], is_new), ("Face masks", "Hand masks", "piece", True))
        self.assertEqual(self.table.rows, before)

    def test_move_to_a_new_item_creates_it_and_recomputes_both(self):
        move_events.move("face-masks", "hand-masks", "hand masks", "Hand masks")
        self.assertEqual(self.texts("hand-masks"), ["Hand masks 10 pcs $1.45 from Taobao", "HAND masks 10 pcs $1.45 from Taobao"])
        hand = store.get_item("hand-masks")
        self.assertEqual((hand["name"], hand["base_unit"], hand["created_at"]), ("Hand masks", "piece", "2026-10-09T22:41:17Z"))
        self.assertEqual((hand["status"], hand["stock_est"], hand["pack_size"]), ("ok", 20, 10))
        face = store.get_item("face-masks")
        self.assertEqual(len(self.texts("face-masks")), 3)
        self.assertEqual(face["stock_est"], 1205)

    def test_move_to_an_existing_item_keeps_every_field_of_the_entry(self):
        original = store.get_event("EVT#face-masks", "2026-10-09T15:40:49Z#77#0")
        move_events.move("face-masks", "kn95-masks", "KN95")
        self.assertIsNone(store.get_event("EVT#face-masks", "2026-10-09T15:40:49Z#77#0"))
        moved = store.get_event("EVT#kn95-masks", "2026-10-09T15:40:49Z#77#0")
        self.assertEqual({k: v for k, v in moved.items() if k != "pk"}, {k: v for k, v in original.items() if k != "pk"})
        self.assertEqual(store.get_item("kn95-masks")["stock_est"], 300)

    def test_all_three_moves_leave_each_item_with_its_own_entries(self):
        move_events.move("face-masks", "kn95-masks", "kn95")
        move_events.move("face-masks", "hand-masks", "hand masks", "Hand masks")
        move_events.move("face-masks", "facial-masks", "facial masks", "Facial masks")
        self.assertEqual(self.texts("face-masks"), ["Face masks 1000 pieces for 17.13 from Taobao"])
        self.assertEqual(store.get_item("face-masks")["stock_est"], 1000)
        self.assertEqual([len(self.texts(item)) for item in ("kn95-masks", "hand-masks", "facial-masks")], [2, 2, 1])
        self.assertEqual(self.table.keys("ITEM"), [("ITEM", item) for item in ("face-masks", "facial-masks", "hand-masks", "kn95-masks")])

    def test_recent_entry_stays_undoable_and_its_buttons_are_retired(self):
        sk = "2026-10-11T02:33:03Z#105#0"
        store.put_event("face-masks", sk, {"action": "bought", "qty": 2, "total_price": 1.96, "user_id": MEI,
                                           "raw_text": "+ kans facial masks for 2 pcs $1.96", "at": "2026-10-11T02:33:03Z"})
        store.add_recent_event(MEI, sk, {"event_pk": "EVT#face-masks", "event_sk": sk, "item_name": "Face masks",
                                         "summary": "bought 2 pieces for $1.96", "at": "2026-10-11T02:33:03Z"})
        store.put_pending("105", "0", {"phrase": "kans facial masks for", "matched_item_id": "face-masks",
                                       "event_pk": "EVT#face-masks", "event_sk": sk, "base_unit": "piece"})
        move_events.move("face-masks", "facial-masks", "facial masks", "Facial masks")
        recent, = store.list_recent_events(MEI)
        self.assertEqual((recent["event_pk"], recent["item_name"]), ("EVT#facial-masks", "Facial masks"))
        self.assertIsNone(store.get_pending("105", "0"))
        self.assertEqual(store.get_item("facial-masks")["stock_est"], 7)

    def test_nothing_matching_changes_nothing(self):
        before = dict(self.table.rows)
        _, _, entries, _ = move_events.move("face-masks", "foot-masks", "foot masks", "Foot masks")
        self.assertEqual(entries, [])
        self.assertEqual(self.table.rows, before)

    def test_moving_every_entry_away_removes_the_emptied_item(self):
        move_events.move("kn95-masks", "face-masks", "kn95")
        self.assertIsNone(store.get_item("kn95-masks"))
        self.assertEqual(len(self.texts("face-masks")), 6)

    def test_requests_that_cannot_be_done_say_why(self):
        cases = [
            (("no-such-item", "hand-masks", "x", "Hand masks"), 'There is no item "no-such-item"'),
            (("face-masks", "face-masks", "x", None), "The two items are the same"),
            (("face-masks", "hand-masks", "hand", None), "Give it a name with --name"),
            (("face-masks", "Hand Masks", "hand", "Hand masks"), "can't be an item ID"),
        ]
        for args, message in cases:
            with self.subTest(args=args):
                with self.assertRaisesRegex(move_events.MoveError, message):
                    move_events.move(*args)
        self.assertEqual(len(self.texts("face-masks")), 5)


if __name__ == "__main__":
    unittest.main()
