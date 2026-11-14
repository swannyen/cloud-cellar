"""Currencies (PLAN.md §8.5): the ones prices can be logged in, how each is written and shown, and conversion.

Pure: no I/O. rates.py finds the exchange rates.

Every price is kept in the currency it was paid in. The base currency
(HOME_CURRENCY, SGD) is what a plain "$" or a bare 12.90 means, and what
prices in other currencies are compared in.
"""
import math
import os
import re

# Entries logged before events stored a currency, or a base currency, are in this one: SGD,
# whatever HOME_CURRENCY says when they are read, so a change of base currency can't relabel them.
LEGACY_CURRENCY = "SGD"

# The currencies prices can be logged in. Adding one is a row here and a deploy; Frankfurter has the rates.
# "before": written in front of the amount, touching it or with one space ("RM12.90", "RM 12.90").
# "after": written after the amount, the same way ("12.90myr", "50 yuan").
# All lowercase, and chosen so that none of them can be an item word. A plain "$" is the base currency.
# "sign" is how the bot shows an amount in it.
CURRENCIES = {
    "SGD": {"sign": "$", "before": ("s$", "sgd"), "after": ("sgd",)},
    "MYR": {"sign": "RM", "before": ("rm", "myr"), "after": ("myr", "rm", "ringgit")},
    "USD": {"sign": "US$", "before": ("us$", "usd"), "after": ("usd",)},
    # "¥" is yuan while JPY isn't tracked. Adding JPY means deciding what "¥" means first.
    "CNY": {"sign": "¥", "before": ("¥", "￥", "rmb", "cny"), "after": ("cny", "rmb", "yuan", "元")},
}

# Currencies people may write that aren't tracked. A line priced in one is refused, so that the
# price doesn't end up in the item's name ("snacks 500jpy") or get read as SGD.
UNTRACKED = {
    "JPY": {"before": ("jpy",), "after": ("jpy", "yen")},
    "THB": {"before": ("thb", "฿"), "after": ("thb", "baht", "฿")},
    "IDR": {"before": ("idr", "rp"), "after": ("idr", "rupiah")},
    "VND": {"before": ("vnd", "₫"), "after": ("vnd", "₫")},
    "KRW": {"before": ("krw", "₩"), "after": ("krw",)},
    "TWD": {"before": ("twd", "nt$"), "after": ("twd",)},
    "HKD": {"before": ("hkd", "hk$"), "after": ("hkd",)},
    "AUD": {"before": ("aud", "a$"), "after": ("aud",)},
    "NZD": {"before": ("nzd", "nz$"), "after": ("nzd",)},
    "EUR": {"before": ("eur", "€"), "after": ("eur", "euro", "euros", "€")},
    "GBP": {"before": ("gbp", "£"), "after": ("gbp",)},
    "PHP": {"before": ("php", "₱"), "after": ("php", "peso", "pesos")},
    "INR": {"before": ("inr", "₹"), "after": ("inr", "rupees")},
}

HOME_SIGN = "$"  # a plain "$" means the base currency


def home() -> str:
    """The base currency: what a plain "$" means, and what every price is compared in."""
    return os.environ.get("HOME_CURRENCY", "SGD")


def foreign_codes() -> list:
    """The tracked currencies other than the base one, in a fixed order."""
    return sorted(code for code in CURRENCIES if code != home())


def is_tracked(code) -> bool:
    return code in CURRENCIES


def sign(code) -> str:
    """How an amount in the currency is shown: "$" for the base currency, "RM" for MYR."""
    if code is None or code == home():
        return HOME_SIGN
    return CURRENCIES[code]["sign"] if code in CURRENCIES else f"{code} "


def show(amount, code=None) -> str:
    """An amount with its sign: "$12.90", "RM18.00", "¥50.00"."""
    return f"{sign(code)}{amount:.2f}"


def show_rate(code, fx_to_home) -> str:
    """"RM1 = $0.315": what one unit of the currency is worth in the base currency."""
    return f"{sign(code)}1 = {HOME_SIGN}{rate_text(fx_to_home)}"


def rate_text(value) -> str:
    """A rate as people read it: two decimals from 1 up, and below that enough to show three figures ("0.315")."""
    decimals = 2 if value >= 1 else min(6, 2 - math.floor(math.log10(value)))
    return f"{value:.{decimals}f}"


def to_home(amount, fx_to_home) -> float:
    """An amount in the base currency, to the cent."""
    return round(amount * fx_to_home, 2)


def rate_figure(value) -> float:
    """A rate kept to six significant figures, so that small ones (IDR) keep their precision."""
    return float(f"{value:.6g}")


def code_for(word):
    """The currency a word names: its code ("myr"), or one of its markers ("rm", "yuan", "¥"). None if none.

    Untracked currencies count too, so that the caller can say they aren't tracked.
    """
    word = word.strip().lower()
    if word == HOME_SIGN:
        return home()
    for code, markers in {**CURRENCIES, **UNTRACKED}.items():
        if word == code.lower() or word in markers["before"] or word in markers["after"]:
            return code
    return None


# --- Reading prices -------------------------------------------------------------

_AMOUNT = r"\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?"   # "1,200" before "1200", so the comma isn't left over


def _markers(side) -> dict:
    """Every marker written on one side of an amount -> its currency."""
    found = {}
    for code, markers in {**CURRENCIES, **UNTRACKED}.items():
        for marker in markers[side]:
            assert found.get(marker, code) == code, marker  # one marker, one currency
            found[marker] = code
    return found


