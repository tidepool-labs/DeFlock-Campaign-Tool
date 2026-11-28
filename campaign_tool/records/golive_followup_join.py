"""Go-live no-response follow-up join (DFS1124-0921-12, stage 14): is this exact (queue id, revision) a real follow-up draft in ONE sealed export?

``python -m campaign_tool.records.golive_followup_join --export-root DIR --name EXPORT --queue-id ID --revision N
[--max-age-hours H] [--json]`` verifies the named export with ``Pinner.historical``, reads the manifest bytes once and
prints their SHA-256 with every result, so a freshness check and a signoff cite the same manifest. The queue and
mail-draft bytes parsed for the join are hashed against that same manifest, so a file swapped only while being read is
refused. A nonzero placement count is not provenance and a reply or first request is not a follow-up: the pair must be a
queue row of a follow-up kind (reminder, formal notice or engine follow-up) AND have a mail-draft row in the Drafts
mailbox (placed / found_existing) in that same export.

Read-only on the export; it never opens the live project or the mailbox and prints no draft text, address or subject,
only ids, kind, state and digests. A PASS is machine-valid evidence for a reviewer, not a semantic acceptance.
Self-contained: it imports no other go-live join.

``--evidence-row`` prints, instead of the result, a JSON array with ONE stage-14 ``records-golive-evidence-v1`` live
row whose source is the cited manifest (``EXPORT.json`` and the same SHA-256): a pass only when the join passed, else a
fail with the join's code. Save it to a file and record it with ``golive_record live``. An unverified export prints no
row (exit 2).
"""
import argparse
import hashlib
import json
import math
import os
import re
import sys
import time
from datetime import datetime, timezone

from . import approval_queue as aq, console_export as ce, console_snapshot as cs, draft_templates, followup_clock, golive_record, mail_drafts

STAGE = "14"
ENGINE_FOLLOWUP = "followup-playbook"
FOLLOWUP_KINDS = followup_clock.REMINDERS + (followup_clock.NOTICE, ENGINE_FOLLOWUP)
IN_MAILBOX = ("placed", "found_existing")
DEFAULT_MAX_AGE_HOURS = 36.0
ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:/-]{0,127}")
STAMP = "%Y-%m-%dT%H:%M:%SZ"
KNOWN_KINDS = draft_templates.KINDS + (draft_templates.MESSAGE_LINK,)
# The only reasons EXPORT_UNVERIFIED may print; an exported model's own error text is never one of them.
REASONS = frozenset((
    "export_name_invalid", "export_root_unsafe", "export_missing", "export_unsafe", "export_manifest_invalid",
    "export_hash_mismatch", "export_not_regular", "export_oversize", "export_changed", "export_rows_invalid",
    "export_time_invalid", "export_unavailable", "current_invalid", "console_model_missing", "console_model_invalid",
    "console_model_mismatch", "console_model_unavailable", "export_unreadable"))
UNVERIFIED = (cs.SnapshotError, ce.ExportError, aq.ApprovalQueueError, mail_drafts.MailDraftError, OSError, ValueError,
              KeyError, TypeError, AttributeError)


