"""Phase 0 handler tests: secret check, dedupe, allowed-chat filter, /ping.

DynamoDB and Telegram are faked; nothing here touches AWS or the network.
Run from the repo root: python3 -m unittest discover -s tests -v
"""
import base64
import json
import os
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

from botocore.exceptions import ClientError

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import config  # noqa: E402
import handler  # noqa: E402

WEBHOOK_SECRET = "test-webhook-secret"
GROUP_CHAT = -1001234567890
OTHER_CHAT = 987654321
SECRETS = {"webhook-secret": WEBHOOK_SECRET, "telegram-token": "test-token"}


class FakeTable:
    """In-memory DynamoDB table that honours attribute_not_exists(pk)."""

    def __init__(self, fail_with=None):
        self.items = {}
        self.fail_with = fail_with

    def put_item(self, Item, ConditionExpression=None):
        if self.fail_with:
            raise ClientError({"Error": {"Code": self.fail_with, "Message": "boom"}}, "PutItem")
        key = (Item["pk"], Item["sk"])
        if ConditionExpression == "attribute_not_exists(pk)" and key in self.items:
            raise ClientError(
                {"Error": {"Code": "ConditionalCheckFailedException", "Message": "exists"}},
                "PutItem",
            )
        self.items[key] = Item


def message_update(update_id=1, chat_id=GROUP_CHAT, text="/ping"):
    return {
        "update_id": update_id,
        "message": {"message_id": 10, "chat": {"id": chat_id, "type": "group"}, "text": text},
    }


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
        patches = [
            mock.patch.dict(os.environ, {"ALLOWED_CHAT_IDS": str(GROUP_CHAT)}),
            mock.patch.object(config, "secret", side_effect=SECRETS.__getitem__),
            mock.patch.object(handler, "_dedupe_table", side_effect=lambda: self.table),
            mock.patch.object(handler.telegram_api, "send_message"),
            mock.patch.object(handler.logger, "exception"),  # keep expected errors quiet
        ]
        started = [p.start() for p in patches]
        for p in patches:
            self.addCleanup(p.stop)
        self.send_message = started[3]

    def call(self, *args, **kwargs):
        return handler.lambda_handler(event(*args, **kwargs), None)


class SecretCheckTest(HandlerTestCase):
    def test_missing_header_is_403_and_does_no_work(self):
        response = self.call(message_update(), secret=None)
        self.assertEqual(response["statusCode"], 403)
        self.assertEqual(self.table.items, {})
        self.send_message.assert_not_called()

    def test_wrong_secret_is_403_and_does_no_work(self):
        response = self.call(message_update(), secret="not-the-secret")
        self.assertEqual(response["statusCode"], 403)
        self.assertEqual(self.table.items, {})
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
        self.send_message.assert_called_once_with(GROUP_CHAT, "pong")


class DedupeTest(HandlerTestCase):
    def test_same_update_twice_gets_one_reply(self):
        first = self.call(message_update(update_id=7))
        second = self.call(message_update(update_id=7))
        self.assertEqual((first["statusCode"], second["statusCode"]), (200, 200))
        self.send_message.assert_called_once_with(GROUP_CHAT, "pong")

    def test_different_updates_each_get_a_reply(self):
        self.call(message_update(update_id=7))
        self.call(message_update(update_id=8))
        self.assertEqual(self.send_message.call_count, 2)

    def test_marker_has_plan_key_and_two_day_ttl(self):
        before = int(time.time())
        self.call(message_update(update_id=42))
        marker = self.table.items[("UPD#42", "-")]
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
    def test_other_chat_gets_no_reply(self):
        response = self.call(message_update(chat_id=OTHER_CHAT))
        self.assertEqual(response["statusCode"], 200)
        self.send_message.assert_not_called()

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
            [c.args for c in self.send_message.call_args_list],
            [(OTHER_CHAT, "pong"), (-4123456789, "pong")],
        )


class RouteTest(HandlerTestCase):
    def test_ping_with_bot_name_suffix(self):
        self.call(message_update(text="/ping@bettynuffbot"))
        self.send_message.assert_called_once_with(GROUP_CHAT, "pong")

    def test_plain_text_and_other_commands_get_no_reply(self):
        self.call(message_update(update_id=1, text="finished the rice"))
        self.call(message_update(update_id=2, text="/list"))
        self.call(message_update(update_id=3, text="ping"))
        self.send_message.assert_not_called()

    def test_base64_body_is_decoded(self):
        self.call(message_update(), base64_body=True)
        self.send_message.assert_called_once_with(GROUP_CHAT, "pong")

    def test_malformed_body_returns_200(self):
        bad = {"headers": {handler.SECRET_HEADER: WEBHOOK_SECRET}, "body": "not json"}
        self.assertEqual(handler.lambda_handler(bad, None)["statusCode"], 200)
        self.send_message.assert_not_called()

    def test_telegram_failure_returns_200_and_tries_an_apology(self):
        self.send_message.side_effect = handler.telegram_api.TelegramError("sendMessage: HTTP 500")
        response = self.call(message_update())
        self.assertEqual(response["statusCode"], 200)
        self.assertEqual(
            [c.args for c in self.send_message.call_args_list],
            [(GROUP_CHAT, "pong"), (GROUP_CHAT, handler.ERROR_REPLY)],
        )


if __name__ == "__main__":
    unittest.main()
