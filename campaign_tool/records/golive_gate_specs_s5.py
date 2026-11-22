"""Read-only gate templates for the Sitting 5 engine, backup and units.

Supply the approved candidate wrapper/LIVE hashes, not hashes learned from the
captures being checked. Supply `pair` as copied golive_pair JSON for the engine
that will perform the step (the candidate for the switch and S8, the pre-switch
engine for S5b). Read back that the report came from the intended pair, is
consistent, and names the full engine commit. These specs do not run golive_pair.

Required attended readback: confirm today's booked date in the SITTING6/engine
packets. The Thursday time check is evidence only within that separately enforced
booking. It does not encode a date, rebook a slot or lift a hold.

Exact receipt-ID binding in golive_gate/golive_record is a known evaluator gap
for the owner's roadmap, not covered by this pack. Use the registered gate IDs
as receipt basenames; the registry's prefix check only disambiguates real IDs.
A receipt never performs or authorizes a step.
"""

from copy import deepcopy

from .golive_gate import SPEC_SCHEMA, load_spec

_ENGINE_SWITCH = "s5-engine-switch"
# The plan's 40 GB threshold is in decimal bytes; the evaluator takes GiB.
_DISK_FLOOR = 40_000_000_000 / (1 << 30)


def _rehearsal(stage, label):
    # Keep rehearsal proof fresh across the working week; the predecessor
    # below is intended for the same sitting, not a previous week's GO.
    return {"type": "evidence", "label": label, "stage": stage, "kind": "rehearsal",
            "max_age_days": 7, "engine_input": "pair"}


GATES = {
    _ENGINE_SWITCH: {
        "schema": SPEC_SCHEMA,
        "gate_id": _ENGINE_SWITCH,
        "stages": ["15"],
        "title": "Sitting 5 engine switch",
        "checks": [
            {"type": "sha256_pin", "label": "Candidate run-records.sh", "input": "wrapper", "expected": None},
            {"type": "sha256_pin", "label": "Candidate LIVE.json", "input": "live", "expected": None},
            {"type": "disk_floor_gib", "label": "Free space before the switch", "input": "disk", "min": _DISK_FLOOR},
            {"type": "window", "label": "Thursday Pacific switch slot (booking confirmed separately)",
             "days": ["thu"], "start": "10:00", "end": "10:10"},
            _rehearsal("15", "Candidate engine switch rehearsal"),
        ],
    },
    "s5b-backup": {
        "schema": SPEC_SCHEMA,
        "gate_id": "s5b-backup",
        "stages": ["19"],
        "title": "S5b D2-A pre-switch backup (not Part 2)",
        "checks": [
            {"type": "file_present", "label": "Writer quiescence evidence copy", "input": "quiescence"},
            {"type": "disk_floor_gib", "label": "Free space for pre-switch backup", "input": "disk", "min": _DISK_FLOOR},
            _rehearsal("19", "Backup rehearsal"),
        ],
    },
    "s8-units": {
        "schema": SPEC_SCHEMA,
        "gate_id": "s8-units",
        "stages": ["3", "18"],
        "title": "S8 arrival and monitoring units",
        "checks": [
            {"type": "prior_gate", "label": "Sitting 5 engine-switch GO receipt", "gate_id": _ENGINE_SWITCH,
             "max_age_days": 1},
            _rehearsal("3", "Arrival rehearsal"),
            _rehearsal("18", "Monitoring rehearsal"),
        ],
    },
}


def spec(gate_id, **pins):
    """Return a detached valid spec; pin keywords are the SHA check input names.

    The switch requires `wrapper` and `live`; the other gates take no pins.
    Unknown gate IDs raise KeyError. Missing, extra or invalid pins raise ValueError.
    """
    value = deepcopy(GATES[gate_id])
    pinned = {check["input"]: check for check in value["checks"] if check["type"] == "sha256_pin"}
    if pins.keys() != pinned.keys():
        raise ValueError(f"{gate_id} requires exactly these pins: {', '.join(sorted(pinned)) or '(none)'}")
    for name, check in pinned.items():
        check["expected"] = pins[name]
    return load_spec(value)
