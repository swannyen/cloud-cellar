"""Handler tests: secret check, dedupe, allowed-chat filter and routing.

DynamoDB, Telegram and Claude are faked; nothing here touches AWS or the network.
Run from the repo root: python3 -m unittest discover -s tests -v
"""
import base64
import json
import os
import time
import unittest
from unittest import mock

from fakes import FakeTable, no_ai

import ai
import claude_api
import config
import handler
import replies
import store

WEBHOOK_SECRET = "test-webhook-secret"
GROUP_CHAT = -1001234567890
OTHER_CHAT = 987654321
MESSAGE_ID = 10
SECRETS = {"webhook-secret": WEBHOOK_SECRET, "telegram-token": "test-token"}


def message_update(update_id=1, chat_id=GROUP_CHAT, text="/ping", **message_fields):
    message = {
        "message_id": MESSAGE_ID,
        "date": 1791555000,
        "chat": {"id": chat_id, "type": "group"},
        "from": {"id": 42, "is_bot": False, "first_name": "Mei"},
        "text": text,
    }
    message.update(message_fields)
    return {"update_id": update_id, "message": message}


def event(update, secret=WEBHOOK_SECRET, base64_body=False):
    body = json.dumps(update)
    headers = {"content-type": "application/json"}
    if secret is not None:
        headers[handler.SECRET_HEADER] = secret
    if base64_body:
        body = base64.b64encode(body.encode()).decode()
    return {"headers": headers, "body": body, "isBase64Encoded": base64_body}


class HandlerTestCase(unittest.TestCase):
    def setUp(self):
        self.table = FakeTable()
        patches = {
            "env": mock.patch.dict(os.environ, {"ALLOWED_CHAT_IDS": str(GROUP_CHAT)}),
            "secret": mock.patch.object(config, "secret", side_effect=SECRETS.__getitem__),
            "table": mock.patch.object(store, "_table", self.table),
            "send": mock.patch.object(handler.telegram_api, "send_message"),
            "match_item": mock.patch.object(ai, "match_item", side_effect=no_ai),
            "free_text": mock.patch.object(ai, "parse_free_text", side_effect=no_ai),
            "log": mock.patch.object(handler.logger, "exception"),  # keep expected errors quiet
        }
        started = {name: patch.start() for name, patch in patches.items()}
        for patch in patches.values():
            self.addCleanup(patch.stop)
        self.send_message = started["send"]
        self.match_item = started["match_item"]
        self.parse_free_text = started["free_text"]

    def call(self, *args, **kwargs):
        return handler.lambda_handler(event(*args, **kwargs), None)

    def replies(self):
        return [call.args[1] for call in self.send_message.call_args_list]

    def add_item(self, item_id, name, base_unit, aliases=(), **fields):
        store.create_item({"item_id": item_id, "name": name, "base_unit": base_unit, "aliases": list(aliases), **fields})

    def stats(self, month="2026-10"):
        return {name: count for name, count in store.get_stats(month).items() if count}


class SecretCheckTest(HandlerTestCase):
    def test_missing_header_is_403_and_does_no_work(self):
        response = self.call(message_update(), secret=None)
        self.assertEqual(response["statusCode"], 403)
        self.assertEqual(self.table.rows, {})
        self.send_message.assert_not_called()

    def test_wrong_secret_is_403_and_does_no_work(self):
        response = self.call(message_update(), secret="not-the-secret")
        self.assertEqual(response["statusCode"], 403)
        self.assertEqual(self.table.rows, {})
        self.send_message.assert_not_called()

    def test_empty_secret_is_403(self):
        self.assertEqual(self.call(message_update(), secret="")["statusCode"], 403)

    def test_non_ascii_secret_is_403_not_a_crash(self):
        self.assertEqual(self.call(message_update(), secret="sécret")["statusCode"], 403)

    def test_no_headers_at_all_is_403(self):
        response = handler.lambda_handler({"body": json.dumps(message_update())}, None)
        self.assertEqual(response["statusCode"], 403)

    def test_correct_secret_is_200_and_replies(self):
        response = self.call(message_update())
        self.assertEqual(response["statusCode"], 200)
        self.assertEqual(json.loads(response["body"]), {"ok": True})
        self.send_message.assert_called_once_with(GROUP_CHAT, "pong", reply_to=MESSAGE_ID)


