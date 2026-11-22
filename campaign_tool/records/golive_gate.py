"""Evaluate an attended gate from copied evidence. A receipt never authorizes a step."""

import argparse
from collections.abc import Callable
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal
from fractions import Fraction
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
from typing import NamedTuple
from zoneinfo import ZoneInfo

from .golive_stages import STAGES

SPEC_SCHEMA = "records-golive-gate-spec-v1"
RECEIPT_SCHEMA = "records-golive-gate-v1"
MAX_INPUT = 1 << 20
GIB = 1 << 30
DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
CLOCK = re.compile(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]")
DF_AVAIL = re.compile(rb"[ \t]*Avail[ \t]*\r?\n[ \t]*(-?[0-9]+)[ \t]*(?:\r?\n)?")
MISSING = ("unknown", "GATE_INPUT_MISSING")


def _text(value):
    return isinstance(value, str) and bool(value.strip())


def _fields(value, required, where):
    if not isinstance(value, dict):
        raise ValueError(f"{where} must be an object")
    extra = value.keys() - required
    missing = required - value.keys()
    if extra:
        raise ValueError(f"{where}: unknown fields: {', '.join(sorted(map(repr, extra)))}")
    if missing:
        raise ValueError(f"{where}: missing fields: {', '.join(sorted(missing))}")


def _validate_sha(check):
    expected = check["expected"]
    if not isinstance(expected, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", expected):
        raise ValueError("expected must be a 64-hex SHA-256 pin")


def _validate_floor(check):
    floor = check["min"]
    if (type(floor) not in (int, float) or floor < 0
            or (isinstance(floor, float) and not math.isfinite(floor))):
        raise ValueError("min must be a finite nonnegative GiB number")


def _validate_window(check):
    days = check["days"]
    if (not isinstance(days, list) or not days
            or any(not isinstance(day, str) or day not in DAYS for day in days)
            or len(set(days)) != len(days)):
        raise ValueError("days must be a nonempty list of distinct mon..sun values")
    for field in ("start", "end"):
        value = check[field]
        if not isinstance(value, str) or not CLOCK.fullmatch(value):
            raise ValueError(f"{field} must be HH:MM")
    if check["start"] == check["end"]:
        raise ValueError("start and end must differ")


@contextmanager
def _parent_fd(path):
    """Open directory components without resolving symlinks, including ancestors."""
    path = Path(path).absolute()
    if not path.name:
        raise ValueError("input must name a file")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    fd = os.open(path.anchor, flags)
    try:
        for part in path.parts[1:-1]:
            child = os.open(part, flags, dir_fd=fd)
            os.close(fd)
            fd = child
        yield fd, path.name
    finally:
        os.close(fd)


class Evidence(NamedTuple):
    raw: bytes
    sha256: str


def _read_input(path):
    try:
        with _parent_fd(path) as (parent, name):
            # Nonblocking open avoids waiting on a FIFO before we can reject it.
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
            with os.fdopen(fd, "rb") as stream:
                info = os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_INPUT:
                    return None
                raw = stream.read(MAX_INPUT + 1)
                if len(raw) <= MAX_INPUT:
                    return Evidence(raw, hashlib.sha256(raw).hexdigest())
    except (OSError, ValueError):
        pass
    return None


def _sha(check, get_input, now):
    evidence = get_input(check["input"])
    if evidence is None:
        return MISSING
    if evidence.sha256 == check["expected"].lower():
        return "pass", "GATE_SHA256_MATCH"
    return "fail", "GATE_SHA256_MISMATCH"


def _disk(check, get_input, now):
    evidence = get_input(check["input"])
    if evidence is None:
        return MISSING
    match = DF_AVAIL.fullmatch(evidence.raw)
    if match is None:
        return "unknown", "GATE_DISK_UNPARSED"
    floor = check["min"]
    minimum_bytes = (Fraction(str(floor)) if isinstance(floor, float) else floor) * GIB
    if Decimal(match[1].decode("ascii")) >= minimum_bytes:
        return "pass", "GATE_DISK_SUFFICIENT"
    return "fail", "GATE_DISK_BELOW_FLOOR"


def _window(check, get_input, now):
    local = now.astimezone(ZoneInfo("America/Los_Angeles"))
    clock = f"{local.hour:02d}:{local.minute:02d}"
    day = local.weekday()
    start, end = check["start"], check["end"]
    # Wall-clock intervals are start-inclusive/end-exclusive. An overnight
    # window belongs to the weekday on which it opens, even after midnight.
    if start < end:
        inside = start <= clock < end
    elif clock < end:
        day = (day - 1) % len(DAYS)
        inside = True
    else:
        inside = clock >= start
    if inside and DAYS[day] in check["days"]:
        return "pass", "GATE_WINDOW_OPEN"
    return "fail", "GATE_WINDOW_CLOSED"


def _present(check, get_input, now):
    if get_input(check["input"]) is None:
        return MISSING
    return "pass", "GATE_FILE_PRESENT"


class CheckType(NamedTuple):
    """Fields, optional validation, and a (check, get_input, utc_now) handler."""
    fields: frozenset[str]
    handler: Callable[[dict, Callable[[str], Evidence | None], datetime], tuple[str, str]]
    validate: Callable[[dict], None] | None = None


CHECK_TYPES = {
    "sha256_pin": CheckType(frozenset({"input", "expected"}), _sha, _validate_sha),
    "disk_floor_gib": CheckType(frozenset({"input", "min"}), _disk, _validate_floor),
    "window": CheckType(frozenset({"days", "start", "end"}), _window, _validate_window),
    "file_present": CheckType(frozenset({"input"}), _present),
}


def load_spec(value):
    """Validate the v1 dict and return it unchanged; raise ValueError on bad specs."""
    _fields(value, {"schema", "gate_id", "stages", "title", "checks"}, "spec")
    if value["schema"] != SPEC_SCHEMA:
        raise ValueError(f"schema must be {SPEC_SCHEMA}")
    gate_id = value["gate_id"]
    if not isinstance(gate_id, str) or not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", gate_id):
        raise ValueError("gate_id must be lowercase-dash")
    stages = value["stages"]
    if not isinstance(stages, list) or not stages:
        raise ValueError("stages must be a nonempty list of go-live stage ids")
    for stage in stages:
        if not isinstance(stage, str) or stage not in STAGES:
            raise ValueError(f"unknown stage id: {stage!r}")
    if not _text(value["title"]):
        raise ValueError("title must be nonempty text")
    checks = value["checks"]
    if not isinstance(checks, list) or not checks:
        raise ValueError("checks must be a nonempty list")
    for index, check in enumerate(checks):
        where = f"checks[{index}]"
        if not isinstance(check, dict) or "type" not in check:
            raise ValueError(f"{where} must be an object with a type")
        name = check["type"]
        kind = CHECK_TYPES.get(name) if isinstance(name, str) else None
        if kind is None:
            raise ValueError(f"{where}: unknown check type {name!r}")
        _fields(check, {"type", "label"} | kind.fields, where)
        if not _text(check["label"]):
            raise ValueError(f"{where}: label must be nonempty text")
        if "input" in kind.fields and (not _text(check["input"]) or "=" in check["input"]):
            raise ValueError(f"{where}: input must be a name usable in name=path")
        if kind.validate is not None:
            kind.validate(check)
    return value


def evaluate(spec, inputs, now):
    """Evaluate against explicitly mapped inputs, reading each used name once.

    `now` is an aware datetime. The receipt records UTC, while window checks
    use Pacific wall time: both occurrences of a repeated minute are eligible.
    Only readable, consulted inputs are hashed into the receipt.
    """
    spec = load_spec(spec)
    if not isinstance(now, datetime) or now.utcoffset() is None:
        raise ValueError("now must be a timezone-aware datetime")
    now = now.astimezone(timezone.utc)
    cache = {}

    def get_input(name):
        if name not in cache:
            path = inputs.get(name)
            cache[name] = None if path is None else _read_input(path)
        return cache[name]

    checks = []
    for check in spec["checks"]:
        name = check["type"]
        state, code = CHECK_TYPES[name].handler(check, get_input, now)
        checks.append({"type": name, "label": check["label"], "state": state, "code": code})
    return {
        "schema": RECEIPT_SCHEMA,
        "gate_id": spec["gate_id"],
        "stages": list(spec["stages"]),
        "evaluated_at": now.isoformat(),
        "verdict": "GO" if all(check["state"] == "pass" for check in checks) else "NO-GO",
        "checks": checks,
        "inputs": {name: item.sha256 for name, item in cache.items() if item is not None},
    }


def _unique_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"duplicate JSON field: {key!r}")
        value[key] = item
    return value


