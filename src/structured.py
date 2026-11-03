"""Parse the message format (PLAN.md §5.1). Pure: no I/O, no AI.

One entry per line: a keyword, then the item words, one quantity, a price and a
store in any order, except that the store goes last. A date ("on 15/09/2026")
can go anywhere. Numbers are never guessed at: a quantity says what it counts,
or the caller asks (resolve_quantity). The same goes for dates (resolve_date).
"""
import calendar
import re
from datetime import date, timedelta

MAX_PACKS = 1000  # more than this in one entry is taken for a typo (a phone number, a price without its $)
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

# The same entries written as bot commands: "/add rice 5kg" is "+ rice 5kg", "/used rice" is
# "- rice", and "/stocktake rice 2" is "have rice 2".
COMMANDS = {"/add": "bought", "/bought": "bought", "/used": "finished", "/stocktake": "stock_count"}

# This action takes no quantity or price: everything after the keyword is the item.
NO_QUANTITY = ("opened_last",)

PACK = "pack"  # what a pack word counts, as opposed to a base unit
COUNT_UNITS = ("piece", "roll", "sheet")  # base units that are counted, not weighed or poured

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
UNCLEAR_COUNT = "unclear_count"

# Why a date can't be used. BAD_DATE is also what parse_line returns for a line
# with something date-like that isn't in the format.
BAD_DATE = "bad_date"
FUTURE_DATE = "future_date"
# What parse_line returns for an expiry word with no date it can read after it, and
# what resolve_expiry returns for a date that doesn't exist.
BAD_EXPIRY = "bad_expiry"
EXPIRED_WINDOW_DAYS = 14   # a date with no year that fell this recently is taken as past, not as next year

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
_MONTHS = ("jan|january|feb|february|mar|march|apr|april|may|jun|june|jul|july|aug|august|"
           "sep|sept|september|oct|october|nov|november|dec|december")
# The one date format: "on" and day/month, with the year if wanted. "on sale" stays an item word.
_ON_DATE = re.compile(r"(?<!\S)on\s+(\d{1,2})/(\d{1,2})(?:/(\d{4}|\d{2}))?(?=[,;.]?(?:\s|$))", re.IGNORECASE)
# What a date looks like when it isn't in the format. Better to ask than to log it as today.
_STORE_LIKE_A_DATE = re.compile(r"\d{1,2}/\d{1,2}")  # "@7/11" is a shop. "@ntuc 15/9" is a shop and a date.
# An expiry (PLAN.md §8.6): a word, then the date. Read before anything else, so that
# "exp 31/12" isn't a date in the wrong format and "expires on 31/12/2026" isn't the day it was bought.
_EXPIRY_WORD = re.compile(
    r"(?<![\w/])(?:exp\.?|expiry(?:\s+date)?|expires?|expiring|best\s+before|use\s+by)(?![\w])"
    r"(?:\s*:)?(?:\s+(?:in|on|by|is|date|of))?\s*",
    re.IGNORECASE,
)
_END = r"(?![\w/-])"
_EXPIRY_FORMS = [
    ("ymd", re.compile(rf"(\d{{4}})-(\d{{1,2}})-(\d{{1,2}}){_END}")),                      # 2026-12-31
    ("dmy", re.compile(rf"(\d{{1,2}})/(\d{{1,2}})/(\d{{4}}|\d{{2}}){_END}")),              # 31/12/2026, 31/12/26
    ("my", re.compile(rf"(\d{{1,2}})/(\d{{4}}){_END}")),                                   # 01/2028
    ("ab", re.compile(rf"(\d{{1,2}})/(\d{{1,2}}){_END}")),                                 # 31/12, or 12/27 for Dec 2027
    ("d_mon", re.compile(rf"(\d{{1,2}})(?:st|nd|rd|th)?\s+({_MONTHS})\.?(?:,?\s+(\d{{4}}))?{_END}",
                         re.IGNORECASE)),                                                  # 31 dec, 31 dec 2027
    ("mon_y", re.compile(rf"({_MONTHS})\.?[\s/-]*(\d{{4}}){_END}", re.IGNORECASE)),       # Jan 2028, jan-2028
    ("mon", re.compile(rf"({_MONTHS})\.?{_END}", re.IGNORECASE)),                           # dec
    ("in", re.compile(rf"(\d{{1,3}})\s*(d|days?|w|wks?|weeks?|m|mths?|months?){_END}", re.IGNORECASE)),  # 5d, 2w, 3m
]
_DATE_LIKE = re.compile(
    r"(?<![\w/.-])\d{1,2}/\d{1,2}(?:/\d+)?(?![\w/])"          # 15/9, or a year that can't be right
    r"|(?<![\w/.-])\d{1,2}[-.]\d{1,2}[-.]\d{2,4}(?![\w-])"     # 15-09-2026, 15.09.26
    r"|(?<![\w/.-])\d{4}[-/.]\d{1,2}[-/.]\d{1,2}(?![\w-])"     # 2026-09-15
    rf"|\b\d{{1,2}}(?:st|nd|rd|th)?\s+(?:{_MONTHS})\b"           # 15 sep, 15th september
    rf"|\b(?:{_MONTHS})\s+\d{{1,2}}\b",                          # sep 15
    re.IGNORECASE,
)