class DedupeTest(HandlerTestCase):
    def test_same_update_twice_gets_one_reply(self):
        first = self.call(message_update(update_id=7))
        second = self.call(message_update(update_id=7))
        self.assertEqual((first["statusCode"], second["statusCode"]), (200, 200))
        self.assertEqual(self.replies(), ["pong"])

    def test_different_updates_each_get_a_reply(self):
        self.call(message_update(update_id=7))
        self.call(message_update(update_id=8))
        self.assertEqual(self.send_message.call_count, 2)

    def test_marker_has_plan_key_and_two_day_ttl(self):
        before = int(time.time())
        self.call(message_update(update_id=42))
        marker = self.table.rows[("UPD#42", "-")]
        two_days = 2 * 24 * 60 * 60
        self.assertGreaterEqual(marker["ttl"], before + two_days)
        self.assertLessEqual(marker["ttl"], int(time.time()) + two_days)

    def test_dynamodb_error_returns_200_without_reply(self):
        self.table.fail_with = "ProvisionedThroughputExceededException"
        response = self.call(message_update())
        self.assertEqual(response["statusCode"], 200)
        self.send_message.assert_not_called()
        handler.logger.exception.assert_called_once()


class AllowedChatTest(HandlerTestCase):
    def test_other_chat_gets_no_reply_and_nothing_is_recorded(self):
        self.add_item("rice", "Rice", "g")
        response = self.call(message_update(chat_id=OTHER_CHAT, text="+ rice 2"))
        self.assertEqual(response["statusCode"], 200)
        self.send_message.assert_not_called()
        self.assertEqual(self.table.keys("EVT#"), [])

    def test_callback_query_from_other_chat_gets_no_reply(self):
        update = {
            "update_id": 3,
            "callback_query": {"id": "1", "data": "a:bought:rice", "message": {"chat": {"id": OTHER_CHAT}}},
        }
        self.assertEqual(self.call(update)["statusCode"], 200)
        self.send_message.assert_not_called()

    def test_update_without_a_chat_gets_no_reply(self):
        self.assertEqual(self.call({"update_id": 4})["statusCode"], 200)
        self.send_message.assert_not_called()

    def test_empty_allow_list_ignores_everything(self):
        with mock.patch.dict(os.environ, {"ALLOWED_CHAT_IDS": ""}):
            self.call(message_update())
        self.send_message.assert_not_called()

    def test_several_ids_with_spaces_are_all_allowed(self):
        with mock.patch.dict(os.environ, {"ALLOWED_CHAT_IDS": f"-4123456789, {OTHER_CHAT}"}):
            self.call(message_update(update_id=1, chat_id=OTHER_CHAT))
            self.call(message_update(update_id=2, chat_id=-4123456789))
            self.call(message_update(update_id=3, chat_id=GROUP_CHAT))
        self.assertEqual(
            [call.args[0] for call in self.send_message.call_args_list],
            [OTHER_CHAT, -4123456789],
        )


class CommandRouteTest(HandlerTestCase):
    def test_ping_with_bot_name_suffix(self):
        self.call(message_update(text="/ping@bettynuffbot"))
        self.assertEqual(self.replies(), ["pong"])

    def test_help_shows_the_format_and_the_commands(self):
        self.call(message_update(text="/help"))
        self.assertEqual(self.replies(), [replies.HELP])
        self.assertIn("/undo", replies.HELP)

    def test_alias_and_stats_are_routed(self):
        self.add_item("toilet-paper", "Toilet paper", "roll")
        self.call(message_update(update_id=1, text="/alias loo roll = toilet paper"))
        self.call(message_update(update_id=2, text="/stats"))
        self.assertEqual(self.replies()[0], 'Got it: "loo roll" now means Toilet paper.')
        self.assertIn("October 2026 so far:", self.replies()[1])

    def test_unknown_command_gets_no_reply(self):
        self.call(message_update(text="/somethingelse"))
        self.send_message.assert_not_called()

    def test_base64_body_is_decoded(self):
        self.call(message_update(), base64_body=True)
        self.assertEqual(self.replies(), ["pong"])

    def test_malformed_body_returns_200(self):
        bad = {"headers": {handler.SECRET_HEADER: WEBHOOK_SECRET}, "body": "not json"}
        self.assertEqual(handler.lambda_handler(bad, None)["statusCode"], 200)
        self.send_message.assert_not_called()

    def test_telegram_failure_returns_200_and_tries_an_apology(self):
        self.send_message.side_effect = handler.telegram_api.TelegramError("sendMessage: HTTP 500")
        response = self.call(message_update())
        self.assertEqual(response["statusCode"], 200)
        self.assertEqual(self.replies(), ["pong", handler.ERROR_REPLY])


