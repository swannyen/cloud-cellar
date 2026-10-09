"""Store tests: key layout and number conversion, against an in-memory table."""
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

    def test_last_event_pointer(self):
        self.assertIsNone(store.get_last_event(42))
        store.set_last_event(42, "EVT#rice", "sk", "2026-10-09T10:00:00Z")
        self.assertEqual(self.table.keys("USER#"), [("USER#42", "LAST")])
        self.assertEqual(store.get_last_event(42)["event_pk"], "EVT#rice")
        store.clear_last_event(42)
        self.assertIsNone(store.get_last_event(42))


if __name__ == "__main__":
    unittest.main()
