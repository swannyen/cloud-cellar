"""Item matching tests (PLAN.md §5.2). Pure: no table, no AI."""
import unittest

import fakes  # noqa: F401  (puts src/ on sys.path)

import matcher


def item(item_id, name, *aliases):
    return {"item_id": item_id, "name": name, "base_unit": "piece", "aliases": list(aliases)}


ITEMS = [
    item("toilet-paper", "Toilet paper", "loo roll", "TP"),
    item("dish-soap", "Dishwashing liquid", "dish soap"),
    item("eggs", "Eggs"),
    item("tissue-box", "Tissue box", "tissues"),
    item("rice", "Rice"),
]


class MatchTest(unittest.TestCase):
    def test_exact_name(self):
        self.assertEqual(matcher.match("toilet paper", ITEMS), ("toilet-paper", "exact"))
        self.assertEqual(matcher.match("  Toilet-Paper! ", ITEMS), ("toilet-paper", "exact"))

    def test_alias(self):
        self.assertEqual(matcher.match("loo roll", ITEMS), ("toilet-paper", "exact"))
        self.assertEqual(matcher.match("tp", ITEMS), ("toilet-paper", "exact"))
        self.assertEqual(matcher.match("Dish Soap", ITEMS), ("dish-soap", "exact"))

    def test_plural_and_singular_are_the_same(self):
        self.assertEqual(matcher.match("egg", ITEMS), ("eggs", "exact"))
        self.assertEqual(matcher.match("toilet papers", ITEMS), ("toilet-paper", "exact"))
        self.assertEqual(matcher.match("tissue boxes", ITEMS), ("tissue-box", "exact"))
        self.assertEqual(matcher.match("tissue", ITEMS), ("tissue-box", "exact"))

    def test_typo(self):
        self.assertEqual(matcher.match("toilet papr", ITEMS), ("toilet-paper", "fuzzy"))
        self.assertEqual(matcher.match("dishwashing liquad", ITEMS), ("dish-soap", "fuzzy"))

    def test_unknown_first_word_is_kept(self):
        self.assertIsNone(matcher.match("kleenex toilet paper", ITEMS))
        self.assertIsNone(matcher.match("baby rice", ITEMS))
        self.assertEqual(matcher.match_phrase("kleenex toilet paper", ITEMS), (None, "kleenex toilet paper", None))

    def test_a_different_describing_word_is_a_different_item(self):
        masks = [item("face-masks", "Face masks"), item("hair-masks", "Hair masks")]
        self.assertIsNone(matcher.match("hand masks", masks))
        self.assertIsNone(matcher.match("facial masks", masks))
        self.assertIsNone(matcher.match("facial mask", masks[:1]))
        self.assertIsNone(matcher.match("hand mask", masks[:1]))
        self.assertIsNone(matcher.match("hand soap", [item("dish-soap", "Dish soap")]))
        # Alike overall (0.875), but "hand" is a different word from "hair", not a typo of it.
        self.assertIsNone(matcher.match("hand moisturiser", [item("hair-moisturiser", "Hair moisturiser")]))

    def test_leading_filler_words_are_ignored(self):
        self.assertEqual(matcher.match("the dish soap", ITEMS), ("dish-soap", "exact"))
        self.assertEqual(matcher.match("some rice", ITEMS), ("rice", "exact"))
        self.assertEqual(matcher.match("my eggs", ITEMS), ("eggs", "exact"))

    def test_two_similar_items_are_left_to_the_ai(self):
        items = [item("kitchen-towel", "Kitchen towel"), item("kitchen-bowl", "Kitchen bowl")]
        self.assertIsNone(matcher.match("kitchen towl", items))
        # With only one of them tracked, the same typo is close enough.
        self.assertEqual(matcher.match("kitchen towl", items[:1]), ("kitchen-towel", "fuzzy"))

    def test_unknown_phrase(self):
        for phrase in ("soy sauce", "detergent", "x", "", "!!!"):
            with self.subTest(phrase=phrase):
                self.assertIsNone(matcher.match(phrase, ITEMS))

    def test_no_items(self):
        self.assertIsNone(matcher.match("rice", []))


