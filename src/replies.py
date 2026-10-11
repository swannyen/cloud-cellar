"""Message text the bot sends (PLAN.md §4.4)."""
from datetime import datetime

import model

HELP = """\
Log one thing per line. Start the line with what happened:

+ toilet paper 2x10 $12.90 @fairprice — bought
- toilet paper 2 — used 2 packs
- dish soap — ran out, nothing left
low shampoo — on the last one
have rice 2x5kg — count what's at home
? detergent 3kg $8.95 @shengsiong — is this a good price?

Other ways to start a line:
bought: bought, buy, add, /add, /bought
used or ran out: done, finished, out, /used
last one: last
count: /stocktake
price check: price

How much:
2pcs, 10 rolls, 3kg, 500ml — that amount
2x10rolls — 2 packs of 10, and I remember the pack size
2, 2pkt, 2 boxes — packs, once I know the pack size. If I don't, I ask instead of guessing.

Price and store:
$12.90 or a plain 12.90 is the total paid.
The store goes last: @giant, or from/at a store I already know.

See what's there:
/listitems — what's in the cellar, and how much
/list toilet paper — one item: how much is left, and of which brands
/items — the same items with their IDs and pack sizes
/listbrands — brands on what we've bought
/stats — how many lines needed AI this month

Fix or teach:
/undo — remove your last entry (send it again to go back further)
/alias loo roll = toilet paper — teach me a name
/brand kans — teach me a brand
/set toilet paper pack 10 — set a pack size
/unit sticks = pcs — teach me a unit word
/rename dish soap = dishwashing liquid — change an item's name
/forget alias loo roll — take a name back. Also: /forget brand, /forget unit, /forget store
When I had to guess the item, tap Same item or New item under my reply."""

FORMAT_HINT = "Didn't catch that. Try: + rice 5kg $12 @ntuc — or /help for all formats."
FREE_TEXT_FAILED = "I couldn't read that just now. Please send it again, or use the format (/help)."
NO_ITEMS = "No items yet. Log one like this: + rice 5kg $12 @ntuc"
NOT_AVAILABLE_YET = "That command isn't available yet."
NOTHING_TO_UNDO = "Nothing to undo in the last 24 hours."
ALIAS_USAGE = "Use it like this: /alias loo roll = toilet paper"
SET_USAGE = "Use it like this: /set toilet paper pack 10"
UNIT_USAGE = "Use it like this: /unit sticks = pcs"
BRAND_USAGE = "Use it like this: /brand kans"
LIST_USAGE = "Use it like this: /list toilet paper — for how much is left and of which brands. /listitems shows everything."
RENAME_USAGE = "Use it like this: /rename dish soap = dishwashing liquid"
FORGET_USAGE = ("Use it like this: /forget alias loo roll — or /forget brand kans — or /forget unit sticks "
                "— or /forget store giant")
NO_BRANDS = "No brands yet. Teach me one with /brand <name>."
ENTRY_COMMAND_USAGE = "Use it like this: /add rice 5kg $12 @ntuc — or /used dish soap — or /stocktake rice 2x5kg"
TOO_OLD_TO_CHANGE = "Too old to change — use /alias or /undo"
ENTRY_WAS_UNDONE = "That entry was undone, so there is nothing to change."
BUTTON_FAILED = "Sorry, that didn't work — please try again"

_UNIT_NAMES = {"piece": "pieces", "roll": "rolls", "sheet": "sheets", "g": "grams", "ml": "millilitres"}
_COUNT_UNITS = {"piece": "pieces", "roll": "rolls", "sheet": "sheets"}
# For the examples in questions: (a pack size, the unit word to type).
_EXAMPLES = {"piece": (5, "pcs"), "roll": (10, "rolls"), "sheet": (100, "sheets"), "g": (5, "kg"), "ml": (1, "L")}
_KEYWORDS = {"bought": "+", "used": "-", "stock_count": "have", "price_check": "?"}


