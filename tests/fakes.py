"""Shared test helpers. Importing this module also puts src/ on sys.path."""
import copy
import sys
from pathlib import Path

from botocore.exceptions import ClientError

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


class FakeTable:
    """In-memory DynamoDB table covering the calls store.py makes.

    Like boto3 it refuses floats, so a missed Decimal conversion fails the test.
    """

    def __init__(self):
        self.rows = {}
        self.fail_with = None   # an error code to raise from every call
        self.page_size = None   # set to exercise query pagination

    def put_item(self, Item, ConditionExpression=None):
        self._maybe_fail("PutItem")
        _reject_floats(Item)
        key = (Item["pk"], Item["sk"])
        if ConditionExpression is not None:
            assert ConditionExpression == "attribute_not_exists(pk)", ConditionExpression
            if key in self.rows:
                raise _client_error("ConditionalCheckFailedException", "PutItem")
        self.rows[key] = copy.deepcopy(Item)
        return {}

    def get_item(self, Key, ConsistentRead=False):
        self._maybe_fail("GetItem")
        row = self.rows.get((Key["pk"], Key["sk"]))
        return {"Item": copy.deepcopy(row)} if row else {}

    def delete_item(self, Key):
        self._maybe_fail("DeleteItem")
        self.rows.pop((Key["pk"], Key["sk"]), None)
        return {}

    def query(self, KeyConditionExpression, ConsistentRead=False, ExclusiveStartKey=None):
        self._maybe_fail("Query")
        expression = KeyConditionExpression.get_expression()
        key, value = expression["values"]
        assert expression["operator"] == "=" and key.name == "pk", expression
        rows = sorted((row for (pk, _), row in self.rows.items() if pk == value), key=lambda r: r["sk"])
        if ExclusiveStartKey:
            rows = [row for row in rows if row["sk"] > ExclusiveStartKey["sk"]]
        page = rows[: self.page_size] if self.page_size else rows
        result = {"Items": copy.deepcopy(page)}
        if len(page) < len(rows):
            result["LastEvaluatedKey"] = {"pk": value, "sk": page[-1]["sk"]}
        return result

    def keys(self, pk_prefix=""):
        return sorted(key for key in self.rows if key[0].startswith(pk_prefix))

    def _maybe_fail(self, operation):
        if self.fail_with:
            raise _client_error(self.fail_with, operation)


def _client_error(code, operation):
    return ClientError({"Error": {"Code": code, "Message": code}}, operation)


def _reject_floats(value):
    if isinstance(value, float):
        raise TypeError("Float types are not supported. Use Decimal types instead.")
    if isinstance(value, dict):
        for inner in value.values():
            _reject_floats(inner)
    elif isinstance(value, (list, tuple)):
        for inner in value:
            _reject_floats(inner)


def parsed_event(action="bought", item_id=None, item_name="Toilet paper", base_unit="roll", **fields):
    """An event shaped like parser.normalise() output."""
    event = {
        "action": action,
        "item_id": item_id,
        "item_name": item_name,
        "base_unit": base_unit,
        "packs": None,
        "units_per_pack": None,
        "total_price": None,
        "store": None,
        "brand": None,
    }
    event.update(fields)
    return event
