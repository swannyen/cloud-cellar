"""Command tests: /listitems, /list, /items, /undo, /alias, /rename, /set, /unit, /brand, /listbrands, /forget,
/stats, /help, /ping."""
import unittest
from unittest import mock

from fakes import FakeTable, entries, no_ai

import ai
import apply
import commands
import matcher
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
        for command in ("/status", "/price detergent", "/fx MYR", "/expiring"):
            self.assertEqual(commands.handle(command, MEI, NOW), replies.NOT_AVAILABLE_YET)

    def test_unknown_command_stays_silent(self):
        self.assertIsNone(commands.handle("/weather", MEI, NOW))

    def test_items_when_empty(self):
        self.table.rows.clear()
        self.assertEqual(commands.handle("/items", MEI, NOW), replies.NO_ITEMS)

    def test_listitems_shows_what_is_in_the_cellar(self):
        self.assertEqual(commands.handle("/listitems", MEI, NOW),
                         "Not counted yet:\n• Dishwashing liquid\n• Toilet paper")
        self.record("+ toilet paper 2x10\n- dish soap")
        self.assertEqual(commands.handle("/ListItems@bettynuffbot", MEI, NOW),
                         "In the cellar:\n• Toilet paper — 20 rolls (2 packs)\n\nOut:\n• Dishwashing liquid")

    def test_listitems_when_empty(self):
        self.table.rows.clear()
        self.assertEqual(commands.handle("/listitems", MEI, NOW), replies.NO_ITEMS)

    def test_items_lists_what_is_tracked(self):
        self.record("+ toilet paper 2x10\n- dish soap")
        self.assertEqual(commands.handle("/ITEMS", MEI, NOW), "\n".join([
            "Tracked items:",
            "dish-soap — Dishwashing liquid (ml): out",
            "toilet-paper — Toilet paper (roll, pack of 10 rolls): ok, 20 rolls (2 packs)",
        ]))


