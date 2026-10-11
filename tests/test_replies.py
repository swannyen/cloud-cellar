"""Reply formatting tests."""
import unittest

import fakes  # noqa: F401  (puts src/ on sys.path)

import replies
import structured

TP = {"item_id": "toilet-paper", "name": "Toilet paper", "base_unit": "roll"}
RICE = {"item_id": "rice", "name": "Rice", "base_unit": "g"}
SOAP = {"item_id": "dish-soap", "name": "Dishwashing liquid", "base_unit": "ml"}
MASKS = {"item_id": "face-masks", "name": "Face masks", "base_unit": "piece"}
FACIALS = {"item_id": "facial-masks", "name": "Facial masks", "base_unit": "piece"}  # skincare: not Face masks


def ev(action="bought", **fields):
    event = {"action": action, "qty": None, "packs": None, "units_per_pack": None, "total_price": None,
             "unit_price": None, "store": None, "brand": None}
    event.update(fields)
    return event


class EventLineTest(unittest.TestCase):
    def test_the_plan_examples(self):
        self.assertEqual(
            replies.event_line(MASKS, ev(qty=2, total_price=1.96, unit_price=0.98, store="Taobao")),
            "✓ Face masks: bought 2 pieces for $1.96 at Taobao ($0.98/piece).",
        )
        event = ev(qty=20, packs=2, units_per_pack=10, total_price=12.9, unit_price=0.645, store="FairPrice")
        self.assertEqual(
            replies.event_line(TP, event),
            "✓ Toilet paper: bought 2 packs × 10 rolls (20 rolls) for $12.90 at FairPrice ($0.65/roll).",
        )

    def test_every_amount_states_the_total_in_the_base_unit(self):
        cases = [
            (TP, ev(qty=10), "bought 10 rolls"),
            (TP, ev(qty=1), "bought 1 roll"),
            (TP, ev(qty=10, packs=1), "bought 1 pack of 10 rolls"),
            (TP, ev(qty=30, packs=3), "bought 3 packs × 10 rolls (30 rolls)"),
            (RICE, ev(qty=5000), "bought 5 kg"),
            (RICE, ev(qty=10000, packs=2, units_per_pack=5000), "bought 2 packs × 5 kg (10 kg)"),
            (SOAP, ev(qty=750), "bought 750 ml"),
            (SOAP, ev(qty=250, packs=0.5), "bought 0.5 packs × 500 ml (250 ml)"),
        ]
        for item, event, expected in cases:
            with self.subTest(expected=expected):
                self.assertEqual(replies.summary(item, event), expected)

    def test_price_per_100g_for_weights(self):
        event = ev(qty=5000, total_price=13.5, unit_price=0.0027, store="Sheng Siong")
        self.assertEqual(replies.event_line(RICE, event), "✓ Rice: bought 5 kg for $13.50 at Sheng Siong ($0.27/100 g).")

    def test_new_item_is_flagged(self):
        self.assertEqual(replies.event_line(TP, ev(qty=10), created=True), "✓ Toilet paper (new item): bought 10 rolls.")

    def test_guessed_match_is_said_plainly(self):
        self.assertEqual(replies.event_line(MASKS, ev(qty=10), read_from="hand masks"),
                         '✓ Face masks: bought 10 pieces. (Read "hand masks" as Face masks.)')

    def test_facts_the_ai_gave_that_were_not_in_the_message_are_named(self):
        self.assertEqual(replies.event_line(MASKS, ev(qty=10), missed=("store",)),
                         "✓ Face masks: bought 10 pieces. (Didn't catch the store.)")
        self.assertEqual(replies.event_line(MASKS, ev(qty=10), read_from="masks", missed=("store", "price")),
                         '✓ Face masks: bought 10 pieces. (Read "masks" as Face masks.) (Didn\'t catch the store or price.)')

    def test_status_events(self):
        self.assertEqual(replies.event_line(SOAP, ev("finished")), "✓ Dishwashing liquid: marked finished.")
        self.assertEqual(replies.event_line(TP, ev("opened_last")), "✓ Toilet paper: marked as running low.")
        self.assertEqual(replies.event_line(TP, ev("still_have")), "✓ Toilet paper: noted, still have plenty.")
        self.assertEqual(replies.event_line(TP, ev("stock_count", qty=30, packs=3)),
                         "✓ Toilet paper: counted 3 packs × 10 rolls (30 rolls) in stock.")
        self.assertEqual(replies.event_line(RICE, ev("stock_count", qty=2500)), "✓ Rice: counted 2.5 kg in stock.")

    def test_price_check_has_no_tick(self):
        event = ev("price_check", qty=3000, total_price=8.95, unit_price=8.95 / 3000, store="Giant")
        self.assertEqual(replies.event_line(RICE, event), "Rice: $8.95 for 3 kg = $0.30/100 g at Giant. Price noted.")

    def test_events_from_before_quantities_were_stored_still_read_well(self):
        old = {"action": "bought", "packs": 2, "units_per_pack": 10, "total_price": 12.9, "unit_price": 0.645}
        self.assertEqual(replies.summary(TP, old), "bought 2 packs × 10 rolls (20 rolls) for $12.90 ($0.65/roll)")
        self.assertEqual(replies.summary(TP, {"action": "bought", "packs": 2}), "bought 2 packs")
        self.assertEqual(replies.summary(dict(TP, pack_size=10), {"action": "bought", "packs": 2}),
                         "bought 2 packs × 10 rolls (20 rolls)")
        self.assertEqual(replies.summary(TP, {"action": "bought"}), "bought")

    def test_undo_confirmation_says_what_was_removed(self):
        self.assertEqual(replies.undone("Face masks", "bought 10 pieces"), "Undid: Face masks, bought 10 pieces.")


