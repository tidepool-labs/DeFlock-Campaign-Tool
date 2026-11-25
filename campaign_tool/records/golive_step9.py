"""M1 attended-session step 9: one current seal must carry the placed reply, its applied approval and the phone packet.

``python -m campaign_tool.records.golive_step9 --export-root DIR --queue-id ID --revision N --decision-id SHA
--journal J/decisions.jsonl --console-head SHA --cli-json FILE --page FILE --capture FILE --denial FILE``

Read-only composition of existing reviewed readers on the export that ``DIR/current`` names, read before and after:

1. golive_draft_join (D1): that (queue id, revision) is a placed reply draft in this export (``code`` None);
2. the same verified manifest bytes give the seal and ``approval_anchor.state == "ok"``;
3. golive_approval_receipt (D5): the journal entry with ``entry_id == decision-id`` is APPLIED on this seal;
4. golive_phone_check_packet (D6): saved signed-in CLI/Go-live captures and the signed-out / unauthorized denial
   receipts bound to this seal and console head agree (same stage IDs and counts).

READY only when all four name the one seal and ``current`` did not move meanwhile; otherwise HOLD with one fixed
reason. Output carries ids, digests, fixed codes and the D6 comparison text only: never draft text, addresses,
journal values or capture bytes. It does not run the engine, contact a mailbox or host, sign, or apply anything,
and READY is machine-valid evidence for the attended reviewer, not the phone check itself or semantic acceptance.
"""
import argparse
import hashlib
import json
import os
import re
import stat
import sys

from . import approval_queue as aq, console_export as ce, console_snapshot as cs
from . import golive_approval_receipt, golive_draft_join, golive_parity, golive_phone_check_packet

MAX_JOURNAL = 4 * 1024 * 1024
MAX_RECEIPT = 64 * 1024
_HEX40 = re.compile(r"[0-9a-f]{40}")
_HEX64 = re.compile(r"[0-9a-f]{64}")
REASONS = ("CURRENT_UNREADABLE", "CURRENT_MOVED", "EXPORT_UNVERIFIED", "D1_NOT_PASS", "ANCHOR_UNAVAILABLE",
           "DECISION_NOT_FOUND", "DECISION_AMBIGUOUS", "D5_NOT_APPLIED", "INPUT_UNREADABLE", "PACKET_HOLD")