def parse(text, stores=(), units=None) -> list:
    """Return (line, result) for each non-blank line of a message. See parse_line for result."""
    return [(line.strip(), parse_line(line, stores, units)) for line in text.splitlines() if line.strip()]


def parse_line(line, stores=(), units=None):
    """Read one line of a message.

    stores are the saved STORE records and units the taught unit words
    ({word: unit word}). Returns the entry as a dict (action, phrase, quantity,
    total_price, store, store_is_new, on, expiry, raw, explicit), UNREADABLE for a line
    that starts with a keyword but can't be read, BAD_DATE for one with a date
    that isn't in the format, BAD_EXPIRY for an expiry word with no date it can
    read, or None for a line that doesn't start with a keyword. A None in an entry means "not given".
    """
    raw = " ".join(line.split())
    action, rest = _keyword(raw)
    explicit = is_explicit(raw)
    # A keyword with nothing after it ("done", "??") is ordinary chat, not an attempt.
    if action is None or not _READABLE.search(rest):
        return None
    rest, expiry = split_expiry(rest)
    if expiry == BAD_EXPIRY:
        return BAD_EXPIRY
    rest, on = split_date(rest)
    if on == BAD_DATE:
        return BAD_DATE
    rest, store, store_is_new = _split_store(rest, store_names(stores))
    if action in NO_QUANTITY:
        phrase = " ".join(rest.split())
        if not _READABLE.search(phrase):
            return UNREADABLE
        return _entry(action, phrase, raw, explicit=explicit, on=on, expiry=expiry)
    reading = _read(rest, units or {})
    if reading is None:
        return UNREADABLE
    prices, quantities, words = reading
    phrase = " ".join(words)
    price = prices[0] if prices else None
    quantity = quantities[0] if quantities else None
    if not _READABLE.search(phrase):
        return UNREADABLE
    if action == "finished":
        # "- rice" has run out. "- rice 2" is two packs used, with the rest still there.
        if quantity is None:
            return _entry("finished", phrase, raw, explicit=explicit, on=on, expiry=expiry)
        return _entry("used", phrase, raw, quantity, explicit=explicit, on=on, expiry=expiry)
    # A price check needs both, or there is no unit price to compare.
    if action == "price_check" and (price is None or quantity is None):
        return UNREADABLE
    return _entry(action, phrase, raw, quantity, price, store, store_is_new, explicit, on, expiry)


def parse_bought(line, stores=(), units=None):
    """Read a line that has no keyword as if it were a purchase ("facial masks 5 pcs 1.86 from taobao")."""
    entry = parse_line("+ " + line, stores, units)
    if isinstance(entry, dict):
        entry["raw"] = " ".join(line.split())
        entry["explicit"] = False  # nobody typed the "+"
    return entry