class QuestionTest(unittest.TestCase):
    def entry(self, line):
        return structured.parse_line(line)

    def test_asking_for_the_pack_size_of_something_counted(self):
        self.assertEqual(
            replies.ask_pack_size(MASKS, self.entry("+ face masks 2 $1.96")),
            "How many in each pack of Face masks? Resend like: + face masks 2x5pcs $1.96 — "
            "or write 2pcs if you mean single pieces.",
        )
        self.assertIn("Resend like: have toilet paper 3x10rolls — or write 3rolls if you mean single rolls.",
                      replies.ask_pack_size(TP, self.entry("have Toilet Paper 3")))

    def test_asking_for_the_pack_size_of_something_weighed_or_poured(self):
        self.assertEqual(
            replies.ask_pack_size(RICE, self.entry("+ rice $12")),
            "How much is in each pack of Rice? Resend like: + rice 1x5kg $12.00 — or write the total, like 5kg.",
        )
        self.assertIn("? dish soap 2x1L $5.00", replies.ask_pack_size(SOAP, self.entry("? dish soap 2 btl $5")))

    def test_the_example_keeps_the_store_so_a_resend_does_not_lose_it(self):
        text = replies.ask_pack_size(FACIALS, self.entry("bought 2 facial masks for 1.96 from Taobao"))
        self.assertIn("Resend like: + facial masks 2x5pcs $1.96 @Taobao — ", text)
        resent = structured.parse_line("+ facial masks 2x5pcs $1.96 @Taobao")
        self.assertEqual((resent["store"], resent["total_price"]), ("Taobao", 1.96))

    def test_the_example_keeps_the_brand(self):
        entry = dict(self.entry("bought 2 facial masks for 1.96 from Taobao"), brand="Kans")
        self.assertIn("Resend like: + kans facial masks 2x5pcs $1.96 @Taobao — ", replies.ask_pack_size(FACIALS, entry))

    def test_the_question_says_which_item_a_guess_landed_on(self):
        text = replies.ask_pack_size(MASKS, self.entry("+ hand masks 2"), read_from="hand masks")
        self.assertTrue(text.endswith('(Read "hand masks" as Face masks.)'))

    def test_wrong_unit(self):
        self.assertEqual(replies.wrong_unit(MASKS), "Face masks are counted in pieces. Try 2pcs or 2x5pcs.")
        self.assertEqual(replies.wrong_unit(RICE), "Rice is counted in grams. Try 2kg or 2x5kg.")
        self.assertEqual(replies.wrong_unit(SOAP), "Dishwashing liquid is counted in millilitres. Try 2L or 2x1L.")


class FormattingTest(unittest.TestCase):
    def test_quantity(self):
        cases = [
            ((3000, "g"), "3 kg"), ((3600, "g"), "3.6 kg"), ((500, "g"), "500 g"),
            ((2000, "ml"), "2 L"), ((750, "ml"), "750 ml"),
            ((10, "roll"), "10 rolls"), ((1, "roll"), "1 roll"),
            ((30, "piece"), "30 pieces"), ((200, "sheet"), "200 sheets"),
        ]
        for args, expected in cases:
            with self.subTest(args=args):
                self.assertEqual(replies.quantity(*args), expected)

    def test_unit_price(self):
        self.assertEqual(replies.unit_price(0.645, "roll"), "$0.65/roll")
        self.assertEqual(replies.unit_price(0.004, "sheet"), "$0.004/sheet")
        self.assertEqual(replies.unit_price(0.0031, "g"), "$0.31/100 g")
        self.assertEqual(replies.unit_price(0.00295, "ml"), "$0.29/100 ml")


