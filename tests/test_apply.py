"""Event application tests (PLAN.md §4.2) against an in-memory table.

Messages go through the real format parser and matcher; only the AI calls are faked.
"""
import unittest
from datetime import date
from unittest import mock

from fakes import FakeTable, entries, no_ai

import ai
import apply
import claude_api
import model
import pricing
import replies
import store
import structured

MEI = {"id": 42, "name": "Mei"}
AT = "2026-10-09T10:00:00Z"
ASK_FACE_MASKS = ("How many in each pack of Face masks? Resend like: + face masks 2x5pcs $1.96 — "
                  "or write 2pcs if you mean single pieces.")


class ApplyTestCase(unittest.TestCase):
    def setUp(self):
        self.table = FakeTable()
        patches = [
            mock.patch.object(store, "_table", self.table),
            mock.patch.object(ai, "match_item", side_effect=no_ai),
            mock.patch.object(apply.logger, "exception"),  # keep expected errors quiet
        ]
        started = [patch.start() for patch in patches]
        for patch in patches:
            self.addCleanup(patch.stop)
        self.match_item = started[1]

    def send(self, text, update_id=1, at=AT, sender=MEI, units=None, brands=()):
        """Apply a message in the format and return (reply lines, counts, button rows)."""
        return self.apply(entries(text, store.list_stores(), units), update_id=update_id, at=at, sender=sender,
                          brands=brands)

    def apply(self, entry_list, update_id=1, at=AT, sender=MEI, brands=()):
        return apply.apply_entries(entry_list, items=store.list_items(), brands=brands, sender=sender,
                                   update_id=update_id, at=at)

    def lines(self, text, **kwargs):
        return self.send(text, **kwargs)[0]

    def said(self, text, *events, update_id=1):
        """Apply free text as the fallback would have read it. events are what the model returned."""
        return self.apply(ai.ground_free_text([free_event(**event) for event in events], text), update_id=update_id)

    def ai_says(self, item_id=None, name="", base_unit="piece"):
        self.match_item.side_effect = None
        self.match_item.return_value = ai.ItemMatch(item_id, name, base_unit)

    def add_item(self, item_id, name, base_unit, aliases=(), **fields):
        store.create_item({"item_id": item_id, "name": name, "base_unit": base_unit, "aliases": list(aliases), **fields})

    def add_toilet_paper(self, **fields):
        self.add_item("toilet-paper", "Toilet paper", "roll", **fields)

    def add_face_masks(self, **fields):
        self.add_item("face-masks", "Face masks", "piece", **fields)

    def add_facial_masks(self, **fields):
        """Skincare masks: a different product from Face masks, with its own item."""
        self.add_item("facial-masks", "Facial masks", "piece", **fields)

    def events(self, item_id):
        return store.list_events(item_id)


def free_event(**fields):
    """An event shaped like ai.normalise_free_text() output."""
    event = {"action": "bought", "item_id": None, "item_name": "Toilet paper", "base_unit": "roll",
             "quantity_text": None, "total_price": None, "store": None, "brand": None}
    event.update(fields)
    return event


class AcceptanceTest(ApplyTestCase):
    def test_formatted_purchase_records_the_total_and_saves_the_pack_size_without_ai(self):
        self.add_toilet_paper()
        lines, counts, buttons = self.send("+ toilet paper 2x10 $12.90 @fairprice")
        event, = self.events("toilet-paper")
        self.assertEqual((event["action"], event["qty"], event["packs"], event["units_per_pack"]), ("bought", 20, 2, 10))
        self.assertEqual((event["store"], event["currency"], event["parse_path"]), ("FairPrice", "SGD", "structured"))
        self.assertAlmostEqual(event["unit_price"], 0.645)
        self.assertEqual(lines, ["✓ Toilet paper: bought 2 packs × 10 rolls (20 rolls) for $12.90 at FairPrice ($0.65/roll). First price recorded."])
        self.assertEqual((counts, buttons), ({"lines_structured": 1}, []))
        self.assertEqual(store.get_item("toilet-paper")["pack_size"], 10)

        lines, _, _ = self.send("+ toilet paper 2 $12.90", update_id=2)
        self.assertEqual(lines, ["✓ Toilet paper: bought 2 packs × 10 rolls (20 rolls) for $12.90 ($0.65/roll). 🔥 As good as your best: $0.65/roll at FairPrice."])
        self.assertEqual([event["qty"] for event in self.events("toilet-paper")], [20, 20])
        self.assertEqual(store.get_item("toilet-paper")["stock_est"], 40)
        self.match_item.assert_not_called()

    def test_single_pieces_are_recorded_as_written(self):
        self.add_face_masks()
        self.assertEqual(self.lines("+ face masks 2pcs $1.96 @taobao"),
                         ["✓ Face masks: bought 2 pieces for $1.96 at Taobao ($0.98/piece). First price recorded."])
        event, = self.events("face-masks")
        self.assertEqual((event["qty"], event["unit_price"]), (2, 0.98))
        self.assertNotIn("packs", event)

    def test_a_pack_count_without_a_pack_size_records_nothing_and_asks(self):
        self.add_face_masks()
        lines, counts, buttons = self.send("+ face masks 2 $1.96")
        self.assertEqual(lines, [ASK_FACE_MASKS])
        self.assertEqual((counts, buttons), ({"lines_rejected": 1}, []))
        self.assertEqual(self.table.keys("EVT#") + self.table.keys("USER#") + self.table.keys("PEND#"), [])

    def test_an_unknown_word_after_the_number_asks_until_it_is_taught(self):
        self.add_facial_masks()
        self.ai_says("facial-masks", "Facial masks")
        line, = self.lines("+ 2 sticks facial masks $1.96 from taobao")
        self.assertIn("How many in each pack of Facial masks?", line)
        self.assertEqual(self.table.keys("EVT#"), [])

        self.match_item.side_effect = no_ai
        lines = self.lines("+ 2 sticks facial masks $1.96 from taobao", update_id=2, units={"sticks": "pcs"})
        self.assertEqual(lines, ["✓ Facial masks: bought 2 pieces for $1.96 at Taobao ($0.98/piece). First price recorded."])

    def test_a_unit_that_does_not_fit_the_item_is_rejected(self):
        self.add_face_masks()
        self.assertEqual(self.lines("+ face masks 500ml"), ["Face masks are counted in pieces. Try 2pcs or 2x5pcs."])
        self.assertEqual(self.table.keys("EVT#"), [])

    def test_finished_marks_the_item_out_and_confirms_in_one_line(self):
        self.add_item("dish-soap", "Dishwashing liquid", "ml", ["dish soap"])
        self.assertEqual(self.lines("- dish soap"), ["✓ Dishwashing liquid: marked finished."])
        item = store.get_item("dish-soap")
        self.assertEqual((item["status"], item["stock_est"]), ("out", 0))

    def test_unknown_name_costs_one_ai_call_and_asks_for_a_tap(self):
        self.add_toilet_paper()
        self.ai_says("toilet-paper", "Toilet paper", "roll")
        lines, counts, buttons = self.send("+ loo roll 1x10 $6", update_id=7)
        self.assertEqual(lines, ['✓ Toilet paper: bought 1 pack of 10 rolls for $6.00 ($0.60/roll). First price recorded.'
                                 ' (Read "loo roll" as Toilet paper.)'])
        self.assertEqual(buttons, [[("Same item", "m:same:7:0"), ("New item: Loo roll", "m:new:7:0")]])
        self.assertEqual(counts, {"lines_structured": 1, "ai_match_calls": 1})
        self.assertEqual(self.events("toilet-paper")[0]["parse_path"], "ai_match")
        # Recorded straight away, but the name isn't learned until someone confirms it.
        self.assertEqual(store.get_item("toilet-paper")["aliases"], [])
        self.assertEqual(store.get_pending("7", "0")["phrase"], "loo roll")

        apply.confirm_same(store.get_pending("7", "0"))
        self.match_item.side_effect = no_ai
        lines, counts, buttons = self.send("+ loo roll 1x10 $6", update_id=8)
        self.assertEqual(lines, ["✓ Toilet paper: bought 1 pack of 10 rolls for $6.00 ($0.60/roll). 🔥 As good as your best: $0.60/roll."])
        self.assertEqual((counts, buttons), ({"lines_structured": 1}, []))
        self.match_item.assert_called_once()

    def test_typo_matches_without_ai_and_asks_for_a_tap(self):
        self.add_toilet_paper(pack_size=10, pack_size_set_at=AT)
        lines, counts, buttons = self.send("+ toilet papr 2", update_id=3)
        self.assertEqual(lines, ['✓ Toilet paper: bought 2 packs × 10 rolls (20 rolls). (Read "toilet papr" as Toilet paper.)'])
        self.assertEqual(buttons, [[("Same item", "m:same:3:0"), ("New item: Toilet papr", "m:new:3:0")]])
        self.assertEqual(counts, {"lines_structured": 1})

    def test_recorded_brand_is_dropped_from_the_front_without_ai(self):
        self.add_toilet_paper(pack_size=10, pack_size_set_at=AT)
        self.said("bought kleenex toilet paper 2x10 rolls", dict(item_id="toilet-paper", item_name="toilet paper",
                                                                 quantity_text="2x10 rolls", brand="Kleenex"))
        self.assertEqual(store.get_item("toilet-paper")["brands"], ["Kleenex"])
        lines, _, buttons = self.send("+ kleenex toilet paper 2", update_id=2)
        self.assertEqual((lines, buttons), (["✓ Toilet paper: bought 2 packs × 10 rolls (20 rolls)."], []))

    def test_a_different_describing_word_does_not_become_the_same_item(self):
        self.add_face_masks()
        self.ai_says(None, "Hand masks", "piece")
        lines, _, buttons = self.send("+ hand masks 10 pcs $1.45 from taobao")
        self.assertEqual(lines, ["✓ Hand masks (new item): bought 10 pieces for $1.45 at Taobao ($0.14/piece). First price recorded."])
        self.assertEqual(buttons, [])
        self.assertEqual(self.events("face-masks"), [])
        self.assertEqual(self.events("hand-masks")[0]["store"], "Taobao")

    def test_facial_masks_are_not_face_masks(self):
        # Skincare masks and the masks worn over the nose and mouth are different products.
        self.add_face_masks(pack_size=50, pack_size_set_at=AT)
        self.ai_says(None, "Facial masks", "piece")
        lines, _, buttons = self.send("+ facial masks 5pcs $1.86 @taobao")
        self.assertEqual(lines, ["✓ Facial masks (new item): bought 5 pieces for $1.86 at Taobao ($0.37/piece). First price recorded."])
        self.assertEqual(buttons, [])
        self.assertEqual(self.match_item.call_args.args[0], "facial masks")  # the matcher didn't call it a typo
        self.assertEqual(self.events("face-masks"), [])
        # From then on each name goes to its own item, with no AI.
        self.match_item.side_effect = no_ai
        self.send("+ facial masks 5pcs\n+ face masks 1", update_id=2)
        self.assertEqual(store.get_item("facial-masks")["stock_est"], 10)
        self.assertEqual(store.get_item("face-masks")["stock_est"], 50)

    def test_new_item_button_moves_a_wrong_guess(self):
        self.add_face_masks()
        self.ai_says("face-masks", "Face masks")
        _, _, buttons = self.send("+ hand masks 10 pcs $1.45 from taobao", update_id=5)
        self.assertEqual(buttons, [[("Same item", "m:same:5:0"), ("New item: Hand masks", "m:new:5:0")]])
        self.assertEqual(store.get_item("face-masks")["stock_est"], 10)

        new = apply.split_into_new_item(store.get_pending("5", "0"))

        self.assertEqual((new["item_id"], new["name"], new["base_unit"]), ("hand-masks", "Hand masks", "piece"))
        event, = self.events("hand-masks")
        self.assertEqual((event["qty"], event["store"], event["total_price"]), (10, "Taobao", 1.45))
        self.assertEqual(store.get_item("hand-masks")["stock_est"], 10)
        # Face masks had no other entry, so nothing of the wrong guess is left on it.
        self.assertEqual(self.events("face-masks"), [])


