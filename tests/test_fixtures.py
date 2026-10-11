"""Checks on the free-text fixture file and on the eval script's grader. No API calls."""
import importlib.util
import unittest
from unittest import mock
from pathlib import Path

import fakes  # noqa: F401  (puts src/ on sys.path)

import ai
import structured

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("eval_freetext", ROOT / "scripts" / "eval_freetext.py")
eval_freetext = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(eval_freetext)


class FixtureFileTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.known_items, cls.fixtures = eval_freetext.load_fixtures()

    def test_there_are_enough_fixtures_including_chatter(self):
        self.assertGreaterEqual(len(self.fixtures), 20)
        self.assertGreaterEqual(sum(eval_freetext.is_chatter(fixture) for fixture in self.fixtures), 5)

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
        allowed = {"action", "item_id", "new", "base_unit", "qty", "packs", "count", "total_price", "store", "brand"}
        for fixture in self.fixtures:
            known = set(fixture.get("known", all_ids))
            self.assertLessEqual(known, all_ids, fixture["id"])
            for event in fixture["expect"]:
                with self.subTest(fixture=fixture["id"]):
                    self.assertLessEqual(set(event), allowed)
                    self.assertIn(event["action"], ai.ACTIONS)
                    self.assertNotEqual("item_id" in event, "new" in event, "needs item_id or new, not both")
                    self.assertLessEqual(len({"qty", "packs", "count"} & set(event)), 1, "one way of giving the quantity")
                    if "item_id" in event:
                        self.assertIn(event["item_id"], known)
                    else:
                        unit = event["base_unit"]
                        for option in unit["any"] if isinstance(unit, dict) else [unit]:
                            self.assertIn(option, ai.BASE_UNITS)

    def test_every_action_is_covered(self):
        actions = {event["action"] for fixture in self.fixtures for event in fixture["expect"]}
        self.assertEqual(actions, set(ai.ACTIONS))

    def test_grader_accepts_right_answers_and_rejects_wrong_ones(self):
        # Also fails if a fixture's text has been copied into the system prompt.
        self.assertEqual(eval_freetext.selftest(self.known_items, self.fixtures), [])


class RunTest(unittest.TestCase):
    """The live run, with the Claude call faked: every fixture gets the answer the oracle would give."""

    @classmethod
    def setUpClass(cls):
        cls.known_items, cls.fixtures = eval_freetext.load_fixtures()

    def run_with(self, answer):
        with mock.patch.object(eval_freetext.ai, "parse_free_text", side_effect=answer), \
                mock.patch.object(eval_freetext.ai, "ground_free_text", side_effect=lambda events, text: events):
            return eval_freetext.run(self.known_items, self.fixtures)

    def oracle(self, text, sender, items, today):
        fixture = next(f for f in self.fixtures if f["text"] == text and len(eval_freetext.items_for(f, self.known_items)) == len(items))
        entries, clarify = eval_freetext.oracle_output(fixture, items)
        return eval_freetext.ai.Parsed(entries, clarify, {"input_tokens": 100, "output_tokens": 10}, "claude-haiku-5-5")

    def test_right_answers_meet_the_targets_and_are_added_up(self):
        results = self.run_with(self.oracle)
        self.assertTrue(results.targets_met)
        self.assertEqual((results.passed, results.total), (len(self.fixtures), len(self.fixtures)))
        self.assertEqual((results.chatter_events, results.failures, results.errors), (0, [], []))
        self.assertEqual(results.tokens_in, 100 * len(self.fixtures))
        expected_events = sum(len(fixture["expect"]) for fixture in self.fixtures)
        self.assertEqual(results.hits["action"], [expected_events, expected_events])

    def test_failed_calls_are_reported_and_miss_the_target(self):
        def fail(*args):
            raise eval_freetext.claude_api.ClaudeError("HTTP 529 overloaded_error")
        results = self.run_with(fail)
        self.assertFalse(results.targets_met)
        self.assertEqual((results.total, len(results.errors)), (0, len(self.fixtures)))

    def test_an_answer_from_another_model_is_a_failed_call(self):
        def other_model(*args):
            return eval_freetext.ai.Parsed([], None, {}, "claude-opus-5-5")
        self.assertIn("served by 'claude-opus-5-5'", self.run_with(other_model).errors[0])

    def test_events_on_chatter_miss_the_target(self):
        def chatty(text, sender, items, today):
            parsed = self.oracle(text, sender, items, today)
            if not parsed.events and not parsed.clarify:
                parsed.events = [{"action": "bought", "item_id": None, "phrase": "Tickets", "base_unit": "piece",
                                  "quantity": None, "total_price": None, "store": None, "brand": None, "missed": ()}]
            return parsed
        results = self.run_with(chatty)
        self.assertFalse(results.targets_met)
        self.assertEqual(results.chatter_events, sum(eval_freetext.is_chatter(f) for f in self.fixtures))


class GroundingFixtureTest(unittest.TestCase):
    """PLAN.md Phase 1b: every expected store and number is written in its message (or is null)."""

    @classmethod
    def setUpClass(cls):
        cls.known_items, cls.fixtures = eval_freetext.load_fixtures()

    def test_expected_prices_are_written_in_the_message(self):
        for fixture in self.fixtures:
            written = structured.numbers_in(fixture["text"])
            for event in fixture["expect"]:
                with self.subTest(fixture=fixture["id"]):
                    if "total_price" in event:
                        self.assertIn(float(event["total_price"]), written)

    def test_expected_stores_are_written_in_the_message(self):
        names = structured.store_names()
        for fixture in self.fixtures:
            text = structured.squash(fixture["text"])
            for event in fixture["expect"]:
                store = event.get("store")
                for option in store["any"] if isinstance(store, dict) else [store]:
                    with self.subTest(fixture=fixture["id"], store=option):
                        if option is not None:
                            spellings = [spelling for spelling, name in names.items() if name == option]
                            self.assertTrue(any(spelling in text for spelling in spellings + [structured.squash(option)]))

    def test_the_plans_grounding_examples_are_fixtures(self):
        texts = {fixture["text"] for fixture in self.fixtures}
        self.assertIn("Facial masks 5 pcs 1.86 from Taobao", texts)
        self.assertIn("HAND masks 10 pcs $1.45 from Taobao", texts)


