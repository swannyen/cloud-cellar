"""Store tests: key layout and number conversion, against an in-memory table."""
import time
import unittest
from decimal import Decimal
from unittest import mock

from fakes import FakeTable

import store


class StoreTest(unittest.TestCase):
    def setUp(self):
        self.table = FakeTable()
        patch = mock.patch.object(store, "_table", self.table)
        patch.start()
        self.addCleanup(patch.stop)

    def test_first_delivery_is_true_once_per_update(self):
        self.assertTrue(store.first_delivery(7))
        self.assertFalse(store.first_delivery(7))
        self.assertTrue(store.first_delivery(8))

    def test_other_dynamodb_errors_are_raised(self):
        self.table.fail_with = "ProvisionedThroughputExceededException"
        with self.assertRaises(Exception):
            store.first_delivery(7)

    def test_numbers_are_stored_as_decimals_and_read_back_as_python_numbers(self):
        store.put_event("rice", "2026-10-09T10:00:00Z#1#0", {"action": "bought", "packs": 2, "unit_price": 0.645})
        raw = self.table.rows[("EVT#rice", "2026-10-09T10:00:00Z#1#0")]
        self.assertEqual(raw["unit_price"], Decimal("0.645"))
        event = store.list_events("rice")[0]
        self.assertEqual((event["packs"], event["unit_price"]), (2, 0.645))
        self.assertIsInstance(event["packs"], int)
        self.assertIsInstance(event["unit_price"], float)

    def test_unknown_values_are_not_stored(self):
        store.put_event("rice", "sk", {"action": "finished", "packs": None, "store": None})
        self.assertEqual(set(self.table.rows[("EVT#rice", "sk")]), {"pk", "sk", "action"})

    def test_items_use_the_item_partition_and_round_trip(self):
        item = {"item_id": "rice", "name": "Rice", "base_unit": "g", "aliases": ["beras"], "stock_est": 1.5}
        self.assertTrue(store.create_item(item))
        self.assertFalse(store.create_item(dict(item, name="Other")))
        self.assertIn(("ITEM", "rice"), self.table.rows)
        self.assertNotIn("item_id", self.table.rows[("ITEM", "rice")])
        self.assertEqual(store.get_item("rice"), item)
        self.assertEqual(store.list_items(), [item])
        self.assertIsNone(store.get_item("nope"))

    def test_save_item_overwrites_and_delete_removes(self):
        store.create_item({"item_id": "rice", "name": "Rice", "base_unit": "g", "aliases": []})
        store.save_item({"item_id": "rice", "name": "Rice", "base_unit": "g", "aliases": [], "status": "out"})
        self.assertEqual(store.get_item("rice")["status"], "out")
        store.delete_item("rice")
        self.assertEqual(store.list_items(), [])

    def test_events_come_back_oldest_first_across_pages(self):
        for day in (3, 1, 2):
            store.put_event("rice", f"2026-10-0{day}T00:00:00Z#{day}#0", {"action": "bought"})
        store.put_event("eggs", "2026-10-01T00:00:00Z#9#0", {"action": "bought"})
        self.table.page_size = 2
        self.assertEqual([event["sk"][:10] for event in store.list_events("rice")],
                         ["2026-10-01", "2026-10-02", "2026-10-03"])

    def test_recent_events_are_kept_per_user_oldest_first_with_a_day_to_live(self):
        before = int(time.time())
        self.assertEqual(store.list_recent_events(42), [])
        store.add_recent_event(42, "2026-10-09T10:00:00Z#2#0", {"event_pk": "EVT#rice", "summary": "bought 5 kg"})
        store.add_recent_event(42, "2026-10-09T09:00:00Z#1#0", {"event_pk": "EVT#eggs"})
        store.add_recent_event(43, "2026-10-09T09:30:00Z#3#0", {"event_pk": "EVT#milk"})
        recent = store.list_recent_events(42)
        self.assertEqual([record["event_pk"] for record in recent], ["EVT#eggs", "EVT#rice"])
        self.assertGreaterEqual(recent[0]["ttl"], before + 24 * 60 * 60)
        store.delete_recent_event(42, "2026-10-09T10:00:00Z#2#0")
        self.assertEqual(self.table.keys("USER#42"), [("USER#42", "2026-10-09T09:00:00Z#1#0")])

    def test_stores(self):
        self.assertEqual(store.list_stores(), [])
        store.save_store("don-don-donki", "Don Don Donki", "2026-10-09T10:00:00Z")
        self.assertEqual(store.list_stores(), [{
            "pk": "STORE", "sk": "don-don-donki", "store_id": "don-don-donki", "name": "Don Don Donki",
            "aliases": [], "last_used_at": "2026-10-09T10:00:00Z",
        }])

    def test_taught_unit_words(self):
        self.assertEqual(store.list_units(), {})
        store.save_unit("sticks", "pcs", "Mei")
        store.save_unit("ctn", "carton", "Jun")
        self.assertEqual(store.list_units(), {"sticks": "pcs", "ctn": "carton"})
        self.assertEqual(self.table.rows[("UNIT", "sticks")]["added_by"], "Mei")

    def test_taught_brands(self):
        self.assertEqual(store.list_brands(), [])
        store.save_brand("kans", "KANS", "Mei")
        store.save_brand("royal umbrella", "Royal Umbrella", "Jun")
        self.assertEqual(store.list_brands(), ["KANS", "Royal Umbrella"])
        self.assertEqual(self.table.rows[("BRAND", "kans")]["added_by"], "Mei")

    def test_pending_confirmations_live_for_a_week(self):
        before = int(time.time())
        self.assertIsNone(store.get_pending(7, 0))
        store.put_pending(7, 0, {"phrase": "loo roll", "matched_item_id": "toilet-paper"})
        self.assertEqual(self.table.keys("PEND#"), [("PEND#7#0", "-")])
        # Button data arrives as text, so the same record has to be found by "7" and "0".
        pending = store.get_pending("7", "0")
        self.assertEqual(pending["phrase"], "loo roll")
        self.assertGreaterEqual(pending["ttl"], before + 7 * 24 * 60 * 60)
        store.delete_pending("7", "0")
        self.assertIsNone(store.get_pending(7, 0))

    def test_stats_counters_add_up_per_month(self):
        store.add_stats("2026-10", {"lines_structured": 2, "ai_match_calls": 1})
        store.add_stats("2026-10", {"lines_structured": 3, "lines_rejected": 1, "ai_match_calls": 0})
        store.add_stats("2026-11", {"lines_structured": 7})
        self.assertEqual(self.table.keys("STATS"), [("STATS", "2026-10"), ("STATS", "2026-11")])
        self.assertEqual(store.get_stats("2026-10"),
                         {"lines_structured": 5, "ai_match_calls": 1, "ai_full_calls": 0, "lines_rejected": 1})

    def test_stats_with_nothing_to_add_write_nothing(self):
        store.add_stats("2026-10", {})
        store.add_stats("2026-10", {"lines_structured": 0})
        self.assertEqual(self.table.keys(), [])
        self.assertEqual(set(store.get_stats("2026-10").values()), {0})


if __name__ == "__main__":
    unittest.main()
