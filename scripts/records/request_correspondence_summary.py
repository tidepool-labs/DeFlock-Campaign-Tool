#!/usr/bin/env python3
"""Request-level correspondence summary for the attended sitting (read-only).

usage: request_correspondence_summary.py INPUT.json [--format json|text]

INPUT.json (``request-correspondence-input-v1``) is a sanitized export with three lists:

* ``requests``: the ledger requests, each ``{"agency_id", "request_id"}``;
* ``received``: received originals, each ``{"agency_id", "request_id", "original_sha256", "item_id"}``; several parts
  of one original are several rows with the same ``original_sha256`` and count once;
* ``events``: reply-draft events, each naming ``agency_id``, ``request_id``, ``queue_id``, ``revision`` and ``kind``:
  ``draft`` (optional ``original_sha256`` the draft answers), ``approval_applied`` (``from_revision``,
  ``decision_id``) or ``drafts_placement`` (``decision_id``, ``message_id``).

For each ledger request it prints the incoming state (AWAITING_REPLY, RECEIVED, or NEEDS_ATTRIBUTION when an original
is claimed by more than one request) with the original hashes and item IDs, and one outgoing line per reply-draft
queue ID: PENDING_DRAFT, APPROVED or PLACED_IN_DRAFTS with the revision, decision ID and message ID that prove it.

Progress is matched on the exact agency, request and queue ID; a neighbouring request to the same agency never
inherits it. An approval must name the draft revision it applies to and a placement must carry the applied decision
and the same revision, so a later edit returns the queue to PENDING_DRAFT. A draft that answers an original not
received for its own request is SOURCE_UNATTRIBUTED and goes no further. Placement in Drafts is never reported as
sent: this summary has no sent state. Events for a request that is not in the ledger are counted as unmatched.

Exit 0 = summary printed, 2 = refused, usage or unreadable input. Review aid only: it infers no deadline, fulfilment
or legal conclusion, opens no mailbox and writes no file, queue, store or ledger. Standard library only.
"""
import argparse
import json
import os
import re
import stat
import sys

SCHEMA = "request-correspondence-summary-v1"
INPUT_SCHEMA = "request-correspondence-input-v1"
MAX_INPUT = 8 * 1024 * 1024
HEX64 = re.compile(r"[0-9a-f]{64}")
NOTE = ("Review aid for the sitting. Placed in Drafts means placed, not sent. No deadline, fulfilment or legal "
        "conclusion is drawn.")
AWAITING, RECEIVED, NEEDS_ATTRIBUTION = "AWAITING_REPLY", "RECEIVED", "NEEDS_ATTRIBUTION"
PENDING, APPROVED, PLACED, UNATTRIBUTED = "PENDING_DRAFT", "APPROVED", "PLACED_IN_DRAFTS", "SOURCE_UNATTRIBUTED"


class Refused(Exception):
    pass


def _ident(value):
    return isinstance(value, str) and value.strip() != "" and len(value) <= 200


def load(path):
    try:
        st = os.lstat(path)
    except OSError:
        raise Refused("input unreadable") from None
    if not stat.S_ISREG(st.st_mode) or st.st_size > MAX_INPUT:
        raise Refused("input is not a regular file under the size limit")
    try:
        with open(path, "rb") as handle:
            data = json.loads(handle.read(MAX_INPUT + 1))
    except (OSError, ValueError):
        raise Refused("input is not readable JSON") from None
    if not isinstance(data, dict) or data.get("schema") != INPUT_SCHEMA:
        raise Refused("input schema is not %s" % INPUT_SCHEMA)
    for key in ("requests", "received", "events"):
        if not isinstance(data.get(key), list) or not all(isinstance(r, dict) for r in data[key]):
            raise Refused("%s must be a list of objects" % key)
    keys = []
    for r in data["requests"]:
        if not (_ident(r.get("agency_id")) and _ident(r.get("request_id"))):
            raise Refused("a ledger request lacks agency_id or request_id")
        keys.append((r["agency_id"], r["request_id"]))
    if len(set(keys)) != len(keys):
        raise Refused("a ledger request is listed twice")
    for r in data["received"]:
        if not (_ident(r.get("agency_id")) and _ident(r.get("request_id")) and _ident(r.get("item_id"))
                and isinstance(r.get("original_sha256"), str) and HEX64.fullmatch(r["original_sha256"])):
            raise Refused("a received row lacks agency_id, request_id, item_id or a sha256 original")
    return data, keys


