"""Derive stage A3 evidence from a saved `records console-check` transcript.

The transcript proves only that loopback probes and the export freshness check
passed when it was captured. It never proves Access or phone login, and this
module never runs the check or opens a port.
"""

import argparse
import hashlib
import json
import os
import re
import stat
import sys
from datetime import datetime, timedelta, timezone

from . import console_check, golive_evidence

MAX_BYTES = 64 * 1024
MAX_AGE = timedelta(hours=24)
LOOPBACK_ONLY = "CONSOLE_LOOPBACK_ONLY"
_CODE = re.compile(r"[A-Z][A-Z0-9_]*")


def expected_labels():
    """Labels `console_check.main` prints, in its order, from its own constants."""
    return (*(f"GET {path}" for path in (*console_check.PAGES, "/send")),
            f"POST {console_check.POST_PATH}", "export")


def _problem(text):
    """Return the code naming the first problem in a transcript, or None."""
    expected = expected_labels()
    seen = set()
    lines = text.split("\n")
    if lines[-1] == "":
        lines.pop()
    for line in lines:
        label, _, code = line.rpartition(" ")
        if label not in expected or not _CODE.fullmatch(code) or label in seen:
            return "CONSOLE_CHECK_UNEXPECTED_LINE"
        seen.add(label)
        if code != "PASS":
            return "CONSOLE_CHECK_NOT_PASS"
    if seen != set(expected):
        return "CONSOLE_CHECK_LABEL_MISSING"
    return None


def records(raw, observed, now, *, name="console-check.txt"):
    """Return one record from saved transcript bytes; bad input raises ValueError."""
    if not isinstance(raw, bytes) or len(raw) > MAX_BYTES:
        raise ValueError("transcript must be bounded bytes")
    if observed.utcoffset() is None or now.utcoffset() is None:
        raise ValueError("observation and evaluation clocks must be timezone-aware")
    if observed > now:
        raise ValueError("future observation time")
    if now - observed > MAX_AGE:
        raise ValueError("observation older than 24 hours")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise ValueError("transcript is not UTF-8") from None
    problem = _problem(text)
    row = {
        "schema": "records-golive-evidence-v1", "stage": "A3", "kind": "live",
        "result": "fail" if problem else "pass",
        "recorded_at": observed.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "engine": None, "actor": "operator",
        "codes": [*([problem] if problem else []), LOOPBACK_ONLY],
        "source": {"schema": "records-console-check-v1", "sha256": hashlib.sha256(raw).hexdigest(),
                   "name": name},
    }
    golive_evidence.validate(row)
    return row


def _now():
    return datetime.now(timezone.utc)


def main(argv=None):
    parser = argparse.ArgumentParser(prog="golive_adapt_console", description=__doc__, allow_abbrev=False)
    parser.add_argument("transcript", help="saved `records console-check` stdout")
    parser.add_argument("--observed-at", required=True, help="ISO-8601 capture time with a UTC offset")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    try:
        observed = datetime.fromisoformat(args.observed_at)
        fd = os.open(args.transcript, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
        with os.fdopen(fd, "rb") as source:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise ValueError("transcript must be a regular file")
            raw = source.read(MAX_BYTES + 1)
        row = records(raw, observed, _now(), name=os.path.basename(args.transcript))
    except (OSError, ValueError, OverflowError):
        print("CONSOLE_EVIDENCE_BAD_INPUT", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps([row]))
    else:
        print(row["stage"], row["kind"], row["result"], row["recorded_at"],
              row["source"]["schema"], ",".join(row["codes"]), sep="\t")
    return 0 if row["result"] == "pass" else 2


if __name__ == "__main__":
    raise SystemExit(main())
