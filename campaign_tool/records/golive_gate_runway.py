"""Show whether each gate's evidence and prior-gate inputs will still be fresh at a target time.

For every `evidence` and `prior_gate` check in the registered packs, report OK,
EXPIRES_BEFORE_WINDOW (fresh now, stale at the target), STALE_NOW, MISSING or FAILED,
with the last instant the input is fresh. Freshness is `golive_gate._fresh`; the record
selection mirrors `golive_gate._evidence` and `_prior_gate`. Ages are the pack defaults:
an operator `max_age_days` passed to a pack's `spec()` is not known here. Engine pairing and
non-evidence checks (pins, disk, window, files) are not examined.

Read-only and offline. This is not a booking, a receipt or an authorization; it
performs and schedules nothing. Exit 0 only if every row is OK, 1 otherwise, 2 on error.
"""

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from fractions import Fraction

from . import golive_evidence, golive_gate, golive_gate_order

NOTE = ("not a booking, a receipt or an authorization: this report performs and schedules nothing; "
        "ages are pack defaults and engine pairing is not checked")
MAX_AHEAD = timedelta(days=30)
DATETIME_MAX = datetime.max.replace(tzinfo=timezone.utc)
E2E_PROOF = "records-e2e-proof-v1"
OK, EXPIRES, STALE, MISSING, FAILED = "OK", "EXPIRES_BEFORE_WINDOW", "STALE_NOW", "MISSING", "FAILED"


def _expiry(record, max_age_days):
    """Last instant `_fresh` accepts: recorded_at + max_age_days, to the microsecond."""
    recorded = datetime.strptime(record["recorded_at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    limit = Fraction(str(max_age_days)) if isinstance(max_age_days, float) else Fraction(max_age_days)
    try:
        return recorded + timedelta(microseconds=int(limit * 86_400_000_000 // 1))
    except OverflowError:  # the true expiry is past year 9999: report the latest representable instant
        return DATETIME_MAX


def _select(check, records):
    """Return (status, passing records) the way the evaluator picks them, before any clock."""
    if check["type"] == "evidence":
        usable = (row for row in records if row["source"]["schema"] != E2E_PROOF)
        record = golive_evidence.latest(usable, check["stage"], check["kind"])
        if record is None:
            return MISSING, []
        return (None, [record]) if record["result"] == "pass" else (FAILED, [])
    seen = [row for row in records
            if row["kind"] == "gate" and row["source"]["name"].startswith(check["gate_id"])]
    passing = [row for row in seen if row["result"] == "pass"]
    if passing:
        return None, passing
    return (FAILED if seen else MISSING), []


def check_row(check, records, now, at):
    """One row for an evidence or prior_gate check; `now` and `at` are aware UTC datetimes."""
    age = check["max_age_days"]
    status, passing = _select(check, records)
    last = max((_expiry(row, age) for row in passing), default=None)
    if status is None:
        if not any(golive_gate._fresh(row, now, age) for row in passing):
            status = STALE
        elif any(golive_gate._fresh(row, at, age) for row in passing):
            status = OK
        else:
            status = EXPIRES
    what = check["gate_id"] if check["type"] == "prior_gate" else f"stage {check['stage']} {check['kind']}"
    return {"check": check["type"], "label": check["label"], "input": what, "max_age_days": age,
            "status": status,
            "last_fresh": None if last is None else last.strftime("%Y-%m-%dT%H:%M:%S.%fZ")}


def report(records, now, at, gate_id=None):
    """Rows for every gate (or one), priors first. Unknown gate_id raises KeyError."""
    gates = golive_gate_order.registry()
    if gate_id is not None and gate_id not in gates:
        raise KeyError(gate_id)
    rows = []
    for gid in golive_gate_order.order():
        if gate_id is not None and gid != gate_id:
            continue
        for check in gates[gid]["checks"]:
            if check["type"] in ("evidence", "prior_gate"):
                rows.append({"gate_id": gid, **check_row(check, records, now, at)})
    return rows


def _aware_time(text):
    try:
        value = datetime.fromisoformat(text)
    except ValueError:
        raise ValueError("--at must be an ISO 8601 time with a UTC offset") from None
    if value.utcoffset() is None:
        raise ValueError("--at must carry a UTC offset (naive times are refused)")
    try:
        return value.astimezone(timezone.utc)
    except OverflowError:
        raise ValueError("--at is outside the supported UTC time range") from None


def main(argv=None, now=None):
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--evidence", required=True, metavar="DIR", help="go-live evidence folder (read only)")
    parser.add_argument("--at", required=True, metavar="TIME", help="target time, ISO 8601 with offset")
    parser.add_argument("--gate", metavar="ID", help="report only this gate")
    parser.add_argument("--json", action="store_true", help="print the report as JSON")
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 2
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    try:
        at = _aware_time(args.at)
        if at < now:
            raise ValueError("--at is in the past")
        if at > now + MAX_AHEAD:
            raise ValueError("--at is more than 30 days ahead")
        rows = report(golive_evidence.load(args.evidence), now, at, args.gate)
    except KeyError as exc:
        print(f"unknown gate {exc.args[0]}", file=sys.stderr)
        return 2
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    ok = all(row["status"] == OK for row in rows)
    if args.json:
        print(json.dumps({"note": NOTE, "now": now.isoformat(), "at": at.isoformat(), "all_ok": ok,
                          "rows": rows}, indent=2, sort_keys=True))
    else:
        print(f"runway to {at.isoformat()} (evaluated now {now.isoformat()})")
        for row in rows:
            fresh = f"last fresh {row['last_fresh']}" if row["last_fresh"] else "no passing record"
            print(f"{row['gate_id']}: {row['status']} {row['check']} {row['input']} "
                  f"(max {row['max_age_days']} days, {fresh}) - {row['label']}")
        print(f"note: {NOTE}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
