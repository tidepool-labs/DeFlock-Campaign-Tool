"""Synthetic evaluation of lane gate packs; no attended step is performed."""

from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from zoneinfo import ZoneInfo

from campaign_tool.records import golive_gate, golive_record
from campaign_tool.records.golive_gate_specs_lanes import GATES, spec


PORTAL = "c5-portal-apply"
PACIFIC = ZoneInfo("America/Los_Angeles")
NOW = datetime(2025, 2, 5, 14, tzinfo=PACIFIC)
RECORDED = (NOW - timedelta(days=1)).astimezone(timezone.utc)
ENGINE = "a" * 40
RELEASE = b"synthetic operator hold-release record\n"
MATCH = ("pass", "EVIDENCE_MATCH")
MISSING = ("unknown", "EVIDENCE_MISSING")
WINDOW_OPEN = ("pass", "GATE_WINDOW_OPEN")
RELEASE_PRESENT = ("pass", "GATE_FILE_PRESENT")
RELEASE_MISSING = ("unknown", "GATE_INPUT_MISSING")
LANES = (
    ("c6-findings-on", "16", "16", "s6a-trust-mount", "20",
     "Trust mount gate", "Findings lane rehearsal"),
    ("l17-packets", "17", "17", "c6-findings-on", "16",
     "Findings lane gate", "Packets lane rehearsal"),
    ("a4-hostname", "A4", "A3", "s10-console", "A3",
     "Console gate", "New console rehearsal"),
)


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


class LaneSpecTests(unittest.TestCase):
    def test_c2_returns_validated_independent_copy_with_or_without_age_pin(self):
        """C2: an age pin takes effect without mutating the default or dropping a check."""
        expected = {
            "schema": "records-golive-gate-spec-v1", "gate_id": PORTAL,
            "stages": ["4"], "title": "Portal apply", "checks": [
                {"type": "file_present", "label": "Operator hold-release record", "input": "hold_release"},
                {"type": "window", "label": "Outside run slots (Pacific)",
                 "days": ["mon", "tue", "wed", "thu", "fri", "sat", "sun"],
                 "start": "14:00", "end": "18:00"},
                {"type": "prior_gate", "label": "Engine switch gate", "gate_id": "s5-engine-switch",
                 "max_age_days": 14},
            ],
        }
        candidate = spec(PORTAL, max_age_days=2)
        pinned = {**expected, "checks": [*expected["checks"][:2],
                                        {**expected["checks"][2], "max_age_days": 2}]}
        self.assertEqual(golive_gate.load_spec(candidate), pinned)
        candidate["checks"][1]["days"].clear()
        self.assertEqual(spec(PORTAL), expected)
        self.assertEqual(golive_gate.load_spec(GATES[PORTAL]), expected)

    def test_c2_builder_refusals(self):
        """C2: typos and malformed age pins do not silently fall back to defaults."""
        cases = (
            ("unlisted", {}, KeyError),
            (PORTAL, {"hold_release": False}, TypeError),
            (PORTAL, {"max_age_days": -1}, ValueError),
        )
        for gate_id, pins, error in cases:
            with self.subTest(gate_id=gate_id, pins=pins):
                with self.assertRaises(error) as raised:
                    spec(gate_id, **pins)
                self.assertIs(type(raised.exception), error)


