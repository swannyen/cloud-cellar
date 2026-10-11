"""AI item match tests (PLAN.md §5.3) and the check that guards the free-text fallback (§5.4).

The Claude call is faked; nothing is sent. The fallback's prompt and
normalisation are tested in test_ai_normalise.py.
"""
import unittest
from unittest import mock

import fakes  # noqa: F401  (puts src/ on sys.path)

import ai
import claude_api

ITEMS = [
    {"item_id": "toilet-paper", "name": "Toilet paper", "base_unit": "roll", "aliases": ["loo roll", "TP"]},
    {"item_id": "dish-soap", "name": "Dishwashing liquid", "base_unit": "ml", "aliases": ["dish soap"]},
]
KNOWN_IDS = {"toilet-paper", "dish-soap"}


def answer(item_id=None, new_item_name="", base_unit="piece"):
    return {"item_id": item_id, "new_item_name": new_item_name, "base_unit": base_unit}


class ItemMatchRequestTest(unittest.TestCase):
    def setUp(self):
        message = {"usage": {"input_tokens": 210, "output_tokens": 18}}
        patch = mock.patch.object(
            claude_api, "complete_json", return_value=(answer("toilet-paper", "Toilet paper", "roll"), message)
        )
        self.complete_json = patch.start()
        self.addCleanup(patch.stop)

    def test_sends_only_the_phrase_and_item_names_with_a_small_token_limit(self):
        match = ai.match_item("bog roll", ITEMS)
        system, user, schema = self.complete_json.call_args.args
        self.assertEqual(system, ai.ITEM_MATCH_PROMPT)
        self.assertEqual(user, "\n".join([
            "Items (id | name):",
            "toilet-paper | Toilet paper",
            "dish-soap | Dishwashing liquid",
            "",
            "Phrase: bog roll",
        ]))
        self.assertEqual(schema, ai.ITEM_MATCH_SCHEMA)
        self.assertEqual(self.complete_json.call_args.kwargs, {"max_tokens": 100})
        self.assertEqual((match.item_id, match.usage["input_tokens"]), ("toilet-paper", 210))

    def test_empty_item_list_is_said_plainly(self):
        self.assertIn("(none yet)", ai.item_match_content("rice", []))

    def test_schema_requires_every_field(self):
        schema = ai.ITEM_MATCH_SCHEMA
        self.assertEqual(set(schema["required"]), set(schema["properties"]))
        self.assertFalse(schema["additionalProperties"])


class ItemMatchNormaliseTest(unittest.TestCase):
    def read(self, data):
        return ai.normalise_item_match(data, KNOWN_IDS)

    def test_known_item(self):
        match = self.read(answer("toilet-paper", "Toilet paper", "roll"))
        self.assertEqual((match.item_id, match.name, match.base_unit), ("toilet-paper", "Toilet paper", "roll"))

    def test_new_item(self):
        match = self.read(answer(None, " Soy sauce ", "ml"))
        self.assertEqual((match.item_id, match.name, match.base_unit), (None, "Soy sauce", "ml"))

    def test_id_that_is_not_tracked_counts_as_a_new_item(self):
        match = self.read(answer("soy-sauce", "Soy sauce", "ml"))
        self.assertEqual((match.item_id, match.name), (None, "Soy sauce"))

    def test_empty_name_means_not_a_household_product(self):
        match = self.read(answer(None, ""))
        self.assertEqual((match.item_id, match.name), (None, ""))

    def test_capitalisation_is_forgiven(self):
        match = self.read(answer("Toilet-Paper", "Toilet paper", "ROLL"))
        self.assertEqual((match.item_id, match.base_unit), ("toilet-paper", "roll"))

    def test_bad_values_fall_back_safely(self):
        match = self.read({"item_id": 7, "new_item_name": None, "base_unit": "bottle"})
        self.assertEqual((match.item_id, match.name, match.base_unit), (None, "", "piece"))
        self.assertEqual(self.read("not a dict").name, "")
        self.assertLessEqual(len(self.read(answer(None, "x" * 200)).name), ai.MAX_NAME_LENGTH)


def event(**fields):
    """An event shaped like ai.normalise_free_text() output."""
    base = {"action": "bought", "item_id": "face-masks", "item_name": "face masks", "base_unit": "piece",
            "quantity_text": None, "total_price": None, "store": None, "brand": None}
    base.update(fields)
    return base


