"""Synthetic contract tests for stage 17b capture evidence."""

import hashlib
import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from campaign_tool.records import golive_adapt_site as site, golive_evidence

NOW = datetime(2026, 11, 22, 10, 1, 2, 123, timezone(timedelta(hours=-8)))
STAMP = "2026-11-22T18:01:02Z"


def expected(raw, result, codes, name="site-check.tsv"):
    return {
        "schema": "records-golive-evidence-v1", "stage": "17b", "kind": "live",
        "result": result, "recorded_at": STAMP, "engine": None, "actor": "operator",
        "source": {"schema": "records-site-page-check-v1",
                   "sha256": hashlib.sha256(raw).hexdigest(), "name": name},
        "codes": codes,
    }


class SiteRecordsTests(unittest.TestCase):
    def test_c1_coverage(self):
        """C1: Only the expected roster counts; missing and non-200 are distinct."""
        # Counting successful rows or accepting any 2xx could hide a gap.
        cases = (
            ("alpha\t200\r\nbeta\t200\r\ngamma\t503\r\n", "pass", []),
            ("alpha\t200\ngamma\t200\n", "fail", ["SITE_PAGE_MISSING_1"]),
            ("alpha\t204\nbeta\t302\n", "fail", ["SITE_PAGE_ERROR_2"]),
            ("alpha\t503\n", "fail", ["SITE_PAGE_MISSING_1", "SITE_PAGE_ERROR_1"]),
            ("", "fail", ["SITE_PAGE_MISSING_2"]),
        )
        for text, result, codes in cases:
            with self.subTest(text=text):
                row = site.records(text, iter(("alpha", "beta")), NOW)
                self.assertEqual(row, expected(text.encode(), result, codes))
                self.assertIsNone(golive_evidence.validate(row))

    def test_c1_no_vacuous_evidence(self):
        """C1: A missing expected roster cannot certify coverage."""
        with self.assertRaises(ValueError) as cm:
            site.records("alpha\t200\n", [], NOW)
        self.assertIs(type(cm.exception), ValueError)
        self.assertEqual(str(cm.exception), "campaign has no agency slugs")

    def test_c1_timestamp_requires_instant(self):
        """C1: A local timestamp must not silently produce UTC evidence."""
        with self.assertRaises(ValueError) as cm:
            site.records("alpha\t200", ["alpha"], NOW.replace(tzinfo=None))
        self.assertIs(type(cm.exception), ValueError)
        self.assertEqual(str(cm.exception), "timestamp must identify an instant")

    def test_c2_invalid_rows(self):
        """C2: Refuse malformed and ambiguous rows at their physical line."""
        # A permissive parser could silently drop rows or overwrite a failure.
        cases = (b"beta 200", b"beta\t200\textra", b"\t200", b"/beta\t200",
                 b"beta\tunknown", b"beta\t600", b"beta\t 200", b"\xff\t200", b"",
                 b"alpha\t503")
        for row in cases:
            with self.subTest(row=row):
                with self.assertRaises(ValueError) as cm:
                    site.records(b"alpha\t200\n" + row + b"\n", ["alpha"], NOW)
                self.assertIs(type(cm.exception), ValueError)
                reason = "duplicate slug" if row.startswith(b"alpha") else "expected slug and HTTP status"
                self.assertEqual(str(cm.exception), f"line 2: {reason}")


class SiteCliTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        content_dir = self.root / "content"
        content_dir.mkdir()
        (content_dir / "agencies.json").write_text(json.dumps([
            {"agency_id": "zz-alpha", "name": "Alpha"},
            {"agency_id": "zz-beta", "name": "Beta", "slug": "renamed"},
            {"agency_id": "zz-gamma", "name": "Gamma", "selected": False},
        ]), encoding="utf-8")
        self.capture = self.root / "operator-capture.tsv"

    def run_cli(self, raw=None, *flags):
        if raw is not None:
            self.capture.write_bytes(raw)
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err), patch.object(site, "datetime") as clock:
            clock.now.return_value = NOW
            status = site.main([str(self.capture), str(self.root), *flags])
        return status, out.getvalue(), err.getvalue()

    def test_c3_json_success(self):
        """C3: JSON binds the actual receipt and uses the public slug rules."""
        raw = b"alpha\t200\r\nrenamed\t200\r\n"
        status, out, err = self.run_cli(raw, "--json")
        self.assertEqual((status, json.loads(out), err),
                         (0, [expected(raw, "pass", [], "operator-capture.tsv")], ""))

    def test_c3_tsv_failure(self):
        """C3: Negative evidence is emitted but cannot look like CLI success."""
        self.assertEqual(self.run_cli(b"alpha\t200\n"),
                         (2, f"17b\tlive\tfail\t{STAMP}\trecords-site-page-check-v1\tSITE_PAGE_MISSING_1\n", ""))

    def test_c3_malformed_capture(self):
        """C3: A parser refusal emits no evidence or captured values."""
        self.assertEqual(self.run_cli(b"secret\tbad-status\n"),
                         (2, "", "golive_adapt_site: line 1: expected slug and HTTP status\n"))

    def test_c3_iodiagnostic(self):
        """C3: Missing inputs emit no record or local path."""
        self.assertEqual(self.run_cli(), (2, "", "SITE_EVIDENCE_BAD_INPUT\n"))
