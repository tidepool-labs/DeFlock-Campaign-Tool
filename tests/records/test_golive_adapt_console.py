import hashlib
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from campaign_tool.records import console_check, golive_adapt_console as adapter, golive_evidence, golive_ledger

NOW = datetime(2026, 1, 2, 12, tzinfo=timezone.utc)
OBSERVED = datetime(2026, 1, 2, 10, tzinfo=timezone.utc)
STAMP = "2026-01-02T10:00:00Z"
# Pinned independently of the adapter so a console_check change shows up here.
LABELS = ("GET /", "GET /board", "GET /documents", "GET /calendar", "GET /settings", "GET /next-steps",
          "GET /send", "POST /approvals/reject", "export")
LOOPBACK = "CONSOLE_LOOPBACK_ONLY"


def transcript(*, drop=(), replace=None, extra=()):
    replace = replace or {}
    lines = [f"{label} {replace.get(label, 'PASS')}" for label in LABELS if label not in drop]
    return ("\n".join([*lines, *extra]) + "\n").encode()


def expected(raw, result="pass", codes=(), *, name="console-check.txt"):
    return {"schema": "records-golive-evidence-v1", "stage": "A3", "kind": "live", "result": result,
            "recorded_at": STAMP, "engine": None, "actor": "operator", "codes": [*codes, LOOPBACK],
            "source": {"schema": "records-console-check-v1", "sha256": hashlib.sha256(raw).hexdigest(),
                       "name": name}}


class ConsoleEvidenceTests(unittest.TestCase):
    def test_c1_full_pass(self):
        """C1: A complete all-PASS transcript is one valid pass row bound to its bytes (a)."""
        raw = transcript()
        row = adapter.records(raw, OBSERVED, NOW)
        self.assertEqual(row, expected(raw))
        self.assertIsNone(golive_evidence.validate(row))

    def test_c1_ledger_reads_a3_live(self):
        """C1: The ledger counts the pass row as A3 live evidence; a fail row blocks it."""
        for result_raw, state, status in ((transcript(), "LIVE", "evidence"),
                                          (transcript(replace={"export": "EXPORT_STALE"}), "BLOCKED", "missing")):
            with self.subTest(state=state):
                row = adapter.records(result_raw, OBSERVED, NOW)
                a3 = next(item for item in golive_ledger.build([], [row], NOW) if item["stage"] == "A3")
                self.assertEqual((a3["state"], a3["live"]["status"]), (state, status))

    def test_c2_non_pass_line(self):
        """C2: Any non-PASS probe fails the row (b); a wrong 'any line is fine' check would pass."""
        for label in LABELS:
            for code in ("HTTP_500", "HTTP_UNAVAILABLE", "EXPORT_STALE", "SEND_EXPOSED"):
                with self.subTest(label=label, code=code):
                    raw = transcript(replace={label: code})
                    self.assertEqual(adapter.records(raw, OBSERVED, NOW),
                                     expected(raw, "fail", ["CONSOLE_CHECK_NOT_PASS"]))

    def test_c2_missing_label(self):
        """C2: A partial run (any label absent, or an empty file) cannot certify the console (c)."""
        for drop in [(label,) for label in LABELS] + [LABELS]:
            with self.subTest(drop=drop):
                raw = transcript(drop=drop) if drop != LABELS else b""
                self.assertEqual(adapter.records(raw, OBSERVED, NOW),
                                 expected(raw, "fail", ["CONSOLE_CHECK_LABEL_MISSING"]))

    def test_c2_duplicate_unknown_and_malformed(self):
        """C2: A repeated label (even PASS) or a stray line cannot hide a bad probe (c)."""
        for extra in ("export PASS", "GET /board PASS", "GET /admin PASS", "PASS", "GET /send pass", "",
                      "GET /send  PASS", "POST /approvals/reject PASS\r"):
            with self.subTest(extra=extra):
                raw = transcript(extra=(extra,))
                self.assertEqual(adapter.records(raw, OBSERVED, NOW),
                                 expected(raw, "fail", ["CONSOLE_CHECK_UNEXPECTED_LINE"]))

    def test_c2_first_problem_only(self):
        """C2: Codes name the first problem in transcript order, then loopback-only."""
        raw = transcript(drop=("export",), replace={"GET /board": "HTTP_404"})
        self.assertEqual(adapter.records(raw, OBSERVED, NOW)["codes"], ["CONSOLE_CHECK_NOT_PASS", LOOPBACK])
        raw = transcript(drop=("GET /board",), extra=("GET /x PASS",))
        self.assertEqual(adapter.records(raw, OBSERVED, NOW)["codes"], ["CONSOLE_CHECK_UNEXPECTED_LINE", LOOPBACK])

    def test_c2_labels_follow_console_check(self):
        """C2: Expected labels come from console_check, so a page change cannot go unnoticed (d)."""
        self.assertEqual(adapter.expected_labels(), LABELS)
        with patch.object(console_check, "PAGES", (*console_check.PAGES, "/new-page")):
            self.assertIn("GET /new-page", adapter.expected_labels())
            self.assertEqual(adapter.records(transcript(), OBSERVED, NOW)["codes"],
                             ["CONSOLE_CHECK_LABEL_MISSING", LOOPBACK])
        # A label console_check prints from outside its constants would escape the adapter.
        out = io.StringIO()
        with patch.object(console_check, "_http_check", return_value="PASS"), \
                patch.object(console_check, "_export_check", return_value="PASS"), redirect_stdout(out):
            self.assertEqual(console_check.main(["--port", "1", "--export-root", "/nowhere"]), 0)
        self.assertEqual([line.rpartition(" ")[0] for line in out.getvalue().splitlines()],
                         list(adapter.expected_labels()))
        self.assertEqual(adapter.records(out.getvalue().encode(), OBSERVED, NOW)["result"], "pass")

    def test_c3_loopback_only_always(self):
        """C3: A pass is never read as Access or phone-login proof, so the code is on every row."""
        for raw in (transcript(), transcript(drop=("export",)), transcript(replace={"export": "EXPORT_STALE"}),
                    transcript(extra=("junk",))):
            self.assertEqual(adapter.records(raw, OBSERVED, NOW)["codes"][-1], LOOPBACK)
        self.assertEqual(adapter.records(transcript(), OBSERVED, NOW)["codes"], [LOOPBACK])

    def test_c3_time_bounds(self):
        """C3: Future, over-24h and zone-less observation times are refused; exactly 24h is kept."""
        self.assertEqual(adapter.records(transcript(), NOW - timedelta(hours=24), NOW)["result"], "pass")
        for observed in (NOW + timedelta(seconds=1), NOW - timedelta(hours=24, seconds=1),
                         datetime(2026, 1, 2, 10)):
            with self.subTest(observed=observed), self.assertRaises(ValueError):
                adapter.records(transcript(), observed, NOW)

    def test_c3_input_bounds(self):
        """C3: Oversize or non-UTF-8 bytes never become evidence."""
        for raw in (b"x" * (adapter.MAX_BYTES + 1), b"export PASS\xff\n"):
            with self.subTest(size=len(raw)), self.assertRaises(ValueError):
                adapter.records(raw, OBSERVED, NOW)