def event_line(item, event, created=False, read_from=None, missed=()) -> str:
    """The one-line confirmation for a recorded event.

    read_from is the sender's phrase when the item was a guess (a close spelling or
    the AI's match). missed names what the AI gave that wasn't in the message.
    """
    text = describe(item, event, name_suffix=" (new item)" if created else "")
    if event["action"] == "used" and item.get("stock_est") is not None:
        # item is as it stands after the entry, so this is what the family should still find at home.
        text += f", {_stock(item)} left" if item["stock_est"] else ", none left"
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
    if action == "used":
        return f"used {amount}" if amount else "used some"
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
    example = _resend(entry, f"{count}x{size}{word}")
    guess = _guess_note(item, read_from)
    if unit in _COUNT_UNITS:
        return (f"How many in each pack of {item['name']}? Resend like: {example} — "
                f"or write {count}{word} if you mean single {_COUNT_UNITS[unit]}.{guess}")
    return (f"How much is in each pack of {item['name']}? Resend like: {example} — "
            f"or write the total, like {size}{word}.{guess}")


def ask_packs_or_units(item, entry, read_from=None) -> str:
    """The question for a bare number in a sentence ("got 30 eggs"): that many, or that many packs?"""
    unit = item["base_unit"]
    count = entry["quantity"]["packs"]
    example = _resend(entry, f"{_number(count)}{_EXAMPLES[unit][1]}")
    return (f"Is that {quantity(count, unit)} of {item['name']}, or {_packs(count)}? Resend like: {example} — "
            f"or write {_packs(count)} if you mean packs of {quantity(item['pack_size'], unit)}."
            f"{_guess_note(item, read_from)}")


def _resend(entry, amount) -> str:
    """The sender's line in the format with the amount filled in, keeping the brand, price and store."""
    price = f" {money(entry['total_price'])}" if entry.get("total_price") else ""
    store = f" @{entry['store']}" if entry.get("store") else ""
    named = " ".join(filter(None, [entry.get("brand"), entry["phrase"]])).lower()
    return f"{_KEYWORDS.get(entry['action'], '+')} {named} {amount}{price}{store}"


def _guess_note(item, read_from) -> str:
    return f' (Read "{read_from}" as {item["name"]}.)' if read_from else ""


def shared_total(amounts) -> str:
    """Sent when a message gives one price for several items: nobody can say what each of them cost."""
    one = len(amounts) == 1
    return (f"{' and '.join(money(amount) for amount in amounts)} {'covers' if one else 'cover'} several items, "
            f"so I haven't recorded {'it' if one else 'them'} as the price of any one. "
            "Send an item with its own price to record it.")


def wrong_unit(item) -> str:
    """Sent when a quantity's unit can't count the item ("500ml" for something counted in pieces)."""
    unit = item["base_unit"]
    size, word = _EXAMPLES[unit]
    verb = "are" if item["name"].endswith("s") else "is"
    return f"{item['name']} {verb} counted in {_UNIT_NAMES[unit]}. Try 2{word} or 2x{size}{word}."


def not_a_product(phrase) -> str:
    """For a "+", "-" or "?" line whose item the AI didn't take for a household product."""
    return (f'I don\'t know what "{phrase}" is, so it isn\'t recorded. Use the product\'s usual name, '
            f"or teach me this one: /alias {phrase.lower()} = <item>")


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


def forgot_alias(alias, items) -> str:
    return f'Forgot "{alias}". It used to mean {" and ".join(item["name"] for item in items)}.'


def alias_not_known(name) -> str:
    return f'"{name}" isn\'t another name for anything I track. /list <item> shows an item\'s other names.'


def alias_is_the_name(item) -> str:
    return (f"{item['name']} is what the item is called, so there is nothing to forget. "
            f"To change it: /rename {item['name'].lower()} = <new name>")


def forgot_brand(name, entries, items) -> str:
    """entries is how many entries carried the brand; items the names of the items they are on."""
    if not entries:
        return f'Forgot the brand "{name}". It wasn\'t on any entry.'
    on, stays = ("1 entry", "That entry stays") if entries == 1 else (f"{entries} entries", "Those entries stay")
    return f'Forgot the brand "{name}". It was on {on} ({", ".join(items)}). {stays}, without the brand.'


def brand_not_known(name) -> str:
    return f'I don\'t know "{name}" as a brand. /listbrands shows the ones I know.'


def forgot_unit(word, meant) -> str:
    return f'Forgot "{word}" as a unit word. It used to mean {meant}.'


def unit_not_known(word) -> str:
    return f'I don\'t know "{word}" as a unit word.'


