"""The two AI calls: the small item match (PLAN.md §5.3) and the free-text
fallback (§5.4 to §5.7).

The AI is never trusted with facts that are written in the message: stores,
prices and quantities are read by code, and ground_free_text drops anything
the fallback returns that isn't in the text.
"""
import math
import re
from collections import Counter
from dataclasses import dataclass, field

import claude_api
import matcher
import structured

ACTIONS = ("bought", "used", "stock_count", "opened_last", "finished", "still_have", "price_check")
BASE_UNITS = ("piece", "roll", "sheet", "g", "ml")
MAX_NAME_LENGTH = 60
ITEM_MATCH_MAX_TOKENS = 100

_NULLABLE_NUMBER = {"type": ["number", "null"]}
_NULLABLE_STRING = {"type": ["string", "null"]}


# --- Item match: which tracked item does this phrase mean? (§5.3) ---------------

# Every field is required (structured outputs limit optional fields).
ITEM_MATCH_SCHEMA = {
    "type": "object",
    "properties": {
        "item_id": _NULLABLE_STRING,
        "new_item_name": {"type": "string"},
        "base_unit": {"type": "string", "enum": list(BASE_UNITS)},
    },
    "required": ["item_id", "new_item_name", "base_unit"],
    "additionalProperties": False,
}

# Kept short on purpose: this prompt is sent with every phrase the matcher can't place.
ITEM_MATCH_PROMPT = """\
Match a household product phrase from a Singapore family's chat to one of their tracked items. 
Stock and prices are recorded against the item you choose, so a wrong merge does more harm than a duplicate, which the family removes with one tap.

item_id: an existing item only if the phrase is the same product for the same use. Brand, size and spelling differences don't matter ("loo roll" = Toilet paper), and a brand on its own stands for the product it is known for ("Dynamo" = Laundry detergent). If a describing word changes what the product is or what it's for, it is a different item: "facial masks" (skincare) and "hand masks" are not Face masks (the kind worn over the nose and mouth), "hand soap" is not Dishwashing liquid, "baby wipes" is not Kitchen wipes. If unsure, null.
new_item_name: with a null item_id, a short generic name for a new item in sentence case, keep the describing word ("Hand masks") and leave out the brand. If the phrase is not a household product at all (ordinary conversation, say), leave it empty and nothing is recorded. With an item_id, repeat that item's name.
base_unit: how it is sold: "g" by weight, "ml" for liquids, "roll", "sheet", or "piece" for other things sold by count.
"""


@dataclass
class ItemMatch:
    item_id: str | None   # a tracked item's ID, or None when the phrase is something new
    name: str             # the name for a new item; "" when the phrase isn't a household product
    base_unit: str
    usage: dict = field(default_factory=dict)


def match_item(phrase, items) -> ItemMatch:
    """Ask Claude which tracked item a phrase means. Raises claude_api.ClaudeError on failure."""
    data, message = claude_api.complete_json(
        ITEM_MATCH_PROMPT, item_match_content(phrase, items), ITEM_MATCH_SCHEMA,
        max_tokens=ITEM_MATCH_MAX_TOKENS,
    )
    match = normalise_item_match(data, {item["item_id"] for item in items})
    match.usage = message.get("usage") or {}
    return match


def item_match_content(phrase, items) -> str:
    """The per-phrase prompt: just IDs and names, to keep the call small."""
    lines = ["Items (id | name):"]
    lines += [f"{item['item_id']} | {item['name']}" for item in items] or ["(none yet)"]
    lines += ["", f"Phrase: {phrase}"]
    return "\n".join(lines)


def normalise_item_match(data, known_ids) -> ItemMatch:
    """Clean the model's answer. An ID that isn't a tracked item counts as a new item."""
    data = data if isinstance(data, dict) else {}
    # Structured outputs don't guarantee enum capitalisation.
    item_id = (_text(data.get("item_id")) or "").lower()
    base_unit = (_text(data.get("base_unit")) or "").lower()
    return ItemMatch(
        item_id=item_id if item_id in known_ids else None,
        name=(_text(data.get("new_item_name")) or "")[:MAX_NAME_LENGTH].strip(),
        base_unit=base_unit if base_unit in BASE_UNITS else "piece",
    )


