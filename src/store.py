"""DynamoDB reads and writes for the single table (PLAN.md §3).

Callers work with plain dicts holding ints, floats and strings. The DynamoDB
details (Decimal numbers, pk/sk keys, dropping empty attributes) stay in here.
"""
import time
from decimal import Decimal

import boto3
from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

import config

DEDUPE_TTL_SECONDS = 2 * 24 * 60 * 60
UNDO_TTL_SECONDS = 24 * 60 * 60
PENDING_TTL_SECONDS = 7 * 24 * 60 * 60
ITEM_PK = "ITEM"
STORE_PK = "STORE"
UNIT_PK = "UNIT"
BRAND_PK = "BRAND"
STATS_PK = "STATS"
STAT_NAMES = ("lines_structured", "ai_match_calls", "ai_full_calls", "lines_rejected")

_table = None


def _get_table():
    global _table
    if _table is None:
        _table = boto3.resource("dynamodb").Table(config.table_name())
    return _table


# --- Dedupe markers ---------------------------------------------------------

def first_delivery(update_id) -> bool:
    """Record the update ID. False means it was already recorded (a Telegram retry)."""
    marker = {"ttl": int(time.time()) + DEDUPE_TTL_SECONDS}
    return _put(f"UPD#{update_id}", "-", marker, only_if_new=True)


# --- Items ------------------------------------------------------------------

def list_items() -> list:
    return [_as_item(record) for record in _query(ITEM_PK)]


def get_item(item_id):
    record = _get(ITEM_PK, item_id)
    return _as_item(record) if record else None


def create_item(item) -> bool:
    """Write a new item. False if its ID is already taken."""
    return _put(ITEM_PK, item["item_id"], _without_id(item), only_if_new=True)


def save_item(item):
    _put(ITEM_PK, item["item_id"], _without_id(item))


def delete_item(item_id):
    _delete(ITEM_PK, item_id)


# --- Events -----------------------------------------------------------------

def event_pk(item_id) -> str:
    return f"EVT#{item_id}"


def put_event(item_id, sk, event):
    _put(event_pk(item_id), sk, event)


def list_events(item_id) -> list:
    """The item's events, oldest first."""
    return _query(event_pk(item_id))


def get_event(pk, sk):
    return _get(pk, sk)


def delete_event(pk, sk):
    _delete(pk, sk)


# --- Each user's recent events (for /undo) -------------------------------------

def add_recent_event(user_id, sk, record):
    """Remember an event the user just logged. sk is the event's own sort key.

    DynamoDB removes the record some time after 24 hours; callers still check its age.
    """
    _put(f"USER#{user_id}", sk, {**record, "ttl": int(time.time()) + UNDO_TTL_SECONDS})


def list_recent_events(user_id) -> list:
    """The user's remembered events, oldest first."""
    return _query(f"USER#{user_id}")


def delete_recent_event(user_id, sk):
    _delete(f"USER#{user_id}", sk)


# --- Stores first used with "@" --------------------------------------------------

def list_stores() -> list:
    return [{**record, "store_id": record["sk"]} for record in _query(STORE_PK)]


def save_store(store_id, name, at):
    _put(STORE_PK, store_id, {"name": name, "aliases": [], "last_used_at": at})


def delete_store(store_id):
    _delete(STORE_PK, store_id)


# --- Unit words taught with /unit ------------------------------------------------

def list_units() -> dict:
    """Taught unit words: {word: the built-in unit word it means}."""
    return {record["sk"]: record["means"] for record in _query(UNIT_PK)}


def save_unit(word, means, added_by):
    _put(UNIT_PK, word, {"means": means, "added_by": added_by})


def delete_unit(word):
    _delete(UNIT_PK, word)


# --- Brands taught with /brand ----------------------------------------------------

def list_brands() -> list:
    """The names of the taught brands, as they were typed."""
    return [record["name"] for record in _query(BRAND_PK)]


def save_brand(brand_id, name, added_by):
    _put(BRAND_PK, brand_id, {"name": name, "added_by": added_by})


def delete_brand(brand_id):
    _delete(BRAND_PK, brand_id)


# --- Guessed matches waiting for a button tap -------------------------------------

def put_pending(update_id, line, record):
    _put(_pending_pk(update_id, line), "-", {**record, "ttl": int(time.time()) + PENDING_TTL_SECONDS})


def get_pending(update_id, line):
    return _get(_pending_pk(update_id, line), "-")


def delete_pending(update_id, line):
    _delete(_pending_pk(update_id, line), "-")


def _pending_pk(update_id, line) -> str:
    return f"PEND#{update_id}#{line}"


# --- Monthly usage counters (for /stats) ----------------------------------------

def add_stats(month, counts):
    """Add to the month's counters. month is "yyyy-mm"; counts maps STAT_NAMES to amounts.

    The update is atomic, so two messages handled at once are both counted.
    """
    counts = {name: amount for name, amount in counts.items() if amount}
    if not counts:
        return
    _get_table().update_item(
        Key={"pk": STATS_PK, "sk": month},
        UpdateExpression="ADD " + ", ".join(f"#n{i} :v{i}" for i in range(len(counts))),
        ExpressionAttributeNames={f"#n{i}": name for i, name in enumerate(counts)},
        ExpressionAttributeValues={f":v{i}": amount for i, amount in enumerate(counts.values())},
    )


def get_stats(month) -> dict:
    """The month's counters, with 0 for any that haven't been counted yet."""
    record = _get(STATS_PK, month) or {}
    return {name: record.get(name, 0) for name in STAT_NAMES}


# --- Table access -------------------------------------------------------------

def _put(pk, sk, attributes, only_if_new=False) -> bool:
    kwargs = {"Item": _to_db({**attributes, "pk": pk, "sk": sk})}
    if only_if_new:
        kwargs["ConditionExpression"] = "attribute_not_exists(pk)"
    try:
        _get_table().put_item(**kwargs)
    except ClientError as err:
        if only_if_new and err.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return False
        raise
    return True


# Reads are strongly consistent: an event is replayed right after it is written.
def _get(pk, sk):
    record = _get_table().get_item(Key={"pk": pk, "sk": sk}, ConsistentRead=True).get("Item")
    return _from_db(record) if record else None


def _query(pk) -> list:
    kwargs = {"KeyConditionExpression": Key("pk").eq(pk), "ConsistentRead": True}
    records = []
    while True:
        page = _get_table().query(**kwargs)
        records.extend(page["Items"])
        if "LastEvaluatedKey" not in page:
            return [_from_db(record) for record in records]
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]


def _delete(pk, sk):
    _get_table().delete_item(Key={"pk": pk, "sk": sk})


def _as_item(record) -> dict:
    item = {key: value for key, value in record.items() if key not in ("pk", "sk")}
    item["item_id"] = record["sk"]
    item.setdefault("aliases", [])
    return item


def _without_id(item) -> dict:
    return {key: value for key, value in item.items() if key != "item_id"}


def _to_db(value):
    """boto3 rejects floats, and unknown (None) attributes are simply left out."""
    if isinstance(value, float):
        return Decimal(str(value))
    if isinstance(value, dict):
        return {key: _to_db(inner) for key, inner in value.items() if inner is not None}
    if isinstance(value, (list, tuple)):
        return [_to_db(inner) for inner in value]
    return value


def _from_db(value):
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, dict):
        return {key: _from_db(inner) for key, inner in value.items()}
    if isinstance(value, list):
        return [_from_db(inner) for inner in value]
    return value
