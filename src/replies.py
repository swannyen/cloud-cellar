"""Message text the bot sends (PLAN.md §4.4)."""
from datetime import datetime

import model

HELP = """\
One entry per line, starting with what happened:
+ toilet paper 2x10 $12.90 @fairprice — bought (or: bought, buy, add)
- dish soap — finished (or: done, finished, out)
low shampoo — on the last one (or: last)
have rice 2x5kg — what's at home right now
? detergent 3kg $8.95 @shengsiong — price check (or: price)

How much:
2pcs, 10 rolls, 3kg, 500ml: that amount
2x10rolls: 2 packs of 10, and I remember the pack size
2, 2pkt, 2 boxes: packs, once I know the pack size. If I don't, I ask instead of guessing.

$12.90 or a plain 12.90 is the total price. The store goes last: @giant, or from/at a store I already know.

Commands:
/items — what I'm tracking
/undo — remove your last entry (send it again to go back further)
/alias loo roll = toilet paper — teach me a name
/set toilet paper pack 10 — set a pack size
/unit sticks = pcs — teach me a unit word
/brand kans — teach me a brand
/stats — how many lines needed AI this month"""

FORMAT_HINT = "Didn't catch that. Try: + rice 5kg $12 @ntuc — or /help for all formats."
FREE_TEXT_FAILED = "I couldn't read that just now. Please send it again, or use the format (/help)."
NO_ITEMS = "No items yet. Log one like this: + rice 5kg $12 @ntuc"
NOT_AVAILABLE_YET = "That command isn't available yet."
NOTHING_TO_UNDO = "Nothing to undo in the last 24 hours."
ALIAS_USAGE = "Use it like this: /alias loo roll = toilet paper"
SET_USAGE = "Use it like this: /set toilet paper pack 10"
UNIT_USAGE = "Use it like this: /unit sticks = pcs"
BRAND_USAGE = "Use it like this: /brand kans"
TOO_OLD_TO_CHANGE = "Too old to change — use /alias or /undo"
ENTRY_WAS_UNDONE = "That entry was undone, so there is nothing to change."
BUTTON_FAILED = "Sorry, that didn't work — please try again"

_UNIT_NAMES = {"piece": "pieces", "roll": "rolls", "sheet": "sheets", "g": "grams", "ml": "millilitres"}
_COUNT_UNITS = {"piece": "pieces", "roll": "rolls", "sheet": "sheets"}
# For the examples in questions: (a pack size, the unit word to type).
_EXAMPLES = {"piece": (5, "pcs"), "roll": (10, "rolls"), "sheet": (100, "sheets"), "g": (5, "kg"), "ml": (1, "L")}
_KEYWORDS = {"bought": "+", "stock_count": "have", "price_check": "?"}


def event_line(item, event, created=False, read_from=None, missed=()) -> str:
    """The one-line confirmation for a recorded event.

    read_from is the sender's phrase when the item was a guess (a close spelling or
    the AI's match). missed names what the AI gave that wasn't in the message.
    """
    text = describe(item, event, name_suffix=" (new item)" if created else "")
    tick = "" if event["action"] == "price_check" else "✓ "
    notes = f' (Read "{read_from}" as {item["name"]}.)' if read_from else ""
    notes += f" (Didn't catch the {' or '.join(missed)}.)" if missed else ""
    return f"{tick}{text}.{notes}"


def describe(item, event, name_suffix="") -> str:
    """What happened: "Toilet paper: bought 2 packs × 10 rolls (20 rolls) for $12.90 at FairPrice ($0.65/roll)"."""
    return f"{item['name']}{name_suffix}: {summary(item, event)}"


def summary(item, event) -> str:
    """describe without the item's name. The total is always stated in the item's base unit."""
    unit = item["base_unit"]
    amount = _amount(event, item)
    price = money(event["total_price"]) if event.get("total_price") else ""
    store = f" at {event['store']}" if event.get("store") else ""
    per_unit = unit_price(event["unit_price"], unit) if event.get("unit_price") else ""
    action = event["action"]
    if action == "bought":
        text = "bought" + _join(amount, f"for {price}" if price else "") + store
        return text + (f" ({per_unit})" if per_unit else "")
    if action == "price_check":
        text = price + (f" for {amount}" if amount else "")
        return text + (f" = {per_unit}" if per_unit else "") + store + ". Price noted"
    if action == "stock_count":
        return f"counted {amount} in stock" if amount else "noted as in stock"
    if action == "opened_last":
        return "marked as running low"  # also used for "running low", not only "opened the last one"
    if action == "finished":
        return "marked finished"
    return "noted, still have plenty"  # still_have


def undone(item_name, what) -> str:
    return f"Undid: {item_name}, {what}."


def ask_pack_size(item, entry, read_from=None) -> str:
    """The question sent instead of guessing what a pack count means for an item with no pack size."""
    unit = item["base_unit"]
    size, word = _EXAMPLES[unit]
    count = _number((entry["quantity"] or {}).get("packs", 1))
    price = f" {money(entry['total_price'])}" if entry.get("total_price") else ""
    store = f" @{entry['store']}" if entry.get("store") else ""
    named = " ".join(filter(None, [entry.get("brand"), entry["phrase"]])).lower()
    example = f"{_KEYWORDS.get(entry['action'], '+')} {named} {count}x{size}{word}{price}{store}"
    guess = f' (Read "{read_from}" as {item["name"]}.)' if read_from else ""
    if unit in _COUNT_UNITS:
        return (f"How many in each pack of {item['name']}? Resend like: {example} — "
                f"or write {count}{word} if you mean single {_COUNT_UNITS[unit]}.{guess}")
    return (f"How much is in each pack of {item['name']}? Resend like: {example} — "
            f"or write the total, like {size}{word}.{guess}")


