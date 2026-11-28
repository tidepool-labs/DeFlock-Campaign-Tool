"""Record receipt metadata as go-live evidence, not authorization to act."""

import argparse
import ctypes
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys
from uuid import uuid4

from . import golive_evidence, golive_gate, rehearsal_receipt


class EvidenceDuplicate(ValueError):
    """The filename or the full source/stage/kind identity already exists."""

    def __init__(self):
        super().__init__("EVIDENCE_DUPLICATE")


def _read_receipt(path):
    # Parse and hash the same byte snapshot (including whitespace and newlines).
    path = Path(path)
    with golive_gate._parent_fd(path) as (parent, name):
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        with os.fdopen(fd, "rb") as stream:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise ValueError("receipt is not a regular file")
            raw = stream.read()
    try:
        receipt = json.loads(raw.decode("utf-8"), object_pairs_hook=golive_evidence._unique_object)
    except (ValueError, RecursionError):
        raise ValueError("invalid receipt JSON") from None
    return receipt, hashlib.sha256(raw).hexdigest(), path.name


def _record(stage, kind, passed, now, engine, schema, digest, name, actor, codes):
    record = dict(schema="records-golive-evidence-v1", stage=stage, kind=kind,
                  result="pass" if passed else "fail", recorded_at=rehearsal_receipt._stamp(now),
                  engine=engine.lower() if engine is not None else None,
                  source=dict(schema=schema, sha256=digest, name=name), actor=actor, codes=codes)
    golive_evidence.validate(record)
    return record


def from_rehearsal(path, stage, now):
    """Requalify a rehearsal receipt and bind evidence to a declared stage.

    If stage is omitted (None), only a single distinct declared stage is unambiguous.
    """
    receipt, digest, name = _read_receipt(path)
    # qualify() performs the same validation as read(), on the hashed snapshot.
    qualified, codes = rehearsal_receipt.qualify(receipt, now=now)
    if stage is None:
        stages = list(dict.fromkeys(receipt["stages"]))
        if len(stages) != 1:
            raise ValueError("--stage is required unless the rehearsal declares one stage")
        stage = stages[0]
    if stage not in receipt["stages"]:
        raise ValueError("stage is not declared in the rehearsal receipt")
    # This adapter has no trusted target-engine context with which to clear a
    # previously recorded failure. Add contextual failures at the new clock.
    codes = [code for code in rehearsal_receipt._CODES
             if code in codes or code in receipt["qualify_codes"]]
    return _record(stage, "rehearsal", qualified and receipt["qualified"], now, receipt["engine"],
                   rehearsal_receipt.SCHEMA, digest, name, "rehearsal", codes)


def from_gate(path, now):
    """Project a consistent gate receipt into one record per distinct stage."""
    receipt, digest, name = _read_receipt(path)
    if (not isinstance(receipt, dict)
            or receipt.keys() != {"schema", "gate_id", "stages", "evaluated_at", "verdict", "checks", "inputs"}
            or receipt["schema"] != golive_gate.RECEIPT_SCHEMA
            or not isinstance(receipt["gate_id"], str)
            or not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", receipt["gate_id"])
            or not isinstance(receipt["stages"], list) or not receipt["stages"]
            or any(not isinstance(stage, str) for stage in receipt["stages"])
            or not isinstance(receipt["checks"], list) or not receipt["checks"]
            or not isinstance(receipt["inputs"], dict)):
        raise ValueError("invalid gate receipt")
    stamp = receipt["evaluated_at"]
    # The gate evaluator emits UTC ISO format, including +00:00 and optional fractions.
    if (not isinstance(stamp, str)
            or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,6})?(?:Z|\+00:00)", stamp)):
        raise ValueError("invalid gate timestamp")
    evaluated_at = datetime.fromisoformat(stamp)
    if evaluated_at > rehearsal_receipt._utc(now):
        raise ValueError("gate timestamp is in the future")
    for key, pin in receipt["inputs"].items():
        if not key.strip() or "=" in key or not isinstance(pin, str) or not re.fullmatch(r"[0-9a-f]{64}", pin):
            raise ValueError("invalid gate input digest")
    failed_codes = []
    for check in receipt["checks"]:
        if (not isinstance(check, dict) or check.keys() != {"type", "label", "state", "code"}
                or any(not isinstance(check[key], str) or not check[key].strip() for key in ("type", "label"))
                or check["state"] not in ("pass", "fail", "unknown")
                or not isinstance(check["code"], str)
                or not re.fullmatch(r"[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)*", check["code"])):
            raise ValueError("invalid gate check")
        if check["state"] != "pass":
            failed_codes.append(check["code"])
    passed = not failed_codes
    if receipt["verdict"] != ("GO" if passed else "NO-GO"):
        raise ValueError("inconsistent gate verdict")
    codes = list(dict.fromkeys(failed_codes))
    if len(codes) > 20:
        codes = codes[:19] + ["CODES_TRUNCATED"]
    return [_record(stage, "gate", passed, evaluated_at, None, golive_gate.RECEIPT_SCHEMA,
                    digest, name, "operator", codes.copy())
            for stage in dict.fromkeys(receipt["stages"])]


