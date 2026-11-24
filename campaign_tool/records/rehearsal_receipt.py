"""Record and requalify synthetic rehearsal evidence, without running a driver.

Qualification is evidence only. A receipt is not signed: requalification uses
its recorded sandbox and hash observations, not a new read of the outputs.
"""

import ctypes
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import tempfile

from . import release_manifest
from .run_stages import SANDBOX_MARKER

SCHEMA = "records-rehearsal-v1"
FAILED, MANUAL, EMPTY, OUTPUT, SANDBOX, ENGINE, STALE, MISMATCH = _CODES = (
    "REHEARSAL_FAILED_STEP",
    "REHEARSAL_MANUAL_STEP",
    "REHEARSAL_NO_STEPS",
    "REHEARSAL_OUTPUT_MISMATCH",
    "REHEARSAL_NOT_SANDBOX",
    "REHEARSAL_ENGINE_UNKNOWN",
    "REHEARSAL_STALE",
    "REHEARSAL_ENGINE_MISMATCH",
)
_FIELDS = {
    "schema", "module", "stages", "started_at", "finished_at", "engine",
    "steps", "manual_steps", "codes", "result", "qualified", "qualify_codes",
}
_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW


def _matches(pattern, value):
    return isinstance(value, str) and re.fullmatch(pattern, value) is not None


