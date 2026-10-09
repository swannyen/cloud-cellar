"""Turn a chat message into structured events with Claude (PLAN.md §5)."""
import math
from dataclasses import dataclass, field

import claude_api

# Bump when SYSTEM_PROMPT changes, so logged results can be tied to a prompt.
PROMPT_VERSION = 2

ACTIONS = ("bought", "stock_count", "opened_last", "finished", "still_have", "price_check")
BASE_UNITS = ("piece", "roll", "sheet", "g", "ml")
MAX_PACKS = 100

_NULLABLE_NUMBER = {"type": ["number", "null"]}
_NULLABLE_STRING = {"type": ["string", "null"]}

# Every field is required (structured outputs limit optional fields); unknowns are null.
SCHEMA = {
    "type": "object",
    "properties": {
        "events": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": list(ACTIONS)},
                    "item_id": _NULLABLE_STRING,
                    "item_name": {"type": "string"},
                    "base_unit": {"type": "string", "enum": list(BASE_UNITS)},
                    "packs": _NULLABLE_NUMBER,
                    "units_per_pack": _NULLABLE_NUMBER,
                    "total_price_sgd": _NULLABLE_NUMBER,
                    "store": _NULLABLE_STRING,
                    "brand": _NULLABLE_STRING,
                },
                "required": [
                    "action", "item_id", "item_name", "base_unit", "packs",
                    "units_per_pack", "total_price_sgd", "store", "brand",
                ],
                "additionalProperties": False,
            },
        },
        "clarify": _NULLABLE_STRING,
    },
    "required": ["events", "clarify"],
    "additionalProperties": False,
}

