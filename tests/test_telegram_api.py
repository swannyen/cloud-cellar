"""Telegram client tests: what is sent for messages, buttons and taps. urlopen is faked; nothing is sent."""
import io
import json
import unittest
import urllib.error
from unittest import mock

import fakes  # noqa: F401  (puts src/ on sys.path)

import config
import telegram_api

TOKEN = "123456:test-token"
CHAT = -1001234567890
ROWS = [[("Same item", "m:same:8:0"), ("New item: Loo roll", "m:new:8:0")]]


class TelegramApiTest(unittest.TestCase):
    def setUp(self):
        self.requests = []
        self.result = {"message_id": 1}
        patches = [
            mock.patch.object(config, "secret", return_value=TOKEN),
            mock.patch.object(telegram_api.urllib.request, "urlopen", side_effect=self.urlopen),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def urlopen(self, request, timeout):
        self.requests.append(request)
        if isinstance(self.result, Exception):
            raise self.result
        return io.BytesIO(json.dumps({"ok": True, "result": self.result}).encode())

    def sent(self):
        request, = self.requests
        return request.full_url.rsplit("/", 1)[1], json.loads(request.data)

    def test_plain_reply(self):
        telegram_api.send_message(CHAT, "pong", reply_to=10)
        self.assertEqual(self.sent(), ("sendMessage", {
            "chat_id": CHAT, "text": "pong",
            "reply_parameters": {"message_id": 10, "allow_sending_without_reply": True},
        }))

    def test_message_with_buttons(self):
        telegram_api.send_message(CHAT, "✓ Toilet paper: bought 10 rolls.", reply_to=10, buttons=ROWS)
        method, payload = self.sent()
        self.assertEqual(payload["reply_markup"], {"inline_keyboard": [[
            {"text": "Same item", "callback_data": "m:same:8:0"},
            {"text": "New item: Loo roll", "callback_data": "m:new:8:0"},
        ]]})

    def test_button_data_fits_telegrams_limit(self):
        # The longest data this bot sends: a 64-bit update ID and a three-digit line number.
        self.assertLessEqual(len("m:same:18446744073709551615:999".encode()), 64)

    def test_long_text_is_cut_to_telegrams_limit(self):
        telegram_api.send_message(CHAT, "x" * 5000)
        self.assertEqual(len(self.sent()[1]["text"]), telegram_api.MAX_MESSAGE_LENGTH)

    def test_answering_a_tap(self):
        self.result = True
        telegram_api.answer_callback_query("cb1", "Saved: 'loo roll' = Toilet paper")
        self.assertEqual(self.sent(), ("answerCallbackQuery", {
            "callback_query_id": "cb1", "text": "Saved: 'loo roll' = Toilet paper",
        }))

    def test_toast_is_cut_to_telegrams_limit(self):
        self.result = True
        telegram_api.answer_callback_query("cb1", "x" * 500)
        self.assertEqual(len(self.sent()[1]["text"]), 200)

    def test_replacing_and_removing_buttons(self):
        kept = [[{"text": "Same item", "callback_data": "m:same:8:1"}]]
        telegram_api.edit_buttons(CHAT, 77, kept)
        self.assertEqual(self.sent(), ("editMessageReplyMarkup", {
            "chat_id": CHAT, "message_id": 77, "reply_markup": {"inline_keyboard": kept},
        }))
        self.requests.clear()
        telegram_api.edit_buttons(CHAT, 77, [])
        self.assertEqual(self.sent()[1]["reply_markup"], {"inline_keyboard": []})

    def test_errors_never_leak_the_token(self):
        body = io.BytesIO(json.dumps({"ok": False, "description": "Bad Request: message is not modified"}).encode())
        self.result = urllib.error.HTTPError(f"https://api.telegram.org/bot{TOKEN}/x", 400, "Bad Request", {}, body)
        with self.assertRaises(telegram_api.TelegramError) as caught:
            telegram_api.edit_buttons(CHAT, 77, [])
        self.assertEqual(str(caught.exception), "editMessageReplyMarkup: HTTP 400 Bad Request: message is not modified")
        self.assertNotIn(TOKEN, str(caught.exception))
        self.assertIsNone(caught.exception.__cause__)


if __name__ == "__main__":
    unittest.main()
