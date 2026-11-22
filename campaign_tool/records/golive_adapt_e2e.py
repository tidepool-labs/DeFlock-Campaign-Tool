"""Derive descriptive rehearsal evidence from a reviewed sandbox e2e report."""

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys

_SCHEMA = "records-e2e-proof-v1"
_MARKER = b"e2e-proof sandbox\n"
_DIR = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
_FILE = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
_CODE = re.compile(r"[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)*")

# The current producer does not emit sync by default (delta/adoption isn't sync).
# It has no per-stage positive outcomes for outputs -> 9–14; omit those rows.
_STAGES = (("sync", "2"), ("extract", "5"), ("digest_gates", "8"))


def _read_at(directory, name, *, marker=False):
    with os.fdopen(os.open(name, _FILE, dir_fd=directory), "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise ValueError("expected a regular file")
        if marker:
            if info.st_uid != os.geteuid() or info.st_mode & 0o077:
                raise ValueError("unsafe e2e sandbox marker")
            return stream.read(len(_MARKER) + 1)
        return stream.read()


def _read_report(path):
    directory = os.open(path.parent, _DIR)
    try:
        project = os.open("project", _DIR, dir_fd=directory)
        try:
            if _read_at(project, ".t12-sandbox-v1", marker=True) != _MARKER:
                raise ValueError("invalid e2e sandbox marker")
        finally:
            os.close(project)
        return _read_at(directory, path.name)
    finally:
        os.close(directory)


def _unique_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate e2e report field")
        value[key] = item
    return value


def _positive(name, entry):
    detail = entry.get("detail")
    counts = detail.get("counts") if isinstance(detail, dict) else None
    if not isinstance(counts, dict):
        return False
    output = counts.get("output")
    if type(output) is not int or output <= 0:
        return False
    # digest_gates uses failed/skipped to report blocked/pending respectively.
    # Missing counters are not evidence of zero.
    if any(type(counts.get(key)) is not int or counts[key] != 0 for key in ("failed", "skipped")):
        return False
    if name == "sync":
        return entry["status"] == "ok" and entry.get("code") is None
    return entry["status"] in ("ok", "warn")


def _code(value, fallback):
    return value if isinstance(value, str) and _CODE.fullmatch(value) else fallback


def _rows(value, source, stamp):
    if not isinstance(value, dict) or value.get("schema") != _SCHEMA:
        raise ValueError("unsupported e2e report schema")
    engine = value.get("engine")
    if engine is not None and (not isinstance(engine, str) or not re.fullmatch(r"[0-9a-f]{40}", engine)):
        raise ValueError("invalid rehearsal engine identity")
    timeline, manual_steps = value.get("stages"), value.get("manual_steps")
    if not isinstance(timeline, list) or not isinstance(manual_steps, list):
        raise ValueError("invalid e2e timeline")
    by_name = {}
    for entry in timeline:
        if not isinstance(entry, dict) or not isinstance(entry.get("stage"), str):
            raise ValueError("invalid e2e stage")
        name = entry["stage"]
        if name in by_name:
            raise ValueError("duplicate e2e stage")
        by_name[name] = entry
    manual = {}
    for step in manual_steps:
        if not isinstance(step, dict) or not isinstance(step.get("stage"), str):
            raise ValueError("invalid e2e manual step")
        name = step["stage"]
        why = step.get("why")
        if isinstance(why, str):
            why = why.removeprefix(name + ":")  # producer prefixes unfinished-work codes
        manual.setdefault(name, []).append(_code(why, "REHEARSAL_MANUAL_STEP"))
    for name, stage in _STAGES:
        if name not in by_name:
            continue
        entry = by_name[name]
        status = entry.get("status")
        if status not in ("ok", "warn", "fail", "skipped", "not_run"):
            raise ValueError("invalid e2e stage status")
        codes = manual.get(name, [])
        if status == "fail":
            codes = [_code(entry.get("code"), "REHEARSAL_FAILED_STEP"), *codes]
        if codes or _positive(name, entry):
            yield {
                "schema": "records-golive-evidence-v1", "stage": stage, "kind": "rehearsal",
                "result": "fail" if codes else "pass", "recorded_at": stamp, "engine": engine,
                "source": dict(source), "actor": "rehearsal", "codes": list(dict.fromkeys(codes))[:20],
            }


def records(report, now):
    """Read the reviewed report and its adjacent sandbox marker; write nothing.

    An optional engine field is the run's DFRM-03 identity, not the identity of
    the adapter's current checkout. The caller establishes capture provenance.
    """
    path = Path(report)
    if not path.name or "\\" in path.name:
        raise ValueError("invalid e2e report basename")
    try:
        raw = _read_report(path)
        value = json.loads(raw, object_pairs_hook=_unique_object)
    except (OSError, ValueError, RecursionError):
        raise ValueError("invalid e2e report or sandbox marker") from None
    instant = datetime.fromisoformat(now) if isinstance(now, str) else now
    if not isinstance(instant, datetime) or instant.utcoffset() is None:
        raise ValueError("timestamp must identify an instant")
    stamp = instant.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    source = {"schema": _SCHEMA, "sha256": hashlib.sha256(raw).hexdigest(), "name": path.name}
    return list(_rows(value, source, stamp))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("report", help="reviewed proof-report.json")
    parser.add_argument("--json", action="store_true", help="print a JSON array instead of one row per line")
    args = parser.parse_args(argv)
    try:
        rows = records(args.report, datetime.now(timezone.utc))
    except ValueError:
        print("E2E_EVIDENCE_BAD_INPUT", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(rows))
    else:
        for row in rows:
            print(json.dumps(row))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
