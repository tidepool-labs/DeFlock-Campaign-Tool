"""Derive stage-19 evidence from captured backup reports, without running recovery."""

import argparse
import hashlib
import json
import os
import re
import stat
import sys
from datetime import datetime, timezone

from .golive_evidence import validate

_V1 = "records-backup-v1"
_V2 = "records-backup-v2"
_RESTORE_V1 = "records-restore-v1"
_V2_FIELDS = {"schema", "manifest_sha256", "files", "sqlite_snapshots",
              "declared_scope_closed", "live_pipeline_recovery_proven"}
_V2_RESTORE = {"isolated": True, "activation": False, "sends": 0, "replay": 0,
               "runnable_reconciliation_proven": False}
_V1_BACKUP = {"scope": "explicit_selected_files", "backup_complete": True}
_V1_RESTORE = {"scope": "explicit_selected_files", "hash_reconciliation": "exact",
               "isolated": True, "sends": 0, "deployments": 0, "activation": False}
_LIMIT = 64 * 1024


def _digest(value):
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def _utc(value):
    try:
        stamp = datetime.fromisoformat(value) if isinstance(value, str) else value
        if isinstance(stamp, datetime) and stamp.utcoffset() is not None:
            return stamp.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    except (ValueError, OverflowError):
        pass
    raise ValueError("timestamp must identify an instant")


def _matches(receipt, fields):
    return all(type(receipt.get(key)) is type(value) and receipt[key] == value
               for key, value in fields.items())


def _count(receipt, key, minimum=0):
    value = receipt.get(key)
    return type(value) is int and value >= minimum


def _v2_report(receipt, extra=None):
    extra = extra or {}
    return (isinstance(receipt, dict) and receipt.keys() == _V2_FIELDS | extra.keys()
            and _matches(receipt, {"schema": _V2, "declared_scope_closed": True,
                                   "live_pipeline_recovery_proven": False, **extra})
            and _count(receipt, "files", 1) and _count(receipt, "sqlite_snapshots")
            and receipt["sqlite_snapshots"] <= receipt["files"])


def _v1_report(receipt, fields):
    return (_matches(receipt, fields) and _digest(receipt.get("manifest_sha256"))
            and _count(receipt, "files", 1) and _count(receipt, "total_bytes"))


def _v2_code(backup, verify, digest):
    if not _v2_report(backup):
        return "BACKUP_VERIFY_FAILED"
    if backup["manifest_sha256"] != digest:
        return "BACKUP_MISMATCH"
    if verify is None:
        return "BACKUP_VERIFY_MISSING"
    # verify_v2 raises on failure; a normal, closed-scope report signals success.
    # A restore report has additional keys and is not a verify report.
    if not _v2_report(verify):
        return "BACKUP_VERIFY_FAILED"
    if verify["manifest_sha256"] != digest:
        return "BACKUP_MISMATCH"
    return None


def _receipt_hash(receipt):
    raw = json.dumps(receipt, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    return hashlib.sha256(raw).hexdigest()


def _record(kind, stamp, schema, digest, name, code=None):
    record = {
        "schema": "records-golive-evidence-v1", "stage": "19", "kind": kind,
        "result": "fail" if code else "pass", "recorded_at": stamp, "engine": None,
        "source": {"schema": schema, "sha256": digest, "name": name},
        "actor": "operator", "codes": [code] if code else [],
    }
    validate(record)
    return record


def _restore_record(receipt, now):
    if not isinstance(receipt, dict):
        raise ValueError("unsupported restore receipt")
    schema = receipt.get("schema")
    if (schema == _RESTORE_V1
            and receipt.keys() == _V1_RESTORE.keys() | {"schema", "manifest_sha256", "files", "total_bytes"}
            and _v1_report(receipt, _V1_RESTORE)):
        name = "RESTORE-RECEIPT.json"
    elif _v2_report(receipt, _V2_RESTORE) and _digest(receipt["manifest_sha256"]):
        name = "restore-receipt.json"
    else:
        raise ValueError("unsupported restore receipt")
    return _record("rehearsal", _utc(now), schema, _receipt_hash(receipt), name)


def records(backup_receipt, verify_receipt, now, *, digest_row=None, restore_receipt=None):
    """Return live evidence first, then optional isolated-restore evidence.

    The caller must establish capture provenance (which API produced each
    report) and pin the digest row independently. Matching metadata does not
    authenticate either input or establish recoverability on the live host.
    """
    if not isinstance(backup_receipt, dict):
        raise ValueError("unsupported backup receipt")
    verify_v2 = isinstance(verify_receipt, dict) and verify_receipt.get("schema") == _V2
    if backup_receipt.get("schema") == _V2 or verify_v2:
        if not isinstance(digest_row, dict) or not _digest(digest_row.get("manifest_sha256")):
            raise ValueError("trusted digest row required for v2")
        digest = digest_row["manifest_sha256"]
        stamp = _utc(digest_row.get("at"))
        code = _v2_code(backup_receipt, verify_receipt, digest)
        live = _record("live", stamp, _V2, digest, "manifest.json", code)
    elif (backup_receipt.keys() == _V1_BACKUP.keys() | {"manifest_sha256", "files", "total_bytes"}
          and _v1_report(backup_receipt, _V1_BACKUP)):
        # recovery.backup has no v1 verify report. A v1 restore is separate
        # evidence and cannot certify a live pair.
        code = "BACKUP_VERIFY_MISSING" if verify_receipt is None else "BACKUP_VERIFY_FAILED"
        live = _record("live", _utc(now), _V1, _receipt_hash(backup_receipt), "backup-receipt.json", code)
    else:
        raise ValueError("unsupported backup receipt")
    return [live] if restore_receipt is None else [live, _restore_record(restore_receipt, now)]


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON field")
        result[key] = value
    return result


def _read_with_bytes(path):
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as source:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError("receipt must be a regular file")
        raw = source.read(_LIMIT + 1)
    if len(raw) > _LIMIT:
        raise ValueError("receipt exceeds size bound")
    value = json.loads(raw, object_pairs_hook=_unique_object)
    if not isinstance(value, dict):
        raise ValueError("expected a JSON object")
    return value, raw


def _read(path):
    return _read_with_bytes(path)[0] if path is not None else None


def main(argv=None):
    parser = argparse.ArgumentParser(prog="golive_adapt_backup", description=__doc__, allow_abbrev=False)
    parser.add_argument("backup", help="captured backup API return (JSON)")
    parser.add_argument("verify", nargs="?", help="captured verify API return (JSON)")
    parser.add_argument("--digest-row", help="one independently pinned digest row (JSON)")
    parser.add_argument("--restore", help="optional isolated restore receipt (JSON)")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    try:
        restore_bytes = b""
        if args.restore is not None:
            restore, restore_bytes = _read_with_bytes(args.restore)
        else:
            restore = None
        rows = records(_read(args.backup), _read(args.verify), datetime.now(timezone.utc),
                       digest_row=_read(args.digest_row), restore_receipt=restore)
        if args.restore is not None:
            rows[1]["source"]["sha256"] = hashlib.sha256(restore_bytes).hexdigest()
            rows[1]["source"]["name"] = os.path.basename(args.restore)
            validate(rows[1])
    except (OSError, ValueError, RecursionError):
        print("BACKUP_EVIDENCE_BAD_INPUT", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(rows))
    else:
        for row in rows:
            print(row["stage"], row["kind"], row["result"], row["recorded_at"],
                  row["source"]["schema"], ",".join(row["codes"]), sep="\t")
    return 0 if rows[0]["result"] == "pass" else 2


if __name__ == "__main__":
    raise SystemExit(main())