class BrandTest(unittest.TestCase):
    MASKS = [item("face-masks", "Face masks"), item("facial-masks", "Facial masks")]

    def test_taught_brand_is_taken_off_the_front(self):
        self.assertEqual(matcher.match_phrase("kans facial masks", self.MASKS, ["kans"]),
                         (("facial-masks", "exact"), "facial masks", "kans"))
        self.assertEqual(matcher.match_phrase("KANS Facial-Masks", self.MASKS, ["Kans"]),
                         (("facial-masks", "exact"), "facial masks", "Kans"))

    def test_brand_recorded_on_an_earlier_entry_counts_too(self):
        items = ITEMS + [dict(item("body-wash", "Body wash"), brands=["Shokubutsu", "Royal Umbrella"])]
        self.assertEqual(matcher.match_phrase("shokubutsu toilet paper", items),
                         (("toilet-paper", "exact"), "toilet paper", "Shokubutsu"))
        self.assertEqual(matcher.match_phrase("Royal Umbrella rice", items), (("rice", "exact"), "rice", "Royal Umbrella"))

    def test_the_rest_can_still_be_a_typo_or_unknown(self):
        self.assertEqual(matcher.match_phrase("kans facial maskss", self.MASKS, ["kans"]),
                         (("facial-masks", "fuzzy"), "facial maskss", "kans"))
        self.assertEqual(matcher.match_phrase("kans sleeping mask", self.MASKS, ["kans"]), (None, "sleeping mask", "kans"))

    def test_a_phrase_that_is_itself_a_known_name_is_left_whole(self):
        items = [item("face-masks", "Face masks", "kans masks")]
        self.assertEqual(matcher.match_phrase("kans masks", items, ["kans"]), (("face-masks", "exact"), "kans masks", None))

    def test_a_brand_on_its_own_is_not_split(self):
        self.assertEqual(matcher.split_brand("kans", ["kans"]), (None, "kans"))

    def test_brands_are_compared_as_written_not_in_the_singular(self):
        # "kans" and "pampers" end in s; they must not be cut down to "kan" and "pamper".
        self.assertEqual(matcher.split_brand("Pampers diapers", ["pampers"]), ("pampers", "diapers"))
        self.assertEqual(matcher.split_brand("kan facial masks", ["kans"]), (None, "kan facial masks"))

    def test_longest_brand_wins(self):
        self.assertEqual(matcher.split_brand("royal umbrella rice", ["Royal", "Royal Umbrella"]), ("Royal Umbrella", "rice"))

    def test_plain(self):
        self.assertEqual(matcher.plain("  Head & Shoulders' Shampoos "), "head shoulders shampoos")


class MentionsTest(unittest.TestCase):
    def test_a_sentence_that_names_a_tracked_item(self):
        for text in ("rice finish already", "the Toilet Paper is almost gone", "we need more eggs!",
                     "last box of tissues", "TP?"):
            with self.subTest(text=text):
                self.assertTrue(matcher.mentions(text, ITEMS))

    def test_a_sentence_that_does_not(self):
        for text in ("ok thanks", "what time is dinner", "the price is crazy", "paper", ""):
            with self.subTest(text=text):
                self.assertFalse(matcher.mentions(text, ITEMS))
        self.assertFalse(matcher.mentions("rice finish already", []))


class NormaliseTest(unittest.TestCase):
    def test_case_punctuation_and_spaces(self):
        self.assertEqual(matcher.normalise("  LOO   roll "), "loo roll")
        self.assertEqual(matcher.normalise("Toilet-Paper!"), "toilet paper")
        self.assertEqual(matcher.normalise("kellogg's cornflakes"), "kelloggs cornflake")
        self.assertEqual(matcher.normalise("?!"), "")

    def test_trailing_plural_is_dropped(self):
        cases = {
            "eggs": "egg", "tissue boxes": "tissue box", "glasses": "glass", "sponges": "sponge",
            "tomatoes": "tomato", "matches": "match", "brushes": "brush",
            # not plurals
            "rice": "rice", "gas": "gas", "hummus": "hummus", "glass": "glass",
        }
        for phrase, expected in cases.items():
            with self.subTest(phrase=phrase):
                self.assertEqual(matcher.normalise(phrase), expected)

    def test_names_are_the_name_and_aliases_normalised(self):
        self.assertEqual(matcher.names(ITEMS[0]), {"toilet paper", "loo roll", "tp"})
        self.assertEqual(matcher.names({"item_id": "x", "name": "Eggs"}), {"egg"})


if __name__ == "__main__":
    unittest.main()
