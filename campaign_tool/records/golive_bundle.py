"""Render a sitting packet from operator-copied metadata; evidence only, not authorization."""

import argparse
import hashlib
import json
from datetime import datetime
from pathlib import Path
import re
import sys

from . import golive_gate, rehearsal_receipt


_CODE = re.compile(r"[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)*")
_SHA256 = re.compile(r"[0-9a-f]{64}")


def _decode(raw):
    try:
        return json.loads(raw.decode("utf-8"), object_pairs_hook=golive_gate._unique_object)
    except (ValueError, RecursionError):
        raise ValueError("invalid metadata JSON") from None


def _stages(value):
    if not isinstance(value, list) or not value or any(
        not isinstance(stage, str) or stage not in golive_gate.STAGES for stage in value
    ):
        raise ValueError("invalid stage list")
    return list(dict.fromkeys(value))


def _text(value):
    return isinstance(value, str) and bool(value.strip())


def _matches(pattern, value):
    return isinstance(value, str) and pattern.fullmatch(value) is not None


def _gate_checks(receipt):
    if not isinstance(receipt, dict) or receipt.get("schema") != golive_gate.RECEIPT_SCHEMA:
        raise ValueError("invalid gate receipt schema")
    if not _matches(golive_gate.GATE_ID, receipt.get("gate_id")):
        raise ValueError("invalid gate receipt ID")
    stamp, inputs = receipt.get("evaluated_at"), receipt.get("inputs")
    if (not isinstance(stamp, str) or datetime.fromisoformat(stamp).utcoffset() is None
            or not isinstance(inputs, dict) or not all(_matches(_SHA256, pin) for pin in inputs.values())):
        raise ValueError("invalid gate receipt timestamp or input digests")
    checks = receipt.get("checks")
    if not isinstance(checks, list) or not checks:
        raise ValueError("invalid gate receipt checks")
    for check in checks:
        if (not isinstance(check, dict) or not _text(check.get("label"))
                or not isinstance(check.get("type"), str) or check["type"] not in golive_gate.CHECK_TYPES
                or check.get("state") not in ("pass", "fail", "unknown")
                or not _matches(_CODE, check.get("code"))):
            raise ValueError("invalid gate receipt check")
    expected = "GO" if all(check["state"] == "pass" for check in checks) else "NO-GO"
    if receipt.get("verdict") != expected:
        raise ValueError("inconsistent gate receipt verdict")
    return checks


def _ledger_lines(rows, stages):
    if not isinstance(rows, list):
        raise ValueError("ledger rows must be a list")
    lines = {}
    for row in rows:
        if not isinstance(row, dict) or not _text(row.get("stage")):
            raise ValueError("invalid ledger row")
        stage = row["stage"]
        if stage not in stages:
            continue
        cell = row.get("rehearsed")
        if (stage in lines or not _text(row.get("label")) or not isinstance(cell, dict)
                or cell.get("status") not in ("pass", "fail", "missing", "not needed")
                or cell.get("reason") not in (None, "STALE", "ENGINE_CHANGED", "FUTURE_DATED")):
            raise ValueError("invalid or duplicate stage row")
        status = cell["status"]
        if cell.get("reason"):
            status += f" ({cell['reason']})"
        lines[stage] = f"- Stage {stage} — {row['label']}: ledger rehearsal {status}"
    return lines


def _rehearsal_lines(receipts, stages):
    lines = {stage: {} for stage in stages}
    for raw in receipts:
        if not isinstance(raw, bytes):
            raise ValueError("rehearsal receipts must be original JSON bytes")
        receipt = _decode(raw)
        rehearsal_receipt._validate(receipt)  # Validate recorded facts; don't requalify them now.
        digest = hashlib.sha256(raw).hexdigest()
        qualification = ("qualified" if receipt["qualified"] else
                         "not qualified: " + ", ".join(receipt["qualify_codes"]))
        for stage in lines:
            if stage in receipt["stages"]:
                lines[stage][digest] = f"  - Receipt: {receipt['result']} ({qualification}); SHA-256: {digest}"
    return lines


def render(gate_receipt, ledger_rows, receipts, *, gate_spec, rollback):
    """Return Markdown for one step. ``receipts`` is an iterable of exact JSON file bytes.

    Ledger rehearsal status and supplied receipt results are shown separately:
    a ledger row does not identify the exact receipt it was built from.
    """
    checks = _gate_checks(gate_receipt)
    stages = _stages(gate_receipt.get("stages"))
    spec = golive_gate.load_spec(gate_spec)
    if spec["gate_id"] != gate_receipt["gate_id"] or set(spec["stages"]) != set(stages):
        raise ValueError("gate spec does not match receipt ID and stage set")
    if not _text(rollback) or not rollback.isprintable():
        raise ValueError("rollback must be a nonempty, single-line reference")
    ledger = _ledger_lines(ledger_rows, stages)
    rehearsals = _rehearsal_lines(receipts, stages)
    lines = [f"# {spec['title']}", "", f"Gate verdict: **{gate_receipt['verdict']}**",
             "Evidence only; not authorization to proceed.", "", "## Gate checks"]
    for check in checks:
        label = json.dumps(check["label"], ensure_ascii=False)
        lines.append(f"- {label} ({check['type']}): {check['state']} — {check['code']}")
    lines.extend(["", "## Stages and rehearsals"])
    for stage in stages:
        lines.append(ledger.get(stage, f"- Stage {stage}: no ledger row supplied"))
        lines.extend(rehearsals[stage].values() or ["  - No rehearsal receipt supplied."])
    lines.extend(["", f"Rollback reference: {rollback}", ""])
    return "\n".join(lines)


def _copy(path):
    snapshot = golive_gate._read_input(path)
    if snapshot is None:
        raise ValueError("expected a readable copied metadata file")
    return snapshot.raw


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--spec", required=True, type=Path, help="copied gate spec JSON")
    parser.add_argument("--gate", required=True, type=Path, help="copied gate receipt JSON")
    parser.add_argument("--ledger", required=True, type=Path, help="copied golive_ledger --json output")
    parser.add_argument("--receipt", action="append", default=[], type=Path,
                        help="copied rehearsal receipt JSON (repeatable)")
    parser.add_argument("--rollback", required=True, help="rollback reference, not a command to execute")
    parser.add_argument("--out", type=Path, help="new private output file (default: stdout)")
    args = parser.parse_args(argv)
    try:
        spec = _decode(_copy(args.spec))
        gate = _decode(_copy(args.gate))
        ledger = _decode(_copy(args.ledger))
        page = render(gate, ledger.get("rows") if isinstance(ledger, dict) else None,
                      (_copy(path) for path in args.receipt), gate_spec=spec, rollback=args.rollback)
        if args.out is None:
            sys.stdout.write(page)
        else:
            page.encode("utf-8")  # Refuse unencodable input before creating the output file.
            golive_gate._write_receipt(args.out, page)
    except (ValueError, OSError) as exc:
        print(f"golive_bundle: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
