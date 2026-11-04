"""Tests for scripts/export_csv.py against an in-memory table. Nothing touches AWS."""
import csv
import importlib.util
import json
import sys
import tempfile
import unittest
from datetime import date, datetime, timezone
from pathlib import Path
from unittest import mock

from fakes import FakeTable

import store

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("export_csv", ROOT / "scripts" / "export_csv.py")
export_csv = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(export_csv)

NOW = datetime(2026, 11, 4, 7, 30, tzinfo=timezone.utc)   # 15:30 in Singapore
STEM = "cellar-2026-11-04-1530"


class ExportCsvTest(unittest.TestCase):
    def setUp(self):
        self.table = FakeTable()
        patch = mock.patch.object(store, "_table", self.table)
        patch.start()
        self.addCleanup(patch.stop)
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.out = Path(folder.name) / "backups"

        store.create_item({"item_id": "hand-wash", "name": "Hand wash", "base_unit": "ml", "aliases": ["handwash"],
                           "status": "ok", "stock_est": 1200, "pack_size": 400, "brands": ["Aesop", "Dettol"]})
        store.create_item({"item_id": "toilet-paper", "name": "Toilet paper", "base_unit": "roll", "aliases": []})
        store.put_event("hand-wash", "2026-11-03T06:00:00Z#120#2", {
            "action": "stock_count", "qty": 800, "packs": 2, "units_per_pack": 400, "brand": "Dettol",
            "description": "green tea fragrance", "expires_on": "2028-01-01", "adds": True,
            "user_id": 42, "user_name": "Mei", "raw_text": 'Dettol, "green tea" 2x400ml\nexpires Jan 2028',
            "at": "2026-11-03T06:00:00Z", "source": "chat", "parse_path": "ai_full"})
        store.put_event("hand-wash", "2026-11-03T06:00:00Z#120#10", {
            "action": "stock_count", "qty": 400, "brand": "Aesop", "user_id": 42, "user_name": "Mei",
            "raw_text": "Aesop 400ml 半亩花田", "at": "2026-11-03T06:00:00Z"})
        store.put_event("toilet-paper", "2026-11-03T02:00:00Z#110#0", {
            "action": "bought", "qty": 20, "packs": 2, "units_per_pack": 10, "total_price": 12.9,
            "unit_price": 0.645, "currency": "SGD", "store": "FairPrice", "user_id": 7, "user_name": "Ben",
            "raw_text": "+ toilet paper 2x10 $12.90 @fairprice on 15/09/2026",
            "at": "2026-09-15T04:00:00Z", "logged_at": "2026-11-03T02:00:00Z"})
        store.put_event("old-item", "2026-10-09T15:39:19Z#75#0", {   # its item record is gone
            "action": "bought", "packs": 1, "units_per_pack": 1000, "total_price": 17.13, "store": "Taobao",
            "user_id": 42, "raw_text": "Face masks 1000 pieces for 17.13 from Taobao", "at": "2026-10-09T15:39:19Z"})
        store.save_store("taobao", "Taobao", "2026-10-09T15:39:19Z")
        store.save_brand("aesop", "Aesop", 42)
        store.add_stats("2026-11", {"lines_structured": 3, "ai_full_calls": 1})
        store.first_delivery(120)

    def export(self):
        return export_csv.export(store.all_records(), self.out, NOW, "test-table")

    def csv_rows(self):
        with open(self.out / f"{STEM}.csv", newline="", encoding="utf-8-sig") as file:
            return list(csv.DictReader(file))

    def json_file(self):
        with open(self.out / f"{STEM}.json", encoding="utf-8") as file:
            return json.load(file)

    def test_one_row_per_entry_oldest_first_and_lines_in_order(self):
        result = self.export()
        rows = self.csv_rows()
        self.assertEqual((result["entries"], result["rows_read"]), (4, 4))
        self.assertEqual([(row["item_id"], row["sk"]) for row in rows], [
            ("toilet-paper", "2026-11-03T02:00:00Z#110#0"),   # dated 15 Sep, so first
            ("old-item", "2026-10-09T15:39:19Z#75#0"),
            ("hand-wash", "2026-11-03T06:00:00Z#120#2"),
            ("hand-wash", "2026-11-03T06:00:00Z#120#10"),     # line 10 after line 2
        ])

    def test_columns_hold_what_the_entry_says(self):
        self.export()
        paper, masks, dettol, aesop = self.csv_rows()
        self.assertEqual(list(paper), [column for column, _ in export_csv.COLUMNS])
        self.assertEqual(
            {key: paper[key] for key in ("item", "when", "logged_at", "action", "quantity", "unit", "packs",
                                         "units_per_pack", "total_price", "unit_price", "currency", "store", "who")},
            {"item": "Toilet paper", "when": "2026-09-15 12:00:00", "logged_at": "2026-11-03 10:00:00",
             "action": "bought", "quantity": "20", "unit": "roll", "packs": "2", "units_per_pack": "10",
             "total_price": "12.9", "unit_price": "0.645", "currency": "SGD", "store": "FairPrice", "who": "Ben"})
        self.assertEqual((dettol["brand"], dettol["description"], dettol["expires_on"], dettol["adds"]),
                         ("Dettol", "green tea fragrance", "2028-01-01", "yes"))
        self.assertEqual(dettol["raw_text"], 'Dettol, "green tea" 2x400ml\nexpires Jan 2028')
        self.assertEqual(aesop["raw_text"], "Aesop 400ml 半亩花田")
        self.assertEqual((aesop["adds"], aesop["logged_at"], aesop["total_price"]), ("", "", ""))
        self.assertEqual((masks["item"], masks["unit"], masks["quantity"], masks["who"]), ("", "", "", ""))

    def test_json_holds_every_record_and_can_be_put_back_as_it_was(self):
        result = self.export()
        saved = self.json_file()
        self.assertEqual((saved["table"], saved["exported_at"], saved["home_currency"]),
                         ("test-table", "2026-11-04T07:30:00Z", "SGD"))
        self.assertEqual(saved["record_count"], len(self.table.rows))
        self.assertEqual((result["records"], result["records_read"]), (len(self.table.rows), len(self.table.rows)))
        self.assertEqual(dict(result["kinds"]), {"BRAND": 1, "EVT": 4, "ITEM": 2, "STATS": 1, "STORE": 1, "UPD": 1})
        put_back = {(record["pk"], record["sk"]): store._to_db(record) for record in saved["records"]}
        self.assertEqual(put_back, self.table.rows)

    def test_reads_every_page_of_the_scan(self):
        self.table.page_size = 2
        result = self.export()
        self.assertEqual((result["records"], result["entries"]), (len(self.table.rows), 4))

    def test_changes_nothing_in_the_table(self):
        before = {key: dict(row) for key, row in self.table.rows.items()}
        self.export()
        self.assertEqual(self.table.rows, before)

    def test_never_overwrites_an_earlier_export(self):
        self.export()
        written = (self.out / f"{STEM}.csv").read_bytes()
        store.put_event("toilet-paper", "2026-11-04T07:00:00Z#130#0", {"action": "finished", "at": "2026-11-04T07:00:00Z"})
        with self.assertRaisesRegex(export_csv.ExportError, "already in"):
            self.export()
        self.assertEqual((self.out / f"{STEM}.csv").read_bytes(), written)

    def test_an_empty_table_writes_just_the_header(self):
        self.table.rows.clear()
        result = self.export()
        self.assertEqual((result["entries"], result["records"], result["rows_read"]), (0, 0, 0))
        self.assertEqual(self.csv_rows(), [])


    def test_a_complete_export_leaves_no_partial_files(self):
        result = self.export()
        self.assertTrue(result["complete"])
        self.assertEqual(sorted(path.name for path in self.out.iterdir()), [f"{STEM}.csv", f"{STEM}.json"])

    def test_files_that_do_not_read_back_keep_their_partial_names(self):
        with mock.patch.object(export_csv.csv, "DictReader", return_value=iter([{}])):   # reads back 1 row, not 4
            result = self.export()
        self.assertFalse(result["complete"])
        self.assertEqual(sorted(path.name for path in self.out.iterdir()), [f"{STEM}.csv.partial", f"{STEM}.json.partial"])
        self.assertIsNone(export_csv.latest_export(self.out))


