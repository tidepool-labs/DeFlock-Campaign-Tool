"""Exercise the Sitting 6/S10 gate packs against synthetic copied inputs."""

from copy import deepcopy
from datetime import timedelta
from hashlib import sha256
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from campaign_tool.records import golive_gate as gate, golive_gate_specs_s6 as pack
from tests.records.test_golive_gate import GateFixture, NOW, receipt, result

A, B, S10 = "s6a-trust-mount", "s6b-decisions", "s10-console"
ENGINE = "a" * 40
PASSED = ("pass", "EVIDENCE_MATCH")
MISSING = ("unknown", "EVIDENCE_MISSING")
UNREADABLE = ("unknown", "EVIDENCE_UNREADABLE")
FAILED = ("fail", "EVIDENCE_FAILED")
STALE = ("fail", "EVIDENCE_STALE")
COPY_MISSING = ("unknown", "GATE_INPUT_MISSING")
COPIES = {
    "run_records": b"synthetic wrapper\n",
    "live_json": b'{"synthetic": true}\n',
    "space": b"Avail\n1610612736\n",
    "pair": json.dumps({"engines": [f"engine-{ENGINE}"]}).encode(),
}
PINS = {"run_records_sha256": sha256(COPIES["run_records"]).hexdigest(),
        "live_json_sha256": sha256(COPIES["live_json"]).hexdigest(), "disk_floor_gib": 1.5}
REQUIREMENTS = {
    A: ("s5-engine-switch", "15", ("20",)),
    B: (A, "20", ("A5", "A6")),
    S10: ("s5-engine-switch", "15", ("A3",)),
}


def expected_spec(gate_id, max_age=14) -> dict[str, Any]:
    predecessor, _, stages = REQUIREMENTS[gate_id]
    prior_label = "Trust mount preflight gate" if gate_id == B else "Engine switch gate"
    checks = [dict(type="prior_gate", label=prior_label, gate_id=predecessor, max_age_days=max_age)]
    if gate_id == A:
        checks += [
            dict(type="sha256_pin", label="Approved wrapper byte pin", input="run_records",
                 expected=PINS["run_records_sha256"]),
            dict(type="sha256_pin", label="Approved LIVE.json byte pin", input="live_json",
                 expected=PINS["live_json_sha256"]),
            dict(type="disk_floor_gib", label="Available space against supplied floor", input="space", min=1.5),
        ]
    labels = {"20": "Trust mount rehearsal", "A5": "Decisions rehearsal",
              "A6": "Signing rehearsal", "A3": "Console rehearsal"}
    checks.extend(dict(type="evidence", label=labels[stage], stage=stage, kind="rehearsal",
                       max_age_days=max_age, engine_input="pair") for stage in stages)
    titles = {A: "Sitting 6 Part A: trust mount preflight",
              B: "Sitting 6 Part B: decisions and first signed approval", S10: "S10: console install preflight"}
    return dict(schema="records-golive-gate-spec-v1", gate_id=gate_id, stages=list(stages),
                title=titles[gate_id], checks=checks)