# --- Free-text fallback: the full parse of a message that isn't in the format (§5.4) ---

# A free-text line is only sent to Claude if it has a number, a price, a tracked
# item's name or one of these, so ordinary chat costs nothing.
LOG_WORDS = (
    "bought", "finished", "finish", "used up", "ran out", "run out", "running out",
    "running low", "no more", "habis", "restocked",
)
_LOG_SIGNS = re.compile(r"[\d$]|\b(?:" + "|".join(LOG_WORDS) + r")\b", re.IGNORECASE)

# A price next to one of these is one total for everything in the message ("$31.40 total").
_TOTAL_WORDS = re.compile(
    r"\b(?:total|altogether|all together|in all|for everything|for both|for the lot|combined)\b|一共|总共"
)
_TOTAL_TOKEN = re.compile(r"\$?\d+(?:\.\d+)?|[^\W\d_]+|[,;:()]|\.(?!\d)")  # a number, a word, or the end of a clause
TOTAL_REACH = 4  # how many words may lie between the price and the word
PRICED_ACTIONS = ("bought", "price_check")  # the entries a price can belong to

# Bump when FREE_TEXT_PROMPT changes, so logged results can be tied to a prompt.
FREE_TEXT_PROMPT_VERSION = 7

# Every field is required (structured outputs limit optional fields); unknowns are null.
FREE_TEXT_SCHEMA = {
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
                    "quantity_text": _NULLABLE_STRING,
                    "total_price_sgd": _NULLABLE_NUMBER,
                    "store": _NULLABLE_STRING,
                    "brand": _NULLABLE_STRING,
                },
                "required": [
                    "action", "item_id", "item_name", "base_unit", "quantity_text",
                    "total_price_sgd", "store", "brand",
                ],
                "additionalProperties": False,
            },
        },
        "clarify": _NULLABLE_STRING,
    },
    "required": ["events", "clarify"],
    "additionalProperties": False,
}