class UsedTest(ApplyTestCase):
    """A quantity on a "-" line is what was used; the rest stays in stock."""

    def add_pocket_tissues(self):
        self.add_item("pocket-tissues", "Pocket tissues", "piece")
        self.send("+ pocket tissues 90x6 pieces $3.74 @taobao\n/add pocket tissues 90x6 pieces $3.76 @taobao", update_id=1)

    def test_using_90_of_180_packets_leaves_90(self):
        self.add_pocket_tissues()
        lines, counts, buttons = self.send("/used pocket tissues 90 packets", update_id=2)
        self.assertEqual(lines, ["✓ Pocket tissues: used 90 packs × 6 pieces (540 pieces), 540 pieces (90 packs) left."])
        self.assertEqual((counts, buttons), ({"lines_structured": 1}, []))
        item = store.get_item("pocket-tissues")
        self.assertEqual((item["status"], item["stock_est"]), ("ok", 540))
        event = self.events("pocket-tissues")[-1]
        self.assertEqual((event["action"], event["qty"], event["packs"]), ("used", 540, 90))
        self.assertNotIn("unit_price", event)
        self.match_item.assert_not_called()

    def test_single_pieces_and_the_minus_sign_work_the_same_way(self):
        self.add_pocket_tissues()
        self.assertEqual(self.lines("- pocket tissues 6pcs", update_id=2),
                         ["✓ Pocket tissues: used 6 pieces, 1074 pieces (179 packs) left."])

    def test_minus_without_a_quantity_has_run_out(self):
        self.add_pocket_tissues()
        self.assertEqual(self.lines("- pocket tissues", update_id=2), ["✓ Pocket tissues: marked finished."])
        self.assertEqual(store.get_item("pocket-tissues")["stock_est"], 0)

    def test_used_without_a_quantity_is_one_pack(self):
        self.add_pocket_tissues()
        self.assertEqual(self.lines("/used pocket tissues", update_id=2),
                         ["✓ Pocket tissues: used 1 pack of 6 pieces, 1074 pieces (179 packs) left."])

    def test_using_everything_marks_it_out(self):
        self.add_pocket_tissues()
        self.assertEqual(self.lines("- pocket tissues 180", update_id=2),
                         ["✓ Pocket tissues: used 180 packs × 6 pieces (1080 pieces), none left."])
        self.assertEqual(store.get_item("pocket-tissues")["status"], "out")

    def test_a_pack_count_without_a_pack_size_asks_instead_of_guessing(self):
        self.add_face_masks()
        self.send("+ face masks 50pcs", update_id=1)
        lines, counts, _ = self.send("- face masks 2", update_id=2)
        self.assertEqual(lines, ["How many in each pack of Face masks? Resend like: - face masks 2x5pcs — "
                                 "or write 2pcs if you mean single pieces."])
        self.assertEqual(counts, {"lines_rejected": 1})
        self.assertEqual(store.get_item("face-masks")["stock_est"], 50)

    def test_a_unit_that_does_not_fit_is_rejected(self):
        self.add_pocket_tissues()
        self.assertEqual(self.lines("- pocket tissues 500ml", update_id=2),
                         ["Pocket tissues are counted in pieces. Try 2pcs or 2x5pcs."])

    def test_free_text_use_goes_the_same_way(self):
        self.add_pocket_tissues()
        lines, _, _ = self.said("took 3 packets of pocket tissues to the office", dict(
            action="used", item_id="pocket-tissues", item_name="pocket tissues", base_unit="piece",
            quantity_text="3 packets"), update_id=2)
        self.assertEqual(lines, ["✓ Pocket tissues: used 3 packs × 6 pieces (18 pieces), 1062 pieces (177 packs) left."])


class RecordTest(ApplyTestCase):
    def test_event_record_has_the_plan_attributes(self):
        self.add_toilet_paper()
        line = "+ toilet paper 2x10 $12.90 @fairprice"
        self.send(f"{line}\n- toilet paper", update_id=77)
        (pk, sk), second = self.table.keys("EVT#")
        self.assertEqual((pk, sk), ("EVT#toilet-paper", f"{AT}#77#0"))
        self.assertEqual(second[1], f"{AT}#77#1")
        event = store.get_event(pk, sk)
        self.assertEqual((event["user_id"], event["user_name"], event["source"]), (42, "Mei", "chat"))
        self.assertEqual((event["at"], event["currency"]), (AT, "SGD"))
        self.assertEqual(event["raw_text"], line)  # the line, not the whole message

    def test_the_whole_message_is_kept_up_to_the_longest_telegram_allows(self):
        self.add_toilet_paper(pack_size=10, pack_size_set_at=AT)
        self.send("+ toilet paper @" + "x" * 500)
        self.assertEqual(len(self.events("toilet-paper")[0]["raw_text"]), 516)
        self.send("+ toilet paper @" + "x" * 5000, update_id=2)
        self.assertEqual(len(self.events("toilet-paper")[1]["raw_text"]), 4096)

    def test_no_unit_price_without_a_price(self):
        self.add_toilet_paper()
        self.send("+ toilet paper 2x10")
        event = self.events("toilet-paper")[0]
        self.assertNotIn("unit_price", event)
        self.assertNotIn("total_price", event)
        self.assertNotIn("store", event)

    def test_each_event_is_remembered_for_undo(self):
        self.add_toilet_paper()
        self.add_item("rice", "Rice", "g")
        self.send("+ toilet paper 2x10\n- rice", update_id=5)
        recent = store.list_recent_events(42)
        self.assertEqual([record["sk"] for record in recent], [f"{AT}#5#0", f"{AT}#5#1"])
        self.assertEqual((recent[0]["event_pk"], recent[0]["event_sk"]), ("EVT#toilet-paper", f"{AT}#5#0"))
        self.assertEqual((recent[0]["item_name"], recent[0]["summary"]),
                         ("Toilet paper", "bought 2 packs × 10 rolls (20 rolls)"))
        self.assertEqual((recent[1]["item_name"], recent[1]["summary"]), ("Rice", "marked finished"))

    def test_two_events_for_one_item_in_one_message_are_both_kept(self):
        self.add_toilet_paper()
        self.send("+ toilet paper 2x10 @fairprice\n+ toilet paper 1 @giant")
        self.assertEqual([event["qty"] for event in self.events("toilet-paper")], [20, 10])
        self.assertEqual(store.get_item("toilet-paper")["stock_est"], 30)

    def test_cached_state_is_replayed_from_all_events(self):
        self.add_toilet_paper()
        self.send("+ toilet paper 2x10", update_id=1, at="2026-10-01T00:00:00Z")
        self.send("low toilet paper", update_id=2, at="2026-10-09T00:00:00Z")
        item = store.get_item("toilet-paper")
        self.assertEqual((item["status"], item["stock_est"]), ("low", 10))
        self.assertEqual(item["last_event_at"], "2026-10-09T00:00:00Z")

    def test_a_store_first_used_with_at_sign_is_saved(self):
        self.add_toilet_paper()
        self.send("+ toilet paper 2x10 @don don donki\n+ toilet paper 1x10 @ntuc")
        self.assertEqual([(s["store_id"], s["name"]) for s in store.list_stores()], [("don-don-donki", "Don Don Donki")])
        # From now on it is a known store, so "from" works for it.
        self.assertEqual(self.lines("+ toilet paper 1 from don don donki", update_id=2),
                         ["✓ Toilet paper: bought 1 pack of 10 rolls at Don Don Donki."])


