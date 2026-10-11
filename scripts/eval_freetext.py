#!/usr/bin/env python3
"""Run the free-text fixtures against the live Claude API and print accuracy.

From the repo root:

    AWS_PROFILE=household AWS_REGION=ap-southeast-2 python3 scripts/eval_freetext.py
    python3 scripts/eval_freetext.py --selftest    # checks the grader only, no API calls

The API key is read from SSM (PARAM_PREFIX/anthropic-key), or from
ANTHROPIC_API_KEY if that is set. Every run makes one Claude call per fixture
(about four US cents for the whole file).

Fixture file (tests/fixtures/freetext.jsonl): the first line holds the
"known_items" every message is parsed against. Each later line is one message:

    {"id": ..., "text": ..., "expect": [events], "clarify": true|false}

What is graded is what would be recorded: the model's answer after
ai.ground_free_text has dropped anything that isn't written in the message
and read the quantity words by the format's rules.

An expected event names "action" and either "item_id" (a known item) or "new"
(keywords, one of which must appear in the new item's name, plus its
"base_unit"). The quantity is "qty" (a total in base units, when the message
gives sizes), "packs" (a bare pack count), or left out (no quantity). Other
fields left out are expected to be null. A value of {"any": [...]} accepts any
of the listed values. An optional "known" lists the item IDs the bot knows for
that message (default: all of them), which is how the "product the bot has
never seen" cases are written.

Exit status is 0 when the Phase 1b targets are met: at least 90% of fixtures
fully correct, no events on chatter, and no failed calls.
"""
import argparse
import itertools
import json
import operator
import os
import statistics
import sys
import time
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import ai  # noqa: E402
import claude_api  # noqa: E402
import config  # noqa: E402
import structured  # noqa: E402

DEFAULT_FIXTURES = ROOT / "tests" / "fixtures" / "freetext.jsonl"
TODAY = date(2026, 10, 9)  # fixed, so every run sends the same prompt
FIELDS = ("action", "item", "base_unit", "quantity", "total_price", "store", "brand")
TARGET_CORRECT = 0.90
# Claude Haiku 5.5, US$ per million tokens (prompts up to 100K tokens).
PRICE_IN, PRICE_OUT = 0.10, 0.50


# --- Fixtures -----------------------------------------------------------------

def load_fixtures(path=DEFAULT_FIXTURES):
    """Return (known_items, fixtures)."""
    lines = [json.loads(line) for line in Path(path).read_text().splitlines() if line.strip()]
    return lines[0]["known_items"], lines[1:]


def items_for(fixture, known_items) -> list:
    """The known items this fixture is parsed against."""
    if "known" not in fixture:
        return known_items
    return [item for item in known_items if item["item_id"] in fixture["known"]]


def is_chatter(fixture) -> bool:
    return not fixture["expect"] and not fixture.get("clarify")


# --- Grading ------------------------------------------------------------------

def grade(fixture, entries, clarify):
    """Compare the entries the fallback's output turned into with a fixture.

    Returns (passed, field_hits, problems), where field_hits maps each field to
    [correct, expected] counts over the fixture's expected events.
    """
    expected = fixture["expect"]
    hits = {name: [0, len(expected)] for name in FIELDS}
    problems = []
    for want, got in zip(expected, _best_pairing(expected, entries)):
        result = _compare(want, got)
        for name, correct in result.items():
            hits[name][0] += correct
        wrong = [name for name, correct in result.items() if not correct]
        if got is None:
            problems.append(f"missing event: {want['action']} {want.get('item_id') or want.get('new')}")
        elif wrong:
            problems.append(f"{want['action']} {want.get('item_id') or 'new item'}: wrong {', '.join(wrong)}")
    if len(entries) > len(expected):
        problems.append(f"{len(entries) - len(expected)} unexpected event(s)")
    if bool(clarify) != bool(fixture.get("clarify")):
        problems.append("asked a question" if clarify else "expected a clarifying question")
    return not problems, hits, problems


