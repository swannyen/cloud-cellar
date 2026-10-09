"""Parser tests: cleaning the model's output and building the prompt. No network."""
import json
import unittest
from datetime import date
from unittest import mock

import fakes  # noqa: F401  (puts src/ on sys.path)

import parser

KNOWN = {"toilet-paper", "dish-soap"}


def raw_event(**fields):
    event = {
        "action": "bought",
        "item_id": "toilet-paper",
        "item_name": "TP",
        "base_unit": "roll",
        "packs": 2,
        "units_per_pack": 10,
        "total_price_sgd": 12.9,
        "store": "FairPrice",
        "brand": None,
    }
    event.update(fields)
    return event


def normalise_one(**fields):
    events, _ = parser.normalise({"events": [raw_event(**fields)], "clarify": None}, KNOWN)
    return events[0] if events else None


class NormaliseTest(unittest.TestCase):
    def test_clean_event_passes_through_with_price_renamed(self):
        self.assertEqual(normalise_one(), {
            "action": "bought",
            "item_id": "toilet-paper",
            "item_name": "TP",
            "base_unit": "roll",
            "packs": 2,
            "units_per_pack": 10,
            "total_price": 12.9,
            "store": "FairPrice",
            "brand": None,
        })

    def test_enum_values_are_matched_case_insensitively(self):
        event = normalise_one(action="Bought", base_unit="ROLL")
        self.assertEqual((event["action"], event["base_unit"]), ("bought", "roll"))

    def test_unknown_action_or_missing_name_drops_the_event(self):
        self.assertIsNone(normalise_one(action="borrowed"))
        self.assertIsNone(normalise_one(item_name="  "))
        self.assertIsNone(normalise_one(item_name=None))

    def test_unknown_base_unit_falls_back_to_piece(self):
        self.assertEqual(normalise_one(base_unit="bottle")["base_unit"], "piece")

    def test_item_id_that_is_not_known_becomes_a_new_item(self):
        self.assertIsNone(normalise_one(item_id="toilet-roll")["item_id"])
        self.assertIsNone(normalise_one(item_id=None)["item_id"])
        self.assertEqual(normalise_one(item_id="Toilet-Paper ")["item_id"], "toilet-paper")

    def test_non_positive_and_non_numeric_numbers_become_unknown(self):
        for bad in (0, -1, "2", True, float("nan"), float("inf")):
            with self.subTest(bad=bad):
                event = normalise_one(packs=bad, units_per_pack=bad, total_price_sgd=bad, action="bought")
                self.assertEqual(
                    (event["packs"], event["units_per_pack"], event["total_price"]), (None, None, None)
                )

    def test_packs_are_capped_at_100(self):
        self.assertEqual(normalise_one(packs=5000)["packs"], 100)
        self.assertEqual(normalise_one(units_per_pack=5000)["units_per_pack"], 5000)

    def test_blank_store_and_brand_become_none(self):
        event = normalise_one(store="  ", brand="")
        self.assertEqual((event["store"], event["brand"]), (None, None))

    def test_price_check_needs_a_price(self):
        self.assertIsNone(normalise_one(action="price_check", total_price_sgd=None))
        self.assertIsNotNone(normalise_one(action="price_check", total_price_sgd=8.95))

    def test_clarify_is_trimmed_and_blank_means_none(self):
        self.assertEqual(parser.normalise({"events": [], "clarify": " Which one? "}, KNOWN), ([], "Which one?"))
        self.assertEqual(parser.normalise({"events": [], "clarify": ""}, KNOWN), ([], None))

    def test_garbage_output_gives_nothing(self):
        for garbage in (None, [], "text", {"events": "none"}, {"events": [None, 3, "x"]}):
            with self.subTest(garbage=garbage):
                self.assertEqual(parser.normalise(garbage, KNOWN), ([], None))


class PromptTest(unittest.TestCase):
    ITEMS = [
        {"item_id": "toilet-paper", "name": "Toilet paper", "base_unit": "roll", "aliases": ["loo roll", "TP"]},
        {"item_id": "dish-soap", "name": "Dishwashing liquid", "base_unit": "ml", "aliases": []},
    ]

    def test_user_content_follows_the_plan_layout(self):
        content = parser.build_user_content("finished the dish soap", "Mei", self.ITEMS, date(2026, 10, 9))
        self.assertEqual(content, "\n".join([
            "Sender: Mei",
            "Today: 2026-10-09 (Fri)",
            "Known items (id | name | base unit | aliases):",
            "toilet-paper | Toilet paper | roll | loo roll, TP",
            "dish-soap | Dishwashing liquid | ml |",
            "",
            "Message:",
            "finished the dish soap",
        ]))

    def test_user_content_with_no_items(self):
        content = parser.build_user_content("hi", "Mei", [], date(2026, 10, 9))
        self.assertIn("(none yet)", content)

    def test_schema_requires_every_field_and_forbids_extras(self):
        event_schema = parser.SCHEMA["properties"]["events"]["items"]
        for schema in (parser.SCHEMA, event_schema):
            self.assertEqual(sorted(schema["required"]), sorted(schema["properties"]))
            self.assertIs(schema["additionalProperties"], False)

    def test_every_example_in_the_system_prompt_is_valid_for_its_own_known_items(self):
        examples = parser.SYSTEM_PROMPT.split("## Examples")[1].split("\n\n")[2:]
        self.assertGreaterEqual(len(examples), 10)
        fields = set(parser.SCHEMA["properties"]["events"]["items"]["required"])
        for example in examples:
            lines = example.strip().splitlines()
            with self.subTest(example=lines[-2][:60]):
                self.assertTrue(lines[0].startswith("Known items"))
                self.assertTrue(lines[-2].startswith("Message: "))
                known = {line.split(" | ")[0] for line in lines[1:-2] if " | " in line}
                data = json.loads(lines[-1].removeprefix("Output: "))
                self.assertEqual(set(data), {"events", "clarify"})
                for event in data["events"]:
                    self.assertEqual(set(event), fields)
                    self.assertIn(event["item_id"], known | {None})
                kept, _ = parser.normalise(data, known)
                self.assertEqual(len(kept), len(data["events"]))


class ParseTest(unittest.TestCase):
    def test_parse_sends_the_prompt_and_returns_normalised_events(self):
        output = {"events": [raw_event(action="Bought")], "clarify": None}
        message = {"model": "claude-haiku-5-5", "usage": {"input_tokens": 1500, "output_tokens": 80}}
        with mock.patch.object(parser.claude_api, "complete_json", return_value=(output, message)) as call:
            parsed = parser.parse("bought TP", "Mei", PromptTest.ITEMS, date(2026, 10, 9))
        system, user, schema = call.call_args.args
        self.assertIs(system, parser.SYSTEM_PROMPT)
        self.assertIs(schema, parser.SCHEMA)
        self.assertTrue(user.endswith("Message:\nbought TP"))
        self.assertEqual([event["action"] for event in parsed.events], ["bought"])
        self.assertIsNone(parsed.clarify)
        self.assertEqual(parsed.usage["input_tokens"], 1500)
        self.assertEqual(parsed.model, "claude-haiku-5-5")


if __name__ == "__main__":
    unittest.main()
