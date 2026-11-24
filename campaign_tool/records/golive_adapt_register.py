"""Derive stage-1 evidence from a saved request register (read-only)."""

import argparse
import hashlib
import json
import sys
from datetime import date, datetime, timezone
from pathlib import Path

from . import golive_evidence
from .followup_clock import MAX_REGISTER, REGISTER_NAME, REGISTER_SCHEMA, REGISTER_ZONE, _read_bounded, _strict


def _record(register, now, raw, name):
    if not isinstance(now, datetime) or now.utcoffset() is None:
        raise ValueError("evaluation time must be timezone-aware")
    if (not isinstance(register, dict) or register.get("schema") != REGISTER_SCHEMA
            or not isinstance(register.get("requests"), list)):
        raise ValueError("invalid request register")

    today = now.astimezone(REGISTER_ZONE).date()
    sent = future = 0
    for row in register["requests"]:
        if not isinstance(row, dict):
            raise ValueError("invalid request row")
        # The receipt-first request_date is a clock input, not a send date.
        value = row.get("submitted_at")
        if value is None:
            continue
        if not isinstance(value, str):
            raise ValueError("invalid submission date")
        try:
            day = date.fromisoformat(value)
        except ValueError:
            raise ValueError("invalid submission date") from None
        if day.isoformat() != value:
            raise ValueError("invalid submission date")
        if day > today:
            future += 1
        else:
            sent += 1

    record = {
        "schema": "records-golive-evidence-v1", "stage": "1", "kind": "live",
        "result": "pass" if sent else "fail",
        "recorded_at": now.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "engine": None, "actor": "pipeline",
        "source": {"schema": REGISTER_SCHEMA, "sha256": hashlib.sha256(raw).hexdigest(), "name": name},
        "codes": ["REGISTER_FUTURE_SENT"] if future else ([] if sent else ["REGISTER_NO_SENT"]),
    }
    golive_evidence.validate(record)
    return record


def records(register, now):
    """Return one evidence record; hash canonical JSON for a decoded register.

    Missing and null dates mean no admitted explicit submission, not proof
    that nothing was sent. Date comparisons use the register's Pacific day.
    """
    try:
        raw = json.dumps(register, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, RecursionError):
        raise ValueError("register must be JSON serializable") from None
    return _record(register, now, raw, REGISTER_NAME)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("register", type=Path, help="saved request register JSON")
    parser.add_argument("--json", action="store_true", help="print a JSON array instead of TSV")
    args = parser.parse_args(argv)
    try:
        raw = _read_bounded(args.register, MAX_REGISTER)
        row = _record(_strict(raw), datetime.now(timezone.utc), raw, args.register.name)
    except (OSError, ValueError, RecursionError):
        print("REGISTER_EVIDENCE_BAD_INPUT", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps([row]))
    else:
        print(row["stage"], row["kind"], row["result"], row["recorded_at"],
              row["source"]["schema"], ",".join(row["codes"]), sep="\t")
    return 0 if row["result"] == "pass" else 2


if __name__ == "__main__":
    raise SystemExit(main())
