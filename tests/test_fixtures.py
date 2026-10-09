"""Checks on the parser fixture file and on the eval script's grader. No API calls."""
import importlib.util
import unittest
from pathlib import Path

import fakes  # noqa: F401  (puts src/ on sys.path)

import parser

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("eval_parser", ROOT / "scripts" / "eval_parser.py")
eval_parser = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(eval_parser)


class FixtureFileTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.known_items, cls.fixtures = eval_parser.load_fixtures()

    def test_there_are_enough_fixtures_including_chatter(self):
        self.assertGreaterEqual(len(self.fixtures), 30)
        self.assertGreaterEqual(sum(eval_parser.is_chatter(fixture) for fixture in self.fixtures), 5)

    def test_ids_are_unique_and_no_message_is_repeated_with_the_same_known_items(self):
        ids = [fixture["id"] for fixture in self.fixtures]
        self.assertEqual(len(ids), len(set(ids)))
        cases = [(fixture["text"], tuple(fixture.get("known", ["*"]))) for fixture in self.fixtures]
        self.assertEqual(len(cases), len(set(cases)))

    def test_products_the_bot_has_never_seen_are_covered(self):
        unseen = [f for f in self.fixtures if "known" in f and any("new" in event for event in f["expect"])]
        actions = {event["action"] for fixture in unseen for event in fixture["expect"]}
        self.assertGreaterEqual(actions, {"bought", "finished", "opened_last", "stock_count", "still_have"})

    def test_expected_events_are_well_formed(self):
        all_ids = {item["item_id"] for item in self.known_items}
        allowed = {"action", "item_id", "new", "base_unit", "packs", "units_per_pack", "total_price", "store", "brand"}
        for fixture in self.fixtures:
            known = set(fixture.get("known", all_ids))
            self.assertLessEqual(known, all_ids, fixture["id"])
            for event in fixture["expect"]:
                with self.subTest(fixture=fixture["id"]):
                    self.assertLessEqual(set(event), allowed)
                    self.assertIn(event["action"], parser.ACTIONS)
                    self.assertNotEqual("item_id" in event, "new" in event, "needs item_id or new, not both")
                    if "item_id" in event:
                        self.assertIn(event["item_id"], known)
                    else:
                        unit = event["base_unit"]
                        for option in unit["any"] if isinstance(unit, dict) else [unit]:
                            self.assertIn(option, parser.BASE_UNITS)

    def test_every_action_is_covered(self):
        actions = {event["action"] for fixture in self.fixtures for event in fixture["expect"]}
        self.assertEqual(actions, set(parser.ACTIONS))

    def test_grader_accepts_right_answers_and_rejects_wrong_ones(self):
        # Also fails if a fixture's text has been copied into the system prompt.
        self.assertEqual(eval_parser.selftest(self.known_items, self.fixtures), [])


class GraderTest(unittest.TestCase):
    KNOWN = [{"item_id": "rice", "name": "Rice", "base_unit": "g", "aliases": []}]
    FIXTURE = {"id": "x", "text": "t", "expect": [
        {"action": "bought", "item_id": "rice", "packs": 1, "units_per_pack": 5000, "total_price": 13.5, "store": "Sheng Siong"}
    ]}

    def got(self, **changes):
        event = {"action": "bought", "item_id": "rice", "item_name": "rice", "base_unit": "g", "packs": 1,
                 "units_per_pack": 5000, "total_price": 13.5, "store": "sheng siong", "brand": None}
        event.update(changes)
        return [event]

    def test_store_names_ignore_case_and_spacing(self):
        self.assertTrue(eval_parser.grade(self.FIXTURE, self.got(store="ShengSiong"), None, self.KNOWN)[0])

    def test_each_wrong_field_is_named(self):
        ok, hits, problems = eval_parser.grade(self.FIXTURE, self.got(total_price=13.0, brand="Royal"), None, self.KNOWN)
        self.assertFalse(ok)
        self.assertEqual(problems, ["bought rice: wrong total_price, brand"])
        self.assertEqual((hits["total_price"], hits["action"]), ([0, 1], [1, 1]))

    def test_a_value_where_none_is_expected_is_wrong(self):
        fixture = dict(self.FIXTURE, expect=[{"action": "finished", "item_id": "rice"}])
        self.assertFalse(eval_parser.grade(fixture, self.got(action="finished"), None, self.KNOWN)[0])

    def test_events_are_matched_in_any_order(self):
        known = self.KNOWN + [{"item_id": "eggs", "name": "Eggs", "base_unit": "piece", "aliases": []}]
        fixture = {"id": "x", "text": "t", "expect": [
            {"action": "finished", "item_id": "rice"}, {"action": "finished", "item_id": "eggs"},
        ]}
        blank = dict(packs=None, units_per_pack=None, total_price=None, store=None)
        events = (self.got(action="finished", item_id="eggs", base_unit="piece", **blank)
                  + self.got(action="finished", **blank))
        self.assertTrue(eval_parser.grade(fixture, events, None, known)[0])

    def test_unwanted_question_fails_chatter(self):
        chatter = {"id": "c", "text": "hello", "expect": []}
        self.assertTrue(eval_parser.grade(chatter, [], None, self.KNOWN)[0])
        self.assertFalse(eval_parser.grade(chatter, [], "Which item?", self.KNOWN)[0])


if __name__ == "__main__":
    unittest.main()