FREE_TEXT_PROMPT = """\
You read messages from a Singapore family's Telegram group and extract household-supply events from them. The family uses the group to keep track of what they have at home: groceries, toiletries, cleaning supplies and kitchen supplies. A program turns your output into a stock ledger and a price book, and the bot replies in the group once for every event you return. So an event that isn't really there does more harm than a missed one: it corrupts the stock count and makes the bot interrupt ordinary conversation.

The program checks the store, the price and the quantity you return against the message, and throws away anything that isn't written there. So copy those from the message; never work them out, convert them or fill them in from what you know.

## When to return an event

Return an event only when the message reports something that has already happened to a supply, or asks whether a specific price is good.

- bought: someone bought it, or an order of it arrived.
- used: some of it was used and an amount is given, but it has not run out ("used 2 rolls", "took 3 packets to the office"). Without an amount this is not an event.
- finished: it has run out ("finished", "used up", "ran out", "no more", "finish").
- opened_last: the last pack is now in use, or it is nearly gone ("opened the last one", "last bottle", "running low", "almost out").
- stock_count: a count of what is at home right now ("we have 3 bags", "4 packs left").
- still_have: there is plenty and no need to buy, with no count given.
- price_check: a price that was seen or asked about but not paid ("is $8.95 for 3kg good?", "Giant selling it at $5, worth it?").

Return no events for everything else: greetings, chatter, jokes, meals and food that was eaten, things that are not consumable household supplies (tickets, appliances, clothes, furniture), and questions that are not price checks. Plans and requests are not events either. "Need to buy eggs", "can someone get milk?" and "going to NTUC later" are about the future, so they return nothing.

## Fields

Return one event per item per thing that happened. One message can produce several events.

- item_id: the ID of an item under "Known items", exactly as listed, only when the message means the same product for the same use. Brand, size and spelling differences don't matter, and nicknames count ("loo roll", "TP" -> toilet-paper). A describing word that changes what the product is or what it's for means a different item: "facial masks" (skincare) and "hand masks" are not face-masks (the kind worn over the nose and mouth), and "hand soap" is not dish-soap. If unsure, or if nothing listed is that product, null, which creates a new item. That is the normal way products get added, whatever the action: the list starts out empty and grows as the family talks.
- item_name: for a known item, the words the sender used for it ("loo roll"); if they named only a brand, repeat the known item's name. For a new item, a generic product name in sentence case that keeps the describing word ("Hand masks", "Dishwashing liquid", "Soy sauce"), never a brand.
- brand: the brand if one is written in the message ("Mama Lemon", "Darlie"), otherwise null.
- base_unit: "g" for things sold by weight, "ml" for liquids and anything else sold by volume, "roll", "sheet", or "piece" for things sold by count. Go by how the product is sold, not by the container the message names: a jar of jam is still "g" and a bottle of floor cleaner is still "ml". For a known item, use its base unit.
- quantity_text: the quantity words copied exactly as written, as one continuous piece of the message: "5kg", "35 sticks", "3 x 2L", "pack of 10 rolls", "6 cans". When the number of packs and the size of a pack are written apart, copy the whole stretch from the first to the last, with the words in between ("2 packs toilet paper 10 rolls each"). Don't convert units, multiply or decide what a number counts. If the message gives no quantity for this item, null. For finished, opened_last and still_have, null; "used" always has one.
- total_price_sgd: the total price written in the message for this event, as a number: "$12.90" -> 12.9, "2 for $9.90" -> 9.9. When a promotional price was paid, use it, not the usual price. Only a total that is written counts. If the message gives just a price per pack ("$3.20 each" for two), or one total covering several different items, null.
- store: the shop copied as it is written in the message ("ntuc", "Sheng Siong", "taobao"), otherwise null. Never replace it with another shop or another name for it. A shop named once for a whole shopping trip applies to every item in the message.

## clarify

The family reads this text exactly as you write it, as a message from the bot in their group chat. Use it only when the sender's words fit two or more different known items and nothing in the message says which one, for example "finished the big bottle" when several known items come in bottles. Return no event for that item, and ask what they mean in everyday words, naming the choices. Never mention IDs, the known items list or how you work.

A product that is not among the known items is never a reason to ask. Record it as a new item with item_id null. A missing size, count, price or shop is never a reason to ask either: leave those fields null. In every other case make your best guess and set "clarify" to null.

## Examples

Every message comes with its own list of known items, and so does each example below. Only the list given with a message counts for that message. A product that is in another example's list but not in the message's own list is not known: it is a new item, as the body wash example shows.

Known items (id | name | base unit | aliases):
toilet-paper | Toilet paper | roll | loo roll, TP
dish-soap | Dishwashing liquid | ml | dish soap
Message: Got 2 packs of TP from NTUC, 10 rolls each, $12.90. Also grabbed a 1L Mama Lemon for $3.20
Output: {"events":[{"action":"bought","item_id":"toilet-paper","item_name":"TP","base_unit":"roll","quantity_text":"2 packs of TP from NTUC, 10 rolls each","total_price_sgd":12.9,"store":"NTUC","brand":null},{"action":"bought","item_id":"dish-soap","item_name":"Dishwashing liquid","base_unit":"ml","quantity_text":"1L","total_price_sgd":3.2,"store":"NTUC","brand":"Mama Lemon"}],"clarify":null}

Known items (id | name | base unit | aliases):
(none yet)
Message: bought Dynamo 3.6kg at Giant for 11.95
Output: {"events":[{"action":"bought","item_id":null,"item_name":"Laundry detergent","base_unit":"g","quantity_text":"3.6kg","total_price_sgd":11.95,"store":"Giant","brand":"Dynamo"}],"clarify":null}

Known items (id | name | base unit | aliases):
rice | Rice | g |
shampoo | Shampoo | ml |
Message: is $7.50 for 5kg Royal Umbrella rice at Sheng Siong ok?
Output: {"events":[{"action":"price_check","item_id":"rice","item_name":"rice","base_unit":"g","quantity_text":"5kg","total_price_sgd":7.5,"store":"Sheng Siong","brand":"Royal Umbrella"}],"clarify":null}

Known items (id | name | base unit | aliases):
toilet-paper | Toilet paper | roll | loo roll, TP
Message: anyone want bubble tea? I'm at Koufu
Output: {"events":[],"clarify":null}

Known items (id | name | base unit | aliases):
body-wash | Body wash | ml | shower gel
rice | Rice | g |
Message: shower gel finish
Output: {"events":[{"action":"finished","item_id":"body-wash","item_name":"shower gel","base_unit":"ml","quantity_text":null,"total_price_sgd":null,"store":null,"brand":null}],"clarify":null}

Known items (id | name | base unit | aliases):
toilet-paper | Toilet paper | roll | loo roll, TP
rice | Rice | g |
Message: the body wash is finished
Output: {"events":[{"action":"finished","item_id":null,"item_name":"Body wash","base_unit":"ml","quantity_text":null,"total_price_sgd":null,"store":null,"brand":null}],"clarify":null}

Known items (id | name | base unit | aliases):
hair-conditioner | Hair conditioner | ml | conditioner
wet-wipes | Wet wipes | piece |
Message: ordered fabric conditioner 2 bottles 3.10 from taobao
Output: {"events":[{"action":"bought","item_id":null,"item_name":"Fabric conditioner","base_unit":"ml","quantity_text":"2 bottles","total_price_sgd":3.1,"store":"taobao","brand":null}],"clarify":null}

Known items (id | name | base unit | aliases):
toilet-paper | Toilet paper | roll | loo roll, TP
rice | Rice | g |
Message: this is the last bottle of soy sauce, just opened it
Output: {"events":[{"action":"opened_last","item_id":null,"item_name":"Soy sauce","base_unit":"ml","quantity_text":null,"total_price_sgd":null,"store":null,"brand":null}],"clarify":null}

Known items (id | name | base unit | aliases):
(none yet)
Message: ran out of oyster sauce
Output: {"events":[{"action":"finished","item_id":null,"item_name":"Oyster sauce","base_unit":"ml","quantity_text":null,"total_price_sgd":null,"store":null,"brand":null}],"clarify":null}

Known items (id | name | base unit | aliases):
rice | Rice | g |
Message: counted the storeroom: 6 cans of tuna
Output: {"events":[{"action":"stock_count","item_id":null,"item_name":"Canned tuna","base_unit":"piece","quantity_text":"6 cans","total_price_sgd":null,"store":null,"brand":null}],"clarify":null}

Known items (id | name | base unit | aliases):
kitchen-towel | Kitchen towel | roll | kitchen roll
Message: used 2 rolls of kitchen towel cleaning up the spill
Output: {"events":[{"action":"used","item_id":"kitchen-towel","item_name":"kitchen towel","base_unit":"roll","quantity_text":"2 rolls","total_price_sgd":null,"store":null,"brand":null}],"clarify":null}

Known items (id | name | base unit | aliases):
shampoo | Shampoo | ml |
Message: I'll pick up shampoo on the way home tomorrow
Output: {"events":[],"clarify":null}

Known items (id | name | base unit | aliases):
shampoo | Shampoo | ml |
Message: 2 bottles of shampoo at $4.50 each from Watsons
Output: {"events":[{"action":"bought","item_id":"shampoo","item_name":"shampoo","base_unit":"ml","quantity_text":"2 bottles","total_price_sgd":null,"store":"Watsons","brand":null}],"clarify":null}

Known items (id | name | base unit | aliases):
rice | Rice | g |
Message: got bin liners at Giant, 2 packs of 30 pcs for $5.50 (U.P. $6.40)
Output: {"events":[{"action":"bought","item_id":null,"item_name":"Bin liners","base_unit":"piece","quantity_text":"2 packs of 30 pcs","total_price_sgd":5.5,"store":"Giant","brand":null}],"clarify":null}

Known items (id | name | base unit | aliases):
dish-soap | Dishwashing liquid | ml | dish soap
body-wash | Body wash | ml | shower gel
shampoo | Shampoo | ml |
Message: finished the big bottle
Output: {"events":[],"clarify":"What did you finish: the dishwashing liquid, body wash or shampoo?"}

Known items (id | name | base unit | aliases):
(none yet)
Message: Sheng Siong trip: bread and peanut butter, $7.80 altogether
Output: {"events":[{"action":"bought","item_id":null,"item_name":"Bread","base_unit":"piece","quantity_text":null,"total_price_sgd":null,"store":"Sheng Siong","brand":null},{"action":"bought","item_id":null,"item_name":"Peanut butter","base_unit":"g","quantity_text":null,"total_price_sgd":null,"store":"Sheng Siong","brand":null}],"clarify":null}

Known items (id | name | base unit | aliases):
rice | Rice | g |
toilet-paper | Toilet paper | roll | loo roll, TP
Message: there's a lot of rice left, enough for the month
Output: {"events":[{"action":"still_have","item_id":"rice","item_name":"rice","base_unit":"g","quantity_text":null,"total_price_sgd":null,"store":null,"brand":null}],"clarify":null}

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


def looks_like_log(line, items) -> bool:
    """Whether a line that isn't in the format is worth a fallback call."""
    return bool(_LOG_SIGNS.search(line)) or matcher.mentions(line, items)


