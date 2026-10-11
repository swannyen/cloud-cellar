"""Message-format parsing tests (PLAN.md §5.1). Pure: no table, no AI."""
import unittest

import fakes  # noqa: F401  (puts src/ on sys.path)

import structured
from structured import NEED_PACK_SIZE, UNCLEAR_COUNT, UNREADABLE, WRONG_UNIT

DONKI = {"store_id": "don-don-donki", "name": "Don Don Donki", "aliases": ["donki"]}


def amount(value, unit):
    return {"kind": "amount", "amount": value, "unit": unit}


def packs(count):
    return {"kind": "packs", "packs": count}


def bare(count):
    """A number with no unit or pack word, as free text marks it."""
    return {"kind": "packs", "packs": count, "bare": True}


def packs_of(count, size, unit=None):
    return {"kind": "packs_of", "packs": count, "size": size, "unit": unit}


def entry(action, phrase, quantity=None, price=None, store=None, store_is_new=False):
    return {"action": action, "phrase": phrase, "quantity": quantity, "total_price": price,
            "store": store, "store_is_new": store_is_new}


def read(line, **context):
    """parse_line without the "raw" and "explicit" keys, to keep expectations short."""
    result = structured.parse_line(line, **context)
    if isinstance(result, dict):
        result = {key: value for key, value in result.items() if key not in ("raw", "explicit")}
    return result


class KeywordTest(unittest.TestCase):
    def test_every_keyword_and_synonym(self):
        cases = {
            "bought": ["+ rice", "bought rice", "buy rice", "add rice", "Add rice"],
            "finished": ["- rice", "done rice", "finished rice", "out rice"],
            "opened_last": ["low rice", "last rice"],
            "stock_count": ["have rice 2"],
            "price_check": ["? rice 5kg $5", "price rice 5kg $5"],
        }
        for action, lines in cases.items():
            for line in lines:
                with self.subTest(line=line):
                    result = read(line)
                    self.assertEqual((result["action"], result["phrase"]), (action, "rice"))

    def test_symbol_can_touch_the_item(self):
        self.assertEqual(read("+rice"), entry("bought", "rice"))
        self.assertEqual(read("-dish soap"), entry("finished", "dish soap"))
        self.assertEqual(read("?rice 5kg $5"), entry("price_check", "rice", amount(5000, "g"), 5))

    def test_add_reads_like_bought(self):
        self.assertEqual(read("add toilet paper 2x10 $12.90 @fairprice"), read("bought toilet paper 2x10 $12.90 @fairprice"))
        self.assertEqual(read("add toilet paper 2x10 $12.90 @fairprice"),
                         entry("bought", "toilet paper", packs_of(2, 10), 12.9, "FairPrice"))

    def test_symbols_and_entry_commands_are_the_format_used_on_purpose(self):
        for line in ("+ rice", "-dish soap", "  ? rice 5kg $5", "+", "/add rice", "/used rice", "/Bought@bettynuffbot rice",
                     "/stocktake rice 2"):
            with self.subTest(line=line):
                self.assertTrue(structured.is_explicit(line))
        for line in ("bought rice", "add rice", "have you eaten", "rice + beans", "", "/items", "/help add"):
            with self.subTest(line=line):
                self.assertFalse(structured.is_explicit(line))
        self.assertTrue(structured.parse_line("+ rice")["explicit"])
        self.assertTrue(structured.parse_line("/add rice")["explicit"])
        self.assertFalse(structured.parse_line("add rice")["explicit"])

    def test_entry_commands_read_like_their_symbols(self):
        self.assertEqual(read("/add toilet paper 2x10 $12.90 @fairprice"), read("+ toilet paper 2x10 $12.90 @fairprice"))
        self.assertEqual(read("/bought toilet paper 2x10 $12.90 @fairprice"), read("+ toilet paper 2x10 $12.90 @fairprice"))
        self.assertEqual(read("/used dish soap"), read("- dish soap"))
        self.assertEqual(read("/stocktake rice 2x5kg"), read("have rice 2x5kg"))
        self.assertEqual(read("/Stocktake@bettynuffbot pocket tissues 90 packets"),
                         entry("stock_count", "pocket tissues", packs(90)))
        self.assertEqual(read("/ADD@bettynuffbot rice 5kg $12 from ntuc"),
                         entry("bought", "rice", amount(5000, "g"), 12, "FairPrice"))
        # What was typed is kept, not the symbol it stands for.
        self.assertEqual(structured.parse_line("/add  rice 5kg")["raw"], "/add rice 5kg")

    def test_entry_commands_follow_the_same_rules_as_any_other_line(self):
        self.assertIs(read("/add 2 3"), UNREADABLE)
        for line in ("/add", "/used", "/bought@bettynuffbot", "/stocktake"):
            with self.subTest(line=line):
                self.assertIsNone(read(line))        # nothing after it: the handler answers with how to use it
                self.assertTrue(structured.is_entry_command(line))
        for line in ("/items", "/address rice", "/adds rice", "add rice", "+ rice"):
            with self.subTest(line=line):
                self.assertFalse(structured.is_entry_command(line))
        self.assertIsNone(read("/items rice"))

    def test_keywords_are_case_insensitive(self):
        self.assertEqual(read("BOUGHT rice"), entry("bought", "rice"))
        self.assertEqual(read("Low Shampoo"), entry("opened_last", "Shampoo"))

    def test_uppercase_input(self):
        self.assertEqual(read("+ RICE 5KG $12 @NTUC"), entry("bought", "RICE", amount(5000, "g"), 12, "FairPrice"))
        self.assertEqual(read("+ HAND masks 10 PCS $1.45 FROM TAOBAO"),
                         entry("bought", "HAND masks", amount(10, "piece"), 1.45, "Taobao"))

    def test_a_keyword_must_be_a_whole_word(self):
        for line in ("buying rice tomorrow", "outside now", "lastly, rice", "haven't eaten", "boughtrice",
                     "added rice", "address is 5 rice road"):
            with self.subTest(line=line):
                self.assertIsNone(read(line))
                self.assertFalse(structured.has_keyword(line))

    def test_lines_without_a_keyword_are_not_in_the_format(self):
        for line in ("ok thanks", "rice finish already", "what time is dinner?", "", "   "):
            with self.subTest(line=line):
                self.assertIsNone(read(line))

    def test_a_keyword_with_nothing_after_it_is_ordinary_chat(self):
        for line in ("done", "out", "?", "??", "-", "---", "+", "-_-", "? !"):
            with self.subTest(line=line):
                self.assertIsNone(read(line))


