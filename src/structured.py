"""Parse the message format (PLAN.md §5.1). Pure: no I/O, no AI.

One entry per line: a keyword, then the item words, one quantity, a price and a
store in any order, except that the store goes last. Numbers are never guessed
at: a quantity says what it counts, or the caller asks (resolve_quantity).
"""
import re

MAX_PACKS = 100
MAX_STORE_LENGTH = 40

# Returned for a line that starts with a keyword but can't be read.
UNREADABLE = "unreadable"

# A symbol may touch the item ("+rice"). A word must stand alone ("buy rice", not "buyer").
SYMBOLS = {"+": "bought", "-": "finished", "?": "price_check"}
WORDS = {
    "bought": "bought", "buy": "bought", "add": "bought",
    "done": "finished", "finished": "finished", "out": "finished",
    "low": "opened_last", "last": "opened_last",
    "have": "stock_count",
    "price": "price_check",
}

# These actions take no quantity or price: everything after the keyword is the item.
NO_QUANTITY = ("finished", "opened_last")

PACK = "pack"  # what a pack word counts, as opposed to a base unit

# Unit word -> (base unit, multiplier). A number directly before one is a quantity.
UNIT_WORDS = {
    **dict.fromkeys(("pc", "pcs", "pce", "piece", "pieces", "ea", "nos", "unit", "units"), ("piece", 1)),
    **dict.fromkeys(("roll", "rolls", "rl"), ("roll", 1)),
    **dict.fromkeys(("sheet", "sheets", "sht"), ("sheet", 1)),
    **dict.fromkeys(("g", "gm", "gram", "grams"), ("g", 1)),
    **dict.fromkeys(("kg", "kgs"), ("g", 1000)),
    "ml": ("ml", 1),
    **dict.fromkeys(("l", "ltr", "litre", "litres", "liter", "liters"), ("ml", 1000)),
    **dict.fromkeys(
        ("pkt", "pkts", "pack", "packs", "packet", "packets", "box", "boxes", "bottle", "bottles", "btl",
         "can", "cans", "tin", "tins", "bag", "bags", "tube", "tubes", "jar", "jars", "carton", "cartons",
         "tub", "tubs"),
        (PACK, 1),
    ),
}

# Stores every household starts with: spelling -> name. Capitals, spaces and
# punctuation in a spelling don't matter ("jb watsons" also covers "JBWatsons").
# Stores first used with "@" are saved as STORE records and passed in as `stores`.
STORE_ALIASES = {
    "ntuc": "FairPrice", "fairprice": "FairPrice", "fp": "FairPrice", "ntucfairprice": "FairPrice",
    "ss": "Sheng Siong", "shengsiong": "Sheng Siong",
    "cs": "Cold Storage", "coldstorage": "Cold Storage",
    "tb": "Taobao", "taobao": "Taobao", "淘宝": "Taobao",
    "jd": "JD", "京东": "JD", "jd.com": "JD",
    "lazada": "Lazada", "lzd": "Lazada",
    "sp": "Shopee", "shopee": "Shopee",
    "jb": "JB"
}
STORE_WORDS = ("from", "at")  # "from taobao", "at ntuc": only before a known store

# Why resolve_quantity couldn't work out a total.
NEED_PACK_SIZE = "need_pack_size"
WRONG_UNIT = "wrong_unit"

_NUMBER = r"\d+(?:\.\d+)?"
_TIMES = re.compile(r"(\d)\s*[x×*]\s*(?=\d)", re.IGNORECASE)  # "2 x 10", "2×10", "2*10"
_SIZE_TIMES = re.compile(rf"({_NUMBER}[^\W\d_]+)\s*[x×*]\s*(\d+)(?![\w.])", re.IGNORECASE)  # "500ml x2"
_SPACED_PRICE = re.compile(r"\$\s+(?=\d)")                     # "$ 12"
_PRICE = re.compile(rf"\$({_NUMBER})")
_PACKS_SIZE = re.compile(rf"({_NUMBER})x({_NUMBER})([^\W\d_]+)?", re.IGNORECASE)
_NUMBER_WORD = re.compile(rf"({_NUMBER})([^\W\d_]+)")
_BARE = re.compile(_NUMBER)
_TWO_DECIMALS = re.compile(r"\d+\.\d\d")
_READABLE = re.compile(r"[^\W_]")  # a letter or a digit
_LETTER = re.compile(r"[^\W\d_]")
_EACH = ("each", "per", "ea")      # "2 packs ... 10 rolls each", in free text