def wrong_unit(item) -> str:
    """Sent when a quantity's unit can't count the item ("500ml" for something counted in pieces)."""
    unit = item["base_unit"]
    size, word = _EXAMPLES[unit]
    verb = "are" if item["name"].endswith("s") else "is"
    return f"{item['name']} {verb} counted in {_UNIT_NAMES[unit]}. Try 2{word} or 2x{size}{word}."


def lookup_failed(phrase) -> str:
    return f'Couldn\'t look up "{phrase}" just now, so it isn\'t recorded. Please send that line again.'


def alias_saved(alias, item, previous=None) -> str:
    moved = f" (It used to mean {previous['name']}.)" if previous else ""
    return f'Got it: "{alias}" now means {item["name"]}.{moved}'


def alias_already(alias, item) -> str:
    return f'"{alias}" already means {item["name"]}.'


def unknown_item(name) -> str:
    return f'I don\'t track "{name}" yet. /items shows what I track.'


def alias_is_a_name(alias, other) -> str:
    return f'"{alias}" is the name of another item ({other["name"]}), so I\'ve left it alone.'


def alias_full(item, limit) -> str:
    return f"{item['name']} already has {limit} other names, which is the most I keep."


def alias_toast(phrase, item) -> str:
    return f"Saved: '{phrase}' = {item['name']}"


def new_item_toast(item) -> str:
    return f"Made a new item: {item['name']}"


def pack_size_set(item) -> str:
    unit = item["base_unit"]
    size = item["pack_size"]
    return (f"{item['name']}: a pack is now {quantity(size, unit)}, "
            f"so a bare 2 means {quantity(2 * size, unit)}.")


def brand_saved(name) -> str:
    return (f'Got it: "{name}" is a brand. In "{name.lower()} facial masks" I\'ll read the item as '
            "facial masks and note the brand.")


def brand_already(name) -> str:
    return f'I already know "{name}" as a brand.'


def brand_is_an_item(name, item) -> str:
    return f'"{name}" is what I call an item ({item["name"]}), so it can\'t also be a brand.'


def brands_list(names) -> str:
    return "Brands I know: " + ", ".join(sorted(names, key=str.lower)) + ". Add one with /brand <name>."


def unit_saved(word, means) -> str:
    return f'Got it: "{word}" now means {means}, as in 2 {word}.'


def unit_unknown(target) -> str:
    return (f'I don\'t know "{target}" as a unit. Use one I know: pcs, rolls, sheets, g, kg, ml, l, '
            "or a pack word like pkt or box.")


def unit_taken(word) -> str:
    return f'"{word}" already means something to me, so I\'ve left it alone.'


def stats(month, counts) -> str:
    """The /stats reply. month is "yyyy-mm"."""
    title = datetime.strptime(month, "%Y-%m").strftime("%B %Y")
    return "\n".join([
        f"{title} so far:",
        f"• Lines read by the format: {counts['lines_structured']}",
        f"• AI item matches: {counts['ai_match_calls']}",
        f"• AI free-text fallbacks: {counts['ai_full_calls']}",
        f"• Lines I couldn't read: {counts['lines_rejected']}",
    ])


def items_list(items) -> str:
    if not items:
        return NO_ITEMS
    lines = ["Tracked items:"]
    for item in sorted(items, key=lambda i: i["name"].lower()):
        unit, pack_size = item["base_unit"], item.get("pack_size")
        pack = f", pack of {quantity(pack_size, unit)}" if pack_size else ""
        line = f"{item['item_id']} — {item['name']} ({unit}{pack})"
        status, stock = item.get("status"), item.get("stock_est")
        if status in ("ok", "low") and stock:
            packs = f" ({_packs(stock / pack_size)})" if pack_size else ""
            line += f": {status}, {quantity(stock, unit)}{packs}"
        elif status:
            line += f": {status}"
        lines.append(line)
    return "\n".join(lines)


def money(value) -> str:
    return f"${value:.2f}"


def quantity(amount, base_unit) -> str:
    """An amount in base units, e.g. "3.6 kg", "750 ml", "10 rolls"."""
    if base_unit in ("g", "ml"):
        large = "kg" if base_unit == "g" else "L"
        return f"{_number(amount / 1000)} {large}" if amount >= 1000 else f"{_number(amount)} {base_unit}"
    return f"{_number(amount)} {base_unit if amount == 1 else _COUNT_UNITS[base_unit]}"


def unit_price(value, base_unit) -> str:
    """Per roll/piece/sheet, or per 100 g / 100 ml (PLAN.md §8.2)."""
    if base_unit in ("g", "ml"):
        value, label = value * 100, f"100 {base_unit}"
    else:
        label = base_unit
    digits = 2 if value >= 0.095 else 3
    return f"${value:.{digits}f}/{label}"


def _amount(event, item) -> str:
    """How much, with the base-unit total: "2 pieces", "2 packs × 10 rolls (20 rolls)", "1 pack of 5 kg"."""
    unit = item["base_unit"]
    packs = event.get("packs")
    total = model.event_qty(event, item.get("pack_size"))
    if total is None:
        return _packs(packs) if packs else ""
    if not packs:
        return quantity(total, unit)
    if packs == 1:
        return f"1 pack of {quantity(total, unit)}"
    return f"{_packs(packs)} × {quantity(total / packs, unit)} ({quantity(total, unit)})"


def _packs(count) -> str:
    return f"{_number(count)} {'pack' if count == 1 else 'packs'}"


def _number(value) -> str:
    return f"{value:.2f}".rstrip("0").rstrip(".")


def _join(*parts) -> str:
    return "".join(f" {part}" for part in parts if part)
