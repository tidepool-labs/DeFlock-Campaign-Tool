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
from datetime import datetime, timedelta, timezone

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


class DraftJoinEvidenceRow(_RowCases, DraftJoin):
    STAGE, PASS_KEY, PASS_REV, FAIL_KEY, FAIL_CODE = "13", "ok", 1, "first", "REPLY_NOT_REPLY_KIND"


class FollowupJoinEvidenceRow(_RowCases, FollowupJoin):
    STAGE, PASS_KEY, PASS_REV, FAIL_KEY, FAIL_CODE = "14", "reminder", 0, "first", "FOLLOWUP_NOT_FOLLOWUP_KIND"
