"""Derive stage A4 evidence from a saved anonymous `console_edge_smoke` transcript.

The transcript proves only that anonymous requests were refused at the edge
when it was captured. It never proves G's phone login, and this module never
runs the sweep or opens a connection.
"""

import argparse
import hashlib
import json
import os
import re
import stat
import sys
from datetime import datetime, timedelta, timezone

from . import console_edge_smoke, golive_evidence
from .golive_adapt_console import MAX_AGE

MAX_BYTES = 64 * 1024
ANONYMOUS_ONLY = "ACCESS_SWEEP_ANONYMOUS_ONLY"
FAILED = "ACCESS_SWEEP_FAILED"
_ACCESS_HEADER = "ACCESS CHECK"
_ISOLATION_HEADER = "ISOLATION CHECK (not an Access check)"
_PROBE = re.compile(r"(GET|HEAD|OPTIONS|POST) ([!-~]+) (?:[0-9]{3}|ERR:[A-Za-z0-9_]+) (?:[A-Za-z0-9._-]+|\?) (ok|FAIL)")
_FOOTER_FAIL = re.compile(r"FAIL ([1-9][0-9]*) probes")


class AccessSweepRefused(ValueError):
    """Input that cannot be evidence; `.code` is the stable reason."""

    def __init__(self, code):
        super().__init__(code)
        self.code = code


def _shown(scheme, path):
    """Pattern for the path column the checked-out sweep prints for one probe."""
    return re.escape(path) if scheme == "https" else r"http://[a-z0-9.-]+" + re.escape(path)


def _judge(text):
    """Return True when a well-formed Access transcript records a failure."""
    lines = text.split("\n")
    if lines[-1] == "":
        lines.pop()
    if lines[:1] == [_ISOLATION_HEADER]:
        raise AccessSweepRefused("ACCESS_SWEEP_NOT_ACCESS_MODE")
    if len(lines) < 2 or lines[0] != _ACCESS_HEADER:
        raise AccessSweepRefused("ACCESS_SWEEP_BAD_TRANSCRIPT")
    footer, probes = lines[-1], lines[1:-1]
    footer_fail = _FOOTER_FAIL.fullmatch(footer)
    if footer != "PASS" and not footer_fail:
        raise AccessSweepRefused("ACCESS_SWEEP_BAD_TRANSCRIPT")
    expected = console_edge_smoke._probes()  # read from the checked-out sweep, never a literal
    if len(probes) != len(expected):
        raise AccessSweepRefused("ACCESS_SWEEP_PROBE_COUNT")
    failures = 0
    for line, (scheme, method, path, _body, _extra) in zip(probes, expected):
        match = _PROBE.fullmatch(line)
        if not match:
            raise AccessSweepRefused("ACCESS_SWEEP_BAD_TRANSCRIPT")
        if match.group(1) != method or not re.fullmatch(_shown(scheme, path), match.group(2)):
            raise AccessSweepRefused("ACCESS_SWEEP_PROBE_SET")
        failures += match.group(3) == "FAIL"
    if footer_fail and int(footer_fail.group(1)) != failures:
        raise AccessSweepRefused("ACCESS_SWEEP_BAD_TRANSCRIPT")
    return failures > 0 or footer != "PASS"


def records(raw, observed, now, *, name="edge-sweep.txt"):
    """Return one record from saved transcript bytes; refused input raises AccessSweepRefused."""
    if not isinstance(raw, bytes) or len(raw) > MAX_BYTES:
        raise AccessSweepRefused("ACCESS_SWEEP_BAD_INPUT")
    if observed.utcoffset() is None or now.utcoffset() is None:
        raise AccessSweepRefused("ACCESS_SWEEP_BAD_INPUT")
    if observed > now:
        raise AccessSweepRefused("ACCESS_SWEEP_FUTURE")
    if now - observed > MAX_AGE:
        raise AccessSweepRefused("ACCESS_SWEEP_STALE")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise AccessSweepRefused("ACCESS_SWEEP_BAD_INPUT") from None
    failed = _judge(text)
    row = {
        "schema": "records-golive-evidence-v1", "stage": "A4", "kind": "live",
        "result": "fail" if failed else "pass",
        "recorded_at": observed.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "engine": None, "actor": "operator",
        "codes": [*([FAILED] if failed else []), ANONYMOUS_ONLY],
        "source": {"schema": "records-console-edge-sweep-v1", "sha256": hashlib.sha256(raw).hexdigest(),
                   "name": name},
    }
    golive_evidence.validate(row)
    return row


def _now():
    return datetime.now(timezone.utc)


def main(argv=None):
    parser = argparse.ArgumentParser(prog="golive_adapt_access", description=__doc__, allow_abbrev=False)
    parser.add_argument("transcript", help="saved `console_edge_smoke` stdout (access mode)")
    parser.add_argument("--observed-at", required=True, help="ISO-8601 capture time with a UTC offset")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    try:
        observed = datetime.fromisoformat(args.observed_at)
        fd = os.open(args.transcript, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
        with os.fdopen(fd, "rb") as source:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise AccessSweepRefused("ACCESS_SWEEP_BAD_INPUT")
            raw = source.read(MAX_BYTES + 1)
        row = records(raw, observed, _now(), name=os.path.basename(args.transcript))
    except AccessSweepRefused as refused:
        print(refused.code, file=sys.stderr)
        return 2
    except (OSError, ValueError, OverflowError):
        print("ACCESS_SWEEP_BAD_INPUT", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps([row]))
    else:
        print(row["stage"], row["kind"], row["result"], row["recorded_at"],
              row["source"]["schema"], ",".join(row["codes"]), sep="\t")
    return 0 if row["result"] == "pass" else 2


if __name__ == "__main__":
    raise SystemExit(main())
