"""Currency tests (PLAN.md §8.5): the tracked currencies, how prices in them are written and shown. Pure."""
import unittest
import unittest.mock

import fakes  # noqa: F401  (puts src/ on sys.path)

import currency


class ReadPriceTest(unittest.TestCase):
    def test_every_marker_in_the_plan(self):
        cases = {
            "SGD": ["$12.90", "S$12.90", "SGD12.90", "sgd12.90", "12.90sgd"],
            "MYR": ["RM12.90", "rm12.90", "MYR12.90", "12.90myr", "12.90rm", "12.90ringgit"],
            "USD": ["US$12.90", "us$12.90", "USD12.90", "12.90usd"],
            "CNY": ["¥12.90", "￥12.90", "CNY12.90", "RMB12.90", "12.90cny", "12.90rmb", "12.90yuan", "12.90元"],
        }
        for code, tokens in cases.items():
            for token in tokens:
                with self.subTest(token=token):
                    self.assertEqual(currency.read_price(token), (12.9, code))

    def test_a_plain_dollar_is_the_base_currency(self):
        self.assertEqual(currency.read_price("$12"), (12, "SGD"))
        with unittest.mock.patch.dict("os.environ", {"HOME_CURRENCY": "MYR"}):
            self.assertEqual(currency.read_price("$12"), (12, "MYR"))

    def test_thousands_separators(self):
        self.assertEqual(currency.read_price("RM1,200"), (1200, "MYR"))
        self.assertEqual(currency.read_price("¥12,345.50"), (12345.5, "CNY"))

    def test_what_isnt_a_price_in_a_tracked_currency(self):
        for token in ("12", "12.90", "rm", "$", "$12myr", "RM12,50", "JPY500", "500yen", "€4.50", "12pcs", "farm12"):
            with self.subTest(token=token):
                self.assertIsNone(currency.read_price(token))


class WrittenInTextTest(unittest.TestCase):
    def test_a_space_after_a_marker_in_front_or_before_one_after(self):
        self.assertEqual(currency.join_markers("rice RM 12.90 and 50 yuan, $ 12, US$ 5, 5 usd"),
                         "rice RM12.90 and 50yuan, $12, US$5, 5usd")

    def test_words_that_only_look_like_markers_are_left_alone(self):
        self.assertEqual(currency.join_markers("+ farm 12 eggs"), "+ farm 12 eggs")
        self.assertEqual(currency.join_markers("+ 2 yenta dolls"), "+ 2 yenta dolls")

    def test_prices_are_stood_on_their_own(self):
        cases = {
            "rice RM18 ($5.70)": "rice RM18 $5.70",
            "rice RM18 / S$5.70": "rice RM18 S$5.70",
            "rice RM18/$5.70": "rice RM18 $5.70",
            "rice RM18 = $5.70": "rice RM18 $5.70",
            "rice RM16 ~$5.05": "rice RM16 $5.05",
            "rice RM18 (≈ $5.70)": "rice RM18 $5.70",
            "rice $5.70 (RM18)": "rice $5.70 RM18",
            "rice ≈$5.70": "rice $5.70",
            "rice RM18 (5.70)": "rice RM18 5.70",
            "rice RM18/5.70": "rice RM18 5.70",
            "rice RM18 = 5.70": "rice RM18 5.70",
            "rice RM18 ~5.70": "rice RM18 5.70",
            "rice 5.70 (RM18)": "rice 5.70 RM18",
            "rice (12.90)": "rice 12.90",
        }
        for written, read in cases.items():
            with self.subTest(written=written):
                self.assertEqual(" ".join(currency.separate_prices(written).split()), read)

    def test_brackets_and_slashes_elsewhere_stay(self):
        for text in ("bin liners (U.P. $6.40)", "milk 2x1L 15/9", "body wash (pink) $5", "eggs (10) RM18",
                     "rice (5.7) RM18", "milk ~1.50L RM18", "rice 15.09.26 RM18"):
            with self.subTest(text=text):
                self.assertEqual(currency.separate_prices(text), text)

    def test_untracked_currencies_are_found(self):
        cases = {"snacks JPY500": "JPY", "500 yen": "JPY", "€4.50": "EUR", "4,50€": "EUR", "฿120": "THB",
                 "Rp25000": "IDR", "HK$30": "HKD", "3 pesos": "PHP", "₩5000": "KRW"}
        for text, code in cases.items():
            with self.subTest(text=text):
                self.assertEqual(currency.untracked_in(text), code)
        for text in ("rice RM18 $5.70", "¥50", "2 bottles", "+ toilet paper 2x10 $12.90"):
            with self.subTest(text=text):
                self.assertIsNone(currency.untracked_in(text))

    def test_money_in_a_sentence(self):
        self.assertEqual(currency.money_in("2 bottles of shampoo in JB for RM25.90, about $8.10 on the card"),
                         [(25.9, "MYR"), (8.1, "SGD"), (2.0, None)])
        self.assertEqual(currency.money_in("¥50一包, 3包"), [(50.0, "CNY"), (3.0, None)])
        self.assertEqual(currency.money_in("fabric conditioner 2 bottles 3.10"), [(2.0, None), (3.1, None)])

    def test_has_price(self):
        self.assertTrue(currency.has_price("$6.40)"))
        self.assertTrue(currency.has_price("RM5each"))
        self.assertFalse(currency.has_price("(U.P."))
        self.assertFalse(currency.has_price("3m"))