def ground(text, stores=(), units=None, **fields):
    entry, = ai.ground_free_text([event(**fields)], text, stores, units)
    return entry


class GroundingTest(unittest.TestCase):
    """Only what is written in the message survives (PLAN.md §5.4)."""

    def test_entry_has_what_apply_needs(self):
        entry = ground("Facial masks 5 pcs 1.86 from Taobao", item_id=None, item_name="Facial masks",
                       quantity_text="5 pcs", total_price=1.86, store="Taobao")
        self.assertEqual(entry, {
            "action": "bought", "item_id": None, "phrase": "Facial masks", "base_unit": "piece",
            "quantity": {"kind": "amount", "amount": 5, "unit": "piece"}, "total_price": 1.86,
            "store": "Taobao", "store_is_new": False, "brand": None, "missed": (),
            "raw": "Facial masks 5 pcs 1.86 from Taobao", "free_text": True,
        })

    def test_store_read_by_code_beats_the_models_answer(self):
        entry = ground("HAND masks 10 pcs $1.45 from Taobao", store="Shopee")
        self.assertEqual((entry["store"], entry["missed"]), ("Taobao", ()))
        entry = ground("masks 10 pcs @Qoo10", store="Shopee")
        self.assertEqual((entry["store"], entry["store_is_new"]), ("Qoo10", True))

    def test_models_store_is_kept_only_if_it_is_written_in_the_message(self):
        self.assertEqual(ground("got masks at giant today", store="Giant")["store"], "Giant")
        self.assertEqual(ground("got masks from Don Don Donki", store="don don donki")["store"], "Don Don Donki")
        entry = ground("got masks yesterday", store="Shopee")
        self.assertEqual((entry["store"], entry["missed"]), (None, ("store",)))

    def test_a_known_spelling_in_the_message_counts(self):
        self.assertEqual(ground("masks from ntuc lah", store="FairPrice")["store"], "FairPrice")
        self.assertEqual(ground("bought masks fairprice", store="fairprice")["store"], "FairPrice")
        saved = [{"store_id": "donki", "name": "Don Don Donki", "aliases": ["donki"]}]
        self.assertEqual(ground("masks, donki run", saved, store="Don Don Donki")["store"], "Don Don Donki")

    def test_a_short_spelling_inside_another_word_does_not_count(self):
        # "ss" is Sheng Siong, but not when it is the middle of "tissues".
        entry = ground("bought tissues", store="Sheng Siong")
        self.assertEqual((entry["store"], entry["missed"]), (None, ("store",)))

    def test_with_several_stores_written_the_model_may_only_pick_one_of_them(self):
        text = "rice from ntuc\nmasks from taobao"
        self.assertEqual(ground(text, store="Taobao")["store"], "Taobao")
        self.assertEqual(ground(text, store="NTUC")["store"], "FairPrice")
        self.assertEqual(ground(text, store="Shopee")["missed"], ("store",))
        self.assertIsNone(ground(text, store=None)["store"])

    def test_price_must_be_written_in_the_message(self):
        self.assertEqual(ground("masks 10 pcs $1.45", total_price=1.45)["total_price"], 1.45)
        self.assertEqual(ground("masks for 12.9", total_price=12.90)["total_price"], 12.9)
        entry = ground("2 boxes of masks at $3.20 each", total_price=6.4)
        self.assertEqual((entry["total_price"], entry["missed"]), (None, ("price",)))

    def test_one_price_put_on_several_items_backs_none_of_them(self):
        shared = [event(item_id="eggs", total_price=31.4), event(item_id="milk", total_price=31.4)]
        entries = ai.ground_free_text(shared, "NTUC run: eggs and milk. $31.40")
        self.assertEqual([(e["total_price"], e["missed"]) for e in entries], [(None, ("price",)), (None, ("price",))])
        # Written twice, it can back two.
        entries = ai.ground_free_text(shared, "eggs $31.40, milk $31.40")
        self.assertEqual([e["total_price"] for e in entries], [31.4, 31.4])

    def test_a_total_for_several_items_is_kept_off_all_of_them(self):
        # The model put the whole bill on the first item. Once is "written once", so only this rule stops it.
        events = [event(item_id="eggs", total_price=31.4), event(item_id="milk"), event(item_id="rice")]
        entries = ai.ground_free_text(events, "NTUC run: eggs, milk and rice. $31.40 total")
        # Not "missed": the reply says once that the total covers several items.
        self.assertEqual([(e["total_price"], e["missed"]) for e in entries], [(None, ())] * 3)

    def test_a_price_of_its_own_stays_when_the_message_also_has_a_total(self):
        events = [event(item_id="rice", total_price=12), event(item_id="eggs", total_price=5)]
        entries = ai.ground_free_text(events, "rice $12, eggs $5, total $17")
        self.assertEqual([e["total_price"] for e in entries], [12, 5])
        entries = ai.ground_free_text(events, "bought rice $12 and eggs $5")
        self.assertEqual([e["total_price"] for e in entries], [12, 5])

    def test_with_one_purchase_a_total_is_simply_its_price(self):
        entry = ground("rice, 2 bags, $24 total", total_price=24)
        self.assertEqual((entry["total_price"], entry["missed"]), (24, ()))
        # Something that ran out in the same message isn't a second purchase.
        bought, _ = ai.ground_free_text([event(total_price=24), event(action="finished", item_id="rice")],
                                        "masks $24 in total, and the rice finished")
        self.assertEqual(bought["total_price"], 24)

    def test_quantity_must_be_written_word_for_word(self):
        self.assertEqual(ground("masks, 10  PCS today", quantity_text="10 pcs")["quantity"],
                         {"kind": "amount", "amount": 10, "unit": "piece"})
        entry = ground("masks, ten pieces", quantity_text="10 pcs")
        self.assertEqual((entry["quantity"], entry["missed"]), (None, ("quantity",)))

    def test_quantity_words_may_skip_what_lies_between_them_but_not_add_any(self):
        text = "kitchen towel on offer at fairprice, 2 packs for $9.90 (usual 6.50 each), 6 rolls per pack. bought 2"
        self.assertEqual(ground(text, quantity_text="2 packs, 6 rolls per pack")["quantity"],
                         {"kind": "packs_of", "packs": 2, "size": 6, "unit": "roll"})
        self.assertEqual(ground("got a pack of sponges from Daiso, 5 pieces $2.14",
                                quantity_text="a pack of sponges, 5 pieces")["quantity"],
                         {"kind": "amount", "amount": 5, "unit": "piece"})
        # Every word has to be there, and in the order it was written.
        self.assertEqual(ground(text, quantity_text="3 packs, 6 rolls per pack")["missed"], ("quantity",))
        self.assertEqual(ground(text, quantity_text="6 rolls per pack, 2 packs for")["missed"], ("quantity",))

    def test_quantity_is_read_by_the_same_rules_as_the_format(self):
        text = "bought 2 packs toilet paper 10 rolls each, also 2 sticks of masks"
        self.assertEqual(ground(text, quantity_text="2 packs toilet paper 10 rolls each")["quantity"],
                         {"kind": "packs_of", "packs": 2, "size": 10, "unit": "roll"})
        # A number that doesn't say what it counts is marked, for the caller to ask about.
        self.assertEqual(ground(text, quantity_text="2 sticks")["quantity"], {"kind": "packs", "packs": 2, "bare": True})
        self.assertEqual(ground("got 3 boxes of masks", quantity_text="3 boxes")["quantity"], {"kind": "packs", "packs": 3})
        self.assertEqual(ground(text, units={"sticks": "pcs"}, quantity_text="2 sticks")["quantity"],
                         {"kind": "amount", "amount": 2, "unit": "piece"})

    def test_written_quantity_that_cannot_be_read_is_said_too(self):
        entry = ground("bought a few masks", quantity_text="a few")
        self.assertEqual((entry["quantity"], entry["missed"]), (None, ("quantity",)))

    def test_brand_that_is_not_written_is_dropped_quietly(self):
        self.assertEqual(ground("1L Mama Lemon", brand="Mama Lemon")["brand"], "Mama Lemon")
        entry = ground("1L dish soap", brand="Mama Lemon")
        self.assertEqual((entry["brand"], entry["missed"]), (None, ()))

    def test_price_check_without_a_grounded_price_is_dropped(self):
        self.assertEqual(ai.ground_free_text([event(action="price_check", total_price=5)], "is masks worth it?"), [])
        kept, = ai.ground_free_text([event(action="price_check", total_price=5, quantity_text="10 pcs")],
                                    "is $5 for 10 pcs of masks ok?")
        self.assertEqual((kept["total_price"], kept["quantity"]["amount"]), (5, 10))

    def test_used_without_a_grounded_quantity_is_dropped(self):
        self.assertEqual(ai.ground_free_text([event(action="used")], "used some masks"), [])
        self.assertEqual(ai.ground_free_text([event(action="used", quantity_text="5 pcs")], "used a few masks"), [])
        kept, = ai.ground_free_text([event(action="used", quantity_text="5 pcs")], "used 5 pcs of masks")
        self.assertEqual((kept["action"], kept["quantity"]["amount"]), ("used", 5))

    def test_several_events_are_each_checked(self):
        entries = ai.ground_free_text(
            [event(store="Giant", total_price=3), event(item_id=None, item_name="Rice", store="Shopee", total_price=99)],
            "masks $3 and rice from Giant",
        )
        self.assertEqual([(e["store"], e["total_price"], e["missed"]) for e in entries],
                         [("Giant", 3, ()), (None, None, ("store", "price"))])