class PackSizeTest(ApplyTestCase):
    def test_pack_size_follows_the_newest_entry_written_as_packs_times_size(self):
        self.add_toilet_paper()
        self.send("+ toilet paper 2x10", update_id=1, at="2026-10-01T00:00:00Z")
        self.send("+ toilet paper 1x12", update_id=2, at="2026-10-05T00:00:00Z")
        self.assertEqual(store.get_item("toilet-paper")["pack_size"], 12)
        self.send("+ toilet paper 2", update_id=3, at="2026-10-06T00:00:00Z")
        self.assertEqual(self.events("toilet-paper")[-1]["qty"], 24)

    def test_a_size_set_by_hand_holds_until_a_newer_entry_gives_one(self):
        self.add_toilet_paper(pack_size=8, pack_size_set_at="2026-10-05T00:00:00Z")
        self.send("+ toilet paper 2x10", update_id=1, at="2026-10-01T00:00:00Z")
        self.assertEqual(store.get_item("toilet-paper")["pack_size"], 8)
        self.send("+ toilet paper 2x12", update_id=2, at="2026-10-09T00:00:00Z")
        self.assertEqual(store.get_item("toilet-paper")["pack_size"], 12)

    def test_no_quantity_is_one_pack_once_the_size_is_known(self):
        self.add_toilet_paper(pack_size=10, pack_size_set_at=AT)
        self.assertEqual(self.lines("+ toilet paper $6"), ["✓ Toilet paper: bought 1 pack of 10 rolls for $6.00 ($0.60/roll). First price recorded."])
        self.add_item("rice", "Rice", "g")
        self.assertEqual(self.lines("+ rice $12", update_id=2), [
            "How much is in each pack of Rice? Resend like: + rice 1x5kg $12.00 — or write the total, like 5kg."
        ])

    def test_stock_count_uses_the_same_rules(self):
        self.add_toilet_paper(pack_size=10, pack_size_set_at=AT)
        self.assertEqual(self.lines("have toilet paper 3"), ["✓ Toilet paper: counted 3 packs × 10 rolls (30 rolls) in stock."])
        self.assertEqual(store.get_item("toilet-paper")["stock_est"], 30)
        self.add_face_masks()
        line, = self.lines("have face masks 3", update_id=2)
        self.assertIn("Resend like: have face masks 3x5pcs", line)

    def test_price_check_records_the_unit_price(self):
        self.add_item("laundry-detergent", "Laundry detergent", "g", ["detergent"])
        self.assertEqual(self.lines("? detergent 3kg $8.95 @shengsiong"),
                         ["Laundry detergent: $8.95 for 3 kg = $0.30/100 g at Sheng Siong. Price noted. First price recorded."])
        self.assertNotIn("stock_est", store.get_item("laundry-detergent"))

    def test_grams_and_millilitres_stand_in_for_each_other(self):
        self.add_item("laundry-detergent", "Laundry detergent", "g")
        self.send("+ laundry detergent 3L $9")
        self.assertAlmostEqual(self.events("laundry-detergent")[0]["unit_price"], 0.003)


class ItemResolutionTest(ApplyTestCase):
    def test_exact_name_or_alias_is_used_quietly(self):
        self.add_toilet_paper(aliases=["TP"])
        lines, _, buttons = self.send("+ tp 1x10")
        self.assertEqual((lines, buttons), (["✓ Toilet paper: bought 1 pack of 10 rolls."], []))
        self.assertEqual(self.table.keys("ITEM"), [("ITEM", "toilet-paper")])
        self.assertEqual(store.get_item("toilet-paper")["aliases"], ["TP"])

    def test_ai_sees_the_phrase_and_the_tracked_items(self):
        self.add_toilet_paper()
        self.ai_says("toilet-paper")
        self.send("+ Loo Roll 2x10")
        phrase, items = self.match_item.call_args.args
        self.assertEqual(phrase, "Loo Roll")
        self.assertEqual([item["item_id"] for item in items], ["toilet-paper"])

    def test_new_item_gets_the_ais_name_and_the_senders_phrase_as_an_alias_straight_away(self):
        self.ai_says(None, "Dishwashing liquid", "ml")
        lines, counts, buttons = self.send("+ mama lemon 2x500ml $5")
        item = store.get_item("dishwashing-liquid")
        self.assertEqual((item["name"], item["base_unit"], item["created_at"]), ("Dishwashing liquid", "ml", AT))
        self.assertEqual((item["aliases"], item["pack_size"]), (["mama lemon"], 500))
        self.assertEqual(lines, ["✓ Dishwashing liquid (new item): bought 2 packs × 500 ml (1 L) for $5.00 ($0.50/100 ml). First price recorded."])
        self.assertEqual((counts, buttons), ({"lines_structured": 1, "ai_match_calls": 1}, []))

    def test_typed_unit_beats_the_ais_guess_for_a_new_item(self):
        self.ai_says(None, "Laundry detergent", "ml")
        self.send("+ dynamo 3.6kg $11.95")
        self.assertEqual(store.get_item("laundry-detergent")["base_unit"], "g")

    def test_new_item_with_only_a_pack_count_is_created_but_nothing_is_recorded(self):
        self.ai_says(None, "Soy sauce", "ml")
        lines, counts, _ = self.send("+ kikkoman 2 $7")
        self.assertEqual(lines, ["How much is in each pack of Soy sauce? Resend like: + kikkoman 2x1L $7.00 — "
                                 "or write the total, like 1L."])
        self.assertEqual(counts, {"ai_match_calls": 1, "lines_rejected": 1})
        self.assertEqual(self.table.keys("EVT#"), [])
        self.assertEqual(store.get_item("soy-sauce")["aliases"], ["kikkoman"])
        # The resend finds the item by the saved name: no second AI call.
        self.match_item.side_effect = no_ai
        self.assertEqual(self.lines("+ kikkoman 2x1L $7", update_id=2),
                         ["✓ Soy sauce: bought 2 packs × 1 L (2 L) for $7.00 ($0.35/100 ml). First price recorded."])

    def test_slug_is_capped_and_never_empty(self):
        self.assertEqual(len(apply.slugify("extra " * 20)), 40)
        self.assertFalse(apply.slugify("a" * 39 + " b").endswith("-"))
        self.assertEqual(apply.slugify("酱油"), "item")

    def test_taken_slug_gets_a_number(self):
        self.add_item("soap", "Soap bars", "piece")
        self.ai_says(None, "Soap", "ml")
        self.send("+ liquid soap 500ml")
        self.assertEqual(store.get_item("soap-2")["name"], "Soap")

    def test_ai_naming_a_known_item_without_its_id_reuses_it_as_a_guess(self):
        self.add_toilet_paper()
        self.ai_says(None, "Toilet Paper", "roll")
        _, _, buttons = self.send("+ loo roll 1x10")
        self.assertEqual(self.table.keys("ITEM"), [("ITEM", "toilet-paper")])
        self.assertEqual(len(buttons), 1)

    def test_same_new_item_twice_in_one_message_is_created_and_looked_up_once(self):
        self.ai_says(None, "Sponge", "piece")
        self.send("+ sponge 2pcs\n+ sponges 1pc")
        self.assertEqual(self.table.keys("ITEM"), [("ITEM", "sponge")])
        self.assertEqual(store.get_item("sponge")["stock_est"], 3)
        self.match_item.assert_called_once()

    def test_phrase_that_is_not_a_product_records_nothing_and_stays_silent(self):
        self.add_toilet_paper()
        self.ai_says(None, "")
        lines, counts, buttons = self.send("have you eaten 2")
        self.assertEqual((lines, buttons), ([], []))
        self.assertEqual(counts, {"ai_match_calls": 1, "lines_rejected": 1})
        self.assertEqual(self.table.keys("EVT#") + self.table.keys("USER#"), [])

    def test_symbol_line_always_gets_an_answer_even_when_nothing_is_recorded(self):
        self.add_toilet_paper()
        self.ai_says(None, "")
        answer = ('I don\'t know what "mama lemon" is, so it isn\'t recorded. '
                  "Use the product's usual name, or teach me this one: /alias mama lemon = <item>")
        for update_id, text in enumerate(["+ mama lemon 2x500ml $5", "- mama lemon", "? mama lemon 500ml $2.50", "+mama lemon"]):
            with self.subTest(text=text):
                lines, counts, buttons = self.send(text, update_id=update_id)
                self.assertEqual(lines, [answer])
                self.assertEqual((counts, buttons), ({"ai_match_calls": 1, "lines_rejected": 1}, []))
        self.assertEqual(self.table.keys("EVT#") + self.table.keys("USER#"), [])
        self.assertEqual(self.table.keys("ITEM"), [("ITEM", "toilet-paper")])

    def test_entry_commands_always_get_an_answer_too(self):
        self.ai_says(None, "")
        for update_id, text in enumerate(["/add mama lemon 2x500ml", "/bought mama lemon $3", "/used mama lemon"]):
            with self.subTest(text=text):
                line, = self.lines(text, update_id=update_id)
                self.assertIn('I don\'t know what "mama lemon" is', line)

    def test_word_keyword_chat_still_gets_no_answer(self):
        self.ai_says(None, "")
        for text in ("have you eaten 2", "last night was fun", "done with dinner", "add oil!"):
            with self.subTest(text=text):
                self.assertEqual(self.lines(text), [])

    def test_ai_failure_skips_that_line_and_keeps_the_rest(self):
        self.add_toilet_paper()
        self.match_item.side_effect = claude_api.ClaudeError("HTTP 529 overloaded_error")
        lines, counts, _ = self.send("+ loo roll 2x10\n+ toilet paper 1x10")
        self.assertEqual(lines, [
            'Couldn\'t look up "loo roll" just now, so it isn\'t recorded. Please send that line again.',
            "✓ Toilet paper: bought 1 pack of 10 rolls.",
        ])
        self.assertEqual(counts, {"lines_structured": 1})

    def test_several_guesses_in_one_message_get_a_button_row_each(self):
        self.add_toilet_paper()
        self.add_item("rice", "Rice", "g")
        _, _, buttons = self.send("+ toilet papr 2x10\n- rice\n+ toilet papers 1x10\n- ricee", update_id=4)
        self.assertEqual([[data for _, data in row] for row in buttons],
                         [["m:same:4:0", "m:new:4:0"], ["m:same:4:3", "m:new:4:3"]])

    def test_long_phrase_is_shortened_on_the_button(self):
        self.add_toilet_paper()
        self.ai_says("toilet-paper")
        _, _, buttons = self.send("+ super soft three ply quilted luxury bathroom tissue 1x10")
        label = buttons[0][1][0]
        self.assertLessEqual(len(label), len("New item: ") + apply.MAX_BUTTON_NAME)
        self.assertTrue(label.endswith("…"))