SYSTEM_PROMPT = """\
You read messages from a Singapore family's Telegram group and extract household-supply events from them. The family uses the group to keep track of what they have at home: groceries, toiletries, cleaning supplies and kitchen supplies. A program turns your output into a stock ledger and a price book, and the bot replies in the group once for every event you return. So an event that isn't really there does more harm than a missed one: it corrupts the stock count and makes the bot interrupt ordinary conversation.

## When to return an event

Return an event only when the message reports something that has already happened to a supply, or asks whether a specific price is good.

- bought: someone bought it, or an order of it arrived.
- finished: it has run out ("finished", "used up", "ran out", "no more", "finish already", "habis").
- opened_last: the last pack is now in use, or it is nearly gone ("opened the last one", "last bottle", "running low", "almost out").
- stock_count: a count of what is at home right now ("we have 3 bags", "4 packs left").
- still_have: there is plenty and no need to buy, with no count given.
- price_check: a price that was seen or asked about but not paid ("is $8.95 for 3kg good?", "Giant selling it at $5, worth it?").

Return no events for everything else: greetings, chatter, jokes, meals and food that was eaten, things that are not consumable household supplies (tickets, appliances, clothes, furniture), and questions that are not price checks. Plans and requests are not events either. "Need to buy eggs", "can someone get milk?" and "going to NTUC later" are about the future, so they return nothing.

## Fields

Return one event per item per thing that happened. One message can produce several events.

- item_id: if the item is the same kind of product as one under "Known items", use that ID exactly, including when the sender uses a nickname or only a brand ("loo roll", "TP" -> toilet-paper). Otherwise null, which creates a new item. That is the normal way products get added, whatever the action: the list starts out empty and grows as the family talks.
- item_name: for a known item, the words the sender used for it ("loo roll"); if they named only a brand, repeat the known item's name. For a new item, a generic product name in sentence case ("Dishwashing liquid", "Soy sauce"), never a brand.
- brand: the brand if one is mentioned ("Mama Lemon", "Darlie"), otherwise null.
- base_unit: "g" for things sold by weight, "ml" for liquids and anything else sold by volume, "roll", "sheet", or "piece" for things sold by count. Go by how the product is sold, not by the container the message names: a jar of jam is still "g" and a bottle of floor cleaner is still "ml", even when no size is given. For a known item, use its base unit.
- packs: how many packs, bottles, bags, boxes or trays. One item with a stated size is 1 pack.
- units_per_pack: the size of one pack in base units. Convert kg to g and L to ml (x1000). "3 x 2L" -> packs 3, units_per_pack 2000. "pack of 10 rolls" -> packs 1, units_per_pack 10. "tray of 30 eggs" -> packs 1, units_per_pack 30.
- total_price_sgd: the total paid, or quoted, in Singapore dollars for all the packs in this event. "$3.20 each" for 2 packs -> 6.40. "2 for $9.90" -> 9.90. When a promotional price was paid, use it, not the usual price. When one total covers several different items it cannot be split, so leave the price null on each of them.
- store: where it was bought or seen, written the usual way ("Sheng Siong", "Giant", "Cold Storage", "Watsons", "Shopee"). NTUC and FairPrice are the same shop: write "FairPrice". A shop named once for a whole shopping trip applies to every item in the message. Otherwise null.

Leave any number the message does not give as null; do not guess it. For finished, opened_last and still_have, set packs, units_per_pack and total_price_sgd to null.

## clarify

The family reads this text exactly as you write it, as a message from the bot in their group chat. Use it only when the sender's words fit two or more different known items and nothing in the message says which one, for example "finished the big bottle" when several known items come in bottles. Return no event for that item, and ask what they mean in everyday words, naming the choices. Never mention IDs, the known items list or how you work.

A product that is not among the known items is never a reason to ask. Record it as a new item with item_id null. A missing size, count, price or shop is never a reason to ask either: leave those fields null. In every other case make your best guess and set "clarify" to null.

## Examples

Every message comes with its own list of known items, and so does each example below. Only the list given with a message counts for that message. A product that is in another example's list but not in the message's own list is not known: it is a new item, as the body wash example shows.

Known items (id | name | base unit | aliases):
toilet-paper | Toilet paper | roll | loo roll, TP
dish-soap | Dishwashing liquid | ml | dish soap
Message: Got 2 packs of TP from NTUC, 10 rolls each, $12.90. Also grabbed a 1L Mama Lemon for $3.20
Output: {"events":[{"action":"bought","item_id":"toilet-paper","item_name":"TP","base_unit":"roll","packs":2,"units_per_pack":10,"total_price_sgd":12.9,"store":"FairPrice","brand":null},{"action":"bought","item_id":"dish-soap","item_name":"Dishwashing liquid","base_unit":"ml","packs":1,"units_per_pack":1000,"total_price_sgd":3.2,"store":"FairPrice","brand":"Mama Lemon"}],"clarify":null}

Known items (id | name | base unit | aliases):
(none yet)
Message: bought Dynamo 3.6kg at Giant for 11.95
Output: {"events":[{"action":"bought","item_id":null,"item_name":"Laundry detergent","base_unit":"g","packs":1,"units_per_pack":3600,"total_price_sgd":11.95,"store":"Giant","brand":"Dynamo"}],"clarify":null}

Known items (id | name | base unit | aliases):
rice | Rice | g |
shampoo | Shampoo | ml |
Message: is $7.50 for 5kg Royal Umbrella rice at Sheng Siong ok?
Output: {"events":[{"action":"price_check","item_id":"rice","item_name":"rice","base_unit":"g","packs":1,"units_per_pack":5000,"total_price_sgd":7.5,"store":"Sheng Siong","brand":"Royal Umbrella"}],"clarify":null}

Known items (id | name | base unit | aliases):
toilet-paper | Toilet paper | roll | loo roll, TP
Message: anyone want bubble tea? I'm at Koufu
Output: {"events":[],"clarify":null}

Known items (id | name | base unit | aliases):
body-wash | Body wash | ml | shower gel
rice | Rice | g |
Message: shower gel finish
Output: {"events":[{"action":"finished","item_id":"body-wash","item_name":"shower gel","base_unit":"ml","packs":null,"units_per_pack":null,"total_price_sgd":null,"store":null,"brand":null}],"clarify":null}

Known items (id | name | base unit | aliases):
toilet-paper | Toilet paper | roll | loo roll, TP
rice | Rice | g |
Message: the body wash is finished
Output: {"events":[{"action":"finished","item_id":null,"item_name":"Body wash","base_unit":"ml","packs":null,"units_per_pack":null,"total_price_sgd":null,"store":null,"brand":null}],"clarify":null}

Known items (id | name | base unit | aliases):
toilet-paper | Toilet paper | roll | loo roll, TP
rice | Rice | g |
Message: this is the last bottle of soy sauce, just opened it
Output: {"events":[{"action":"opened_last","item_id":null,"item_name":"Soy sauce","base_unit":"ml","packs":null,"units_per_pack":null,"total_price_sgd":null,"store":null,"brand":null}],"clarify":null}

Known items (id | name | base unit | aliases):
(none yet)
Message: ran out of oyster sauce
Output: {"events":[{"action":"finished","item_id":null,"item_name":"Oyster sauce","base_unit":"ml","packs":null,"units_per_pack":null,"total_price_sgd":null,"store":null,"brand":null}],"clarify":null}

Known items (id | name | base unit | aliases):
rice | Rice | g |
Message: counted the storeroom: 6 cans of tuna
Output: {"events":[{"action":"stock_count","item_id":null,"item_name":"Canned tuna","base_unit":"piece","packs":6,"units_per_pack":null,"total_price_sgd":null,"store":null,"brand":null}],"clarify":null}

Known items (id | name | base unit | aliases):
shampoo | Shampoo | ml |
Message: I'll pick up shampoo on the way home tomorrow
Output: {"events":[],"clarify":null}

Known items (id | name | base unit | aliases):
rice | Rice | g |
Message: got bin liners at Giant, 2 packs for $5.50 (U.P. $3.20 each), 30 bags per pack
Output: {"events":[{"action":"bought","item_id":null,"item_name":"Trash bags","base_unit":"piece","packs":2,"units_per_pack":30,"total_price_sgd":5.5,"store":"Giant","brand":null}],"clarify":null}

Known items (id | name | base unit | aliases):
dish-soap | Dishwashing liquid | ml | dish soap
body-wash | Body wash | ml | shower gel
shampoo | Shampoo | ml |
Message: finished the big bottle
Output: {"events":[],"clarify":"What did you finish: the dishwashing liquid, body wash or shampoo?"}

Known items (id | name | base unit | aliases):
(none yet)
Message: Sheng Siong trip: bread and peanut butter, $7.80 altogether
Output: {"events":[{"action":"bought","item_id":null,"item_name":"Bread","base_unit":"piece","packs":null,"units_per_pack":null,"total_price_sgd":null,"store":"Sheng Siong","brand":null},{"action":"bought","item_id":null,"item_name":"Peanut butter","base_unit":"g","packs":null,"units_per_pack":null,"total_price_sgd":null,"store":"Sheng Siong","brand":null}],"clarify":null}

Known items (id | name | base unit | aliases):
rice | Rice | g |
toilet-paper | Toilet paper | roll | loo roll, TP
Message: there's a lot of rice left, enough for the month
Output: {"events":[{"action":"still_have","item_id":"rice","item_name":"rice","base_unit":"g","packs":null,"units_per_pack":null,"total_price_sgd":null,"store":null,"brand":null}],"clarify":null}

Known items (id | name | base unit | aliases):
rice | Rice | g |
Message: had nasi lemak for breakfast, the sambal damn shiok
Output: {"events":[],"clarify":null}
"""


