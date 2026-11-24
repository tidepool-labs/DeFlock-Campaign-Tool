"""Derive go-live stage 11 and 12 evidence from a copied run-outputs folder.

OUTPUTS_DIR is an owner-only, non-symlink copy of ``run-outputs/<run_id>/`` named exactly like the run id of
RUN_REPORT (a valid, finished, non-test, non-stub run report; otherwise OUTPUTS_RUN_UNBOUND). A pass is
machine-valid, review-required metadata, never acceptance of any proposal, date or request; only file
bytes are read and no request text is echoed.

Positive for each file (all: regular, owner-only, single link, bounded, strict JSON object, no duplicate keys
or non-finite numbers):

* ``outcomes/gap-proposals.json`` (stage 11, ``gap_proposals.write_run_gap_proposals``): valid against
  ``schemas/records-gap-proposals-v1.json``, confidence counts equal the proposal total, the review list names
  only listed proposals, and ``run_id`` is this run's. Zero proposals passes only with that run id.
* ``outcomes/received-items.json`` (stage 12, ``received_items.write_run_received_items``): its schema string,
  an ``items`` list and this run's ``run_id``.
* ``CURRENT-REQUEST-REGISTER.json`` (stage 12, ``register.write_for_run``): its schema string and at least one
  request. It has no run id, so it must also agree with the timeline (as-of date, ordered agency/request
  identities); every row must name its agency and request (a ledger id may be null for a tracker-only row).
  A register copied from another run fails.
* ``outcomes/timeline.json`` (stage 12, ``timeline.write_run_timeline``): its schema string, a consistent
  request count, a named agency and request per row and this run's ``run_id``.

A stage also fails with its own ``golive_stages.OUTPUT_FAILURES`` code when the run report carries it.
"""

import argparse
import hashlib
import json
import os
import re
import stat
import sys
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path

from campaign_tool import law
from . import approval_queue as aq, golive_evidence, golive_stages, pipeline_health as health, run_report
from .outcomes import gap_proposals, received_items, register, timeline

UNBOUND = "OUTPUTS_RUN_UNBOUND"
MAX_BYTES = received_items.MAX_OUTPUT_BYTES
RUN_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")
GAPS, ITEMS, REGISTER, TIMELINE = gap_proposals.OUTPUT, gap_proposals.INPUT, "CURRENT-REQUEST-REGISTER.json", "timeline.json"
GAPS_SCHEMA, REGISTER_SCHEMA = "records-golive-gaps-files-v1", "records-golive-register-files-v1"


class Unbound(ValueError):
    pass


@lru_cache(maxsize=1)
def _gap_schema():
    path = Path(__file__).resolve().parents[2] / "schemas" / "records-gap-proposals-v1.json"
    return json.loads(path.read_text(encoding="utf-8"))


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def _open(path, flags=0, dir_fd=None):
    return os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | flags, dir_fd=dir_fd)


def _report(path, now):
    """Parse the run report only if it is a valid, finished, real (not test or stub) run's report."""
    path = Path(path)
    try:
        parent = aq._open_chain(path.parent)
        try:
            fd = _open(path.name, dir_fd=parent)
        finally:
            os.close(parent)
        try:
            raw = aq._read_capped(fd, health.MAX_JSON_BYTES) if stat.S_ISREG(os.fstat(fd).st_mode) else b""
        finally:
            os.close(fd)
        report = aq._strict_load(raw, "run_report_invalid") if len(raw) <= health.MAX_JSON_BYTES else None
        run_report.validate_run_report(report)
        codes = {item["code"] for item in report["needs_g"]}
        stage = next(item for item in report["stages"] if item["name"] == "outputs")
        if (report["mode"] == "test" or "STUB_STAGE_BACKEND" in codes or health._future(report, now)
                or not RUN_ID.fullmatch(report["run_id"])):
            raise Unbound(UNBOUND)
    except (aq.ApprovalQueueError, OSError, ValueError, KeyError, TypeError, RecursionError):
        raise Unbound(UNBOUND) from None
    return report, raw, path.name, codes | ({stage["error_code"]} - {None})


def _file(dir_fd, name, absent):
    """(state, bytes) of one regular owner-only file below dir_fd; ``absent`` is the state for a missing parent."""
    if dir_fd is None:
        return absent, None
    try:
        fd = _open(name, dir_fd=dir_fd)
    except OSError as error:
        return ("MISSING" if isinstance(error, FileNotFoundError) else "UNSAFE"), None
    try:
        info = os.fstat(fd)
        raw = aq._read_capped(fd, MAX_BYTES) if aq._private(info, False) and info.st_size <= MAX_BYTES else None
    finally:
        os.close(fd)
    return ("OK", raw) if raw is not None and len(raw) <= MAX_BYTES else ("UNSAFE", None)


def _gaps(doc, run_id):
    try:
        law.validate(doc, _gap_schema())
        proposals = [p for a in doc["agencies"] for r in a["requests"] for p in r["proposals"]]
        ids, by = {p["proposal_id"] for p in proposals}, doc["counts"]["by_confidence"]
        ok = (sorted(by) == ["high", "low", "medium"] and all(type(v) is int for v in by.values())
              and sum(by.values()) == len(proposals) == len(ids) and set(doc["review_list"]) <= ids)
    except (ValueError, KeyError, TypeError, RecursionError):
        ok = False
    return "SCHEMA" if not ok else "OK" if doc["run_id"] == run_id else "RUN"


def _items(doc, run_id):
    ok = doc.get("schema") == received_items.SCHEMA and type(doc.get("items")) is list and type(doc.get("run_id")) is str
    return "SCHEMA" if not ok else "OK" if doc["run_id"] == run_id else "RUN"


