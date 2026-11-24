"""Contracts for the gate dry run (C1-C4). Synthetic fixtures; nothing here is a receipt."""

from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
import hashlib
import io
import json
from unittest.mock import patch

from campaign_tool.records import golive_gate as gate
from campaign_tool.records import golive_gate_dryrun as dryrun
from campaign_tool.records import golive_gate_specs_s5 as s5
from campaign_tool.records import golive_record
from tests.records.test_golive_gate import GateFixture, PRESENT, spec

# S5's real Thursday 10:00-10:10 Pacific check, so the boundary tracks the shipped spec.
SWITCH_WINDOW = next(c for c in s5.GATES["s5-engine-switch"]["checks"] if c["type"] == "window")
THURSDAY_1005 = "2026-11-26T10:05:00-08:00"
EVIDENCE = {"type": "evidence", "label": "rehearsal", "stage": "3", "kind": "rehearsal", "max_age_days": 2}
BEFORE = "2026-11-21T20:00:00+00:00"
ROW = {
    "schema": "records-golive-evidence-v1", "stage": "3", "kind": "rehearsal", "result": "pass",
    "engine": None, "recorded_at": "2026-11-20T20:00:00Z", "actor": "rehearsal", "codes": [],
    "source": {"schema": "records-rehearsal-v1", "name": "r.json", "sha256": "ab" * 32},
}


