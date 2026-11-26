"""Explain a saved go-live gate receipt in plain words. Evidence only; it never authorizes a step."""

import argparse
import json
import os
import re
import stat
import sys
from datetime import datetime, timezone
from pathlib import Path

RECEIPT_SCHEMA = "records-golive-gate-v1"
MAX_INPUT = 1 << 20
KEYS = {"schema", "gate_id", "stages", "evaluated_at", "verdict", "checks", "inputs"}
CHECK_KEYS = {"type", "label", "state", "code"}
STAMP = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,6})?(?:Z|\+00:00)")
CODE = re.compile(r"[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)*")
GATE_ID = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
DIGEST = re.compile(r"[0-9a-f]{64}")
HEX_RUN = re.compile(r"[0-9a-fA-F]{64}")
PATHISH = re.compile(r"\S*[/\\]\S*")
NOTE = "evidence only: this receipt grants no permission and starts nothing."
UNKNOWN_FIX = "redo the offline capture this check reads from its original source, then have the gate evaluated again"
_UNREADABLE = "receipt must be one regular, non-symlink file of at most 1 MiB"

_RECAPTURE = "have the gate evaluated again"
# code -> (state the gate gives it, what it means, one offline fix or None for a pass)
EXPLANATIONS = {
    "GATE_INPUT_MISSING": (
        "unknown", "the file this check needs was not supplied, or could not be read as a plain file "
        "of at most 1 MiB, so nothing was checked",
        f"re-copy the needed file as a regular file (not a link) of at most 1 MiB and map it to this check's input name, then {_RECAPTURE}"),
    "GATE_SHA256_MATCH": ("pass", "the copied file matches the pinned SHA-256", None),
    "GATE_SHA256_MISMATCH": (
        "fail", "the copied file does not match the pinned SHA-256, so it is not the file that was reviewed "
        "(it was changed, cut short or is a different file)",
        f"re-copy the exact reviewed file from its original source and {_RECAPTURE}; do not edit the pin to fit the copy"),
    "GATE_DISK_UNPARSED": (
        "unknown", "the saved free-space reading is not in the expected form (a line 'Avail' followed by one byte count), "
        "so free space could not be read",
        f"re-capture the free-space reading in that two-line form, save it as the disk input, then {_RECAPTURE}"),
    "GATE_DISK_SUFFICIENT": ("pass", "the captured free space is at or above the required minimum", None),
    "GATE_DISK_BELOW_FLOOR": (
        "fail", "the captured free space is below the required minimum",
        f"re-capture the free-space reading once the disk has enough room (the disk's owner decides how), then {_RECAPTURE}"),
    "GATE_WINDOW_OPEN": ("pass", "the time of this evaluation was inside the allowed Pacific-time window", None),
    "GATE_WINDOW_CLOSED": (
        "fail", "the time of this evaluation was outside the allowed Pacific-time window",
        f"wait for the next allowed window and {_RECAPTURE} then; this receipt's time does not change"),
    "GATE_FILE_PRESENT": ("pass", "the required file was supplied and read as a plain file", None),
    "EVIDENCE_MISSING": (
        "unknown", "no go-live evidence record was found for what this check asks for, or no evidence folder was supplied",
        f"record the missing rehearsal (or earlier gate result) as evidence in the offline evidence folder, then {_RECAPTURE}"),
    "EVIDENCE_UNREADABLE": (
        "unknown", "the evidence folder, or the engine-pairing file this check uses, could not be read as valid records",
        f"re-copy the evidence folder (and the engine-pairing file, if used) from the original so every record is valid, then {_RECAPTURE}"),
    "EVIDENCE_FAILED": (
        "fail", "the latest matching evidence record says the rehearsal or gate failed",
        f"record a new passing rehearsal (or earlier gate result) once the cause is dealt with, then {_RECAPTURE}"),
    "EVIDENCE_STALE": (
        "fail", "the latest passing evidence is older than this check allows",
        f"record a fresh rehearsal (or earlier gate result) so a recent pass is on file, then {_RECAPTURE}"),
    "EVIDENCE_MATCH": ("pass", "a recent passing evidence record is on file and, where checked, matches the engine", None),
    "EVIDENCE_ENGINE_MISMATCH": (
        "fail", "the passing evidence was recorded for a different engine version than the candidate being checked",
        f"record a rehearsal made with the candidate's engine, or re-copy the candidate files that match the recorded engine, then {_RECAPTURE}"),
}