class ListTest(CommandTestCase):
    """/list <item>: one item in detail. /list on its own is kept for the shopping list (Phase 2)."""

    def body_wash(self):
        self.add_item("body-wash", "Body wash", "ml", ["shower gel"])
        for brand in ("Shokubutsu", "Lux"):
            commands.handle(f"/brand {brand}", MEI, NOW)
        self.record("+ shokubutsu body wash 2x800ml", update_id=1, at="2026-10-01T08:00:00Z")
        self.record("+ lux body wash 1x800ml", update_id=2, at="2026-10-03T08:00:00Z")
        self.record("+ body wash 1", update_id=3, at="2026-10-05T08:00:00Z")
        self.record("- body wash 400ml", update_id=4, at="2026-10-07T08:00:00Z")

    def test_without_an_item_it_says_how_to_use_it(self):
        self.assertEqual(commands.handle("/list", MEI, NOW), replies.LIST_USAGE)
        self.assertEqual(commands.handle("/list@bettynuffbot  ", MEI, NOW), replies.LIST_USAGE)

    def test_what_is_left_is_split_by_brand_with_the_oldest_used_first(self):
        self.body_wash()
        self.assertEqual(commands.handle("/list body wash", MEI, NOW), "\n".join([
            "Body wash (body-wash)",
            "Left: 2.8 L (3.5 packs)",
            "Pack size: 800 ml",
            "By brand, if the oldest is used first:",
            "• Shokubutsu — 1.2 L",
            "• Lux — 800 ml",
            "• no brand — 800 ml",
            "Also called: shower gel",
        ]))

    def test_a_used_line_that_names_a_brand_comes_off_that_brand(self):
        self.body_wash()
        self.record("- lux body wash 800ml", update_id=5, at="2026-10-08T08:00:00Z")
        self.assertEqual(commands.handle("/list shower gel", MEI, NOW), "\n".join([
            "Body wash (body-wash)",
            "Left: 2 L (2.5 packs)",
            "Pack size: 800 ml",
            "By brand, if the oldest is used first:",
            "• Shokubutsu — 1.2 L",
            "• no brand — 800 ml",
            "Bought before: Lux",
            "Also called: shower gel",
        ]))

    def test_item_can_be_named_by_id_name_alias_or_a_close_spelling(self):
        for target in ("toilet-paper", "Toilet Paper", "toilet papers", "toilet papr"):
            with self.subTest(target=target):
                self.assertEqual(commands.handle(f"/list {target}", MEI, NOW), "Toilet paper (toilet-paper)\nLeft: not counted yet")
        self.assertEqual(commands.handle("/list dish soap", MEI, NOW),
                         "Dishwashing liquid (dish-soap)\nLeft: not counted yet\nAlso called: dish soap")

    def test_stock_with_no_brand_on_any_entry(self):
        self.record("+ toilet paper 2x10")
        self.assertEqual(commands.handle("/list toilet paper", MEI, NOW),
                         "Toilet paper (toilet-paper)\nLeft: 20 rolls (2 packs)\nPack size: 10 rolls\nBrand: not noted")

    def test_running_low_and_out(self):
        commands.handle("/brand kleenex", MEI, NOW)
        self.record("+ kleenex toilet paper 2x10", update_id=1, at="2026-10-09T08:00:00Z")
        self.record("low toilet paper", update_id=2, at="2026-10-09T09:00:00Z")
        self.assertEqual(commands.handle("/list toilet paper", MEI, NOW), "\n".join([
            "Toilet paper (toilet-paper)",
            "Left: 10 rolls (1 pack), running low",
            "Pack size: 10 rolls",
            "By brand, if the oldest is used first:",
            "• kleenex — 10 rolls",
        ]))
        self.record("- toilet paper", update_id=3)
        self.assertEqual(commands.handle("/list toilet paper", MEI, NOW),
                         "Toilet paper (toilet-paper)\nLeft: none\nPack size: 10 rolls\nBought before: kleenex")

    def test_unknown_item(self):
        self.assertEqual(commands.handle("/list soy sauce", MEI, NOW), 'I don\'t track "soy sauce" yet. /items shows what I track.')

    def test_looking_changes_nothing(self):
        self.record("+ toilet paper 2x10")
        before = dict(self.table.rows)
        commands.handle("/list toilet paper", MEI, NOW)
        self.assertEqual(self.table.rows, before)


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

    def test_undoing_a_used_entry_puts_the_amount_back(self):
        self.record("+ toilet paper 3x10", update_id=1, at="2026-10-09T08:00:00Z")
        self.assertEqual(self.record("- toilet paper 2", update_id=2),
                         ["✓ Toilet paper: used 2 packs × 10 rolls (20 rolls), 10 rolls (1 pack) left."])
        self.assertEqual(store.get_item("toilet-paper")["status"], "low")
        self.assertEqual(commands.handle("/undo", MEI, NOW), "Undid: Toilet paper, used 2 packs × 10 rolls (20 rolls).")
        item = store.get_item("toilet-paper")
        self.assertEqual((item["status"], item["stock_est"]), ("ok", 30))

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


