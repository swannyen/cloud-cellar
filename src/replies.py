"""Message text the bot sends (PLAN.md §4.4)."""

HELP = """\
Tell me what you bought or used up, in your own words. For example:
• bought 2 packs toilet paper, 10 rolls each, $12.90 at FairPrice
• finished the dish soap
• opened the last bag of rice
• we have 3 bottles of shampoo
• is $8.95 for 3kg of detergent a good price?

Commands: /items (what I'm tracking), /undo (remove your last entry), /help"""

NO_ITEMS = "No items yet. Tell me what you bought or used up."
NOT_AVAILABLE_YET = "That command isn't available yet."
NOTHING_TO_UNDO = "Nothing to undo."
UNDO_TOO_OLD = "Your last entry is more than 24 hours old, so I've left it alone."

_COUNT_UNITS = {"piece": "pieces", "roll": "rolls", "sheet": "sheets"}


def event_line(item, event, created=False) -> str:
    """The one-line confirmation for a recorded event."""
    text = describe(item, event, name_suffix=" (new item)" if created else "")
    tick = "" if event["action"] == "price_check" else "✓ "
    return f"{tick}{text}."


def undone(item, event) -> str:
    return f"Undone: {describe(item, event)}."


def describe(item, event, name_suffix="") -> str:
    """What happened, e.g. "Toilet paper: bought 2 × 10 rolls for $12.90 at FairPrice ($0.65/roll)"."""
    unit = item["base_unit"]
    amount = _amount(event, unit)
    price = money(event["total_price"]) if event.get("total_price") else ""
    store = f" at {event['store']}" if event.get("store") else ""
    per_unit = unit_price(event["unit_price"], unit) if event.get("unit_price") else ""
    action = event["action"]
    if action == "bought":
        text = "bought" + _join(amount, f"for {price}" if price else "") + store
        text += f" ({per_unit})" if per_unit else ""
    elif action == "price_check":
        text = price + (f" for {amount}" if amount else "")
        text += (f" = {per_unit}" if per_unit else "") + store + ". Price noted"
    elif action == "stock_count":
        packs = event.get("packs")
        text = f"counted {_packs(packs)} in stock" if packs else "noted as in stock"
    elif action == "opened_last":
        text = "marked as running low"  # also used for "running low", not only "opened the last one"
    elif action == "finished":
        text = "marked finished"
    else:  # still_have
        text = "noted, still have plenty"
    return f"{item['name']}{name_suffix}: {text}"


def items_list(items) -> str:
    if not items:
        return NO_ITEMS
    lines = ["Tracked items:"]
    for item in sorted(items, key=lambda i: i["name"].lower()):
        line = f"{item['item_id']} — {item['name']} ({item['base_unit']})"
        status = item.get("status")
        if status in ("ok", "low") and item.get("stock_est"):
            line += f": {status}, {_packs(item['stock_est'])}"
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


def _amount(event, base_unit) -> str:
    packs, units = event.get("packs"), event.get("units_per_pack")
    if packs and units:
        size = quantity(units, base_unit)
        return size if packs == 1 else f"{_number(packs)} × {size}"
    if packs:
        return _packs(packs)
    return quantity(units, base_unit) if units else ""


def _packs(count) -> str:
    return f"{_number(count)} {'pack' if count == 1 else 'packs'}"


def _number(value) -> str:
    return f"{value:.2f}".rstrip("0").rstrip(".")


def _join(*parts) -> str:
    return "".join(f" {part}" for part in parts if part)