class DryRunTests(GateFixture):
    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = dryrun.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def spec_file(self, *checks):
        return str(self.fixture("spec.json", json.dumps(spec(*checks)).encode()))

    def tree(self):
        return sorted((str(p.relative_to(self.root)), p.stat().st_mtime_ns, p.stat().st_size)
                      for p in self.root.rglob("*"))

    def ledger(self, *extra_names):
        directory = self.root / "evidence"
        directory.mkdir()
        record = directory / "20261120T200000Z-3-rehearsal-abababab.json"
        record.write_text(json.dumps(ROW), encoding="utf-8")
        record.chmod(0o600)
        for name in extra_names:
            (directory / name).write_text("{}", encoding="utf-8")
        return directory

    def assertRefused(self, *argv, mention):
        before = self.tree()
        code, out, err = self.run_cli(*argv)
        self.assertEqual((code, out), (2, ""), argv)
        self.assertEqual(len(err.splitlines()), 1, err)
        self.assertIn(mention, err)
        self.assertEqual(self.tree(), before)

    def test_c1_first_line_marks_dry_run_in_both_formats(self):
        """C1: a verdict printed without the DRY RUN line looks like a gate result."""
        path = self.spec_file(SWITCH_WINDOW)
        for extra in ((), ("--json",)):
            with self.subTest(extra=extra):
                code, out, _ = self.run_cli("--spec", path, "--at", THURSDAY_1005, *extra)
                self.assertEqual((code, out.splitlines()[0]), (0, "DRY RUN - not a receipt, not authorization"))
        text = self.run_cli("--spec", path, "--at", THURSDAY_1005)[1].splitlines()
        # Bug guard: a bare "GO" line is what the real command prints for a receipt.
        self.assertEqual(text[1].split()[0], "would-be")
        self.assertNotIn("GO", [line.strip() for line in text])

    def test_c1_switch_window_boundary_matches_evaluator(self):
        """C1/C3(a): 10:05 PT is GO and 10:10 is NO-GO; a closed end or wrong-zone clock moves the line."""
        path = self.spec_file(SWITCH_WINDOW)
        cases = (
            ("2026-11-26T10:00:00-08:00", 0), (THURSDAY_1005, 0), ("2026-11-26T10:09:59-08:00", 0),
            ("2026-11-26T10:10:00-08:00", 1), ("2026-11-26T09:59:59-08:00", 1),
            ("2026-11-27T10:05:00-08:00", 1),  # Friday, same wall clock
            ("2026-11-26T18:05:00Z", 0), ("2026-11-27T03:05:00+09:00", 0),  # same instants, other offsets
            ("2026-11-26T10:05:00Z", 1),  # 10:05 UTC is 02:05 Pacific: the offset is honoured, not stripped
        )
        for at, code in cases:
            with self.subTest(at=at):
                got = self.run_cli("--spec", path, "--at", at)[0]
                expected = gate.evaluate(spec(SWITCH_WINDOW), {}, datetime.fromisoformat(at))["verdict"]
                self.assertEqual((got, expected), (code, "GO" if code == 0 else "NO-GO"))

    def test_c1_full_s5_spec_names_the_checks_that_would_fail(self):
        """C1: the window passes at 10:05 but unreadable evidence must still hold the verdict at NO-GO."""
        pins = {"wrapper": "a" * 64, "live": "b" * 64}
        path = str(self.fixture("s5.json", json.dumps(s5.spec("s5-engine-switch", **pins)).encode()))
        absent = str(self.root / "absent")
        mapping = [f"--input={name}={absent}" for name in ("wrapper", "live", "disk", "pair", "evidence")]
        code, out, _ = self.run_cli("--spec", path, "--at", THURSDAY_1005, *mapping)
        self.assertEqual(code, 1)
        self.assertIn('window "Thursday Pacific switch slot (booking confirmed separately)": pass GATE_WINDOW_OPEN', out)
        self.assertIn('sha256_pin "Candidate run-records.sh": unknown GATE_INPUT_MISSING', out)
        self.assertIn("EVIDENCE_UNREADABLE", out)

    def test_c2_writes_no_file_and_has_no_out_option(self):
        """C2(b): a copy of golive_gate's --out would leave a receipt-shaped file behind."""
        path = self.spec_file(PRESENT)
        evidence = self.fixture("copy", b"synthetic")
        before = self.tree()
        with patch.object(gate, "_write_receipt", side_effect=AssertionError("writer called")):
            for extra in ((), ("--json",)):
                code, _, err = self.run_cli("--spec", path, "--input", f"copy={evidence}", "--at", THURSDAY_1005, *extra)
                self.assertEqual((code, err), (0, ""))
        self.assertEqual(self.tree(), before)
        self.assertRefused("--spec", path, "--input", f"copy={evidence}", "--at", THURSDAY_1005,
                           "--out", str(self.root), mention="--out")
        self.assertRefused("--spec", path, "--input", f"copy={evidence}", "--at", THURSDAY_1005,
                           "--ou", str(self.root), mention="--ou")

    def test_c2_json_is_wrapped_and_refused_by_golive_record(self):
        """C2(c): a bare receipt printed with a past --at is a valid gate receipt for golive_record."""
        path = self.spec_file(SWITCH_WINDOW)
        at = datetime.fromisoformat(THURSDAY_1005)
        out = self.run_cli("--spec", path, "--at", THURSDAY_1005, "--json")[1]
        banner, _, body = out.partition("\n")
        wrapped = json.loads(body)
        receipt = gate.evaluate(spec(SWITCH_WINDOW), {}, at)
        self.assertEqual(wrapped, {"dry_run": True, "notice": banner, "evaluation": receipt})
        later = datetime(2030, 1, 1, tzinfo=timezone.utc)
        control = self.fixture("control.json", (json.dumps(receipt) + "\n").encode())
        self.assertEqual([r["result"] for r in golive_record.from_gate(control, later)], ["pass"] * len(receipt["stages"]))
        for name, raw in (("wrapped.json", body), ("whole-stdout.json", out)):
            with self.subTest(name=name), self.assertRaises(ValueError):
                golive_record.from_gate(self.fixture(name, raw.encode()), later)

    def test_c3_naive_or_unparseable_time_is_refused(self):
        """C3(d): a naive --at would be read in the host zone, so the booked slot could be evaluated wrongly."""
        path = self.spec_file(SWITCH_WINDOW)
        for at in ("2026-11-26T10:05:00", "2026-11-26", "10:05", "Thursday 10:05", ""):
            with self.subTest(at=at):
                self.assertRefused("--spec", path, "--at", at, mention="--at must be an ISO 8601 time with a UTC offset")
        self.assertRefused("--spec", path, mention="--at")

    def test_c3_bad_spec_and_input_arguments_exit_two(self):
        """C3: bad input is exit 2 with one message, never a verdict (1) or a traceback."""
        path = self.spec_file(PRESENT)
        evidence = self.fixture("copy", b"x")
        at = ("--at", THURSDAY_1005)
        bad_json = self.fixture("bad.json", b"{")
        dup = self.fixture("dup.json", b'{"schema": 1, "schema": 2}')
        invalid = self.fixture("invalid.json", b"{}")
        cases = (
            (("--spec", str(self.root / "absent.json"), *at), "cannot read spec"),
            (("--spec", str(bad_json), *at), "cannot read spec"),
            (("--spec", str(dup), *at), "cannot read spec"),
            (("--spec", str(invalid), *at), "invalid spec"),
            (("--spec", path, *at), "missing --input NAME=PATH for: copy"),
            (("--spec", path, *at, "--input", "copy"), "NAME=PATH"),
            (("--spec", path, *at, "--input", "copy="), "NAME=PATH"),
            (("--spec", path, *at, "--input", f"copy={evidence}", "--input", f"copy={evidence}"), "distinct"),
        )
        for argv, mention in cases:
            with self.subTest(argv=argv):
                self.assertRefused(*argv, mention=mention)

    def test_c3_every_file_the_spec_can_consult_must_be_mapped(self):
        """C3/C4: a missing pair or live mapping would otherwise degrade silently to an unknown NO-GO."""
        every = [
            {"type": "sha256_pin", "label": "s", "input": "wrapper", "expected": "a" * 64},
            {"type": "disk_floor_gib", "label": "d", "input": "disk", "min": 1},
            {**EVIDENCE, "engine_input": "pair", "candidate_inputs": ["wrapper", "live"]},
            {"type": "prior_gate", "label": "p", "gate_id": "pre-flight", "max_age_days": 1},
        ]
        self.assertRefused("--spec", self.spec_file(*every), "--at", THURSDAY_1005,
                           mention="for: disk, evidence, live, pair, wrapper")
        self.assertEqual(dryrun.required_inputs(spec(SWITCH_WINDOW)), set())

    def test_c4_evidence_is_a_hash_of_validated_records(self):
        """C4: hashing raw directory bytes, or hashing a ledger holding an invalid file, would pin unvalidated data."""
        path = self.spec_file(EVIDENCE)
        at = ("--at", BEFORE)
        ledger = self.ledger()
        good = json.loads(self.run_cli("--spec", path, *at, "--input", f"evidence={ledger}", "--json")[1].split("\n", 1)[1])
        canonical = json.dumps([ROW], sort_keys=True, separators=(",", ":")).encode("ascii")
        self.assertEqual(good["evaluation"]["inputs"], {"evidence": hashlib.sha256(canonical).hexdigest()})
        self.assertEqual(good["evaluation"]["checks"][0]["code"], "EVIDENCE_MATCH")
        self.assertEqual(good["evaluation"], gate.evaluate(spec(EVIDENCE), {"evidence": ledger}, datetime.fromisoformat(BEFORE)))
        (ledger / "garbage.json").write_text("{}", encoding="utf-8")
        bad = json.loads(self.run_cli("--spec", path, *at, "--input", f"evidence={ledger}", "--json")[1].split("\n", 1)[1])
        self.assertEqual((bad["evaluation"]["inputs"], bad["evaluation"]["checks"][0]["code"], bad["evaluation"]["verdict"]),
                         ({}, "EVIDENCE_UNREADABLE", "NO-GO"))
