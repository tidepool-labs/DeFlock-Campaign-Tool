"""Read-only specs for four attended steps; a GO receipt is not authorization."""

from copy import deepcopy
from typing import Any

from .golive_gate import DAYS, SPEC_SCHEMA, load_spec

# Match the rehearsal receipt's default qualification horizon.
_DEFAULT_MAX_AGE_DAYS = 14


def _prior(gate_id, label):
    return dict(type="prior_gate", label=label, gate_id=gate_id,
                max_age_days=_DEFAULT_MAX_AGE_DAYS)


def _rehearsal(stage, label):
    return dict(type="evidence", label=label, stage=stage, kind="rehearsal",
                engine_input="pair", max_age_days=_DEFAULT_MAX_AGE_DAYS)


def _gate(gate_id, stage, title, *checks) -> dict[str, Any]:
    return dict(schema=SPEC_SCHEMA, gate_id=gate_id, stages=[stage], title=title, checks=list(checks))


GATES = {definition["gate_id"]: definition for definition in (
    _gate(
        "c6-findings-on", "16", "Findings lane switch-on",
        _prior("s6a-trust-mount", "Trust mount gate"),
        _rehearsal("16", "Findings lane rehearsal"),
    ),
    _gate(
        "l17-packets", "17", "Packets lane switch-on",
        _prior("c6-findings-on", "Findings lane gate"),
        _rehearsal("17", "Packets lane rehearsal"),
    ),
    _gate(
        "c5-portal-apply", "4", "Portal apply",
        dict(type="file_present", label="Operator hold-release record", input="hold_release"),
        # Use the conservative attended afternoon window between run slots.
        # It does not prove that the preceding slot ran cleanly.
        dict(type="window", label="Outside run slots (Pacific)",
             days=list(DAYS), start="14:00", end="18:00"),
        _prior("s5-engine-switch", "Engine switch gate"),
    ),
    _gate(
        "a4-hostname", "A4", "Public admin hostname",
        _prior("s10-console", "Console gate"),
        _rehearsal("A3", "New console rehearsal"),
    ),
)}


def spec(gate_id, **pins):
    """Return a validated copy, with an optional operator-pinned evidence age.

    Supply copied evidence, pair, and hold-release inputs to the evaluator,
    not here. The portal hold-release check cannot be removed by a pin.
    """
    definition = deepcopy(GATES[gate_id])
    max_age_days = pins.pop("max_age_days", _DEFAULT_MAX_AGE_DAYS)
    if pins:
        raise TypeError(f"unrecognized gate pins: {', '.join(sorted(pins))}")
    for check in definition["checks"]:
        if "max_age_days" in check:
            check["max_age_days"] = max_age_days
    return load_spec(definition)
