"""Command tests: /items, /undo, /alias, /set, /unit, /brand, /listbrands, /stats, /help, /ping."""
import unittest
from unittest import mock

from fakes import FakeTable, entries, no_ai

import ai
import apply
import commands
import replies
import store

MEI = {"id": 42, "name": "Mei"}
JUN = {"id": 43, "name": "Jun"}
AT = "2026-10-09T10:00:00Z"
NOW = apply.epoch(AT) + 60


class CommandTestCase(unittest.TestCase):
    def setUp(self):
        self.table = FakeTable()
        patches = [
            mock.patch.object(store, "_table", self.table),
            mock.patch.object(ai, "match_item", side_effect=no_ai),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        self.add_item("toilet-paper", "Toilet paper", "roll")
        self.add_item("dish-soap", "Dishwashing liquid", "ml", ["dish soap"])

    def add_item(self, item_id, name, base_unit, aliases=(), **fields):
        store.create_item({"item_id": item_id, "name": name, "base_unit": base_unit, "aliases": list(aliases), **fields})

    def record(self, text, sender=MEI, update_id=1, at=AT):
        return apply.apply_entries(entries(text, units=store.list_units()), items=store.list_items(),
                                   brands=store.list_brands(), sender=sender, update_id=update_id, at=at)[0]

    def aliases(self, item_id):
        return store.get_item(item_id)["aliases"]


class SimpleCommandTest(CommandTestCase):
    def test_ping(self):
        self.assertEqual(commands.handle("/ping", MEI, NOW), "pong")

    def test_help_and_start_show_the_format(self):
        self.assertEqual(commands.handle("/help", MEI, NOW), replies.HELP)
        self.assertEqual(commands.handle("/start@bettynuffbot", MEI, NOW), replies.HELP)

    def test_commands_from_later_phases_say_so(self):
        for command in ("/list", "/status", "/price detergent", "/fx MYR", "/expiring"):
            self.assertEqual(commands.handle(command, MEI, NOW), replies.NOT_AVAILABLE_YET)

    def test_unknown_command_stays_silent(self):
        self.assertIsNone(commands.handle("/weather", MEI, NOW))

    def test_items_when_empty(self):
        self.table.rows.clear()
        self.assertEqual(commands.handle("/items", MEI, NOW), replies.NO_ITEMS)

    def test_items_lists_what_is_tracked(self):
        self.record("+ toilet paper 2x10\n- dish soap")
        self.assertEqual(commands.handle("/ITEMS", MEI, NOW), "\n".join([
            "Tracked items:",
            "dish-soap — Dishwashing liquid (ml): out",
            "toilet-paper — Toilet paper (roll, pack of 10 rolls): ok, 20 rolls (2 packs)",
        ]))


class UndoTest(CommandTestCase):
    def test_undo_removes_the_last_event_and_state_reflects_it(self):
        self.record("+ toilet paper 2x10", update_id=1, at="2026-10-09T08:00:00Z")
        self.record("- toilet paper", update_id=2)
        self.assertEqual(store.get_item("toilet-paper")["status"], "out")

        reply = commands.handle("/undo", MEI, NOW)

        self.assertEqual(reply, "Undid: Toilet paper, marked finished.")
        self.assertEqual([event["action"] for event in store.list_events("toilet-paper")], ["bought"])
        item = store.get_item("toilet-paper")
        self.assertEqual((item["status"], item["stock_est"]), ("ok", 20))

    def test_undo_again_steps_back_one_more_until_nothing_is_left(self):
        self.record("+ toilet paper 2x10\n+ toilet paper 1x10", update_id=1, at="2026-10-09T08:00:00Z")
        self.record("- toilet paper", update_id=2)
        self.assertEqual(commands.handle("/undo", MEI, NOW), "Undid: Toilet paper, marked finished.")
        self.assertEqual(commands.handle("/undo", MEI, NOW), "Undid: Toilet paper, bought 1 pack of 10 rolls.")
        self.assertEqual(store.get_item("toilet-paper")["stock_est"], 20)
        self.assertEqual(commands.handle("/undo", MEI, NOW), "Undid: Toilet paper, bought 2 packs × 10 rolls (20 rolls).")
        self.assertEqual(commands.handle("/undo", MEI, NOW), replies.NOTHING_TO_UNDO)
        self.assertEqual(self.table.keys("EVT#") + self.table.keys("USER#"), [])

    def test_undoing_the_only_event_removes_the_item(self):
        self.record("+ toilet paper 1x10")
        self.assertEqual(commands.handle("/undo", MEI, NOW), "Undid: Toilet paper, bought 1 pack of 10 rolls.")
        self.assertIsNone(store.get_item("toilet-paper"))

    def test_undoing_a_mistyped_pack_size_takes_the_pack_size_back_too(self):
        self.record("+ toilet paper 2x10", update_id=1, at="2026-10-09T08:00:00Z")
        self.record("+ toilet paper 2x100", update_id=2)
        self.assertEqual(store.get_item("toilet-paper")["pack_size"], 100)
        commands.handle("/undo", MEI, NOW)
        self.assertEqual(store.get_item("toilet-paper")["pack_size"], 10)

    def test_undo_only_touches_the_callers_own_entries(self):
        self.record("+ toilet paper 2x10", sender=MEI, update_id=1)
        self.record("- toilet paper", sender=JUN, update_id=2, at="2026-10-09T10:00:30Z")
        commands.handle("/undo", MEI, NOW)
        self.assertEqual([event["action"] for event in store.list_events("toilet-paper")], ["finished"])
        self.assertEqual(len(store.list_recent_events(JUN["id"])), 1)

    def test_nothing_to_undo_for_someone_who_never_logged(self):
        self.assertEqual(commands.handle("/undo", JUN, NOW), replies.NOTHING_TO_UNDO)

    def test_entries_older_than_24_hours_are_left_alone(self):
        self.record("+ toilet paper 2x10")
        day_later = apply.epoch(AT) + 24 * 60 * 60 + 1
        self.assertEqual(commands.handle("/undo", MEI, day_later), replies.NOTHING_TO_UNDO)
        self.assertEqual(len(store.list_events("toilet-paper")), 1)
        self.assertEqual(commands.handle("/undo", MEI, day_later - 2),
                         "Undid: Toilet paper, bought 2 packs × 10 rolls (20 rolls).")

    def test_an_entry_that_is_already_gone_is_skipped(self):
        self.record("+ toilet paper 2x10", update_id=1, at="2026-10-09T08:00:00Z")
        self.record("+ toilet paper 1x10", update_id=2)
        store.delete_event("EVT#toilet-paper", f"{AT}#2#0")
        self.assertEqual(commands.handle("/undo", MEI, NOW), "Undid: Toilet paper, bought 2 packs × 10 rolls (20 rolls).")

    def test_pointer_left_by_the_previous_version_is_ignored(self):
        self.table.put_item(Item={"pk": "USER#42", "sk": "LAST", "event_pk": "EVT#toilet-paper",
                                  "event_sk": "2026-10-09T09:59:00Z#1#0", "at": "2026-10-07T09:59:00Z"})
        self.assertEqual(commands.handle("/undo", MEI, NOW), replies.NOTHING_TO_UNDO)


class AliasTest(CommandTestCase):
    def test_alias_is_saved_and_then_matches_without_ai(self):
        reply = commands.handle("/alias loo roll = toilet paper", MEI, NOW)
        self.assertEqual(reply, 'Got it: "loo roll" now means Toilet paper.')
        self.assertEqual(self.aliases("toilet-paper"), ["loo roll"])
        self.record("+ loo roll 2x10")  # no_ai would raise if the matcher needed help
        self.assertEqual(store.get_item("toilet-paper")["stock_est"], 20)

    def test_item_can_be_named_by_id_name_or_alias(self):
        for number, target in enumerate(["dish-soap", "Dishwashing Liquid", "dish soap", "DISH-SOAP"]):
            with self.subTest(target=target):
                reply = commands.handle(f"/alias name {number} = {target}", MEI, NOW)
                self.assertIn("now means Dishwashing liquid", reply)

    def test_alias_is_stored_normalised(self):
        commands.handle("/alias@bettynuffbot   Loo-Rolls!  =  Toilet Paper ", MEI, NOW)
        self.assertEqual(self.aliases("toilet-paper"), ["loo roll"])

    def test_alias_moves_away_from_the_item_that_had_it(self):
        self.add_item("kitchen-towel", "Kitchen towel", "roll", ["Loo Roll", "kitchen roll"])
        reply = commands.handle("/alias loo roll = toilet paper", MEI, NOW)
        self.assertEqual(reply, 'Got it: "loo roll" now means Toilet paper. (It used to mean Kitchen towel.)')
        self.assertEqual(self.aliases("kitchen-towel"), ["kitchen roll"])
        self.assertEqual(self.aliases("toilet-paper"), ["loo roll"])

    def test_alias_that_is_already_right_changes_nothing(self):
        self.assertEqual(commands.handle("/alias dish soap = dish-soap", MEI, NOW),
                         '"dish soap" already means Dishwashing liquid.')
        self.assertEqual(commands.handle("/alias toilet papers = toilet paper", MEI, NOW),
                         '"toilet paper" already means Toilet paper.')
        self.assertEqual(self.aliases("toilet-paper"), [])

    def test_another_items_name_cannot_be_taken(self):
        reply = commands.handle("/alias toilet paper = dish soap", MEI, NOW)
        self.assertEqual(reply, '"toilet paper" is the name of another item (Toilet paper), so I\'ve left it alone.')
        self.assertEqual(self.aliases("dish-soap"), ["dish soap"])

    def test_unknown_item(self):
        self.assertEqual(commands.handle("/alias loo roll = toilet papr", MEI, NOW),
                         'I don\'t track "toilet papr" yet. /items shows what I track.')

    def test_usage_is_shown_for_anything_else(self):
        for text in ("/alias", "/alias loo roll", "/alias = toilet paper", "/alias loo roll =", "/alias !!! = rice"):
            with self.subTest(text=text):
                self.assertEqual(commands.handle(text, MEI, NOW), replies.ALIAS_USAGE)

    def test_full_alias_list_is_reported(self):
        self.add_item("rice", "Rice", "g", [f"name {n}" for n in range(apply.MAX_ALIASES)])
        self.assertEqual(commands.handle("/alias beras = rice", MEI, NOW),
                         "Rice already has 30 other names, which is the most I keep.")


class SetPackTest(CommandTestCase):
    def test_pack_size_is_set_and_then_a_bare_count_works(self):
        self.assertEqual(commands.handle("/set toilet paper pack 10", MEI, NOW),
                         "Toilet paper: a pack is now 10 rolls, so a bare 2 means 20 rolls.")
        item = store.get_item("toilet-paper")
        self.assertEqual((item["pack_size"], item["pack_size_set_at"]), (10, apply.iso_utc(NOW)))
        self.assertEqual(self.record("+ toilet paper 2", at=apply.iso_utc(NOW + 60)),
                         ["✓ Toilet paper: bought 2 packs × 10 rolls (20 rolls)."])

    def test_size_can_carry_a_unit(self):
        self.assertEqual(commands.handle("/set dish soap pack 1.5L", MEI, NOW),
                         "Dishwashing liquid: a pack is now 1.5 L, so a bare 2 means 3 L.")
        self.assertEqual(store.get_item("dish-soap")["pack_size"], 1500)
        self.assertEqual(commands.handle("/SET Toilet-Paper PACK 12 rolls", MEI, NOW),
                         "Toilet paper: a pack is now 12 rolls, so a bare 2 means 24 rolls.")

    def test_set_by_hand_beats_an_older_entry(self):
        self.record("+ toilet paper 2x10", at="2026-10-09T08:00:00Z")
        commands.handle("/set toilet paper pack 12", MEI, NOW)
        self.record("+ toilet paper 1", update_id=2, at=apply.iso_utc(NOW + 60))
        self.assertEqual(store.list_events("toilet-paper")[-1]["qty"], 12)

    def test_unit_that_does_not_fit_the_item(self):
        self.assertEqual(commands.handle("/set toilet paper pack 5kg", MEI, NOW),
                         "Toilet paper is counted in rolls. Try 2rolls or 2x10rolls.")
        self.assertNotIn("pack_size", store.get_item("toilet-paper"))

    def test_unknown_item(self):
        self.assertEqual(commands.handle("/set soy sauce pack 2", MEI, NOW),
                         'I don\'t track "soy sauce" yet. /items shows what I track.')

    def test_usage_is_shown_for_anything_else(self):
        for text in ("/set", "/set toilet paper", "/set toilet paper pack", "/set toilet paper pack ten",
                     "/set toilet paper pack 0", "/set toilet paper pack 2 boxes", "/set toilet paper pack 2 bananas"):
            with self.subTest(text=text):
                self.assertEqual(commands.handle(text, MEI, NOW), replies.SET_USAGE)


class UnitTest(CommandTestCase):
    def test_taught_word_is_saved_and_then_read_as_that_unit(self):
        self.add_item("facial-masks", "Facial masks", "piece")
        self.assertEqual(commands.handle("/unit sticks = pcs", MEI, NOW), 'Got it: "sticks" now means pcs, as in 2 sticks.')
        self.assertEqual(store.list_units(), {"sticks": "pcs"})
        self.assertEqual(self.table.rows[("UNIT", "sticks")]["added_by"], "Mei")
        self.assertEqual(self.record("+ 2 sticks facial masks $1.96 from taobao"),
                         ["✓ Facial masks: bought 2 pieces for $1.96 at Taobao ($0.98/piece)."])

    def test_a_pack_word_can_be_taught_too(self):
        self.assertIn("now means carton", commands.handle("/unit CTN = Carton", MEI, NOW))
        self.assertEqual(store.list_units(), {"ctn": "carton"})

    def test_target_must_be_a_unit_word_i_know(self):
        self.assertIn('I don\'t know "bananas" as a unit', commands.handle("/unit sticks = bananas", MEI, NOW))
        self.assertEqual(store.list_units(), {})

    def test_built_in_words_and_keywords_cannot_be_retaught(self):
        for word in ("pcs", "kg", "box", "have", "low", "add"):
            with self.subTest(word=word):
                self.assertEqual(commands.handle(f"/unit {word} = rolls", MEI, NOW),
                                 f'"{word}" already means something to me, so I\'ve left it alone.')

    def test_usage_is_shown_for_anything_else(self):
        for text in ("/unit", "/unit sticks", "/unit = pcs", "/unit sticks =", "/unit two words = pcs", "/unit k4ns = pcs"):
            with self.subTest(text=text):
                self.assertEqual(commands.handle(text, MEI, NOW), replies.UNIT_USAGE)


class BrandCommandTest(CommandTestCase):
    def test_brand_is_saved_and_then_left_out_of_item_names(self):
        self.add_item("facial-masks", "Facial masks", "piece")
        self.assertEqual(commands.handle("/brand kans", MEI, NOW),
                         'Got it: "kans" is a brand. In "kans facial masks" I\'ll read the item as facial masks '
                         "and note the brand.")
        self.assertEqual(store.list_brands(), ["kans"])
        self.assertEqual(self.table.rows[("BRAND", "kans")]["added_by"], "Mei")
        # no_ai would raise if the matcher needed help.
        self.assertEqual(self.record("+ kans facial masks 2pcs $1.96 @taobao"),
                         ["✓ Facial masks: bought 2 pieces for $1.96 at Taobao ($0.98/piece)."])
        self.assertEqual(store.list_events("facial-masks")[0]["brand"], "kans")

    def test_brand_name_is_kept_as_typed_and_may_have_several_words(self):
        commands.handle("/brand@bettynuffbot   Royal   Umbrella ", MEI, NOW)
        self.assertEqual(store.list_brands(), ["Royal Umbrella"])
        self.assertEqual(self.table.keys("BRAND"), [("BRAND", "royal umbrella")])

    def test_a_brand_already_known_is_not_added_twice(self):
        commands.handle("/brand KANS", MEI, NOW)
        self.assertEqual(commands.handle("/brand kans", JUN, NOW), 'I already know "kans" as a brand.')
        self.assertEqual(store.list_brands(), ["KANS"])

    def test_an_items_own_name_cannot_be_a_brand(self):
        for name in ("dish soap", "Toilet Paper", "toilet-paper"):
            with self.subTest(name=name):
                self.assertIn("so it can't also be a brand", commands.handle(f"/brand {name}", MEI, NOW))
        self.assertEqual(store.list_brands(), [])

    def test_without_a_name_it_lists_the_brands_or_shows_how(self):
        self.assertEqual(commands.handle("/brand", MEI, NOW), replies.BRAND_USAGE)
        commands.handle("/brand kans", MEI, NOW)
        commands.handle("/brand Darlie", MEI, NOW)
        self.assertEqual(commands.handle("/brand", MEI, NOW),
                         "Brands:\n• Darlie — not on any entry yet\n• kans — not on any entry yet")

    def test_usage_is_shown_for_names_that_cannot_be_brands(self):
        for text in ("/brand !!!", "/brand " + "x" * 31):
            with self.subTest(text=text):
                self.assertEqual(commands.handle(text, MEI, NOW), replies.BRAND_USAGE)


class ListBrandsTest(CommandTestCase):
    def test_no_brands_yet(self):
        self.assertEqual(commands.handle("/listbrands", MEI, NOW), replies.NO_BRANDS)

    def test_brands_are_listed_with_the_items_they_were_bought_as(self):
        self.add_item("facial-masks", "Facial masks", "piece")
        self.add_item("sleeping-masks", "Sleeping masks", "piece")
        for brand in ("kans", "Darlie", "Kleenex"):
            commands.handle(f"/brand {brand}", MEI, NOW)
        self.record("+ kans facial masks 2pcs\n+ Kans sleeping masks 1pc\n+ kleenex toilet paper 2x10")
        self.assertEqual(commands.handle("/listbrands@bettynuffbot", MEI, NOW), "\n".join([
            "Brands:",
            "• Darlie — not on any entry yet",
            "• kans — Facial masks, Sleeping masks",
            "• Kleenex — Toilet paper",
        ]))

    def test_a_brand_only_ever_seen_on_an_entry_is_listed_too(self):
        # Brands also arrive with free-text entries, without anyone teaching them.
        store.put_event("dish-soap", f"{AT}#9#0", {"action": "bought", "qty": 1000, "brand": "Mama Lemon", "at": AT})
        apply.refresh_item(store.get_item("dish-soap"))
        self.assertEqual(commands.handle("/listbrands", MEI, NOW), "Brands:\n• Mama Lemon — Dishwashing liquid")

    def test_undoing_the_only_entry_with_a_brand_takes_it_off_the_item(self):
        commands.handle("/brand kleenex", MEI, NOW)
        self.record("+ toilet paper 2x10", update_id=1, at="2026-10-09T08:00:00Z")
        self.record("+ kleenex toilet paper 1x10", update_id=2)
        self.assertIn("• kleenex — Toilet paper", commands.handle("/listbrands", MEI, NOW))
        commands.handle("/undo", MEI, NOW)
        self.assertEqual(commands.handle("/listbrands", MEI, NOW), "Brands:\n• kleenex — not on any entry yet")


class StatsTest(CommandTestCase):
    def test_stats_for_a_month_with_nothing_counted(self):
        self.assertEqual(commands.handle("/stats", MEI, NOW), "\n".join([
            "October 2026 so far:",
            "• Lines read by the format: 0",
            "• AI item matches: 0",
            "• AI free-text fallbacks: 0",
            "• Lines I couldn't read: 0",
        ]))

    def test_stats_show_this_months_counters_only(self):
        store.add_stats("2026-10", {"lines_structured": 12, "ai_match_calls": 3, "lines_rejected": 1})
        store.add_stats("2026-09", {"lines_structured": 99})
        reply = commands.handle("/stats", MEI, NOW)
        self.assertIn("Lines read by the format: 12", reply)
        self.assertIn("AI item matches: 3", reply)
        self.assertIn("Lines I couldn't read: 1", reply)


if __name__ == "__main__":
    unittest.main()
