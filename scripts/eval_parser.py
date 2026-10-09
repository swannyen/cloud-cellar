#!/usr/bin/env python3
"""Run the parser fixtures against the live Claude API and print accuracy.

From the repo root:

    AWS_PROFILE=household AWS_REGION=ap-southeast-2 python3 scripts/eval_parser.py
    python3 scripts/eval_parser.py --selftest    # checks the grader only, no API calls

The API key is read from SSM (PARAM_PREFIX/anthropic-key), or from
ANTHROPIC_API_KEY if that is set. Every run makes one small Claude call per
fixture (about one US cent for the whole file).

Fixture file (tests/fixtures/messages.jsonl): the first line holds the
"known_items" every message is parsed against. Each later line is one message:

    {"id": ..., "text": ..., "expect": [events], "clarify": true|false}

An expected event names "action" and either "item_id" (a known item) or "new"
(keywords, one of which must appear in the new item's name). Fields left out
are expected to be null; "base_unit" defaults to the known item's unit. A value
of {"any": [...]} accepts any of the listed values. An optional "known" lists the
item IDs the bot knows for that message (default: all of them), which is how
the "product the bot has never seen" cases are written.

Exit status is 0 when the Phase 1 targets are met: at least 90% of fixtures
fully correct, no events on chatter, and no failed calls.
"""
import argparse
import itertools
import json
import os
import re
import statistics
import sys
import time
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import claude_api  # noqa: E402
import config  # noqa: E402
import parser  # noqa: E402

DEFAULT_FIXTURES = ROOT / "tests" / "fixtures" / "messages.jsonl"
TODAY = date(2026, 10, 9)  # fixed, so every run sends the same prompt
FIELDS = ("action", "item", "base_unit", "packs", "units_per_pack", "total_price", "store", "brand")
TARGET_CORRECT = 0.90
# Claude Haiku 5.5, US$ per million tokens (prompts up to 100K tokens).
PRICE_IN, PRICE_OUT = 0.10, 0.50


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


def grade(fixture, events, clarify, known_items):
    """Compare parser output with a fixture.

    Returns (passed, field_hits, problems), where field_hits maps each field to
    [correct, expected] counts over the fixture's expected events.
    """
    units = {item["item_id"]: item["base_unit"] for item in known_items}
    expected = fixture["expect"]
    hits = {name: [0, len(expected)] for name in FIELDS}
    problems = []

    # Events may come back in any order: use the pairing that matches the most fields.
    padded = list(events) + [None] * max(0, len(expected) - len(events))
    best = max(
        (pairing for pairing in itertools.permutations(padded, len(expected))),
        key=lambda pairing: sum(sum(_compare(e, g, units).values()) for e, g in zip(expected, pairing)),
        default=(),
    )
    for want, got in zip(expected, best):
        result = _compare(want, got, units)
        for name, correct in result.items():
            hits[name][0] += correct
        wrong = [name for name, correct in result.items() if not correct]
        if got is None:
            problems.append(f"missing event: {want['action']} {want.get('item_id') or want.get('new')}")
        elif wrong:
            problems.append(f"{want['action']} {want.get('item_id') or 'new item'}: wrong {', '.join(wrong)}")
    if len(events) > len(expected):
        problems.append(f"{len(events) - len(expected)} unexpected event(s)")
    if bool(clarify) != bool(fixture.get("clarify")):
        problems.append("asked a question" if clarify else "expected a clarifying question")
    return not problems, hits, problems


def _compare(want, got, units) -> dict:
    """Per-field booleans for one expected event against one parsed event (or None)."""
    if got is None:
        return dict.fromkeys(FIELDS, False)
    if "new" in want:
        name = got["item_name"].lower()
        item_ok = got["item_id"] is None and any(keyword in name for keyword in want["new"])
        unit = want["base_unit"]
    else:
        item_ok = got["item_id"] == want["item_id"]
        unit = want.get("base_unit", units[want["item_id"]])
    return {
        "action": got["action"] == want["action"],
        "item": item_ok,
        "base_unit": _matches(unit, got["base_unit"], lambda a, b: a == b),
        "packs": _matches(want.get("packs"), got["packs"], _same_number),
        "units_per_pack": _matches(want.get("units_per_pack"), got["units_per_pack"], _same_number),
        "total_price": _matches(want.get("total_price"), got["total_price"], _same_number),
        "store": _matches(want.get("store"), got["store"], _same_text),
        "brand": _matches(want.get("brand"), got["brand"], _same_text),
    }


def _matches(want, got, same) -> bool:
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
    squash = lambda text: re.sub(r"[^a-z0-9]", "", text.lower())  # noqa: E731
    return squash(want) == squash(got)


def oracle_output(fixture, known_items):
    """Parser output that exactly satisfies a fixture (for checking the grader)."""
    units = {item["item_id"]: item["base_unit"] for item in known_items}
    first = lambda value: value["any"][0] if isinstance(value, dict) else value  # noqa: E731
    events = []
    for want in fixture["expect"]:
        item_id = want.get("item_id")
        events.append({
            "action": want["action"],
            "item_id": item_id,
            "item_name": want["new"][0] if "new" in want else item_id,
            "base_unit": first(want.get("base_unit")) or units[item_id],
            "packs": first(want.get("packs")),
            "units_per_pack": first(want.get("units_per_pack")),
            "total_price": first(want.get("total_price")),
            "store": first(want.get("store")),
            "brand": first(want.get("brand")),
        })
    return events, "Which one?" if fixture.get("clarify") else None


