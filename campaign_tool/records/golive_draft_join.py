"""Go-live reply-draft join (DFS1123-2312-13): is this exact (queue id, revision) a real reply draft in ONE sealed export?

``python -m campaign_tool.records.golive_draft_join --export-root DIR --name EXPORT --queue-id ID --revision N
[--max-age-hours H] [--json]`` verifies the named export with ``Pinner.historical_sealed``, which returns the Pin AND the
manifest bytes from the one manifest read that was verified, and prints those bytes' SHA-256 with every result, so a
freshness check and a signoff cite the same manifest (its ``exported_at`` is parsed from those same bytes). The queue and
mail-draft bytes parsed for the join are hashed against that same manifest, so a file swapped only while being read is refused.
A nonzero placement
count is not provenance and a link in the review page does not name a reply: the pair must be a queue row of a reply
kind AND have a mail-draft row in the Drafts mailbox (placed / found_existing) in that same export.

Read-only on the export; it never opens the live project or the mailbox and prints no draft text, address or subject,
only ids, kind, state and digests. A PASS is machine-valid evidence for a reviewer, not a semantic acceptance.
"""
import argparse
import hashlib
import json
import re
import sys
import time
from datetime import datetime, timezone

from . import approval_queue as aq, console_export as ce, console_snapshot as cs, draft_templates, mail_drafts

REPLY_KINDS = ("reply-extension", "appeal-denial", "fee-reply")
IN_MAILBOX = ("placed", "found_existing")
DEFAULT_MAX_AGE_HOURS = 36.0
ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:/-]{0,127}")
STAMP = "%Y-%m-%dT%H:%M:%SZ"
UNVERIFIED = (cs.SnapshotError, ce.ExportError, aq.ApprovalQueueError, mail_drafts.MailDraftError, OSError, ValueError,
              KeyError, TypeError, AttributeError)


class Unverified(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


def _sealed_files(raw):
    """The manifest's ``files`` map (relative path -> sha256) from the exact manifest bytes this run cites."""
    files = aq._strict_load(raw, "export_manifest_invalid").get("files")
    if not (isinstance(files, dict) and all(isinstance(k, str) and isinstance(v, str) for k, v in files.items())):
        raise Unverified("export_manifest_invalid")
    return files


def _rows(rows, id_key):
    ok = isinstance(rows, list) and all(
        isinstance(r, dict) and isinstance(r.get(id_key), str) and type(r.get("revision")) is int for r in rows)
    if not ok:
        raise Unverified("export_rows_invalid")
    return rows


def _exported_at(pin):
    try:
        return datetime.strptime(pin.exported_at, STAMP).replace(tzinfo=timezone.utc).timestamp()
    except (TypeError, ValueError):
        raise Unverified("export_time_invalid") from None


def _word(value, allowed):
    """A controlled-vocabulary value for output; anything else is shown as ``unknown`` (never exported text)."""
    return value if value in allowed else "unknown"


def _decide(queue, drafts, queue_id, revision):
    """(code or None, kind, state) from the exported queue rows and mail-draft rows for one exact pair."""
    same_id = [r for r in queue if r["id"] == queue_id]
    exact = [r for r in same_id if r["revision"] == revision]
    if not same_id:
        return "REPLY_ROW_MISSING", None, None
    if not exact:  # the id exists only at another revision: this one is superseded (or not reached)
        return "REPLY_REVISION_MISMATCH", None, None
    kind = exact[0].get("kind")
    if kind not in REPLY_KINDS:  # first request, follow-up, non-mail decision, anything else
        return "REPLY_NOT_REPLY_KIND", kind, None
    mine = [d for d in drafts if d["queue_id"] == queue_id]
    placed = [d for d in mine if d["revision"] == revision]
    if not placed:
        # a placement of another revision never counts for this one
        return ("REPLY_REVISION_MISMATCH" if mine else "REPLY_NOT_PLACED"), kind, None
    state = placed[0].get("state")
    if state not in IN_MAILBOX:  # failed-only or superseded: nothing is in the Drafts mailbox
        return "REPLY_NOT_PLACED", kind, state
    return None, kind, state


def check(export_root, name, queue_id, revision, max_age_hours=DEFAULT_MAX_AGE_HOURS, now=None):
    """The result dict for one export; ``code`` is None on PASS. Raises Unverified for a bad export."""
    now = time.time() if now is None else now
    try:
        pin, raw = cs.Pinner(export_root).historical_sealed(name)  # the Pin and the bytes it verified: one manifest read
        source = cs.SnapshotSource(pin, expected=_sealed_files(raw))  # parsed bytes must hash to the cited manifest
        queue = _rows(source.queue_rows(), "id")
        drafts = _rows(source.mail_drafts(), "queue_id")
        if cs.Pinner(export_root).historical_sealed(name) != (pin, raw):  # a swap or edit after we read is refused
            raise Unverified("export_changed")
        taken = _exported_at(pin)
    except UNVERIFIED as error:
        code = getattr(error, "code", None) or (str(error) if isinstance(error, ce.ExportError) else "export_unreadable")
        raise Unverified(code) from None
    result = {"export": pin.name, "run_id": pin.run_id, "exported_at": pin.exported_at,
              "manifest_sha256": hashlib.sha256(raw).hexdigest(), "queue_id": queue_id, "revision": revision,
              "kind": None, "state": None, "code": None}
    if taken > now:
        result["code"] = "EXPORT_FUTURE"
    elif now - taken > max_age_hours * 3600:
        result["code"] = "EXPORT_STALE"
    else:
        code, kind, state = _decide(queue, drafts, queue_id, revision)
        result.update(code=code, kind=_word(kind, draft_templates.KINDS), state=_word(state, mail_drafts.STATES))
    return result


def render(result, as_json):
    if as_json:
        return json.dumps(result, sort_keys=True)
    fields = " ".join("%s=%s" % (k, result[k]) for k in ("queue_id", "revision", "kind", "state", "export", "exported_at",
                                                         "manifest_sha256") if result.get(k) is not None)
    return ("PASS " if result["code"] is None else result["code"] + " ") + fields


def _args(argv):
    parser = argparse.ArgumentParser(prog="golive_draft_join", description="Confirm one reply draft by queue id and revision in one pinned export.")
    parser.add_argument("--export-root", required=True)
    parser.add_argument("--name", required=True)
    parser.add_argument("--queue-id", required=True)
    parser.add_argument("--revision", required=True, type=int)
    parser.add_argument("--max-age-hours", type=float, default=DEFAULT_MAX_AGE_HOURS)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    if not ID.fullmatch(args.queue_id) or args.revision < 0 or not args.max_age_hours > 0:
        parser.error("queue id, revision or max age out of range")
    return args


def main(argv=None):
    args = _args(sys.argv[1:] if argv is None else argv)
    try:
        result = check(args.export_root, args.name, args.queue_id, args.revision, args.max_age_hours)
    except Unverified as error:
        print(json.dumps({"code": "EXPORT_UNVERIFIED", "reason": error.code}, sort_keys=True) if args.json
              else "EXPORT_UNVERIFIED reason=" + error.code)
        return 2
    print(render(result, args.json))
    return 0 if result["code"] is None else 1


if __name__ == "__main__":
    sys.exit(main())