class BrandTest(ApplyTestCase):
    """A brand taught with /brand is taken off the front of the item phrase and noted on the entry."""

    def test_brand_then_a_known_name_needs_no_ai_and_no_tap(self):
        self.add_facial_masks()
        lines, counts, buttons = self.send("+ kans facial masks 2pcs $1.96 @taobao", brands=["kans"])
        self.assertEqual(lines, ["✓ Facial masks: bought 2 pieces for $1.96 at Taobao ($0.98/piece). First price recorded."])
        self.assertEqual((counts, buttons), ({"lines_structured": 1}, []))
        self.assertEqual(self.events("facial-masks")[0]["brand"], "kans")
        item = store.get_item("facial-masks")
        self.assertEqual((item["aliases"], item["brands"]), ([], ["kans"]))
        self.match_item.assert_not_called()

    def test_the_sentence_that_went_wrong(self):
        # Before brands: read as an item called "kans facial masks for", sent to the AI, shown with buttons.
        self.add_facial_masks(pack_size=5, pack_size_set_at=AT)
        lines, _, buttons = self.send("bought 2 kans facial masks for 1.96 from Taobao", brands=["kans"])
        self.assertEqual(lines, ["✓ Facial masks: bought 2 packs × 5 pieces (10 pieces) for $1.96 at Taobao ($0.20/piece). First price recorded."])
        self.assertEqual(buttons, [])

    def test_the_question_keeps_the_brand_and_the_store_in_its_example(self):
        self.add_facial_masks()
        lines, _, _ = self.send("bought 2 kans facial masks for 1.96 from Taobao", brands=["kans"])
        self.assertEqual(lines, ["How many in each pack of Facial masks? Resend like: "
                                 "+ kans facial masks 2x5pcs $1.96 @Taobao — or write 2pcs if you mean single pieces."])
        # Sending the example back records it, brand and store included.
        self.send("+ kans facial masks 2x5pcs $1.96 @Taobao", update_id=2, brands=["kans"])
        event, = self.events("facial-masks")
        self.assertEqual((event["qty"], event["brand"], event["store"]), (10, "kans", "Taobao"))

    def test_brand_then_an_unknown_name_asks_the_ai_about_the_name_only(self):
        self.add_face_masks()
        self.ai_says(None, "Sleeping masks", "piece")
        lines, _, _ = self.send("+ kans sleeping mask 3pcs", brands=["kans"])
        self.assertEqual(self.match_item.call_args.args[0], "sleeping mask")
        self.assertEqual(lines, ["✓ Sleeping masks (new item): bought 3 pieces."])
        self.assertEqual(self.events("sleeping-masks")[0]["brand"], "kans")
        self.assertEqual(store.get_item("sleeping-masks")["aliases"], [])

    def test_a_guess_after_a_brand_names_the_phrase_without_the_brand(self):
        self.add_face_masks()
        self.ai_says("face-masks")
        lines, _, buttons = self.send("+ kans beauty masks 3pcs", update_id=3, brands=["kans"])
        self.assertEqual(lines, ['✓ Face masks: bought 3 pieces. (Read "beauty masks" as Face masks.)'])
        self.assertEqual(buttons[0][1], ("New item: Beauty masks", "m:new:3:0"))

    def test_brand_in_free_text_is_taken_off_the_models_wording_too(self):
        self.add_facial_masks()
        events = [free_event(item_id="facial-masks", item_name="Kans facial masks", base_unit="piece", quantity_text="2 pcs")]
        lines, _, buttons = self.apply(ai.ground_free_text(events, "got 2 pcs Kans facial masks"), brands=["kans"])
        self.assertEqual((lines, buttons), (["✓ Facial masks: bought 2 pieces."], []))
        self.assertEqual(self.events("facial-masks")[0]["brand"], "kans")

    def test_without_the_brand_being_known_the_first_word_stays(self):
        self.add_facial_masks()
        self.ai_says("facial-masks")
        _, _, buttons = self.send("+ kans facial masks 2pcs")
        self.assertEqual(self.match_item.call_args.args[0], "kans facial masks")
        self.assertEqual(len(buttons), 1)


class ConfirmationTest(ApplyTestCase):
    def guess(self, text="+ loo roll 1x10 $6", update_id=7):
        self.add_toilet_paper(pack_size=10, pack_size_set_at="2026-10-01T00:00:00Z")
        self.ai_says("toilet-paper")
        self.send(text, update_id=update_id)
        return store.get_pending(str(update_id), "0")

    def test_same_item_saves_the_alias(self):
        item = apply.confirm_same(self.guess())
        self.assertEqual(item["name"], "Toilet paper")
        self.assertEqual(store.get_item("toilet-paper")["aliases"], ["loo roll"])

    def test_new_item_moves_the_event_and_keeps_undo_working(self):
        pending = self.guess("+ bog roll 2 $6")
        new = apply.split_into_new_item(pending)
        self.assertEqual(new["name"], "Bog roll")
        self.assertEqual(self.events("toilet-paper"), [])
        self.assertEqual(self.events("bog-roll")[0]["qty"], 20)
        recent, = store.list_recent_events(42)
        self.assertEqual((recent["event_pk"], recent["item_name"]), ("EVT#bog-roll", "Bog roll"))

    def test_new_item_takes_the_unit_typed_on_the_line(self):
        self.add_item("dish-soap", "Dishwashing liquid", "ml")
        self.ai_says("dish-soap")
        self.send("+ dishwasher powder 500g", update_id=9)
        new = apply.split_into_new_item(store.get_pending("9", "0"))
        self.assertEqual((new["name"], new["base_unit"]), ("Dishwasher powder", "g"))

    def test_second_tap_for_the_same_phrase_joins_the_item_already_made(self):
        self.add_face_masks()
        self.ai_says("face-masks")
        self.send("+ hand masks 10pcs", update_id=1)
        self.send("+ hand masks 5pcs", update_id=2)
        first = apply.split_into_new_item(store.get_pending("1", "0"))
        second = apply.split_into_new_item(store.get_pending("2", "0"))
        self.assertEqual(first["item_id"], second["item_id"])
        self.assertEqual(store.get_item("hand-masks")["stock_est"], 15)
        self.assertEqual(self.table.keys("ITEM"), [("ITEM", "hand-masks")])

    def test_entry_undone_before_the_tap_is_reported(self):
        pending = self.guess()
        store.delete_event(pending["event_pk"], pending["event_sk"])
        self.assertIsNone(apply.split_into_new_item(pending))