def _named(row, key, optional=False):
    value = row.get(key)
    return (optional and value is None) or (type(value) is str and bool(value.strip()))


def _shape(doc, schema):
    """Schema string, a consistent request count and a row identity (agency, request id, ledger id or None) per row."""
    rows = doc.get("requests")
    return (doc.get("schema") == schema and type(rows) is list and type(doc.get("request_count")) is int
            and doc["request_count"] == len(rows) and type(doc.get("as_of")) is str
            and all(type(row) is dict and _named(row, "agency_key") and _named(row, "request_id")
                    and _named(row, "ledger_request_id", True) for row in rows))


def _register(doc, _run_id):
    return "SCHEMA" if not _shape(doc, register.SCHEMA) else "OK" if doc["requests"] else "EMPTY"


def _timeline(doc, run_id):
    ok = _shape(doc, timeline.SCHEMA) and type(doc.get("run_id")) is str
    return "SCHEMA" if not ok else "OK" if doc["run_id"] == run_id else "RUN"


def _identity(doc):
    return doc["as_of"], [[row.get(key) for key in ("agency_key", "ledger_request_id", "request_id")]
                          for row in doc["requests"]]


def _read(dir_fd, absent, name, check, run_id):
    """(state, bytes, document) for one file; state is OK only when it parsed strictly and passed ``check``."""
    state, raw = _file(dir_fd, name, absent)
    doc = None
    if state == "OK":
        try:
            doc = aq._strict_load(raw, "json")
        except aq.ApprovalQueueError:
            pass
        state = check(doc, run_id) if type(doc) is dict else "INVALID"
    return state, raw, doc


def _row(stage, schema, own, states, raws, run_id, now, report_raw, report_name):
    codes = list(own) + [f"{label}_{state}" for label, state in states if state != "OK"]
    hashed = "".join(f"{name}\t{_sha(raw)}\n" for name, raw in sorted(raws.items()) if raw is not None)
    source = ({"schema": schema, "sha256": _sha(hashed.encode()), "name": run_id} if hashed else
              {"schema": "records-run-report-v1", "sha256": _sha(report_raw), "name": report_name})
    row = {"schema": "records-golive-evidence-v1", "stage": stage, "kind": "live", "result": "fail" if codes else "pass",
           "recorded_at": now.isoformat(timespec="seconds").replace("+00:00", "Z"), "engine": None, "source": source,
           "actor": "pipeline", "codes": codes}
    golive_evidence.validate(row)
    return row


def records(outputs_dir, run_report_path, now):
    """Return the stage 11 then stage 12 rows. Raise Unbound (OUTPUTS_RUN_UNBOUND) if the copy is not this run's."""
    now = (now.replace(tzinfo=timezone.utc) if now.tzinfo is None else now).astimezone(timezone.utc)
    report, report_raw, report_name, report_codes = _report(run_report_path, now)
    run_id, base = report["run_id"], os.path.abspath(outputs_dir)
    try:
        root = aq._open_chain(base)
    except aq.ApprovalQueueError:
        raise Unbound(UNBOUND) from None
    outcomes = None
    try:
        if os.path.basename(base) != run_id or not aq._private(os.fstat(root), True):
            raise Unbound(UNBOUND)
        try:
            outcomes, absent = _open("outcomes", os.O_DIRECTORY, root), "UNSAFE"
        except OSError as error:
            absent = "MISSING" if isinstance(error, FileNotFoundError) else "UNSAFE"
        if outcomes is not None and not aq._private(os.fstat(outcomes), True):
            os.close(outcomes)
            outcomes = None
        gap = _read(outcomes, absent, GAPS, _gaps, run_id)
        items = _read(outcomes, absent, ITEMS, _items, run_id)
        reg = _read(root, absent, REGISTER, _register, run_id)
        line = _read(outcomes, absent, TIMELINE, _timeline, run_id)
    finally:
        os.close(root)
        if outcomes is not None:
            os.close(outcomes)
    if reg[0] == line[0] == "OK" and _identity(reg[2]) != _identity(line[2]):
        reg = ("RUN", reg[1], reg[2])
    own = {stage: [c for c in golive_stages.OUTPUT_FAILURES[stage] if c in report_codes] for stage in ("11", "12")}
    common = (run_id, now, report_raw, report_name)
    return [_row("11", GAPS_SCHEMA, own["11"], [("GAPS_FILE", gap[0])], {GAPS: gap[1]}, *common),
            _row("12", REGISTER_SCHEMA, own["12"], [("REGISTER_FILE_ITEMS", items[0]), ("REGISTER_FILE_REGISTER", reg[0]),
                                                    ("REGISTER_FILE_TIMELINE", line[0])],
                 {ITEMS: items[1], REGISTER: reg[1], TIMELINE: line[1]}, *common)]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--outputs-dir", type=Path, required=True)
    parser.add_argument("--run-report", type=Path, required=True)
    parser.add_argument("--json", action="store_true", help="print a JSON array instead of one row per line")
    args = parser.parse_args(argv)
    try:
        rows = records(args.outputs_dir, args.run_report, datetime.now(timezone.utc))
    except (OSError, ValueError, RecursionError) as error:
        print(UNBOUND if isinstance(error, Unbound) else "GAPS_EVIDENCE_BAD_INPUT", file=sys.stderr)
        return 2
    print(json.dumps(rows) if args.json else "\n".join(json.dumps(row) for row in rows))
    return 0 if all(row["result"] == "pass" for row in rows) else 2


if __name__ == "__main__":
    raise SystemExit(main())
