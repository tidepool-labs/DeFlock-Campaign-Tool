"""Derive stage-4 live evidence from a copied portal queue status JSON file.

Reads one saved file only. It never opens the queue database, runs a fetch or
reads an approval or egress file. A row is metadata about a copy, not proof
of who captured it or that the saved documents are acceptable.
"""

import argparse
import hashlib
import ipaddress
import json
import os
import re
import stat
import sys
from datetime import datetime, timedelta, timezone

from . import golive_evidence

SCHEMA = "records-portal-status-v1"
MAX_BYTES = 1024 * 1024
MAX_AGE = timedelta(hours=24)
_BAD = "PORTAL_EVIDENCE_BAD_INPUT"
_RESERVED = (".invalid", ".test", ".example", ".localhost")
_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")
_SHA = re.compile(r"[0-9a-f]{64}")


class _BadInput(ValueError):
    pass


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        # argparse would echo the offending argument text; refuse with the fixed code only.
        raise _BadInput(_BAD)


def _sandbox(host):
    """Loopback, IP-literal or reserved names: what a rehearsal or fixture uses."""
    if host == "localhost" or host.endswith(_RESERVED) or re.fullmatch(r"[0-9.]+", host):
        return True
    try:
        ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        return False
    return True


def _public(host):
    return (len(host) <= 253 and "." in host and not _sandbox(host)
            and all(_LABEL.fullmatch(label) for label in host.split(".")))


def _observed(value, now):
    try:
        stamp = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        raise _BadInput(_BAD) from None
    if stamp.utcoffset() is None or not isinstance(now, datetime) or now.utcoffset() is None:
        raise _BadInput(_BAD)
    stamp = stamp.astimezone(timezone.utc)
    if stamp > now or now - stamp > MAX_AGE:
        raise _BadInput(_BAD)
    return stamp.replace(microsecond=0)


def records(status, observed_at, now, *, raw=None, name="portal-status.json"):
    """Return one stage-4 row for a decoded Queue.status() result.

    Invalid, sandbox-only, future or older-than-24h input raises ValueError
    with a fixed message. The hash is over ``raw`` when given, else canonical JSON.
    """
    items, pending = (status.get("items"), status.get("ledger_pending")) if isinstance(status, dict) else (None, None)
    if not isinstance(items, list) or type(pending) is not int or pending < 0:
        raise _BadInput(_BAD)
    stamp = _observed(observed_at, now)
    qualifying, sandbox = False, 0
    for item in items:
        if not isinstance(item, dict) or not isinstance(item.get("host"), str) or not isinstance(item.get("state"), str):
            raise _BadInput(_BAD)
        host, sha = item["host"], item.get("current_sha256")
        if _sandbox(host):
            sandbox += 1
        elif (item["state"] == "received" and isinstance(sha, str) and _SHA.fullmatch(sha)
              and _public(host)):
            qualifying = True
    if items and sandbox == len(items):
        raise _BadInput(_BAD)
    codes = [] if qualifying else ["PORTAL_NO_RECEIVED_ITEM"]
    if pending > 0:
        codes.append("PORTAL_LEDGER_PENDING")
    if raw is None:
        try:
            raw = json.dumps(status, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
        except (TypeError, ValueError, RecursionError):
            raise _BadInput(_BAD) from None
    row = {
        "schema": "records-golive-evidence-v1", "stage": "4", "kind": "live",
        "result": "pass" if qualifying else "fail",
        "recorded_at": stamp.isoformat().replace("+00:00", "Z"),
        "engine": None, "actor": "operator", "codes": codes,
        "source": {"schema": SCHEMA, "sha256": hashlib.sha256(raw).hexdigest(), "name": name},
    }
    try:
        golive_evidence.validate(row)
    except ValueError:
        raise _BadInput(_BAD) from None
    return row


def _read(path):
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as source:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise _BadInput(_BAD)
        raw = source.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        raise _BadInput(_BAD)
    return json.loads(raw.decode("utf-8"), object_pairs_hook=golive_evidence._unique_object), raw


def main(argv=None):
    parser = _Parser(prog="golive_adapt_portal", description=__doc__, allow_abbrev=False)
    parser.add_argument("status", help="copied output of the portal queue status command")
    parser.add_argument("--observed-at", required=True, help="timezone-aware instant the operator observed it")
    parser.add_argument("--json", action="store_true")
    try:
        args = parser.parse_args(argv)
        status, raw = _read(args.status)
        row = records(status, args.observed_at, datetime.now(timezone.utc),
                      raw=raw, name=os.path.basename(args.status))
    except (OSError, ValueError, RecursionError):
        print(_BAD, file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps([row]))
    else:
        print(row["stage"], row["kind"], row["result"], row["recorded_at"],
              row["source"]["schema"], ",".join(row["codes"]), sep="\t")
    return 0 if row["result"] == "pass" else 2


if __name__ == "__main__":
    raise SystemExit(main())