class RenameTest(CommandTestCase):
    def test_item_is_renamed_and_keeps_its_id_and_entries(self):
        self.record("+ toilet paper 2x10")
        self.assertEqual(
            commands.handle("/rename toilet paper = bathroom tissue", MEI, NOW),
            'Renamed Toilet paper to Bathroom tissue (ID still toilet-paper). "toilet paper" no longer means it. '
            "To keep that name too: /alias toilet paper = bathroom tissue",
        )
        item = store.get_item("toilet-paper")
        self.assertEqual((item["name"], item["stock_est"], item["pack_size"]), ("Bathroom tissue", 20, 10))
        self.record("+ bathroom tissue 1", update_id=2)  # no_ai would raise if the matcher needed help
        self.assertEqual(store.get_item("toilet-paper")["stock_est"], 30)
        self.assertIsNone(matcher.match("toilet paper", store.list_items()))

    def test_the_suggested_alias_brings_the_old_name_back(self):
        commands.handle("/rename toilet-paper = bathroom tissue", MEI, NOW)
        self.assertEqual(commands.handle("/alias toilet paper = bathroom tissue", MEI, NOW),
                         'Got it: "toilet paper" now means Bathroom tissue.')

    def test_new_name_is_kept_as_typed_with_a_capital_first(self):
        self.add_item("kn95-masks", "Kn95 masks", "piece")
        # Only the capitals change, so the old spelling still matches and there is nothing to warn about.
        self.assertEqual(commands.handle("/rename@bettynuffbot kn95 masks =  KN95   masks ", MEI, NOW),
                         "Renamed Kn95 masks to KN95 masks (ID still kn95-masks).")
        self.assertEqual(store.get_item("kn95-masks")["name"], "KN95 masks")

    def test_a_name_that_was_one_of_its_aliases_is_not_kept_as_both(self):
        commands.handle("/alias mama lemon = dish soap", MEI, NOW)
        self.assertIn("Renamed Dishwashing liquid to Dish soap", commands.handle("/rename dish-soap = dish soap", MEI, NOW))
        item = store.get_item("dish-soap")
        self.assertEqual((item["name"], item["aliases"]), ("Dish soap", ["mama lemon"]))

    def test_another_items_name_or_alias_cannot_be_taken(self):
        for name in ("Dishwashing liquids", "dish soap"):
            with self.subTest(name=name):
                taken = name[:1].upper() + name[1:]
                self.assertEqual(commands.handle(f"/rename toilet paper = {name}", MEI, NOW),
                                 f'"{taken}" already means Dishwashing liquid, so I\'ve left it alone.')
        self.assertEqual(store.get_item("toilet-paper")["name"], "Toilet paper")

    def test_the_name_it_already_has(self):
        self.assertEqual(commands.handle("/rename toilet-paper = Toilet paper", MEI, NOW), "It is already called Toilet paper.")

    def test_unknown_item(self):
        self.assertEqual(commands.handle("/rename soy sauce = shoyu", MEI, NOW),
                         'I don\'t track "soy sauce" yet. /items shows what I track.')

    def test_usage_is_shown_for_anything_else(self):
        for text in ("/rename", "/rename toilet paper", "/rename = tissue", "/rename toilet paper =", "/rename toilet paper = !!!",
                     "/rename toilet paper = a = b", "/rename toilet paper = " + "x" * 61):
            with self.subTest(text=text):
                self.assertEqual(commands.handle(text, MEI, NOW), replies.RENAME_USAGE)

    def test_undo_names_the_item_as_it_is_called_now(self):
        self.record("+ toilet paper 2x10")
        commands.handle("/rename toilet paper = bathroom tissue", MEI, NOW)
        self.assertEqual(commands.handle("/undo", MEI, NOW), "Undid: Bathroom tissue, bought 2 packs × 10 rolls (20 rolls).")


