"""List the proof requirements for pipeline and console go-live rows."""

import argparse
import json
from dataclasses import asdict, dataclass
from types import MappingProxyType
from typing import Literal, Mapping

from . import golive_stages


@dataclass(frozen=True)
class Row:
    label: str
    pipeline: bool
    needs_rehearsal: bool
    needs_gate: bool
    live_source: Literal["run_report", "evidence"]
    owner: Literal["pipeline", "operator", "G", "flock-coordinator"]


# This inventory follows the go-live status table, including off-path rows.
_OWNERS = {
    "1": "G",
    "2": "pipeline",
    "3": "operator",
    "4": "operator",
    "5": "flock-coordinator",
    "6": "flock-coordinator",
    "7": "pipeline",
    "8": "pipeline",
    "9": "pipeline",
    "10": "pipeline",
    "11": "pipeline",
    "12": "pipeline",
    "13": "pipeline",
    "14": "pipeline",
    "15": "operator",
    "16": "operator",
    "17": "operator",
    "17b": "operator",
    "18": "operator",
    "19": "operator",
    "20": "operator",
    "A1": "operator",
    "A2": "pipeline",
    "A3": "operator",
    "A4": "G",
    "A5": "operator",
    "A6": "operator",
    "A7": "operator",
}
_CONSOLE_ONLY = frozenset({"A1", "A3", "A4", "A5", "A6", "A7"})
_NO_REHEARSAL = frozenset({"1", "17b"})
_ATTENDED = frozenset({"3", "4", "15", "16", "17", "18", "19", "20", "A3", "A4", "A5", "A6"})
# V1 has only historical OCR counts, no engine-switch receipt, and no findings stage.
_SEPARATE_EVIDENCE = frozenset({"6", "15", "16"})


def _row(stage_id, owner):
    report_stage = golive_stages.Stage("Public site") if stage_id == "17b" else golive_stages.STAGES[stage_id]
    live_source = (
        "run_report" if report_stage.source is not None and stage_id not in _SEPARATE_EVIDENCE else "evidence"
    )
    return Row(
        label=report_stage.label,
        pipeline=stage_id not in _CONSOLE_ONLY,
        needs_rehearsal=stage_id not in _NO_REHEARSAL,
        needs_gate=stage_id in _ATTENDED,
        live_source=live_source,
        owner=owner,
    )


STAGES: Mapping[str, Row] = MappingProxyType({stage_id: _row(stage_id, owner) for stage_id, owner in _OWNERS.items()})


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="print the catalogue as a JSON object keyed by stage ID")
    args = parser.parse_args(argv)
    if args.json:
        print(json.dumps({stage_id: asdict(row) for stage_id, row in STAGES.items()}, indent=2))
    else:
        print("Stage | Label | Pipeline | Rehearsal | Attended gate | Live source | Owner")
        for stage_id, row in STAGES.items():
            print(f"{stage_id} | {row.label} | {'yes' if row.pipeline else 'no'} | "
                  f"{'yes' if row.needs_rehearsal else 'no'} | {'yes' if row.needs_gate else 'no'} | "
                  f"{row.live_source} | {row.owner}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
