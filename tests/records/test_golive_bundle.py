"""Synthetic runtime coverage for the sitting packet renderer and CLI."""

from contextlib import redirect_stderr, redirect_stdout
from copy import deepcopy
import hashlib
import io
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest

from campaign_tool.records import golive_bundle


STAMP = "2026-01-02T03:04:05Z"
ROLLBACK = "Notebook / RB-01"
SPEC = {
    "schema": "records-golive-gate-spec-v1", "gate_id": "synthetic-step",
    "stages": ["19", "18"], "title": "Switch Å — Phase II",
    "checks": [{"type": "file_present", "label": "Copy present", "input": "copy"},
               {"type": "sha256_pin", "label": "Pin", "input": "copy", "expected": "0" * 64},
               {"type": "file_present", "label": "Other copy", "input": "other"}],
}
GATE = {
    "schema": "records-golive-gate-v1", "gate_id": "synthetic-step",
    "stages": ["18", "19", "18"], "evaluated_at": STAMP, "verdict": "NO-GO",
    "inputs": {"copy": "1" * 64},
    "checks": [
        {"type": "file_present", "label": "Copy present", "state": "pass", "code": "GATE_FILE_PRESENT"},
        {"type": "sha256_pin", "label": "Pin", "state": "fail", "code": "GATE_SHA256_MISMATCH"},
        {"type": "file_present", "label": "Other copy", "state": "unknown", "code": "GATE_INPUT_MISSING"},
    ],
}
ROWS = [
    {"stage": "19", "label": "Backup check", "rehearsed": {"status": "missing", "reason": "STALE"}},
    {"stage": "18", "label": "Monitoring", "rehearsed": {"status": "pass"}},
    {"stage": "15", "label": "Unrelated ledger stage", "rehearsed": {"status": "pass"}},
]


def receipt(stages, *, code="OK", output="sandbox.txt"):
    # Whitespace and newline are part of the receipt identity.
    return (json.dumps({
        "schema": "records-rehearsal-v1", "module": "synthetic", "stages": stages,
        "started_at": STAMP, "finished_at": STAMP, "engine": "a" * 40,
        "steps": [{"name": "synthetic step", "code": code,
                   "outputs": [{"path": output, "sha256": "b" * 64}]}],
        "manual_steps": [], "codes": [], "result": "pass" if code == "OK" else "fail",
        "qualified": code == "OK", "qualify_codes": [] if code == "OK" else ["REHEARSAL_FAILED_STEP"],
    }, indent=2) + "\n").encode()


PASS = receipt(["18", "19"])
FAIL = receipt(["19"], code="SYNTHETIC_FAILURE")
FULL_PAGE = f"""# Switch Å — Phase II

Gate verdict: **NO-GO**
Evidence only; not authorization to proceed.

## Gate checks
- "Copy present" (file_present): pass — GATE_FILE_PRESENT
- "Pin" (sha256_pin): fail — GATE_SHA256_MISMATCH
- "Other copy" (file_present): unknown — GATE_INPUT_MISSING

## Stages and rehearsals
- Stage 18 — Monitoring: ledger rehearsal pass
  - Receipt: pass (qualified); SHA-256: {hashlib.sha256(PASS).hexdigest()}
- Stage 19 — Backup check: ledger rehearsal missing (STALE)
  - Receipt: pass (qualified); SHA-256: {hashlib.sha256(PASS).hexdigest()}
  - Receipt: fail (not qualified: REHEARSAL_FAILED_STEP); SHA-256: {hashlib.sha256(FAIL).hexdigest()}

Rollback reference: Notebook / RB-01
"""


