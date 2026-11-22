"""Offline contract tests for the console-export evidence adapter."""

import contextlib
import hashlib
import io
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from campaign_tool.records import golive_adapt_export as adapter, golive_evidence
from tests.records.test_pipeline_health import report_for

NOW = datetime(2026, 11, 22, 12, tzinfo=timezone.utc)
ENGINE = "ab" * 20


class ExportEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.base = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.root = self.base / "console-export"
        self.root.mkdir(mode=0o700)
        self.report = self.base / "run-report.json"
        self.latest = self.write_report(48 * 60)
        generation = Path("exports") / ("20261120T120000Z-" + self.latest["run_id"])
        (self.root / generation).mkdir(mode=0o700, parents=True)
        (self.root / "current").symlink_to(generation, target_is_directory=True)
        self.manifest = self.root / generation / "EXPORT.json"
        self.write_manifest(approval_anchor={"state": "ok"})

    def write_report(self, minutes):
        value = report_for(NOW - timedelta(minutes=minutes))
        self.report.write_text(json.dumps(value) + "\n", encoding="utf-8")
        return value

    def write_manifest(self, **fields):
        value = {"schema": "records-console-export-v1", "run_id": self.latest["run_id"],
                 "exported_at": "2026-11-20T12:00:00Z", "source_project_id": "0123456789abcdef", "files": {}}
        self.manifest.write_text(json.dumps(dict(value, **fields)) + "\n", encoding="utf-8")

    def expected(self, export_status, anchor_state="OK", *, engine=None, from_report=False):
        path, schema = ((self.report, "records-run-report-v1") if from_report
                        else (self.manifest, "records-console-export-v1"))
        common = {"schema": "records-golive-evidence-v1", "kind": "live", "recorded_at": "2026-11-22T12:00:00Z",
                  "engine": engine, "actor": "pipeline",
                  "source": {"schema": schema, "sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "name": path.name}}
        return [dict(common, stage="A2", result="pass" if export_status == "CURRENT" else "fail",
                     codes=["CONSOLE_EXPORT_" + export_status]),
                dict(common, stage="20", result="pass" if anchor_state == "OK" else "fail",
                     codes=["APPROVAL_ANCHOR_" + anchor_state])]

    def assert_records(self, expected, *, root=True):
        actual = adapter.records(self.root if root else None, self.report, NOW)
        self.assertEqual(actual, expected)
        for row in actual:
            self.assertIsNone(golive_evidence.validate(row))

    def test_c1_current(self):
        """C1: Matching current export passes both; age alone must not mark it stale."""
        self.assert_records(self.expected("CURRENT"))

    def test_c1_stale(self):
        """C1: A different run beyond grace fails A2, not the recorded anchor."""
        self.write_report(16)
        self.assert_records(self.expected("STALE"))

    def test_c1_pending(self):
        """C1: The inclusive grace boundary is pending, not an A2 pass."""
        self.write_report(15)
        self.assert_records(self.expected("PENDING"))

    def test_c1_anchor_non_ok(self):
        """C1: Missing or unfamiliar anchor state cannot pass; known unavailable is distinct."""
        for fields, state in (({}, "UNKNOWN"), ({"approval_anchor": {"state": "OK"}}, "UNKNOWN"),
                              ({"approval_anchor": {"state": "unavailable"}}, "UNAVAILABLE")):
            with self.subTest(state=fields):
                self.write_manifest(**fields)
                self.assert_records(self.expected("CURRENT", state))

    def test_c1_no_run(self):
        """C1: Missing, unreadable, invalid or future reports yield no-run, not false freshness."""
        absent = self.base / "absent.json"
        expected = self.expected("NO_RUN")
        self.assertEqual(adapter.records(self.root, absent, NOW), expected)
        for raw in ("{", json.dumps({"run_id": self.latest["run_id"]})):
            with self.subTest(raw=raw):
                self.report.write_text(raw, encoding="utf-8")
                self.assert_records(expected)
        self.write_report(-1)
        self.assert_records(expected)

    def test_c1_unreadable(self):
        """C1: Invalid export bytes cannot be proof; failure binds the real run-report bytes."""
        for raw in ("{", json.dumps({"run_id": self.latest["run_id"], "schema": "unrecognised"})):
            with self.subTest(raw=raw):
                self.manifest.write_text(raw, encoding="utf-8")
                self.assert_records(self.expected("UNREADABLE", "UNKNOWN", from_report=True))

    def test_c1_not_configured(self):
        """C1: An unconfigured export with a valid run is an explicit failure."""
        self.assert_records(self.expected("NOT_CONFIGURED", "UNKNOWN", from_report=True), root=False)

    def test_c1_no_bindable_receipt(self):
        """C1: Two unreadable inputs cannot supply a source digest."""
        with self.assertRaises(ValueError) as error:
            adapter.records(None, self.base / "absent.json", NOW)
        self.assertIs(type(error.exception), ValueError)

    def test_c2_engine(self):
        """C2: Both pipeline rows use the manifest engine, not an unvalidated token."""
        for engine, expected in ((ENGINE, ENGINE), ("unverified", None)):
            with self.subTest(engine=engine):
                self.write_manifest(engine=engine, approval_anchor={"state": "ok"})
                self.assert_records(self.expected("CURRENT", engine=expected))

    def cli(self, *flags):
        out, err = io.StringIO(), io.StringIO()
        args = ["--export-root", str(self.root), "--run-report", str(self.report), *flags]
        # Freeze only the clock; exercise the real CLI parser, reader and serializer.
        with patch.object(adapter, "datetime") as clock, contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            clock.now.return_value = NOW
            status = adapter.main(args)
        return status, out.getvalue(), err.getvalue()

    def test_c3_cli_pass(self):
        """C3: Both passes are printed as JSON Lines and exit 0."""
        status, out, err = self.cli()
        self.assertEqual((status, list(map(json.loads, out.splitlines())), err), (0, self.expected("CURRENT"), ""))

    def test_c3_cli_failed_stage(self):
        """C3: A failed stage still prints both rows, with JSON array output and exit 2."""
        self.write_manifest(approval_anchor={"state": "unavailable"})
        status, out, err = self.cli("--json")
        self.assertEqual((status, json.loads(out), err), (2, self.expected("CURRENT", "UNAVAILABLE"), ""))

    def test_c3_cli_bad_input(self):
        """C3: A refusal prints only the stable diagnostic and exits 2."""
        self.report.write_text("{", encoding="utf-8")
        self.manifest.write_text("{", encoding="utf-8")
        self.assertEqual(self.cli(), (2, "", "CONSOLE_EXPORT_EVIDENCE_BAD_INPUT\n"))