def _best_pairing(expected, entries) -> tuple:
    """One entry (or None) for each expected event. Entries may come back in any
    order, so this is the pairing that gets the most fields right."""
    padded = list(entries) + [None] * max(0, len(expected) - len(entries))
    return max(
        itertools.permutations(padded, len(expected)),
        key=lambda pairing: sum(sum(_compare(want, got).values()) for want, got in zip(expected, pairing)),
        default=(),
    )


def _compare(want, got) -> dict:
    """Per-field booleans for one expected event against one grounded entry (or None)."""
    if got is None:
        return dict.fromkeys(FIELDS, False)
    if "new" in want:
        name = got["phrase"].lower()
        item_ok = got["item_id"] is None and any(keyword in name for keyword in want["new"])
        # A new item takes the unit written in the message, or else the model's.
        unit = (got["quantity"] or {}).get("unit") or got["base_unit"]
        unit_ok = _matches(want["base_unit"], unit, operator.eq)
    else:
        item_ok = got["item_id"] == want["item_id"]
        unit_ok = True  # a known item keeps its own unit
    return {
        "action": got["action"] == want["action"],
        "item": item_ok,
        "base_unit": unit_ok,
        "quantity": _same_quantity(want, got["quantity"]),
        "total_price": _matches(want.get("total_price"), got["total_price"], _same_number),
        "store": _matches(want.get("store"), got["store"], _same_text),
        "brand": _matches(want.get("brand"), got["brand"], _same_text),
    }


def _same_quantity(want, quantity) -> bool:
    """"qty" is a total in base units, "packs" a bare pack count, neither means no quantity."""
    if "qty" in want:
        if not quantity or quantity["kind"] == "packs":
            return False
        total = quantity["amount"] if quantity["kind"] == "amount" else quantity["packs"] * quantity["size"]
        return _matches(want["qty"], total, _same_number)
    if "packs" in want:
        return bool(quantity) and quantity["kind"] == "packs" and _matches(want["packs"], quantity["packs"], _same_number)
    return quantity is None


def _matches(want, got, same) -> bool:
    """Whether got is the wanted value, or one of the values in {"any": [...]}."""
    options = want["any"] if isinstance(want, dict) else [want]
    return any(same(option, got) for option in options)


def _same_number(want, got) -> bool:
    if want is None or got is None:
        return want is got
    return abs(want - got) < 0.005


def _same_text(want, got) -> bool:
    """Ignore case, spacing and punctuation: "FairPrice" equals "fairprice"."""
    if want is None or got is None:
        return want is got
    return structured.squash(want) == structured.squash(got)


# --- Checking the grader itself -------------------------------------------------

def oracle_output(fixture, items):
    """(entries, clarify) that exactly satisfy a fixture. items are the fixture's known items."""
    units = {item["item_id"]: item["base_unit"] for item in items}
    entries = []
    for want in fixture["expect"]:
        item_id = want.get("item_id")
        quantity = None
        if "qty" in want:
            quantity = {"kind": "amount", "amount": _first(want["qty"]), "unit": None}
        elif "packs" in want:
            quantity = {"kind": "packs", "packs": _first(want["packs"])}
        entries.append({
            "action": want["action"],
            "item_id": item_id,
            "phrase": want["new"][0] if "new" in want else item_id,
            "base_unit": _first(want.get("base_unit")) or units[item_id],
            "quantity": quantity,
            "total_price": _first(want.get("total_price")),
            "store": _first(want.get("store")),
            "brand": _first(want.get("brand")),
            "missed": (),
        })
    return entries, "Which one?" if fixture.get("clarify") else None


def _first(value):
    """The value itself, or the first of the values in {"any": [...]}."""
    return value["any"][0] if isinstance(value, dict) else value