class QuantityTest(unittest.TestCase):
    def test_the_plan_examples(self):
        self.assertEqual(read("+ toilet paper 2x10 $12.90 @fairprice"),
                         entry("bought", "toilet paper", packs_of(2, 10), 12.9, "FairPrice"))
        self.assertEqual(read("- dish soap"), entry("finished", "dish soap"))
        self.assertEqual(read("low shampoo"), entry("opened_last", "shampoo"))
        self.assertEqual(read("have rice 2"), entry("stock_count", "rice", packs(2)))
        self.assertEqual(read("? detergent 3kg $8.95 @shengsiong"),
                         entry("price_check", "detergent", amount(3000, "g"), 8.95, "Sheng Siong"))

    def test_number_with_a_unit_word_is_an_amount_in_base_units(self):
        cases = {
            "2pcs": (2, "piece"), "1pc": (1, "piece"), "6 pieces": (6, "piece"), "5 pcs": (5, "piece"),
            "3 ea": (3, "piece"), "4nos": (4, "piece"), "2 units": (2, "piece"),
            "10rolls": (10, "roll"), "10 rolls": (10, "roll"), "1 rl": (1, "roll"),
            "100sheets": (100, "sheet"), "90 sht": (90, "sheet"),
            "500g": (500, "g"), "250 gm": (250, "g"), "3kg": (3000, "g"), "5 KG": (5000, "g"), "0.5kg": (500, "g"),
            "500ml": (500, "ml"), "2L": (2000, "ml"), "1.5 litres": (1500, "ml"), "1 ltr": (1000, "ml"),
        }
        for text, (value, unit) in cases.items():
            with self.subTest(text=text):
                self.assertEqual(read(f"+ thing {text}"), entry("bought", "thing", amount(value, unit)))

    def test_packs_times_size(self):
        cases = {
            "2x10": packs_of(2, 10),
            "2x5pcs": packs_of(2, 5, "piece"),
            "2x10rolls": packs_of(2, 10, "roll"),
            "3x2L": packs_of(3, 2000, "ml"),
            "2 x 500ml": packs_of(2, 500, "ml"),
            "2×10 rolls": packs_of(2, 10, "roll"),
            "4*1.5l": packs_of(4, 1500, "ml"),
            "2 packs of 10 rolls": packs_of(2, 10, "roll"),
            "3 boxes of 100sheets": packs_of(3, 100, "sheet"),
            "500ml x2": packs_of(2, 500, "ml"),
            "5kg x 2": packs_of(2, 5000, "g"),
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(read(f"+ thing {text}"), entry("bought", "thing", expected))

    def test_pack_words_and_bare_numbers_count_packs(self):
        for text in ("2", "2pkt", "2 packs", "2 boxes", "2 bottles", "2 btl", "2 cans", "2 cartons", "2 tubs"):
            with self.subTest(text=text):
                self.assertEqual(read(f"+ thing {text}"), entry("bought", "thing", packs(2)))
        self.assertEqual(read("have rice 0.5")["quantity"], packs(0.5))

    def test_unknown_word_after_a_number_belongs_to_the_item(self):
        self.assertEqual(read("+ 2 sticks facial masks $1.96 from taobao"),
                         entry("bought", "sticks facial masks", packs(2), 1.96, "Taobao"))

    def test_taught_unit_word(self):
        taught = {"sticks": "pcs", "ctn": "carton"}
        self.assertEqual(read("+ 2 sticks facial masks $1.96", units=taught),
                         entry("bought", "facial masks", amount(2, "piece"), 1.96))
        self.assertEqual(read("+ milk 2ctn", units=taught), entry("bought", "milk", packs(2)))

    def test_unit_words_can_be_part_of_the_item_phrase(self):
        self.assertEqual(read("+ loo roll 1x10 $6"), entry("bought", "loo roll", packs_of(1, 10), 6))
        self.assertEqual(read("have kitchen roll 2"), entry("stock_count", "kitchen roll", packs(2)))
        self.assertEqual(read("+ tea bags 2 boxes"), entry("bought", "tea bags", packs(2)))

    def test_glue_words_next_to_a_number_are_not_item_words(self):
        self.assertEqual(read("bought 2 sticks facial masks for 1.96 from Taobao"),
                         entry("bought", "sticks facial masks", packs(2), 1.96, "Taobao"))
        self.assertEqual(read("+ sticks facial masks for 2 pcs $1.96"),
                         entry("bought", "sticks facial masks for", amount(2, "piece"), 1.96))  # "for 2 pcs": not a price
        self.assertEqual(read("bought 2 packs of rice for $24"), entry("bought", "rice", packs(2), 24))
        self.assertEqual(read("have 5 pcs of face masks"), entry("stock_count", "face masks", amount(5, "piece")))
        # Elsewhere they are ordinary words.
        self.assertEqual(read("+ food for cats 2"), entry("bought", "food for cats", packs(2)))
        self.assertEqual(read("+ bag of chips 2"), entry("bought", "bag of chips", packs(2)))

    def test_numbers_inside_words_stay_in_the_item_phrase(self):
        self.assertEqual(read("+ 100plus 6"), entry("bought", "100plus", packs(6)))
        self.assertEqual(read("+ 3-in-1 coffee 2"), entry("bought", "3-in-1 coffee", packs(2)))

    def test_no_quantity(self):
        self.assertEqual(read("+ rice $12"), entry("bought", "rice", None, 12))
        self.assertEqual(read("have rice"), entry("stock_count", "rice"))

    def test_minus_without_a_quantity_means_it_has_run_out(self):
        for line in ("- dish soap", "/used dish soap", "done dish soap", "finished dish soap", "out dish soap"):
            with self.subTest(line=line):
                self.assertEqual(read(line), entry("finished", "dish soap"))
        self.assertEqual(read("- 7up"), entry("finished", "7up"))
        self.assertEqual(read("- 100plus"), entry("finished", "100plus"))
        # A price or a store on such a line means nothing, and is left out.
        self.assertEqual(read("- dish soap $3 @ntuc"), entry("finished", "dish soap"))

    def test_minus_with_a_quantity_means_that_much_was_used(self):
        cases = {
            "- pocket tissues 90": packs(90),
            "/used pocket tissues 90 packets": packs(90),
            "- pocket tissues 90pcs": amount(90, "piece"),
            "- rice 2kg": amount(2000, "g"),
            "- toilet paper 2x10": packs_of(2, 10),
            "finished toilet paper 3 rolls": amount(3, "roll"),
            "- 2 pocket tissues": packs(2),
        }
        for line, quantity in cases.items():
            with self.subTest(line=line):
                result = read(line)
                self.assertEqual((result["action"], result["quantity"]), ("used", quantity))
                self.assertNotIn(result["phrase"], ("", "90"))
        self.assertEqual(read("/used pocket tissues 90 packets"), entry("used", "pocket tissues", packs(90)))
        self.assertIs(read("- rice 2 3"), UNREADABLE)

    def test_large_pack_counts_are_fine(self):
        # A family that buys pocket tissues 90 packets at a time has 180 of them.
        self.assertEqual(read("have pocket tissues 180")["quantity"], packs(180))
        self.assertEqual(read("+ pocket tissues 180x6pcs")["quantity"], packs_of(180, 6, "piece"))
        self.assertEqual(read("- pocket tissues 1000")["quantity"], packs(1000))

    def test_low_lines_take_everything_as_the_item(self):
        self.assertEqual(read("low rice @ntuc"), entry("opened_last", "rice"))
        self.assertEqual(read("low 100 plus"), entry("opened_last", "100 plus"))

    def test_the_line_is_kept_as_raw(self):
        self.assertEqual(structured.parse_line("  +  rice   5kg ")["raw"], "+ rice 5kg")


class PriceTest(unittest.TestCase):
    def test_price_with_and_without_cents(self):
        self.assertEqual(read("+ rice $12")["total_price"], 12)
        self.assertEqual(read("+ rice $12.90")["total_price"], 12.9)
        self.assertEqual(read("+ rice $ 12.90")["total_price"], 12.9)
        self.assertIsInstance(read("+ rice $12")["total_price"], int)

    def test_bare_number_with_two_decimals_is_a_price(self):
        self.assertEqual(read("+ facial masks 5 pcs 1.86 @taobao"),
                         entry("bought", "facial masks", amount(5, "piece"), 1.86, "Taobao"))
        self.assertEqual(read("+ rice 2 12.90"), entry("bought", "rice", packs(2), 12.9))

    def test_other_bare_numbers_are_not_prices(self):
        self.assertEqual(read("+ rice 2.5"), entry("bought", "rice", packs(2.5)))
        self.assertEqual(read("+ rice 2.50 kg"), entry("bought", "rice", amount(2500, "g")))

    def test_tokens_come_in_any_order(self):
        expected = entry("bought", "olive oil", amount(1000, "ml"), 15.5, "Giant", True)
        self.assertEqual(read("+ $15.50 olive 1L oil @giant"), expected)
        self.assertEqual(read("+ olive oil 1l, $15.50 @giant"), expected)


class StoreTest(unittest.TestCase):
    def test_built_in_stores_are_normalised(self):
        cases = {
            "ntuc": "FairPrice", "FairPrice": "FairPrice", "fp": "FairPrice", "fair price": "FairPrice",
            "ss": "Sheng Siong", "sheng siong": "Sheng Siong", "ShengSiong": "Sheng Siong",
            "cs": "Cold Storage", "cold storage": "Cold Storage", "tb": "Taobao", "TAOBAO": "Taobao",
        }
        for typed, expected in cases.items():
            with self.subTest(typed=typed):
                result = read(f"+ rice @{typed}")
                self.assertEqual((result["store"], result["store_is_new"]), (expected, False))

    def test_every_built_in_spelling_is_recognised_however_it_is_written_in_the_table(self):
        for spelling, name in structured.STORE_ALIASES.items():
            with self.subTest(spelling=spelling):
                result = read(f"+ rice @{spelling}")
                self.assertEqual((result["store"], result["store_is_new"]), (name, False))
                self.assertEqual(read(f"+ rice from {spelling.upper()}")["store"], name)

    def test_a_store_typed_with_at_sign_for_the_first_time_is_new(self):
        self.assertEqual(read("+ rice @giant"), entry("bought", "rice", store="Giant", store_is_new=True))
        self.assertEqual(read("+ rice @ don don donki ")["store"], "Don Don Donki")
        self.assertEqual(read("+ rice @MUSTAFA")["store"], "Mustafa")

    def test_saved_stores_are_known_by_name_and_alias(self):
        for typed in ("donki", "Don Don Donki", "dondondonki"):
            with self.subTest(typed=typed):
                result = read(f"+ rice @{typed}", stores=[DONKI])
                self.assertEqual((result["store"], result["store_is_new"]), ("Don Don Donki", False))

    def test_store_name_with_a_space(self):
        self.assertEqual(read("+ rice 5kg $12 @sheng siong"),
                         entry("bought", "rice", amount(5000, "g"), 12, "Sheng Siong"))

    def test_from_or_at_before_a_known_store(self):
        self.assertEqual(read("+ hand masks 10 pcs $1.45 from taobao"),
                         entry("bought", "hand masks", amount(10, "piece"), 1.45, "Taobao"))
        self.assertEqual(read("+ rice 5kg at sheng siong"), entry("bought", "rice", amount(5000, "g"), None, "Sheng Siong"))
        self.assertEqual(read("+ rice 5kg from donki", stores=[DONKI])["store"], "Don Don Donki")

    def test_from_or_at_before_anything_else_stays_in_the_item(self):
        self.assertEqual(read("+ bread at home"), entry("bought", "bread at home"))
        self.assertEqual(read("+ rice 5kg from giant"), entry("bought", "rice from giant", amount(5000, "g")))

    def test_find_store(self):
        self.assertEqual(structured.find_store("got rice from Taobao"), ("Taobao", False))
        self.assertEqual(structured.find_store("got rice @Giant"), ("Giant", True))
        self.assertEqual(structured.find_store("got rice at home"), (None, False))

    def test_empty_or_overlong_store(self):
        self.assertIsNone(read("+ rice @")["store"])
        self.assertEqual(len(read("+ rice @" + "x" * 80)["store"]), structured.MAX_STORE_LENGTH)


class UnreadableTest(unittest.TestCase):
    def test_lines_that_try_the_format_but_cannot_be_read(self):
        cases = {
            "+ 2 3": "two bare numbers and no item",
            "+ rice 2 3": "two bare numbers",
            "+ rice 2pcs 3pcs": "two quantities",
            "+ rice 2 5kg": "two quantities",
            "+ rice 2x5kg 1kg": "two quantities",
            "+ $5": "no item",
            "+1": "no item",
            "+ @ntuc": "no item",
            "+ rice $5 $6": "two prices",
            "+ rice $5 6.50": "two prices",
            "? detergent 3kg": "price check without a price",
            "? detergent $8.95": "price check without a quantity",
            "+ rice 0": "zero",
            "+ rice 0kg": "zero",
            "+ rice $0": "zero",
            "+ rice 5000": "more packs than is believable",
            "+ rice 2x10pkt": "a pack size has to be in base units",
            "bought 2 packs toilet paper 10 rolls each $12.90 fairprice": "free text, not the format",
        }
        for line, why in cases.items():
            with self.subTest(line=line, why=why):
                self.assertIs(read(line), UNREADABLE)

    def test_which_unreadable_lines_have_words_worth_a_second_look(self):
        for line in ("+ rice 2 3", "bought 2 packs toilet paper 10 rolls each $12.90 fairprice"):
            with self.subTest(line=line):
                self.assertTrue(structured.has_words(line))
        for line in ("+ 2 3", "+ $5", "+1", "price $5"):
            with self.subTest(line=line):
                self.assertFalse(structured.has_words(line))


class ResolveQuantityTest(unittest.TestCase):
    """Turning what was written into a total for one item, without guessing."""

    def resolve(self, text, base_unit="piece", pack_size=None, units=None):
        quantity = structured.parse_line(f"+ thing {text}", units=units)["quantity"]
        return structured.resolve_quantity(quantity, base_unit, pack_size)

    def test_amount_is_taken_as_written(self):
        self.assertEqual(self.resolve("2pcs"), {"qty": 2, "packs": None, "units_per_pack": None})
        self.assertEqual(self.resolve("5kg", "g"), {"qty": 5000, "packs": None, "units_per_pack": None})

    def test_packs_times_size_gives_the_total_and_the_pack_size(self):
        self.assertEqual(self.resolve("2x5pcs"), {"qty": 10, "packs": 2, "units_per_pack": 5})
        self.assertEqual(self.resolve("2x10", "roll"), {"qty": 20, "packs": 2, "units_per_pack": 10})
        self.assertEqual(self.resolve("3x2L", "ml"), {"qty": 6000, "packs": 3, "units_per_pack": 2000})

    def test_pack_count_converts_with_a_saved_pack_size(self):
        for text in ("2", "2pkt", "2 boxes"):
            with self.subTest(text=text):
                self.assertEqual(self.resolve(text, "roll", pack_size=10),
                                 {"qty": 20, "packs": 2, "units_per_pack": None})

    def test_pack_count_without_a_saved_pack_size_is_not_guessed(self):
        for text in ("2", "2pkt", "2 sticks"):
            with self.subTest(text=text):
                self.assertEqual(self.resolve(text), NEED_PACK_SIZE)

    def test_taught_word_makes_the_same_line_an_amount(self):
        self.assertEqual(self.resolve("2 sticks", units={"sticks": "pcs"})["qty"], 2)

    def test_no_quantity_is_one_pack_if_the_size_is_known(self):
        self.assertEqual(structured.resolve_quantity(None, "roll", 10), {"qty": 10, "packs": 1, "units_per_pack": None})
        self.assertEqual(structured.resolve_quantity(None, "roll", None), NEED_PACK_SIZE)

    def test_unit_that_does_not_fit_the_item_is_rejected(self):
        self.assertEqual(self.resolve("500ml", "piece"), WRONG_UNIT)
        self.assertEqual(self.resolve("2x10rolls", "piece"), WRONG_UNIT)
        self.assertEqual(self.resolve("2pcs", "g"), WRONG_UNIT)

    def test_grams_and_millilitres_stand_in_for_each_other(self):
        self.assertEqual(self.resolve("3L", "g")["qty"], 3000)
        self.assertEqual(self.resolve("2x500g", "ml")["qty"], 1000)

    def test_bare_number_from_a_sentence_is_not_taken_for_packs_of_something_counted(self):
        # "got 30 eggs": 30 eggs, or 30 trays?
        for base_unit in ("piece", "roll", "sheet"):
            with self.subTest(base_unit=base_unit):
                self.assertEqual(structured.resolve_quantity(bare(30), base_unit, 10), UNCLEAR_COUNT)
        # Without a pack size the usual question already offers both readings.
        self.assertEqual(structured.resolve_quantity(bare(30), "piece", None), NEED_PACK_SIZE)

    def test_bare_number_from_a_sentence_still_counts_packs_of_what_is_weighed_or_poured(self):
        # "got 2 shampoo" can only be 2 bottles.
        self.assertEqual(structured.resolve_quantity(bare(2), "ml", 500), {"qty": 1000, "packs": 2, "units_per_pack": None})
        self.assertEqual(structured.resolve_quantity(bare(2), "g", 5000)["qty"], 10000)

    def test_in_the_format_a_bare_number_is_packs_as_the_help_says(self):
        self.assertEqual(self.resolve("30", "piece", pack_size=10), {"qty": 300, "packs": 30, "units_per_pack": None})


class FreeTextQuantityTest(unittest.TestCase):
    """read_quantity: the same rules, for quantity words copied out of a sentence."""

    def test_forms_shared_with_the_format(self):
        cases = {
            "5 pcs": amount(5, "piece"), "5kg bag": amount(5000, "g"), "3 x 2L": packs_of(3, 2000, "ml"),
            "2x10 rolls": packs_of(2, 10, "roll"), "3 bottles": packs(3), "2": bare(2),
            "2 packs of 10 rolls": packs_of(2, 10, "roll"), "twin pack 2x225g": packs_of(2, 225, "g"),
            "10-roll pack": amount(10, "roll"), "500ml x2": packs_of(2, 500, "ml"),
            "2 packs for $9.90 (usual 6.50 each), 6 rolls per pack": packs_of(2, 6, "roll"),
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(structured.read_quantity(text), expected)

    def test_pack_count_with_a_size_marked_each(self):
        expected = packs_of(2, 10, "roll")
        for text in ("2 packs toilet paper 10 rolls each", "2 packs, 10 rolls each", "10 rolls per pack, 2 packs"):
            with self.subTest(text=text):
                self.assertEqual(structured.read_quantity(text), expected)

    def test_one_named_pack(self):
        self.assertEqual(structured.read_quantity("1 bottle of Kikkoman soy sauce 600ml"), packs_of(1, 600, "ml"))
        self.assertEqual(structured.read_quantity("pack of 10 rolls"), packs_of(1, 10, "roll"))
        self.assertEqual(structured.read_quantity("a box of 100 sheets"), packs_of(1, 100, "sheet"))

    def test_taught_words_and_unknown_words(self):
        self.assertEqual(structured.read_quantity("2 sticks"), bare(2))
        self.assertEqual(structured.read_quantity("2 sticks", {"sticks": "pcs"}), amount(2, "piece"))

    def test_number_that_does_not_say_what_it_counts_is_marked(self):
        for text in ("30", "a tray of 30 eggs", "30 eggs", "35 sticks"):
            with self.subTest(text=text):
                self.assertTrue(structured.read_quantity(text)["bare"])
        # A pack word or a size says what the number counts, and the format never marks one.
        for text in ("3 bottles", "2 pkt", "2 packs toilet paper 10 rolls each", "2 toilet paper 10 rolls each"):
            with self.subTest(text=text):
                self.assertNotIn("bare", structured.read_quantity(text))
        self.assertEqual(structured.parse_line("+ eggs 30")["quantity"], packs(30))

    def test_text_that_is_not_one_clear_quantity(self):
        for text in ("some", "a few", "2 packs 10 rolls", "2 3", ""):
            with self.subTest(text=text):
                self.assertIsNone(structured.read_quantity(text))


class MessageTest(unittest.TestCase):
    def test_each_line_of_a_message_is_read_on_its_own(self):
        text = "Back from NTUC!\n+ rice 5kg $12 @ntuc\n\n- dish soap\n+ 2 3\n"
        results = structured.parse(text)
        self.assertEqual([line for line, _ in results],
                         ["Back from NTUC!", "+ rice 5kg $12 @ntuc", "- dish soap", "+ 2 3"])
        self.assertIsNone(results[0][1])
        self.assertEqual(results[1][1]["phrase"], "rice")
        self.assertEqual(results[2][1]["action"], "finished")
        self.assertIs(results[3][1], UNREADABLE)

    def test_empty_message(self):
        self.assertEqual(structured.parse(""), [])

    def test_line_without_a_keyword_can_be_read_as_a_purchase(self):
        result = structured.parse_bought("facial masks  5 pcs 1.86 from taobao")
        self.assertEqual(result, {**entry("bought", "facial masks", amount(5, "piece"), 1.86, "Taobao"),
                                  "raw": "facial masks 5 pcs 1.86 from taobao", "explicit": False})
        self.assertIs(structured.parse_bought("2 3"), UNREADABLE)

    def test_numbers_in_text(self):
        self.assertEqual(structured.numbers_in("2 packs for $12.90, 10 rolls each"), [2.0, 12.9, 10.0])


if __name__ == "__main__":
    unittest.main()