def parse_free_text(text, sender_name, items, today) -> Parsed:
    """Ask Claude for the events in free text. Raises claude_api.ClaudeError on failure.

    The events are as the model gave them, cleaned up. Pass them through
    ground_free_text before recording anything.
    """
    user = free_text_content(text, sender_name, items, today)
    data, message = claude_api.complete_json(FREE_TEXT_PROMPT, user, FREE_TEXT_SCHEMA)
    events, clarify = normalise_free_text(data, {item["item_id"] for item in items})
    return Parsed(events, clarify, message.get("usage") or {}, message.get("model") or "")


def free_text_content(text, sender_name, items, today) -> str:
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


def normalise_free_text(data, known_ids):
    """Clean the model's output into (events, clarify).

    Events come back with keys: action, item_id, item_name, base_unit,
    quantity_text, total_price, store, brand.
    """
    events = []
    raw_events = data.get("events") if isinstance(data, dict) else None
    for raw in raw_events if isinstance(raw_events, list) else []:
        event = _normalise_event(raw, known_ids)
        if event is not None:
            events.append(event)
    clarify = _text(data.get("clarify")) if isinstance(data, dict) else None
    return events, clarify


def ground_free_text(events, text, stores=(), units=None) -> list:
    """Turn the fallback's events into entries for apply.py, keeping only what is in the message.

    text is the free text the events were read from; stores and units are as
    for structured.parse_line. The store comes from code where code can read
    it. A store, price or quantity the model gave that isn't written in the
    text is dropped and named in the entry's "missed", so the reply can say so.
    A total for several items (see shared_totals) is dropped too. The quantity
    words are then read by the format's own rules.
    """
    stores_read = [
        found for line in text.splitlines() if (found := structured.find_store(line, stores))[0]
    ]
    # A price counts once for each time it is written: one total put on three items backs none of them.
    written = Counter(round(number, 2) for number in structured.numbers_in(text))
    used = Counter(round(event["total_price"], 2) for event in events if event["total_price"] is not None)
    shared = shared_totals(events, text)
    entries = []
    for event in events:
        missed = []
        store, store_is_new = _grounded_store(event["store"], text, stores_read, stores, missed)
        price = event["total_price"]
        if price is not None and round(price, 2) in shared:
            price = None  # not "missed": the reply says once that the total covers several items
        elif price is not None and not 0 < used[round(price, 2)] <= written[round(price, 2)]:
            price = None
            missed.append("price")
        quantity = None
        if event["quantity_text"]:
            if _quoted_from(event["quantity_text"], text):
                quantity = structured.read_quantity(event["quantity_text"], units)
            if quantity is None:
                missed.append("quantity")
        brand = event["brand"]
        if brand and _squeeze(brand) not in _squeeze(text):
            brand = None
        if event["action"] == "price_check" and price is None:
            continue  # a price check without a price has nothing to add to the price book
        if event["action"] == "used" and quantity is None:
            continue  # nothing to take off the stock
        entries.append({
            "action": event["action"],
            "item_id": event["item_id"],
            "phrase": event["item_name"],
            "base_unit": event["base_unit"],
            "quantity": quantity,
            "total_price": price,
            "store": store,
            "store_is_new": store_is_new,
            "brand": brand,
            "missed": tuple(missed),
            "raw": text,
            "free_text": True,
        })
    return entries


