"""Event application tests (PLAN.md §4.2) against an in-memory table."""
import unittest
from unittest import mock

from fakes import FakeTable, parsed_event

import apply
import store

MEI = {"id": 42, "name": "Mei"}
AT = "2026-10-09T10:00:00Z"


class ApplyTestCase(unittest.TestCase):
    def setUp(self):
        self.table = FakeTable()
        patch = mock.patch.object(store, "_table", self.table)
        patch.start()
        self.addCleanup(patch.stop)

    def apply(self, *events, update_id=1, at=AT, sender=MEI, raw_text="message text"):
        return apply.apply_events(
            list(events), items=store.list_items(), sender=sender,
            update_id=update_id, at=at, raw_text=raw_text,
        )

    def add_item(self, item_id, name, base_unit, aliases=()):
        store.create_item({"item_id": item_id, "name": name, "base_unit": base_unit, "aliases": list(aliases)})


class AcceptanceTest(ApplyTestCase):
    def test_toilet_paper_purchase_creates_one_bought_event_with_unit_price(self):
        # "bought 2 packs toilet paper 10 rolls each $12.90 fairprice"
        lines = self.apply(parsed_event(packs=2, units_per_pack=10, total_price=12.9, store="FairPrice"))
        events = store.list_events("toilet-paper")
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["action"], "bought")
        self.assertAlmostEqual(events[0]["unit_price"], 0.645)
        self.assertEqual(lines, ["✓ Toilet paper (new item): bought 2 × 10 rolls for $12.90 at FairPrice ($0.65/roll)."])

    def test_finished_marks_the_item_out_and_confirms_in_one_line(self):
        self.add_item("dish-soap", "Dishwashing liquid", "ml", ["dish soap"])
        lines = self.apply(parsed_event("finished", "dish-soap", "dish soap", "ml"))
        self.assertEqual(lines, ["✓ Dishwashing liquid: marked finished."])
        item = store.get_item("dish-soap")
        self.assertEqual((item["status"], item["stock_est"]), ("out", 0))


class RecordTest(ApplyTestCase):
    def test_event_record_has_the_plan_attributes(self):
        self.apply(
            parsed_event(packs=2, units_per_pack=10, total_price=12.9, store="FairPrice", brand="Kleenex"),
            update_id=77, raw_text="x" * 500,
        )
        (pk, sk), = self.table.keys("EVT#")
        self.assertEqual((pk, sk), ("EVT#toilet-paper", f"{AT}#77#0"))
        event = store.get_event(pk, sk)
        self.assertEqual(event["user_id"], 42)
        self.assertEqual(event["user_name"], "Mei")
        self.assertEqual(event["source"], "chat")
        self.assertEqual(event["brand"], "Kleenex")
        self.assertEqual(event["at"], AT)
        self.assertEqual(len(event["raw_text"]), 300)

    def test_unknown_values_are_left_out_and_no_unit_price_is_invented(self):
        self.apply(parsed_event(packs=2, total_price=15))
        event = store.list_events("toilet-paper")[0]
        self.assertNotIn("unit_price", event)
        self.assertNotIn("units_per_pack", event)
        self.assertNotIn("store", event)

    def test_last_event_pointer_follows_the_senders_newest_event(self):
        self.apply(parsed_event(), parsed_event("finished", item_name="Rice", base_unit="g"), update_id=5)
        last = store.get_last_event(42)
        self.assertEqual((last["event_pk"], last["event_sk"], last["at"]), ("EVT#rice", f"{AT}#5#1", AT))

    def test_two_events_for_one_item_in_one_message_are_both_kept(self):
        self.apply(parsed_event(packs=2, store="FairPrice"), parsed_event(packs=1, store="Giant"))
        self.assertEqual(len(store.list_events("toilet-paper")), 2)
        self.assertEqual(store.get_item("toilet-paper")["stock_est"], 3)

    def test_cached_state_is_replayed_from_all_events(self):
        self.apply(parsed_event(packs=2), update_id=1, at="2026-10-01T00:00:00Z")
        self.apply(parsed_event("opened_last"), update_id=2, at="2026-10-09T00:00:00Z")
        item = store.get_item("toilet-paper")
        self.assertEqual((item["status"], item["stock_est"]), ("low", 1))
        self.assertEqual(item["last_event_at"], "2026-10-09T00:00:00Z")