def parse(text, stores=(), units=None) -> list:
    """Return (line, result) for each non-blank line of a message. See parse_line for result."""
    return [(line.strip(), parse_line(line, stores, units)) for line in text.splitlines() if line.strip()]


def parse_line(line, stores=(), units=None):
    """Read one line of a message.

    stores are the saved STORE records and units the taught unit words
    ({word: unit word}). Returns the entry as a dict (action, phrase, quantity,
    total_price, store, store_is_new, raw), UNREADABLE for a line that starts
    with a keyword but can't be read, or None for a line that doesn't start
    with a keyword. A None in an entry means "not given".
    """
    raw = " ".join(line.split())
    action, rest = _keyword(raw)
    # A keyword with nothing after it ("done", "??") is ordinary chat, not an attempt.
    if action is None or not _READABLE.search(rest):
        return None
    rest, store, store_is_new = _split_store(rest, store_names(stores))
    if action in NO_QUANTITY:
        phrase = " ".join(rest.split())
        if not _READABLE.search(phrase):
            return UNREADABLE
        return _entry(action, phrase, raw)
    reading = _read(rest, units or {})
    if reading is None:
        return UNREADABLE
    prices, quantities, words = reading
    phrase = " ".join(words)
    price = prices[0] if prices else None
    quantity = quantities[0] if quantities else None
    if not _READABLE.search(phrase):
        return UNREADABLE
    # A price check needs both, or there is no unit price to compare.
    if action == "price_check" and (price is None or quantity is None):
        return UNREADABLE
    return _entry(action, phrase, raw, quantity, price, store, store_is_new)


def parse_bought(line, stores=(), units=None):
    """Read a line that has no keyword as if it were a purchase ("facial masks 5 pcs 1.86 from taobao")."""
    entry = parse_line("+ " + line, stores, units)
    if isinstance(entry, dict):
        entry["raw"] = " ".join(line.split())
    return entry


def has_keyword(line) -> bool:
    return _keyword(" ".join(line.split()))[0] is not None


def has_words(line) -> bool:
    """Whether a line has words after its keyword, not only numbers and prices ("+ rice 2 3", not "+ 2 3")."""
    return bool(_LETTER.search(_keyword(" ".join(line.split()))[1]))


def read_quantity(text, units=None):
    """The one quantity in a piece of free text ("35 sticks", "pack of 10 rolls"), or None.

    Reads by the same rules as the format, but other words may sit around the
    numbers, and a pack count with a size marked "each" is packs x size.
    """
    reading = _read(text, units or {}, loose=True)
    if reading is None or len(reading[1]) != 1:
        return None
    return reading[1][0]


def resolve_quantity(quantity, base_unit, pack_size):
    """Work out an entry's total for an item, in the item's base unit.

    Returns {"qty", "packs", "units_per_pack"}, or NEED_PACK_SIZE / WRONG_UNIT
    when the total can't be known without guessing. No quantity means one
    pack. units_per_pack is only set when the quantity was written as packs x size.
    """
    quantity = quantity or {"kind": "packs", "packs": 1}
    kind = quantity["kind"]
    if kind == "packs":
        if not pack_size:
            return NEED_PACK_SIZE
        return {"qty": _number(quantity["packs"] * pack_size), "packs": quantity["packs"], "units_per_pack": None}
    if not unit_fits(quantity["unit"], base_unit):
        return WRONG_UNIT
    if kind == "amount":
        return {"qty": quantity["amount"], "packs": None, "units_per_pack": None}
    return {
        "qty": _number(quantity["packs"] * quantity["size"]),
        "packs": quantity["packs"],
        "units_per_pack": quantity["size"],
    }