class FormatRouteTest(HandlerTestCase):
    def setUp(self):
        super().setUp()
        self.add_item("toilet-paper", "Toilet paper", "roll")
        self.add_item("dish-soap", "Dishwashing liquid", "ml", ["dish soap"])

    def test_entries_are_recorded_and_confirmed_in_one_reply_to_the_message(self):
        self.call(message_update(update_id=5, text="+ toilet paper 2x10 $12.90 @fairprice\n- dish soap"))
        self.send_message.assert_called_once_with(
            GROUP_CHAT,
            "✓ Toilet paper: bought 2 packs × 10 rolls (20 rolls) for $12.90 at FairPrice ($0.65/roll).\n"
            "✓ Dishwashing liquid: marked finished.",
            reply_to=MESSAGE_ID,
        )
        self.assertEqual(len(self.table.keys("EVT#")), 2)
        self.assertEqual(self.stats(), {"lines_structured": 2})
        self.match_item.assert_not_called()

    def test_event_time_is_when_the_message_was_sent(self):
        self.call(message_update(text="- dish soap", date=1791555000))
        self.assertEqual(store.list_events("dish-soap")[0]["at"], "2026-10-09T14:10:00Z")

    def test_stats_go_to_the_singapore_month_of_the_message(self):
        # 2026-10-31 16:30 UTC is already 1 November in Singapore.
        self.call(message_update(text="- dish soap", date=1793464200))
        self.assertEqual(self.stats("2026-11"), {"lines_structured": 1})
        self.assertEqual(self.stats("2026-10"), {})

    def test_line_that_cannot_be_read_gets_the_format_hint(self):
        self.call(message_update(text="+ 2 3"))
        self.assertEqual(self.replies(), [replies.FORMAT_HINT])
        self.assertEqual(self.stats(), {"lines_rejected": 1})
        self.match_item.assert_not_called()

    def test_hint_is_sent_once_after_the_lines_that_could_be_read(self):
        self.call(message_update(text="+ 2 3\n- dish soap\n+ $5"))
        self.assertEqual(self.replies(), ["✓ Dishwashing liquid: marked finished.\n" + replies.FORMAT_HINT])
        self.assertEqual(self.stats(), {"lines_structured": 1, "lines_rejected": 2})

    def test_a_question_about_the_pack_size_is_the_reply_and_nothing_is_recorded(self):
        self.call(message_update(text="+ toilet paper 2 $12.90"))
        self.assertEqual(self.replies(), [
            "How many in each pack of Toilet paper? Resend like: + toilet paper 2x10rolls $12.90 — "
            "or write 2rolls if you mean single rolls."
        ])
        self.assertEqual(self.table.keys("EVT#"), [])
        self.assertEqual(self.stats(), {"lines_rejected": 1})

    def test_guessed_match_is_sent_with_its_buttons(self):
        self.match_item.side_effect = None
        self.match_item.return_value = ai.ItemMatch("toilet-paper", "Toilet paper", "roll")
        self.call(message_update(update_id=8, text="+ loo roll 1x10 $6"))
        self.send_message.assert_called_once_with(
            GROUP_CHAT,
            '✓ Toilet paper: bought 1 pack of 10 rolls for $6.00 ($0.60/roll). (Read "loo roll" as Toilet paper.)',
            reply_to=MESSAGE_ID,
            buttons=[[("Same item", "m:same:8:0"), ("New item: Loo roll", "m:new:8:0")]],
        )
        self.assertEqual(self.stats(), {"lines_structured": 1, "ai_match_calls": 1})

    def test_taught_brands_are_used(self):
        self.add_item("facial-masks", "Facial masks", "piece", pack_size=5, pack_size_set_at="2026-10-01T00:00:00Z")
        self.call(message_update(update_id=1, text="/brand kans"))
        self.call(message_update(update_id=2, text="bought 2 kans facial masks for 1.96 from Taobao"))
        self.send_message.assert_called_with(
            GROUP_CHAT, "✓ Facial masks: bought 2 packs × 5 pieces (10 pieces) for $1.96 at Taobao ($0.20/piece).",
            reply_to=MESSAGE_ID,
        )
        self.match_item.assert_not_called()

    def test_taught_unit_words_and_saved_stores_are_used(self):
        self.add_item("facial-masks", "Facial masks", "piece")
        store.save_unit("sticks", "pcs", "Mei")
        store.save_store("qoo10", "Qoo10", "2026-10-01T00:00:00Z")
        self.call(message_update(text="+ 2 sticks facial masks $1.96 from qoo10"))
        self.assertEqual(self.replies(), ["✓ Facial masks: bought 2 pieces for $1.96 at Qoo10 ($0.98/piece)."])

    def test_symbol_line_the_ai_does_not_take_for_a_product_is_answered_not_dropped(self):
        self.match_item.side_effect = None
        self.match_item.return_value = ai.ItemMatch(None, "", "piece")
        self.call(message_update(update_id=1, text="+ mama lemon 2x500ml $5\n- dish soap"))
        self.assertEqual(self.replies(), [replies.not_a_product("mama lemon") + "\n✓ Dishwashing liquid: marked finished."])
        self.assertEqual(self.stats(), {"ai_match_calls": 1, "lines_rejected": 1, "lines_structured": 1})
        # The same answer for a word keyword is taken as chat and gets nothing.
        self.send_message.reset_mock()
        self.call(message_update(update_id=2, text="have you eaten 2"))
        self.send_message.assert_not_called()

    def test_ai_failure_says_which_line_was_not_recorded(self):
        self.match_item.side_effect = claude_api.ClaudeError("HTTP 529 overloaded_error")
        with mock.patch.object(handler.apply.logger, "exception"):
            response = self.call(message_update(text="+ loo roll 2x10"))
        self.assertEqual(response["statusCode"], 200)
        self.send_message.assert_called_once_with(GROUP_CHAT, replies.lookup_failed("loo roll"), reply_to=MESSAGE_ID)

    def test_photo_caption_is_read(self):
        self.call(message_update(text=None, caption="- dish soap"))
        self.assertEqual(self.replies(), ["✓ Dishwashing liquid: marked finished."])

    def test_stickers_and_other_bots_are_ignored(self):
        self.call(message_update(update_id=1, text=None, sticker={"file_id": "x"}))
        bot_message = message_update(update_id=2, text="- dish soap")
        bot_message["message"]["from"]["is_bot"] = True
        self.call(bot_message)
        self.send_message.assert_not_called()
        self.assertEqual(self.table.keys("EVT#"), [])

    def test_counter_failure_does_not_cost_the_reply(self):
        with mock.patch.object(store, "add_stats", side_effect=RuntimeError("throttled")):
            self.call(message_update(text="- dish soap"))
        self.assertEqual(self.replies(), ["✓ Dishwashing liquid: marked finished."])
        handler.logger.exception.assert_called_once()