def forgot_store(name) -> str:
    return f"Forgot the store {name}. Entries that name it keep it."


def store_not_known(name) -> str:
    return f'I don\'t have "{name}" as a saved store.'


def built_in(name) -> str:
    """For a unit word or a store that every household starts with."""
    return f'"{name}" is built in, so I can\'t forget it.'


def renamed(old_name, item, same_words=False) -> str:
    """same_words: only the capitals or punctuation changed, so the old spelling still matches."""
    text = f"Renamed {old_name} to {item['name']} (ID still {item['item_id']})."
    if same_words:
        return text
    old = old_name.lower()
    return f'{text} "{old}" no longer means it. To keep that name too: /alias {old} = {item["name"].lower()}'


def already_named(item) -> str:
    return f"It is already called {item['name']}."


def name_taken(name, other) -> str:
    return f'"{name}" already means {other["name"]}, so I\'ve left it alone.'


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


def brands_list(brands) -> str:
    """The /listbrands reply. brands is (name, the items it has been bought as) pairs."""
    brands = sorted(brands, key=lambda brand: brand[0].lower())
    if not brands:
        return NO_BRANDS
    lines = ["Brands:"]
    for name, items in brands:
        lines.append(f"• {name} — {', '.join(sorted(items)) if items else 'not on any entry yet'}")
    return "\n".join(lines)


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
    """The /items reply: every tracked item with its ID, unit and pack size, for /alias, /set and the scripts."""
    if not items:
        return NO_ITEMS
    lines = ["Tracked items:"]
    for item in sorted(items, key=lambda i: i["name"].lower()):
        unit, pack_size = item["base_unit"], item.get("pack_size")
        pack = f", pack of {quantity(pack_size, unit)}" if pack_size else ""
        line = f"{item['item_id']} — {item['name']} ({unit}{pack})"
        status = item.get("status")
        if status in ("ok", "low") and item.get("stock_est"):
            line += f": {status}, {_stock(item)}"
        elif status:
            line += f": {status}"
        lines.append(line)
    return "\n".join(lines)


def cellar(items) -> str:
    """The /listitems reply: what is at home, grouped by how it is doing."""
    if not items:
        return NO_ITEMS
    headings = {"ok": "In the cellar:", "low": "Running low:", "out": "Out:", None: "Not counted yet:"}
    groups = {heading: [] for heading in headings.values()}
    for item in sorted(items, key=lambda i: i["name"].lower()):
        amount = f" — {_stock(item)}" if item.get("stock_est") else ""
        groups[headings.get(item.get("status"), headings[None])].append(f"• {item['name']}{amount}")
    return "\n\n".join("\n".join([heading, *lines]) for heading, lines in groups.items() if lines)


def item_detail(item, by_brand) -> str:
    """The /list <item> reply. by_brand is model.brands_left: (brand or None, amount) for what should be at home."""
    unit = item["base_unit"]
    stock, status = item.get("stock_est"), item.get("status")
    if stock:
        left = _stock(item)
    else:
        left = "none" if stock == 0 or status == "out" else "not counted yet"
    lines = [f"{item['name']} ({item['item_id']})", f"Left: {left}{', running low' if status == 'low' else ''}"]
    if item.get("pack_size"):
        lines.append(f"Pack size: {quantity(item['pack_size'], unit)}")
    brands = [brand for brand, _ in by_brand if brand]
    if brands:
        # Nobody logs which pack they opened, so this is worked out, not counted.
        lines.append("By brand, if the oldest is used first:")
        lines += [f"• {brand or 'no brand'} — {quantity(amount, unit)}" for brand, amount in by_brand]
    elif by_brand:
        lines.append("Brand: not noted")
    earlier = [brand for brand in item.get("brands", []) if brand not in brands]
    if earlier:
        lines.append(f"Bought before: {', '.join(earlier)}")
    if item.get("aliases"):
        lines.append(f"Also called: {', '.join(item['aliases'])}")
    return "\n".join(lines)


def _stock(item) -> str:
    """How much of an item there is: "20 rolls (2 packs)", or just "20 rolls" without a pack size."""
    amount = quantity(item["stock_est"], item["base_unit"])
    pack_size = item.get("pack_size")
    return f"{amount} ({_packs(item['stock_est'] / pack_size)})" if pack_size else amount


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