class RenderTests(unittest.TestCase):
    def assert_bad_input(self, *, gate=GATE, rows=ROWS, receipts=(), spec=SPEC, rollback: str | None = ROLLBACK):
        with self.assertRaises(ValueError) as error:
            golive_bundle.render(gate, rows, receipts, gate_spec=spec, rollback=rollback)
        self.assertIs(type(error.exception), ValueError)

    def test_c1_full_packet(self):
        """C1: Preserve the spec title, check order, stage set and exact receipt-byte hashes."""
        self.assertEqual(golive_bundle.render(
            GATE, ROWS, [PASS, FAIL, PASS, receipt(["15"])], gate_spec=SPEC, rollback=ROLLBACK,
        ), FULL_PAGE)

    def test_c1_go_with_missing_evidence(self):
        """C1: A recorded GO stays GO; missing supporting copies are explicit, not invented."""
        gate = dict(GATE, stages=["18"], checks=GATE["checks"][:1], verdict="GO")
        spec = dict(SPEC, stages=["18"], checks=SPEC["checks"][:1])
        self.assertEqual(golive_bundle.render(gate, [], [], gate_spec=spec, rollback="RB-02"), """# Switch Å — Phase II

Gate verdict: **GO**
Evidence only; not authorization to proceed.

## Gate checks
- "Copy present" (file_present): pass — GATE_FILE_PRESENT

## Stages and rehearsals
- Stage 18: no ledger row supplied
  - No rehearsal receipt supplied.

Rollback reference: RB-02
""")

    def test_c1_spec_id_mismatch(self):
        """C1: A spec for another gate cannot name the packet."""
        self.assert_bad_input(spec=dict(SPEC, gate_id="other-step"))

    def test_c1_spec_stage_mismatch(self):
        """C1: A spec with a different stage set cannot name the packet."""
        self.assert_bad_input(spec=dict(SPEC, stages=["19"]))

    def test_c1_spec_schema_mismatch(self):
        """C1: A foreign spec schema cannot name the packet."""
        self.assert_bad_input(spec=dict(SPEC, schema="other-v1"))

    def test_c1_spec_invalid_title(self):
        """C1: Missing or invalid titles are refused, never inferred from the gate ID."""
        for fields in ({}, {"title": ""}, {"title": "   "}, {"title": 7}):
            with self.subTest(fields=fields):
                self.assert_bad_input(spec={k: v for k, v in SPEC.items() if k != "title"} | fields)

    def test_c1_malformed_gate(self):
        """C1: Malformed receipt metadata or a forged GO cannot produce a page."""
        for change in (
            {"schema": "other-v1"}, {"gate_id": "bad id"}, {"stages": [[]]},
            {"evaluated_at": None}, {"inputs": {"copy": "not a digest"}}, {"checks": []},
            {"checks": [dict(GATE["checks"][0], state="ok")]}, {"verdict": "GO"},
        ):
            with self.subTest(change=change):
                self.assert_bad_input(gate=GATE | change)

    def test_c1_malformed_ledger(self):
        """C1: Bad and ambiguous ledger rows cannot be mistaken for stage evidence."""
        for rows in ({}, ["wrong shape"], [{"stage": "18"}], ROWS + [ROWS[1]]):
            with self.subTest(rows=rows):
                self.assert_bad_input(rows=rows)

    def test_c1_malformed_rehearsals(self):
        """C1: Neither reserialized objects nor malformed receipt files become evidence."""
        for item in ({}, b"{", b'{"schema":"x","schema":"y"}', b"{}"):
            with self.subTest(item=item):
                self.assert_bad_input(receipts=[item])

    def test_c1_invalid_rollback(self):
        """C1: The rollback reference cannot be absent or inject another packet line."""
        for value in (None, "", "RB-01\nRB-02"):
            with self.subTest(value=value):
                self.assert_bad_input(rollback=value)

    def test_c3_metadata_only(self):
        """C3: Input names, content fields and receipt output paths never reach the page."""
        gate = deepcopy(GATE)
        gate["inputs"]["SYNTHETIC_PATH"] = "e" * 64
        gate["checks"][0]["record_text"] = "SYNTHETIC_CHECK_TEXT"
        rows = [dict(row, record_text="SYNTHETIC_ROW_TEXT") for row in ROWS]
        raw = receipt(["18", "19"], output="SYNTHETIC_RECORD_PATH.txt")
        self.assertEqual(golive_bundle.render(
            gate, rows, [raw, FAIL], gate_spec=SPEC, rollback=ROLLBACK,
        ), FULL_PAGE.replace(hashlib.sha256(PASS).hexdigest(), hashlib.sha256(raw).hexdigest()))


class CliTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name).resolve()
        for name, item in (("spec", SPEC), ("gate", GATE), ("ledger", {"rows": ROWS})):
            (self.root / f"{name}.json").write_text(json.dumps(item), encoding="utf-8")
        (self.root / "pass.json").write_bytes(PASS)
        (self.root / "fail.json").write_bytes(FAIL)
        self.args = ["--spec", str(self.root / "spec.json"), "--gate", str(self.root / "gate.json"),
                     "--ledger", str(self.root / "ledger.json"), "--receipt", str(self.root / "pass.json"),
                     "--receipt", str(self.root / "fail.json"), "--rollback", ROLLBACK]

    def invoke(self, args):
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            status = golive_bundle.main(args)
        return status, stdout.getvalue(), stderr.getvalue()

    def test_c2_stdout(self):
        """C2: A NO-GO packet still writes exact stdout successfully, without creating a file."""
        before = sorted(self.root.iterdir())
        self.assertEqual(self.invoke(self.args), (0, FULL_PAGE, ""))
        self.assertEqual(sorted(self.root.iterdir()), before)

    def test_c2_private_file(self):
        """C2: The new file is exactly 0600 even under a restrictive umask; stdout stays empty."""
        out = self.root / "packet.md"
        previous = os.umask(0o777)
        try:
            self.assertEqual(self.invoke(self.args + ["--out", str(out)]), (0, "", ""))
        finally:
            os.umask(previous)
        self.assertEqual((out.read_bytes(), stat.S_IMODE(out.stat().st_mode)), (FULL_PAGE.encode(), 0o600))

    def test_c2_no_overwrite(self):
        """C2: An existing page or dangling link cannot be overwritten or followed."""
        out = self.root / "existing.md"
        out.write_bytes(b"original")
        dangling = self.root / "link.md"
        target = self.root / "absent.md"
        dangling.symlink_to(target)
        for path in (out, dangling):
            with self.subTest(path=path):
                status, stdout, _ = self.invoke(self.args + ["--out", str(path)])
                self.assertEqual((status, stdout), (2, ""))
        self.assertEqual((out.read_bytes(), dangling.readlink(), target.exists()), (b"original", target, False))

    def test_c2_bad_input(self):
        """C2: Missing, malformed and mismatched copied inputs do not create a packet."""
        spec = self.root / "spec.json"
        out = self.root / "packet.md"
        cases = [("unreadable", self.args[:1] + [str(self.root / "absent.json")] + self.args[2:], None),
                 ("malformed", self.args, "{"),
                 ("unencodable", self.args, json.dumps(dict(SPEC, title="\ud800"))),
                 ("mismatched", self.args, json.dumps(dict(SPEC, gate_id="other-step")))]
        for name, args, content in cases:
            with self.subTest(name=name):
                if content is not None:
                    spec.write_text(content, encoding="utf-8")
                status, stdout, _ = self.invoke(args + ["--out", str(out)])
                self.assertEqual((status, stdout, out.exists()), (2, "", False))

    def test_c2_spec_required(self):
        """C2: Omitting --spec is a usage error, without stdout or an output file."""
        out = self.root / "packet.md"
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr), self.assertRaises(SystemExit) as error:
            golive_bundle.main(self.args[2:] + ["--out", str(out)])
        self.assertEqual((error.exception.code, stdout.getvalue(), out.exists()), (2, "", False))
