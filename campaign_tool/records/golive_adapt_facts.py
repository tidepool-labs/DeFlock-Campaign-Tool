"""Derive stage 9 (Facts) and 10 (Policy comparison) live evidence from a copied run-outputs folder.

Both stages map to the aggregate ``outputs`` stage, so the ledger only says AGGREGATE_ONLY. The per-stage positive
outcome is the file each writer leaves in ``run-outputs/<run_id>/``. Positive was chosen from the writers:
``write_run_factsheets`` also writes ``agencies: []`` for an empty ledger and ``write_run_comparison`` writes
``rows: []``, so a present, valid file is not enough. Stage 9 passes only for a sheet bound to this run listing at
least one agency (whether any fact was FOUND is not asked: statements are review-required quotes); stage 10 only for
a bound report with at least one row. The folder must be an owner-only, non-symlink directory named like the
finished, non-stub run in the run report; that it is a copy cannot be proved here. A pass is machine-valid metadata,
not semantic acceptance. Output carries counts and codes only, never agency text. Read-only; a file over
MAX_FILE_BYTES is refused unread.
"""

import argparse
import hashlib
import json
import os
import stat
import sys
from datetime import datetime, timezone
from pathlib import Path

from . import golive_evidence, pipeline_health as health
from .outcomes import comparison, factsheets

MAX_FILE_BYTES = factsheets.MAX_OUTPUT_BYTES
UNBOUND = "OUTPUTS_RUN_UNBOUND"
BAD_INPUT = "FACTS_EVIDENCE_BAD_INPUT"
# stage, prefix, file name, writer schema, list key naming the items that must be present
_KINDS = (("9", "FACTS", "agency-factsheets.json", factsheets.SCHEMA, "agencies", "agency_id"),
          ("10", "COMPARISON", "comparison.json", comparison.SCHEMA, "rows", "agency_id"))


class _Unbound(ValueError):
    pass


def _file(name, cap, dir_fd=None):
    """Return (bytes, status); status is ok, missing or invalid. A special file or oversize file is never read."""
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=dir_fd)
    except FileNotFoundError:
        return None, "missing"
    except OSError:
        return None, "invalid"
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > cap:
            return None, "invalid"
        chunks, total = [], 0
        while chunk := os.read(fd, 1 << 20):
            total += len(chunk)
            if total > cap:
                return None, "invalid"
            chunks.append(chunk)
        return b"".join(chunks), "ok"
    except OSError:
        return None, "invalid"
    finally:
        os.close(fd)


def _refuse_constant(_):
    raise ValueError("non-finite JSON number")


def _strict(raw):
    return json.loads(raw.decode("utf-8"), object_pairs_hook=golive_evidence._unique_object,
                      parse_constant=_refuse_constant)


def _bound_report(path, now):
    """Return (bytes, report) for a valid, finished, non-stub run whose outputs stage completed, else _Unbound."""
    try:
        raw, status = _file(path, health.MAX_JSON_BYTES)
        if status != "ok":
            raise ValueError("run report unreadable")
        report = _strict(raw)
        codes = {item["code"] for item in report["needs_g"]}
        outputs = next(stage for stage in report["stages"] if stage["name"] == "outputs")
        if (not health._valid_report(report) or health._future(report, now) or report["mode"] == "test"
                or "STUB_STAGE_BACKEND" in codes or outputs["status"] not in ("ok", "warn")):
            raise ValueError("run report cannot bind outputs")
        return raw, report
    except (OSError, ValueError, TypeError, KeyError, StopIteration, RecursionError):
        raise _Unbound(UNBOUND) from None


def _open_outputs(outputs_dir, run_id):
    try:
        path = Path(os.path.abspath(outputs_dir))
        if path.name != run_id:
            raise ValueError("directory name is not the run id")
        fd = golive_evidence._open_directory(path)  # refuses a symlink at any component
    except (OSError, ValueError):
        raise _Unbound(UNBOUND) from None
    info = os.fstat(fd)
    if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) & 0o077:
        os.close(fd)
        raise _Unbound(UNBOUND)
    return fd


def _judge(raw, status, run_id, schema, key, ident):
    """Return (outcome, count); outcome is pass, missing, empty or invalid. Never keeps any record text."""
    if status != "ok":
        return status, 0
    if not raw.strip():
        return "empty", 0
    try:
        value = _strict(raw)
    except (ValueError, RecursionError):
        return "invalid", 0
    if not isinstance(value, dict) or value.get("schema") != schema or value.get("run_id") != run_id:
        return "invalid", 0
    items = value.get(key)
    if (not isinstance(items, list)
            or any(not isinstance(i, dict) or not isinstance(i.get(ident), str) or not i[ident].strip()
                   for i in items)):
        return "invalid", 0
    return ("pass", len(items)) if items else ("empty", 0)


def _assess(outputs_dir, run_report, now):
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    now = now.astimezone(timezone.utc)
    report_bytes, report = _bound_report(run_report, now)
    run_id = report["run_id"]
    dir_fd = _open_outputs(outputs_dir, run_id)
    rows, counts = [], []
    try:
        for stage, prefix, name, schema, key, ident in _KINDS:
            raw, status = _file(name, MAX_FILE_BYTES, dir_fd)
            outcome, count = _judge(raw, status, run_id, schema, key, ident)
            if raw is not None:
                source = {"schema": schema, "sha256": hashlib.sha256(raw).hexdigest(), "name": name}
            else:  # No bytes of the file exist to hash; bind the observation to the run report instead.
                source = {"schema": "records-run-report-v1", "sha256": hashlib.sha256(report_bytes).hexdigest(),
                          "name": Path(run_report).name}
            row = {"schema": "records-golive-evidence-v1", "stage": stage, "kind": "live",
                   "result": "pass" if outcome == "pass" else "fail",
                   "recorded_at": now.isoformat(timespec="seconds").replace("+00:00", "Z"),
                   "engine": None, "actor": "pipeline",
                   "codes": [] if outcome == "pass" else [f"{prefix}_FILE_{outcome.upper()}"], "source": source}
            golive_evidence.validate(row)
            rows.append(row)
            counts.append(count)
    finally:
        os.close(dir_fd)
    return rows, counts


def records(outputs_dir, run_report, now):
    """Return the stage 9 then stage 10 rows. An unbindable folder or run report raises ValueError(OUTPUTS_RUN_UNBOUND)."""
    return _assess(outputs_dir, run_report, now)[0]


def main(argv=None):
    parser = argparse.ArgumentParser(description="Derive stage 9 and 10 live evidence from a copied run-outputs folder.",
                                     allow_abbrev=False)
    parser.add_argument("--outputs-dir", type=Path, required=True)
    parser.add_argument("--run-report", type=Path, required=True)
    parser.add_argument("--json", action="store_true", help="print a JSON array instead of one text row per stage")
    args = parser.parse_args(argv)
    try:
        rows, counts = _assess(args.outputs_dir, args.run_report, datetime.now(timezone.utc))
    except _Unbound:
        print(UNBOUND, file=sys.stderr)
        return 2
    except (OSError, ValueError, RecursionError):
        print(BAD_INPUT, file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(rows))
    else:
        for row, count in zip(rows, counts):
            print(row["stage"], row["kind"], row["result"], ",".join(row["codes"]) or "-", f"count={count}", sep="\t")
    return 0 if all(row["result"] == "pass" for row in rows) else 2


if __name__ == "__main__":
    raise SystemExit(main())
