"""Synthetic evaluation of lane gate packs; no attended step is performed."""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from zoneinfo import ZoneInfo

from campaign_tool.records import (
    golive_catalog, golive_gate, golive_gate_specs_lanes, golive_gate_specs_s5, golive_gate_specs_s6,
    golive_record, rehearse,
)
from campaign_tool.records.golive_gate_specs_lanes import GATES, spec


PORTAL = "c5-portal-apply"
PACIFIC = ZoneInfo("America/Los_Angeles")
NOW = datetime(2025, 2, 5, 14, tzinfo=PACIFIC)
RECORDED = (NOW - timedelta(days=1)).astimezone(timezone.utc)
ENGINE = "a" * 40
RELEASE = b"synthetic operator hold-release record\n"
PAIR = json.dumps({"engines": [f"engine-{ENGINE}"]}).encode()
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
                {"type": "evidence", "label": "Portal rehearsal", "stage": "4", "kind": "rehearsal",
                 "engine_input": "pair", "max_age_days": 14},
            ],
        }
        candidate = spec(PORTAL, max_age_days=2)
        pinned = {**expected, "checks": [*expected["checks"][:2],
                                        *({**check, "max_age_days": 2} for check in expected["checks"][2:])]}
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

    def record(self, stage, *, kind, name, recorded=RECORDED):
        row = {
            "schema": "records-golive-evidence-v1", "stage": stage, "kind": kind, "result": "pass",
            "engine": ENGINE if kind == "rehearsal" else None,
            "actor": "rehearsal" if kind == "rehearsal" else "operator", "codes": [],
            "recorded_at": recorded.strftime("%Y-%m-%dT%H:%M:%SZ"),
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

    def portal_inputs(self, *, rehearsed=True, recorded=RECORDED):
        """Copy the hold release and engine pair; record the stage 4 rehearsal unless told not to."""
        release = self.root / "release"
        release.write_bytes(RELEASE)
        pair = self.root / "pair.json"
        pair.write_bytes(PAIR)
        if rehearsed:
            self.record("4", kind="rehearsal", name="rehearse-4.json", recorded=recorded)
        return {"hold_release": release, "pair": pair}

    def assert_portal(self, inputs, hold, window, prior, verdict, *, now=NOW, rehearsal=MATCH):
        hashes = {"hold_release": digest(RELEASE)} if hold == RELEASE_PRESENT else {}
        if "pair" in inputs and rehearsal != MISSING:
            hashes["pair"] = digest(PAIR)
        self.assert_receipt(PORTAL, "4", {"evidence": self.evidence, **inputs}, [
            ("file_present", "Operator hold-release record", hold),
            ("window", "Outside run slots (Pacific)", window),
            ("prior_gate", "Engine switch gate", prior),
            ("evidence", "Portal rehearsal", rehearsal),
        ], verdict, now=now, hashes=hashes)

    def test_c1_portal_requires_engine_switch_and_off_slot_window(self):
        """C1: a hold release alone cannot replace the engine gate or open the run slots."""
        inputs = self.portal_inputs()
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
        base = self.portal_inputs()
        release = base.pop("hold_release")
        link = self.root / "link"
        link.symlink_to(release)
        too_large = self.root / "too-large"
        with too_large.open("wb") as stream:
            stream.truncate(golive_gate.MAX_INPUT + 1)
        for inputs in (base, {**base, "unrelated": release}, *({**base, "hold_release": path} for path in (
                self.root / "missing", self.evidence, link, too_large))):
            with self.subTest(inputs=inputs):
                self.assert_portal(inputs, RELEASE_MISSING, WINDOW_OPEN, MATCH, "NO-GO")

    def test_dfs2312_c1_portal_needs_a_qualified_stage_4_rehearsal(self):
        """DFS1123-2312-03 C1: hold release, open window and engine gate cannot stand in for the rehearsal.

        Fails if c5-portal-apply drops its stage 4 rehearsal check (GO with no rehearsal evidence),
        accepts a rehearsal of another stage, or accepts one run on a different engine.
        """
        self.record("15", kind="gate", name="s5-engine-switch.json")
        inputs = self.portal_inputs(rehearsed=False)
        self.assert_portal(inputs, RELEASE_PRESENT, WINDOW_OPEN, MATCH, "NO-GO", rehearsal=MISSING)
        self.record("3", kind="rehearsal", name="rehearse-3.json")
        self.assert_portal(inputs, RELEASE_PRESENT, WINDOW_OPEN, MATCH, "NO-GO", rehearsal=MISSING)
        self.record("4", kind="rehearsal", name="rehearse-4.json")
        self.assert_portal(inputs, RELEASE_PRESENT, WINDOW_OPEN, MATCH, "GO")
        other_engine = json.dumps({"engines": ["b" * 40]}).encode()
        inputs["pair"].write_bytes(other_engine)
        self.assert_receipt(PORTAL, "4", {"evidence": self.evidence, **inputs}, [
            ("file_present", "Operator hold-release record", RELEASE_PRESENT),
            ("window", "Outside run slots (Pacific)", WINDOW_OPEN),
            ("prior_gate", "Engine switch gate", MATCH),
            ("evidence", "Portal rehearsal", ("fail", "EVIDENCE_ENGINE_MISMATCH")),
        ], "NO-GO", hashes={"hold_release": digest(RELEASE), "pair": digest(other_engine)})

    def test_dfs2312_c2_age_pin_reaches_the_portal_rehearsal(self):
        """DFS1123-2312-03 C2: a pinned max age rejects an old portal rehearsal that the default accepts.

        Fails if spec() applies the pin to the prior gate only, leaving the new check at 14 days.
        """
        self.record("15", kind="gate", name="s5-engine-switch.json")
        inputs = self.portal_inputs(recorded=(NOW - timedelta(days=3)).astimezone(timezone.utc))
        self.assert_portal(inputs, RELEASE_PRESENT, WINDOW_OPEN, MATCH, "GO")
        pinned = golive_gate.evaluate(spec(PORTAL, max_age_days=2), {"evidence": self.evidence, **inputs}, NOW)
        self.assertEqual(pinned["verdict"], "NO-GO")
        self.assertEqual([(check["label"], check["state"], check["code"]) for check in pinned["checks"]], [
            ("Operator hold-release record", "pass", "GATE_FILE_PRESENT"),
            ("Outside run slots (Pacific)", "pass", "GATE_WINDOW_OPEN"),
            ("Engine switch gate", "pass", "EVIDENCE_MATCH"),
            ("Portal rehearsal", "fail", "EVIDENCE_STALE"),
        ])


def missing_rehearsals(packs, drivers):
    """List (gate, stage) pairs whose stage needs a rehearsal and has a driver but no rehearsal check."""
    covered = {stage for driver in drivers for stage in driver.STAGES}
    return [(gate_id, stage) for pack in packs for gate_id, gate in pack.GATES.items() for stage in gate["stages"]
            if golive_catalog.STAGES[stage].needs_rehearsal and stage in covered
            and not any(check["type"] == "evidence" and check.get("kind") == "rehearsal"
                        and check.get("stage") == stage for check in gate["checks"])]


class RehearsalInvariantTests(unittest.TestCase):
    PACKS = (golive_gate_specs_s5, golive_gate_specs_s6, golive_gate_specs_lanes)

    def test_dfs2312_c3_every_driven_stage_has_a_rehearsal_check(self):
        """DFS1123-2312-03 C3: a gate cannot say GO for a stage whose rehearsal has a driver but no check.

        Fails if a new or edited gate omits the rehearsal check (as c5-portal-apply once did).
        The only gate stage with no driver, A4, is skipped because nothing covers it, not by a list.
        """
        drivers = rehearse.discover()
        self.assertEqual(missing_rehearsals(self.PACKS, drivers), [])
        covered = {stage for driver in drivers for stage in driver.STAGES}
        stages = {stage for pack in self.PACKS for gate in pack.GATES.values() for stage in gate["stages"]}
        self.assertIn("4", stages & covered)
        self.assertEqual(sorted(stages - covered), ["A4"])

    def test_dfs2312_c3_invariant_is_red_when_the_check_is_removed(self):
        """DFS1123-2312-03 C3: the invariant names the gate and stage when a pack copy loses its check."""
        drivers = rehearse.discover()
        removed = 0
        for pack in self.PACKS:
            baseline = missing_rehearsals([pack], drivers)
            for gate_id, gate in pack.GATES.items():
                for index, check in enumerate(gate["checks"]):
                    if (check["type"], check.get("kind")) != ("evidence", "rehearsal") \
                            or check["stage"] not in gate["stages"]:
                        continue
                    with self.subTest(gate=gate_id, stage=check["stage"]):
                        gates = deepcopy(pack.GATES)
                        del gates[gate_id]["checks"][index]
                        copy = SimpleNamespace(GATES=gates)
                        self.assertEqual(sorted(missing_rehearsals([copy], drivers)),
                                         sorted([*baseline, (gate_id, check["stage"])]))
                        removed += 1
        self.assertGreaterEqual(removed, 8)