_MAX_LIVE_ROWS = 28  # one row per catalogue stage at most


def from_live(path, now):
    """Return the live rows of one adapter ``--json`` array, validated together.

    The adapters (for example golive_adapt_facts, golive_adapt_gaps) already
    bind each row to the output file they read; this only checks the rows and
    refuses anything that is not plainly live evidence. Nothing is returned
    unless every row is valid, so a bad row never leaves a partial record.
    """
    rows, _digest, _name = _read_receipt(path)
    if not isinstance(rows, list) or not rows:
        raise ValueError("live input must be a non-empty JSON array of evidence rows")
    if len(rows) > _MAX_LIVE_ROWS:
        raise ValueError(f"live input has more than {_MAX_LIVE_ROWS} rows")
    stages = set()
    for row in rows:
        golive_evidence.validate(row)
        # Rehearsal and gate evidence must come from their receipts, never
        # from a hand-written array passed through the live path.
        if row["kind"] != "live":
            raise ValueError("live input row is not kind live")
        if row["actor"] == "rehearsal":
            raise ValueError("live input row has a rehearsal actor")
        if datetime.fromisoformat(row["recorded_at"].replace("Z", "+00:00")) > rehearsal_receipt._utc(now):
            raise ValueError("live input row is future-dated")
        if row["result"] == "pass" and row["codes"]:
            raise ValueError("live input pass row carries failure codes")
        if row["stage"] in stages:
            raise ValueError("live input names a stage twice")
        stages.add(row["stage"])
    return rows


def _identity(record):
    return record["source"]["sha256"], record["stage"], record["kind"]


def _rename_new(dir_fd, scratch, name):
    # Linux RENAME_NOREPLACE, as used by the rehearsal receipt writer. A plain
    # rename would clobber an existing filename even with our advisory lock.
    rename = getattr(ctypes.CDLL(None, use_errno=True), "renameat2", None)
    if rename is None:
        raise OSError("atomic no-clobber evidence publication unavailable")
    rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    rename.restype = ctypes.c_int
    if rename(dir_fd, os.fsencode(scratch), dir_fd, os.fsencode(name), 1):
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), name)


def append(record, evidence_dir):
    """Append a private JSON record to an existing directory; return its Path."""
    golive_evidence.validate(record)
    payload = (json.dumps(record, sort_keys=True) + "\n").encode("utf-8")
    if len(payload) > golive_evidence._MAX_SIZE:
        raise ValueError("evidence file exceeds 64 KiB")
    name = golive_evidence._filename(record)
    directory = Path(evidence_dir)
    dir_fd = golive_evidence._open_directory(directory)
    try:
        # Serialize full-digest deduplication with other recorder invocations,
        # including duplicates whose timestamps give them different filenames.
        fcntl.flock(dir_fd, fcntl.LOCK_EX)
        with os.scandir(dir_fd) as entries:
            for count, entry in enumerate(entries, start=1):
                if entry.name == name:
                    raise EvidenceDuplicate()
                if count >= golive_evidence._MAX_FILES:
                    raise ValueError("evidence directory is full")
                if entry.name.endswith(".json"):
                    old = golive_evidence._read_at(dir_fd, entry.name)
                    if _identity(old) == _identity(record):
                        raise EvidenceDuplicate()
        scratch = f".golive-{uuid4().hex}.tmp"
        fd = os.open(scratch, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=dir_fd)
        try:
            with os.fdopen(fd, "wb") as stream:
                os.fchmod(stream.fileno(), 0o600)
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            try:
                _rename_new(dir_fd, scratch, name)
            except FileExistsError:
                raise EvidenceDuplicate() from None
        finally:
            try:
                os.unlink(scratch, dir_fd=dir_fd)
            except FileNotFoundError:
                pass
    finally:
        os.close(dir_fd)
    return directory / name


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("kind", choices=("rehearsal", "gate", "live"))
    parser.add_argument("receipt", type=Path, help="receipt file, or for live an adapter --json output file")
    parser.add_argument("--evidence", required=True, type=Path, help="existing evidence directory")
    parser.add_argument("--stage", help="select a declared stage (required for multi-stage rehearsals)")
    args = parser.parse_args(argv)
    now = datetime.now(timezone.utc)
    try:
        if args.kind == "rehearsal":
            records = [from_rehearsal(args.receipt, args.stage, now)]
        else:
            records = from_gate(args.receipt, now) if args.kind == "gate" else from_live(args.receipt, now)
            if args.stage is not None:
                records = [record for record in records if record["stage"] == args.stage]
                if not records:
                    raise ValueError("stage is not declared in the "
                                     + ("gate receipt" if args.kind == "gate" else "live input"))
        for record in records:
            print(append(record, args.evidence).name)
    except (ValueError, OSError) as exc:
        message = (exc.strerror or "filesystem refusal") if isinstance(exc, OSError) else str(exc)
        print(f"golive_record: {message}", file=sys.stderr)
        return 2
    return int(any(record["result"] == "fail" for record in records))


if __name__ == "__main__":
    raise SystemExit(main())