class GraderTest(unittest.TestCase):
    FIXTURE = {"id": "x", "text": "t", "expect": [
        {"action": "bought", "item_id": "rice", "qty": 5000, "total_price": 13.5, "store": "Sheng Siong"}
    ]}

    def got(self, **changes):
        entry = {"action": "bought", "item_id": "rice", "phrase": "rice", "base_unit": "g",
                 "quantity": {"kind": "amount", "amount": 5000, "unit": "g"},
                 "total_price": 13.5, "store": "sheng siong", "brand": None, "missed": ()}
        entry.update(changes)
        return [entry]

    def passes(self, fixture, entries):
        return eval_freetext.grade(fixture, entries, None)[0]

    def test_store_names_ignore_case_and_spacing(self):
        self.assertTrue(self.passes(self.FIXTURE, self.got(store="ShengSiong")))

    def test_each_wrong_field_is_named(self):
        ok, hits, problems = eval_freetext.grade(self.FIXTURE, self.got(total_price=13.0, brand="Royal"), None)
        self.assertFalse(ok)
        self.assertEqual(problems, ["bought rice: wrong total_price, brand"])
        self.assertEqual((hits["total_price"], hits["action"]), ([0, 1], [1, 1]))

    def test_a_total_can_be_written_as_an_amount_or_as_packs_times_size(self):
        self.assertTrue(self.passes(self.FIXTURE, self.got(quantity={"kind": "packs_of", "packs": 2, "size": 2500, "unit": "g"})))
        self.assertFalse(self.passes(self.FIXTURE, self.got(quantity={"kind": "amount", "amount": 500, "unit": "g"})))
        self.assertFalse(self.passes(self.FIXTURE, self.got(quantity={"kind": "packs", "packs": 5000})))
        self.assertFalse(self.passes(self.FIXTURE, self.got(quantity=None)))

    def test_a_pack_count_is_not_a_total(self):
        fixture = dict(self.FIXTURE, expect=[{"action": "stock_count", "item_id": "rice", "packs": 3}])
        blank = dict(total_price=None, store=None)
        self.assertTrue(self.passes(fixture, self.got(action="stock_count", quantity={"kind": "packs", "packs": 3}, **blank)))
        self.assertFalse(self.passes(fixture, self.got(action="stock_count", quantity={"kind": "amount", "amount": 3, "unit": "g"}, **blank)))

    def test_a_number_that_does_not_say_what_it_counts_is_not_a_pack_count(self):
        blank = dict(total_price=None, store=None)
        counted = dict(self.FIXTURE, expect=[{"action": "bought", "item_id": "rice", "count": 30}])
        packs = dict(self.FIXTURE, expect=[{"action": "bought", "item_id": "rice", "packs": 30}])
        bare = self.got(quantity={"kind": "packs", "packs": 30, "bare": True}, **blank)
        named = self.got(quantity={"kind": "packs", "packs": 30}, **blank)
        self.assertEqual([self.passes(counted, bare), self.passes(counted, named)], [True, False])
        self.assertEqual([self.passes(packs, bare), self.passes(packs, named)], [False, True])

    def test_a_value_where_none_is_expected_is_wrong(self):
        fixture = dict(self.FIXTURE, expect=[{"action": "finished", "item_id": "rice"}])
        self.assertFalse(self.passes(fixture, self.got(action="finished")))
        self.assertTrue(self.passes(fixture, self.got(action="finished", quantity=None, total_price=None, store=None)))

    def test_a_new_item_takes_the_written_unit_before_the_models(self):
        fixture = {"id": "x", "text": "t", "expect": [{"action": "bought", "new": ["soy"], "base_unit": "ml", "qty": 600}]}
        entry = dict(item_id=None, phrase="Soy sauce", total_price=None, store=None)
        self.assertTrue(self.passes(fixture, self.got(base_unit="piece", quantity={"kind": "amount", "amount": 600, "unit": "ml"}, **entry)))
        self.assertFalse(self.passes(fixture, self.got(base_unit="piece", quantity={"kind": "packs_of", "packs": 1, "size": 600, "unit": None}, **entry)))

    def test_events_are_matched_in_any_order(self):
        fixture = {"id": "x", "text": "t", "expect": [
            {"action": "finished", "item_id": "rice"}, {"action": "finished", "item_id": "eggs"},
        ]}
        blank = dict(quantity=None, total_price=None, store=None)
        entries = (self.got(action="finished", item_id="eggs", base_unit="piece", **blank)
                   + self.got(action="finished", **blank))
        self.assertTrue(self.passes(fixture, entries))

    def test_unwanted_question_fails_chatter(self):
        chatter = {"id": "c", "text": "hello", "expect": []}
        self.assertTrue(eval_freetext.grade(chatter, [], None)[0])
        self.assertFalse(eval_freetext.grade(chatter, [], "Which item?")[0])


if __name__ == "__main__":
    unittest.main()