class ShowTest(unittest.TestCase):
    def test_amounts_with_their_sign(self):
        self.assertEqual([currency.show(12.9), currency.show(12.9, "SGD"), currency.show(18, "MYR"),
                          currency.show(5, "USD"), currency.show(50, "CNY")],
                         ["$12.90", "$12.90", "RM18.00", "US$5.00", "¥50.00"])

    def test_rates_show_three_figures(self):
        self.assertEqual(currency.show_rate("MYR", 0.315), "RM1 = $0.315")
        self.assertEqual(currency.show_rate("MYR", 0.3), "RM1 = $0.300")
        self.assertEqual(currency.show_rate("CNY", 1 / 5.1901), "¥1 = $0.193")
        self.assertEqual(currency.show_rate("USD", 1 / 0.77888), "US$1 = $1.28")
        self.assertEqual(currency.rate_text(0.0000705), "0.000071")

    def test_codes_for_words(self):
        cases = {"myr": "MYR", "RM": "MYR", "ringgit": "MYR", "usd": "USD", "US$": "USD", "yuan": "CNY",
                 "rmb": "CNY", "¥": "CNY", "sgd": "SGD", "$": "SGD", "jpy": "JPY", "nonsense": None}
        for word, code in cases.items():
            with self.subTest(word=word):
                self.assertEqual(currency.code_for(word), code)


class MathsTest(unittest.TestCase):
    def test_conversion_is_to_the_cent(self):
        self.assertEqual(currency.to_home(18, 0.315), 5.67)
        self.assertEqual(currency.to_home(25.9, 1 / 3.1744), 8.16)

    def test_rates_keep_six_figures(self):
        self.assertEqual(currency.rate_figure(1 / 3.1744), 0.315020)
        self.assertEqual(currency.rate_figure(1 / 14181), 7.05169e-05)

    def test_the_tracked_currencies(self):
        self.assertEqual(currency.foreign_codes(), ["CNY", "MYR", "USD"])
        self.assertTrue(currency.is_tracked("MYR"))
        self.assertFalse(currency.is_tracked("JPY"))

    def test_no_marker_belongs_to_two_currencies(self):
        for side in ("before", "after"):
            markers = [marker for table in (currency.CURRENCIES, currency.UNTRACKED)
                       for found in table.values() for marker in found[side]]
            self.assertEqual(len(markers), len(set(markers)), side)


if __name__ == "__main__":
    unittest.main()