class ItemResolutionTest(ApplyTestCase):
    def test_known_item_id_is_used_and_nothing_new_is_created(self):
        self.add_item("toilet-paper", "Toilet paper", "roll", ["TP"])
        lines = self.apply(parsed_event(item_id="toilet-paper", item_name="TP", packs=1))
        self.assertEqual(lines, ["✓ Toilet paper: bought 1 pack."])
        self.assertEqual(self.table.keys("ITEM"), [("ITEM", "toilet-paper")])

    def test_new_item_gets_a_slug_name_unit_and_creation_time(self):
        self.apply(parsed_event(item_name="Dishwashing Liquid!", base_unit="ml"))
        item = store.get_item("dishwashing-liquid")
        self.assertEqual((item["name"], item["base_unit"], item["created_at"]), ("Dishwashing Liquid!", "ml", AT))

    def test_slug_is_capped_and_never_empty(self):
        self.assertEqual(len(apply.slugify("extra " * 20)), 40)
        self.assertFalse(apply.slugify("a" * 39 + " b").endswith("-"))
        self.assertEqual(apply.slugify("酱油"), "item")

    def test_taken_slug_gets_a_number(self):
        self.add_item("soap", "Soap bars", "piece")
        self.apply(parsed_event(item_name="Soap", base_unit="ml"))
        self.assertEqual(store.get_item("soap-2")["name"], "Soap")

    def test_model_naming_a_known_item_without_its_id_reuses_it(self):
        self.add_item("toilet-paper", "Toilet paper", "roll", ["loo roll"])
        self.apply(parsed_event(item_name="toilet paper"), update_id=1)
        self.apply(parsed_event(item_name="Loo Roll"), update_id=2)
        self.assertEqual(self.table.keys("ITEM"), [("ITEM", "toilet-paper")])
        self.assertEqual(len(store.list_events("toilet-paper")), 2)

    def test_same_new_item_twice_in_one_message_is_created_once(self):
        self.apply(parsed_event(item_name="Sponge", base_unit="piece"), parsed_event(item_name="Sponge", base_unit="piece"))
        self.assertEqual(self.table.keys("ITEM"), [("ITEM", "sponge")])

    def test_senders_wording_becomes_an_alias_once(self):
        self.add_item("toilet-paper", "Toilet paper", "roll", ["TP"])
        for update_id, wording in enumerate(["bog roll", "Bog Roll", "tp", "Toilet Paper"]):
            self.apply(parsed_event(item_id="toilet-paper", item_name=wording), update_id=update_id)
        self.assertEqual(store.get_item("toilet-paper")["aliases"], ["TP", "bog roll"])

    def test_brand_and_overlong_wording_are_not_learned_as_aliases(self):
        self.add_item("dish-soap", "Dishwashing liquid", "ml")
        self.apply(parsed_event(item_id="dish-soap", item_name="Mama Lemon", base_unit="ml", brand="Mama Lemon"))
        self.apply(parsed_event(item_id="dish-soap", item_name="x" * 31, base_unit="ml"), update_id=2)
        self.assertEqual(store.get_item("dish-soap")["aliases"], [])


class UnitTest(ApplyTestCase):
    def test_pack_size_in_a_different_kind_of_unit_is_dropped(self):
        self.add_item("eggs", "Eggs", "piece")
        self.apply(parsed_event(item_id="eggs", item_name="eggs", base_unit="g", packs=1, units_per_pack=600, total_price=4))
        event = store.list_events("eggs")[0]
        self.assertNotIn("units_per_pack", event)
        self.assertNotIn("unit_price", event)

    def test_grams_and_millilitres_are_treated_as_the_same_size(self):
        self.add_item("laundry-detergent", "Laundry detergent", "g")
        self.apply(parsed_event(item_id="laundry-detergent", item_name="detergent", base_unit="ml",
                                packs=1, units_per_pack=3000, total_price=9))
        self.assertAlmostEqual(store.list_events("laundry-detergent")[0]["unit_price"], 0.003)


class TimeTest(unittest.TestCase):
    def test_iso_and_epoch_round_trip(self):
        self.assertEqual(apply.iso_utc(1791555000), "2026-10-09T14:10:00Z")
        self.assertEqual(apply.epoch("2026-10-09T14:10:00Z"), 1791555000)


if __name__ == "__main__":
    unittest.main()