class FreeTextTest(ApplyTestCase):
    """Entries from the AI free-text fallback, after grounding (PLAN.md §5.4)."""

    SAID = "bought 2 packs toilet paper 10 rolls each $12.90 fairprice"

    def test_free_text_purchase_gives_the_same_event_as_the_formatted_one(self):
        self.add_toilet_paper()
        said, counts, buttons = self.said(self.SAID, dict(
            item_id="toilet-paper", item_name="toilet paper",
            quantity_text="2 packs toilet paper 10 rolls each", total_price=12.9, store="fairprice",
        ))
        typed, _, _ = self.send("+ toilet paper 2x10 $12.90 @fairprice", update_id=2)
        self.assertEqual(said, ["✓ Toilet paper: bought 2 packs × 10 rolls (20 rolls) for $12.90 at FairPrice ($0.65/roll). First price recorded."])
        self.assertEqual(typed, ["✓ Toilet paper: bought 2 packs × 10 rolls (20 rolls) for $12.90 at FairPrice ($0.65/roll). 🔥 As good as your best: $0.65/roll at FairPrice."])
        self.assertEqual((counts, buttons), ({}, []))
        first, second = self.events("toilet-paper")
        differ = {"sk", "raw_text", "parse_path"}
        self.assertEqual({k: v for k, v in first.items() if k not in differ},
                         {k: v for k, v in second.items() if k not in differ})
        self.assertEqual((first["parse_path"], second["parse_path"]), ("ai_full", "structured"))
        self.assertEqual(first["raw_text"], self.SAID)

    def test_new_wording_for_a_known_item_is_a_guess_to_confirm(self):
        self.add_toilet_paper()
        lines, _, buttons = self.said("got loo roll, 10 rolls", dict(
            item_id="toilet-paper", item_name="loo roll", quantity_text="10 rolls"), update_id=6)
        self.assertEqual(lines, ['✓ Toilet paper: bought 10 rolls. (Read "loo roll" as Toilet paper.)'])
        self.assertEqual(buttons, [[("Same item", "m:same:6:0"), ("New item: Loo roll", "m:new:6:0")]])
        self.assertEqual(store.get_item("toilet-paper")["aliases"], [])

    def test_known_wording_beats_the_models_pick(self):
        self.add_face_masks()
        self.add_item("hand-masks", "Hand masks", "piece")
        lines, _, buttons = self.said("HAND masks 10 pcs $1.45 from Taobao", dict(
            item_id="face-masks", item_name="HAND masks", quantity_text="10 pcs", total_price=1.45, store="Taobao"))
        self.assertEqual(lines, ["✓ Hand masks: bought 10 pieces for $1.45 at Taobao ($0.14/piece). First price recorded."])
        self.assertEqual(buttons, [])

    def test_brand_alone_is_recorded_and_remembered_but_not_learned_as_a_name(self):
        self.add_item("dish-soap", "Dishwashing liquid", "ml")
        lines, _, buttons = self.said("grabbed a 1L Mama Lemon", dict(
            item_id="dish-soap", item_name="Mama Lemon", quantity_text="1L", brand="Mama Lemon"))
        self.assertEqual((lines, buttons), (["✓ Dishwashing liquid: bought 1 L."], []))
        item = store.get_item("dish-soap")
        self.assertEqual((item["aliases"], item["brands"]), ([], ["Mama Lemon"]))
        self.assertEqual(self.events("dish-soap")[0]["brand"], "Mama Lemon")

    def test_new_item_takes_the_models_unit_when_none_is_written(self):
        lines, _, _ = self.said("ran out of oyster sauce", dict(action="finished", item_name="Oyster sauce", base_unit="ml"))
        self.assertEqual(lines, ["✓ Oyster sauce (new item): marked finished."])
        self.assertEqual(store.get_item("oyster-sauce")["base_unit"], "ml")

    def test_dropped_fact_is_named_in_the_reply(self):
        self.add_facial_masks()
        lines, _, _ = self.said("Facial masks 5 pcs 1.86 from Qoo10", dict(
            item_id="facial-masks", item_name="facial masks", quantity_text="5 pcs", total_price=1.86, store="Shopee"))
        self.assertEqual(lines, ["✓ Facial masks: bought 5 pieces for $1.86 ($0.37/piece). First price recorded. (Didn't catch the store.)"])
        self.assertNotIn("store", self.events("facial-masks")[0])

    def test_quantity_the_model_made_up_falls_back_to_asking(self):
        self.add_face_masks()
        lines, _, _ = self.said("bought face masks", dict(item_id="face-masks", item_name="face masks", quantity_text="2 boxes"))
        self.assertIn("How many in each pack of Face masks?", lines[0])
        self.assertEqual(self.table.keys("EVT#"), [])

    def test_bare_number_for_something_counted_is_asked_about_not_taken_for_packs(self):
        self.add_item("eggs", "Eggs", "piece", pack_size=10, pack_size_set_at=AT)
        lines, counts, buttons = self.said("got 30 eggs $6.80 ntuc", dict(
            item_id="eggs", item_name="eggs", base_unit="piece", quantity_text="30 eggs", total_price=6.8, store="ntuc"))
        self.assertEqual(lines, ["Is that 30 pieces of Eggs, or 30 packs? Resend like: + eggs 30pcs $6.80 @FairPrice — "
                                 "or write 30 packs if you mean packs of 10 pieces."])
        self.assertEqual((counts, buttons), ({"lines_rejected": 1}, []))
        self.assertEqual(self.table.keys("EVT#"), [])
        # Either answer is then read by the format.
        self.assertEqual(self.lines("+ eggs 30pcs $6.80 @FairPrice", update_id=2),
                         ["✓ Eggs: bought 30 pieces for $6.80 at FairPrice ($0.23/piece). First price recorded."])
        self.assertEqual(self.lines("+ eggs 30 packs", update_id=3), ["✓ Eggs: bought 30 packs × 10 pieces (300 pieces)."])

    def test_bare_number_is_asked_about_for_a_count_and_for_what_was_used_too(self):
        self.add_toilet_paper(pack_size=10, pack_size_set_at=AT)
        lines, _, _ = self.said("we have 3 toilet paper left", dict(
            action="stock_count", item_id="toilet-paper", item_name="toilet paper", quantity_text="3 toilet paper"))
        self.assertEqual(lines, ["Is that 3 rolls of Toilet paper, or 3 packs? Resend like: have toilet paper 3rolls — "
                                 "or write 3 packs if you mean packs of 10 rolls."])
        lines, _, _ = self.said("used 2 toilet paper", dict(
            action="used", item_id="toilet-paper", item_name="toilet paper", quantity_text="2"))
        self.assertIn("Resend like: - toilet paper 2rolls — or write 2 packs", lines[0])
        self.assertEqual(self.table.keys("EVT#"), [])

    def test_bare_number_for_something_weighed_or_poured_is_packs(self):
        self.add_item("shampoo", "Shampoo", "ml", pack_size=500, pack_size_set_at=AT)
        lines, _, _ = self.said("got 2 shampoo", dict(item_id="shampoo", item_name="shampoo", base_unit="ml", quantity_text="2 shampoo"))
        self.assertEqual(lines, ["✓ Shampoo: bought 2 packs × 500 ml (1 L)."])

    def test_a_pack_word_in_the_sentence_needs_no_question(self):
        self.add_item("eggs", "Eggs", "piece", pack_size=10, pack_size_set_at=AT)
        lines, _, _ = self.said("got 3 boxes of eggs", dict(item_id="eggs", item_name="eggs", base_unit="piece", quantity_text="3 boxes"))
        self.assertEqual(lines, ["✓ Eggs: bought 3 packs × 10 pieces (30 pieces)."])

    def test_format_and_free_text_entries_in_one_message_get_their_own_keys(self):
        self.add_toilet_paper()
        both = entries("+ toilet paper 2x10") + ai.ground_free_text(
            [free_event(item_id="toilet-paper", item_name="toilet paper", quantity_text="10 rolls")], "also 10 rolls of toilet paper")
        self.apply(both, update_id=9)
        self.assertEqual([sk for _, sk in self.table.keys("EVT#")], [f"{AT}#9#0", f"{AT}#9#1"])


class AliasTest(ApplyTestCase):
    def test_aliases_stop_at_the_cap_and_skip_overlong_phrases(self):
        item = {"item_id": "rice", "name": "Rice", "base_unit": "g", "aliases": [f"name {n}" for n in range(apply.MAX_ALIASES)]}
        self.assertFalse(apply.learn_alias(item, "nasi"))
        item["aliases"] = []
        self.assertFalse(apply.learn_alias(item, "very " * 10 + "long grain rice"))
        self.assertFalse(apply.learn_alias(item, "Rice"))
        self.assertTrue(apply.learn_alias(item, "Nasi!"))
        self.assertEqual(item["aliases"], ["nasi"])