class FixedTextTest(unittest.TestCase):
    def test_help_has_an_example_for_every_action_and_lists_the_commands(self):
        for example in ("+ toilet paper 2x10 $12.90 @fairprice", "- dish soap", "low shampoo", "have rice 2x5kg",
                        "? detergent 3kg $8.95 @shengsiong"):
            self.assertIn(example, replies.HELP)
        for command in ("/items", "/undo", "/alias", "/set", "/unit", "/brand", "/listbrands", "/stats", "/add", "/bought", "/used"):
            self.assertIn(command, replies.HELP)
        for rule in ("2pcs", "2x10rolls", "2pkt", "I ask instead of guessing"):
            self.assertIn(rule, replies.HELP)

    def test_every_example_in_the_help_and_the_hint_really_parses(self):
        examples = [line.split(" — ")[0] for line in replies.HELP.split("\n\n")[0].splitlines() if " — " in line]
        examples.append(replies.FORMAT_HINT.split("Try: ")[1].split(" — ")[0])
        self.assertEqual(len(examples), 6)
        for example in examples:
            with self.subTest(example=example):
                self.assertIsInstance(structured.parse_line(example), dict)

    def test_usage_examples_are_real_commands(self):
        self.assertIn("/alias loo roll = toilet paper", replies.ALIAS_USAGE)
        self.assertIn("/set toilet paper pack 10", replies.SET_USAGE)
        self.assertIn("/unit sticks = pcs", replies.UNIT_USAGE)
        self.assertIn("/brand kans", replies.BRAND_USAGE)

    def test_format_hint_matches_the_plan(self):
        self.assertEqual(replies.FORMAT_HINT, "Didn't catch that. Try: + rice 5kg $12 @ntuc — or /help for all formats.")

    def test_brands_list(self):
        self.assertEqual(replies.brands_list([]), replies.NO_BRANDS)
        brands = [("kans", ["Sleeping masks", "Facial masks"]), ("Darlie", []), ("半亩花田", ["Body wash"])]
        self.assertEqual(replies.brands_list(brands), "\n".join([
            "Brands:",
            "• Darlie — not on any entry yet",
            "• kans — Facial masks, Sleeping masks",
            "• 半亩花田 — Body wash",
        ]))

    def test_entry_command_usage_examples_really_parse(self):
        add, used = replies.ENTRY_COMMAND_USAGE.removeprefix("Use it like this: ").split(" — or ")
        self.assertEqual(structured.parse_line(add)["action"], "bought")
        self.assertEqual(structured.parse_line(used)["action"], "finished")

    def test_toasts(self):
        self.assertEqual(replies.alias_toast("hand masks", MASKS), "Saved: 'hand masks' = Face masks")
        self.assertEqual(replies.new_item_toast({"name": "Hand masks"}), "Made a new item: Hand masks")
        self.assertEqual(replies.TOO_OLD_TO_CHANGE, "Too old to change — use /alias or /undo")

    def test_stats(self):
        counts = {"lines_structured": 40, "ai_match_calls": 3, "ai_full_calls": 2, "lines_rejected": 1}
        self.assertEqual(replies.stats("2026-10", counts), "\n".join([
            "October 2026 so far:",
            "• Lines read by the format: 40",
            "• AI item matches: 3",
            "• AI free-text fallbacks: 2",
            "• Lines I couldn't read: 1",
        ]))


class ItemsListTest(unittest.TestCase):
    def test_empty(self):
        self.assertEqual(replies.items_list([]), replies.NO_ITEMS)

    def test_lists_ids_names_units_pack_sizes_and_stock_sorted_by_name(self):
        items = [
            dict(TP, status="ok", stock_est=25, pack_size=10),
            dict(SOAP, status="out", stock_est=0, pack_size=500),
            dict(RICE),
            dict(MASKS, status="low", stock_est=3),
        ]
        self.assertEqual(replies.items_list(items), "\n".join([
            "Tracked items:",
            "dish-soap — Dishwashing liquid (ml, pack of 500 ml): out",
            "face-masks — Face masks (piece): low, 3 pieces",
            "rice — Rice (g)",
            "toilet-paper — Toilet paper (roll, pack of 10 rolls): ok, 25 rolls (2.5 packs)",
        ]))


if __name__ == "__main__":
    unittest.main()