def has_keyword(line) -> bool:
    return _keyword(" ".join(line.split()))[0] is not None


def is_explicit(line) -> bool:
    """Whether a line opens with "+", "-", "?" or an entry command: the format used on purpose, not chat."""
    return line.strip()[:1] in SYMBOLS or is_entry_command(line)


def is_entry_command(text) -> bool:
    """Whether a message starts with /add, /bought, /used or /stocktake (with or without "@BotName")."""
    return _command(text.strip().partition(" ")[0]) in COMMANDS


def has_words(line) -> bool:
    """Whether a line has words after its keyword, not only numbers and prices ("+ rice 2 3", not "+ 2 3")."""
    return bool(_LETTER.search(_keyword(" ".join(line.split()))[1]))


def read_quantity(text, units=None):
    """The one quantity in a piece of free text ("35 sticks", "pack of 10 rolls"), or None.

    Reads by the same rules as the format, but other words may sit around the
    numbers, and a pack count with a size marked "each" is packs x size. A
    number with no unit or pack word comes back marked "bare": in the format
    that means packs, but nobody writing a sentence is thinking of that rule.
    """
    reading = _read(text, units or {}, loose=True)
    if reading is None or len(reading[1]) != 1:
        return None
    return reading[1][0]


def resolve_quantity(quantity, base_unit, pack_size):
    """Work out an entry's total for an item, in the item's base unit.

    Returns {"qty", "packs", "units_per_pack"}, or NEED_PACK_SIZE / WRONG_UNIT /
    UNCLEAR_COUNT when the total can't be known without guessing. No quantity
    means one pack. units_per_pack is only set when the quantity was written as
    packs x size.
    """
    quantity = quantity or {"kind": "packs", "packs": 1}
    kind = quantity["kind"]
    if kind == "packs":
        if not pack_size:
            return NEED_PACK_SIZE
        # "30 eggs" in a sentence: 30 eggs, or 30 packs of them? Rice and shampoo aren't
        # counted singly, so there a number on its own can only be packs.
        if quantity.get("bare") and base_unit in COUNT_UNITS:
            return UNCLEAR_COUNT
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


def split_date(text):
    """Take the date out of a line or a message, wherever it is.

    Returns (the rest of the text, the date as written). The date is None if
    there isn't one, or {"day", "month", "year", "written"}, with no year as
    None. It is BAD_DATE for something date-like that isn't in the format, or
    for two different dates, since nothing says which belongs to what.
    """
    written = set(_ON_DATE.findall(text))
    rest = " ".join(_ON_DATE.sub(" ", text).split())
    typed_store = rest.partition("@")[2].strip()
    without_store = rest if not _STORE_LIKE_A_DATE.fullmatch(typed_store) else rest.partition("@")[0]
    if len(written) > 1 or _DATE_LIKE.search(without_store):
        return rest, BAD_DATE
    if not written:
        return rest, None
    day, month, year = written.pop()
    return rest, {
        "day": int(day),
        "month": int(month),
        "year": int(year) if year else None,
        "written": "/".join(filter(None, (day, month, year))),
    }


def split_expiry(text):
    """Take the expiry off a line, wherever it is: (the rest of the line, the expiry as read).

    The expiry is None if there isn't one, or a dict for resolve_expiry. It is
    BAD_EXPIRY for an expiry word with no date it can read after it, or for two
    different expiries, since nothing says which is right.
    """
    found, rest, start = [], "", 0
    for word in _EXPIRY_WORD.finditer(text):
        if word.start() < start:
            continue  # inside the expiry just read
        expiry = _expiry_at(text, word.end())
        if expiry is None:
            return " ".join(text.split()), BAD_EXPIRY
        found.append(expiry)
        rest += text[start:word.start()] + " "
        start = word.end() + len(expiry["written"])
    rest = " ".join((rest + text[start:]).split())
    if len({expiry["written"].lower() for expiry in found}) > 1:
        return rest, BAD_EXPIRY
    return rest, (found[0] if found else None)