class EntryCommandTest(HandlerTestCase):
    """/add, /bought and /used are the format written as commands: "+" and "-" under other names."""

    def setUp(self):
        super().setUp()
        self.add_item("toilet-paper", "Toilet paper", "roll")
        self.add_item("dish-soap", "Dishwashing liquid", "ml", ["dish soap"])

    def test_add_and_bought_record_a_purchase_like_the_plus_sign(self):
        self.call(message_update(update_id=1, text="+ toilet paper 2x10 $12.90 @fairprice"))
        self.call(message_update(update_id=2, text="/add toilet paper 2x10 $12.90 @fairprice"))
        self.call(message_update(update_id=3, text="/bought@bettynuffbot toilet paper 2x10 $12.90 @fairprice"))
        self.assertEqual(self.replies(), ["✓ Toilet paper: bought 2 packs × 10 rolls (20 rolls) for $12.90 at FairPrice ($0.65/roll)."] * 3)
        events = store.list_events("toilet-paper")
        self.assertEqual([event["qty"] for event in events], [20, 20, 20])
        self.assertEqual(events[1]["raw_text"], "/add toilet paper 2x10 $12.90 @fairprice")
        self.assertEqual(self.stats(), {"lines_structured": 3})
        self.match_item.assert_not_called()

    def test_used_marks_finished_like_the_minus_sign(self):
        self.call(message_update(text="/used dish soap"))
        self.assertEqual(self.replies(), ["✓ Dishwashing liquid: marked finished."])
        self.assertEqual(store.get_item("dish-soap")["status"], "out")

    def test_command_with_nothing_after_it_says_how_to_use_it(self):
        for update_id, text in enumerate(["/add", "/used", "/bought@bettynuffbot"]):
            self.call(message_update(update_id=update_id, text=text))
        self.assertEqual(self.replies(), [replies.ENTRY_COMMAND_USAGE] * 3)
        self.assertEqual(self.table.keys("EVT#"), [])

    def test_command_that_cannot_be_read_gets_the_format_hint(self):
        self.call(message_update(text="/add 2 3"))
        self.assertEqual(self.replies(), [replies.FORMAT_HINT])

    def test_more_lines_can_follow_in_the_same_message(self):
        self.call(message_update(text="/add toilet paper 1x10\n/used dish soap\n+ toilet paper 1x10"))
        self.assertEqual(self.replies(), ["✓ Toilet paper: bought 1 pack of 10 rolls.\n✓ Dishwashing liquid: marked finished.\n"
                                          "✓ Toilet paper: bought 1 pack of 10 rolls."])

    def test_other_commands_are_not_taken_for_entries(self):
        self.call(message_update(update_id=1, text="/listbrands"))
        self.call(message_update(update_id=2, text="/address 5 rice road"))
        self.assertEqual(self.replies(), [replies.NO_BRANDS])
        self.assertEqual(self.table.keys("EVT#"), [])