def _write_receipt(path, payload):
    with _parent_fd(path) as (parent, name):
        fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                     0o600, dir_fd=parent)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(payload)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--spec", required=True, type=Path)
    parser.add_argument("--input", action="append", default=[], metavar="NAME=PATH")
    parser.add_argument("--out", type=Path, help="existing directory for a new <gate_id>.json receipt")
    parser.add_argument("--json", action="store_true", help="print the JSON receipt rather than a check list")
    args = parser.parse_args(argv)
    inputs = {}
    try:
        for pair in args.input:
            name, _, path = pair.partition("=")
            if not _text(name) or not path or name in inputs:
                raise ValueError("--input requires a distinct NAME=PATH for each input")
            inputs[name] = path
        spec = json.loads(args.spec.read_text(encoding="utf-8"), object_pairs_hook=_unique_object)
        receipt = evaluate(spec, inputs, datetime.now(timezone.utc))
        payload = json.dumps(receipt, indent=2) + "\n"
        if args.out is not None:
            _write_receipt(args.out / (receipt["gate_id"] + ".json"), payload)
    except (ValueError, OSError, RecursionError) as exc:
        parser.error(str(exc))
    if args.json:
        print(payload, end="")
    else:
        print(receipt["verdict"])
        for check in receipt["checks"]:
            print(f'{check["type"]} {json.dumps(check["label"])}: {check["state"]} {check["code"]}')
    return 0 if receipt["verdict"] == "GO" else 1


if __name__ == "__main__":
    raise SystemExit(main())
