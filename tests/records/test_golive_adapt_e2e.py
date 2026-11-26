"""Runtime contracts for the read-only e2e evidence adapter."""

from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from campaign_tool.records import golive_adapt_e2e, golive_evidence

NOW = "2026-11-22T09:15:23Z"
ENGINE = "a0" * 20


def stage(name, output=2, failed=0, skipped=0, status="ok", code=None):
    return {"stage": name, "status": status, "code": code, "seconds": 0.01, "outputs": [],
            "detail": {"counts": {"input": output + failed + skipped, "output": output,
                                  "failed": failed, "skipped": skipped}, "outcomes": []}}


class AdapterTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.replay = self.root / "replay"
        self.project = self.replay / "project"
        self.project.mkdir(parents=True)
        self.path = self.replay / "proof-report.json"
        self.marker = self.project / ".t12-sandbox-v1"
        self.marker.write_bytes(b"e2e-proof sandbox\n")
        self.marker.chmod(0o600)

    def report(self, *timeline, **fields):
        raw = (json.dumps({"schema": "records-e2e-proof-v1", "result": "PASS", "stages": timeline,
                           "manual_steps": [], "sandbox": str(self.replay), "sent": False,
                           "published": False, **fields}, indent=2) + "\n").encode()
        self.path.write_bytes(raw)
        self.sha256 = hashlib.sha256(raw).hexdigest()

    def row(self, number, result="pass", codes=(), engine=None):
        return {"schema": "records-golive-evidence-v1", "stage": number, "kind": "rehearsal",
                "result": result, "actor": "rehearsal", "recorded_at": NOW, "engine": engine,
                "source": {"schema": "records-e2e-proof-v1", "sha256": self.sha256,
                           "name": "proof-report.json"}, "codes": list(codes)}

    def assert_rows(self, expected, now: str | datetime = NOW):
        rows = golive_adapt_e2e.records(self.path, now)
        self.assertEqual(rows, expected)
        for row in rows:
            golive_evidence.validate(row)

    def assert_refused(self, now: str | datetime = NOW):
        with self.assertRaises(ValueError) as raised:
            golive_adapt_e2e.records(self.path, now)
        self.assertIs(type(raised.exception), ValueError)

    def test_c1_positive_rows_bind_original_bytes_and_run_identity(self):
        """C1: map explicit work; don't hash reserialized JSON or use the reader's engine."""
        self.report(stage("digest_gates", status="warn", code="GATE_REVIEW_REQUIRED"),
                    stage("extract"), stage("sync"), engine=ENGINE, sandbox="/ignored")
        self.assert_rows([self.row(n, engine=ENGINE) for n in ("2", "5", "8")],
                         now=datetime.fromisoformat("2026-11-22T01:15:23.999999-08:00"))

    def test_c1_default_replay_only_extract(self):
        """C1: legacy seeded replay doesn't turn adoption, pending work or paths into passes."""
        artifact = self.project / "comparison.json"
        artifact.write_text("{}", encoding="utf-8")
        self.report(stage("arrival_trigger"), stage("adoption_setup"), stage("delta"), stage("adoption"),
                    stage("extract"), stage("digest_gates", output=0, skipped=10),
                    dict(stage("outputs"), outputs=["project/comparison.json"]),
                    stage("followups"), stage("next_steps"), with_adoption=True)
        self.assert_rows([self.row("5")])

    def test_c1_omits_aggregate_output_signals(self):
        """C1: no generic PASS, aggregate code, outcome list or artifact name proves 9–14."""
        (self.project / "drafts.json").write_text("{}", encoding="utf-8")
        for label, timeline in (
            ("generic PASS alone", []),
            ("aggregate ok and empty outcomes", [stage("outputs")]),
            ("existing artifact names", [dict(stage("outputs"), outputs=["project/drafts.json"])]),
            ("generic code", [stage("outputs", code="PASS")]),
            ("unrecognized positive-looking outcomes", [{**stage("outputs"),
              "detail": {"counts": {"output": 4}, "outcomes": [{"code": "OK", "count": 4}]}}]),
            ("aggregate failure also cannot identify individual stages", [stage("outputs", status="fail")]),
        ):
            with self.subTest(label=label):
                self.report(*timeline)
                self.assert_rows([])

    def test_c1_sync_requires_explicit_successful_work(self):
        """C1: skipped sync, zero work, warnings and offline snapshots aren't intake proof."""
        for entry in (stage("sync", status="not_run"), stage("sync", status="skipped"),
                      stage("sync", output=0), stage("sync", code="SYNC_OFFLINE_SNAPSHOT"),
                      stage("sync", status="warn"), stage("sync", failed=1)):
            with self.subTest(entry=entry):
                self.report(entry)
                self.assert_rows([])

    def test_c1_counts_must_show_completed_extract_and_digest(self):
        """C1: reject empty, partial, blocked, pending and non-integer counter evidence."""
        for name in ("extract", "digest_gates"):
            for label, counts in (
                ("empty", {"output": 0, "failed": 0, "skipped": 0}),
                ("failed/blocked", {"output": 2, "failed": 1, "skipped": 0}),
                ("skipped/pending", {"output": 2, "failed": 0, "skipped": 1}),
                ("unknown skipped/pending", {"output": 2, "failed": 0}),
                ("bool output", {"output": True, "failed": 0, "skipped": 0}),
                ("missing counts", None),
            ):
                with self.subTest(name=name, label=label):
                    self.report(dict(stage(name), detail={"counts": counts}))
                    self.assert_rows([])

    def test_c1_manual_steps_and_failures_are_stage_local(self):
        """C1: manual work overrides good counters; unrelated failure doesn't poison another stage."""
        self.report(stage("sync", output=0, status="fail", code="SYNC_FAILED"),
                    stage("extract", status="warn"), stage("digest_gates"), stage("outputs"),
                    result="FAIL", manual_steps=[{"stage": "sync", "why": "SYNC_FAILED"},
                    {"stage": "extract", "why": "extract:AWAITING_OCR"},
                    {"stage": "outputs", "why": "OUTPUTS_BLOCKED"}])
        self.assert_rows([self.row("2", "fail", ["SYNC_FAILED"]),
                          self.row("5", "fail", ["AWAITING_OCR"]), self.row("8")])

    def test_c1_failure_codes_are_valid_and_bounded(self):
        """C1: invalid/raw diagnostics stay out; overlong code lists remain ledger-valid."""
        steps = [{"stage": "digest_gates", "why": f"REVIEW_{n}"} for n in range(25)]
        self.report(stage("extract", status="fail", code="unsafe raw text"), stage("digest_gates"),
                    manual_steps=[{"stage": "extract", "why": None}, *steps])
        self.assert_rows([self.row("5", "fail", ["REHEARSAL_FAILED_STEP", "REHEARSAL_MANUAL_STEP"]),
                          self.row("8", "fail", [f"REVIEW_{n}" for n in range(20)])])

    def test_c2_missing_adjacent_marker_does_not_follow_report_path(self):
        """C2: a decoy valid sandbox named by the report cannot attest to a moved report."""
        self.report(stage("extract"))
        other = self.root / "review"
        other.mkdir()
        self.path = self.path.rename(other / "proof-report.json")
        self.assert_refused()

    def test_c2_rejects_invalid_marker_bytes_or_mode(self):
        """C2: a same-named marker must match the producer and be owner-only."""
        self.report(stage("extract"))
        self.marker.write_bytes(b"e2e-proof sandbox\nextra")
        self.assert_refused()
        self.marker.write_bytes(b"e2e-proof sandbox\n")
        self.marker.chmod(0o644)
        self.assert_refused()

    def test_c2_refuses_symlinked_project_marker_and_report(self):
        """C2: matching bytes elsewhere don't legitimize links through the read boundary."""
        self.report(stage("extract"))
        decoy = self.root / "decoy"
        self.project.rename(decoy)
        self.project.symlink_to(decoy, target_is_directory=True)
        self.assert_refused()
        self.project.rename(self.replay / "project-link")
        decoy.rename(self.project)
        self.marker.rename(self.root / "marker")
        self.marker.symlink_to(self.root / "marker")
        self.assert_refused()
        self.marker.rename(self.project / "marker-link")
        (self.root / "marker").rename(self.marker)
        self.path.rename(self.root / "report.json")
        self.path.symlink_to(self.root / "report.json")
        self.assert_refused()

    def test_c2_refuses_malformed_or_ambiguous_reports(self):
        """C2: don't choose winners in invalid schemas, duplicate keys or duplicate stage claims."""
        for raw in (b"{", b"[]", b'{"schema":"ignored","schema":"records-e2e-proof-v1"}'):
            with self.subTest(raw=raw):
                self.path.write_bytes(raw)
                self.assert_refused()
        self.report(stage("extract"), schema="records-rehearsal-v1")
        self.assert_refused()
        self.report(stage("extract"), stage("extract", status="fail"))
        self.assert_refused()
        self.report(stage("extract"), engine="not-a-commit")
        self.assert_refused()
        self.report(stage("extract", status="invalid"))
        self.assert_refused()
        for fields in ({"manual_steps": None}, {"manual_steps": ["extract"]}, {"stages": ["extract"]}):
            with self.subTest(fields=fields):
                self.report(**fields)
                self.assert_refused()

    def test_c2_missing_report_or_naive_clock_refuses(self):
        """C2: a valid marker can't substitute for report bytes or a UTC-bound observation time."""
        self.assert_refused()
        self.report(stage("extract"))
        self.assert_refused(now=datetime(2026, 11, 22))

    def test_c3_cli_json_and_default_lines(self):
        """C3: both stdout formats emit only complete rows, including descriptive failures."""
        self.report(stage("extract"), stage("digest_gates", status="fail", code="GATES_LEDGER_UNREADABLE"))
        expected = [self.row("5"), self.row("8", "fail", ["GATES_LEDGER_UNREADABLE"])]

        class Clock(datetime):
            @classmethod
            def now(cls, tz=None):
                return cls.fromisoformat(NOW).astimezone(tz)

        for args in ([], ["--json"]):
            with self.subTest(args=args), patch.object(golive_adapt_e2e, "datetime", Clock):
                out, err = io.StringIO(), io.StringIO()
                with redirect_stdout(out), redirect_stderr(err):
                    self.assertEqual(golive_adapt_e2e.main([str(self.path), *args]), 0)
                actual = json.loads(out.getvalue()) if args else [json.loads(s) for s in out.getvalue().splitlines()]
                self.assertEqual((actual, err.getvalue()), (expected, ""))

    def test_c3_refusal_exits_without_receipt_data(self):
        """C3: invalid input exits 2 with a fixed diagnostic and no path or data leak."""
        digest = stage("digest_gates")
        self.report(digest, metadata={"samples": [0.5]})
        self.assert_rows([self.row("8")])

        # A permissive decoder would ignore bad numbers in unconsumed metadata
        # and still emit the otherwise valid stage 8 pass.
        for literal in (None, "NaN", "Infinity", "-Infinity"):
            if literal is None:
                self.path.write_text("not a JSON report", encoding="utf-8")
            else:
                self.report(digest, metadata={"samples": [float(literal)]})
            for args in ([], ["--json"]):
                with self.subTest(literal=literal, args=args):
                    out, err = io.StringIO(), io.StringIO()
                    with redirect_stdout(out), redirect_stderr(err):
                        code = golive_adapt_e2e.main([str(self.path), *args])
                    self.assertEqual((code, out.getvalue(), err.getvalue()),
                                     (2, "", "E2E_EVIDENCE_BAD_INPUT\n"))
