"""Header uniqueness in the read-only tracker proposal checker."""
import csv
import tempfile
import unittest
from pathlib import Path

from campaign_tool.records import tracker_proposal as tp
from tests.records.test_tracker_proposal_row_shape import GOOD


class DuplicateColumnTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "tracker.csv"

    def check(self, *columns, **overrides):
        row = dict(GOOD, **overrides)
        with self.path.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.writer(fh)
            writer.writerow([*tp.REQUIRED, *(name for name, _ in columns)])
            writer.writerow([*(row[name] for name in tp.REQUIRED), *(value for _, value in columns)])
        return tp.check_proposal(self.path)

    def test_duplicate_status_cannot_hide_an_invalid_value(self):
        self.assertEqual(
            self.check(("status", "unknown"), status="INVALID"),
            ([], ["header_duplicate:status"]),
        )

    def test_duplicate_optional_column_cannot_hide_a_note(self):
        self.assertEqual(
            self.check(("notes", "first note"), ("notes", "second note")),
            ([], ["header_duplicate:notes"]),
        )

    def test_unique_optional_column_retains_its_value(self):
        self.assertEqual(self.check(("notes", "keep me")), ([dict(GOOD, notes="keep me")], []))

    def test_duplicate_header_is_rejected_before_malformed_row_is_parsed(self):
        self.path.write_text(
            ",".join([*tp.REQUIRED, "notes", "notes"]) + '\n"unterminated\n',
            encoding="utf-8",
        )
        self.assertEqual(tp.check_proposal(self.path), ([], ["header_duplicate:notes"]))