class Sitting6GateTests(GateFixture):
    def test_c1_pack_has_exact_footprints(self):
        """C1: a wrong predecessor, row, copy, or byte floor changes the returned spec."""
        self.assertEqual(set(pack.GATES), set(REQUIREMENTS))
        for gate_id in REQUIREMENTS:
            with self.subTest(gate_id=gate_id):
                self.assertEqual(pack.spec(gate_id, **(PINS if gate_id == A else {})), expected_spec(gate_id))

    def test_c2_operator_overrides_and_isolation(self):
        """C2: operator pins reach every check without changing subsequent builds."""
        for gate_id in REQUIREMENTS:
            with self.subTest(gate_id=gate_id):
                pins = dict(run_records_sha256="b" * 64, live_json_sha256="c" * 64,
                            disk_floor_gib=2.25) if gate_id == A else {}
                expected = expected_spec(gate_id, max_age=2)
                if gate_id == A:
                    expected["checks"][1]["expected"] = pins["run_records_sha256"]
                    expected["checks"][2]["expected"] = pins["live_json_sha256"]
                    expected["checks"][3]["min"] = 2.25
                built = pack.spec(gate_id, max_age_days=2, **pins)
                self.assertEqual(built, expected)
                built["checks"].clear()
                built["stages"].clear()
                self.assertEqual(pack.spec(gate_id, **(PINS if gate_id == A else {})), expected_spec(gate_id))

    def test_c2_refusals(self):
        """C2: typos and absent or invalid operator values cannot silently default."""
        cases = [("not-a-gate", {}, KeyError), (A, dict(PINS, extra=1), TypeError),
                 (B, PINS, TypeError), (S10, {"max_age_days": -1}, ValueError)]
        for name in PINS:
            cases += [(A, {k: v for k, v in PINS.items() if k != name}, ValueError),
                      (A, dict(PINS, **{name: "invalid"}), ValueError)]
        for gate_id, pins, raised in cases:
            with self.subTest(gate_id=gate_id, pins=pins), self.assertRaises(raised) as caught:
                pack.spec(gate_id, **pins)
            self.assertIs(type(caught.exception), raised)

    def records(self, gate_id):
        prior, prior_stage, stages = REQUIREMENTS[gate_id]
        rows = []
        for stage, kind, name in [(prior_stage, "gate", prior), *((s, "rehearsal", f"row-{s}") for s in stages)]:
            rows.append(dict(
                schema="records-golive-evidence-v1", stage=stage, kind=kind, result="pass",
                engine=ENGINE if kind == "rehearsal" else None,
                recorded_at=(NOW - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                source=dict(schema="records-rehearsal-v1" if kind == "rehearsal" else "records-golive-gate-v1",
                            name=name + ".json", sha256=sha256(name.encode()).hexdigest()),
                actor="rehearsal" if kind == "rehearsal" else "operator", codes=[],
            ))
        return rows

    def assert_evaluation(self, gate_id, rows, outcomes, *, copies=None, ledger="ok"):
        if copies is None:
            copies = {k: v for k, v in COPIES.items() if gate_id == A or k == "pair"}
        inputs = {name: self.fixture(name, raw) for name, raw in copies.items()}
        hashes = {name: sha256(raw).hexdigest() for name, raw in copies.items()}
        # No pair bytes are consulted when all rehearsals short-circuit at the ledger.
        evidence_indexes = (4,) if gate_id == A else range(1, len(REQUIREMENTS[gate_id][2]) + 1)
        if ledger != "ok" or all(outcomes.get(i) in (MISSING, FAILED, STALE) for i in evidence_indexes):
            hashes.pop("pair", None)
        if ledger == "ok":
            # Each replay gets a separate ledger so removed rows cannot survive a case.
            with TemporaryDirectory(dir=self.root) as directory:
                path = Path(directory)
                by_filename = {}
                for row in rows:
                    stamp = row["recorded_at"].replace("-", "").replace(":", "")
                    name = f'{stamp}-{row["stage"]}-{row["kind"]}-{row["source"]["sha256"][:8]}.json'
                    target = path / name
                    target.write_text(json.dumps(row), encoding="utf-8")
                    target.chmod(0o600)
                    by_filename[name] = row
                canonical = json.dumps([by_filename[name] for name in sorted(by_filename)],
                                       sort_keys=True, separators=(",", ":")).encode("ascii")
                self.compare_receipt(gate_id, dict(inputs, evidence=path),
                                     dict(hashes, evidence=sha256(canonical).hexdigest()), outcomes)
        else:
            if ledger == "unreadable":
                inputs["evidence"] = self.fixture("not-a-ledger", b"not a directory")
            self.compare_receipt(gate_id, inputs, hashes, outcomes)

    def compare_receipt(self, gate_id, inputs, hashes, outcomes):
        expected = expected_spec(gate_id)
        checks = expected["checks"]
        states = [outcomes.get(i, {
            "sha256_pin": ("pass", "GATE_SHA256_MATCH"), "disk_floor_gib": ("pass", "GATE_DISK_SUFFICIENT"),
        }.get(check["type"], PASSED)) for i, check in enumerate(checks)]
        projected = receipt("NO-GO" if outcomes else "GO", [result(c, *s) for c, s in zip(checks, states)], hashes)
        projected.update(gate_id=gate_id, stages=expected["stages"])
        actual = pack.spec(gate_id, **(PINS if gate_id == A else {}))
        self.assertEqual(gate.evaluate(actual, inputs, NOW), projected)

    def test_c3_go_for_each_pack(self):
        """C3: every required synthetic receipt and pinned copy yields the exact GO receipt."""
        for gate_id in REQUIREMENTS:
            with self.subTest(gate_id=gate_id):
                self.assert_evaluation(gate_id, self.records(gate_id), {})

    def test_c3_each_predecessor_and_rehearsal_can_block(self):
        """C3: no unrelated, stale or failed row may substitute for any named prerequisite."""
        for gate_id in REQUIREMENTS:
            rows = self.records(gate_id)
            indexes = (0, 4) if gate_id == A else range(len(rows))
            for i, check in enumerate(indexes):
                for outcome in (MISSING, FAILED, STALE):
                    with self.subTest(gate_id=gate_id, check=check, outcome=outcome):
                        case = deepcopy(rows)
                        if outcome == MISSING:
                            case.pop(i)
                        elif outcome == FAILED:
                            case[i]["result"] = "fail"
                        else:
                            case[i]["recorded_at"] = (NOW - timedelta(days=15)).strftime("%Y-%m-%dT%H:%M:%SZ")
                        self.assert_evaluation(gate_id, case, {check: outcome})

    def test_c3_missing_or_unreadable_ledger(self):
        """C3: omitted and unusable snapshots both block all evidence checks."""
        for gate_id in REQUIREMENTS:
            indexes = (0, 4) if gate_id == A else range(len(self.records(gate_id)))
            for ledger, outcome in (("missing", MISSING), ("unreadable", UNREADABLE)):
                with self.subTest(gate_id=gate_id, ledger=ledger):
                    copies = {k: v for k, v in COPIES.items() if gate_id == A and k != "pair"}
                    self.assert_evaluation(gate_id, [], dict.fromkeys(indexes, outcome), copies=copies, ledger=ledger)

    def test_c3_rehearsal_engine_binding(self):
        """C3: an unreadable or wrong engine copy cannot qualify any rehearsal."""
        wrong_engine = json.dumps({"engines": ["b" * 40]}).encode()
        for gate_id in REQUIREMENTS:
            indexes = (4,) if gate_id == A else range(1, len(self.records(gate_id)))
            for raw, outcome in ((None, UNREADABLE), (b"{", UNREADABLE),
                                 (wrong_engine, ("fail", "EVIDENCE_ENGINE_MISMATCH"))):
                with self.subTest(gate_id=gate_id, raw=raw):
                    copies = {k: v for k, v in COPIES.items() if gate_id == A and k != "pair"}
                    if raw is not None:
                        copies["pair"] = raw
                    self.assert_evaluation(gate_id, self.records(gate_id), dict.fromkeys(indexes, outcome), copies=copies)

    def test_c3_each_copy_and_disk_failure(self):
        """C3: either pair member can veto GO; the floor is inclusive but not rounded down."""
        rows = self.records(A)
        for index, name in enumerate(("run_records", "live_json", "space"), start=1):
            with self.subTest(name=name, cause="missing"):
                copies = {k: v for k, v in COPIES.items() if k != name}
                self.assert_evaluation(A, rows, {index: COPY_MISSING}, copies=copies)
            cases = ((b"changed copy", ("fail", "GATE_SHA256_MISMATCH")),) if name != "space" else (
                (b"Avail\n1610612735\n", ("fail", "GATE_DISK_BELOW_FLOOR")),
                (b"unparsed", ("unknown", "GATE_DISK_UNPARSED")),
            )
            for raw, outcome in cases:
                with self.subTest(name=name, outcome=outcome):
                    self.assert_evaluation(A, rows, {index: outcome}, copies=dict(COPIES, **{name: raw}))
