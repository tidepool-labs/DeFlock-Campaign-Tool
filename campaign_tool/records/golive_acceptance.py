"""Read-only list of what still stands between the go-live ledger and goal acceptance."""

import argparse
import json
import sys
from datetime import datetime, timezone

from . import golive_catalog, golive_evidence, golive_ledger, golive_stages


# Human-only M8 items. This module never checks, performs or schedules them.
MANUAL = (
    "rollback drill",
    "FLAWLESS check",
    "stages 7 and 8 proven on new documents",
)


def evaluate(rows):
    """Derive blockers from ledger rows and the catalogue flags; console-only rows never count.

    Raises ValueError for a row that is malformed, names a stage outside the catalogue, repeats a
    stage, disagrees with the catalogue on pipeline membership, or when a catalogue pipeline stage
    has no row, so an incomplete ledger can never read as ready.
    """
    order = {stage: index for index, stage in enumerate(golive_catalog.STAGES)}
    rows = list(rows)
    live = total = 0
    found = []
    seen = set()
    try:
        for row in rows:
            stage = row["stage"]
            if stage not in order:
                raise ValueError(f"unknown stage {stage!r}")
            if stage in seen:
                raise ValueError(f"duplicate stage {stage!r}")
            seen.add(stage)
            if row["pipeline"] is not golive_catalog.STAGES[stage].pipeline:
                raise ValueError(f"stage {stage!r} pipeline flag disagrees with the catalogue")
        missing = [stage for stage, rule in golive_catalog.STAGES.items() if rule.pipeline and stage not in seen]
        if missing:
            raise ValueError(f"missing pipeline stages: {', '.join(missing)}")
        for row in rows:
            stage = row["stage"]
            if not row["pipeline"]:
                continue
            rule = golive_catalog.STAGES[stage]
            total += 1
            codes = []
            if row["state"] != "LIVE":
                codes.append("NOT_LIVE")
            else:
                live += 1
            if rule.needs_rehearsal and row["rehearsed"]["status"] != "pass":
                codes.append("REHEARSAL_NOT_PASS")
            if rule.needs_gate and row["gate"]["status"] != "pass":
                codes.append("GATE_NOT_PASS")
            found.extend((order[stage], dict(stage=stage, code=code)) for code in codes)
    except (KeyError, TypeError) as exc:
        raise ValueError(f"invalid ledger row: {exc!r}") from exc
    # Stable sort keeps the NOT_LIVE, REHEARSAL, GATE order within a stage.
    found.sort(key=lambda item: item[0])
    return dict(live=live, total=total, blockers=[blocker for _, blocker in found], manual=list(MANUAL))


def _verdict(result):
    count = len(result["blockers"])
    return "READY FOR HUMAN ACCEPTANCE" if not count else f"NOT READY: {count} blockers"


def render(result):
    lines = [f"{result['live']} of {result['total']} pipeline stages live"]
    grouped = {}
    for blocker in result["blockers"]:
        grouped.setdefault(blocker["stage"], []).append(blocker["code"])
    lines.append("Blockers:" if grouped else "Blockers: none")
    for stage, codes in grouped.items():
        lines.append(f"  {stage} {golive_catalog.STAGES[stage].label}: {', '.join(codes)}")
    lines.append("Human-only items (not machine-checked):")
    lines.extend(f"  - {item}" for item in result["manual"])
    lines.append(_verdict(result))
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reports", required=True)
    parser.add_argument("--evidence", required=True)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    now = datetime.now(timezone.utc)
    try:
        reports = golive_stages.load_reports(args.reports, now=now)
        evidence = golive_evidence.load(args.evidence)
        result = evaluate(golive_ledger.build(reports, evidence, now))
    except (ValueError, OSError) as exc:
        print(f"golive_acceptance: {exc}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(dict(result, summary=f"{result['live']} of {result['total']} pipeline stages live",
                              verdict=_verdict(result)), indent=2))
    else:
        print(render(result))
    return 1 if result["blockers"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
