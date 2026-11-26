"""Copied-evidence gates for Sitting 6 Parts A/B and S10; GO is not authorization.

Independent attended checklist (SITTING6-PACKET.md and SITTING6-PLAN §2):
* Part A: verify the approved pair verdict binds both captures (A-G1/A-G4).
  Byte pins compare the copies; the rehearsal engine check reads the reported
  engine, not the pair verdict or its binding to those copies. Confirm the
  clean scheduled run, exact kit approval, recipe and anchor metadata, fresh
  attended space budget (measurement age, pending backup, then backup-done
  recheck), retained-container identity, booked window, quiescence and owner
  presence. The numeric floor check does not verify the budget or its age.
* Part B: require the separate attended A12 `anchor ok` readback and B2
  recheck. Neither a Part A preflight GO nor stage 20 rehearsal proves that
  the anchor is mounted. Confirm Q restored, the approved C9 kit and base,
  the active loopback console without a drop-in, a decidable item, slot
  clearance and the signer present (B-G1 through B-G6).
* S10: S9 must have a qualifying real draft and a fresh attended precheck.
  An A3 rehearsal does not prove either or that the console was installed.

For every prior_gate, independently verify the named predecessor receipt;
the shared evaluator matches its ledger source-name prefix, not its exact ID.
"""

from copy import deepcopy
from typing import Any

from .golive_gate import SPEC_SCHEMA, load_spec

_DEFAULT_MAX_AGE_DAYS = 14  # Default rehearsal qualification horizon.


def _prior(gate_id, label):
    return dict(type="prior_gate", label=label, gate_id=gate_id, max_age_days=_DEFAULT_MAX_AGE_DAYS)


def _rehearsal(stage, label):
    return dict(type="evidence", label=label, stage=stage, kind="rehearsal",
                engine_input="pair", max_age_days=_DEFAULT_MAX_AGE_DAYS)


def _gate(gate_id, stages, title, *checks) -> dict[str, Any]:
    return dict(schema=SPEC_SCHEMA, gate_id=gate_id, stages=stages, title=title, checks=list(checks))


GATES = {definition["gate_id"]: definition for definition in (
    _gate(
        "s6a-trust-mount", ["20"], "Sitting 6 Part A: trust mount preflight",
        _prior("s5-engine-switch", "Engine switch gate"),
        dict(type="sha256_pin", label="Approved wrapper byte pin", input="run_records", expected=None),
        dict(type="sha256_pin", label="Approved LIVE.json byte pin", input="live_json", expected=None),
        dict(type="disk_floor_gib", label="Available space against supplied floor", input="space", min=None),
        _rehearsal("20", "Trust mount rehearsal"),
    ),
    _gate(
        "s6b-decisions", ["A5", "A6"], "Sitting 6 Part B: decisions and first signed approval",
        _prior("s6a-trust-mount", "Trust mount preflight gate"),
        _rehearsal("A5", "Decisions rehearsal"),
        _rehearsal("A6", "Signing rehearsal"),
    ),
    _gate(
        "s10-console", ["A3"], "S10: console install preflight",
        _prior("s5-engine-switch", "Engine switch gate"),
        _rehearsal("A3", "Console rehearsal"),
    ),
)}


def spec(gate_id, **pins):
    """Return a validated copy, using only operator-supplied live pins.

    Part A requires run_records_sha256, live_json_sha256 and disk_floor_gib.
    Derive the floor in GiB from the attended budget, including the pending
    backup when applicable; do not round down. All three gates accept an
    optional max_age_days. Pass the copied inputs separately to the evaluator.
    """
    definition = deepcopy(GATES[gate_id])
    max_age_days = pins.pop("max_age_days", _DEFAULT_MAX_AGE_DAYS)
    for check in definition["checks"]:
        if check["type"] == "sha256_pin":
            check["expected"] = pins.pop(f'{check["input"]}_sha256', None)
        elif check["type"] == "disk_floor_gib":
            check["min"] = pins.pop("disk_floor_gib", None)
        else:
            check["max_age_days"] = max_age_days
    if pins:
        raise TypeError(f"unrecognized gate pins: {', '.join(sorted(pins))}")
    return load_spec(definition)