def bought(count):
    return [event() for _ in range(count)]


class SharedTotalTest(unittest.TestCase):
    """A price next to "total", "altogether" and the like, in a message with several purchases."""

    def test_totals_are_found_before_or_after_the_word(self):
        cases = {
            "NTUC run: eggs, milk and rice. $31.40 total": [31.4],
            "Sheng Siong trip: bread and peanut butter, $7.80 altogether": [7.8],
            "eggs and milk, 31.40 for everything": [31.4],
            "shampoo and conditioner $15 for both": [15],
            "coffee and milk $12 in all": [12],
            "Total: $31.40 (eggs, milk)": [31.4],
            "total came up to $31.40": [31.4],
            "paid a total of $31.40 for eggs and milk": [31.4],
            "eggs $3.10\nmilk $4\nTotal 7.10": [7.1],
            "鸡蛋和牛奶一共31.40": [31.4],
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(ai.shared_totals(bought(2), text), expected)

    def test_the_word_goes_with_one_number_only(self):
        cases = {
            "rice $12, eggs $5, total $17": [17],      # not the $5 just before the comma
            "rice $12, eggs $5, total 17": [17],
            "rice $12 eggs $5 total $17": [17],
            "$9.90 total 2 packs of each": [9.9],      # the one written as a price
            "$31.40 in total for 3 items": [31.4],
            "2 packs each, total $9.90": [9.9],
            "eggs $3 and milk $4. Total was $7.": [7],
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(ai.shared_totals(bought(2), text), expected)

    def test_messages_without_a_shared_total(self):
        for text in ("bought rice $12 and eggs", "rice $12, eggs $5", "totally out of rice and eggs, $5 each",
                     "eggs and milk. The total for this very long trip today was $40"):
            with self.subTest(text=text):
                self.assertEqual(ai.shared_totals(bought(2), text), [])

    def test_it_takes_two_purchases_or_price_checks_to_share_a_total(self):
        text = "$31.40 total"
        self.assertEqual(ai.shared_totals(bought(1), text), [])
        self.assertEqual(ai.shared_totals([event(), event(action="finished")], text), [])
        self.assertEqual(ai.shared_totals([event(), event(action="price_check")], text), [31.4])
        self.assertEqual(ai.shared_totals([], text), [])


class LooksLikeLogTest(unittest.TestCase):
    def test_lines_worth_a_fallback_call(self):
        cases = {
            "got 2 packs from ntuc": "a number",
            "paid $5 for it": "a price",
            "shampoo finish already": "a word like finished",
            "we ran out again": "a word like finished",
            "Bought more of the usual": "a word like finished",
            "the loo roll is almost gone": "a tracked item's alias",
            "need more dishwashing liquid": "a tracked item's name",
        }
        for line, why in cases.items():
            with self.subTest(line=line, why=why):
                self.assertTrue(ai.looks_like_log(line, ITEMS))

    def test_ordinary_chat_is_not(self):
        for line in ("ok thanks", "what time is dinner?", "haha nice", "see you later", "unfinished business"):
            with self.subTest(line=line):
                self.assertFalse(ai.looks_like_log(line, ITEMS))


if __name__ == "__main__":
    unittest.main()
