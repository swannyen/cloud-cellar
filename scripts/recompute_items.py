#!/usr/bin/env python3
"""Recompute every item's cached state (stock, status, pack size) from its events.

Run it once after deploying a change to how stock is counted, so /items is
right straight away instead of item by item as new entries arrive. Events are
never changed; only the figures cached on each item are rewritten.

From the repo root:

    AWS_PROFILE=household AWS_REGION=ap-southeast-2 python3 scripts/recompute_items.py           # show what would change
    AWS_PROFILE=household AWS_REGION=ap-southeast-2 python3 scripts/recompute_items.py --write   # save it

The table is found from the CloudFormation stack (STACK_NAME, default nuffnuffbot),
unless TABLE_NAME is set.
"""
import argparse
import copy
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import live_table  # noqa: E402  (also puts src/ on the path)

import apply  # noqa: E402
import model  # noqa: E402
import replies  # noqa: E402
import store  # noqa: E402

SHOWN = ("status", "stock_est", "pack_size")


def recomputed(item, events) -> dict:
    """The item as apply.refresh_item would leave it, without saving anything."""
    after = copy.deepcopy(item)
    apply._update_pack_size(after, events)
    after.update(model.replay(events, after.get("pack_size")))
    return after


def describe(item) -> str:
    unit = item["base_unit"]
    stock, pack = item.get("stock_est"), item.get("pack_size")
    return ", ".join([
        str(item.get("status")),
        replies.quantity(stock, unit) if stock is not None else "stock unknown",
        f"pack of {replies.quantity(pack, unit)}" if pack else "no pack size",
    ])


def main() -> int:
    args = argparse.ArgumentParser(description="Recompute each item's cached state from its events.")
    args.add_argument("--write", action="store_true", help="save the recomputed state (default: only show it)")
    options = args.parse_args()

    live_table.use_live_table()
    changed = 0
    for item in store.list_items():
        events = store.list_events(item["item_id"])
        if not events:
            print(f"{item['item_id']}: no events, left alone")
            continue
        after = recomputed(item, events)
        if all(item.get(key) == after.get(key) for key in SHOWN):
            print(f"{item['item_id']}: unchanged ({describe(item)})")
            continue
        changed += 1
        print(f"{item['item_id']}: {describe(item)}  ->  {describe(after)}")
        if options.write:
            apply.refresh_item(item)
    print(f"\n{changed} item(s) {'updated' if options.write else 'would change; run again with --write to save'}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