class NoKeywordTest(HandlerTestCase):
    """A line with no keyword is still a purchase if it has a price and an item known by exactly that name."""

    def setUp(self):
        super().setUp()
        self.add_item("facial-masks", "Facial masks", "piece")

    def test_known_item_with_a_price_is_read_as_bought_without_ai(self):
        self.call(message_update(text="facial masks 5 pcs 1.86 from taobao"))
        self.assertEqual(self.replies(), ["✓ Facial masks: bought 5 pieces for $1.86 at Taobao ($0.37/piece)."])
        event, = store.list_events("facial-masks")
        self.assertEqual((event["parse_path"], event["raw_text"]), ("structured", "facial masks 5 pcs 1.86 from taobao"))
        self.assertEqual(self.stats(), {"lines_structured": 1})
        self.parse_free_text.assert_not_called()

    def test_facial_masks_are_not_read_as_face_masks(self):
        # Only Face masks is tracked here: "facial masks" is a different product, so code doesn't match it.
        self.table.rows.clear()
        self.add_item("face-masks", "Face masks", "piece")
        self.parse_free_text.side_effect = None
        self.parse_free_text.return_value = ai.Parsed([], None)
        self.call(message_update(text="facial masks 5 pcs 1.86 from taobao"))
        self.parse_free_text.assert_called_once()
        self.assertEqual(self.table.keys("EVT#"), [])

    def test_unknown_item_goes_to_the_free_text_fallback_instead(self):
        self.parse_free_text.side_effect = None
        self.parse_free_text.return_value = ai.Parsed([], None)
        self.call(message_update(text="hand masks 10 pcs 1.45 from taobao"))
        self.parse_free_text.assert_called_once()
        self.match_item.assert_not_called()

    def test_taught_brand_in_front_of_a_known_item_is_still_a_purchase(self):
        store.save_brand("kans", "kans", "Mei")
        self.call(message_update(text="Kans facial masks 5 pcs 1.86 from taobao"))
        self.assertEqual(self.replies(), ["✓ Facial masks: bought 5 pieces for $1.86 at Taobao ($0.37/piece)."])
        self.assertEqual(store.list_events("facial-masks")[0]["brand"], "kans")
        self.parse_free_text.assert_not_called()

    def test_known_item_without_a_price_is_not_assumed_to_be_a_purchase(self):
        self.parse_free_text.side_effect = None
        self.parse_free_text.return_value = ai.Parsed([], None)
        self.call(message_update(text="facial masks 5 pcs"))
        self.assertEqual(self.table.keys("EVT#"), [])
        self.parse_free_text.assert_called_once()


