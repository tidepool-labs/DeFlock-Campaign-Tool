"""Derive stage 16 live evidence from a copied public-packets journal; machine-valid, not acceptance."""

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import stat
import sys

from . import golive_evidence, public_packets

_CODE = "FINDINGS_EVIDENCE_BAD_INPUT"
_MARKER = ".t12-sandbox-v1"  # run_stages.SANDBOX_MARKER: a rehearsal sandbox is never live evidence
_OPEN = {"proposed", "approved", "written"}
_DEAD = {"expired", "rejected", "cancelled"}


def _is_int(value):
    return isinstance(value, int) and not isinstance(value, bool)


def _read(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError("expected a regular file")
        raw = stream.read(public_packets.MAX_JOURNAL + 1)
    if len(raw) > public_packets.MAX_JOURNAL or not raw.endswith(b"\n"):
        raise ValueError("journal is oversized, empty or unterminated")
    return raw


def _reject_constant(_):
    raise ValueError("non-finite number")


def _text(value, limit):
    return isinstance(value, str) and 0 < len(value) <= limit


def _event_ok(event, index, state):
    """Check one journal line against what public_packets writes; ``state`` maps id -> [revision, status]."""
    kind, ident, rev = event["event"], event.get("id"), event.get("revision")
    if event.get("seq") != index or not isinstance(event.get("at"), str):
        return False
    if kind == "switch":
        return (event.get("state") in ("on", "off") and _text(event.get("principal"), 256)
                and _is_int(event.get("round")) and event["round"] >= 0
                and (event["state"] == "off" or _text(event.get("signature"), 16384)))
    if kind not in ("packet", "approved", "rejected", "cancelled", "written"):
        return False
    if not (isinstance(ident, str) and public_packets.PACKET_ID.fullmatch(ident) and _is_int(rev) and rev >= 0):
        return False
    if kind == "packet":
        if not (isinstance(event.get("packet"), dict) and isinstance(event.get("digest"), str)):
            return False
        if rev != (state[ident][0] + 1 if ident in state else 0):  # the writer numbers revisions in order
            return False
        state[ident] = [rev, "proposed"]
        return True
    if ident not in state or state[ident][0] != rev:  # a transition must name the current revision of a known packet
        return False
    status = state[ident][1]
    if kind == "approved":
        ok = (status == "proposed" and _text(event.get("principal"), 256) and _text(event.get("signature"), 16384)
              and _is_int(event["ts"]))
        status = "approved"
    elif kind == "written":
        ok = (status == "approved" and _text(event.get("file"), 512) and isinstance(event.get("file_sha256"), str)
              and len(event["file_sha256"]) == 64)
        status = "written"
    else:
        reason = event.get("reason")
        ok = (status in ("proposed", "approved") and isinstance(event.get("actor"), str)
              and public_packets.ACTOR.fullmatch(event["actor"]) is not None
              and (reason is None or _text(reason, 500)))
        status = kind
    state[ident][1] = status
    return bool(ok)


def _events(raw, now_ts):
    events = [json.loads(line.decode("utf-8"), parse_constant=_reject_constant) for line in raw.split(b"\n")[:-1]]
    state = {}
    for index, event in enumerate(events):
        if not (isinstance(event, dict) and event.get("schema") == public_packets.SCHEMA
                and isinstance(event.get("event"), str) and _is_int(event.get("ts"))):
            raise ValueError("invalid journal event")
        if event["ts"] > now_ts:  # a copy taken at observed_at cannot hold a later event
            raise ValueError("journal event after observed_at")
        if not _event_ok(event, index, state):  # fold() skips what it cannot place; a copy with such lines is not live proof
            raise ValueError("invalid journal event")
    return events


def _status(row, now_ts):
    if row["status"] in ("proposed", "approved") and now_ts - row["proposed_ts"] > public_packets.EXPIRY_SECONDS:
        return "expired"
    return row["status"]


def _qualifying(rows, now_ts):
    """Only finding-kind packets count; one dead finding packet refuses (fail closed)."""
    states = []
    for row in rows.values():
        if row["packet"].get("kind") != "finding":
            continue
        # Journal lines are untrusted: re-run the proposal checks and bind the id to the content.
        if public_packets.packet_id(public_packets.clean(row["packet"])) != row["id"]:
            raise ValueError("packet id does not match its content")
        states.append(_status(row, now_ts))
    if not any(state in _OPEN for state in states) or any(state in _DEAD for state in states):
        raise ValueError("no qualifying finding packet")


def records(journal, observed_at):
    """Read a copied journal and its neighbours; write nothing. Return one stage 16 row or raise ValueError."""
    try:
        path = Path(journal)
        instant = datetime.fromisoformat(observed_at) if isinstance(observed_at, str) else observed_at
        if not isinstance(instant, datetime) or instant.utcoffset() is None:
            raise ValueError("timestamp must identify an instant")
        instant = instant.astimezone(timezone.utc).replace(microsecond=0)
        if os.path.lexists(path.parent / _MARKER):
            raise ValueError("rehearsal sandbox marker")
        raw = _read(path)
        now_ts = int(instant.timestamp())
        _qualifying(public_packets.fold(_events(raw, now_ts)), now_ts)
        row = {
            "schema": "records-golive-evidence-v1", "stage": "16", "kind": "live", "result": "pass",
            "recorded_at": instant.strftime("%Y-%m-%dT%H:%M:%SZ"), "engine": None, "actor": "operator",
            "codes": [],
            "source": {"schema": public_packets.SCHEMA, "sha256": hashlib.sha256(raw).hexdigest(), "name": path.name},
        }
        golive_evidence.validate(row)
    except (OSError, ValueError, TypeError, KeyError, RecursionError, public_packets.PacketError):
        raise ValueError(_CODE) from None  # paths and journal text may be private
    return [row]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("journal", help="copied public-packets.jsonl")
    parser.add_argument("--observed-at", help="UTC instant the copy was taken (default: now)")
    parser.add_argument("--json", action="store_true", help="print a JSON array instead of one row per line")
    args = parser.parse_args(argv)
    now = datetime.now(timezone.utc)
    try:
        observed = args.observed_at or now
        if isinstance(observed, str):
            observed = datetime.fromisoformat(observed)
        if observed.utcoffset() is None or observed > now:
            raise ValueError(_CODE)
        rows = records(args.journal, observed)
    except ValueError:
        print(_CODE, file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(rows))
    else:
        for row in rows:
            print(json.dumps(row))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