class TimeTest(unittest.TestCase):
    def test_iso_and_epoch_round_trip(self):
        self.assertEqual(apply.iso_utc(1791555000), "2026-10-09T14:10:00Z")
        self.assertEqual(apply.epoch("2026-10-09T14:10:00Z"), 1791555000)

    def test_month_key_is_in_singapore_time(self):
        # 2026-10-31 16:30 UTC is already 1 November in Singapore.
        self.assertEqual(apply.month_key(apply.epoch("2026-10-31T16:30:00Z")), "2026-11")
        self.assertEqual(apply.month_key(apply.epoch("2026-10-31T15:30:00Z")), "2026-10")


class PriceVerdictTest(ApplyTestCase):
    """How a priced entry compares with earlier prices (PLAN.md §8.2), and repeated logs."""

    def setUp(self):
        super().setUp()
        self.add_toilet_paper(pack_size=10)

    def test_a_cheaper_purchase_is_the_best_yet(self):
        self.send("+ toilet paper 2x10 $12.90 @fairprice", update_id=1)
        lines = self.lines("+ toilet paper 2x10 $11.60 @giant", update_id=2)
        self.assertEqual(lines, ["✓ Toilet paper: bought 2 packs × 10 rolls (20 rolls) for $11.60 at Giant ($0.58/roll). "
                                 "🔥 Best price yet. It was $0.65/roll at FairPrice."])

    def test_a_dearer_purchase_says_what_the_best_was(self):
        self.send("+ toilet paper 2x10 $10.00 @fairprice", update_id=1)
        lines = self.lines("+ toilet paper 2x10 $20.00 @giant", update_id=2)
        self.assertEqual(lines, ["✓ Toilet paper: bought 2 packs × 10 rolls (20 rolls) for $20.00 at Giant ($1.00/roll). "
                                 "Above usual (usually $0.50/roll). Best seen: $0.50/roll at FairPrice."])

    def test_an_entry_is_only_compared_with_earlier_ones(self):
        lines = self.lines("+ toilet paper 2x10 $12.90 @fairprice")
        self.assertIn("First price recorded.", lines[0])

    def test_a_price_check_gets_a_verdict_and_joins_the_history(self):
        self.assertEqual(self.lines("? toilet paper 10 rolls $6 @giant", update_id=1),
                         ["Toilet paper: $6.00 for 10 rolls = $0.60/roll at Giant. Price noted. First price recorded."])
        lines = self.lines("? toilet paper 10 rolls $5 @giant", update_id=2)
        self.assertIn("🔥 Best price yet. It was $0.60/roll at Giant.", lines[0])

    def test_no_price_means_no_verdict(self):
        self.assertEqual(self.lines("+ toilet paper 2x10"), ["✓ Toilet paper: bought 2 packs × 10 rolls (20 rolls)."])

    def test_only_purchases_and_price_checks_are_compared(self):
        self.send("+ toilet paper 2x10 $12.90", update_id=1)
        self.assertNotIn("best", self.lines("have toilet paper 2x10 $5", update_id=2)[0].lower())
        self.assertNotIn("best", self.lines("- toilet paper 1", update_id=3)[0].lower())

    def test_two_lines_of_one_message_are_compared_in_order(self):
        lines = self.lines("+ toilet paper 2x10 $12.90\n+ toilet paper 2x10 $11.60")
        self.assertIn("First price recorded.", lines[0])
        self.assertIn("🔥 Best price yet.", lines[1])

    def test_identical_purchases_on_one_day_are_both_true_logs(self):
        self.send("+ toilet paper 2x10 $12.90 @fairprice", update_id=1)
        self.send("+ toilet paper 2x10 $12.90 @fairprice", update_id=2)
        self.assertEqual([event["qty"] for event in self.events("toilet-paper")], [20, 20])
        self.assertEqual(store.get_item("toilet-paper")["stock_est"], 40)

    def test_an_old_entry_with_no_currency_counts_as_sgd(self):
        store.put_event("toilet-paper", "2026-10-08T10:00:00Z#1#0", {
            "action": "bought", "packs": 1, "units_per_pack": 10, "total_price": 5.0, "unit_price": 0.5,
            "store": "Giant", "at": "2026-10-08T10:00:00Z",
        })
        lines = self.lines("+ toilet paper 2x10 $12.90")
        self.assertIn("Best seen: $0.50/roll at Giant.", lines[0])

    def test_a_price_logged_in_another_currency_is_not_compared(self):
        store.put_event("toilet-paper", "2026-10-08T10:00:00Z#1#0", {
            "action": "bought", "qty": 10, "total_price": 1.0, "unit_price": 0.1, "currency": "MYR",
            "store": "Lotus", "at": "2026-10-08T10:00:00Z",
        })
        self.assertIn("First price recorded.", self.lines("+ toilet paper 2x10 $12.90")[0])

    def test_a_new_items_first_price_says_so(self):
        self.ai_says(name="Hand masks", base_unit="piece")
        lines = self.lines("+ hand masks 10 pcs $1.45 @taobao")
        self.assertEqual(lines, ["✓ Hand masks (new item): bought 10 pieces for $1.45 at Taobao ($0.14/piece). First price recorded."])


