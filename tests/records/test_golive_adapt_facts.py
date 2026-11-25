"""Contract tests for the stage 9/10 run-outputs evidence adapter (synthetic ledger, real writers)."""

import contextlib
import hashlib
import io
import json
import os
import shutil
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from campaign_tool.records import golive_adapt_facts as adapter, golive_evidence, golive_ledger
from campaign_tool.records.ledger import store
from campaign_tool.records.outcomes import comparison, factsheets
from tests.records.test_pipeline_health import report_for

NOW = datetime(2026, 11, 24, 12, tzinfo=timezone.utc)
NAMES = {"agency-zzyzx-pd": "Zzyzx Harbor Police Department", "agency-quux-sd": "Quux County Sheriff"}
FACTS, COMPARE = "agency-factsheets.json", "comparison.json"
OTHER_RUN = "run_20261124T000000Z_0000dead"
REPORT = report_for(NOW - timedelta(minutes=10))
RUN_ID = REPORT["run_id"]


def build_outputs(root, agencies):
    """Run the REAL writers on a synthetic processing ledger; return run-outputs/<run id>."""
    processing = root / "processing-v1"
    processing.mkdir(parents=True, mode=0o700)
    database = processing / "ledger.sqlite"
    store.initialize(database)
    with store.ledger(database) as con:
        for agency_id, name in agencies.items():
            con.execute("INSERT INTO agencies VALUES(?,?,NULL,NULL)", (agency_id, name))
        con.commit()
    out = root / "run-outputs" / RUN_ID
    out.parent.mkdir(mode=0o700)
    factsheets.write_run_factsheets(processing, out, RUN_ID)
    comparison.write_run_comparison(root, RUN_ID)
    return out


def variants(doc, key, ident):
    """The same defect list for either file: a replacement value (bytes, dict) or None for an absent file."""
    return {
        "missing": None, "empty": b"", "blank": b" \n", "not json": b"{", "not utf8": b"\xff\xfe",
        "duplicate key": b'{"schema": "x", "schema": "x"}', "NaN": b'{"x": NaN}', "array": b"[]",
        "wrong run": dict(doc, run_id=OTHER_RUN), "no run": {k: v for k, v in doc.items() if k != "run_id"},
        "wrong schema": dict(doc, schema="records-golive-evidence-v1"),
        "no list": {k: v for k, v in doc.items() if k != key}, "list not list": dict(doc, **{key: {"a": 1}}),
        "nameless item": dict(doc, **{key: [{ident: " "}]}), "empty list": dict(doc, **{key: []}),
    }


class FactsEvidenceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        base = Path(tempfile.mkdtemp(prefix="facts-ev-", dir=os.environ.get("TMPDIR"))).resolve()
        cls.addClassCleanup(shutil.rmtree, base, True)
        cls.full, cls.empty = build_outputs(base / "full", NAMES), build_outputs(base / "empty", {})

    def setUp(self):
        self.base = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.report = json.loads(json.dumps(REPORT))
        self.report_path = self.base / "run-report.json"
        self.write_report()
        self.out = self.copy_outputs(self.full)

    def copy_outputs(self, source, name=RUN_ID):
        target = self.base / "copied" / name
        target.parent.mkdir(exist_ok=True)
        shutil.copytree(source, target)
        return target

    def write_report(self):
        self.report_path.write_text(json.dumps(self.report) + "\n", encoding="utf-8")

    def put(self, name, value):
        (self.out / name).write_bytes(value if isinstance(value, bytes) else json.dumps(value).encode())

    def doc(self, name):
        return json.loads((self.out / name).read_text())

    def records(self):
        return adapter.records(self.out, self.report_path, NOW)

    def expected(self, stage, name, schema):
        sha = hashlib.sha256((self.out / name).read_bytes()).hexdigest()
        return {"schema": "records-golive-evidence-v1", "stage": stage, "kind": "live", "result": "pass",
                "recorded_at": "2026-11-24T12:00:00Z", "engine": None, "actor": "pipeline", "codes": [],
                "source": {"schema": schema, "sha256": sha, "name": name}}

    def results(self):
        return {row["stage"]: (row["result"], row["codes"]) for row in self.records()}

    def cli(self, *flags):
        out, err = io.StringIO(), io.StringIO()
        args = ["--outputs-dir", str(self.out), "--run-report", str(self.report_path), *flags]
        with (patch.object(adapter, "datetime", wraps=datetime) as clock,
              contextlib.redirect_stdout(out), contextlib.redirect_stderr(err)):
            clock.now.return_value = NOW
            status = adapter.main(args)
        return status, out.getvalue(), err.getvalue()

    def assert_unbound(self):
        with self.assertRaises(ValueError) as caught:
            self.records()
        self.assertEqual(str(caught.exception), "OUTPUTS_RUN_UNBOUND")

    def test_c1_healthy_control_rows_and_ledger_join(self):
        """C1: real writer output gives two live rows and the ledger leaves AGGREGATE_ONLY (bug: wrong stage/kind)."""
        rows = self.records()
        self.assertEqual(rows, [self.expected("9", FACTS, factsheets.SCHEMA),
                                self.expected("10", COMPARE, comparison.SCHEMA)])
        for row in rows:
            golive_evidence.validate(row)
        without = {row["stage"]: row for row in golive_ledger.build([], [], NOW)}
        with_rows = {row["stage"]: row for row in golive_ledger.build([], rows, NOW)}
        for stage in ("9", "10"):
            self.assertEqual(without[stage]["live"], {"status": "missing", "reason": "AGGREGATE_ONLY"})
            self.assertEqual((with_rows[stage]["live"]["status"], with_rows[stage]["state"]), ("evidence", "LIVE"))

    def test_c1_cli_exit_codes_and_output(self):
        """C1: exit 0 only when both pass (counts only in text); one failing stage prints both rows and exits 2."""
        count = len(self.doc(COMPARE)["rows"])
        self.assertEqual(self.cli(), (0, f"9\tlive\tpass\t-\tcount=2\n10\tlive\tpass\t-\tcount={count}\n", ""))
        status, out, err = self.cli("--json")
        self.assertEqual((status, json.loads(out), err), (0, self.records(), ""))
        self.put(COMPARE, b"")
        status, out, _ = self.cli("--json")
        self.assertEqual((status, [row["result"] for row in json.loads(out)]), (2, ["pass", "fail"]))

    def test_c1_cli_bad_input_has_stable_code_no_traceback(self):
        """C1: unbindable input, and a report name the evidence schema refuses, print one code and exit 2."""
        self.report["mode"] = "test"
        self.write_report()
        self.assertEqual(self.cli(), (2, "", "OUTPUTS_RUN_UNBOUND\n"))
        self.report["mode"] = "scheduled"
        self.report_path = self.report_path.rename(self.base / "bad\\name.json")
        self.write_report()
        (self.out / FACTS).unlink()  # a fail row with no file bytes is bound to the report name
        self.assertEqual(self.cli(), (2, "", "FACTS_EVIDENCE_BAD_INPUT\n"))

    def test_c2_folder_name_must_be_the_run_id(self):
        """C2: a valid folder under another run's name, or another run's report, is refused (bug: trust one side)."""
        renamed = self.out.rename(self.out.with_name(OTHER_RUN))
        self.out = renamed
        self.assert_unbound()
        self.out = renamed.rename(renamed.with_name(RUN_ID))
        self.report = report_for(NOW - timedelta(minutes=30), seq=7)
        self.write_report()
        self.assert_unbound()

    def test_c2_folder_must_be_owner_only_non_symlink_directory(self):
        """C2: group/other access, a symlink, a plain file or nothing in the folder's place is refused."""
        os.chmod(self.out, 0o750)
        self.assert_unbound()
        os.chmod(self.out, 0o700)
        moved = self.out.rename(self.base / "real")
        self.out.symlink_to(moved, target_is_directory=True)
        self.assert_unbound()
        self.out.unlink()
        self.out.write_text("{}")
        self.assert_unbound()
        self.out.unlink()
        self.assert_unbound()

    def test_c2_symlink_ancestor_hidden_by_dotdot_is_refused(self):
        """C2: `link/../<run id>` is judged as supplied, not after collapsing (bug: abspath erased the link)."""
        link = self.base / "link"
        link.symlink_to(self.base / "copied", target_is_directory=True)
        self.out = link / ".." / "copied" / RUN_ID
        self.assert_unbound()
        # A symlinked ancestor on a plain path is refused too.
        self.out = link / RUN_ID
        self.assert_unbound()

    def test_c2_run_report_must_be_valid_finished_non_stub(self):
        """C2: test-mode, stub, failed-outputs, future and invalid reports, and bad report files, bind nothing."""
        stub = [{"code": "STUB_STAGE_BACKEND", "stage": "outputs", "severity": "info", "count": 1}]
        cases = {"test mode": lambda r: r.update(mode="test"), "stub": lambda r: r.update(needs_g=stub),
                 "invalid": lambda r: r.update(overall_status="bogus"),
                 "future": lambda r: r.update(finished_at="2026-11-25T00:00:00Z"),
                 "outputs failed": lambda r: r["stages"][-2].update(status="fail", error_code="X")}
        for label, mutate in cases.items():
            with self.subTest(label):
                self.report = json.loads(json.dumps(REPORT))
                mutate(self.report)
                self.write_report()
                self.assert_unbound()
        for label, raw in (("duplicate key", '{"run_id": "a", "run_id": "b"}'), ("not json", "{")):
            with self.subTest(label):
                self.report_path.write_text(raw)
                self.assert_unbound()
        self.report_path.unlink()
        self.assert_unbound()

    def test_c3_empty_writer_output_never_passes(self):
        """C3: real writers on an agency-less ledger leave valid files; neither is positive (bug: empty list = live)."""
        shutil.rmtree(self.out)
        self.out = self.copy_outputs(self.empty)
        self.assertEqual(self.doc(FACTS)["agencies"], [])
        self.assertEqual(self.results(), {"9": ("fail", ["FACTS_FILE_EMPTY"]), "10": ("fail", ["COMPARISON_FILE_EMPTY"])})

    def test_c3_each_refusal_fails_only_its_own_stage(self):
        """C3: missing, empty, duplicate-key, wrong-run, wrong-schema and malformed files give the stable codes."""
        for name, prefix, stage, key, ident in ((FACTS, "FACTS", "9", "agencies", "agency_id"),
                                                (COMPARE, "COMPARISON", "10", "rows", "agency_id")):
            good = (self.out / name).read_bytes()
            other = "10" if stage == "9" else "9"
            for label, value in variants(self.doc(name), key, ident).items():
                with self.subTest(label, file=name):
                    if value is None:
                        (self.out / name).unlink()
                    else:
                        self.put(name, value)
                    code = "EMPTY" if label in ("empty", "blank", "empty list") else "MISSING" if value is None else "INVALID"
                    results = self.results()
                    self.assertEqual(results[stage], ("fail", [f"{prefix}_FILE_{code}"]))
                    self.assertEqual(results[other][0], "pass")
                    self.put(name, good)

    def test_c3_wrong_run_files_in_the_right_folder_fail(self):
        """C3: genuine files from ANOTHER run in this run's folder fail (bug: schema and size checked, not run id)."""
        for name in (FACTS, COMPARE):
            self.put(name, dict(self.doc(name), run_id=OTHER_RUN))
        self.assertEqual(self.results(), {"9": ("fail", ["FACTS_FILE_INVALID"]),
                                          "10": ("fail", ["COMPARISON_FILE_INVALID"])})

    def test_c4_source_is_the_file_bytes_and_text_never_echoed(self):
        """C4: the digest follows the file bytes; no agency name or id is echoed (bug: file text in codes/CLI)."""
        before = self.records()[0]["source"]["sha256"]
        self.put(FACTS, dict(self.doc(FACTS), run_id=OTHER_RUN))  # fails, and is full of agency text
        row = self.records()[0]
        self.assertNotEqual(row["source"]["sha256"], before)
        self.assertEqual(row["source"]["sha256"], hashlib.sha256((self.out / FACTS).read_bytes()).hexdigest())
        for text in (*self.cli()[1:], *self.cli("--json")[1:], json.dumps(self.records())):
            for agency_id, name in NAMES.items():
                self.assertNotIn(name, text)
                self.assertNotIn(agency_id, text)

    def test_c5_read_only_in_pass_and_refusal(self):
        """C5: nothing in the folder or the report changes (bug: an adapter that repairs or rewrites the output)."""
        def snapshot():
            return {p.name: (p.stat().st_mtime_ns, p.stat().st_mode, p.read_bytes())
                    for p in (*self.out.iterdir(), self.report_path)}
        for broken in (False, True):
            if broken:
                self.put(COMPARE, b"{")
            before = snapshot()
            self.records()
            self.assertEqual(snapshot(), before)

    def test_c5_oversized_file_is_refused_unread(self):
        """C5: a file over the cap is invalid without one read. Bug: reading it all, then checking the size."""
        self.put(FACTS, b"x" * 200)
        fd = os.open(self.out, os.O_RDONLY | os.O_DIRECTORY)
        self.addCleanup(os.close, fd)
        with patch.object(adapter.os, "read", side_effect=AssertionError("read")):
            self.assertEqual(adapter._file(FACTS, 100, fd), (None, "invalid"))
        with patch.object(adapter, "MAX_FILE_BYTES", 100):
            row = self.records()[0]
        self.assertEqual((row["result"], row["codes"], row["source"]["schema"]),
                         ("fail", ["FACTS_FILE_INVALID"], "records-run-report-v1"))

    def test_c5_symlinked_file_is_not_followed(self):
        """C5: a symlink in place of a file is invalid, not read through (bug: following it out of the folder)."""
        (self.out / COMPARE).unlink()
        (self.out / COMPARE).symlink_to(self.out / FACTS)
        self.assertEqual(self.results()["10"], ("fail", ["COMPARISON_FILE_INVALID"]))


if __name__ == "__main__":
    unittest.main()