def selftest(known_items, fixtures) -> list:
    """Check the grader on answers known to be right and wrong. Returns a list of faults."""
    faults = []
    for fixture in fixtures:
        entries, clarify = oracle_output(fixture, items_for(fixture, known_items))
        if not grade(fixture, entries, clarify)[0]:
            faults.append(f"{fixture['id']}: the correct answer is graded as wrong")
        if grade(fixture, [], None)[0] != is_chatter(fixture):
            faults.append(f"{fixture['id']}: an empty answer is graded wrongly")
        if entries:
            first, rest = entries[0], entries[1:]
            other_action = "bought" if first["action"] == "still_have" else "still_have"
            if grade(fixture, [dict(first, action=other_action)] + rest, clarify)[0]:
                faults.append(f"{fixture['id']}: a wrong action is graded as correct")
            if grade(fixture, entries + [first], clarify)[0]:
                faults.append(f"{fixture['id']}: an extra event is graded as correct")
            wrong_amount = dict(first, quantity={"kind": "amount", "amount": 987654, "unit": None})
            if grade(fixture, [wrong_amount] + rest, clarify)[0]:
                faults.append(f"{fixture['id']}: a wrong quantity is graded as correct")
        if fixture["text"] in ai.FREE_TEXT_PROMPT:
            faults.append(f"{fixture['id']}: the fixture text also appears in the system prompt")
    return faults


# --- The live run ---------------------------------------------------------------

@dataclass
class Results:
    """What a run found, added up over every fixture that got an answer."""
    hits: dict = field(default_factory=lambda: {name: [0, 0] for name in FIELDS})
    passed: int = 0
    total: int = 0
    chatter_events: int = 0
    dropped: int = 0      # stores, prices or quantities the model gave that aren't in the message
    tokens_in: int = 0
    tokens_out: int = 0
    failures: list = field(default_factory=list)   # (fixture, parsed, entries, problems)
    errors: list = field(default_factory=list)     # failed calls: reported, not scored
    latencies: list = field(default_factory=list)

    @property
    def targets_met(self) -> bool:
        correct_enough = self.total > 0 and self.passed / self.total >= TARGET_CORRECT
        return correct_enough and self.chatter_events == 0 and not self.errors


def run(known_items, fixtures, reps=1, verbose=False) -> Results:
    """Send every fixture to Claude reps times and grade what would be recorded."""
    results = Results()
    for fixture in fixtures * reps:
        items = items_for(fixture, known_items)
        started = time.monotonic()
        try:
            parsed = ai.parse_free_text(fixture["text"], fixture.get("sender", "Mei"), items, TODAY)
            if not parsed.model.startswith(config.claude_model()):
                raise claude_api.ClaudeError(f"served by {parsed.model!r}, not {config.claude_model()!r}")
        except claude_api.ClaudeError as err:
            # A failed call says nothing about the prompt.
            results.errors.append(f"{fixture['id']}: {err}")
            continue
        results.latencies.append(time.monotonic() - started)
        results.tokens_in += parsed.usage.get("input_tokens", 0)
        results.tokens_out += parsed.usage.get("output_tokens", 0)
        # Only facts written in the message get through.
        entries = ai.ground_free_text(parsed.events, fixture["text"])
        results.dropped += sum(len(entry["missed"]) for entry in entries)
        ok, fixture_hits, problems = grade(fixture, entries, parsed.clarify)
        results.total += 1
        results.passed += ok
        for name, (correct, expected) in fixture_hits.items():
            results.hits[name][0] += correct
            results.hits[name][1] += expected
        if is_chatter(fixture):
            results.chatter_events += len(entries)
        if not ok:
            results.failures.append((fixture, parsed, entries, problems))
        if verbose:
            print(f"  {'ok  ' if ok else 'FAIL'} {fixture['id']}")
    return results


