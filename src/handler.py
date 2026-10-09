"""Webhook entry point: verify secret -> dedupe -> allowed-chat filter -> route.

Flow is PLAN.md §4.1. Phase 0 routes only /ping.
"""
import base64
import hmac
import json
import logging
import time

import boto3
from botocore.exceptions import ClientError

import config
import telegram_api

logger = logging.getLogger()
logger.setLevel(logging.INFO)

SECRET_HEADER = "x-telegram-bot-api-secret-token"  # function URLs lowercase header names
DEDUPE_TTL_SECONDS = 2 * 24 * 60 * 60
ERROR_REPLY = "Sorry, I couldn't process that — please try again"

_table = None


def lambda_handler(event, context):
    # If the secret can't be loaded this raises and the caller gets a 5xx:
    # nothing is processed without authentication.
    if not _secret_ok(event):
        logger.warning("rejected: missing or wrong secret header")
        return _response(403, {"ok": False})

    reply_chat = None  # set only once the chat has passed the allow-list
    try:
        update = _parse_update(event)
        update_id = update["update_id"]
        if not _first_delivery(update_id):
            logger.info("update %s: duplicate, skipped", update_id)
            return _response(200, {"ok": True})
        chat_id = _chat_id(update)
        if chat_id not in config.allowed_chat_ids():
            logger.info("update %s: chat %s not allowed, ignored", update_id, chat_id)
            return _response(200, {"ok": True})
        reply_chat = chat_id
        _route(update, chat_id)
    except Exception:
        # Always 200 from here on, so Telegram doesn't retry in a loop.
        logger.exception("failed to process update")
        if reply_chat is not None:
            _send_error_reply(reply_chat)
    return _response(200, {"ok": True})


def _secret_ok(event) -> bool:
    supplied = (event.get("headers") or {}).get(SECRET_HEADER)
    expected = config.secret("webhook-secret")
    if not supplied or not expected:
        return False
    return hmac.compare_digest(supplied.encode(), expected.encode())


def _parse_update(event) -> dict:
    body = event.get("body") or ""
    if event.get("isBase64Encoded"):
        body = base64.b64decode(body).decode("utf-8")
    return json.loads(body)


def _first_delivery(update_id) -> bool:
    """Record the update ID. False means it was already recorded (a Telegram retry)."""
    try:
        _dedupe_table().put_item(
            Item={
                "pk": f"UPD#{update_id}",
                "sk": "-",
                "ttl": int(time.time()) + DEDUPE_TTL_SECONDS,
            },
            ConditionExpression="attribute_not_exists(pk)",
        )
    except ClientError as err:
        if err.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return False
        raise
    return True


def _dedupe_table():
    global _table
    if _table is None:
        _table = boto3.resource("dynamodb").Table(config.table_name())
    return _table


def _chat_id(update):
    message = update.get("message") or (update.get("callback_query") or {}).get("message") or {}
    return (message.get("chat") or {}).get("id")


def _route(update, chat_id):
    text = (update.get("message") or {}).get("text") or ""
    if not text.startswith("/"):
        return
    command = text.split()[0].split("@")[0].lower()  # "/ping@BotName" -> "/ping"
    if command == "/ping":
        logger.info("update %s: /ping", update["update_id"])
        telegram_api.send_message(chat_id, "pong")


def _send_error_reply(chat_id):
    try:
        telegram_api.send_message(chat_id, ERROR_REPLY)
    except Exception:
        logger.exception("failed to send the error reply")


def _response(status: int, body: dict) -> dict:
    return {
        "statusCode": status,
        "headers": {"content-type": "application/json"},
        "body": json.dumps(body),
    }
