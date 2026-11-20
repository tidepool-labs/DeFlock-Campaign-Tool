"""Exercise run identity at the agency response sheet CLI boundary."""
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from campaign_tool.records import agency_response_sheet


class ResponseSheetRunBindingTests(unittest.TestCase):
    def setUp(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.project = root / "project"
        self.artifact = self.project / "run-outputs/new-run/outcomes/response-items.json"
        self.artifact.parent.mkdir(parents=True)
        self.out = root / "sheet.md"

    def write_report(self, **identity):
        report = {
            "schema": "records-response-items-v1",
            "requests": [{"agency_key": "A1", "request_id": "R-1", "asks": [
                {"ask_id": 1, "text": "Produce the audit log", "rating": "answered"},
            ]}],
            **identity,
        }
        self.artifact.write_text(json.dumps(report), encoding="utf-8")

    def run_sheet(self, *selection):
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            code = agency_response_sheet.main([
                "--project", str(self.project), "--agency", "A1", "--out", str(self.out), *selection,
            ])
        return code, stderr.getvalue()

    def assert_refused_in_both_modes(self):
        for selection in (("--run-id", "new-run"), ()):
            with self.subTest(selection=selection):
                code, stderr = self.run_sheet(*selection)
                written = self.out.exists()
                if written:
                    self.out.unlink()
                self.assertEqual((code, stderr, written), (
                    2, "records agency-response-sheet: refused response_sheet_input\n", False,
                ))

    def test_copied_report_is_refused(self):
        # A shape-valid artifact from another run must not lend its rating to the selected run.
        self.write_report(run_id="old-run")
        self.assert_refused_in_both_modes()

    def test_report_without_embedded_run_is_refused(self):
        # The directory name alone cannot establish which run produced the report.
        self.write_report()
        self.assert_refused_in_both_modes()

    def test_matching_report_preserves_sheet_bytes(self):
        self.write_report(run_id="new-run")
        expected = (
            b"# Response sheet: A1\n"
            b"machine reading, review required; not a legal finding\n"
            b"run: new-run\n\n"
            b"## R-1\n"
            b"- ask 1 | tier 1 | raw answered | produced | flags: none\n\n"
            b"exemption cited means a citation appears in the text read; it is not a withholding and not a validity finding\n"
            b"not addressed means not mentioned in the text read; it is not a duty finding\n"
            b"unclear and text_unreviewed asks need a person to read the response\n"
        )
        for selection in (("--run-id", "new-run"), ()):
            with self.subTest(selection=selection):
                self.assertEqual(self.run_sheet(*selection), (0, ""))
                actual = self.out.read_bytes()
                self.out.unlink()
                self.assertEqual(actual, expected)