class DatedEntryTest(ApplyTestCase):
    """An entry from an old receipt: when it happened isn't when it was logged. AT is 18:00 on 9 Oct in Singapore."""

    SEP_15 = "2026-09-15T04:00:00Z"  # noon in Singapore

    def setUp(self):
        super().setUp()
        self.add_toilet_paper(pack_size=10)

    def test_it_is_recorded_on_that_day_and_remembers_when_it_was_logged(self):
        lines = self.lines("+ toilet paper 2x10 $12.90 @fairprice on 15/09/2026")
        event, = self.events("toilet-paper")
        self.assertEqual((event["at"], event["logged_at"]), (self.SEP_15, AT))
        self.assertTrue(event["sk"].startswith(self.SEP_15))
        self.assertEqual(lines, ["✓ Toilet paper: bought 2 packs × 10 rolls (20 rolls) for $12.90 at FairPrice ($0.65/roll) "
                                 "on Tue 15 Sep 2026. First price recorded."])

    def test_with_no_date_it_is_recorded_when_it_was_sent(self):
        self.send("+ toilet paper 2x10")
        event, = self.events("toilet-paper")
        self.assertEqual(event["at"], AT)
        self.assertNotIn("logged_at", event)

    def test_todays_date_is_no_different_from_no_date(self):
        lines = self.lines("+ toilet paper 2x10 on 9/10/2026")
        event, = self.events("toilet-paper")
        self.assertEqual(event["at"], AT)
        self.assertNotIn("logged_at", event)
        self.assertEqual(lines, ["✓ Toilet paper: bought 2 packs × 10 rolls (20 rolls)."])

    def test_today_is_the_day_in_singapore(self):
        late = "2026-10-09T18:00:00Z"  # already 2 am on 10 Oct there
        self.send("+ toilet paper 2x10 on 10/10/2026", at=late)
        self.assertEqual(self.events("toilet-paper")[0]["at"], late)

    def test_with_no_year_it_is_the_last_time_that_date_came_round(self):
        lines = self.lines("+ toilet paper 2x10 on 5/12")
        self.assertEqual(self.events("toilet-paper")[0]["at"], "2025-12-05T04:00:00Z")
        self.assertIn("on Fri 5 Dec 2025", lines[0])

    def test_a_date_to_come_is_not_recorded(self):
        lines, counts, _ = self.send("+ toilet paper 2x10 on 5/12/2026")
        self.assertEqual(lines, ["5/12/2026 hasn't happened yet, so that line isn't recorded. " + replies.DATE_FORMAT])
        self.assertEqual(self.events("toilet-paper"), [])
        self.assertEqual(counts, {"lines_rejected": 1})

    def test_a_date_that_does_not_exist_is_not_recorded(self):
        for written in ("31/2/2026", "9/15/2026"):
            with self.subTest(written=written):
                lines = self.lines(f"+ toilet paper 2x10 on {written}")
                self.assertEqual(lines, [f"{written} isn't a date, so that line isn't recorded. " + replies.DATE_FORMAT])
        self.assertEqual(self.events("toilet-paper"), [])

    def test_a_bad_date_costs_no_ai_call_and_makes_no_item(self):
        self.send("+ caviar 2pcs on 31/2/2026")  # no_ai would fail the test if the AI were asked
        self.assertEqual([item["item_id"] for item in store.list_items()], ["toilet-paper"])

    def test_the_other_lines_of_the_message_are_still_recorded(self):
        lines = self.lines("+ toilet paper 2x10 on 5/12/2026\n+ toilet paper 1x10 on 15/09/2026")
        self.assertEqual(len(lines), 2)
        self.assertEqual([event["qty"] for event in self.events("toilet-paper")], [10])

    def test_older_entries_replay_in_date_order(self):
        self.send("+ toilet paper 1x10", update_id=1)
        self.send("+ toilet paper 2x10 on 15/09/2026", update_id=2)
        self.assertEqual([event["qty"] for event in self.events("toilet-paper")], [20, 10])

    def test_an_old_purchase_adds_to_stock_when_nothing_came_after_it(self):
        lines = self.lines("+ toilet paper 2x10 on 15/09/2026")
        self.assertEqual(store.get_item("toilet-paper")["stock_est"], 20)
        self.assertNotIn("Stock stays", lines[0])

    def test_a_purchase_before_a_later_count_leaves_the_count_alone_and_says_why(self):
        self.send("have toilet paper 1x10", update_id=1)
        lines = self.lines("+ toilet paper 2x10 on 15/09/2026", update_id=2)
        self.assertEqual(store.get_item("toilet-paper")["stock_est"], 10)
        self.assertTrue(lines[0].endswith("(Stock stays the same: it was counted or ran out after that date.)"), lines[0])

    def test_a_purchase_before_a_run_out_leaves_the_item_out(self):
        self.send("- toilet paper", update_id=1)
        lines = self.lines("+ toilet paper 2x10 on 15/09/2026", update_id=2)
        item = store.get_item("toilet-paper")
        self.assertEqual((item["stock_est"], item["status"]), (0, "out"))
        self.assertIn("Stock stays the same", lines[0])

    def test_an_ordinary_purchase_never_says_stock_stayed_the_same(self):
        self.assertNotIn("Stock stays", self.lines("+ toilet paper 2x10", update_id=1)[0])

    def test_other_kinds_of_entry_spell_out_the_date_too(self):
        self.send("+ toilet paper 5x10", update_id=1, at="2026-08-01T04:00:00Z")
        self.assertEqual(self.lines("- toilet paper 2x10 on 15/09/2026", update_id=2),
                         ["✓ Toilet paper: used 2 packs × 10 rolls (20 rolls) on Tue 15 Sep 2026, 30 rolls (3 packs) left."])
        self.assertIn("at Giant on Tue 15 Sep 2026. Price noted.", self.lines("? toilet paper 10 rolls $6 @giant on 15/09/2026", update_id=5)[0])
        self.assertIn("counted 1 pack of 10 rolls in stock on Tue 15 Sep 2026", self.lines("have toilet paper 1x10 on 15/09/2026", update_id=6)[0])

    def test_two_receipts_with_one_price_on_different_days_are_two_prices(self):
        self.send("+ toilet paper 2x10 $12.90 @fairprice on 1/09/2026", update_id=1)
        self.send("+ toilet paper 2x10 $12.90 @fairprice on 8/09/2026", update_id=2)
        points = pricing.price_points(self.events("toilet-paper"), apply.epoch(AT))
        self.assertEqual([(point["day"], point["entries"]) for point in points], [(date(2026, 9, 1), 1), (date(2026, 9, 8), 1)])

    def test_two_entries_for_one_receipt_date_are_one_price(self):
        self.send("+ toilet paper 2x10 $12.90 @fairprice on 1/09/2026", update_id=1)
        self.send("+ toilet paper 2x10 $12.90 @fairprice on 1/09/2026", update_id=2)
        points = pricing.price_points(self.events("toilet-paper"), apply.epoch(AT))
        self.assertEqual([point["entries"] for point in points], [2])
        self.assertEqual(store.get_item("toilet-paper")["stock_est"], 40)

    def test_a_question_back_keeps_the_date_so_the_resend_is_not_logged_as_today(self):
        self.add_face_masks()
        lines = self.lines("+ face masks 2 $1.96 on 15/09/2026")
        self.assertEqual(lines, ["How many in each pack of Face masks? Resend like: + face masks 2x5pcs $1.96 on 15/09/2026 — "
                                 "or write 2pcs if you mean single pieces."])

    def test_moving_a_dated_guess_to_a_new_item_keeps_undo_working(self):
        self.ai_says(item_id="toilet-paper", name="Toilet paper", base_unit="roll")
        self.send("+ loo roll 1x10 $6 on 15/09/2026", update_id=7)
        new = apply.split_into_new_item(store.get_pending("7", "0"))
        recent, = store.list_recent_events(42)
        self.assertEqual(recent["event_pk"], f"EVT#{new['item_id']}")
        self.assertTrue(recent["event_sk"].startswith(self.SEP_15))

    def test_free_text_uses_the_date_written_in_the_message(self):
        text = "bought 2 packs toilet paper 10 rolls each $12.90 fairprice on 15/09/2026"
        lines, _, _ = self.said(text, dict(item_id="toilet-paper", item_name="toilet paper",
                                           quantity_text="2 packs toilet paper 10 rolls each", total_price=12.9))
        event, = self.events("toilet-paper")
        self.assertEqual((event["at"], event["logged_at"]), (self.SEP_15, AT))
        self.assertIn("on Tue 15 Sep 2026", lines[0])

    def test_free_text_with_an_unreadable_date_is_not_recorded_and_says_so_once(self):
        lines, _, _ = self.said("got toilet paper and rice on 15 sep",
                                dict(item_id="toilet-paper", item_name="toilet paper", quantity_text=None),
                                dict(item_id="toilet-paper", item_name="toilet paper", quantity_text=None))
        self.assertEqual(lines, [replies.DATE_HINT])
        self.assertEqual(self.events("toilet-paper"), [])


class StocktakeTest(ApplyTestCase):
    """A stocktake written over several lines or messages adds up (PLAN.md §8.1)."""

    def setUp(self):
        super().setUp()
        self.add_item("body-wash", "Body wash", "ml", pack_size=750)
        self.add_item("hand-wash", "Hand wash", "g")

    @staticmethod
    def count(item_id, quantity_text, brand=None, **fields):
        name = item_id.replace("-", " ")
        return dict(action="stock_count", item_id=item_id, item_name=name, base_unit="ml" if item_id == "body-wash" else "g",
                    quantity_text=quantity_text, brand=brand, **fields)

    def test_the_lines_of_a_stocktake_add_up_and_each_reply_says_the_total(self):
        text = ("Stock take for body wash\n1 bottle of 1L Dove body wash\n2 Shokubutsu refill packs 500 ml each\n"
                "1 bottle opos body wash 500 ml")
        lines, _, _ = self.said(text, self.count("body-wash", "1 bottle of 1L", "Dove"),
                                self.count("body-wash", "2 Shokubutsu refill packs 500 ml each", "Shokubutsu"),
                                self.count("body-wash", "1 bottle opos body wash 500 ml", "opos"), update_id=5)
        self.assertEqual(lines, ["✓ Body wash: counted 1 pack of 1 L in stock.",
                                 "✓ Body wash: counted 2 packs × 500 ml (1 L) in stock, 2 L in all.",
                                 "✓ Body wash: counted 1 pack of 500 ml in stock, 2.5 L in all."])
        self.assertEqual(store.get_item("body-wash")["stock_est"], 2500)

    def test_counts_of_other_brands_later_that_day_add_up_and_a_brand_counted_again_is_replaced(self):
        self.said("I have 4 bottles of Lux hand wash, 400g each",
                  self.count("hand-wash", "4 bottles of Lux hand wash, 400g each", "Lux"), update_id=5)
        lines, _, _ = self.said("I have 1 bottle Dettol hand wash 250g",
                                self.count("hand-wash", "1 bottle Dettol hand wash 250g", "Dettol"), update_id=6)
        self.assertEqual(lines, ["✓ Hand wash: counted 1 pack of 250 g in stock, 1.85 kg in all."])
        self.said("Lux hand wash 2 bottles 400g", self.count("hand-wash", "2 bottles 400g", "Lux"), update_id=7)
        self.assertEqual(store.get_item("hand-wash")["stock_est"], 1050)

    def test_another_few_add_to_what_was_counted(self):
        self.said("I have 4 bottles of Lux hand wash, 400g each",
                  self.count("hand-wash", "4 bottles of Lux hand wash, 400g each", "Lux"), update_id=5)
        lines, _, _ = self.said("I have another 3 bottles of Lux hand wash, 400g each",
                                self.count("hand-wash", "3 bottles of Lux hand wash, 400g each", "Lux", adds=True), update_id=6)
        self.assertEqual(lines, ["✓ Hand wash: added 3 packs × 400 g (1.2 kg) to the stock, 2.8 kg in all."])
        self.assertTrue(self.events("hand-wash")[-1]["adds"])
        self.assertNotIn("adds", self.events("hand-wash")[0])

    def test_a_count_whose_quantity_cannot_be_read_is_not_recorded(self):
        self.said("4 bottles of Lux hand wash, 400g each", self.count("hand-wash", "4 bottles of Lux hand wash, 400g each", "Lux"))
        lines, counts, _ = self.said("Hand wash: 4 bottles and 2 refills of Lux",
                                     self.count("hand-wash", "4 bottles and 2 refills", "Lux"), update_id=2)
        self.assertEqual(lines, ["Couldn't read how much Hand wash that counts, so it isn't recorded. "
                                 "Resend like: have lux hand wash 2x5kg"])
        self.assertEqual(counts, {"lines_rejected": 1})
        self.assertEqual(store.get_item("hand-wash")["stock_est"], 1600)

    def test_a_purchase_whose_quantity_cannot_be_read_is_still_one_pack(self):
        self.said("4 bottles of Lux hand wash, 400g each", self.count("hand-wash", "4 bottles of Lux hand wash, 400g each", "Lux"))
        lines, _, _ = self.said("got Lux hand wash 4 bottles and 2 refills $9",
                                dict(action="bought", item_id="hand-wash", item_name="hand wash", base_unit="g",
                                     quantity_text="4 bottles and 2 refills", total_price=9, brand="Lux"), update_id=2)
        self.assertTrue(lines[0].startswith("✓ Hand wash: bought 1 pack of 400 g for $9.00"), lines[0])
        self.assertTrue(lines[0].endswith("(Didn't catch the quantity.)"), lines[0])


