"""--evidence-row on the stage 13 (reply draft) and stage 14 (follow-up draft) joins.

The fixtures are the joins' own synthetic exports. Each case runs the join with --evidence-row, then records the
printed array with ``golive_record live`` into a private evidence folder, so the row is checked end to end: a
pass row only when the join passed, a fail row carrying the join's code otherwise, the source bound to the exact
manifest the join cites, and no row at all for an unverified export.
"""
import contextlib
import io
import json
import stat
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from campaign_tool.records import console_export as ce, golive_evidence, golive_record
from tests.records import test_console_snapshot as tcs
from tests.records.test_golive_draft_join import Join as DraftJoin
from tests.records.test_golive_followup_join import Join as FollowupJoin

SENTINELS = ("SENTINEL-BODY-TEXT", "SENTINEL-FOLDER", "sentinel-id@example.invalid")


class _RowCases:
    STAGE = PASS_KEY = PASS_REV = FAIL_KEY = FAIL_CODE = None

    def setUp(self):
        super().setUp()
        self.evidence = self.tmp / "golive-evidence"
        self.evidence.mkdir(mode=0o700)

    def row_run(self, name, key, revision, *extra):
        code, text = self.run_cli(name, key, revision, "--evidence-row", *extra)
        for sentinel in SENTINELS:
            self.assertNotIn(sentinel, text)
        return code, text

    def set_time(self, dest, delta):
        manifest = json.loads((dest / ce.MANIFEST).read_text())
        manifest["exported_at"] = (datetime.now(timezone.utc) + delta).strftime("%Y-%m-%dT%H:%M:%SZ")
        (dest / ce.MANIFEST).write_text(json.dumps(manifest))

    def record(self, text):
        path = self.tmp / "row.json"
        path.write_text(text, encoding="utf-8")
        err = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            status = golive_record.main(["live", str(path), "--evidence", str(self.evidence)])
        return status, err.getvalue()

    def check_row(self, text, name, result, codes):
        rows = json.loads(text)
        self.assertIsInstance(rows, list)
        self.assertEqual(len(rows), 1)
        row = rows[0]
        golive_evidence.validate(row)
        self.assertEqual({k: row[k] for k in ("stage", "kind", "result", "codes", "engine", "actor")},
                         {"stage": self.STAGE, "kind": "live", "result": result, "codes": codes, "engine": None,
                          "actor": "pipeline"})
        self.assertEqual(row["source"], {"schema": ce.SCHEMA, "sha256": self.manifest_digest(name), "name": ce.MANIFEST})
        return row

    def test_pass_row_is_recorded_as_live_evidence_bound_to_the_manifest(self):
        # Bug: no row for a pass, a row for another stage, or a source that is not the cited manifest.
        name = self.make_export()
        code, text = self.row_run(name, self.PASS_KEY, self.PASS_REV)
        self.assertEqual(code, 0, text)
        row = self.check_row(text, name, "pass", [])
        self.assertEqual(self.record(text), (0, ""))
        files = list(self.evidence.iterdir())
        self.assertEqual([p.name for p in files], [golive_evidence._filename(row)])
        self.assertEqual(stat.S_IMODE(files[0].stat().st_mode), 0o600)
        self.assertEqual(golive_evidence.load(self.evidence), [row])

    def test_failed_join_gives_a_fail_row_with_its_code(self):
        # Bug: a join that did not pass yields a pass row, or a fail row without the join's own code.
        name = self.make_export()
        code, text = self.row_run(name, self.FAIL_KEY, 0)
        self.assertEqual(code, 1, text)
        self.check_row(text, name, "fail", [self.FAIL_CODE])
        self.assertEqual(self.record(text), (1, ""))
        self.assertEqual([r["result"] for r in golive_evidence.load(self.evidence)], ["fail"])

    def test_stale_export_gives_a_fail_row(self):
        # Bug: an export older than the age limit is still recorded as a pass.
        name = self.make_export(mutate=lambda dest: self.set_time(dest, timedelta(hours=-37)))
        code, text = self.row_run(name, self.PASS_KEY, self.PASS_REV)
        self.assertEqual(code, 1, text)
        self.check_row(text, name, "fail", ["EXPORT_STALE"])

    def test_unverified_export_prints_no_row_and_records_nothing(self):
        # Bug: an export that cannot be verified still yields an evidence row (with no manifest to cite).
        self.make_export()
        code, text = self.row_run("20260101T000000Z-" + tcs.RUN2, self.PASS_KEY, self.PASS_REV)
        self.assertEqual((code, text.split()[0]), (2, "EXPORT_UNVERIFIED"), text)
        status, err = self.record(text)
        self.assertEqual(status, 2, err)
        self.assertEqual(list(self.evidence.iterdir()), [])

    def test_evidence_row_and_json_are_exclusive(self):
        # Bug: two output formats mixed on one stdout, so the saved file is neither.
        name = self.make_export()
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as caught:
            self.run_cli(name, self.PASS_KEY, self.PASS_REV, "--json", "--evidence-row")
        self.assertEqual(caught.exception.code, 2)

    def test_plain_output_unchanged_without_the_flag(self):
        # Bug: the new flag changes what the join prints when it is not given.
        name = self.make_export()
        code, text = self.run_cli(name, self.PASS_KEY, self.PASS_REV)
        self.assertEqual(code, 0)
        self.assertTrue(text.startswith("PASS "), text)

    def earlier(self, text, minutes=1):
        """The same row as if the join had run ``minutes`` earlier (still a valid, past-dated row)."""
        row, = json.loads(text)
        stamp = datetime.fromisoformat(row["recorded_at"].replace("Z", "+00:00")) - timedelta(minutes=minutes)
        return json.dumps([dict(row, recorded_at=stamp.strftime("%Y-%m-%dT%H:%M:%SZ"))], sort_keys=True)

    def test_corrected_join_on_the_same_export_records_its_pass_after_the_fail(self):
        # Bug (RETURN 1): a fail row for the wrong id/revision blocks the correct pair's pass on the same export.
        name = self.make_export()
        _, wrong = self.row_run(name, self.FAIL_KEY, 0)
        code, right = self.row_run(name, self.PASS_KEY, self.PASS_REV)
        self.assertEqual(code, 0, right)
        self.assertEqual(self.record(self.earlier(wrong)), (1, ""))
        self.assertEqual(self.record(right), (0, ""))
        records = golive_evidence.load(self.evidence)
        # Failure history kept, both rows bound to the same manifest, the newest row is the pass.
        self.assertEqual([r["result"] for r in records], ["fail", "pass"])
        self.assertEqual({r["source"]["sha256"] for r in records}, {self.manifest_digest(name)})
        self.assertEqual(golive_evidence.latest(records, self.STAGE, "live")["result"], "pass")

    def test_after_the_pass_nothing_more_is_recorded_for_that_export(self):
        # Bug: the recovery path turns into replay: a second pass, a later fail or a second fail is accepted.
        name = self.make_export()
        _, wrong = self.row_run(name, self.FAIL_KEY, 0)
        _, right = self.row_run(name, self.PASS_KEY, self.PASS_REV)
        self.assertEqual(self.record(self.earlier(wrong, 3)), (1, ""))
        self.assertEqual(self.record(self.earlier(wrong, 2)), (2, "golive_record: EVIDENCE_DUPLICATE\n"))
        self.assertEqual(self.record(self.earlier(right, 1)), (0, ""))
        # Fresh runs (new timestamps, so new filenames): refused by identity, not by a filename clash.
        _, again_fail = self.row_run(name, self.FAIL_KEY, 0)
        _, again_pass = self.row_run(name, self.PASS_KEY, self.PASS_REV)
        for text in (again_pass, again_fail):
            self.assertEqual(self.record(text), (2, "golive_record: EVIDENCE_DUPLICATE\n"))
        self.assertEqual([r["result"] for r in golive_evidence.load(self.evidence)], ["fail", "pass"])

    def test_pass_not_newer_than_the_recorded_fail_is_refused(self):
        # Bug: a pass taken before the recorded fail is written, so the newest row would still be the fail.
        name = self.make_export()
        _, right = self.row_run(name, self.PASS_KEY, self.PASS_REV)
        _, wrong = self.row_run(name, self.FAIL_KEY, 0)
        self.assertEqual(self.record(wrong), (1, ""))
        self.assertEqual(self.record(self.earlier(right)), (2, "golive_record: EVIDENCE_DUPLICATE\n"))
        self.assertEqual([r["result"] for r in golive_evidence.load(self.evidence)], ["fail"])

    def test_evidence_row_refuses_an_age_limit_above_policy(self):
        # Bug (RETURN 2): --max-age-hours inf (or any wider limit) turns a 90-day-old export into a live pass.
        name = self.make_export(mutate=lambda dest: self.set_time(dest, timedelta(days=-90)))
        for limit in ("inf", "2160", "37"):
            with self.subTest(limit), contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as caught:
                self.row_run(name, self.PASS_KEY, self.PASS_REV, "--max-age-hours", limit)
            self.assertEqual(caught.exception.code, 2)
        code, text = self.row_run(name, self.PASS_KEY, self.PASS_REV)
        self.assertEqual(code, 1, text)
        self.check_row(text, name, "fail", ["EXPORT_STALE"])

    def test_a_tighter_age_limit_is_kept_for_the_row(self):
        # Bug: the policy cap replaces a stricter operator limit.
        name = self.make_export(mutate=lambda dest: self.set_time(dest, timedelta(hours=-2)))
        code, text = self.row_run(name, self.PASS_KEY, self.PASS_REV, "--max-age-hours", "1")
        self.assertEqual(code, 1, text)
        self.check_row(text, name, "fail", ["EXPORT_STALE"])


