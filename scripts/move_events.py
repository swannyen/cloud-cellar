#!/usr/bin/env python3
"""Move entries that were filed under the wrong item to the item they belong to.

From the repo root, with AWS_PROFILE and AWS_REGION set:

    python3 scripts/move_events.py face-masks hand-masks --text "hand masks" --name "Hand masks"           # show what would move
    python3 scripts/move_events.py face-masks hand-masks --text "hand masks" --name "Hand masks" --write   # move it

The first two arguments are item IDs, as /items shows them: where the entries
are now, and where they should be. The entries moved are the ones whose
original message contains --text (capitals don't matter). If the target item
doesn't exist yet it is created, named --name and counted in the same unit as
the source item.

Each entry is moved as it is: its time, numbers, price, store and message stay
the same. Afterwards both items' stock, status and pack size are worked out
again from the entries they now hold.
"""
import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import live_table  # noqa: E402  (also puts src/ on the path)

import apply  # noqa: E402
import replies  # noqa: E402
import store  # noqa: E402

ITEM_ID = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")


class MoveError(Exception):
    """The move can't be done as asked. The message says why."""


def plan(source_id, target_id, text, name=None):
    """Work out a move without changing anything.

    Returns (source item, target item, the entries to move, whether the target is new).
    """
    source = store.get_item(source_id)
    if source is None:
        raise MoveError(f'There is no item "{source_id}". /items shows the IDs.')
    if source_id == target_id:
        raise MoveError("The two items are the same.")
    wanted = text.lower()
    entries = [event for event in store.list_events(source_id) if wanted in (event.get("raw_text") or "").lower()]
    target = store.get_item(target_id)
    if target is not None:
        return source, target, entries, False
    if not name:
        raise MoveError(f'There is no item "{target_id}" yet. Give it a name with --name to create it.')
    if not ITEM_ID.fullmatch(target_id) or len(target_id) > apply.MAX_SLUG:
        raise MoveError(f'"{target_id}" can\'t be an item ID. Use lowercase words joined by hyphens, like hand-masks.')
    created_at = min((event["at"] for event in entries), default=None)
    target = {"item_id": target_id, "name": name, "aliases": [], "base_unit": source["base_unit"], "created_at": created_at}
    return source, target, entries, True


def move(source_id, target_id, text, name=None):
    """Move the matching entries and recompute both items. Returns what plan() returned."""
    source, target, entries, is_new = plan(source_id, target_id, text, name)
    if not entries:
        return source, target, entries, is_new
    if is_new:
        store.create_item(target)
    source_pk = store.event_pk(source_id)
    for event in entries:
        sk = event["sk"]
        # Written to the new item before it is removed from the old one, so a failure can't lose it.
        store.put_event(target_id, sk, event)
        store.delete_event(source_pk, sk)
        _repoint_undo(target, event)
        _drop_pending(source_pk, sk)
    apply.refresh_item(target)
    apply.refresh_item(source)   # an item left with no entries is removed
    return source, target, entries, is_new


def _repoint_undo(target, event):
    """/undo has to find a recent entry where it lives now."""
    user_id, sk = event.get("user_id"), event["sk"]
    if user_id is not None and any(recent["sk"] == sk for recent in store.list_recent_events(user_id)):
        apply.remember_for_undo(target, event, sk)


def _drop_pending(source_pk, sk):
    """Buttons still showing under a moved entry would point at the old item: retire them."""
    _, update_id, line = sk.rsplit("#", 2)
    pending = store.get_pending(update_id, line)
    if pending and pending.get("event_pk") == source_pk:
        store.delete_pending(update_id, line)


def describe(item) -> str:
    if store.get_item(item["item_id"]) is None:
        return "no entries left, so the item is gone"
    unit, stock, pack = item["base_unit"], item.get("stock_est"), item.get("pack_size")
    return ", ".join([
        f"{len(store.list_events(item['item_id']))} entries",
        replies.quantity(stock, unit) if stock is not None else "stock unknown",
        f"pack of {replies.quantity(pack, unit)}" if pack else "no pack size",
    ])


def main() -> int:
    args = argparse.ArgumentParser(description="Move entries from one item to another.")
    args.add_argument("source", help="ID of the item the entries are filed under now, e.g. face-masks")
    args.add_argument("target", help="ID of the item they belong to, e.g. hand-masks")
    args.add_argument("--text", required=True, help="move the entries whose original message contains this")
    args.add_argument("--name", help="name for the target item, if it has to be created")
    args.add_argument("--write", action="store_true", help="make the move (default: only show it)")
    options = args.parse_args()

    live_table.use_live_table()
    try:
        action = move if options.write else plan
        source, target, entries, is_new = action(options.source, options.target, options.text, options.name)
    except MoveError as err:
        print(err, file=sys.stderr)
        return 2
    verb = "Moved" if options.write else "Would move"
    new = " (new item)" if is_new else ""
    print(f"{verb} {len(entries)} of {source['name']}'s entries to {target['name']}{new}:")
    for event in entries:
        print(f"  {event['sk'][:16]}  {replies.summary(target, event)}  <- {event.get('raw_text')!r}")
    if not entries:
        print(f'  none: no entry of {source["name"]} has "{options.text}" in its message')
    elif options.write:
        print(f"{source['name']} now: {describe(source)}")
        print(f"{target['name']} now: {describe(target)}")
    else:
        print("Nothing has changed. Run again with --write to make the move.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
