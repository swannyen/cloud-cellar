#!/usr/bin/env python3
"""Copy the whole stock table to the laptop: a CSV of every entry, and every record as JSON.

It only reads the table. Run it monthly and before any change to the live data
(PLAN.md §7.4), so there is a copy that doesn't depend on AWS.

From the repo root:

    AWS_PROFILE=household AWS_REGION=ap-southeast-2 python3 scripts/export_csv.py
    AWS_PROFILE=household AWS_REGION=ap-southeast-2 python3 scripts/export_csv.py --out ~/Documents/cellar-backups

It writes two files, named after the time it was run (Singapore time):

    ~/cloud-cellar-backups/cellar-2026-11-04-1530.csv    one row per entry, oldest first, for a spreadsheet
    ~/cloud-cellar-backups/cellar-2026-11-04-1530.json   every record in the table as it is stored, keys included

The folder is outside the repo, since the files hold the family's messages (and
backups/ in the repo is git-ignored, in case --out points there). An existing
file is never overwritten. Both files are written under a ".partial" name, read back,
and get their real names only when their counts match the scan; the exit status
is 1 if they don't.

With --if-due it exports only when there is no backup yet since the last 1st or
15th of the month (Singapore time), and otherwise says so without touching AWS.
That is how the schedule runs it every day (scripts/install_backup_schedule.sh),
so a missed 1st or 15th is made up the next time the Mac is on.

The table is found from the CloudFormation stack (STACK_NAME, default nuffnuffbot),
unless TABLE_NAME is set.
"""
import argparse
import collections
import csv
import json
import re
import sys
from datetime import date, datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import live_table  # noqa: E402  (also puts src/ on the path)

import config  # noqa: E402
import pricing  # noqa: E402
import store  # noqa: E402

# Outside the repo, so deleting the repo folder doesn't take the backups with it, and outside ~/Documents,
# which macOS doesn't let the scheduled job (launchd) read or write.
DEFAULT_OUT = Path.home() / "cloud-cellar-backups"
EVENT_PREFIX = "EVT#"
EXPORT_NAME = re.compile(r"cellar-(\d{4}-\d{2}-\d{2})-\d{4}")   # the file name without .csv or .json

# (column, where it comes from). Everything else an entry holds is in the JSON file.
COLUMNS = (
    ("item", None),             # the item's name
    ("item_id", None),
    ("when", "at"),             # when it happened, Singapore time
    ("action", "action"),
    ("quantity", "qty"),        # in the item's base unit
    ("unit", None),             # the item's base unit
    ("packs", "packs"),
    ("units_per_pack", "units_per_pack"),
    ("total_price", "total_price"),
    ("unit_price", "unit_price"),
    ("currency", "currency"),   # empty on the first entries: read as SGD
    ("store", "store"),
    ("brand", "brand"),
    ("description", "description"),
    ("expires_on", "expires_on"),
    ("adds", "adds"),           # "yes" on a count that added to what was there
    ("who", "user_name"),
    ("logged_at", "logged_at"),  # when the message was sent, only on an entry dated in the past
    ("raw_text", "raw_text"),
    ("sk", "sk"),               # the entry's key, to find it in the JSON file
)
TIMES = {"at", "logged_at"}


class ExportError(Exception):
    """The export can't be written as asked. The message says why."""


def entry_rows(records) -> list:
    """One CSV row (a dict keyed by column) per entry, oldest first."""
    items = {record["sk"]: record for record in records if record["pk"] == store.ITEM_PK}
    entries = sorted((record for record in records if record["pk"].startswith(EVENT_PREFIX)), key=_entry_order)
    rows = []
    for entry in entries:
        item_id = entry["pk"][len(EVENT_PREFIX):]
        item = items.get(item_id, {})   # entries of an item whose record is gone still get a row
        row = {"item": item.get("name", ""), "item_id": item_id, "unit": item.get("base_unit", "")}
        for column, attribute in COLUMNS:
            if attribute is not None:
                row[column] = _cell(entry.get(attribute), attribute in TIMES)
        rows.append(row)
    return rows


def _entry_order(entry):
    """By when it happened, then the lines of one message in the order they were written ("…#10" after "…#2")."""
    message, _, line = entry["sk"].rpartition("#")
    return (entry.get("at") or "", entry["pk"], message, int(line) if line.isdigit() else 0)


def _cell(value, is_time=False) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "yes" if value else "no"
    if is_time:
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(pricing.SGT).strftime("%Y-%m-%d %H:%M:%S")
        except (AttributeError, ValueError):
            return str(value)   # not a time after all: written as it is
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def _json_default(value):
    if isinstance(value, (set, frozenset)):   # a DynamoDB set; the bot writes lists, but keep one if it is there
        return sorted(value)
    raise TypeError(f"can't write {type(value).__name__} to JSON")


