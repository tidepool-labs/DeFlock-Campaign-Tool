"""Derive live evidence from private unit readbacks without touching unit state."""

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from . import container_host as host, golive_evidence, systemd_control as control

_CAPTURE = "records-unit-status-capture-v1"
_OPERATION = "records-systemd-operation-v1"
_MANAGED = "records-systemd-managed-v1"
_HOLD = "records-systemd-hold-v1"
_CLOSED = "records-systemd-not-allocated-v1"
_STAMP = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z")
_FAMILIES = (
    ("3", ("records-arrival.path",)),
    ("18", ("records-health.timer", "records-backup-check.timer", "records-console-check.timer")),
)


def _validate_marker(raw, value):
    """Reuse journal readers on just the supplied bytes, never on a live journal."""
    schema = value["schema"]
    if schema == _HOLD:
        host.require(raw == control.FENCE)
        return

    # Standalone receipts can establish profile-digest syntax, not profile
    # identity or cross-file ownership. They never assert unit state.
    digest = value.get("profile_sha256")
    if schema != _MANAGED:
        host.require(isinstance(digest, str) and host.SHA.fullmatch(digest))
    profile = SimpleNamespace(sha256=digest)
    if schema == control.SCHEMA:
        control._validate(raw, profile)
    elif schema == _CLOSED:
        binding = dict(value, schema=control.SCHEMA)
        binding.pop("status", None)
        control._validate(host.encoded(binding), profile)
        host.require(raw == control._closure(binding))
    else:
        marker = control.OPERATION if schema == _OPERATION else "systemd-managed.json"

        def read(name, limit):
            if name != marker:
                raise FileNotFoundError(name)
            host.require(len(raw) <= limit)
            return raw, None

        journal = SimpleNamespace(directory=SimpleNamespace(read=read), profile=profile)
        if schema == _OPERATION:
            control._operation(journal)
        else:
            control._mode(journal)


def _capture_units(value, now):
    if set(value) != {"schema", "observed_at", "units"}:
        raise ValueError("invalid capture fields")
    observed = value["observed_at"]
    if not isinstance(observed, str) or not _STAMP.fullmatch(observed):
        raise ValueError("invalid observed_at")
    stamp = datetime.strptime(observed, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    if stamp > now:
        raise ValueError("future observed_at")
    if not isinstance(value["units"], list):
        raise ValueError("units must be a list")
    units = {}
    for unit in value["units"]:
        if not isinstance(unit, dict) or set(unit) != {"name", "enabled", "active"}:
            raise ValueError("invalid unit fields")
        name = unit["name"]
        if not isinstance(name, str) or not name or name in units:
            raise ValueError("invalid or duplicate unit name")
        if not isinstance(unit["enabled"], bool) or not isinstance(unit["active"], bool):
            raise ValueError("unit states must be booleans")
        units[name] = unit["enabled"] and unit["active"]
    return units


def _rows(raw, value, name, now):
    units = _capture_units(value, now)
    for stage, required in _FAMILIES:
        if not any(unit in units for unit in required):
            continue
        codes = []
        for unit in required:
            if not units.get(unit):
                state = "NOT_ACTIVE" if unit in units else "MISSING"
                suffix = unit.upper().replace("-", "_").replace(".", "_")
                codes.append(f"UNIT_{state}_{suffix}")
        row = {
            "schema": "records-golive-evidence-v1", "stage": stage, "kind": "live",
            "result": "fail" if codes else "pass", "recorded_at": value["observed_at"],
            "engine": None, "actor": "operator", "codes": codes,
            "source": {"schema": _CAPTURE, "sha256": host.digest(raw), "name": name},
        }
        golive_evidence.validate(row)
        yield row


def records(receipt_paths, now):
    """Return rows in input order, evaluating each capture independently.

    ``now`` is a datetime (a naive datetime is interpreted as UTC).
    A valid journal marker yields no rows; any invalid input raises ValueError.
    """
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    results = []
    for receipt in receipt_paths:
        path = Path(receipt)
        try:
            raw = path.read_bytes()
            # The evidence-file size cap does not apply to a unit inventory.
            value = host.decoded(raw.decode("utf-8"), len(raw))
            schema = value.get("schema") if isinstance(value, dict) else None
            if schema == _CAPTURE:
                results.extend(_rows(raw, value, path.name, now))
            elif schema in (_OPERATION, _MANAGED, control.SCHEMA, _HOLD, _CLOSED):
                _validate_marker(raw, value)
            else:
                raise ValueError("unknown receipt schema")
        except (OSError, ValueError, TypeError, RecursionError, host.Rejected):
            # Input paths and parser values may be private.
            raise ValueError("invalid systemd receipt or unit status capture") from None
    return results


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("receipts", nargs="+", metavar="RECEIPT")
    parser.add_argument("--json", action="store_true", help="print a JSON array instead of one row per line")
    args = parser.parse_args(argv)
    try:
        rows = records(args.receipts, datetime.now(timezone.utc))
    except ValueError as exc:
        print(f"golive_adapt_systemd: {exc}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(rows))
    else:
        for row in rows:
            print(json.dumps(row))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