class ConsoleCLITests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)  # mkdtemp: private 0700, not a symlink
        self.path = self.root / "capture.txt"

    def invoke(self, path=None, observed="2026-01-02T02:00:00-08:00", *args):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err), patch.object(adapter, "_now", return_value=NOW):
            status = adapter.main([str(path or self.path), "--observed-at", observed, *args])
        return status, out.getvalue(), err.getvalue()

    def test_c1_modes_and_source(self):
        """C1: JSON binds the saved bytes and basename; text is one tab-separated summary row."""
        raw = transcript()
        self.path.write_bytes(raw)
        status, out, err = self.invoke(None, "2026-01-02T02:00:00-08:00", "--json")
        self.assertEqual((status, json.loads(out), err), (0, [expected(raw, name="capture.txt")], ""))
        self.assertEqual(self.invoke(), (0, f"A3\tlive\tpass\t{STAMP}\trecords-console-check-v1\t{LOOPBACK}\n", ""))
        self.assertEqual(self.path.read_bytes(), raw)

    def test_c2_fail_is_printed(self):
        """C2: A failed check is still recorded evidence (exit 2), unlike refused input."""
        raw = transcript(replace={"GET /send": "SEND_EXPOSED"})
        self.path.write_bytes(raw)
        status, out, err = self.invoke(None, STAMP, "--json")
        self.assertEqual((status, json.loads(out), err),
                         (2, [expected(raw, "fail", ["CONSOLE_CHECK_NOT_PASS"], name="capture.txt")], ""))

    def test_c3_bad_input(self):
        """C3: Unreadable, symlinked, oversize, non-UTF-8 or mis-dated input is refused without a row."""
        refused = (2, "", "CONSOLE_EVIDENCE_BAD_INPUT\n")
        good = self.root / "good.txt"
        good.write_bytes(transcript())
        link = self.root / "link.txt"
        os.symlink(good, link)
        bad = {"missing": self.path, "directory": self.root, "symlink": link}
        for name, path in bad.items():
            with self.subTest(case=name):
                self.assertEqual(self.invoke(path, STAMP), refused)
        for name, raw in (("oversize", b" " * (adapter.MAX_BYTES + 1)), ("non-utf8", b"\xff\xfe")):
            with self.subTest(case=name):
                self.path.write_bytes(raw)
                self.assertEqual(self.invoke(None, STAMP), refused)
        self.path.write_bytes(transcript())
        for observed in ("2026-01-02T12:00:01Z", "2026-01-01T11:59:59Z", "2026-01-02T10:00:00", "yesterday"):
            with self.subTest(observed=observed):
                self.assertEqual(self.invoke(None, observed), refused)

    def test_c4_never_runs_the_check(self):
        """C4: The adapter reads a saved file only; no probe runs and no socket opens."""
        self.path.write_bytes(transcript())
        boom = AssertionError("adapter must not run the check or open a connection")
        with patch.object(console_check, "_http_check", side_effect=boom), \
                patch.object(console_check, "_export_check", side_effect=boom), \
                patch.object(console_check, "main", side_effect=boom), \
                patch("socket.socket.connect", side_effect=boom), \
                patch("http.client.HTTPConnection.connect", side_effect=boom):
            self.assertEqual(self.invoke(None, STAMP)[0], 0)


if __name__ == "__main__":
    unittest.main()