def report(results, fixtures, reps):
    """Print the scores, then every fixture that went wrong."""
    with_events = sum(1 for fixture in fixtures if fixture["expect"])
    chatter = sum(1 for fixture in fixtures if is_chatter(fixture))
    print(f"Fixtures: {len(fixtures)} ({with_events} with events, {chatter} chatter, "
          f"{len(fixtures) - with_events - chatter} clarify) x {reps} run(s)")
    print(f"Model: {config.claude_model()}, prompt version {ai.FREE_TEXT_PROMPT_VERSION}\n")
    print(f"Field accuracy over {results.hits['action'][1]} expected events:")
    for name in FIELDS:
        correct, expected = results.hits[name]
        print(f"  {name:<15} {correct:>3}/{expected:<3} {_percent(correct, expected)}")
    print(f"\nFully correct fixtures: {results.passed}/{results.total} {_percent(results.passed, results.total)}"
          f"   (target: at least {TARGET_CORRECT:.0%})")
    print(f"Events on chatter:      {results.chatter_events}   (target: 0)")
    print(f"Failed calls:           {len(results.errors)}   (not scored)")
    print(f"Dropped by grounding:   {results.dropped}   "
          "(stores, prices or quantities the model gave that aren't in the message)")
    cost = results.tokens_in / 1e6 * PRICE_IN + results.tokens_out / 1e6 * PRICE_OUT
    latency = f"{statistics.median(results.latencies):.1f} s" if results.latencies else "n/a"
    print(f"Tokens: {results.tokens_in:,} in, {results.tokens_out:,} out (about US${cost:.3f}). Median call: {latency}")
    for fixture, parsed, entries, problems in results.failures:
        print(f"\nFAIL {fixture['id']}: {fixture['text']!r}")
        for problem in problems:
            print(f"    {problem}")
        print(f"    model:    {json.dumps(parsed.events, ensure_ascii=False)}"
              + (f" clarify={parsed.clarify!r}" if parsed.clarify else ""))
        shown = [{key: entry[key] for key in ("item_id", "phrase", "quantity", "total_price", "store", "missed")}
                 for entry in entries]
        print(f"    recorded: {json.dumps(shown, ensure_ascii=False)}")
    for error in results.errors:
        print(f"\nERROR {error}")
    print(f"\nRESULT: {'PASS' if results.targets_met else 'FAIL'}")


def _percent(part, whole) -> str:
    return f"{part / whole:6.1%}" if whole else "   n/a"


def _load_api_key():
    """Make config.secret("anthropic-key") work outside Lambda. Raises if the key can't be found."""
    os.environ.setdefault("PARAM_PREFIX", "/bettynuffbot")
    # The AWS CLI reads AWS_REGION, but boto3 only reads AWS_DEFAULT_REGION.
    if os.environ.get("AWS_REGION"):
        os.environ.setdefault("AWS_DEFAULT_REGION", os.environ["AWS_REGION"])
    if os.environ.get("ANTHROPIC_API_KEY"):
        config._secrets = {"anthropic-key": os.environ["ANTHROPIC_API_KEY"]}
    config.secret("anthropic-key")


def main() -> int:
    args = argparse.ArgumentParser(description="Evaluate the free-text fallback on the fixture file.")
    args.add_argument("--fixtures", default=DEFAULT_FIXTURES, help="path to the JSONL fixture file")
    args.add_argument("--reps", type=int, default=1, help="how many times to run every fixture")
    args.add_argument("--only", help="run only fixtures whose id contains this text")
    args.add_argument("--selftest", action="store_true", help="check the grader, without calling Claude")
    args.add_argument("-v", "--verbose", action="store_true", help="print each fixture as it is graded")
    options = args.parse_args()

    known_items, fixtures = load_fixtures(options.fixtures)
    faults = selftest(known_items, fixtures)
    for fault in faults:
        print(f"GRADER FAULT {fault}")
    if faults or options.selftest:
        print(f"Grader self-test: {'FAILED' if faults else 'ok'} ({len(fixtures)} fixtures)")
        return 1 if faults else 0

    if options.only:
        fixtures = [fixture for fixture in fixtures if options.only in fixture["id"]]
    try:
        _load_api_key()
    except Exception as err:
        print(f"Could not load the Claude API key: {err}", file=sys.stderr)
        return 2
    results = run(known_items, fixtures, options.reps, options.verbose)
    report(results, fixtures, options.reps)
    return 0 if results.targets_met else 1


if __name__ == "__main__":
    sys.exit(main())
