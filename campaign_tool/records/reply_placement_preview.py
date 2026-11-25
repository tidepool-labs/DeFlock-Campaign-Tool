"""One-reply Drafts placement preview: what ``mail_drafts.place`` would put in Drafts for ONE exact queue row.

Read-only and offline. It connects nowhere, sends nothing, takes no lock for writing and writes no record: the
approval queue, the placement journal and the ledger are read through their existing read-only readers (SQLite may
still create its own shared-memory file beside the ledger when it opens it read-only). The caller names the
exact queue ID (64 hex characters, never a prefix) and the exact revision it reviewed. The preview then shows the
Message-ID, Drafts folder, threading and message fingerprint the placement would produce for that row, and lists the
other approved, not yet placed rows separately so a reviewer sees that they stay untouched (the bulk
``records mail-drafts place`` command would place them too).

The verdict is ``WOULD_PLACE`` only when every check passes; otherwise ``REFUSED`` with every stable, content-free
refusal code that applies:

- ``queue_id_invalid`` / ``revision_invalid``: not an exact 64-hex ID, or not a non-negative integer revision.
- ``queue_id_unknown``: no queue row has that ID.
- ``not_mail_kind``: the row is a decision or review copy, never a mail draft.
- ``revision_stale`` / ``revision_ahead``: the row has moved on from (or never reached) the reviewed revision.
- ``not_approved``: the current revision is pending or rejected (an edit after approval returns it to pending).
- ``approval_unverified``: an approval is recorded but its signature does not verify (untrusted or no trust anchor).
- ``approval_revision_mismatch``: the stored approval does not name the revision directly before the current one.
- ``body_has_draft_residue``: template text (banner, heading, EDIT ME placeholder, notes, footer) is still in the
  body; ``mail_drafts.place`` would refuse it, so the organizer must edit the draft in the console first.
- ``already_placed``: the placement journal already records this exact ID and revision as placed or found.
- ``target_unresolved``: a reply to an agency message whose stored message cannot be resolved, so the draft would
  have no thread and no recipient; or a thread provenance refusal from ``mail_drafts`` (its own code is listed).
- ``target_shared``: another approved, not yet placed reply answers the same received item, so the target thread
  is ambiguous.

The preview never grants placement: it is a dry-run contract that a separately authorized exact-ID placement and a
subsequent seal must still satisfy. Output carries IDs, digests and codes only, never the letter text.
"""
import argparse
import hashlib
import json
import re
import sys
from datetime import datetime, timezone

from . import approval_queue, draft_residue, draft_templates, mail_drafts

SCHEMA = "records-reply-placement-preview-v1"
WOULD_PLACE, REFUSED = "WOULD_PLACE", "REFUSED"
# Kinds written as a reply to one received agency message: placing them without that thread has no recipient.
THREAD_REQUIRED_KINDS = ("reply-extension", "appeal-denial", "fee-reply", "followup-missing-records")
_HEX64 = re.compile(r"[0-9a-f]{64}")