def incoming(data, keys):
    """Per request: attributed originals, item IDs and conflicts. An original claimed by two requests is withheld."""
    claims = {}
    for r in data["received"]:
        claims.setdefault(r["original_sha256"], set()).add((r["agency_id"], r["request_id"]))
    out = {key: {"originals": {}, "conflicting": set()} for key in keys}
    unmatched = 0
    for r in data["received"]:
        key = (r["agency_id"], r["request_id"])
        if key not in out:
            unmatched += 1
            continue
        digest = r["original_sha256"]
        if len(claims[digest]) > 1:
            out[key]["conflicting"].add(digest)
        else:
            out[key]["originals"].setdefault(digest, set()).add(r["item_id"])
    return out, unmatched


def outgoing(events, key, attributed):
    """One entry per queue ID of this exact request, advanced only by exact-revision evidence."""
    queues = {}
    for e in events:
        if (e.get("agency_id"), e.get("request_id")) != key or not _ident(e.get("queue_id")):
            continue
        n = e.get("revision")
        if type(n) is not int or n < 1:
            continue
        q = queues.setdefault(e["queue_id"], {"queue_id": e["queue_id"], "state": None, "revision": 0,
                                              "decision_id": None, "message_id": None})
        kind = e.get("kind")
        if q["state"] == UNATTRIBUTED:
            continue
        if kind == "draft" and n == q["revision"] + 1:
            source = e.get("original_sha256")
            if source is not None and source not in attributed:
                q.update(state=UNATTRIBUTED, revision=n, decision_id=None, message_id=None)
            else:
                q.update(state=PENDING, revision=n, decision_id=None, message_id=None)
        elif (kind == "approval_applied" and q["state"] == PENDING and n == q["revision"] + 1
              and type(e.get("from_revision")) is int and e["from_revision"] == q["revision"]
              and _ident(e.get("decision_id"))):
            q.update(state=APPROVED, revision=n, decision_id=e["decision_id"])
        elif (kind == "drafts_placement" and q["state"] == APPROVED and n == q["revision"]
              and e.get("decision_id") == q["decision_id"] and _ident(e.get("message_id"))):
            q.update(state=PLACED, message_id=e["message_id"])
    return [q for _, q in sorted(queues.items()) if q["state"] is not None]


def build(path):
    data, keys = load(path)
    received, unmatched_received = incoming(data, keys)
    rows = []
    for key in keys:
        got = received[key]
        state = NEEDS_ATTRIBUTION if got["conflicting"] else (RECEIVED if got["originals"] else AWAITING)
        rows.append({"agency_id": key[0], "request_id": key[1], "incoming": state,
                     "originals": [{"original_sha256": d, "item_ids": sorted(ids)}
                                   for d, ids in sorted(got["originals"].items())],
                     "conflicting_originals": sorted(got["conflicting"]),
                     "replies": outgoing(data["events"], key, set(got["originals"]))})
    known = set(keys)
    unmatched_events = sum(1 for e in data["events"] if (e.get("agency_id"), e.get("request_id")) not in known)
    return {"schema": SCHEMA, "requests": rows, "unmatched_received_rows": unmatched_received,
            "unmatched_events": unmatched_events, "sent_state": "NOT_REPORTED", "note": NOTE}


def render(summary):
    out = [summary["note"], ""]
    for r in summary["requests"]:
        out.append("%s / %s: %s, %d original(s)" % (r["agency_id"], r["request_id"], r["incoming"], len(r["originals"])))
        for o in r["originals"]:
            out.append("    original %s items %s" % (o["original_sha256"][:12], ", ".join(o["item_ids"])))
        for d in r["conflicting_originals"]:
            out.append("    original %s claimed by another request: withheld" % d[:12])
        if not r["replies"]:
            out.append("    reply: none")
        for q in r["replies"]:
            proof = ["revision %d" % q["revision"]]
            if q["decision_id"]:
                proof.append("decision %s" % q["decision_id"])
            if q["message_id"]:
                proof.append("message %s" % q["message_id"])
            label = "PLACED_IN_DRAFTS (not sent)" if q["state"] == PLACED else q["state"]
            out.append("    reply %s: %s (%s)" % (q["queue_id"], label, ", ".join(proof)))
    out.append("")
    out.append("Unmatched received rows: %d. Unmatched reply events: %d." % (
        summary["unmatched_received_rows"], summary["unmatched_events"]))
    return "\n".join(out) + "\n"


def main(argv=None):
    parser = argparse.ArgumentParser(description="Request-level correspondence summary (review aid, read-only).")
    parser.add_argument("input")
    parser.add_argument("--format", choices=("json", "text"), default="json")
    args = parser.parse_args(argv)
    try:
        summary = build(args.input)
    except Refused as exc:
        print("refused: %s" % exc, file=sys.stderr)
        return 2
    if args.format == "json":
        sys.stdout.write(json.dumps(summary, indent=2, sort_keys=True) + "\n")
    else:
        sys.stdout.write(render(summary))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