class Unverified(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


def _reason(error):
    code = getattr(error, "code", None) or (str(error) if isinstance(error, ce.ExportError) else None)
    return code if code in REASONS else "export_unreadable"


class Bound(cs.SnapshotSource):
    """SnapshotSource whose every model file must hash to the manifest entry the run cites, on the bytes it parses."""

    def __init__(self, pin, files):
        super().__init__(pin)
        self.files = files

    def _load(self, name, schema=ce.MODEL_SCHEMA):
        root = model = None
        try:
            root = aq._open_chain(self.pin.path)
            model = ce._open_chain(root, [ce.MODEL_DIR])
            raw = None if model is None else cs._read_file(model, name, cs.MAX_MODEL)
            if raw is None:
                return "console_model_missing"
            if self.files.get(ce.MODEL_DIR + "/" + name) != hashlib.sha256(raw).hexdigest():
                return "export_hash_mismatch"
            doc = aq._strict_load(raw, "console_model_invalid")
        except (aq.ApprovalQueueError, ce.ExportError, cs.SnapshotError, OSError):
            return "console_model_invalid"
        finally:
            for fd in (root, model):
                if fd is not None:
                    os.close(fd)
        if type(doc) is not dict or doc.get("schema") != schema or doc.get("state") not in ("ok", "unavailable"):
            return "console_model_invalid"
        if doc.get("source_project_id") != self.pin.source_project_id:
            return "console_model_mismatch"
        return "console_model_unavailable" if doc["state"] == "unavailable" else doc


def _manifest(pin):
    """(raw bytes, manifest dict) of the sealed manifest, read through the pin's no-symlink, capped reader."""
    fd = aq._open_chain(pin.path)
    try:
        raw = cs._read_file(fd, ce.MANIFEST, cs.MAX_MANIFEST)
    finally:
        os.close(fd)
    if raw is None:
        raise Unverified("export_manifest_invalid")
    manifest = aq._strict_load(raw, "export_manifest_invalid")
    files = manifest.get("files")
    if not (isinstance(files, dict) and all(isinstance(k, str) and isinstance(v, str) for k, v in files.items())):
        raise Unverified("export_manifest_invalid")
    return raw, manifest


def _rows(rows, id_key):
    ok = isinstance(rows, list) and all(
        isinstance(r, dict) and isinstance(r.get(id_key), str) and type(r.get("revision")) is int for r in rows)
    if not ok:
        raise Unverified("export_rows_invalid")
    return rows


def _taken(stamp):
    try:
        parsed = datetime.strptime(stamp, STAMP).replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        raise Unverified("export_time_invalid") from None
    if parsed.strftime(STAMP) != stamp:
        raise Unverified("export_time_invalid")
    return parsed.timestamp()


def _word(value, allowed):
    """A controlled-vocabulary value for output; anything else is shown as ``unknown`` (never exported text)."""
    return value if value in allowed else "unknown"


def _decide(queue, drafts, queue_id, revision):
    """(code or None, kind, state) from the exported queue rows and mail-draft rows for one exact pair."""
    same_id = [r for r in queue if r["id"] == queue_id]
    exact = [r for r in same_id if r["revision"] == revision]
    if not same_id:
        return "FOLLOWUP_ROW_MISSING", None, None
    if not exact:  # the id exists only at another revision: this one is superseded (or not reached)
        return "FOLLOWUP_REVISION_MISMATCH", None, None
    kind = exact[0].get("kind")
    if kind not in FOLLOWUP_KINDS:  # first request, reply, non-mail decision, anything else
        return "FOLLOWUP_NOT_FOLLOWUP_KIND", kind, None
    mine = [d for d in drafts if d["queue_id"] == queue_id]
    placed = [d for d in mine if d["revision"] == revision]
    if not placed:  # a placement of another revision never counts for this one
        return ("FOLLOWUP_REVISION_MISMATCH" if mine else "FOLLOWUP_NOT_PLACED"), kind, None
    state = placed[0].get("state")
    if state not in IN_MAILBOX:  # failed-only or superseded: nothing is in the Drafts mailbox
        return "FOLLOWUP_NOT_PLACED", kind, state
    return None, kind, state


def check(export_root, name, queue_id, revision, max_age_hours=DEFAULT_MAX_AGE_HOURS, now=None):
    """The result dict for one export; ``code`` is None on PASS. Raises Unverified for a bad export."""
    now = time.time() if now is None else now
    try:
        pin = cs.Pinner(export_root).historical(name)
        raw, manifest = _manifest(pin)
        source = Bound(pin, manifest["files"])  # parsed bytes must hash to the cited manifest
        queue, drafts = _rows(source.queue_rows(), "id"), _rows(source.mail_drafts(), "queue_id")
        if len({r["id"] for r in queue}) != len(queue) or len({(d["queue_id"], d["revision"]) for d in drafts}) != len(drafts):
            raise Unverified("export_rows_invalid")
        taken = _taken(manifest.get("exported_at"))
        again = cs.Pinner(export_root).historical(name)  # a swap or edit after we read is refused, not averaged
        if again != pin or _manifest(again)[0] != raw or manifest["exported_at"] != pin.exported_at:
            raise Unverified("export_changed")
    except Unverified:
        raise
    except UNVERIFIED as error:
        raise Unverified(_reason(error)) from None
    result = {"export": pin.name, "run_id": pin.run_id, "exported_at": pin.exported_at,
              "manifest_sha256": hashlib.sha256(raw).hexdigest(), "queue_id": queue_id, "revision": revision,
              "kind": None, "state": None, "code": None}
    if taken > now:
        result["code"] = "EXPORT_FUTURE"
    elif now - taken > max_age_hours * 3600:
        result["code"] = "EXPORT_STALE"
    else:
        code, kind, state = _decide(queue, drafts, queue_id, revision)
        result.update(code=code, kind=_word(kind, KNOWN_KINDS) if kind else None,
                      state=_word(state, mail_drafts.STATES) if state else None)
    return result


def render(result, as_json):
    if as_json:
        return json.dumps(result, sort_keys=True)
    keys = ("queue_id", "revision", "kind", "state", "export", "exported_at", "manifest_sha256")
    fields = " ".join("%s=%s" % (k, result[k]) for k in keys if result.get(k) is not None)
    return ("PASS " if result["code"] is None else result["code"] + " ") + fields


def evidence_row(result, now=None):
    """The one stage-14 live row for this result, bound to the manifest digest the result cites."""
    now = datetime.now(timezone.utc) if now is None else now
    return golive_record.live_row(STAGE, result["code"], ce.SCHEMA, result["manifest_sha256"], ce.MANIFEST, now)


def _args(argv):
    parser = argparse.ArgumentParser(prog="golive_followup_join",
                                     description="Confirm one no-response follow-up draft by queue id and revision in one pinned export.")
    parser.add_argument("--export-root", required=True)
    parser.add_argument("--name", required=True)
    parser.add_argument("--queue-id", required=True)
    parser.add_argument("--revision", required=True, type=int)
    parser.add_argument("--max-age-hours", type=float, default=DEFAULT_MAX_AGE_HOURS)
    out = parser.add_mutually_exclusive_group()
    out.add_argument("--json", action="store_true")
    out.add_argument("--evidence-row", action="store_true", help="print one stage-14 live evidence row (JSON array)")
    args = parser.parse_args(argv)
    if not ID.fullmatch(args.queue_id) or args.revision < 0 or not (math.isfinite(args.max_age_hours) and args.max_age_hours > 0):
        parser.error("queue id, revision or max age out of range")
    if args.evidence_row and args.max_age_hours > DEFAULT_MAX_AGE_HOURS:
        # Live evidence keeps the policy age limit; a wider limit stays diagnostic output only.
        parser.error(f"--evidence-row needs --max-age-hours of at most {DEFAULT_MAX_AGE_HOURS:g}")
    return args


def main(argv=None):
    args = _args(sys.argv[1:] if argv is None else argv)
    try:
        result = check(args.export_root, args.name, args.queue_id, args.revision, args.max_age_hours)
    except Unverified as error:
        print(json.dumps({"code": "EXPORT_UNVERIFIED", "reason": error.code}, sort_keys=True) if args.json
              else "EXPORT_UNVERIFIED reason=" + error.code)
        return 2
    print(json.dumps([evidence_row(result)], sort_keys=True) if args.evidence_row else render(result, args.json))
    return 0 if result["code"] is None else 1


if __name__ == "__main__":
    sys.exit(main())