def _timestamp(value):
    if not _matches(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z", value):
        raise ValueError("invalid rehearsal timestamp")
    return datetime.fromisoformat(value)


def _utc(value):
    if isinstance(value, datetime):
        if value.utcoffset() is None:
            raise ValueError("rehearsal clock must be timezone-aware")
        return value.astimezone(timezone.utc)
    return _timestamp(value)


def _stamp(value):
    return _utc(value).isoformat(timespec="seconds").replace("+00:00", "Z")


def _keys(value, keys):
    return isinstance(value, dict) and value.keys() == keys


def _list_of(value, predicate):
    return isinstance(value, list) and all(predicate(item) for item in value)


def _code(value):
    return _matches(r"[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)*", value)


def _engine(value):
    return _matches(r"[0-9a-fA-F]{40}", value)


def _parts(path):
    if not isinstance(path, str) or "\x00" in path or "\\" in path:
        raise ValueError("invalid rehearsal output path")
    relative = PurePosixPath(path)
    if relative.is_absolute() or not relative.parts or ".." in relative.parts:
        raise ValueError("invalid rehearsal output path")
    return relative.parts


def _validate(receipt):
    """Shape checks plus a recomputation of the verdict from the receipt's own facts."""
    _validate_shape(receipt)
    # Observed output/sandbox codes cannot be re-observed; keep them as recorded and
    # recompute everything else with the same qualifier that build() uses. Stale and
    # target-mismatch are contextual (see qualify), so they are never part of the facts.
    observations = [code for code in (OUTPUT, SANDBOX) if code in receipt["qualify_codes"]]
    facts = _qualify(receipt, _timestamp(receipt["finished_at"]), None, 14, observations)[1]
    recorded = [code for code in receipt["qualify_codes"] if code not in (STALE, MISMATCH)]
    if recorded != facts:
        raise ValueError("qualification_inconsistent")


def _validate_shape(receipt):
    if not _keys(receipt, _FIELDS) or receipt["schema"] != SCHEMA:
        raise ValueError("invalid rehearsal receipt schema")
    if not _matches(r"[A-Za-z_][A-Za-z_0-9]*", receipt["module"]):
        raise ValueError("invalid rehearsal module basename")
    stages = receipt["stages"]
    if not stages or not _list_of(stages, lambda item: isinstance(item, str) and item.strip()):
        raise ValueError("rehearsal stages must be a nonempty list of nonblank strings")
    if len(set(stages)) != len(stages):
        raise ValueError("duplicate rehearsal stage identity")
    if not _list_of(receipt["manual_steps"], lambda item: isinstance(item, str)):
        raise ValueError("invalid rehearsal string list")
    if not _list_of(receipt["codes"], _code):
        raise ValueError("invalid rehearsal driver codes")
    if _timestamp(receipt["finished_at"]) < _timestamp(receipt["started_at"]):
        raise ValueError("rehearsal finished before it started")
    if receipt["engine"] is not None and not _engine(receipt["engine"]):
        raise ValueError("invalid rehearsal engine identity")
    if not isinstance(receipt["steps"], list):
        raise ValueError("invalid rehearsal steps")
    for step in receipt["steps"]:
        if (not _keys(step, {"name", "code", "outputs"}) or not isinstance(step["name"], str)
                or not _code(step["code"]) or not isinstance(step["outputs"], list)):
            raise ValueError("invalid rehearsal step")
        for output in step["outputs"]:
            if (not _keys(output, {"path", "sha256"})
                    or not _matches(r"[0-9a-f]{64}", output["sha256"])):
                raise ValueError("invalid rehearsal output")
            _parts(output["path"])
    codes = receipt["qualify_codes"]
    if not _list_of(codes, lambda code: code in _CODES) or codes != [c for c in _CODES if c in codes]:
        raise ValueError("invalid rehearsal qualification codes")
    if receipt["qualified"] is not (not codes) or receipt["result"] != _result(receipt):
        raise ValueError("inconsistent rehearsal result or qualification")


def _result(receipt):
    return "fail" if receipt["manual_steps"] or any(s["code"] != "OK" for s in receipt["steps"]) else "pass"


def _output_matches(root_fd, output):
    """Open each component relative to an already-open directory, never a link."""
    *parents, leaf = _parts(output["path"])
    fd = os.dup(root_fd)
    try:
        for name in parents:
            child = os.open(name, _DIR_FLAGS, dir_fd=fd)
            os.close(fd)
            fd = child
        # Nonblocking open also lets us reject FIFOs without waiting on a writer.
        with os.fdopen(os.open(leaf, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd), "rb") as source:
            return (stat.S_ISREG(os.fstat(source.fileno()).st_mode)
                    and hashlib.file_digest(source, "sha256").hexdigest() == output["sha256"])
    except (OSError, UnicodeError):
        return False
    finally:
        os.close(fd)


def _has_marker(root_fd):
    try:
        info = os.stat(SANDBOX_MARKER, dir_fd=root_fd, follow_symlinks=False)
    except OSError:
        return False
    # Same owner-only regular-file check as run_stages.sandbox_refusal.
    return stat.S_ISREG(info.st_mode) and info.st_uid == os.geteuid() and not info.st_mode & 0o077


def build(module, result, sandbox, started_at, finished_at, engine):
    """Snapshot driver output and check its hashes at rehearsal finish time.

    Invalid relative path syntax raises ValueError. A well-formed path that
    cannot be safely hashed (including a symlink) is a mismatch, not a read.
    """
    receipt: dict = dict(
        schema=SCHEMA, module=module, started_at=_stamp(started_at), finished_at=_stamp(finished_at),
        engine=engine, qualified=True, qualify_codes=[],
        **deepcopy({k: result[k] for k in ("stages", "steps", "manual_steps", "codes")}),
    )
    receipt["result"] = _result(receipt)
    _validate_shape(receipt)
    outputs = [out for step in receipt["steps"] for out in step["outputs"]]
    try:
        fd = os.open(sandbox, _DIR_FLAGS)
    except OSError:
        marker, matched = False, not outputs
    else:
        try:
            marker = _has_marker(fd)
            # Do not short-circuit after one bad output: check every declaration.
            matched = all([_output_matches(fd, output) for output in outputs])
        finally:
            os.close(fd)
    observations = ([] if matched else [OUTPUT]) + ([] if marker else [SANDBOX])
    receipt["qualified"], receipt["qualify_codes"] = _qualify(
        receipt, _timestamp(receipt["finished_at"]), None, 14, observations,
    )
    _validate(receipt)
    return receipt


def _qualify(receipt, now, target_engine, max_age_days, observations):
    engine = receipt["engine"]
    age = now - _timestamp(receipt["finished_at"])
    bad = set(observations)
    if any(step["code"] != "OK" for step in receipt["steps"]):
        bad.add(FAILED)
    if receipt["manual_steps"]:
        bad.add(MANUAL)
    if not receipt["steps"]:
        bad.add(EMPTY)
    if engine is None:
        bad.add(ENGINE)
    elif target_engine is not None and engine != target_engine:
        bad.add(MISMATCH)
    if not timedelta(0) <= age <= timedelta(days=max_age_days):
        bad.add(STALE)
    return not bad, [code for code in _CODES if code in bad]


def qualify(receipt, *, now, target_engine=None, max_age_days=14):
    """Recompute the verdict, retaining only the recorded filesystem observations."""
    _validate(receipt)
    observations = [code for code in (OUTPUT, SANDBOX) if code in receipt["qualify_codes"]]
    return _qualify(receipt, _utc(now), target_engine, max_age_days, observations)


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate rehearsal receipt key")
        result[key] = value
    return result


def read(path):
    """Load and strictly validate a receipt; let filesystem errors propagate."""
    try:
        receipt = json.loads(Path(path).read_text(encoding="utf-8"), object_pairs_hook=_unique_object)
        _validate(receipt)
    except (ValueError, RecursionError):
        raise ValueError("invalid rehearsal receipt") from None
    return receipt


def write(receipt, out_dir):
    """Publish privately to an existing explicit output directory, without clobbering."""
    _validate(receipt)
    stamp = receipt["finished_at"].replace("-", "").replace(":", "")
    target = Path(out_dir) / f"{stamp}-{receipt['module']}.json"
    # Records runs on Linux. Ordinary rename could replace a concurrent winner.
    rename = getattr(ctypes.CDLL(None, use_errno=True), "renameat2", None)
    if rename is None:
        raise NotImplementedError("atomic no-clobber rehearsal receipt publication unavailable")
    rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    rename.restype = ctypes.c_int
    at_fdcwd, rename_noreplace = -100, 1
    with tempfile.TemporaryDirectory(dir=out_dir, prefix=".rehearsal-") as stage:
        fd, scratch = tempfile.mkstemp(dir=stage)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            os.fchmod(stream.fileno(), 0o600)
            json.dump(receipt, stream, sort_keys=True, indent=2)
            stream.write("\n")
        if rename(at_fdcwd, os.fsencode(scratch), at_fdcwd, os.fsencode(target), rename_noreplace):
            error = ctypes.get_errno()
            raise OSError(error, os.strerror(error), target)
    return target


def engine_identity():
    """Return the commit of a verified install or clean source engine, if known."""
    try:
        report = release_manifest.version_report()
        commit = report.get("commit")
        return commit if _engine(commit) and not report.get("source_dirty") else None
    except Exception:
        return None