def unit_fits(unit, base_unit) -> bool:
    """Whether a typed unit can count an item. No unit ("2x10") means the item's own."""
    # Household liquids are sold by weight or volume interchangeably; 1 ml is close to 1 g.
    return unit is None or unit == base_unit or {unit, base_unit} == {"g", "ml"}


def unit_word(word, units=None):
    """(base unit or PACK, multiplier) for a built-in or taught unit word, or None."""
    word = word.lower()
    if word in UNIT_WORDS:
        return UNIT_WORDS[word]
    return UNIT_WORDS.get((units or {}).get(word, ""))


def store_names(stores=()) -> dict:
    """Every known store spelling (see squash) -> the store's name."""
    names = {squash(spelling): name for spelling, name in STORE_ALIASES.items()}
    for store in stores:
        for spelling in (store["name"], *store.get("aliases", [])):
            names[squash(spelling)] = store["name"]
    return names


def find_store(line, stores=()):
    """The store a line names with "@" or with from/at + a known store: (name, is new), or (None, False)."""
    return _split_store(" ".join(line.split()), store_names(stores))[1:]


def title_store(typed) -> str:
    """How a store nobody has saved yet is written: "don don donki" -> "Don Don Donki"."""
    return " ".join(word.capitalize() for word in typed.split())


def squash(text) -> str:
    """Lowercase letters and digits only, so "Sheng Siong" and "shengsiong" compare equal."""
    return re.sub(r"[\W_]+", "", text.lower())


def numbers_in(text) -> list:
    """Every number written in a piece of text, as floats."""
    return [float(number) for number in re.findall(_NUMBER, text)]


def _entry(action, phrase, raw, quantity=None, price=None, store=None, store_is_new=False) -> dict:
    return {
        "action": action,
        "phrase": phrase,
        "quantity": quantity,
        "total_price": price,
        "store": store,
        "store_is_new": store_is_new,
        "raw": raw,
    }


def _keyword(line):
    """Return (action, rest of the line), or (None, "") if the line doesn't start with a keyword."""
    if line[:1] in SYMBOLS:
        return SYMBOLS[line[0]], line[1:]
    word, _, rest = line.partition(" ")
    action = WORDS.get(word.lower())
    return (action, rest) if action else (None, "")


def _split_store(text, names):
    """Take the store off the end of a line. Returns (the rest, store name or None, whether it is new)."""
    if "@" in text:
        rest, _, typed = text.partition("@")
        typed = " ".join(typed.split())[:MAX_STORE_LENGTH].strip()
        if not typed:
            return rest, None, False
        known = names.get(squash(typed))
        return rest, known or title_store(typed), known is None
    words = text.split()
    for index, word in enumerate(words[:-1]):
        # "+ bread at home" stays an item: only a known store counts here.
        known = word.lower() in STORE_WORDS and names.get(squash(" ".join(words[index + 1:])))
        if known:
            return " ".join(words[:index]), known, False
    return text, None, False