class RecoveryIsLiveOnly(unittest.TestCase):
    def test_rehearsal_and_gate_keep_one_record_per_source(self):
        # Bug: the live recovery path also lets a rehearsal or gate pass follow a fail on the same receipt.
        evidence = Path(self.enterContext(tempfile.TemporaryDirectory(dir=Path.cwd())))
        for kind, actor in (("rehearsal", "rehearsal"), ("gate", "operator")):
            base = dict(schema="records-golive-evidence-v1", stage="9", kind=kind, engine=None, actor=actor,
                        source=dict(schema="x", sha256="a" * 64, name="r.json"))
            golive_record.append(dict(base, result="fail", codes=["X"], recorded_at="2026-01-05T20:30:00Z"), evidence)
            with self.subTest(kind), self.assertRaises(golive_record.EvidenceDuplicate):
                golive_record.append(dict(base, result="pass", codes=[], recorded_at="2026-01-05T20:31:00Z"), evidence)
        self.assertEqual([r["result"] for r in golive_evidence.load(evidence)], ["fail", "fail"])


class DraftJoinEvidenceRow(_RowCases, DraftJoin):
    STAGE, PASS_KEY, PASS_REV, FAIL_KEY, FAIL_CODE = "13", "ok", 1, "first", "REPLY_NOT_REPLY_KIND"

    def test_legacy_diagnostic_output_keeps_any_age_limit(self):
        # Bug: the evidence-row cap also changes the join's plain diagnostic output.
        name = self.make_export(mutate=lambda dest: self.set_time(dest, timedelta(days=-90)))
        code, text = self.run_cli(name, self.PASS_KEY, self.PASS_REV, "--max-age-hours", "inf")
        self.assertEqual(code, 0, text)
        self.assertTrue(text.startswith("PASS "), text)


class FollowupJoinEvidenceRow(_RowCases, FollowupJoin):
    STAGE, PASS_KEY, PASS_REV, FAIL_KEY, FAIL_CODE = "14", "reminder", 0, "first", "FOLLOWUP_NOT_FOLLOWUP_KIND"