class Hold(Exception):
    def __init__(self, reason, **detail):
        super().__init__(reason)
        self.reason, self.detail = reason, detail


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def _read(path, limit):
    """Bytes of one regular file opened without following a final symlink, refused above ``limit``."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)  # a FIFO must not block before fstat rejects it
    try:
        info = os.fstat(fd)
        if not (stat.S_ISREG(info.st_mode) and info.st_size <= limit):
            raise ValueError("input_unsafe")
        with os.fdopen(os.dup(fd), "rb") as handle:
            raw = handle.read(limit + 1)
    finally:
        os.close(fd)
    if len(raw) > limit:
        raise ValueError("input_too_large")
    return raw


def current_name(export_root):
    """The export name ``DIR/current`` links to (exports/NAME), or Hold."""
    try:
        match = cs.LINK.fullmatch(os.readlink(os.path.join(export_root, "current")))
    except (OSError, TypeError, ValueError):
        match = None
    if not match:
        raise Hold("CURRENT_UNREADABLE")
    return match[1]


def find_decision(journal_bytes, decision_id):
    """The one journal entry whose entry_id is ``decision_id``; a missing or repeated id holds."""
    found = []
    for line in journal_bytes.splitlines():
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except (ValueError, RecursionError):
            continue  # a torn or over-nested line cannot be the decision; D5 still checks the entry it gets
        if isinstance(entry, dict) and entry.get("entry_id") == decision_id:
            found.append(entry)
    if not found:
        raise Hold("DECISION_NOT_FOUND")
    if len(found) != 1:
        raise Hold("DECISION_AMBIGUOUS")
    return found[0]


def evaluate(*, export_root, queue_id, revision, decision_id, journal_bytes, console_head, cli_bytes, page_bytes,
             capture, denial, now=None):
    """Return the step-9 verdict dict. Never raises for unusable inputs: every failure is a HOLD."""
    seal = None
    try:
        name = current_name(export_root)
        try:
            d1 = golive_draft_join.check(export_root, name, queue_id, revision, now=now)
            pin, manifest = cs.Pinner(export_root).historical_sealed(name)
        except (golive_draft_join.Unverified, cs.SnapshotError, ce.ExportError, aq.ApprovalQueueError, OSError,
                ValueError):
            raise Hold("EXPORT_UNVERIFIED") from None
        seal = _sha(manifest)
        if d1.get("code") is not None or d1.get("manifest_sha256") != seal:
            raise Hold("D1_NOT_PASS", d1_code=golive_draft_join.safe_reason(d1.get("code") or "seal_changed"))
        try:
            anchor = (aq._strict_load(manifest, "export_manifest_invalid").get("approval_anchor") or {}).get("state")
        except (aq.ApprovalQueueError, AttributeError):
            anchor = None
        if anchor != "ok":
            raise Hold("ANCHOR_UNAVAILABLE")
        decision = find_decision(journal_bytes, decision_id)
        try:
            queue_model = _read(os.path.join(pin.path, ce.MODEL_DIR, "queue.json"), cs.MAX_MODEL)
        except (OSError, ValueError):
            raise Hold("EXPORT_UNVERIFIED") from None
        d5 = golive_approval_receipt.join(draft=d1, current_manifest=manifest, current_seal=seal,
                                          queue_model=queue_model, decision=decision, decision_id=decision_id)
        if d5.get("state") != "APPLIED" or d5.get("current_seal") != seal:
            raise Hold("D5_NOT_APPLIED")
        expected = {"origin": golive_phone_check_packet.ORIGIN, "seal_sha256": seal, "console_head": console_head}
        packet = golive_phone_check_packet.build(expected=expected, page_bytes=page_bytes, cli_bytes=cli_bytes,
                                                 capture=capture, denial=denial)
        if packet.get("status") != "READY":
            raise Hold("PACKET_HOLD", packet_reason=packet.get("reason"))
        if current_name(export_root) != name:
            raise Hold("CURRENT_MOVED")
    except Hold as hold:
        return {"verdict": "HOLD", "reason": hold.reason, "seal_sha256": seal, **hold.detail}
    except RecursionError:
        # Over-nested caller-supplied JSON (capture/denial receipts) reaching a reader: a HOLD, never a traceback.
        return {"verdict": "HOLD", "reason": "INPUT_UNREADABLE", "seal_sha256": seal}
    return {"verdict": "READY", "seal_sha256": seal, "export": name, "run_id": d1["run_id"],
            "queue_id": d1["queue_id"], "revision": d1["revision"], "kind": d1["kind"], "state": d1["state"],
            "decision_id": decision_id, "signed_revision": d5["signed_revision"], "console_head": console_head,
            "phone_check": {k: packet[k] for k in ("title", "url", "compare", "recommendation", "live_stage_ids",
                                                   "pipeline_live", "pipeline_total")}}


def _args(argv):
    parser = argparse.ArgumentParser(prog="golive_step9",
                                     description="M1 step 9: placed reply, applied approval and phone packet on one seal.")
    for flag in ("--export-root", "--queue-id", "--decision-id", "--journal", "--console-head", "--cli-json", "--page",
                 "--capture", "--denial"):
        parser.add_argument(flag, required=True)
    parser.add_argument("--revision", required=True, type=int)
    args = parser.parse_args(argv)
    if not (golive_draft_join.ID.fullmatch(args.queue_id) and args.revision >= 1 and _HEX64.fullmatch(args.decision_id)
            and _HEX40.fullmatch(args.console_head)):
        parser.error("queue id, revision, decision id or console head out of range")
    return args


def main(argv=None):
    args = _args(sys.argv[1:] if argv is None else argv)
    try:
        journal = _read(args.journal, MAX_JOURNAL)
        cli = _read(args.cli_json, golive_parity.MAX_BYTES)
        page = _read(args.page, golive_parity.MAX_BYTES)
        capture = json.loads(_read(args.capture, MAX_RECEIPT))
        denial = json.loads(_read(args.denial, MAX_RECEIPT))
    except (OSError, ValueError, RecursionError):
        result = {"verdict": "HOLD", "reason": "INPUT_UNREADABLE", "seal_sha256": None}
    else:
        result = evaluate(export_root=args.export_root, queue_id=args.queue_id, revision=args.revision,
                          decision_id=args.decision_id, journal_bytes=journal, console_head=args.console_head,
                          cli_bytes=cli, page_bytes=page, capture=capture, denial=denial)
    print(json.dumps(result, indent=1, sort_keys=True))
    return 0 if result["verdict"] == "READY" else 1


if __name__ == "__main__":
    sys.exit(main())