def selftest(known_items, fixtures) -> list:
    """Check the grader on answers known to be right and wrong. Returns a list of faults."""
    faults = []
    all_items = known_items
    for fixture in fixtures:
        known_items = items_for(fixture, all_items)
        events, clarify = oracle_output(fixture, known_items)
        if not grade(fixture, events, clarify, known_items)[0]:
            faults.append(f"{fixture['id']}: the correct answer is graded as wrong")
        if grade(fixture, [], None, known_items)[0] != is_chatter(fixture):
            faults.append(f"{fixture['id']}: an empty answer is graded wrongly")
        if events:
            wrong_action = [dict(events[0], action="still_have" if events[0]["action"] != "still_have" else "bought")]
            if grade(fixture, wrong_action + events[1:], clarify, known_items)[0]:
                faults.append(f"{fixture['id']}: a wrong action is graded as correct")
            if grade(fixture, events + [events[0]], clarify, known_items)[0]:
                faults.append(f"{fixture['id']}: an extra event is graded as correct")
        if fixture["text"] in parser.SYSTEM_PROMPT:
            faults.append(f"{fixture['id']}: the fixture text also appears in the system prompt")
    return faults


def run(known_items, fixtures, reps, verbose):
    hits = {name: [0, 0] for name in FIELDS}
    passed = total = chatter_false_events = 0
    failures, errors, latencies = [], [], []
    tokens_in = tokens_out = 0
    for rep in range(reps):
        for fixture in fixtures:
            started = time.monotonic()
            try:
                items = items_for(fixture, known_items)
                parsed = parser.parse(fixture["text"], fixture.get("sender", "Mei"), items, TODAY)
                if not parsed.model.startswith(config.claude_model()):
                    raise claude_api.ClaudeError(f"served by {parsed.model!r}, not {config.claude_model()!r}")
            except claude_api.ClaudeError as err:
                # A failed call says nothing about the prompt, so it is reported but not scored.
                errors.append(f"{fixture['id']}: {err}")
                continue
            latencies.append(time.monotonic() - started)
            tokens_in += parsed.usage.get("input_tokens", 0)
            tokens_out += parsed.usage.get("output_tokens", 0)
            ok, fixture_hits, problems = grade(fixture, parsed.events, parsed.clarify, items)
            total += 1
            passed += ok
            for name, (correct, expected) in fixture_hits.items():
                hits[name][0] += correct
                hits[name][1] += expected
            if is_chatter(fixture):
                chatter_false_events += len(parsed.events)
            if not ok:
                failures.append((fixture, parsed, problems))
            if verbose:
                print(f"  {'ok  ' if ok else 'FAIL'} {fixture['id']}")

    with_events = sum(1 for fixture in fixtures if fixture["expect"])
    chatter = sum(1 for fixture in fixtures if is_chatter(fixture))
    print(f"Fixtures: {len(fixtures)} ({with_events} with events, {chatter} chatter, "
          f"{len(fixtures) - with_events - chatter} clarify) x {reps} run(s)")
    print(f"Model: {config.claude_model()}, prompt version {parser.PROMPT_VERSION}\n")
    print(f"Field accuracy over {hits['action'][1]} expected events:")
    for name in FIELDS:
        correct, expected = hits[name]
        print(f"  {name:<15} {correct:>3}/{expected:<3} {_percent(correct, expected)}")
    print(f"\nFully correct fixtures: {passed}/{total} {_percent(passed, total)}   (target: at least {TARGET_CORRECT:.0%})")
    print(f"Events on chatter:      {chatter_false_events}   (target: 0)")
    print(f"Failed calls:           {len(errors)}   (not scored)")
    cost = tokens_in / 1e6 * PRICE_IN + tokens_out / 1e6 * PRICE_OUT
    latency = f"{statistics.median(latencies):.1f} s" if latencies else "n/a"
    print(f"Tokens: {tokens_in:,} in, {tokens_out:,} out (about US${cost:.3f}). Median call: {latency}")
    for fixture, parsed, problems in failures:
        print(f"\nFAIL {fixture['id']}: {fixture['text']!r}")
        for problem in problems:
            print(f"    {problem}")
        print(f"    got: {json.dumps(parsed.events, ensure_ascii=False)}"
              + (f" clarify={parsed.clarify!r}" if parsed.clarify else ""))
    for error in errors:
        print(f"\nERROR {error}")
    met = total > 0 and passed / total >= TARGET_CORRECT and chatter_false_events == 0 and not errors
    print(f"\nRESULT: {'PASS' if met else 'FAIL'}")
    return met


def _percent(part, whole) -> str:
    return f"{part / whole:6.1%}" if whole else "   n/a"


def main() -> int:
    args = argparse.ArgumentParser(description="Evaluate the message parser on the fixture file.")
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
    os.environ.setdefault("PARAM_PREFIX", "/bettynuffbot")
    # The AWS CLI reads AWS_REGION, but boto3 only reads AWS_DEFAULT_REGION.
    if os.environ.get("AWS_REGION"):
        os.environ.setdefault("AWS_DEFAULT_REGION", os.environ["AWS_REGION"])
    if os.environ.get("ANTHROPIC_API_KEY"):
        config._secrets = {"anthropic-key": os.environ["ANTHROPIC_API_KEY"]}
    try:
        config.secret("anthropic-key")
    except Exception as err:
        print(f"Could not load the Claude API key: {err}", file=sys.stderr)
        return 2
    return 0 if run(known_items, fixtures, options.reps, options.verbose) else 1


if __name__ == "__main__":
    sys.exit(main())
