"""Derive stage-15 live evidence from a saved, pinned ``golive_pair --json`` report.

Reads one saved file; it never runs golive_pair, executes the wrapper or writes
evidence. The row is machine-valid metadata, not acceptance of the engine switch.
"""

import argparse
import hashlib
import json
import os
import re
import stat
import sys
from datetime import datetime, timedelta, timezone

from . import golive_evidence, golive_pair

SCHEMA = "records-golive-pair-report-v1"
MAX_BYTES = 64 * 1024
MAX_AGE = timedelta(hours=24)
_SHA256 = re.compile(r"[0-9a-f]{64}")
_ENGINE_PIN = re.compile(r"[0-9a-f]{40}")
_ENGINE_ID = re.compile(r"engine-[0-9a-f]{7,40}")
_STAMP = "%Y-%m-%dT%H:%M:%SZ"
_FIELDS = {"wrapper_sha256", "live_sha256", "engines", "codes"}


def _text(value, pattern):
    if type(value) is not str or not pattern.fullmatch(value):
        raise ValueError("invalid pin")
    return value


def _observed(stamp, now):
    if type(stamp) is not str:
        raise ValueError("invalid observation time")
    try:
        observed = datetime.strptime(stamp, _STAMP).replace(tzinfo=timezone.utc)
    except ValueError:
        raise ValueError("invalid observation time") from None
    if not isinstance(now, datetime) or now.utcoffset() is None:
        raise ValueError("evaluation clock must be timezone-aware")
    if observed > now or now - observed > MAX_AGE:
        raise ValueError("observation time outside the 24-hour window")
    return stamp


def _result(report):
    if not isinstance(report, dict) or set(report) != _FIELDS:
        raise ValueError("invalid pair report")
    engines, codes = report["engines"], report["codes"]
    if (not isinstance(engines, list) or not isinstance(codes, list)
            or any(type(item) is not str for item in codes)
            or any(type(item) is not str or not _ENGINE_ID.fullmatch(item) for item in engines)):
        raise ValueError("invalid pair report")
    return golive_pair.PairResult(
        wrapper_sha256=_text(report["wrapper_sha256"], _SHA256), live_sha256=_text(report["live_sha256"], _SHA256),
        engines=engines, consistent=len(engines) == 1)


def records(report, expected, now):
    """Return one stage-15 row for a decoded report; invalid input raises ValueError.

    ``expected`` carries wrapper_sha256, live_sha256, the bare 40-hex engine and
    observed_at. The verdict is recomputed with golive_pair.compare from the
    report's hashes and engines; the report's own ``codes`` are not trusted. The
    CLI replaces the decoded-JSON source hash with the hash of the saved bytes.
    """
    if not isinstance(expected, dict) or set(expected) != {"wrapper_sha256", "live_sha256", "engine", "observed_at"}:
        raise ValueError("invalid pin")
    pins = golive_pair.ExpectedPair(
        wrapper_sha256=_text(expected["wrapper_sha256"], _SHA256), live_sha256=_text(expected["live_sha256"], _SHA256),
        engine="engine-" + _text(expected["engine"], _ENGINE_PIN))
    stamp = _observed(expected["observed_at"], now)
    ok, codes = golive_pair.compare(_result(report), pins)
    try:
        raw = json.dumps(report, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    except (TypeError, ValueError, RecursionError):
        raise ValueError("report must be JSON serializable") from None
    row = {
        "schema": "records-golive-evidence-v1", "stage": "15", "kind": "live",
        "result": "pass" if ok else "fail", "recorded_at": stamp,
        "engine": expected["engine"], "actor": "operator", "codes": codes,
        "source": {"schema": SCHEMA, "sha256": hashlib.sha256(raw).hexdigest(), "name": "pair.json"},
    }
    golive_evidence.validate(row)
    return row


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        # argparse errors can echo argument values and file paths.
        raise ValueError("invalid arguments")


def _read(path):
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as source:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError("report must be a regular file")
        raw = source.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        raise ValueError("report exceeds size bound")
    return raw


def main(argv=None):
    parser = _Parser(prog="golive_adapt_pair", description=__doc__, allow_abbrev=False)
    parser.add_argument("report", help="saved golive_pair --json report")
    parser.add_argument("--expect-wrapper", required=True, metavar="SHA256")
    parser.add_argument("--expect-live", required=True, metavar="SHA256")
    parser.add_argument("--engine", required=True, metavar="HEX40")
    parser.add_argument("--observed-at", required=True, metavar="YYYY-MM-DDTHH:MM:SSZ")
    parser.add_argument("--json", action="store_true")
    try:
        args = parser.parse_args(argv)
        raw = _read(args.report)
        report = json.loads(raw.decode("utf-8"), object_pairs_hook=golive_evidence._unique_object)
        row = records(report, {"wrapper_sha256": args.expect_wrapper, "live_sha256": args.expect_live,
                               "engine": args.engine, "observed_at": args.observed_at},
                      datetime.now(timezone.utc))
        row["source"].update(sha256=hashlib.sha256(raw).hexdigest(), name=os.path.basename(args.report))
        golive_evidence.validate(row)
    except (OSError, ValueError, RecursionError):
        print("PAIR_EVIDENCE_BAD_INPUT", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps([row]))
    else:
        print(row["stage"], row["kind"], row["result"], row["recorded_at"],
              row["source"]["schema"], ",".join(row["codes"]), sep="\t")
    # 1 marks a printed fail row; 2 is reserved for refused input.
    return 0 if row["result"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
