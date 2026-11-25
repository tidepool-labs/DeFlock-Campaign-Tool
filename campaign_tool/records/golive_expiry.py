"""Show which qualified go-live rehearsals lapse soon, from evidence only.

A row is evidence about a rehearsal record; it never performs, authorizes or
schedules an attended step. Read-only: the only clock is the injected ``now``
(``main`` falls back to the system clock only when none is passed).
"""

import argparse
import json
import re
import sys
from datetime import datetime, timedelta, timezone

from . import golive_catalog, golive_evidence, golive_ledger
from .golive_ledger import _REHEARSAL_AGE

STATUSES = ("OK", "EXPIRES_SOON", "EXPIRED", "FAILED", "MISSING", "FUTURE_DATED", "ENGINE_CHANGED")
_STAMP = "%Y-%m-%dT%H:%M:%SZ"
_E2E_PROOF = "records-e2e-proof-v1"
_MAX_WITHIN_DAYS = 30
_LAST = datetime(9999, 12, 31, 23, 59, 59, tzinfo=timezone.utc)


def _utc(stamp):
    return datetime.strptime(stamp, _STAMP).replace(tzinfo=timezone.utc)


def _stamp(moment):
    # Fixed-width UTC: platform strftime("%Y") drops the leading zeros of years < 1000
    # on glibc, which breaks the four-digit format and the string sort by expiry.
    return (f"{moment.year:04d}-{moment.month:02d}-{moment.day:02d}"
            f"T{moment.hour:02d}:{moment.minute:02d}:{moment.second:02d}Z")


def _expiry(recorded):
    try:
        return min(recorded + _REHEARSAL_AGE, _LAST)  # a year-9999 stamp must not crash
    except OverflowError:
        return _LAST


def _status(record, now, within_days, engine_changed=False):
    recorded = _utc(record["recorded_at"])
    expires = _expiry(recorded)
    # Same comparison as the ledger's cutoff (recorded < now - age), checked
    # first as the ledger does, so a stale failure is STALE there too.
    if expires < now:
        return "EXPIRED"
    # The ledger checks the engine before the result, so an old-engine failure is
    # ENGINE_CHANGED there as well; the flag is the ledger's own cell, not a copy of its rule.
    if engine_changed:
        return "ENGINE_CHANGED"
    if record["result"] == "fail":
        return "FAILED"
    if recorded > now:
        return "FUTURE_DATED"
    if expires - now <= timedelta(days=within_days):
        return "EXPIRES_SOON"
    return "OK"


def runway(evidence, now, within_days):
    """Return one row per rehearsal-needing stage, in catalogue order; a naive ``now`` means UTC."""
    if type(within_days) is not int or not 0 <= within_days <= _MAX_WITHIN_DAYS:
        raise ValueError(f"within_days must be an integer from 0 to {_MAX_WITHIN_DAYS}")
    if now.utcoffset() is None:
        now = now.replace(tzinfo=timezone.utc)
    now = now.astimezone(timezone.utc)
    # Same filter as the ledger: descriptive sandbox proof never qualifies a rehearsal.
    evidence = tuple(evidence)
    rehearsals = tuple(row for row in evidence if row["kind"] == "rehearsal"
                       and row["source"]["schema"] != _E2E_PROOF)
    # Engine freshness comes from the ledger itself so the two cannot disagree.
    changed = {entry["stage"] for entry in golive_ledger.build([], evidence, now)
               if entry["rehearsed"].get("reason") == "ENGINE_CHANGED"}
    rows = []
    for stage, rule in golive_catalog.STAGES.items():
        if not rule.needs_rehearsal:
            continue
        record = golive_evidence.latest(rehearsals, stage, "rehearsal")
        if record is None:
            rows.append(dict(stage=stage, status="MISSING", recorded_at=None, expires_at=None))
            continue
        rows.append(dict(stage=stage, status=_status(record, now, within_days, stage in changed),
                         recorded_at=record["recorded_at"],
                         expires_at=_stamp(_expiry(_utc(record["recorded_at"])))))
    return rows


def _within_days(text):
    if not re.fullmatch(r"[0-9]{1,2}", text) or int(text) > _MAX_WITHIN_DAYS:
        raise argparse.ArgumentTypeError(f"expected an integer from 0 to {_MAX_WITHIN_DAYS}")
    return int(text)


def main(argv=None, now=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence", required=True)
    parser.add_argument("--within-days", type=_within_days, default=3)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    try:
        evidence = golive_evidence.load(args.evidence)
    except (ValueError, OSError) as exc:
        print(f"golive_expiry: {exc}", file=sys.stderr)
        return 2
    rows = runway(evidence, now if now is not None else datetime.now(timezone.utc), args.within_days)
    rows.sort(key=lambda row: (row["expires_at"] is None, row["expires_at"] or ""))  # stable: catalogue order on ties
    counts = {status: sum(row["status"] == status for row in rows) for status in STATUSES}
    summary = f"{len(rows)} rehearsals: " + ", ".join(f"{counts[status]} {status}" for status in STATUSES)
    if args.json:
        print(json.dumps(dict(rows=rows, counts=counts, within_days=args.within_days, summary=summary), indent=2))
    else:
        print("Stage | Status | Recorded | Expires")
        for row in rows:
            print(f"{row['stage']} | {row['status']} | {row['recorded_at'] or '-'} | {row['expires_at'] or '-'}")
        print(summary)
    return 0 if counts["OK"] == len(rows) else 1


if __name__ == "__main__":
    raise SystemExit(main())