class LaneEvaluationTests(unittest.TestCase):
    def setUp(self):
        root = TemporaryDirectory(dir=Path.cwd().resolve())
        self.addCleanup(root.cleanup)
        self.root = Path(root.name)
        self.evidence = self.root / "evidence"
        self.evidence.mkdir()
        self.rows = {}

    def record(self, stage, *, kind, name):
        row = {
            "schema": "records-golive-evidence-v1", "stage": stage, "kind": kind, "result": "pass",
            "engine": ENGINE if kind == "rehearsal" else None,
            "actor": "rehearsal" if kind == "rehearsal" else "operator", "codes": [],
            "recorded_at": RECORDED.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "source": {"schema": "records-rehearsal-v1" if kind == "rehearsal" else "records-golive-gate-v1",
                       "name": name, "sha256": digest(name.encode())},
        }
        path = golive_record.append(row, self.evidence)
        self.rows[path.name] = row
        return path

    def assert_receipt(self, gate_id, stage, inputs, checks, verdict, *, now=NOW, hashes=None):
        canonical = json.dumps([self.rows[name] for name in sorted(self.rows)],
                               sort_keys=True, separators=(",", ":")).encode()
        expected = {
            "schema": "records-golive-gate-v1", "gate_id": gate_id, "stages": [stage],
            "evaluated_at": now.astimezone(timezone.utc).isoformat(), "verdict": verdict,
            "checks": [dict(type=kind, label=label, state=state, code=code)
                       for kind, label, (state, code) in checks],
            "inputs": {"evidence": digest(canonical), **(hashes or {})},
        }
        self.assertEqual(golive_gate.evaluate(spec(gate_id), inputs, now), expected)

    def test_c1_lanes_require_each_prerequisite_and_the_same_engine(self):
        """C1: neither prerequisite, nor a rehearsal on a different engine, stands in for both."""
        self.assertEqual(set(GATES), {PORTAL, "c6-findings-on", "l17-packets", "a4-hostname"})
        for gate_id, stage, rehearsal_stage, prior_id, prior_stage, gate_label, rehearsal_label in LANES:
            with self.subTest(gate=gate_id):
                self.evidence = self.root / gate_id
                self.evidence.mkdir()
                self.rows = {}
                pair = self.evidence.with_suffix(".json")
                pair_bytes = json.dumps({"engines": [f"engine-{ENGINE}"]}).encode()
                pair.write_bytes(pair_bytes)
                inputs = {"evidence": self.evidence, "pair": pair}
                pair_hash = {"pair": digest(pair_bytes)}
                prior = self.record(prior_stage, kind="gate", name=f"{prior_id}.json")
                self.assert_receipt(gate_id, stage, inputs, [
                    ("prior_gate", gate_label, MATCH), ("evidence", rehearsal_label, MISSING),
                ], "NO-GO")
                self.record(rehearsal_stage, kind="rehearsal", name=f"rehearse-{rehearsal_stage}.json")
                self.assert_receipt(gate_id, stage, inputs, [
                    ("prior_gate", gate_label, MATCH), ("evidence", rehearsal_label, MATCH),
                ], "GO", hashes=pair_hash)
                other_engine = json.dumps({"engines": ["b" * 40]}).encode()
                pair.write_bytes(other_engine)
                self.assert_receipt(gate_id, stage, inputs, [
                    ("prior_gate", gate_label, MATCH),
                    ("evidence", rehearsal_label, ("fail", "EVIDENCE_ENGINE_MISMATCH")),
                ], "NO-GO", hashes={"pair": digest(other_engine)})
                pair.write_bytes(pair_bytes)
                prior.rename(self.root / f"{gate_id}-prior.json")
                self.rows.pop(prior.name)
                self.assert_receipt(gate_id, stage, inputs, [
                    ("prior_gate", gate_label, MISSING), ("evidence", rehearsal_label, MATCH),
                ], "NO-GO", hashes=pair_hash)

    def assert_portal(self, inputs, hold, window, prior, verdict, *, now=NOW):
        hashes = {"hold_release": digest(RELEASE)} if hold == RELEASE_PRESENT else {}
        self.assert_receipt(PORTAL, "4", {"evidence": self.evidence, **inputs}, [
            ("file_present", "Operator hold-release record", hold),
            ("window", "Outside run slots (Pacific)", window),
            ("prior_gate", "Engine switch gate", prior),
        ], verdict, now=now, hashes=hashes)

    def test_c1_portal_requires_engine_switch_and_off_slot_window(self):
        """C1: a hold release alone cannot replace the engine gate or open the run slots."""
        release = self.root / "release"
        release.write_bytes(RELEASE)
        inputs = {"hold_release": release}
        self.assert_portal(inputs, RELEASE_PRESENT, WINDOW_OPEN, MISSING, "NO-GO")
        self.record("15", kind="gate", name="s5-engine-switch.json")
        for hour, minute in ((7, 0), (13, 0), (13, 59), (18, 0), (20, 30)):
            with self.subTest(hour=hour, minute=minute):
                self.assert_portal(inputs, RELEASE_PRESENT, ("fail", "GATE_WINDOW_CLOSED"), MATCH,
                                   "NO-GO", now=NOW.replace(hour=hour, minute=minute))
        self.assert_portal(inputs, RELEASE_PRESENT, WINDOW_OPEN, MATCH, "GO")

    def test_c3_omitted_or_unusable_hold_release_is_no_go(self):
        """C3: even an open window and passing prerequisite cannot substitute for the copied file."""
        self.record("15", kind="gate", name="s5-engine-switch.json")
        release = self.root / "release"
        release.write_bytes(RELEASE)
        link = self.root / "link"
        link.symlink_to(release)
        too_large = self.root / "too-large"
        with too_large.open("wb") as stream:
            stream.truncate(golive_gate.MAX_INPUT + 1)
        for inputs in ({}, {"unrelated": release}, *({"hold_release": path} for path in (
                self.root / "missing", self.evidence, link, too_large))):
            with self.subTest(inputs=inputs):
                self.assert_portal(inputs, RELEASE_MISSING, WINDOW_OPEN, MATCH, "NO-GO")