def _sha(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _is_mail(row):
    return row["kind"] not in draft_templates.NON_MAIL_KINDS


def _approval_codes(row, revision):
    codes = []
    if row["revision"] > revision:
        codes.append("revision_stale")
    elif row["revision"] < revision:
        codes.append("revision_ahead")
    if row["status"] != "approved":
        codes.append("approval_unverified" if row.get("approval_unverified") else "not_approved")
    elif (row.get("approval_evidence") or {}).get("signed_revision") != row["revision"] - 1:
        codes.append("approval_revision_mismatch")
    return codes


def _target(project, row):
    """(thread, reason, codes): the thread mail_drafts would use for this row, read-only."""
    try:
        thread, reason = mail_drafts._thread_for(project, row)
    except mail_drafts.MailDraftError as error:
        return None, error.code, ["target_unresolved", error.code]
    if thread is None and row["kind"] in THREAD_REQUIRED_KINDS:
        return None, reason, ["target_unresolved"]
    return thread, reason, []


def preview(project, queue_id, revision, *, config=None):
    """Content-free preview for one exact (queue_id, revision). ``config`` is ``mail_drafts.load_config``'s dict
    (only ``from_addr`` and ``folder`` are used, nothing connects); without it no Message-ID or fingerprint is shown.
    Raises ``approval_queue.ApprovalQueueError`` / ``mail_drafts.MailDraftError`` only for unreadable stores."""
    out = {"schema": SCHEMA, "queue_id": queue_id, "revision": revision, "verdict": REFUSED, "refusals": []}
    codes = []
    if not (isinstance(queue_id, str) and _HEX64.fullmatch(queue_id)):
        codes.append("queue_id_invalid")
    if type(revision) is not int or revision < 0:
        codes.append("revision_invalid")
    if codes:
        out["refusals"] = codes
        return out
    rows = approval_queue.load_queue(project)
    latest = {(p["queue_id"], p["revision"]): p for p in mail_drafts.placements(project)}

    def unplaced(r):
        return latest.get((r["id"], r["revision"]), {}).get("state") not in mail_drafts.DONE

    bulk = [r for r in rows if r["status"] == "approved" and _is_mail(r) and unplaced(r)]
    row = next((r for r in rows if r["id"] == queue_id), None)
    out["others_untouched"] = sorted(r["id"] for r in bulk if r["id"] != queue_id)
    out["bulk_place_would_place"] = len(bulk)
    if row is None:
        out["refusals"] = ["queue_id_unknown"]
        return out
    out.update(kind=row["kind"], agency_id=row["agency_id"], request_id=row["request_id"], item_id=row["item_id"],
               status=row["status"], current_revision=row["revision"], body_sha256=_sha(row["body"]))
    if not _is_mail(row):
        out["refusals"] = ["not_mail_kind"]
        return out
    codes += _approval_codes(row, revision)
    residue = draft_residue.codes(row["body"])
    out["residue"] = residue
    if residue:
        codes.append("body_has_draft_residue")
    state = latest.get((row["id"], row["revision"]), {}).get("state")
    out["journal_state"] = state
    if state in mail_drafts.DONE:
        codes.append("already_placed")
    out["supersedes_placed_revisions"] = sorted(
        rev for (qid, rev), p in latest.items()
        if qid == row["id"] and rev < row["revision"] and p["state"] in ("placed", "found_existing"))
    thread, reason, target_codes = _target(project, row)
    codes += target_codes
    if any(r["id"] != row["id"] and r["item_id"] == row["item_id"] and r["kind"] in THREAD_REQUIRED_KINDS
           for r in bulk) and row["kind"] in THREAD_REQUIRED_KINDS:
        codes.append("target_shared")
    out.update(threaded=thread is not None, thread_reason=None if thread is not None else reason,
               to_domain=None, folder=None, message_id=None, fingerprint=None)
    if thread is not None and thread.get("reply_to"):
        out["to_domain"] = thread["reply_to"].rsplit("@", 1)[1]
    if config is not None and not target_codes:
        out["folder"] = config["folder"]
        out["message_id"] = mail_drafts.message_id(row["id"], row["revision"], config["from_addr"])
        now = datetime.now(timezone.utc)
        try:
            raw = mail_drafts.build_message(row, thread, config["from_addr"],
                                            now=mail_drafts._stable_date(row, now))
            out["fingerprint"] = mail_drafts.fingerprint(raw)
        except mail_drafts.MailDraftError as error:
            codes.append(error.code)
    out["refusals"] = sorted(set(codes))
    if not out["refusals"]:
        out["verdict"] = WOULD_PLACE
    return out


def main(argv=None):
    parser = argparse.ArgumentParser(prog="python -m campaign_tool.records.reply_placement_preview",
                                     description="Preview one exact reply draft's Drafts placement. "
                                                 "Read-only: connects nowhere, writes nothing, sends nothing.")
    parser.add_argument("--project", required=True)
    parser.add_argument("--queue-id", required=True, help="the exact 64-hex queue ID (no prefixes)")
    parser.add_argument("--revision", required=True, type=int, help="the exact revision that was reviewed")
    parser.add_argument("--config", help="owner-only mailbox config (read for From and Drafts folder only)")
    args = parser.parse_args(argv)
    try:
        config = mail_drafts.load_config(args.config) if args.config else None
        result = preview(args.project, args.queue_id, args.revision, config=config)
    except (mail_drafts.MailDraftError, approval_queue.ApprovalQueueError) as error:
        print(error.code, file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0 if result["verdict"] == WOULD_PLACE else 2


if __name__ == "__main__":
    raise SystemExit(main())
