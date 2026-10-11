"""Button tap tests (PLAN.md §4.3): the match confirmations. Telegram and the AI are faked."""
import unittest
from unittest import mock

from fakes import FakeTable, entries

import ai
import apply
import buttons
import replies
import store

MEI = {"id": 42, "name": "Mei"}
AT = "2026-10-09T10:00:00Z"
CHAT = -1001234567890
MESSAGE_ID = 55


def keyboard(*rows):
    """A message's buttons as Telegram sends them back with a tap."""
    return {"inline_keyboard": [[{"text": label, "callback_data": data} for label, data in row] for row in rows]}


class ButtonTestCase(unittest.TestCase):
    def setUp(self):
        self.table = FakeTable()
        patches = [
            mock.patch.object(store, "_table", self.table),
            mock.patch.object(ai, "match_item", return_value=ai.ItemMatch("face-masks", "Face masks", "piece")),
            mock.patch.object(buttons.telegram_api, "answer_callback_query"),
            mock.patch.object(buttons.telegram_api, "edit_buttons"),
        ]
        started = [patch.start() for patch in patches]
        for patch in patches:
            self.addCleanup(patch.stop)
        self.answer, self.edit = started[2], started[3]
        store.create_item({"item_id": "face-masks", "name": "Face masks", "base_unit": "piece", "aliases": []})

    def guess(self, text="+ hand masks 10 pcs $1.45 from taobao", update_id=5):
        """Record a guessed match and return its button rows."""
        return apply.apply_entries(entries(text), items=store.list_items(), sender=MEI, update_id=update_id, at=AT)[2]

    def tap(self, data, rows=()):
        callback = {"id": "cb1", "data": data,
                    "message": {"message_id": MESSAGE_ID, "chat": {"id": CHAT}, "reply_markup": keyboard(*rows)}}
        buttons.handle(callback, CHAT)

    def toast(self):
        self.answer.assert_called_once()
        return self.answer.call_args.args


class MatchConfirmationTest(ButtonTestCase):
    def test_same_item_saves_the_alias_and_removes_the_buttons(self):
        rows = self.guess()
        self.tap("m:same:5:0", rows)
        self.assertEqual(self.toast(), ("cb1", "Saved: 'hand masks' = Face masks"))
        self.assertEqual(store.get_item("face-masks")["aliases"], ["hand mask"])  # stored normalised
        self.assertEqual(self.table.keys("PEND#"), [])
        self.edit.assert_called_once_with(CHAT, MESSAGE_ID, [])

    def test_new_item_moves_the_entry(self):
        rows = self.guess()
        self.tap("m:new:5:0", rows)
        self.assertEqual(self.toast(), ("cb1", "Made a new item: Hand masks"))
        self.assertEqual(store.list_events("face-masks"), [])
        event, = store.list_events("hand-masks")
        self.assertEqual((event["qty"], event["store"]), (10, "Taobao"))
        self.assertEqual(self.table.keys("PEND#"), [])
        self.edit.assert_called_once_with(CHAT, MESSAGE_ID, [])

    def test_only_the_tapped_row_is_removed(self):
        rows = self.guess("+ hand masks 10pcs\n+ foot masks 5pcs")
        self.assertEqual(len(rows), 2)
        self.tap("m:new:5:1", rows)
        self.edit.assert_called_once_with(CHAT, MESSAGE_ID, keyboard(rows[0])["inline_keyboard"])
        self.assertEqual(self.table.keys("PEND#"), [("PEND#5#0", "-")])
        self.assertIsNotNone(store.get_item("foot-masks"))

    def test_second_tap_finds_nothing_to_change(self):
        rows = self.guess()
        self.tap("m:same:5:0", rows)
        self.answer.reset_mock()
        self.tap("m:new:5:0", rows)
        self.assertEqual(self.toast(), ("cb1", replies.TOO_OLD_TO_CHANGE))
        self.assertIsNone(store.get_item("hand-masks"))

    def test_expired_confirmation_says_what_to_use_instead(self):
        self.tap("m:same:999:0", [[("Same item", "m:same:999:0"), ("New item: X", "m:new:999:0")]])
        self.assertEqual(self.toast(), ("cb1", "Too old to change — use /alias or /undo"))
        self.edit.assert_called_once_with(CHAT, MESSAGE_ID, [])

    def test_entry_undone_before_the_tap(self):
        rows = self.guess()
        pk, sk = self.table.keys("EVT#")[0]
        store.delete_event(pk, sk)
        self.tap("m:new:5:0", rows)
        self.assertEqual(self.toast(), ("cb1", replies.ENTRY_WAS_UNDONE))
        self.assertIsNone(store.get_item("hand-masks"))

    def test_other_button_data_is_answered_and_left_alone(self):
        for data in ("a:bought:rice", "m:same:5", "m:other:5:0", "", None):
            with self.subTest(data=data):
                self.answer.reset_mock()
                self.tap(data)
                self.assertEqual(self.toast(), ("cb1", ""))
        self.edit.assert_not_called()


if __name__ == "__main__":
    unittest.main()
