"""Derive A2 and stage-20 evidence from a console export and its run context."""

import argparse
import hashlib
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

from . import approval_anchor_status as anchor, approval_queue as aq
from . import console_export as ce, console_snapshot as snap, golive_evidence, pipeline_health as health


def _read(directory, name, limit):
    fd = aq._open_chain(directory)
    try:
        return snap._read_file(fd, name, limit)
    finally:
        os.close(fd)


def _manifest(root, now):
    try:
        # Single request pin, no cached fallback or pin diagnostic. The export
        # publisher makes generation directories immutable before switching current.
        pin = snap.Pinner(root)._pin()
        raw = _read(pin.path, ce.MANIFEST, snap.MAX_MANIFEST)
        if raw is not None:
            value = aq._strict_load(raw, "export_manifest_invalid")
            if value["schema"] == ce.SCHEMA and value["run_id"] == pin.run_id:
                exported_at = datetime.fromisoformat(value["exported_at"])
                if exported_at.utcoffset() is not None and exported_at <= now:
                    return raw, value
    except (snap.SnapshotError, ce.ExportError, aq.ApprovalQueueError, OSError, ValueError, KeyError, TypeError):
        pass
    return None, {}


def _report(path):
    try:
        raw = _read(path.parent, path.name, health.MAX_JSON_BYTES)
        if raw is not None:
            latest = aq._strict_load(raw, "run_report_invalid")
            if health._valid_report(latest):
                return raw, latest
    except (snap.SnapshotError, aq.ApprovalQueueError, OSError, ValueError):
        pass
    return None, None


def records(export_root, run_report, now):
    """Return A2 and 20 rows, in that order. Raise ValueError if neither receipt can be bound."""
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    now = now.astimezone(timezone.utc)
    path = Path(run_report)
    report_bytes, latest = _report(path)
    if latest is not None and health._future(latest, now):
        latest = None  # assess() does not present future reports to the check
    raw, manifest = _manifest(export_root, now)
    if latest is None:
        reason = "console_export_no_run"
    elif raw is None:
        reason = "console_export_not_configured" if export_root is None else "console_export_unreadable"
    else:
        # Compare the same generation whose manifest bytes supply the receipt.
        check = health._console_export(None, latest, now, health.LIMITS, pinned_run_id=manifest["run_id"])
        reason = check["reason_code"]
    a2_ok = reason == "console_export_current"

    if raw is not None:
        source = {"schema": ce.SCHEMA, "sha256": hashlib.sha256(raw).hexdigest(), "name": ce.MANIFEST}
    elif report_bytes is not None:
        # A failed observation can be bound to the run being checked, even
        # when no export receipt exists. Never invent a digest for missing bytes.
        source = {"schema": "records-run-report-v1", "sha256": hashlib.sha256(report_bytes).hexdigest(),
                  "name": path.name}
    else:
        raise ValueError("no readable evidence receipt")

    state = anchor._normalize(manifest)
    engine = manifest.get("engine")
    if not isinstance(engine, str) or re.fullmatch(r"[0-9a-f]{40}", engine) is None:
        engine = None
    common = {"schema": "records-golive-evidence-v1", "kind": "live", "actor": "pipeline", "engine": engine,
              "recorded_at": now.isoformat(timespec="seconds").replace("+00:00", "Z")}
    rows = [dict(common, stage=stage, result="pass" if ok else "fail", codes=[code], source=dict(source))
            for stage, ok, code in (("A2", a2_ok, reason.upper()),
                                    ("20", state == anchor.OK, "APPROVAL_ANCHOR_" + state.upper()))]
    for row in rows:
        golive_evidence.validate(row)
    return rows


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--export-root", type=Path, required=True)
    parser.add_argument("--run-report", type=Path, required=True)
    parser.add_argument("--json", action="store_true", help="print a JSON array instead of one row per line")
    args = parser.parse_args(argv)
    try:
        rows = records(args.export_root, args.run_report, datetime.now(timezone.utc))
    except (OSError, ValueError, RecursionError):
        print("CONSOLE_EXPORT_EVIDENCE_BAD_INPUT", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(rows))
    else:
        for row in rows:
            print(json.dumps(row))
    return 0 if all(row["result"] == "pass" for row in rows) else 2


if __name__ == "__main__":
    raise SystemExit(main())