def shared_totals(events, text) -> list:
    """The prices in a message that are one total for several items ("eggs, milk and rice. $31.40 total").

    events are what the model returned for the text. Nobody can say what each
    item cost, so no entry keeps such a price. With one purchase in the
    message, a total is just that purchase's price: [].
    """
    if sum(event["action"] in PRICED_ACTIONS for event in events) < 2:
        return []
    totals = []
    for line in text.lower().splitlines():
        for word in _TOTAL_WORDS.finditer(line):
            after = _nearest_number(_TOTAL_TOKEN.findall(line[word.end():]))
            before = _nearest_number(reversed(_TOTAL_TOKEN.findall(line[:word.start()])))
            # "eggs $5, total $17": the word goes with one number. That is the one in its own
            # clause, then the nearest, then the one written as a price, then the one after it.
            numbers = [found for found in (after, before) if found]
            if numbers:
                number = min(numbers, key=lambda found: found[:3])[3]
                amount = round(float(number.lstrip("$")), 2)
                if amount not in totals:
                    totals.append(amount)
    return totals


def _nearest_number(tokens):
    """(whether punctuation lies between, words in between, not written as a price, the number), or None."""
    words, punctuated = 0, False
    for token in tokens:
        if token[-1].isdigit():
            return punctuated, words, not (token.startswith("$") or "." in token), token
        if not token[0].isalpha():
            punctuated = True
        elif words == TOTAL_REACH:
            return None
        else:
            words += 1
    return None