class StoreWrittenLastTest(ApplyTestCase):
    def test_a_store_written_last_is_where_it_was_bought_not_part_of_the_item(self):
        self.add_item("body-wash", "Body wash", "ml")
        lines, counts, buttons = self.send("/add body wash 3*400g $21.85 taobao")
        self.assertEqual(lines, ["✓ Body wash: bought 3 packs × 400 ml (1.2 L) for $21.85 at Taobao ($1.82/100 ml). "
                                 "First price recorded."])
        self.assertEqual((counts, buttons), ({"lines_structured": 1}, []))
        self.assertEqual(self.events("body-wash")[0]["store"], "Taobao")
        self.assertEqual(store.get_item("body-wash")["aliases"], [])
        self.match_item.assert_not_called()



class ExpiryApplyTest(ApplyTestCase):
    """An expiry date on a purchase or a count is stored, and the reply says it."""

    def setUp(self):
        super().setUp()
        self.add_toilet_paper(pack_size=10, pack_size_set_at=AT)

    def test_a_count_with_an_expiry(self):
        self.assertEqual(self.lines("have toilet paper 1x10 exp 12/27"),
                         ["✓ Toilet paper: counted 1 pack of 10 rolls in stock, expires 1 Dec 2027."])
        self.assertEqual(self.events("toilet-paper")[0]["expires_on"], "2027-12-01")

    def test_a_purchase_already_past_its_date_is_still_recorded_and_said_so(self):
        line, = self.lines("+ toilet paper 1x10 $5 exp 1/9/2026")
        self.assertIn("($0.50/roll), expired 1 Sep 2026.", line)
        self.assertEqual(self.events("toilet-paper")[0]["expires_on"], "2026-09-01")

    def test_a_date_with_no_year_goes_by_the_day_it_happened(self):
        self.send("have toilet paper 1x10 exp 5/1 on 15/09/2026")
        self.assertEqual(self.events("toilet-paper")[0]["expires_on"], "2027-01-05")

    def test_an_expiry_that_is_not_a_date_records_nothing(self):
        lines, counts, _ = self.send("+ toilet paper 1x10 exp 31/02/2027")
        self.assertEqual((lines, counts), ([replies.EXPIRY_HINT], {"lines_rejected": 1}))
        self.assertEqual(self.events("toilet-paper"), [])

    def test_only_purchases_and_counts_keep_one(self):
        self.send("have toilet paper 2x10")
        self.send("- toilet paper 1 exp 12/27", update_id=2)
        self.assertNotIn("expires_on", self.events("toilet-paper")[1])

    def test_free_text_with_an_expiry_that_cannot_be_read_is_still_recorded(self):
        lines, _, _ = self.said("we have 2 packs of toilet paper, expires 31/02/2027",
                                dict(action="stock_count", item_id="toilet-paper", item_name="toilet paper",
                                     quantity_text="2 packs", expiry_text="expires 31/02/2027"))
        self.assertEqual(lines, ["✓ Toilet paper: counted 2 packs × 10 rolls (20 rolls) in stock. (Didn't catch the expiry.)"])
        self.assertNotIn("expires_on", self.events("toilet-paper")[0])



class DescriptionApplyTest(ApplyTestCase):
    def setUp(self):
        super().setUp()
        self.add_item("body-wash", "Body wash", "ml", brands=["半亩花田"])

    def test_a_description_is_stored_and_the_reply_shows_it(self):
        lines = self.lines("have 半亩花田 body wash 1x400ml (pink) exp 09/2028",
                           brands=["半亩花田"])
        self.assertEqual(lines, ["✓ Body wash: counted 1 pack of 400 ml in stock: 半亩花田 pink, expires 1 Sep 2028."])
        event, = self.events("body-wash")
        self.assertEqual((event["brand"], event["description"]), ("半亩花田", "pink"))

    def test_from_free_text(self):
        self.said("1 bottle green tea fragrance body wash 475 ml expire Nov 2028",
                  dict(action="stock_count", item_id="body-wash", item_name="body wash", base_unit="ml",
                       quantity_text="1 bottle green tea fragrance body wash 475 ml", description="green tea fragrance"))
        self.assertEqual(self.events("body-wash")[0]["description"], "green tea fragrance")

    def test_only_on_what_is_at_home(self):
        self.send("have body wash 2x400ml")
        self.send("- body wash 1 (pink)", update_id=2)
        self.assertNotIn("description", self.events("body-wash")[1])



class FoundApplyTest(ApplyTestCase):
    """Something found at home is added with /add, on any day, without counting everything again."""

    def test_add_with_no_price_adds_to_yesterdays_stocktake(self):
        self.add_item("hand-wash", "Hand wash", "g", brands=["Lux"])
        self.send("have lux hand wash 4x400g exp 07/2028\nhave lux hand wash 1x400g exp 03/2028", brands=["Lux"])
        lines = self.lines("/add lux hand wash 3x400g (white tea) exp 07/2029", update_id=2, at="2026-10-10T10:00:00Z",
                           brands=["Lux"])
        self.assertEqual(lines, ["✓ Hand wash: bought 3 packs × 400 g (1.2 kg): Lux white tea, expires 1 Jul 2029."])
        # A plain count on a later day would have replaced it all.
        self.assertEqual(store.get_item("hand-wash")["stock_est"], 3200)
        self.assertNotIn("unit_price", self.events("hand-wash")[-1])

    def test_bought_needs_its_price(self):
        self.add_toilet_paper(pack_size=10, pack_size_set_at=AT)
        self.assertEqual(structured.parse_line("/bought toilet paper 2x10"), structured.NO_PRICE)
        self.assertEqual(self.lines("/bought toilet paper 2x10 $5"),
                         ["✓ Toilet paper: bought 2 packs × 10 rolls (20 rolls) for $5.00 ($0.25/roll). First price recorded."])
        # The words, unlike the command, are as often chat as a log, so they keep working without one.
        self.assertEqual(structured.parse_line("bought toilet paper 2x10")["action"], "bought")



class OneBrandApplyTest(ApplyTestCase):
    def test_the_reply_says_which_brand_and_what_is_left(self):
        self.add_item("hand-wash", "Hand wash", "g", brands=["Lux", "Aesop"], pack_size=400, pack_size_set_at=AT)
        self.send("have lux hand wash 4x400g\n+ aesop hand wash 500g", brands=["Lux", "Aesop"])
        self.assertEqual(self.lines("- aesop hand wash", update_id=2, brands=["Lux", "Aesop"]),
                         ["✓ Hand wash: Aesop marked finished, 1.6 kg left."])
        self.assertEqual(store.get_item("hand-wash")["status"], "ok")



class UsedOnePackTest(ApplyTestCase):
    """"/used hand wash" is the next bottle to be used, at its own size (3 Nov 2026)."""

    def setUp(self):
        super().setUp()
        self.add_item("hand-wash", "Hand wash", "g", brands=["Lux", "Aesop"])
        # Written the way it was on 3 Nov 2026: a bottle given only by its size.
        self.send("have lux hand wash 4x400g exp 07/2028\n+ aesop hand wash 500g", brands=["Lux", "Aesop"])

    def test_the_brand_named_is_its_own_bottle(self):
        self.assertEqual(self.lines("/used aesop hand wash", update_id=2, brands=["Lux", "Aesop"]),
                         ["✓ Hand wash: used 1 pack of 500 g: Aesop, 1.6 kg (4 packs) left."])
        self.assertEqual(model.brands_left(self.events("hand-wash"), 400), [("Lux", 1600)])

    def test_with_no_brand_it_is_the_one_to_use_next(self):
        self.lines("/used hand wash", update_id=2, brands=["Lux", "Aesop"])
        # The Lux bottles expire first, so one of them went.
        self.assertEqual(model.brands_left(self.events("hand-wash"), 400), [("Lux", 1200), ("Aesop", 500)])

    def test_packs_of_different_sizes_are_given_as_a_total(self):
        self.send("- hand wash 1", update_id=2, brands=["Lux", "Aesop"])
        self.send("- hand wash 1", update_id=3, brands=["Lux", "Aesop"])
        self.send("- hand wash 1", update_id=4, brands=["Lux", "Aesop"])
        line, = self.lines("/used hand wash 2", update_id=5, brands=["Lux", "Aesop"])
        self.assertEqual(line, "✓ Hand wash: used 900 g, none left.")

    def test_with_nothing_known_it_asks_for_the_size(self):
        self.add_item("dish-soap", "Dish soap", "ml")
        line, = self.lines("/used dish soap", update_id=2)
        self.assertTrue(line.startswith("How much is in each pack of Dish soap?"), line)


if __name__ == "__main__":
    unittest.main()