def read_expiry(text):
    """The expiry in a piece of free text, copied with its word ("expires Jan 2028") or without it ("Jan 2028").

    Returns what split_expiry would, or None.
    """
    expiry = split_expiry(text)[1]
    if expiry is None:
        return _expiry_at(text.strip(), 0, whole=True)
    return expiry


def strip_expiries(text) -> str:
    """The text without any expiry in it, line by line: what is left to look for a date in."""
    return "\n".join(split_expiry(line)[0] if split_expiry(line)[1] != BAD_EXPIRY else line
                     for line in text.splitlines())


def resolve_expiry(expiry, today):
    """The day an expiry means, or BAD_EXPIRY for a date that doesn't exist. today is a date.

    A month on its own ("Jan 2028", "12/27", "01/2028") means the first day of
    that month: nobody can tell how far into the month it lasts, and it is
    better to use something early than to use it after its date. A date with
    no year is the next time it comes round, unless it fell in the last two
    weeks, which is more likely something already past its date than something
    a year away. For a month with no year, that goes by the month's last day.
    """
    kind, values = expiry["kind"], expiry["values"]
    try:
        if kind in ("ymd", "dmy"):
            year, month, day = values if kind == "ymd" else values[::-1]
            return date(_full_year(year), month, day)
        if kind == "my":
            return date(values[1], values[0], 1)
        if kind == "ab":
            first, second = values
            if second > 12:
                return date(_full_year(second), first, 1)   # 12/27: December 2027
            return _coming(today, second, first)
        if kind == "d_mon":
            day, month, year = values
            return date(year, month, day) if year else _coming(today, month, day)
        if kind == "mon_y":
            return date(values[1], values[0], 1)
        if kind == "mon":
            recent = _month_end(today.year, values[0]) >= today - timedelta(days=EXPIRED_WINDOW_DAYS)
            return date(today.year if recent else today.year + 1, values[0], 1)
        count, unit = values
        if unit == "m":
            month = today.month - 1 + count
            year, month = today.year + month // 12, month % 12 + 1
            return date(year, month, min(today.day, calendar.monthrange(year, month)[1]))
        return today + timedelta(days=count * (7 if unit == "w" else 1))
    except ValueError:
        return BAD_EXPIRY


def _expiry_at(text, position, whole=False):
    """The expiry date written at a position in the text, as {"kind", "values", "written"}, or None."""
    for kind, form in _EXPIRY_FORMS:
        found = form.match(text, position)
        if found and (not whole or found.end() == len(text)):
            return {"kind": kind, "values": _expiry_values(kind, found), "written": found[0]}
    return None


def _expiry_values(kind, found):
    if kind in ("d_mon", "mon_y", "mon"):
        groups = list(found.groups())
        index = 1 if kind == "d_mon" else 0
        groups[index] = _month_number(groups[index])
        return tuple(int(group) if group is not None else None for group in groups)
    if kind == "in":
        return int(found[1]), found[2][0].lower()
    return tuple(int(group) for group in found.groups())


def _month_number(name) -> int:
    return [month.lower() for month in calendar.month_abbr].index(name[:3].lower())


def _month_end(year, month):
    return date(year, month, calendar.monthrange(year, month)[1])


def _full_year(year) -> int:
    return year + 2000 if year < 100 else year


def _coming(today, month, day):
    """The next time a day and month come round, or this year's if it fell in the last two weeks."""
    this_year = date(today.year, month, day)
    if this_year >= today - timedelta(days=EXPIRED_WINDOW_DAYS):
        return this_year
    return date(today.year + 1, month, day)


