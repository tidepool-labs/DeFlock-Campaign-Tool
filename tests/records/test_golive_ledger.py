"""Offline acceptance for the ledger, using the shipped synthetic report."""

import io
import json
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from campaign_tool.records import golive_catalog, golive_evidence, golive_ledger, golive_stages


FIXTURE = Path("examples/run-report/records-run-report-v1.synthetic.json")
RUN = "run_20260101T060000Z_0a1b2c3d"
NOW = datetime(2026, 1, 16, tzinfo=timezone.utc)
STAMP = "2026-01-02T00:00:00Z"
LATER = "2026-01-03T00:00:00Z"
ENGINE = "a" * 40
MISSING = dict(status="missing", recorded_at=None)
UNNEEDED = dict(status="not needed", recorded_at=None)
PASS = dict(status="pass", recorded_at=STAMP)
FAIL = dict(status="fail", recorded_at=LATER)
NO_LIVE = dict(status="missing")
RECEIPT = dict(status="evidence", recorded_at=STAMP)
REPORT = dict(status="report", run_id=RUN)
AGGREGATE = dict(status="missing", reason="AGGREGATE_ONLY")
NOT_POSITIVE = dict(status="missing", reason="NOT_POSITIVE")


def record(stage, kind, result="pass", stamp=STAMP, engine=ENGINE, *, source_schema="synthetic-v1"):
    return dict(schema="records-golive-evidence-v1", stage=stage, kind=kind, result=result,
                recorded_at=stamp, engine=engine, actor="operator", codes=[],
                source=dict(schema=source_schema, sha256="c" * 64, name="synthetic.json"))


def row(stage, label, *, pipeline=True, rehearsed=MISSING, gate=UNNEEDED,
        live=NO_LIVE, state="BUILT", next_kind: str | None = "rehearsal"):
    return dict(stage=stage, label=label, pipeline=pipeline, rehearsed=rehearsed,
                gate=gate, live=live, state=state, next=next_kind)


def set_stage(report, name, status="ok", *, count=0, output=0, failed=0, skipped=0, code=None):
    stage = next(stage for stage in report["stages"] if stage["name"] == name)
    stage.update(status=status, counts=dict(input=count, output=output, failed=failed, skipped=skipped),
                 error_code=code, duration_seconds=0 if status == "skipped" else 1)
    if status == "warn":
        report["overall_status"] = "warn"


def report():
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    data.update(mode="scheduled", overall_status="ok", needs_g=[], review_required_count=0)
    data["new_data"] = dict.fromkeys(data["new_data"], 0)
    for stage in data["stages"]:
        set_stage(data, stage["name"], "skipped")
    return data


class GoLiveLedgerTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        root = Path(folder.name).resolve()
        self.reports = root / "reports"
        self.evidence = root / "golive-evidence"
        self.reports.mkdir()
        self.evidence.mkdir()
        self.args = ["--reports", str(self.reports), "--evidence", str(self.evidence)]

    def write_report(self, data):
        (self.reports / f"{data['run_id']}.json").write_text(json.dumps(data), encoding="utf-8")

    def write_record(self, data):
        stamp = data["recorded_at"].replace("-", "").replace(":", "")
        name = f"{stamp}-{data['stage']}-{data['kind']}-{data['source']['sha256'][:8]}.json"
        path = self.evidence / name
        path.touch(mode=0o600)
        path.write_text(json.dumps(data), encoding="utf-8")

    def ledger(self, reports=(), evidence=(), now=NOW):
        return {r["stage"]: r for r in golive_ledger.build(reports, evidence, now)}

    def loaded(self):
        return self.ledger(golive_stages.load_reports(self.reports, now=NOW), golive_evidence.load(self.evidence))

    def invoke(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = golive_ledger.main(list(args) or self.args)
        return code, out.getvalue(), err.getvalue()

    def test_c1_states_precedence_and_next(self):
        """C1: Preserve distinct milestones, failed checks and the first unmet requirement."""
        # Treating any receipt as a pass or ignoring catalogue flags collapses these states.
        records = [record("2", "rehearsal"), record("3", "rehearsal"), record("3", "gate"),
                   record("4", "rehearsal"), record("4", "gate", "fail", LATER),
                   record("1", "live"), record("1", "rehearsal", "fail", LATER),
                   record("1", "gate", "fail", LATER), record("18", "gate"),
                   record("19", "rehearsal", "fail", LATER), record("19", "gate"),
                   record("20", "rehearsal"), record("20", "live"), record("20", "gate", "fail", LATER)]
        rows = self.ledger(evidence=records)
        self.assertEqual(list(rows), list(golive_catalog.STAGES))
        self.assertEqual([(r["label"], r["pipeline"]) for r in rows.values()],
                         [(rule.label, rule.pipeline) for rule in golive_catalog.STAGES.values()])
        expected = {
            "1": row("1", "Request sent", rehearsed=UNNEEDED, live=RECEIPT, state="LIVE", next_kind=None),
            "2": row("2", "Email intake / sync", rehearsed=PASS, state="REHEARSED", next_kind="live"),
            "3": row("3", "Arrival trigger", rehearsed=PASS, gate=PASS, state="GATED", next_kind="live"),
            "4": row("4", "Portal downloads", rehearsed=PASS, gate=FAIL, state="BLOCKED", next_kind="gate"),
            "17b": row("17b", "Public site", rehearsed=UNNEEDED, next_kind="live"),
            "18": row("18", "Timers + monitoring", gate=PASS, state="GATED"),
            "19": row("19", "Backups", rehearsed=FAIL, gate=PASS, state="BLOCKED"),
            "20": row("20", "Approval trust anchor", rehearsed=PASS, gate=FAIL, live=RECEIPT,
                      state="LIVE", next_kind="gate"),
            "A1": row("A1", "Console on the box", pipeline=False),
        }
        self.assertEqual({stage: rows[stage] for stage in expected}, expected)

    def test_c1_latest_live_failure_blocks_report_fallback(self):
        """C1: A later live failure cannot be hidden by an old receipt or report."""
        data = report()
        set_stage(data, "sync")
        self.write_report(data)
        self.write_record(record("2", "live"))
        self.assertEqual(self.loaded()["2"], row("2", "Email intake / sync", live=RECEIPT, state="LIVE"))
        self.write_record(record("2", "live", "fail", LATER))
        self.assertEqual(self.loaded()["2"], row("2", "Email intake / sync", state="BLOCKED",
                                                live=dict(status="missing", reason="LIVE_FAILED", recorded_at=LATER)))

    def test_c2_exact_freshness_cutoff_and_gate_does_not_age(self):
        """C2: Rehearsals expire strictly after day 14; gates do not expire by age."""
        # A date-only calculation misses the one-second boundary; aging gates hides GO evidence.
        old = "2026-01-01T23:59:59Z"
        rows = self.ledger(evidence=[record("3", "rehearsal"), record("4", "rehearsal", stamp=old),
                                     record("4", "gate", stamp=old)], now=NOW.replace(tzinfo=None))
        self.assertEqual(rows["3"], row("3", "Arrival trigger", rehearsed=PASS, gate=MISSING,
                                       state="REHEARSED", next_kind="gate"))
        self.assertEqual(rows["4"], row("4", "Portal downloads", gate=dict(status="pass", recorded_at=old),
                                       rehearsed=dict(status="missing", recorded_at=old, reason="STALE"), state="GATED"))

    def test_c2_newest_live_engine_invalidates_other_rows(self):
        """C2: Compare both columns to the newest live engine, not the first or the same stage only."""
        records = [record("3", "rehearsal"), record("3", "gate"), record("1", "live"),
                   record("A2", "live", stamp=LATER, engine="b" * 40)]
        changed = dict(status="missing", recorded_at=STAMP, reason="ENGINE_CHANGED")
        self.assertEqual(self.ledger(evidence=reversed(records))["3"],
                         row("3", "Arrival trigger", rehearsed=changed, gate=changed))
        matching = [dict(r, engine="b" * 40) if r["stage"] == "3" else r for r in records]
        self.assertEqual(self.ledger(evidence=matching)["3"],
                         row("3", "Arrival trigger", rehearsed=PASS, gate=PASS, state="GATED", next_kind="live"))

    def test_c2_later_fail_supersedes_pass_independently(self):
        """C2: Later failures win in both columns regardless of input order."""
        records = [record("3", "rehearsal", "fail", LATER), record("3", "gate", "fail", LATER),
                   record("3", "rehearsal"), record("3", "gate")]
        self.assertEqual(self.ledger(evidence=records)["3"],
                         row("3", "Arrival trigger", rehearsed=FAIL, gate=FAIL, state="BLOCKED"))

    def test_c3_count_excludes_console_rows_in_both_output_formats(self):
        """C3: Count only LIVE pipeline rows, including A2 but not a live console-only row."""
        self.write_record(record("A1", "live"))
        self.write_record(record("A2", "live"))
        # A descriptive pass alone must not qualify. Selecting it before filtering
        # would also lose the older pass or mask the older failure.
        rehearsals = {
            "2": (MISSING, "BUILT", "rehearsal"),
            "5": (PASS, "REHEARSED", "live"),
            "8": (FAIL, "BLOCKED", "rehearsal"),
        }
        self.write_record(record("5", "rehearsal"))
        self.write_record(record("8", "rehearsal", "fail", LATER))
        for stage in rehearsals:
            self.write_record(record(stage, "rehearsal", stamp="2026-01-04T00:00:00Z",
                                     source_schema="records-e2e-proof-v1"))
        expected = list(self.loaded().values())
        self.assertEqual(golive_ledger.live_count(expected), 1)
        with patch.object(golive_ledger, "datetime", wraps=datetime) as clock:
            clock.now.return_value = NOW
            code, out, err = self.invoke(*self.args, "--json")
        self.assertEqual((code, err), (0, ""))
        payload = json.loads(out)
        for boundary, rows in (("build", expected), ("CLI JSON", payload["rows"])):
            by_stage = {item["stage"]: item for item in rows}
            for stage, want in rehearsals.items():
                with self.subTest(boundary=boundary, stage=stage):
                    item = by_stage[stage]
                    self.assertEqual((item["rehearsed"], item["state"], item["next"]), want)
        self.assertEqual(payload, dict(rows=expected, live_count=1, summary="1 of 22 pipeline stages live"))
        code, out, err = self.invoke()
        self.assertEqual((code, err, out.splitlines()[-1]), (0, "", "1 of 22 pipeline stages live"))
        self.assertIn(f"A1 | Console on the box | no | missing | not needed | evidence {STAMP} | LIVE | rehearsal",
                      out.splitlines())

    def test_c4_module_entrypoint_on_empty_archives(self):
        """C4: The actual module entrypoint renders a valid empty archive, without stderr."""
        run = subprocess.run([sys.executable, "-B", "-m", "campaign_tool.records.golive_ledger", *self.args],
                             capture_output=True, text=True, check=False)
        lines = run.stdout.splitlines()
        self.assertEqual((run.returncode, run.stderr, lines[0], lines[-1]),
                         (0, "", "Stage | Label | Pipeline | Rehearsed | Gate | Live | State | Next",
                          "0 of 22 pipeline stages live"))

    def test_c4_refuses_unreadable_and_invalid_inputs(self):
        """C4: Surface either loader's diagnostic and never print a partial ledger."""
        cases = (
            ("reports absent", ["--reports", str(self.reports / "absent"), *self.args[2:]],
             golive_stages.load_reports, f"{str(self.reports / 'absent')!r}: reports directory unreadable"),
            ("evidence absent", [*self.args[:2], "--evidence", str(self.evidence / "absent")],
             golive_evidence.load, f"{self.evidence / 'absent'}: unreadable evidence directory"),
            ("evidence invalid", self.args, golive_evidence.load, "bad.json: invalid evidence filename"),
            ("report invalid", self.args, golive_stages.load_reports, "'bad.json': invalid report"),
        )
        for name, args, loader, error in cases:
            with self.subTest(name=name):
                if name == "evidence invalid":
                    (self.evidence / "bad.json").write_text("{}", encoding="utf-8")
                if name == "report invalid":
                    (self.reports / "bad.json").write_text("{}", encoding="utf-8")
                directory = args[1] if loader is golive_stages.load_reports else args[3]
                with self.assertRaises(ValueError) as raised:
                    loader(directory)
                self.assertIs(type(raised.exception), ValueError)
                self.assertEqual(str(raised.exception), error)
                self.assertEqual(self.invoke(*args, "--json"), (2, "", f"golive_ledger: {error}\n"))

    def test_c5_aggregate_only_and_exact_live_ids(self):
        """C5: An umbrella outputs receipt cannot prove any of its six rows."""
        # A bare last_proven_run consumer would add six false positives here.
        data = report()
        set_stage(data, "outputs")
        self.write_report(data)
        rows = self.loaded()
        labels = {"9": "Facts", "10": "Policy comparison", "11": "Gap proposals",
                  "12": "Received items, timeline, register", "13": "Reply drafts to approval queue",
                  "14": "No-response follow-up drafts"}
        self.assertEqual({stage: rows[stage] for stage in labels},
                         {stage: row(stage, label, live=AGGREGATE) for stage, label in labels.items()})
        self.assertEqual({r["stage"] for r in rows.values() if r["state"] == "LIVE"}, set())
        set_stage(data, "sync", count=1, output=1)
        set_stage(data, "extract", count=1, output=1)
        data["ocr"] = dict(pages_ocr=40, pages_conf_ge80=40, pages_conf_lt80=0, pages_conf_unknown=0,
                           pages_reviewed=0, awaiting_ocr_images=0)
        self.write_report(data)
        self.assertEqual(self.loaded()["6"], row("6", "OCR"))
        for stage in ("1", "6", "9", "A2"):
            self.write_record(record(stage, "live"))
        rows = self.loaded()
        baseline = {"1", "2", "5", "6", "9", "A2"}
        self.assertEqual({r["stage"] for r in rows.values() if r["state"] == "LIVE"}, baseline)
        self.assertEqual(rows["2"], row("2", "Email intake / sync", live=REPORT, state="LIVE"))
        self.assertEqual(rows["5"], row("5", "Save + extract text", live=REPORT, state="LIVE"))
        self.assertEqual(rows["6"], row("6", "OCR", live=RECEIPT, state="LIVE"))
        self.write_record(record("13", "live"))
        rows = self.loaded()
        self.assertEqual({r["stage"] for r in rows.values() if r["state"] == "LIVE"}, baseline | {"13"})
        self.assertEqual(rows["13"], row("13", labels["13"], live=RECEIPT, state="LIVE"))
        self.assertEqual(rows["14"], row("14", labels["14"], live=AGGREGATE))

    def test_c5_idle_skipped_partial_and_snapshot_rows_are_not_live(self):
        """C5: Reject skipped, idle and partial adoption/gates, including a nonzero historical snapshot."""
        for case in ("skipped", "idle", "partial", "snapshot", "pending"):
            with self.subTest(case=case):
                data = report()
                if case == "idle":
                    set_stage(data, "adoption")
                    set_stage(data, "digest_gates")
                elif case == "partial":
                    set_stage(data, "adoption", "warn", count=2, output=1, skipped=1, code="ADOPTION_CODED_GAPS")
                    set_stage(data, "digest_gates", "warn", count=2, output=1, failed=1, code="GATE_REVIEW_REQUIRED")
                elif case == "snapshot":
                    set_stage(data, "digest_gates", count=20, output=20)
                elif case == "pending":
                    set_stage(data, "adoption", count=2, output=1, skipped=1)
                    set_stage(data, "digest_gates", count=2, output=1, skipped=1)
                self.write_report(data)
                rows = self.loaded()
                self.assertEqual((rows["7"], rows["8"]),
                                 (row("7", "Catalogue / adoption", live=NOT_POSITIVE),
                                  row("8", "Digest gates", live=NOT_POSITIVE)))
        self.write_record(record("8", "live"))
        self.assertEqual(self.loaded()["8"], row("8", "Digest gates", live=RECEIPT, state="LIVE"))

    def test_c5_older_positive_adoption_survives_newer_idle_and_test_runs(self):
        """C5: Keep the old positive run ID, never a newer idle or test proof."""
        data = report()
        set_stage(data, "adoption", count=1, output=1)
        self.write_report(data)
        for hour, mode in ((7, "scheduled"), (8, "test")):
            data.update(mode=mode, run_id=f"run_20260101T{hour:02}0000Z_0a1b2c3d",
                        started_at=f"2026-01-01T{hour:02}:00:00Z", finished_at=f"2026-01-01T{hour:02}:07:30Z")
            set_stage(data, "adoption", count=0 if hour == 7 else 1, output=0 if hour == 7 else 1)
            self.write_report(data)
        self.assertEqual(self.loaded()["7"], row("7", "Catalogue / adoption", live=REPORT, state="LIVE"))