def _unique(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate JSON field")
        value[key] = item
    return value


def _no_constant(_name):
    raise ValueError("non-finite JSON number")


def _text(value):
    return isinstance(value, str) and bool(value.strip())


def _clean(value):
    """Make receipt text safe to print: no digests, absolute paths or control characters."""
    value = PATHISH.sub("[path]", HEX_RUN.sub("[digest]", value))
    return " ".join("".join(ch if ch.isprintable() else " " for ch in value).split())[:120]


def validate(receipt, now=None):
    """Same strictness as the go-live recorder's gate projection; raise ValueError if not consistent.

    `now` (timezone-aware) is the clock for the future-timestamp check; it defaults to the current UTC time.
    """
    if (not isinstance(receipt, dict) or receipt.keys() != KEYS or receipt["schema"] != RECEIPT_SCHEMA
            or not isinstance(receipt["gate_id"], str) or not GATE_ID.fullmatch(receipt["gate_id"])
            or not isinstance(receipt["stages"], list) or not receipt["stages"]
            or any(not _text(stage) for stage in receipt["stages"])
            or not isinstance(receipt["checks"], list) or not receipt["checks"]
            or not isinstance(receipt["inputs"], dict)):
        raise ValueError("invalid gate receipt")
    stamp = receipt["evaluated_at"]
    if not isinstance(stamp, str) or not STAMP.fullmatch(stamp):
        raise ValueError("invalid gate timestamp")
    try:  # the shape is not enough: the recorder also needs a real UTC instant
        evaluated_at = datetime.fromisoformat(stamp)
    except ValueError:
        raise ValueError("invalid gate timestamp") from None
    if evaluated_at > (now if now is not None else datetime.now(timezone.utc)):
        raise ValueError("gate timestamp is in the future")
    for key, pin in receipt["inputs"].items():
        if not key.strip() or "=" in key or not isinstance(pin, str) or not DIGEST.fullmatch(pin):
            raise ValueError("invalid gate input digest")
    for check in receipt["checks"]:
        if (not isinstance(check, dict) or check.keys() != CHECK_KEYS
                or not _text(check["type"]) or not _text(check["label"])
                or check["state"] not in ("pass", "fail", "unknown")
                or not isinstance(check["code"], str) or not CODE.fullmatch(check["code"])):
            raise ValueError("invalid gate check")
        known = EXPLANATIONS.get(check["code"])
        if known is not None and known[0] != check["state"]:
            raise ValueError("gate check state does not fit its code")
    go = all(check["state"] == "pass" for check in receipt["checks"])
    if receipt["verdict"] != ("GO" if go else "NO-GO"):
        raise ValueError("inconsistent gate verdict")


def _describe(receipt, now=None):
    validate(receipt, now)
    rows = []
    for check in receipt["checks"]:
        known = EXPLANATIONS.get(check["code"])
        code = _clean(check["code"])  # an unknown code is receipt text: it may carry a digest
        meaning = known[1] if known else f"unrecognized code {code}"
        fix = None if check["state"] == "pass" else (known[2] if known else UNKNOWN_FIX)
        rows.append({"label": _clean(check["label"]), "state": check["state"], "code": code,
                     "meaning": meaning, "fix": fix})
    return rows


def _header(receipt):
    stages = ", ".join(_clean(stage) for stage in receipt["stages"])
    return f"{receipt['verdict']}: gate {_clean(receipt['gate_id'])} (stages {stages})"


def explain(receipt, now=None):
    """Return plain-text lines for a valid receipt; the verdict printed is the receipt's own."""
    rows = _describe(receipt, now)
    lines = [_header(receipt)]
    for row in rows:
        lines.append(f"{row['label']}: {row['state']} - {row['meaning']}")
        if row["fix"] is not None:
            lines.append(f"fix: {row['fix']}")
    lines.append(NOTE)
    return lines


def read_receipt(path):
    """Read one regular non-symlink file (no symlinked directory on the way), at most 1 MiB, into a dict."""
    path = Path(path).absolute()
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    try:
        if not path.name:
            raise ValueError(_UNREADABLE)
        fd = os.open(path.anchor, flags)
        try:
            for part in path.parts[1:-1]:
                child = os.open(part, flags, dir_fd=fd)
                os.close(fd)
                fd = child
            info = os.stat(path.name, dir_fd=fd, follow_symlinks=False)
            if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_INPUT:
                raise ValueError(_UNREADABLE)
            leaf = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
        finally:
            os.close(fd)
        try:
            info = os.fstat(leaf)
            if not stat.S_ISREG(info.st_mode):
                raise ValueError(_UNREADABLE)
            with os.fdopen(leaf, "rb", closefd=False) as stream:
                raw = stream.read(MAX_INPUT + 1)
        finally:
            os.close(leaf)
    except OSError:
        raise ValueError(_UNREADABLE) from None
    if len(raw) > MAX_INPUT:
        raise ValueError(_UNREADABLE)
    try:
        return json.loads(raw.decode("utf-8"), object_pairs_hook=_unique, parse_constant=_no_constant)
    except (ValueError, RecursionError):
        raise ValueError("receipt is not valid JSON without duplicate fields") from None


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("receipt", type=Path, help="saved gate receipt (records-golive-gate-v1 JSON)")
    parser.add_argument("--json", action="store_true", help="print the explanation as JSON")
    args = parser.parse_args(argv)
    try:
        receipt = read_receipt(args.receipt)
        rows = _describe(receipt)
        if args.json:
            text = json.dumps({"verdict": receipt["verdict"], "gate_id": _clean(receipt["gate_id"]),
                               "stages": [_clean(stage) for stage in receipt["stages"]],
                               "checks": rows, "note": NOTE}, indent=2)
        else:
            text = "\n".join(explain(receipt))
    except ValueError as exc:
        print(f"invalid receipt: {exc}", file=sys.stderr)
        return 2
    print(text)
    return 0 if receipt["verdict"] == "GO" else 1


if __name__ == "__main__":
    raise SystemExit(main())
