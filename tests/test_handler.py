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

from fakes import FakeTable, parsed_event

import claude_api
import config
import handler
import parser
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
            "parse": mock.patch.object(parser, "parse", return_value=parser.Parsed([], None)),
            "log": mock.patch.object(handler.logger, "exception"),  # keep expected errors quiet
        }
        started = {name: patch.start() for name, patch in patches.items()}
        for patch in patches.values():
            self.addCleanup(patch.stop)
        self.send_message = started["send"]
        self.parse = started["parse"]

    def call(self, *args, **kwargs):
        return handler.lambda_handler(event(*args, **kwargs), None)

    def replies(self):
        return [call.args[1] for call in self.send_message.call_args_list]


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
    def test_other_chat_gets_no_reply_and_is_not_parsed(self):
        response = self.call(message_update(chat_id=OTHER_CHAT, text="bought rice"))
        self.assertEqual(response["statusCode"], 200)
        self.send_message.assert_not_called()
        self.parse.assert_not_called()

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

    def test_commands_are_not_sent_to_claude(self):
        self.call(message_update(text="/help"))
        self.parse.assert_not_called()
        self.assertIn("/undo", self.replies()[0])

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


class ChatRouteTest(HandlerTestCase):
    def test_chatter_with_no_events_gets_no_reply(self):
        self.call(message_update(text="what time is dinner?"))
        self.parse.assert_called_once()
        self.send_message.assert_not_called()

    def test_parser_gets_text_sender_known_items_and_singapore_date(self):
        store.create_item({"item_id": "rice", "name": "Rice", "base_unit": "g", "aliases": []})
        # 2026-10-09 16:30 UTC is already 10 October in Singapore.
        self.call(message_update(text="rice finish already", date=1791563400))
        text, sender_name, items, today = self.parse.call_args.args
        self.assertEqual((text, sender_name), ("rice finish already", "Mei"))
        self.assertEqual([item["item_id"] for item in items], ["rice"])
        self.assertEqual(today.isoformat(), "2026-10-10")

    def test_events_are_recorded_and_confirmed_in_one_reply_to_the_message(self):
        self.parse.return_value = parser.Parsed([
            parsed_event(packs=2, units_per_pack=10, total_price=12.9, store="FairPrice"),
            parsed_event("finished", item_name="Dishwashing liquid", base_unit="ml"),
        ], None)
        self.call(message_update(update_id=5, text="bought tp and finished the dish soap"))
        self.send_message.assert_called_once_with(
            GROUP_CHAT,
            "✓ Toilet paper (new item): bought 2 × 10 rolls for $12.90 at FairPrice ($0.65/roll).\n"
            "✓ Dishwashing liquid (new item): marked finished.",
            reply_to=MESSAGE_ID,
        )
        self.assertEqual(len(self.table.keys("EVT#")), 2)

    def test_clarify_question_is_sent(self):
        self.parse.return_value = parser.Parsed([], "Which soap did you finish?")
        self.call(message_update(text="finished the soap"))
        self.assertEqual(self.replies(), ["Which soap did you finish?"])

    def test_photo_caption_is_parsed(self):
        update = message_update(text=None, caption="bought rice 5kg $13.50")
        self.call(update)
        self.assertEqual(self.parse.call_args.args[0], "bought rice 5kg $13.50")

    def test_stickers_and_other_bots_are_ignored(self):
        self.call(message_update(update_id=1, text=None, sticker={"file_id": "x"}))
        bot_message = message_update(update_id=2, text="bought rice")
        bot_message["message"]["from"]["is_bot"] = True
        self.call(bot_message)
        self.parse.assert_not_called()
        self.send_message.assert_not_called()

    def test_claude_failure_apologises_in_reply_to_the_message(self):
        self.parse.side_effect = claude_api.ClaudeError("HTTP 529 overloaded_error")
        response = self.call(message_update(text="bought rice"))
        self.assertEqual(response["statusCode"], 200)
        self.send_message.assert_called_once_with(GROUP_CHAT, handler.ERROR_REPLY, reply_to=MESSAGE_ID)


if __name__ == "__main__":
    unittest.main()
