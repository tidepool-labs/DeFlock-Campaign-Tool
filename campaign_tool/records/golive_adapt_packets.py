"""Derive stage-17 live evidence from a copied public-packets journal (read-only, structural)."""

import argparse
import hashlib
import json
import os
import re
import stat
import sys
from datetime import datetime, timezone

from . import golive_evidence, public_packets, run_stages

CODE = "PACKETS_EVIDENCE_BAD_INPUT"
_NAME = public_packets.JOURNAL  # fixed source name: a copied file's own name or path is never echoed
_STAMP = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")
_EVENTS = {"switch", "packet", "approved", "rejected", "cancelled", "written"}


def _canon(value):  # the journal writer's exact line encoding
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)


def _int(value):
    return type(value) is int


def _utc(stamp):
    if not isinstance(stamp, str) or not _STAMP.fullmatch(stamp):
        raise ValueError("invalid time")
    return datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def _events(raw):
    if not raw or not raw.endswith(b"\n") or len(raw) > public_packets.MAX_JOURNAL:
        raise ValueError("invalid journal")
    events = []
    for seq, line in enumerate(raw.split(b"\n")[:-1]):
        try:
            event = json.loads(line.decode("utf-8"), parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
            canonical = isinstance(event, dict) and _canon(event).encode() == line
        except (ValueError, UnicodeError, RecursionError):
            raise ValueError("invalid journal line") from None
        # Appended lines are canonical, so any reformatted or hand-edited line is refused.
        if (not canonical or event.get("schema") != public_packets.SCHEMA or event.get("seq") != seq
                or not isinstance(event.get("event"), str) or event["event"] not in _EVENTS
                or not _int(event.get("ts"))):
            raise ValueError("invalid journal line")
        try:
            at = datetime.fromtimestamp(event["ts"], timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        except (OverflowError, OSError, ValueError):
            raise ValueError("invalid journal line") from None
        if event.get("at") != at:
            raise ValueError("invalid journal line")
        if event["event"] == "packet":
            packet, ident = event.get("packet"), event.get("id")
            if (not isinstance(packet, dict) or not _int(event.get("revision")) or event["revision"] < 0
                    or event.get("digest") != hashlib.sha256(_canon(packet).encode()).hexdigest()):
                raise ValueError("invalid packet event")
            try:
                if ident != public_packets.packet_id(packet):
                    raise ValueError("invalid packet event")
            except (KeyError, TypeError):
                raise ValueError("invalid packet event") from None
        events.append(event)
    return events


def _written(events):
    try:
        rows = public_packets.fold(events)
    except (KeyError, TypeError, ValueError):
        raise ValueError("invalid journal") from None
    for row in rows.values():
        signed = (row.get("signed_by"), row.get("signature"))
        if (row["status"] == "written" and all(isinstance(x, str) and x for x in signed)
                and isinstance(row.get("file"), str) and row["file"]
                and isinstance(row.get("file_sha256"), str)
                and public_packets.SHA.fullmatch(row["file_sha256"])):
            return True
    return False


def records(raw, observed_at):
    """Return one stage-17 row for journal bytes, or raise ValueError (never echoing journal content).

    Structural only: the journal's own signatures are not re-verified against any trust anchor here, and the
    written site-change file is not opened. A row records that a copied journal says a packet was written.
    """
    if not isinstance(raw, bytes) or not isinstance(observed_at, datetime) or observed_at.utcoffset() is None:
        raise ValueError("invalid input")
    events = _events(raw)
    if max(e["ts"] for e in events) > observed_at.timestamp():
        raise ValueError("future journal event")
    if not _written(events):
        raise ValueError("no written packet")
    row = {
        "schema": "records-golive-evidence-v1", "stage": "17", "kind": "live", "result": "pass",
        "recorded_at": events[-1]["at"], "engine": None, "actor": "operator", "codes": [],
        "source": {"schema": public_packets.SCHEMA, "sha256": hashlib.sha256(raw).hexdigest(), "name": _NAME},
    }
    golive_evidence.validate(row)
    return row


class _Parser(argparse.ArgumentParser):
    def error(self, message):  # argparse would echo the offending argument, possibly a path
        print(CODE, file=sys.stderr)
        raise SystemExit(2)


def main(argv=None):
    parser = _Parser(prog="golive_adapt_packets", description=__doc__, allow_abbrev=False)
    parser.add_argument("journal", help="copied public-packets.jsonl")
    parser.add_argument("--observed-at", required=True, help="UTC time, YYYY-MM-DDTHH:MM:SSZ, not in the future")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    try:
        observed = _utc(args.observed_at)
        if observed > datetime.now(timezone.utc):
            raise ValueError("future observation time")
        directory = os.path.dirname(os.path.abspath(args.journal))
        if os.path.lexists(os.path.join(directory, run_stages.SANDBOX_MARKER)):
            raise ValueError("rehearsal sandbox")
        fd = os.open(args.journal, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
        with os.fdopen(fd, "rb") as source:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise ValueError("journal must be a regular file")
            raw = source.read(public_packets.MAX_JOURNAL + 1)
        row = records(raw, observed)
    except (OSError, ValueError, RecursionError):
        print(CODE, file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps([row]))
    else:
        print(row["stage"], row["kind"], row["result"], row["recorded_at"], row["source"]["schema"], sep="\t")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
