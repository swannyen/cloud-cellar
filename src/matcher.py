"""Match an item phrase to a tracked item without AI (PLAN.md §5.2). Pure: no I/O."""
import re
from difflib import SequenceMatcher

# A fuzzy match is for typos only. It must score at least the threshold, beat
# every other item by the margin, and pair every word with a word on the other side.
FUZZY_THRESHOLD = 0.85
FUZZY_MARGIN = 0.05
WORD_THRESHOLD = 0.75

# Leading words that say nothing about the product ("finished the dish soap").
FILLER_WORDS = ("the", "a", "an", "some", "my", "our")

_ES_PLURALS = ("sses", "xes", "zes", "ches", "shes", "oes")
_NOT_PLURALS = ("ss", "us", "is")


def match(phrase, items):
    """Return (item_id, how) for the item the phrase means, or None if it isn't clear.

    how is "exact" (a name or saved alias) or "fuzzy" (a close spelling, which
    the family is asked to confirm).
    """
    wanted = normalise(phrase)
    if not wanted:
        return None
    candidates = [(item["item_id"], names(item)) for item in items]
    return _exact(wanted, candidates) or _fuzzy(wanted, candidates)


def match_phrase(phrase, items, brands=()):
    """match(), after taking a known brand off the front of the phrase.

    brands are the names taught with /brand; brands recorded on earlier entries
    count too. Returns (match or None, the phrase without the brand, the brand
    or None). A phrase that is itself a known name is left whole.
    """
    found = match(phrase, items)
    if found and found[1] == "exact":
        return found, phrase, None
    recorded = [brand for item in items for brand in item.get("brands", [])]
    brand, rest = split_brand(phrase, [*brands, *recorded])
    if brand is None:
        return found, phrase, None
    return match(rest, items), rest, brand


def split_brand(phrase, brands):
    """Return (brand, the rest of the phrase) if the phrase starts with one of the brands, else (None, phrase).

    Only a known brand is taken off: an unknown first word is often what tells
    two products apart ("hand masks").
    """
    words = plain(phrase).split()
    for brand in sorted(brands, key=len, reverse=True):
        brand_words = plain(brand).split()
        if brand_words and words[:len(brand_words)] == brand_words and len(words) > len(brand_words):
            return brand, " ".join(words[len(brand_words):])
    return None, phrase


def plain(text) -> str:
    """Lowercase, without punctuation or extra spaces. Unlike normalise, every word stays as written."""
    return " ".join(re.sub(r"[\W_]+", " ", re.sub(r"['’]", "", text.lower())).split())


def normalise(phrase) -> str:
    """Lowercase, without punctuation, extra spaces or leading filler words, last word in the singular."""
    text = re.sub(r"['’]", "", phrase.lower())
    words = re.sub(r"[\W_]+", " ", text).split()
    while len(words) > 1 and words[0] in FILLER_WORDS:
        words = words[1:]
    if words:
        words[-1] = _singular(words[-1])
    return " ".join(words)


def names(item) -> set:
    """Every normalised name the item goes by: its name and its aliases."""
    return {normalise(name) for name in [item["name"], *item.get("aliases", [])]} - {""}


def mentions(text, items) -> bool:
    """Whether a sentence names any tracked item, as whole words ("the rice finish already")."""
    sentence = f" {_each_word(text)} "
    return any(f" {_each_word(name)} " in sentence for item in items for name in names(item))


def _each_word(text) -> str:
    """Like normalise, but with every word in the singular, so word order and position don't matter."""
    return " ".join(normalise(word) for word in normalise(text).split())


def _exact(wanted, candidates):
    for item_id, known in candidates:
        if wanted in known:
            return item_id, "exact"
    return None


def _fuzzy(wanted, candidates):
    scores = sorted(
        (max(((_ratio(wanted, name), name) for name in known), default=(0, "")) + (item_id,)
         for item_id, known in candidates),
        reverse=True,
    )
    if not scores:
        return None
    best, name, item_id = scores[0]
    runner_up = scores[1][0] if len(scores) > 1 else 0
    if best >= FUZZY_THRESHOLD and best - runner_up >= FUZZY_MARGIN and _words_pair_up(wanted, name):
        return item_id, "fuzzy"
    return None


def _words_pair_up(a, b) -> bool:
    """Whether every word of each phrase is close to some word of the other.

    This is what keeps "hair masks" from matching "hand masks": the phrases are
    alike overall, but one word is a different word, not a typo.
    """
    first, second = a.split(), b.split()
    return all(
        max(_ratio(word, other) for other in theirs) >= WORD_THRESHOLD
        for ours, theirs in ((first, second), (second, first))
        for word in ours
    )


def _ratio(a, b) -> float:
    return SequenceMatcher(None, a, b).ratio()


def _singular(word) -> str:
    if word.endswith(_ES_PLURALS):
        return word[:-2]
    if len(word) > 3 and word.endswith("s") and not word.endswith(_NOT_PLURALS):
        return word[:-1]
    return word