@dataclass
class Parsed:
    events: list
    clarify: str | None
    usage: dict = field(default_factory=dict)
    model: str = ""


def parse(text, sender_name, items, today) -> Parsed:
    """Ask Claude for the events in one message. Raises claude_api.ClaudeError on failure."""
    user = build_user_content(text, sender_name, items, today)
    data, message = claude_api.complete_json(SYSTEM_PROMPT, user, SCHEMA)
    events, clarify = normalise(data, {item["item_id"] for item in items})
    return Parsed(events, clarify, message.get("usage") or {}, message.get("model") or "")


def build_user_content(text, sender_name, items, today) -> str:
    """The per-message prompt. today is a date in Singapore time."""
    lines = [
        f"Sender: {sender_name}",
        f"Today: {today.isoformat()} ({today.strftime('%a')})",
        "Known items (id | name | base unit | aliases):",
    ]
    for item in items:
        aliases = ", ".join(item.get("aliases") or [])
        lines.append(f"{item['item_id']} | {item['name']} | {item['base_unit']} | {aliases}".rstrip())
    if not items:
        lines.append("(none yet)")
    lines += ["", "Message:", text]
    return "\n".join(lines)


def normalise(data, known_ids):
    """Clean the model's output into (events, clarify).

    Events come back with keys: action, item_id, item_name, base_unit, packs,
    units_per_pack, total_price, store, brand.
    """
    events = []
    raw_events = data.get("events") if isinstance(data, dict) else None
    for raw in raw_events if isinstance(raw_events, list) else []:
        event = _normalise_event(raw, known_ids)
        if event is not None:
            events.append(event)
    clarify = _text(data.get("clarify")) if isinstance(data, dict) else None
    return events, clarify


def _normalise_event(raw, known_ids):
    if not isinstance(raw, dict):
        return None
    # Structured outputs don't guarantee enum capitalisation.
    action = (_text(raw.get("action")) or "").lower()
    name = _text(raw.get("item_name"))
    if action not in ACTIONS or not name:
        return None
    base_unit = (_text(raw.get("base_unit")) or "").lower()
    item_id = (_text(raw.get("item_id")) or "").lower()
    packs = _positive(raw.get("packs"))
    event = {
        "action": action,
        "item_id": item_id if item_id in known_ids else None,
        "item_name": name,
        "base_unit": base_unit if base_unit in BASE_UNITS else "piece",
        "packs": min(packs, MAX_PACKS) if packs is not None else None,
        "units_per_pack": _positive(raw.get("units_per_pack")),
        "total_price": _positive(raw.get("total_price_sgd")),
        "store": _text(raw.get("store")),
        "brand": _text(raw.get("brand")),
    }
    # A price check without a price has nothing to add to the price book.
    if action == "price_check" and event["total_price"] is None:
        return None
    return event


def _positive(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value if math.isfinite(value) and value > 0 else None


def _text(value):
    if not isinstance(value, str):
        return None
    return value.strip() or None
