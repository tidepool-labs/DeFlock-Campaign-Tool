"""Runtime contracts for evidence-based attended gates, using only copied fixtures."""

from contextlib import redirect_stdout
from datetime import datetime, timedelta
import hashlib
import io
import json
from unittest.mock import patch

from campaign_tool.records import golive_gate as gate
from tests.records.test_golive_gate import GateFixture, NOW, PRESENT, receipt, result, spec

CHECK = {"type": "evidence", "label": "rehearsal", "stage": "3", "kind": "rehearsal", "max_age_days": 2}
PINNED = dict(CHECK, engine_input="pair")
PRIOR = {"type": "prior_gate", "label": "preflight", "gate_id": "pre-flight", "max_age_days": 2}
ENGINE = "a" * 40
E2E = "records-e2e-proof-v1"


class GateEvidenceTests(GateFixture):
    def setUp(self):
        super().setUp()
        self.ledger = self.root / "golive-evidence"
        self.ledger.mkdir()
        self.pair = self.root / "pair.json"
        self.rows = []

    def row(self, *, age=timedelta(days=1), stage="3", kind="rehearsal", outcome="pass",
            engine: str | None = ENGINE, source_schema=None, source_name=None):
        observed = NOW - age
        digest = hashlib.sha256(str(len(self.rows)).encode("ascii")).hexdigest()
        default_schema = "records-rehearsal-v1" if kind == "rehearsal" else "records-golive-gate-v1"
        row = {
            "schema": "records-golive-evidence-v1", "stage": stage, "kind": kind, "result": outcome,
            "engine": engine, "recorded_at": observed.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "source": {"schema": source_schema or default_schema,
                       "name": source_name or "pre-flight.json", "sha256": digest},
            "actor": "rehearsal" if kind == "rehearsal" else "operator", "codes": [],
        }
        path = self.ledger / f"{observed:%Y%m%dT%H%M%SZ}-{stage}-{kind}-{digest[:8]}.json"
        path.write_text(json.dumps(row), encoding="utf-8")
        path.chmod(0o600)
        self.rows.append((path, row))
        return path

    def ledger_hash(self):
        rows = [row for _, row in sorted(self.rows)]
        raw = json.dumps(rows, sort_keys=True, separators=(",", ":")).encode("ascii")
        return hashlib.sha256(raw).hexdigest()

    def assert_check(self, check, state, code, *, inputs=None, hashes=None, now=NOW):
        if inputs is None:
            inputs = {"evidence": self.ledger}
        if hashes is None:
            hashes = {"evidence": self.ledger_hash()}
        expected = receipt("GO" if state == "pass" else "NO-GO", [result(check, state, code)],
                           hashes, instant=now.isoformat())
        self.assertEqual(gate.evaluate(spec(check), inputs, now), expected)

    def assert_bad_check(self, check, field):
        with self.assertRaisesRegex(ValueError, field) as caught:
            gate.load_spec(spec(check))
        self.assertIs(type(caught.exception), ValueError)

    def test_c1_spec_refusals(self):
        """C1: a misspelled selector or invalid age cannot silently loosen the gate."""
        for field, invalids in {
            "stage": (3, "21", []), "kind": ("live", None, []),
            "max_age_days": (-1, True, "2", float("inf"), float("nan")),
            "engine_input": (None, [], " ", "pair=other", "evidence"),
        }.items():
            for invalid in invalids:
                with self.subTest(field=field, value=invalid):
                    self.assert_bad_check(dict(CHECK, **{field: invalid}), field)
        for field in ("stage", "kind", "max_age_days"):
            self.assert_bad_check({k: v for k, v in CHECK.items() if k != field}, f"missing fields: {field}")
        self.assert_bad_check(dict(CHECK, engine_inputs="pair"), "unknown fields: 'engine_inputs'")

    def test_c1_missing_directory_input_has_no_implicit_lookup(self):
        """C1: an omitted mapping stays missing even when a fixture ledger exists."""
        self.row()
        self.assert_check(CHECK, "unknown", "EVIDENCE_MISSING", inputs={}, hashes={})

    def test_c1_missing_stage_or_kind(self):
        """C1: fresh unrelated evidence cannot substitute for the requested stage and kind."""
        self.assert_check(CHECK, "unknown", "EVIDENCE_MISSING")
        self.row(stage="4")
        self.row(kind="gate")
        self.assert_check(CHECK, "unknown", "EVIDENCE_MISSING")
        self.assert_check(dict(CHECK, kind="gate"), "pass", "EVIDENCE_MATCH")

    def test_c1_latest_failure_masks_older_pass(self):
        """C1: do not search for any pass when the latest matching rehearsal failed."""
        self.row(age=timedelta(days=2))
        self.row(outcome="fail")
        self.assert_check(CHECK, "fail", "EVIDENCE_FAILED")

    def test_c1_freshness_boundary(self):
        """C1: expiry is exact elapsed UTC time, not a truncated day count."""
        self.row(age=timedelta(days=2))
        self.assert_check(CHECK, "pass", "EVIDENCE_MATCH")
        self.assert_check(CHECK, "fail", "EVIDENCE_STALE", now=NOW + timedelta(microseconds=1))

    def test_c1_future_evidence_is_stale(self):
        """C1: negative ages must not be mistaken for fresh rehearsals."""
        self.row(age=-timedelta(seconds=1))
        self.assert_check(CHECK, "fail", "EVIDENCE_STALE")

    def test_c1_unreadable_directory(self):
        """C1: an invalid explicit path is unreadable, not a usable empty snapshot."""
        path = self.row()
        link = self.root / "linked-ledger"
        link.symlink_to(self.ledger, target_is_directory=True)
        for invalid in (self.root / "nonexistent", path, link):
            with self.subTest(path=invalid):
                self.assert_check(CHECK, "unknown", "EVIDENCE_UNREADABLE", inputs={"evidence": invalid}, hashes={})

    def test_c1_corrupt_ledger_is_not_partially_accepted(self):
        """C1: a valid pass must not hide a malformed later record."""
        self.row()
        self.row(age=timedelta(hours=1)).write_text("{", encoding="utf-8")
        self.assert_check(CHECK, "unknown", "EVIDENCE_UNREADABLE", hashes={})

    def assert_pair(self, raw, state, code):
        self.pair.write_bytes(raw)
        self.assert_check(PINNED, state, code, inputs={"evidence": self.ledger, "pair": self.pair},
                          hashes={"evidence": self.ledger_hash(), "pair": hashlib.sha256(raw).hexdigest()})

    def test_c1_full_engine_matches_in_both_admitted_forms(self):
        """C1: match the first full commit whether bare or emitted with the engine prefix."""
        self.row()
        for ref in (f"engine-{ENGINE}", ENGINE):
            with self.subTest(ref=ref):
                self.assert_pair(json.dumps({"engines": [ref]}).encode(), "pass", "EVIDENCE_MATCH")

    def test_c1_other_full_engine_does_not_match(self):
        """C1: do not fall back to engines[1] or an older matching rehearsal."""
        self.row(age=timedelta(days=2), engine="b" * 40)
        self.row()
        self.assert_pair(json.dumps({"engines": ["engine-" + "b" * 40, f"engine-{ENGINE}"]}).encode(),
                         "fail", "EVIDENCE_ENGINE_MISMATCH")

    def test_c1_unsupported_engine_refs_are_not_inferred(self):
        """C1: matching a seven-character prefix, trimming, or folding case would certify the wrong proof."""
        self.row()
        for engines in (["engine-aaaaaaa"], [f"engine-{ENGINE.upper()}"], [ENGINE + "\n"],
                        [None], [], ENGINE):
            with self.subTest(engines=engines):
                self.assert_pair(json.dumps({"engines": engines}).encode(), "fail", "EVIDENCE_ENGINE_MISMATCH")

    def test_c1_null_evidence_engine_is_not_bound(self):
        """C1: a valid full commit cannot bind to a ledger row with no engine."""
        self.row(engine=None)
        self.assert_pair(json.dumps({"engines": [ENGINE]}).encode(), "fail", "EVIDENCE_ENGINE_MISMATCH")

    def test_c1_pair_input_must_be_readable_json(self):
        """C1: a missing, non-file, corrupt or ambiguous pair copy is not a match."""
        self.row()
        for inputs in ({"evidence": self.ledger}, {"evidence": self.ledger, "pair": self.ledger}):
            self.assert_check(PINNED, "unknown", "EVIDENCE_UNREADABLE", inputs=inputs)
        for raw in (b"{", b"\xff", b'{"engines": [], "engines": []}'):
            self.assert_pair(raw, "unknown", "EVIDENCE_UNREADABLE")

    def test_c1a_only_sandbox_rehearsal_is_missing(self):
        """C1a: a descriptive sandbox e2e pass never qualifies as rehearsal proof."""
        self.row(source_schema=E2E)
        self.assert_check(CHECK, "unknown", "EVIDENCE_MISSING")

    def test_c1a_sandbox_gate_is_not_evidence_but_remains_prior_gate(self):
        """Descriptive gate rows cannot satisfy evidence checks, but prior_gate is unchanged."""
        self.row(kind="gate", source_schema=E2E)
        self.assert_check(dict(CHECK, kind="gate"), "unknown", "EVIDENCE_MISSING")
        self.assert_check(PRIOR, "pass", "EVIDENCE_MATCH")

    def test_c1a_sandbox_gate_cannot_mask_qualified_failure(self):
        """Filter descriptive gate rows before choosing the latest qualified row."""
        self.row(kind="gate", source_schema=E2E, age=timedelta(hours=1))
        self.row(kind="gate", outcome="fail")
        self.assert_check(dict(CHECK, kind="gate"), "fail", "EVIDENCE_FAILED")

    def test_c1a_sandbox_pass_cannot_mask_qualified_failure(self):
        """C1a: filtering must happen before latest selection so failures remain visible."""
        self.row(source_schema=E2E, age=timedelta(hours=1))
        self.row(outcome="fail")
        self.assert_check(CHECK, "fail", "EVIDENCE_FAILED")

    def test_c1a_sandbox_pass_cannot_refresh_stale_qualified_pass(self):
        """C1a: measure age from the older qualified pass, not the newer e2e row."""
        self.row(source_schema=E2E)
        self.row(age=timedelta(days=3))
        self.assert_check(CHECK, "fail", "EVIDENCE_STALE")

    def test_c1a_fresh_qualified_pass_survives_sandbox_row(self):
        """C1a: pass using the selected qualified engine, not the newer sandbox engine."""
        self.row(source_schema=E2E, age=timedelta(hours=1), engine="b" * 40)
        self.row()
        self.assert_pair(json.dumps({"engines": [f"engine-{ENGINE}"]}).encode(), "pass", "EVIDENCE_MATCH")

    def test_c2_spec_refusals(self):
        """C2: reject invalid IDs, ages and unsupported selectors instead of broad matching."""
        for patch, field in (({"gate_id": "../pre-flight"}, "gate_id"), ({"gate_id": None}, "gate_id"),
                             ({"max_age_days": -1}, "max_age_days"), ({"stage": "3"}, "unknown fields")):
            self.assert_bad_check(dict(PRIOR, **patch), field)
        for field in ("gate_id", "max_age_days"):
            self.assert_bad_check({k: v for k, v in PRIOR.items() if k != field}, f"missing fields: {field}")

    def test_c2_missing_or_unreadable_ledger(self):
        """C2: never treat an absent or invalid ledger as a passed gate."""
        self.assert_check(PRIOR, "unknown", "EVIDENCE_MISSING", inputs={}, hashes={})
        self.assert_check(PRIOR, "unknown", "EVIDENCE_UNREADABLE",
                          inputs={"evidence": self.pair}, hashes={})

    def test_c2_matches_gate_kind_and_source_prefix_only(self):
        """C2: reject wrong kinds and substring matches; no stage constraint is implied."""
        self.row()
        self.row(kind="gate", source_name="before-pre-flight.json")
        self.assert_check(PRIOR, "unknown", "EVIDENCE_MISSING")
        self.row(kind="gate", stage="17b", source_name="pre-flight-copy.json", age=timedelta(days=2))
        self.row(kind="gate", outcome="fail", age=timedelta(hours=1))
        self.assert_check(PRIOR, "pass", "EVIDENCE_MATCH")

    def test_c2_only_failure_or_stale_pass_is_no_go(self):
        """C2: a matching failure cannot qualify, and a matching pass does not waive age."""
        self.row(kind="gate", outcome="fail")
        self.assert_check(PRIOR, "fail", "EVIDENCE_FAILED")
        self.row(kind="gate", age=timedelta(days=3))
        self.assert_check(PRIOR, "fail", "EVIDENCE_STALE")

    def test_c3_directory_cli_and_core_file_check(self):
        """C3: only evidence checks read the named directory; core checks still require files."""
        self.row()
        path = self.root / "spec.json"
        path.write_text(json.dumps(spec(CHECK, dict(PRESENT, input="evidence"))), encoding="utf-8")
        out = io.StringIO()

        class Clock(datetime):
            @classmethod
            def now(cls, tz=None):
                return cls.fromtimestamp(NOW.timestamp(), tz)

        with redirect_stdout(out), patch.object(gate, "datetime", Clock):
            exit_code = gate.main(["--spec", str(path), "--input", f"evidence={self.ledger}", "--json"])
        self.assertEqual((exit_code, json.loads(out.getvalue())), (1, receipt("NO-GO", [
            result(CHECK, "pass", "EVIDENCE_MATCH"), result(PRESENT, "unknown", "GATE_INPUT_MISSING"),
        ], {"evidence": self.ledger_hash()})))