def free_event(action="bought", item_id=None, item_name="Toilet paper", base_unit="roll", **fields):
    """An event shaped like ai.normalise_free_text() output."""
    event = {"action": action, "item_id": item_id, "item_name": item_name, "base_unit": base_unit,
             "quantity_text": None, "total_price": None, "store": None, "brand": None}
    event.update(fields)
    return event


class FreeTextRouteTest(HandlerTestCase):
    FREE_TEXT = "bought 2 packs toilet paper 10 rolls each $12.90 fairprice"

    def setUp(self):
        super().setUp()
        self.add_item("toilet-paper", "Toilet paper", "roll")
        self.add_item("rice", "Rice", "g")
        self.add_item("face-masks", "Face masks", "piece")

    def ai_reads(self, *events, clarify=None):
        self.parse_free_text.side_effect = None
        self.parse_free_text.return_value = ai.Parsed(list(events), clarify, {"input_tokens": 1500, "output_tokens": 80})

    def test_free_text_gives_the_same_reply_and_event_as_the_format(self):
        self.ai_reads(free_event(item_id="toilet-paper", item_name="toilet paper", total_price=12.9, store="FairPrice",
                                 quantity_text="2 packs toilet paper 10 rolls each"))
        self.call(message_update(update_id=1, text=self.FREE_TEXT))
        self.call(message_update(update_id=2, text="+ toilet paper 2x10 $12.90 @fairprice"))
        said, typed = self.replies()
        self.assertEqual(said, typed)
        self.assertEqual(said, "✓ Toilet paper: bought 2 packs × 10 rolls (20 rolls) for $12.90 at FairPrice ($0.65/roll).")
        first, second = store.list_events("toilet-paper")
        self.assertEqual((first["parse_path"], second["parse_path"]), ("ai_full", "structured"))
        self.assertEqual((first["qty"], first["unit_price"], first["store"]), (second["qty"], second["unit_price"], second["store"]))
        self.assertEqual(self.stats(), {"ai_full_calls": 1, "lines_structured": 1})
        self.match_item.assert_not_called()

    def test_fallback_gets_the_text_sender_items_and_singapore_date(self):
        self.ai_reads()
        # 2026-10-09 16:30 UTC is already 10 October in Singapore.
        self.call(message_update(text="rice finish already", date=1791563400))
        text, sender_name, items, today = self.parse_free_text.call_args.args
        self.assertEqual((text, sender_name), ("rice finish already", "Mei"))
        self.assertEqual([item["item_id"] for item in items], ["face-masks", "rice", "toilet-paper"])
        self.assertEqual(today.isoformat(), "2026-10-10")

    def test_store_comes_from_the_message_not_from_the_model(self):
        # The model answers Shopee; the message says Taobao.
        self.ai_reads(free_event(item_id="face-masks", item_name="face masks", base_unit="piece",
                                 quantity_text="5 pcs", total_price=1.86, store="Shopee"))
        self.call(message_update(text="got Face Masks 5 pcs for $1.86 from Taobao"))
        self.assertEqual(self.replies(), ["✓ Face masks: bought 5 pieces for $1.86 at Taobao ($0.37/piece)."])
        self.assertEqual(store.list_events("face-masks")[0]["store"], "Taobao")

    def test_numbers_the_model_made_up_are_dropped_and_said(self):
        self.ai_reads(free_event(item_id="face-masks", item_name="face masks", base_unit="piece",
                                 quantity_text="10 pcs", total_price=14.5, store="Lazada"))
        self.call(message_update(text="got face masks, 10 pcs"))
        self.assertEqual(self.replies(), ["✓ Face masks: bought 10 pieces. (Didn't catch the store or price.)"])

    def test_models_guess_at_the_item_gets_the_confirmation_buttons(self):
        self.ai_reads(free_event(item_id="face-masks", item_name="HAND masks", base_unit="piece",
                                 quantity_text="10 pcs", total_price=1.45, store="Taobao"))
        self.call(message_update(update_id=4, text="HAND masks 10 pcs $1.45 from Taobao"))
        self.send_message.assert_called_once_with(
            GROUP_CHAT,
            '✓ Face masks: bought 10 pieces for $1.45 at Taobao ($0.14/piece). (Read "HAND masks" as Face masks.)',
            reply_to=MESSAGE_ID,
            buttons=[[("Same item", "m:same:4:0"), ("New item: Hand masks", "m:new:4:0")]],
        )

    def test_switched_off_the_same_message_gets_the_hint_and_no_ai_call(self):
        with mock.patch.dict(os.environ, {"FREE_TEXT_AI": "off"}):
            self.call(message_update(update_id=1, text=self.FREE_TEXT))
            self.call(message_update(update_id=2, text="rice finish already"))
        self.assertEqual(self.replies(), [replies.FORMAT_HINT, replies.FORMAT_HINT])
        self.parse_free_text.assert_not_called()
        self.assertEqual(self.stats(), {"lines_rejected": 2})
        self.assertEqual(self.table.keys("EVT#"), [])

    def test_chat_that_only_looked_like_a_log_stays_silent(self):
        self.ai_reads()
        self.call(message_update(text="see you at 7"))
        self.send_message.assert_not_called()
        self.assertEqual(self.stats(), {"ai_full_calls": 1})

    def test_format_attempt_the_ai_cannot_read_either_gets_the_hint(self):
        self.ai_reads()
        self.call(message_update(text="+ rice 2 3"))
        self.assertEqual(self.replies(), [replies.FORMAT_HINT])
        self.assertEqual(self.stats(), {"ai_full_calls": 1, "lines_rejected": 1})

    def test_clarifying_question_is_sent(self):
        self.ai_reads(clarify="Which soap did you finish?")
        self.call(message_update(text="the big bottle of soap is finished"))
        self.assertEqual(self.replies(), ["Which soap did you finish?"])
        self.assertEqual(self.table.keys("EVT#"), [])

    def test_free_text_lines_share_one_call_and_format_lines_skip_it(self):
        self.ai_reads(free_event("finished", item_id="rice", item_name="rice", base_unit="g"))
        self.call(message_update(text="+ toilet paper 2x10\nrice finish already\nlol\nalso the eggs, 30 for $6"))
        self.parse_free_text.assert_called_once()
        self.assertEqual(self.parse_free_text.call_args.args[0], "rice finish already\nalso the eggs, 30 for $6")
        self.assertEqual(self.replies(), ["✓ Toilet paper: bought 2 packs × 10 rolls (20 rolls).\n✓ Rice: marked finished."])
        self.assertEqual(self.stats(), {"lines_structured": 1, "ai_full_calls": 1})

    def test_fallback_failure_keeps_the_format_lines(self):
        self.parse_free_text.side_effect = claude_api.ClaudeError("HTTP 529 overloaded_error")
        response = self.call(message_update(text="+ toilet paper 2x10\nrice finish already"))
        self.assertEqual(response["statusCode"], 200)
        self.assertEqual(self.replies(), ["✓ Toilet paper: bought 2 packs × 10 rolls (20 rolls).\n" + replies.FREE_TEXT_FAILED])
        self.assertEqual(self.stats(), {"lines_structured": 1})

    def test_free_text_is_on_unless_switched_off(self):
        for value, expected in (("on", True), ("off", False), (" OFF ", False), ("", True)):
            with mock.patch.dict(os.environ, {"FREE_TEXT_AI": value}):
                self.assertIs(config.free_text_ai(), expected)
        with mock.patch.dict(os.environ):
            os.environ.pop("FREE_TEXT_AI", None)
            self.assertTrue(config.free_text_ai())