def _grounded_store(named, text, stores_read, stores, missed):
    """The store for an event: (name, whether it is new). named is what the model returned."""
    distinct = list(dict.fromkeys(stores_read))
    if len(distinct) == 1:
        return distinct[0]  # code read exactly one store in the message, so that is the one
    if not named:
        return None, False
    names = structured.store_names(stores)
    known = names.get(structured.squash(named))
    if distinct:
        # Several stores are written: the model may pick between them, nothing else.
        for name, is_new in distinct:
            if name == known or structured.squash(name) == structured.squash(named):
                return name, is_new
    else:
        written = _phrases(text)
        if structured.squash(named) in written:
            return known or structured.title_store(named), False
        if known and any(spelling in written for spelling, name in names.items() if name == known):
            return known, False
    missed.append("store")
    return None, False


def _phrases(text) -> set:
    """Every run of one to three words in the text, squashed, for finding a store name written in it."""
    words = re.sub(r"[\W_]+", " ", text.lower()).split()
    return {"".join(words[start:start + length]) for length in (1, 2, 3) for start in range(len(words))}


def _squeeze(text) -> str:
    return " ".join(text.lower().split())


def _quoted_from(quoted, text) -> bool:
    """Whether every word of a quotation is in the text, in the same order.

    The model tends to skip the words between a pack count and a pack size
    ("2 packs, 6 rolls per pack"), so gaps are allowed; words that aren't in the
    message are not.
    """
    remaining = iter(_bare_words(text))
    return all(word in remaining for word in _bare_words(quoted))


def _bare_words(text) -> list:
    words = (word.strip(".,;:!?()\"'") for word in text.lower().split())
    return [word for word in words if word]


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
    return {
        "action": action,
        "item_id": item_id if item_id in known_ids else None,
        "item_name": name[:MAX_NAME_LENGTH].strip(),
        "base_unit": base_unit if base_unit in BASE_UNITS else "piece",
        "quantity_text": _text(raw.get("quantity_text")),
        "total_price": _positive(raw.get("total_price_sgd")),
        "store": _text(raw.get("store")),
        "brand": _text(raw.get("brand")),
    }


def _positive(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value if math.isfinite(value) and value > 0 else None


def _text(value):
    if not isinstance(value, str):
        return None
    return value.strip() or None
