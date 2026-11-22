"""Quoted fields from TEXTS.csv survive the handoff to the reviewer source index."""
import csv
import hashlib
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from campaign_tool.records import analysis_bridge as bridge
from campaign_tool.records import analysis_export as export


class AnalysisBridgeCsvRoundtripTests(unittest.TestCase):
    def test_legacy_header_preserves_quoted_multiline_fields(self):
        self.check_roundtrip(export.COLUMNS)

    def test_extended_header_preserves_quoted_multiline_fields(self):
        self.check_roundtrip(export.FLAG_COLUMNS)

    def check_roundtrip(self, columns):
        # Splitting physical lines before CSV parsing collapses these two IDs
        # into one value in SOURCES.csv, despite their different originals.
        for line_end, embedded_break in (("\n", "\n"), ("\r\n", "\n"), ("\r\n", "\r\n")):
            with self.subTest(line_end=line_end, embedded_break=embedded_break):
                self.assert_source_rows(columns, line_end, "REQ-2029" + embedded_break + "0123")

    def assert_source_rows(self, columns, line_end, multiline_id):
        run = "run_20290101T000000Z_aaaaaaaa"
        agency = "fixture-agency"
        files, expected = {}, []
        sink = io.StringIO(newline="")
        writer = csv.writer(sink, lineterminator=line_end)
        writer.writerow(columns)
        for index, request_id in enumerate((multiline_id, "REQ-20290123"), start=1):
            digest = str(index) * 64
            flags = '{"1":"read_with_flag"}' if columns == export.FLAG_COLUMNS and index == 1 else "{}"
            marker = " FLAGGED" if flags != "{}" else ""
            text = (f"[[PAGE 1{marker}]]\nSynthetic text {index}\n" +
                    "".join(f"[[PAGE {page}]]\nSynthetic text\n" for page in range(2, index + 1))).encode()
            files[f"texts/{digest}.txt"] = text
            text_sha = hashlib.sha256(text).hexdigest()
            received_at = f"2029-01-0{index}T12:00:00Z"
            source = f'fixture, "receipt {index}"'
            row = [digest, agency, request_id, str(index), text_sha, "exported", received_at, source]
            writer.writerow(row + [flags] if columns == export.FLAG_COLUMNS else row)
            expected.append(dict(source_id="REC-" + digest[:12], request_id=request_id, original_sha256=digest,
                                 pages=str(index), text_sha256=text_sha, run_id=run, received_at=received_at,
                                 received_at_source=source))

        rows = bridge._rows(sink.getvalue().encode("utf-8"))
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            profiles = root / "profiles"
            profiles.mkdir()
            (profiles / "fixture.json").write_text('{"slug":"fixture","records_agency_id":"fixture-agency"}')
            with patch.object(bridge.ac, "write_config", return_value="CONFIG_WRITTEN"):
                outcome, _, _, _ = bridge._place(root, run, agency, files, rows, dry=False)
            self.assertEqual(outcome, "SYNCED")
            with (root / "agencies/fixture/review/inputs/SOURCES.csv").open(newline="", encoding="utf-8") as handle:
                self.assertEqual(list(csv.DictReader(handle)), expected)