def export(records, out_dir, now, table_name="") -> dict:
    """Write the CSV and JSON files for these records, then read them back.

    now (an aware datetime) names the files. Returns the two paths and the counts:
    {"csv", "json", "entries", "records", "rows_read", "records_read", "kinds"}.
    """
    out_dir = Path(out_dir).expanduser()
    stem = f"cellar-{now.astimezone(pricing.SGT):%Y-%m-%d-%H%M}"
    csv_path, json_path = out_dir / f"{stem}.csv", out_dir / f"{stem}.json"
    taken = [path.name for path in (csv_path, json_path) if path.exists()]
    if taken:
        raise ExportError(f"{' and '.join(taken)} already in {out_dir}. Wait a minute, or choose another folder with --out.")

    rows = entry_rows(records)
    ordered = sorted(records, key=lambda record: (record["pk"], record["sk"]))
    out_dir.mkdir(parents=True, exist_ok=True)
    # Written under .partial names first, so a half-written file is never taken for a backup.
    csv_partial, json_partial = (path.with_name(path.name + ".partial") for path in (csv_path, json_path))
    # utf-8-sig so that a spreadsheet reads the names and messages as UTF-8, not as Latin-1.
    with open(csv_partial, "w", newline="", encoding="utf-8-sig") as file:
        writer = csv.DictWriter(file, fieldnames=[column for column, _ in COLUMNS])
        writer.writeheader()
        writer.writerows(rows)
    with open(json_partial, "w", encoding="utf-8") as file:
        json.dump({
            "table": table_name,
            "exported_at": now.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "home_currency": config.home_currency(),   # what an entry with no currency is in
            "record_count": len(ordered),
            "records": ordered,
        }, file, ensure_ascii=False, indent=1, default=_json_default)
        file.write("\n")

    with open(csv_partial, newline="", encoding="utf-8-sig") as file:
        rows_read = sum(1 for _ in csv.DictReader(file))
    with open(json_partial, encoding="utf-8") as file:
        records_read = len(json.load(file)["records"])
    complete = (rows_read, records_read) == (len(rows), len(records))
    if complete:
        csv_partial.rename(csv_path)
        json_partial.rename(json_path)   # last, so a backup counts as made only once both files are there
    return {
        "csv": csv_path if complete else csv_partial, "json": json_path if complete else json_partial,
        "complete": complete,
        "entries": len(rows), "records": len(records),
        "rows_read": rows_read, "records_read": records_read,
        "kinds": collections.Counter(record["pk"].split("#", 1)[0] for record in records),
    }


def due_day(today) -> date:
    """The 1st or 15th of the month that the latest backup should be from: the last one on or before today."""
    return today.replace(day=15 if today.day >= 15 else 1)


def latest_export(out_dir):
    """The name of the newest complete export in out_dir (its CSV and JSON both there), or None."""
    out_dir = Path(out_dir).expanduser()
    if not out_dir.is_dir():
        return None
    names = [path.stem for path in out_dir.glob("cellar-*.json")
             if EXPORT_NAME.fullmatch(path.stem) and path.with_suffix(".csv").is_file()]
    return max(names, default=None)


def export_due(out_dir, today) -> bool:
    """True when there is no complete export from the latest 1st or 15th or later."""
    latest = latest_export(out_dir)
    return latest is None or date.fromisoformat(EXPORT_NAME.fullmatch(latest)[1]) < due_day(today)


def main() -> int:
    args = argparse.ArgumentParser(description="Copy every entry to a CSV file and every record to a JSON file.")
    args.add_argument("--out", default=str(DEFAULT_OUT), help=f"folder to write to (default: {DEFAULT_OUT})")
    args.add_argument("--if-due", action="store_true",
                      help="export only if there is no backup yet since the last 1st or 15th of the month (for the schedule)")
    options = args.parse_args()

    if options.if_due:
        today = datetime.now(pricing.SGT).date()
        if not export_due(options.out, today):
            print(f"Not due: {latest_export(options.out)} is a backup from {due_day(today):%-d %b %Y} or later.")
            return 0

    live_table.use_live_table()
    table_name = config.table_name()
    records = store.all_records()
    try:
        result = export(records, options.out, datetime.now(timezone.utc), table_name)
    except ExportError as err:
        print(err, file=sys.stderr)
        return 2

    print(f"Scanned {result['records']} records from {table_name}:")
    for kind, count in sorted(result["kinds"].items()):
        print(f"  {kind:<6} {count}")
    print(f"Wrote {result['entries']} entries to {result['csv']}")
    print(f"Wrote {result['records']} records to {result['json']}")
    if not result["complete"]:
        print(f"Read back {result['rows_read']} rows and {result['records_read']} records, which doesn't match the scan, "
              "so the files keep their .partial names and don't count as a backup.", file=sys.stderr)
        return 1
    print("Read both files back: the counts match the scan.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