_BEFORE = {**_markers("before"), HOME_SIGN: None}   # None: the base currency, whichever it is
_AFTER = _markers("after")


def _alternation(markers) -> str:
    # Longest first, so that "us$" is tried before "$" and "rmb" before "rm".
    return "|".join(re.escape(marker) for marker in sorted(markers, key=len, reverse=True))


_BEFORE_ALT, _AFTER_ALT = _alternation(_BEFORE), _alternation(_AFTER)
# One token that is a price: a marker in front, or one after, touching the amount.
_TOKEN = re.compile(
    rf"(?P<before>{_BEFORE_ALT})?(?P<amount>{_AMOUNT})(?P<after>{_AFTER_ALT})?", re.IGNORECASE)
# The same, written anywhere in a text. A marker in front isn't part of a word ("farm 12"), and
# one after isn't the start of a word ("12 yen" but not "12 yenta") or the marker of the next
# amount ("RM18 SGD5.70"). Only Latin letters count, since Chinese is written without spaces ("¥50一包").
_NOT_AFTER = r"(?![a-z\d])"
_IN_TEXT = re.compile(
    rf"(?<![a-z])(?P<before>{_BEFORE_ALT})(?P<amount1>{_AMOUNT})"
    rf"|(?P<amount2>{_AMOUNT})(?P<after>{_AFTER_ALT}){_NOT_AFTER}",
    re.IGNORECASE,
)
# A marked price with the marker touching, for the patterns below (after join_markers).
_MARKED = rf"(?:(?:{_BEFORE_ALT})(?:{_AMOUNT})|(?:{_AMOUNT})(?:{_AFTER_ALT}))"
_SPACE_AFTER_MARKER = re.compile(rf"(?<![^\s(/=≈~])({_BEFORE_ALT})\s(?=\d)", re.IGNORECASE)   # "RM 12"
_SPACE_BEFORE_MARKER = re.compile(rf"(\d)\s({_AFTER_ALT}){_NOT_AFTER}", re.IGNORECASE)          # "50 yuan"
# A price in the forms below: a marked one, or a bare amount with two decimals, which is in the base currency.
_PRICED = rf"(?:{_MARKED}|(?<![\w.,])\d+\.\d\d(?![\w.]))"
_BRACKETED = re.compile(rf"\(\s*[~≈]?\s*({_PRICED})\s*\)", re.IGNORECASE)                       # "($5.70)", "(5.70)"
_BETWEEN = re.compile(rf"({_PRICED})\s*[/=≈~]\s*(?=[~≈]?\s*{_PRICED})", re.IGNORECASE)          # "RM18 / $5.70"
_ABOUT = re.compile(rf"(?<!\S)[~≈]\s*(?={_PRICED})", re.IGNORECASE)                               # "~$5.70", "~5.70"


def join_markers(text) -> str:
    """Close the one space people put between a marker and its amount: "RM 12.90" -> "RM12.90", "50 yuan" -> "50yuan"."""
    return _SPACE_BEFORE_MARKER.sub(r"\1\2", _SPACE_AFTER_MARKER.sub(r"\1", text))


def separate_prices(text) -> str:
    """Stand each price on its own in a line of the format: "RM18 ($5.70)", "RM18/5.70" and "~$5.70" -> "RM18 $5.70".

    Only around and between prices, with a marker or a bare one with two decimals (5.70):
    a bracket or a slash anywhere else is left alone.
    """
    text = _BRACKETED.sub(r" \1 ", join_markers(text))
    return _ABOUT.sub("", _BETWEEN.sub(r"\1 ", text))


def read_price(token):
    """(amount, currency) for one token that is a price in a tracked currency, or None.

    "$12.90" and "S$12.90" are SGD, "RM18" MYR, "50yuan" CNY. A bare number isn't one: the
    format decides about those. A token with markers for two currencies ("$12myr") isn't one either.
    """
    match = _TOKEN.fullmatch(token)
    if not match or not (match["before"] or match["after"]):
        return None
    codes = {_currency(match["before"], _BEFORE), _currency(match["after"], _AFTER)} - {False}
    if len(codes) != 1:
        return None
    code = codes.pop()
    return (_amount(match["amount"]), code) if is_tracked(code) else None


def has_price(text) -> bool:
    """Whether a price with a marker is written somewhere in the text, in any currency it knows."""
    return bool(_IN_TEXT.search(join_markers(text)))


def untracked_in(text):
    """The first currency written in the text that the bot knows of but doesn't track ("JPY500"), or None."""
    for match in _IN_TEXT.finditer(join_markers(text)):
        code = _match_currency(match)
        if not is_tracked(code):
            return code
    return None


def money_in(text) -> list:
    """Every amount written in a text, in order of kind: (amount, currency) for each price written with
    a marker, in any currency it knows, then (amount, None) for each other number."""
    found = []

    def take(match):
        found.append((_amount(match["amount1"] or match["amount2"]), _match_currency(match)))
        return " "

    rest = _IN_TEXT.sub(take, join_markers(text))
    return found + [(float(number), None) for number in re.findall(r"\d+(?:\.\d+)?", rest)]


def _match_currency(match):
    if match["before"]:
        return _currency(match["before"], _BEFORE)
    return _currency(match["after"], _AFTER)


def _currency(marker, markers):
    """The currency a marker names, or False for no marker. A plain "$" is the base currency."""
    if marker is None:
        return False
    return markers[marker.lower()] or home()


def _amount(written) -> float:
    return float(written.replace(",", ""))
