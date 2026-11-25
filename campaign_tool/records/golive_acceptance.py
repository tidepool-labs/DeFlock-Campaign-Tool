"""Read-only list of what still stands between the go-live ledger and goal acceptance."""

import argparse
import json
import sys
from datetime import datetime, timezone

from . import golive_catalog, golive_evidence, golive_ledger, golive_rollback_check, golive_stages, rehearsal_receipt


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


DRILL = MANUAL[0]


def rollback_drill(path, now):
    """Machine-check a stage 15 receipt: PASS only for a passed drill on a qualified receipt.

    Any drill code or qualification code makes the status FAIL and is named in codes. Raises
    ValueError, with no path or receipt content in the message, when the receipt cannot be read.
    """
    try:
        verdict = golive_rollback_check.check(rehearsal_receipt.read(path), now)
    except (OSError, ValueError):
        raise ValueError("unreadable or invalid rehearsal receipt") from None
    passed = verdict["drill"] == "PASS" and verdict["qualified"]
    return dict(status="PASS" if passed else "FAIL", codes=verdict["drill_codes"] + verdict["qualify_codes"])


def with_rollback(result, rollback):
    """Attach the machine-checked drill; only a PASS takes it off the human-only list."""
    manual = [item for item in result["manual"] if item != DRILL] if rollback["status"] == "PASS" else result["manual"]
    return dict(result, rollback=rollback, manual=manual)


def _rollback_failed(result):
    return result.get("rollback", {}).get("status") == "FAIL"


def _verdict(result):
    count = len(result["blockers"])
    failed = _rollback_failed(result)
    if not count and not failed:
        return "READY FOR HUMAN ACCEPTANCE"
    parts = ([f"{count} blockers"] if count else []) + (["rollback drill FAIL"] if failed else [])
    return "NOT READY: " + ", ".join(parts)


def render(result):
    lines = [f"{result['live']} of {result['total']} pipeline stages live"]
    grouped = {}
    for blocker in result["blockers"]:
        grouped.setdefault(blocker["stage"], []).append(blocker["code"])
    lines.append("Blockers:" if grouped else "Blockers: none")
    for stage, codes in grouped.items():
        lines.append(f"  {stage} {golive_catalog.STAGES[stage].label}: {', '.join(codes)}")
    if "rollback" in result:
        codes = result["rollback"]["codes"]
        lines.append(f"Rollback drill (machine-checked): {result['rollback']['status']}"
                     + (f" ({', '.join(codes)})" if codes else ""))
    lines.append("Human-only items (not machine-checked):")
    lines.extend(f"  - {item}" for item in result["manual"])
    lines.append(_verdict(result))
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reports", required=True)
    parser.add_argument("--evidence", required=True)
    parser.add_argument("--rollback-receipt", help="stage 15 rehearsal receipt to machine-check the rollback drill")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    now = datetime.now(timezone.utc)
    try:
        reports = golive_stages.load_reports(args.reports, now=now)
        evidence = golive_evidence.load(args.evidence)
        result = evaluate(golive_ledger.build(reports, evidence, now))
        if args.rollback_receipt is not None:
            result = with_rollback(result, rollback_drill(args.rollback_receipt, now))
    except (ValueError, OSError) as exc:
        print(f"golive_acceptance: {exc}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(dict(result, summary=f"{result['live']} of {result['total']} pipeline stages live",
                              verdict=_verdict(result)), indent=2))
    else:
        print(render(result))
    return 1 if result["blockers"] or _rollback_failed(result) else 0


if __name__ == "__main__":
    raise SystemExit(main())
