"""Derive stage-18 liveness evidence from a saved pipeline-health JSON report."""

import argparse
import hashlib
import json
import os
import stat
import sys
from datetime import datetime, timezone

from . import golive_evidence, pipeline_health

# The timer uses the full assess() inventory, including disk. A partial
# report (even one with all its present checks OK) cannot prove liveness.
_CHECKS = (
    "arrival_backlog", "mail_poll", "last_run", "run_failures", "outputs", "notify",
    "schedule", "analysis", "console_export", "release_links", "disk",
)


def _observation(report, now):
    if (not isinstance(report, dict) or report.get("schema") != pipeline_health.SCHEMA
            or not isinstance(report.get("checks"), list) or not isinstance(report.get("now"), str)):
        raise ValueError("invalid pipeline-health report")
    state, exit_code = report.get("state"), report.get("exit_code")
    if (not isinstance(state, str) or state not in pipeline_health.EXIT
            or type(exit_code) is not int or exit_code != pipeline_health.EXIT[state]):
        raise ValueError("invalid pipeline-health summary")
    try:
        observed = datetime.fromisoformat(report["now"])
        if observed.utcoffset() is not None:
            observed = observed.astimezone(timezone.utc)
    except (ValueError, OverflowError):
        raise ValueError("invalid report observation time") from None
    if observed.utcoffset() is None or not isinstance(now, datetime) or now.utcoffset() is None:
        raise ValueError("report and evaluation clocks must be timezone-aware")
    if observed > now:
        raise ValueError("future report observation time")
    return observed


def _check_states(report):
    checks = {}
    for item in report["checks"]:
        if not isinstance(item, dict):
            raise ValueError("invalid pipeline-health check")
        name, state, reason = item.get("check"), item.get("state"), item.get("reason_code")
        if (name not in _CHECKS or name in checks or state not in ("ok", "warn", "stop", "unknown")
                or not isinstance(reason, str) or not reason.strip()):
            raise ValueError("invalid or duplicate pipeline-health check")
        if name == "mail_poll" and state == "ok" and reason == "mail_poll_stale":
            raise ValueError("contradictory mail-poll check")
        checks[name] = "unknown" if reason in pipeline_health.NOT_CONFIGURED else state
    return checks


def records(report, now, max_age_hours: float = 2):
    """Return one record from a decoded report; invalid or future input raises ValueError.

    Source identity for decoded input is the hash of canonical JSON. The CLI
    can instead bind the exact saved bytes. Neither path authenticates the
    capture or establishes that the monitored pipeline is healthy.
    """
    if type(max_age_hours) not in (int, float) or not 0 <= max_age_hours < float("inf"):
        raise ValueError("invalid maximum report age")
    observed = _observation(report, now)
    checks = _check_states(report)
    # Reproduce the producer's precedence using the original states: optional
    # unconfigured checks are excluded from its aggregate, not promoted.
    states = {item["state"] for item in report["checks"]
              if item["reason_code"] not in pipeline_health.NOT_CONFIGURED}
    if states and report["state"] != next(state for state in ("stop", "warn", "unknown", "ok") if state in states):
        raise ValueError("invalid pipeline-health summary")
    codes = [name.upper() for name in _CHECKS if checks.get(name) != "ok"]
    stale = (now - observed).total_seconds() / 3600 > max_age_hours
    if stale:
        codes.append("HEALTH_REPORT_STALE")
    failed = stale or any(checks.get(name, "unknown") == "unknown" for name in _CHECKS)
    try:
        raw = json.dumps(report, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    except (TypeError, ValueError, RecursionError):
        raise ValueError("report must be JSON serializable") from None
    row = {
        "schema": "records-golive-evidence-v1", "stage": "18", "kind": "live",
        "result": "fail" if failed else "pass",
        "recorded_at": observed.isoformat(timespec="seconds").replace("+00:00", "Z"),
        "engine": None, "actor": "pipeline", "codes": codes,
        "source": {"schema": pipeline_health.SCHEMA, "sha256": hashlib.sha256(raw).hexdigest(),
                   "name": "health.json"},
    }
    golive_evidence.validate(row)
    return row


def main(argv=None):
    parser = argparse.ArgumentParser(prog="golive_adapt_health", description=__doc__, allow_abbrev=False)
    parser.add_argument("report", help="saved pipeline-health JSON report")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    try:
        fd = os.open(args.report, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
        with os.fdopen(fd, "rb") as source:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise ValueError("report must be a regular file")
            raw = source.read(pipeline_health.MAX_JSON_BYTES + 1)
        if len(raw) > pipeline_health.MAX_JSON_BYTES:
            raise ValueError("report exceeds size bound")
        report = json.loads(raw.decode("utf-8"), object_pairs_hook=golive_evidence._unique_object)
        row = records(report, datetime.now(timezone.utc))
        row["source"].update(sha256=hashlib.sha256(raw).hexdigest(), name=os.path.basename(args.report))
        golive_evidence.validate(row)
    except (OSError, ValueError, RecursionError):
        print("HEALTH_EVIDENCE_BAD_INPUT", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps([row]))
    else:
        print(row["stage"], row["kind"], row["result"], row["recorded_at"],
              row["source"]["schema"], ",".join(row["codes"]), sep="\t")
    return 0 if row["result"] == "pass" else 2


if __name__ == "__main__":
    raise SystemExit(main())