class ForgetTest(CommandTestCase):
    """/forget takes back what was taught. It never removes an entry."""

    def test_alias_is_forgotten(self):
        self.assertEqual(commands.handle("/forget alias Dish Soaps", MEI, NOW), 'Forgot "dish soap". It used to mean Dishwashing liquid.')
        self.assertEqual(self.aliases("dish-soap"), [])
        self.assertIsNone(matcher.match("dish soap", store.list_items()))

    def test_an_items_own_name_is_not_something_to_forget(self):
        self.assertEqual(commands.handle("/forget alias toilet paper", MEI, NOW),
                         "Toilet paper is what the item is called, so there is nothing to forget. "
                         "To change it: /rename toilet paper = <new name>")

    def test_alias_nobody_taught(self):
        self.assertEqual(commands.handle("/forget alias loo roll", MEI, NOW),
                         '"loo roll" isn\'t another name for anything I track. /list <item> shows an item\'s other names.')

    def test_brand_is_forgotten_as_taught_and_on_the_entries_that_carry_it(self):
        self.add_item("facial-masks", "Facial masks", "piece")
        commands.handle("/brand kans", MEI, NOW)
        self.record("+ kans facial masks 2pcs $1.96\n+ facial masks 5pcs", update_id=1)
        self.record("+ kans toilet paper 2x10", update_id=2)
        self.assertEqual(commands.handle("/forget brand KANS", MEI, NOW),
                         'Forgot the brand "KANS". It was on 2 entries (Facial masks, Toilet paper). '
                         "Those entries stay, without the brand.")
        self.assertEqual(store.list_brands(), [])
        self.assertEqual(commands.handle("/listbrands", MEI, NOW), replies.NO_BRANDS)
        # The entries are as they were, apart from the brand.
        kept = [("brand" in event, event["qty"], event.get("total_price")) for event in store.list_events("facial-masks")]
        self.assertEqual(kept, [(False, 2, 1.96), (False, 5, None)])
        item = store.get_item("facial-masks")
        self.assertEqual((item["stock_est"], "brands" in item), (7, False))
        # So "kans" at the front of a phrase is no longer taken off it.
        self.assertEqual(matcher.match_phrase("kans facial masks", store.list_items(), store.list_brands()),
                         (None, "kans facial masks", None))

    def test_brand_known_only_from_an_entry_can_be_forgotten_too(self):
        store.put_event("dish-soap", f"{AT}#9#0", {"action": "bought", "qty": 1000, "brand": "Mama Lemon", "at": AT})
        apply.refresh_item(store.get_item("dish-soap"))
        self.assertEqual(commands.handle("/forget brand mama lemon", MEI, NOW),
                         'Forgot the brand "mama lemon". It was on 1 entry (Dishwashing liquid). '
                         "That entry stays, without the brand.")
        self.assertNotIn("brands", store.get_item("dish-soap"))

    def test_taught_brand_that_is_on_no_entry(self):
        commands.handle("/brand Darlie", MEI, NOW)
        self.assertEqual(commands.handle("/forget brand darlie", MEI, NOW), 'Forgot the brand "darlie". It wasn\'t on any entry.')
        self.assertEqual(store.list_brands(), [])

    def test_brand_nobody_taught(self):
        self.assertEqual(commands.handle("/forget brand kans", MEI, NOW),
                         'I don\'t know "kans" as a brand. /listbrands shows the ones I know.')

    def test_taught_unit_word_is_forgotten(self):
        commands.handle("/unit sticks = pcs", MEI, NOW)
        self.assertEqual(commands.handle("/forget unit Sticks", MEI, NOW), 'Forgot "sticks" as a unit word. It used to mean pcs.')
        self.assertEqual(store.list_units(), {})

    def test_built_in_and_unknown_unit_words(self):
        self.assertEqual(commands.handle("/forget unit kg", MEI, NOW), '"kg" is built in, so I can\'t forget it.')
        self.assertEqual(commands.handle("/forget unit bananas", MEI, NOW), 'I don\'t know "bananas" as a unit word.')

    def test_saved_store_is_forgotten_and_entries_keep_it(self):
        self.record("+ toilet paper 2x10 $12 @giant")
        self.assertEqual([saved["name"] for saved in store.list_stores()], ["Giant"])
        self.assertEqual(commands.handle("/forget store GIANT", MEI, NOW), "Forgot the store Giant. Entries that name it keep it.")
        self.assertEqual(store.list_stores(), [])
        self.assertEqual(store.list_events("toilet-paper")[0]["store"], "Giant")

    def test_built_in_and_unknown_stores(self):
        self.assertEqual(commands.handle("/forget store ntuc", MEI, NOW), '"FairPrice" is built in, so I can\'t forget it.')
        self.assertEqual(commands.handle("/forget store giant", MEI, NOW), 'I don\'t have "giant" as a saved store.')

    def test_nothing_is_ever_removed_from_the_entries(self):
        commands.handle("/brand kans", MEI, NOW)
        self.record("+ kans toilet paper 2x10 $12 @giant")
        commands.handle("/alias loo roll = toilet paper", MEI, NOW)
        for text in ("/forget alias loo roll", "/forget brand kans", "/forget store giant"):
            commands.handle(text, MEI, NOW)
        event, = store.list_events("toilet-paper")
        self.assertEqual((event["qty"], event["total_price"], event["store"]), (20, 12, "Giant"))
        self.assertEqual(store.get_item("toilet-paper")["stock_est"], 20)

    def test_usage_is_shown_for_anything_else(self):
        for text in ("/forget", "/forget alias", "/forget brand   ", "/forget everything", "/forget item toilet paper"):
            with self.subTest(text=text):
                self.assertEqual(commands.handle(text, MEI, NOW), replies.FORGET_USAGE)
        self.assertIn("It used to mean Dishwashing liquid", commands.handle("/Forget@bettynuffbot  ALIAS   dish soap", MEI, NOW))


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
