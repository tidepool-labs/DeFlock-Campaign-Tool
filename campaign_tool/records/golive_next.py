"""List missing go-live evidence. This report does not authorize or schedule work."""

import argparse
import json
import sys
from datetime import datetime, timezone

from . import golive_catalog, golive_evidence, golive_ledger, golive_stages


# Non-fleet rehearsals keep their owners and still precede gate and live work.
_STEPS = {
    "rehearsal": ("rehearse", "rehearsed"),
    "gate": ("gate", "gate"),
    "live": ("go live", "live"),
}
_STAGE_ORDER = {stage: index for index, stage in enumerate(golive_catalog.STAGES)}


def _priority(item):
    action = item["action"]
    if action == "rehearse":
        return 0 if item["owner"] == "pipeline" else 1
    return 2 if action == "gate" else 3


def plan(rows):
    """Project the ledger's first unmet requirement for each non-LIVE row."""
    items = []
    for row in rows:
        if row["state"] == "LIVE":
            continue
        stage, kind = row["stage"], row["next"]
        action, column = _STEPS[kind]
        proof = row[column]
        reason = proof.get("reason") or f"{kind.upper()}_{proof['status'].upper()}"
        items.append(dict(stage=stage, action=action, owner=golive_catalog.STAGES[stage].owner, reason=reason))
    return sorted(items, key=lambda item: (_priority(item), _STAGE_ORDER[item["stage"]]))


_REASON_TEXT = {
    "REHEARSAL_MISSING": "a qualified rehearsal is missing",
    "REHEARSAL_FAIL": "the last rehearsal did not pass",
    "STALE": "the rehearsal is out of date",
    "ENGINE_CHANGED": "the receipt is for another engine",
    "FUTURE_DATED": "the evidence is dated in the future",
    "GATE_MISSING": "a passing gate receipt is missing",
    "GATE_FAIL": "the last gate did not pass",
    "LIVE_MISSING": "live proof is missing",
    "LIVE_FAILED": "the last live check failed",
    "AGGREGATE_ONLY": "the run report does not prove this stage on its own",
    "NOT_POSITIVE": "the run report does not show completed work for this stage",
}
_ACTION_TEXT = {"rehearse": "Rehearse offline", "gate": "Check the gate", "go live": "Record live proof"}


def commands(items):
    """Return one hint per item: the rehearse command, covering gate IDs, or a pointer.

    Read from `rehearse.discover()` and `golive_gate_order.registry()` on every
    call, so a new driver or gate pack appears without editing this file. A hint
    names what to run; it does not run, authorize or schedule anything.
    """
    from . import golive_gate_order, rehearse

    driven = {stage for driver in rehearse.discover() for stage in driver.STAGES}
    gates = {}
    for gate_id, spec in golive_gate_order.registry().items():
        for stage in spec["stages"]:
            gates.setdefault(stage, []).append(gate_id)
    hints = []
    for item in items:
        stage = item["stage"]
        if item["action"] == "rehearse":
            hint = (f"records rehearse --stage {stage} --out <DIR>" if stage in driven
                    else f"no rehearsal driver covers stage {stage}")
        elif item["action"] == "gate":
            hint = (f"gate pack IDs: {', '.join(gates[stage])}" if stage in gates
                    else f"no gate pack covers stage {stage}")
        else:
            hint = "see the stage's evidence adapter"
        hints.append(hint)
    return hints


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reports", required=True)
    parser.add_argument("--evidence", required=True)
    parser.add_argument("--json", action="store_true", help="print the ordered list as JSON")
    parser.add_argument("--owner", help="show only this catalogue owner (exact match)")
    parser.add_argument("--stage", choices=golive_catalog.STAGES,
                        help="show only this catalogue stage ID (exact match)")
    parser.add_argument("--commands", action="store_true",
                        help="add the exact rehearse command or covering gate pack IDs to each step")
    args = parser.parse_args(argv)
    now = datetime.now(timezone.utc)
    try:
        reports = golive_stages.load_reports(args.reports, now=now)
        evidence = golive_evidence.load(args.evidence)
    except ValueError as exc:
        print(f"golive_next: {exc}", file=sys.stderr)
        return 2

    items = plan(golive_ledger.build(reports, evidence, now))
    if args.owner is not None:
        items = [item for item in items if item["owner"] == args.owner]
    if args.stage is not None:
        items = [item for item in items if item["stage"] == args.stage]
    hints = None
    if args.commands:
        try:
            hints = commands(items)
        except ValueError as exc:
            print(f"golive_next: {exc}", file=sys.stderr)
            return 2
    if args.json:
        if hints is not None:
            items = [dict(item, command=hint) for item, hint in zip(items, hints)]
        print(json.dumps(items, indent=2))
    elif args.stage is not None and not items:
        print(f"No pending step for stage {args.stage}.")
    else:
        for index, item in enumerate(items, start=1):
            stage = item["stage"]
            label = golive_catalog.STAGES[stage].label
            action = _ACTION_TEXT[item["action"]]
            reason = _REASON_TEXT[item["reason"]]
            print(f"{index}. {item['owner']}: {action} for stage {stage} ({label}) — {reason}.")
            if hints is not None:
                print(f"   run: {hints[index - 1]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