def resolve_date(on, today):
    """The day a written date means, or BAD_DATE / FUTURE_DATE. today is a date.

    Always day first, Singapore style: 3/9 is 3 September, and 9/15 isn't a
    date. A date with no year is the last time it came round. Nothing is logged
    ahead of time, so a date still to come can only be a mistake.
    """
    if on == BAD_DATE:
        return BAD_DATE
    try:
        if on["year"] is not None:
            day = date(on["year"] + (2000 if on["year"] < 100 else 0), on["month"], on["day"])
            return FUTURE_DATE if day > today else day
        day = date(today.year, on["month"], on["day"])
        return day if day <= today else date(today.year - 1, on["month"], on["day"])
    except ValueError:
        return BAD_DATE


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
    return _split_store(split_date(split_expiry(" ".join(line.split()))[0])[0], store_names(stores))[1:]


def title_store(typed) -> str:
    """How a store nobody has saved yet is written: "don don donki" -> "Don Don Donki"."""
    return " ".join(word.capitalize() for word in typed.split())


def squash(text) -> str:
    """Lowercase letters and digits only, so "Sheng Siong" and "shengsiong" compare equal."""
    return re.sub(r"[\W_]+", "", text.lower())


def numbers_in(text) -> list:
    """Every number written in a piece of text, as floats."""
    return [float(number) for number in re.findall(_NUMBER, text)]


def _entry(action, phrase, raw, quantity=None, price=None, store=None, store_is_new=False, explicit=False,
           on=None, expiry=None) -> dict:
    return {
        "explicit": explicit,
        "action": action,
        "phrase": phrase,
        "quantity": quantity,
        "total_price": price,
        "store": store,
        "store_is_new": store_is_new,
        "on": on,
        "expiry": expiry,
        "raw": raw,
    }


def _keyword(line):
    """Return (action, rest of the line), or (None, "") if the line doesn't start with a keyword."""
    if line[:1] in SYMBOLS:
        return SYMBOLS[line[0]], line[1:]
    word, _, rest = line.partition(" ")
    action = COMMANDS.get(_command(word)) or WORDS.get(word.lower())
    return (action, rest) if action else (None, "")


def _command(word) -> str:
    """"/Add@BotName" -> "/add"."""
    return word.split("@")[0].lower()


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
    # "… $21.85 taobao": a known store written last, straight after the price or quantity, is the store too.
    for size in (3, 2, 1):
        known = len(words) > size and _is_amount(words[-size - 1]) and names.get(squash(" ".join(words[-size:])))
        if known:
            return " ".join(words[:-size]), known, False
    return text, None, False


def _is_amount(token) -> bool:
    """Whether a word is a price or a quantity, or ends one ("$1.58", "18x7pcs", the "pcs" of "5 pcs")."""
    core = token.strip(",;").rstrip(".")
    return any(character.isdigit() for character in core) or unit_word(core) is not None


def _read(text, units, loose=False):
    """Sort the words of a line into (prices, quantities, item words).

    Returns None for more than one price or quantity, or a number that can't be
    right (zero, or more packs than MAX_PACKS). With loose (free text), a pack
    count plus a size marked "each" also reads as packs x size, and a bare
    number is marked as one.
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
            parts.append(_bare(core, marked=loose))                           # a bare "2"
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
        # "2 packs ... 10 rolls each". Without "each" rolls or pieces could be the total ("2 packs 10 rolls"),
        # unless it is one pack. A weight or volume after the count is the size on the label, even with
        # the brand between: "4 bottles 400ml", "2 bottles vete 800ml".
        label = amount["unit"] in ("g", "ml") and parts.index(amount) > parts.index(count)
        if count["packs"] == 1 or label or any(word in _EACH for word in words):
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


def _bare(number, marked) -> dict:
    """A number on its own counts packs. Free text marks it, so that resolve_quantity can ask instead."""
    quantity = {"kind": "packs", "packs": _number(number)}
    return {**quantity, "bare": True} if marked else quantity


def _counted(number, unit) -> dict:
    """A number with its unit word: an amount in base units, or a count of packs."""
    base_unit, multiplier = unit
    if base_unit == PACK:
        return {"kind": "packs", "packs": _number(number)}
    return {"kind": "amount", "amount": _number(float(number) * multiplier), "unit": base_unit}


def _number(value):
    value = round(float(value), 6)
    return int(value) if value.is_integer() else value
