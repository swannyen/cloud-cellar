"""Command tests: /items, /undo, /help, /ping."""
import unittest
from unittest import mock

from fakes import FakeTable, parsed_event

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
        patch = mock.patch.object(store, "_table", self.table)
        patch.start()
        self.addCleanup(patch.stop)

    def record(self, *events, sender=MEI, update_id=1, at=AT):
        apply.apply_events(list(events), items=store.list_items(), sender=sender,
                           update_id=update_id, at=at, raw_text="text")


class SimpleCommandTest(CommandTestCase):
    def test_ping(self):
        self.assertEqual(commands.handle("/ping", MEI, NOW), "pong")

    def test_help_and_start_show_examples(self):
        self.assertEqual(commands.handle("/help", MEI, NOW), replies.HELP)
        self.assertEqual(commands.handle("/start@bettynuffbot", MEI, NOW), replies.HELP)

    def test_commands_from_later_phases_say_so(self):
        for command in ("/list", "/status", "/price detergent"):
            self.assertEqual(commands.handle(command, MEI, NOW), replies.NOT_AVAILABLE_YET)

    def test_unknown_command_stays_silent(self):
        self.assertIsNone(commands.handle("/weather", MEI, NOW))

    def test_items_when_empty(self):
        self.assertEqual(commands.handle("/items", MEI, NOW), replies.NO_ITEMS)

    def test_items_lists_what_is_tracked(self):
        self.record(parsed_event(packs=2), parsed_event("finished", item_name="Dishwashing liquid", base_unit="ml"))
        self.assertEqual(commands.handle("/ITEMS", MEI, NOW), "\n".join([
            "Tracked items:",
            "dishwashing-liquid — Dishwashing liquid (ml): out",
            "toilet-paper — Toilet paper (roll): ok, 2 packs",
        ]))


class UndoTest(CommandTestCase):
    def test_undo_removes_the_last_event_and_state_reflects_it(self):
        self.record(parsed_event(packs=2), update_id=1, at="2026-10-08T10:00:00Z")
        self.record(parsed_event("finished", item_id="toilet-paper"), update_id=2)
        self.assertEqual(store.get_item("toilet-paper")["status"], "out")

        reply = commands.handle("/undo", MEI, NOW)

        self.assertEqual(reply, "Undone: Toilet paper: marked finished.")
        self.assertEqual([event["action"] for event in store.list_events("toilet-paper")], ["bought"])
        item = store.get_item("toilet-paper")
        self.assertEqual((item["status"], item["stock_est"]), ("ok", 2))

    def test_second_undo_has_nothing_left(self):
        self.record(parsed_event(packs=2), update_id=1, at="2026-10-08T10:00:00Z")
        self.record(parsed_event("finished", item_id="toilet-paper"), update_id=2)
        commands.handle("/undo", MEI, NOW)
        self.assertEqual(commands.handle("/undo", MEI, NOW), replies.NOTHING_TO_UNDO)
        self.assertEqual(len(store.list_events("toilet-paper")), 1)

    def test_undoing_the_only_event_removes_the_item_it_created(self):
        self.record(parsed_event(item_name="Spnoge", base_unit="piece", packs=1))
        self.assertEqual(commands.handle("/undo", MEI, NOW), "Undone: Spnoge: bought 1 pack.")
        self.assertIsNone(store.get_item("spnoge"))
        self.assertEqual(self.table.keys(), [])

    def test_undo_only_touches_the_callers_own_entry(self):
        self.record(parsed_event(packs=2), sender=MEI, update_id=1)
        self.record(parsed_event("finished", item_id="toilet-paper"), sender=JUN, update_id=2, at="2026-10-09T10:00:30Z")
        commands.handle("/undo", MEI, NOW)
        self.assertEqual([event["action"] for event in store.list_events("toilet-paper")], ["finished"])
        self.assertIsNotNone(store.get_last_event(JUN["id"]))

    def test_nothing_to_undo_for_someone_who_never_logged(self):
        self.assertEqual(commands.handle("/undo", JUN, NOW), replies.NOTHING_TO_UNDO)

    def test_entries_older_than_24_hours_are_left_alone(self):
        self.record(parsed_event(packs=2))
        day_later = apply.epoch(AT) + 24 * 60 * 60 + 1
        self.assertEqual(commands.handle("/undo", MEI, day_later), replies.UNDO_TOO_OLD)
        self.assertEqual(len(store.list_events("toilet-paper")), 1)
        self.assertEqual(
            commands.handle("/undo", MEI, day_later - 2), "Undone: Toilet paper: bought 2 packs."
        )


if __name__ == "__main__":
    unittest.main()