class ButtonRouteTest(HandlerTestCase):
    def setUp(self):
        super().setUp()
        patches = [
            mock.patch.object(handler.telegram_api, "answer_callback_query"),
            mock.patch.object(handler.telegram_api, "edit_buttons"),
        ]
        self.answer, self.edit = (patch.start() for patch in patches)
        for patch in patches:
            self.addCleanup(patch.stop)
        self.add_item("face-masks", "Face masks", "piece")
        self.match_item.side_effect = None
        self.match_item.return_value = ai.ItemMatch("face-masks", "Face masks", "piece")
        self.call(message_update(update_id=5, text="+ hand masks 10 pcs $1.45 from taobao"))
        self.rows = self.send_message.call_args.kwargs["buttons"]

    def tap(self, data, update_id=6, chat_id=GROUP_CHAT):
        keyboard = [[{"text": label, "callback_data": value} for label, value in row] for row in self.rows]
        return self.call({"update_id": update_id, "callback_query": {
            "id": "cb1", "data": data, "from": {"id": 43, "first_name": "Jun"},
            "message": {"message_id": 77, "chat": {"id": chat_id}, "reply_markup": {"inline_keyboard": keyboard}},
        }})

    def test_new_item_tap_moves_the_entry_and_clears_the_buttons(self):
        response = self.tap("m:new:5:0")
        self.assertEqual(response["statusCode"], 200)
        self.answer.assert_called_once_with("cb1", "Made a new item: Hand masks")
        self.edit.assert_called_once_with(GROUP_CHAT, 77, [])
        self.assertEqual(store.list_events("hand-masks")[0]["store"], "Taobao")
        self.assertEqual(store.list_events("face-masks"), [])

    def test_same_item_tap_then_the_phrase_needs_no_ai_and_no_buttons(self):
        self.tap("m:same:5:0")
        self.answer.assert_called_once_with("cb1", "Saved: 'hand masks' = Face masks")
        self.match_item.side_effect = no_ai
        self.send_message.reset_mock()
        self.call(message_update(update_id=7, text="+ hand masks 2pcs"))
        self.send_message.assert_called_once_with(GROUP_CHAT, "✓ Face masks: bought 2 pieces.", reply_to=MESSAGE_ID)

    def test_same_tap_delivered_twice_is_acted_on_once(self):
        self.tap("m:new:5:0", update_id=6)
        self.tap("m:new:5:0", update_id=6)
        self.answer.assert_called_once()

    def test_tap_from_another_chat_is_ignored(self):
        self.tap("m:new:5:0", chat_id=OTHER_CHAT)
        self.answer.assert_not_called()
        self.assertEqual(len(store.list_events("face-masks")), 1)

    def test_failure_still_answers_the_tap(self):
        with mock.patch.object(handler.buttons.apply, "split_into_new_item", side_effect=RuntimeError("boom")):
            response = self.tap("m:new:5:0")
        self.assertEqual(response["statusCode"], 200)
        self.answer.assert_called_once_with("cb1", replies.BUTTON_FAILED)


class ChatterTest(HandlerTestCase):
    def test_chatter_gets_no_reply_and_no_ai_call(self):
        self.add_item("rice", "Rice", "g")
        for update_id, text in enumerate(["ok thanks", "done", "??", "finished", "haha\nsee you later", "the price is crazy"]):
            self.call(message_update(update_id=update_id, text=text))
        self.send_message.assert_not_called()
        self.match_item.assert_not_called()
        self.parse_free_text.assert_not_called()
        self.assertEqual(self.table.keys("EVT#") + self.table.keys("STATS"), [])


if __name__ == "__main__":
    unittest.main()