class ScheduleTest(unittest.TestCase):
    """The schedule runs the export every day with --if-due; it exports on the 1st and 15th, or the first day after."""

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.out = Path(folder.name)

    def backup(self, stem, suffixes=(".csv", ".json")):
        for suffix in suffixes:
            (self.out / f"{stem}{suffix}").write_text("")

    def test_due_day_is_the_latest_1st_or_15th(self):
        cases = [((2026, 11, 1), 1), ((2026, 11, 4), 1), ((2026, 11, 14), 1), ((2026, 11, 15), 15), ((2026, 11, 30), 15)]
        for (year, month, day), expected in cases:
            with self.subTest(day=day):
                self.assertEqual(export_csv.due_day(date(year, month, day)), date(year, month, expected))

    def test_due_until_there_is_a_backup_from_the_due_day_or_later(self):
        self.assertTrue(export_csv.export_due(self.out / "not-made-yet", date(2026, 11, 4)))
        self.assertTrue(export_csv.export_due(self.out, date(2026, 11, 4)))
        self.backup("cellar-2026-10-20-1000")
        self.assertTrue(export_csv.export_due(self.out, date(2026, 11, 1)))   # missed the 1st: due on the next run
        self.backup("cellar-2026-11-04-2157")   # one made by hand counts too
        self.assertFalse(export_csv.export_due(self.out, date(2026, 11, 4)))
        self.assertFalse(export_csv.export_due(self.out, date(2026, 11, 14)))
        self.assertTrue(export_csv.export_due(self.out, date(2026, 11, 15)))
        self.backup("cellar-2026-11-15-1000")
        self.assertFalse(export_csv.export_due(self.out, date(2026, 11, 30)))
        self.assertTrue(export_csv.export_due(self.out, date(2026, 12, 1)))

    def test_only_a_complete_backup_counts(self):
        self.backup("cellar-2026-11-15-1000", (".csv",))                     # no JSON
        self.backup("cellar-2026-11-16-1000", (".csv.partial", ".json.partial"))
        self.backup("notes-2026-11-17-1000")                                 # not an export
        self.assertIsNone(export_csv.latest_export(self.out))
        self.assertTrue(export_csv.export_due(self.out, date(2026, 11, 20)))

    def test_not_due_reads_nothing_from_aws(self):
        self.backup(f"cellar-{datetime.now(export_csv.pricing.SGT):%Y-%m-%d}-0000")
        argv = ["export_csv.py", "--if-due", "--out", str(self.out)]
        with mock.patch.object(sys, "argv", argv), \
                mock.patch.object(export_csv.live_table, "use_live_table", side_effect=AssertionError("touched AWS")), \
                mock.patch.object(export_csv.store, "all_records", side_effect=AssertionError("scanned")), \
                mock.patch("builtins.print"):
            self.assertEqual(export_csv.main(), 0)


class StoreScanTest(unittest.TestCase):
    def test_all_records_returns_plain_numbers_and_the_keys(self):
        table = FakeTable()
        with mock.patch.object(store, "_table", table):
            store.put_event("rice", "2026-11-03T02:00:00Z#1#0", {"action": "bought", "qty": 5000, "total_price": 12.5})
            record, = store.all_records()
        self.assertEqual(record, {"pk": "EVT#rice", "sk": "2026-11-03T02:00:00Z#1#0", "action": "bought",
                                  "qty": 5000, "total_price": 12.5})


if __name__ == "__main__":
    unittest.main()
