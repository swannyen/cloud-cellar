"""Telegram Bot API calls over urllib (no third-party client)."""
import json
import urllib.error
import urllib.request

import config

API_BASE = "https://api.telegram.org"
TIMEOUT_SECONDS = 10
MAX_MESSAGE_LENGTH = 4096  # Telegram's limit for one message


class TelegramError(Exception):
    """A Bot API call failed. The message never includes the bot token."""


def send_message(chat_id: int, text: str, reply_to: int | None = None) -> dict:
    """Send plain text, optionally as a reply to one of the chat's messages."""
    if len(text) > MAX_MESSAGE_LENGTH:
        text = text[: MAX_MESSAGE_LENGTH - 1] + "…"
    payload = {"chat_id": chat_id, "text": text}
    if reply_to is not None:
        # Still send if the original message has been deleted in the meantime.
        payload["reply_parameters"] = {"message_id": reply_to, "allow_sending_without_reply": True}
    return _call("sendMessage", payload)


def _call(method: str, payload: dict) -> dict:
    request = urllib.request.Request(
        f"{API_BASE}/bot{config.secret('telegram-token')}/{method}",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    # The request URL contains the token, so errors are re-raised without the
    # original exception attached ("from None") to keep it out of the logs.
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            body = json.load(response)
    except urllib.error.HTTPError as err:
        raise TelegramError(f"{method}: HTTP {err.code} {_description(err)}") from None
    except OSError as err:  # URLError, timeouts, connection resets
        raise TelegramError(f"{method}: {type(err).__name__}") from None
    if not body.get("ok"):
        raise TelegramError(f"{method}: {body.get('description', 'not ok')}")
    return body["result"]


def _description(err: urllib.error.HTTPError) -> str:
    """Telegram explains a failed call in the JSON body's "description"."""
    try:
        return json.load(err).get("description", "")
    except ValueError:
        return ""
