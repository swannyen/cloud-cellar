"""Reply formatting tests."""
import unittest

import fakes  # noqa: F401  (puts src/ on sys.path)

import replies

TP = {"item_id": "toilet-paper", "name": "Toilet paper", "base_unit": "roll"}
RICE = {"item_id": "rice", "name": "Rice", "base_unit": "g"}
SOAP = {"item_id": "dish-soap", "name": "Dishwashing liquid", "base_unit": "ml"}


def ev(action="bought", **fields):
    event = {"action": action, "packs": None, "units_per_pack": None, "total_price": None,
             "unit_price": None, "store": None, "brand": None}
    event.update(fields)
    return event


class EventLineTest(unittest.TestCase):
    def test_full_purchase_matches_the_plan_example(self):
        event = ev(packs=2, units_per_pack=10, total_price=12.9, unit_price=0.645, store="FairPrice")
        self.assertEqual(
            replies.event_line(TP, event),
            "✓ Toilet paper: bought 2 × 10 rolls for $12.90 at FairPrice ($0.65/roll).",
        )

    def test_single_pack_shows_just_the_size_and_price_per_100g(self):
        event = ev(packs=1, units_per_pack=5000, total_price=13.5, unit_price=0.0027, store="Sheng Siong")
        self.assertEqual(
            replies.event_line(RICE, event),
            "✓ Rice: bought 5 kg for $13.50 at Sheng Siong ($0.27/100 g).",
        )

    def test_purchase_with_missing_details(self):
        self.assertEqual(replies.event_line(TP, ev()), "✓ Toilet paper: bought.")
        self.assertEqual(replies.event_line(TP, ev(packs=3)), "✓ Toilet paper: bought 3 packs.")
        self.assertEqual(replies.event_line(TP, ev(packs=1)), "✓ Toilet paper: bought 1 pack.")
        self.assertEqual(
            replies.event_line(SOAP, ev(packs=2, total_price=15, store="Watsons")),
            "✓ Dishwashing liquid: bought 2 packs for $15.00 at Watsons.",
        )

    def test_new_item_is_flagged(self):
        self.assertEqual(replies.event_line(TP, ev(packs=1), created=True), "✓ Toilet paper (new item): bought 1 pack.")

    def test_status_events(self):
        self.assertEqual(replies.event_line(SOAP, ev("finished")), "✓ Dishwashing liquid: marked finished.")
        self.assertEqual(replies.event_line(TP, ev("opened_last")), "✓ Toilet paper: marked as running low.")
        self.assertEqual(replies.event_line(TP, ev("still_have")), "✓ Toilet paper: noted, still have plenty.")
        self.assertEqual(replies.event_line(RICE, ev("stock_count", packs=3)), "✓ Rice: counted 3 packs in stock.")
        self.assertEqual(replies.event_line(RICE, ev("stock_count")), "✓ Rice: noted as in stock.")

    def test_price_check_has_no_tick(self):
        event = ev("price_check", packs=1, units_per_pack=3000, total_price=8.95, unit_price=8.95 / 3000, store="Giant")
        self.assertEqual(replies.event_line(RICE, event), "Rice: $8.95 for 3 kg = $0.30/100 g at Giant. Price noted.")
        self.assertEqual(replies.event_line(RICE, ev("price_check", total_price=8.95)), "Rice: $8.95. Price noted.")

    def test_undo_confirmation_says_what_was_removed(self):
        event = ev(packs=2, units_per_pack=10, total_price=12.9, unit_price=0.645, store="FairPrice")
        self.assertEqual(
            replies.undone(TP, event),
            "Undone: Toilet paper: bought 2 × 10 rolls for $12.90 at FairPrice ($0.65/roll).",
        )


class FormattingTest(unittest.TestCase):
    def test_quantity(self):
        cases = [
            ((3000, "g"), "3 kg"), ((3600, "g"), "3.6 kg"), ((500, "g"), "500 g"),
            ((2000, "ml"), "2 L"), ((750, "ml"), "750 ml"),
            ((10, "roll"), "10 rolls"), ((1, "roll"), "1 roll"),
            ((30, "piece"), "30 pieces"), ((200, "sheet"), "200 sheets"),
        ]
        for args, expected in cases:
            with self.subTest(args=args):
                self.assertEqual(replies.quantity(*args), expected)

    def test_unit_price(self):
        self.assertEqual(replies.unit_price(0.645, "roll"), "$0.65/roll")
        self.assertEqual(replies.unit_price(0.004, "sheet"), "$0.004/sheet")
        self.assertEqual(replies.unit_price(0.0031, "g"), "$0.31/100 g")
        self.assertEqual(replies.unit_price(0.00295, "ml"), "$0.29/100 ml")


class ItemsListTest(unittest.TestCase):
    def test_empty(self):
        self.assertEqual(replies.items_list([]), replies.NO_ITEMS)

    def test_lists_ids_names_units_and_state_sorted_by_name(self):
        items = [
            dict(TP, status="ok", stock_est=2),
            dict(SOAP, status="out", stock_est=0),
            dict(RICE),
        ]
        self.assertEqual(replies.items_list(items), "\n".join([
            "Tracked items:",
            "dish-soap — Dishwashing liquid (ml): out",
            "rice — Rice (g)",
            "toilet-paper — Toilet paper (roll): ok, 2 packs",
        ]))


if __name__ == "__main__":
    unittest.main()