def _read(text, units, loose=False):
    """Sort the words of a line into (prices, quantities, item words).

    Returns None for more than one price or quantity, or a number that can't be
    right (zero, or more packs than MAX_PACKS). With loose (free text), a pack
    count plus a size marked "each" also reads as packs x size.
    """
    text = _SPACED_PRICE.sub("$", _TIMES.sub(r"\1x", _SIZE_TIMES.sub(r"\2x\1", text)))
    tokens = text.split()
    prices, parts = [], []   # parts keeps quantities and words in order, to spot "2 packs of 10 rolls"
    index = 0
    while index < len(tokens):
        token = tokens[index]
        core = token.strip(",;").rstrip(".")
        following = unit_word(tokens[index + 1].strip(",;."), units) if index + 1 < len(tokens) else None
        if match := _PRICE.fullmatch(core):
            prices.append(_number(match[1]))
        elif match := _PACKS_SIZE.fullmatch(core):
            unit = unit_word(match[3], units) if match[3] else None
            if match[3] is None and following and following[0] != PACK:
                unit, index = following, index + 1   # "2x10 rolls"
            if (match[3] and unit is None) or (unit and unit[0] == PACK):
                return None
            base_unit, multiplier = unit or (None, 1)
            parts.append(_packs_of(_number(match[1]), _number(float(match[2]) * multiplier), base_unit))
        elif (match := _NUMBER_WORD.fullmatch(core.replace("-", ""))) and unit_word(match[2], units):
            parts.append(_counted(match[1], unit_word(match[2], units)))      # "2pcs", "3kg", "2pkt", "10-roll"
        elif _BARE.fullmatch(core) and following:
            parts.append(_counted(core, following))                           # "5 pcs", "2 boxes"
            index += 1
        elif _TWO_DECIMALS.fullmatch(core):
            prices.append(_number(core))                                      # "1.86"
        elif _BARE.fullmatch(core):
            parts.append({"kind": "packs", "packs": _number(core)})           # a bare "2"
        elif core.lower() == "for" and index + 1 < len(tokens) and _is_price(tokens[index + 1]):
            pass                                                              # "... for $1.96": not an item word
        else:
            parts.append(token)
        index += 1
    parts = _join_packs_of(parts, units, loose)
    quantities = [part for part in parts if isinstance(part, dict)]
    # "2 packs of rice": the "of" after a quantity isn't an item word either.
    words = [
        part for position, part in enumerate(parts)
        if isinstance(part, str)
        and not (part.lower() == "of" and position and isinstance(parts[position - 1], dict))
    ]
    if len(quantities) > 1 or 0 in prices or (len(prices) > 1 and not loose):
        return None
    for quantity in quantities:
        if 0 in (quantity.get("packs"), quantity.get("size"), quantity.get("amount")):
            return None
        if quantity.get("packs", 0) > MAX_PACKS:
            return None
    return prices, quantities, words


def _is_price(token) -> bool:
    core = token.strip(",;").rstrip(".")
    return bool(_PRICE.fullmatch(core) or _TWO_DECIMALS.fullmatch(core))


def _join_packs_of(parts, units, loose):
    """Turn "2 packs of 10 rolls" (a pack count, "of", an amount) into one packs x size quantity."""
    kinds = [part["kind"] if isinstance(part, dict) else None for part in parts]
    of_amount = [
        index for index in range(len(parts) - 2)
        if str(parts[index + 1]).lower() == "of" and kinds[index + 2] == "amount"
    ]
    for index in of_amount:
        if kinds[index] == "packs":
            return parts[:index] + [_from_pair(parts[index], parts[index + 2])] + parts[index + 3:]
    if not loose:
        return parts
    words = [part.lower().strip(",;.") for part in parts if isinstance(part, str)]
    quantities = [part for part in parts if isinstance(part, dict)]
    found = sorted(quantity["kind"] for quantity in quantities)
    if found == ["amount", "packs"]:
        count = next(q for q in quantities if q["kind"] == "packs")
        amount = next(q for q in quantities if q["kind"] == "amount")
        # "2 packs ... 10 rolls each". Without "each" the size could be the total, unless it is one pack.
        if count["packs"] == 1 or any(word in _EACH for word in words):
            return [_from_pair(count, amount)]
    for index in of_amount:
        named_pack = isinstance(parts[index], str) and unit_word(parts[index], units) == (PACK, 1)
        if named_pack and found == ["amount"]:
            return [_from_pair({"packs": 1}, parts[index + 2])]               # "pack of 10 rolls"
    return parts


def _from_pair(count, amount) -> dict:
    return _packs_of(count["packs"], amount["amount"], amount["unit"])


def _packs_of(packs, size, unit) -> dict:
    return {"kind": "packs_of", "packs": packs, "size": size, "unit": unit}


def _counted(number, unit) -> dict:
    """A number with its unit word: an amount in base units, or a count of packs."""
    base_unit, multiplier = unit
    if base_unit == PACK:
        return {"kind": "packs", "packs": _number(number)}
    return {"kind": "amount", "amount": _number(float(number) * multiplier), "unit": base_unit}


def _number(value):
    value = round(float(value), 6)
    return int(value) if value.is_integer() else value
