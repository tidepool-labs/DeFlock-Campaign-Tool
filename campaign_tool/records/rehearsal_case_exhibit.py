"""Stage case exhibit: open one synthetic case and follow a stage 10 difference to its sources and the stage 12 timeline.

A reader over three files the stage 10 and stage 12 rehearsals already write (``comparison.json``, ``timeline.json``,
``received-items.json``). Given an agency and a comparison field it shows, for that one agency only: the cited
difference, the policy source locator it rests on, the received item's own page or message-part locator and original
hash, and the timeline events that reference that item. It joins on exact identifiers (agency id, request id, item
id) and never guesses. If any link is missing or disagrees (another agency, a removed locator, no event for the item)
the case is WITHHELD: the exhibit then names fixed reason codes and carries none of the comparison content.

Review only: a difference is a documentary discrepancy for a human reviewer, never a legal conclusion. The reader
opens no source, makes no network call, writes nothing but ``exhibit.json`` and ``EXHIBIT.md`` in a new output
directory, and changes no ledger, queue or draft. Standard library only, so a copied run-outputs folder is enough.
"""
import argparse
import json
import os
import re
import stat
import sys
from pathlib import Path

SCHEMA = "rehearsal-case-exhibit-v1"
OPEN, WITHHELD = "OPEN_FOR_REVIEW", "WITHHELD"
COMPARISON_SCHEMA, TIMELINE_SCHEMA = "records-agency-comparison-v1", "records-timeline-v1"
NOTE = ("Review-only exhibit of synthetic rehearsal output. A difference is a documentary discrepancy that needs a "
        "human reviewer, not a violation and not a legal conclusion. Nothing was sent, opened or changed.")
MAX_INPUT = 8 * 1024 * 1024
_SHA = re.compile(r"\Asha256:[0-9a-f]{64}\Z")
_HEX = re.compile(r"\A[0-9a-f]{64}\Z")
_QUOTE_LIMIT = 240
_PAGE = re.compile(r"\Apage [1-9][0-9]{0,5}\Z")
REASONS = ("INPUT_NOT_RECOGNISED", "NO_REQUEST_FOR_AGENCY", "NO_COMPARISON_ROW", "AMBIGUOUS_COMPARISON_ROW",
           "NOT_A_DIFFERENCE", "POLICY_LOCATOR_MISSING", "RECEIVED_ITEM_MISSING", "RECEIVED_ITEM_AGENCY_MISMATCH",
           "RECEIVED_ITEM_ID_INVALID", "RECEIVED_LOCATOR_MISSING", "NO_TIMELINE_EVENT_FOR_ITEM")


def _is_text(value):
    return isinstance(value, str) and bool(value.strip())


def _policy_evidence(row):
    """Evidence entries that carry a policy page locator (``page N``) and a sha256 source id."""
    found = []
    for entry in row.get("evidence") or []:
        if isinstance(entry, dict) and isinstance(entry.get("locator"), str) and _PAGE.match(entry["locator"].strip().lower()) \
                and isinstance(entry.get("source_id"), str) and _SHA.match(entry["source_id"]):
            found.append({"locator": entry["locator"].strip().lower(), "source_id": entry["source_id"]})
    return found


def _received_locator(item):
    """The item's own citation: a page or a message part, an exact quote, an original hash, no unavailable flag."""
    basis = item.get("kind_basis")
    basis = basis if isinstance(basis, dict) else {}
    page, part = basis.get("page"), basis.get("part")
    kinds = []
    if isinstance(page, int) and not isinstance(page, bool) and page >= 1:
        kinds.append(("page", page))
    if isinstance(part, int) and not isinstance(part, bool) and part >= 0:
        kinds.append(("message part", part))
    if len(kinds) != 1 or not _is_text(basis.get("quote")) or not _HEX.match(str(item.get("original_sha256") or "")):
        return None
    if "page_locator_unavailable" in (item.get("diagnostics") or []) and kinds[0][0] == "page":
        return None
    label, number = kinds[0]
    return {"locator": "%s %d" % (label, number), "quote": basis["quote"][:_QUOTE_LIMIT],
            "original_sha256": item["original_sha256"], "item_id": item.get("item_id"), "kind": item.get("kind")}


def _recognised(comparison, timeline, received):
    ok = isinstance(comparison, dict) and comparison.get("schema") == COMPARISON_SCHEMA
    ok = ok and isinstance(timeline, dict) and timeline.get("schema") == TIMELINE_SCHEMA
    ok = ok and isinstance(received, dict) and isinstance(received.get("items"), list)
    if not ok:
        return False
    if not isinstance(comparison.get("rows"), list) or not isinstance(timeline.get("requests"), list):
        return False
    return all(document.get("legal_conclusion", False) is False and document.get("review_required") is True
               for document in (comparison, timeline, received))


def _withheld(agency_id, field, reasons):
    return {"schema": SCHEMA, "status": WITHHELD, "legal_conclusion": False, "note": NOTE, "agency_id": agency_id,
            "field": field, "reasons": sorted(set(reasons), key=REASONS.index)}


def open_case(comparison, timeline, received, agency_id, field):
    """Return the exhibit dict: OPEN_FOR_REVIEW with the followed chain, or WITHHELD with reason codes only."""
    if not _is_text(agency_id) or not _is_text(field) or not _recognised(comparison, timeline, received):
        return _withheld(agency_id, field, ["INPUT_NOT_RECOGNISED"])
    reasons = []
    rows = [r for r in comparison["rows"] if isinstance(r, dict) and r.get("agency_id") == agency_id
            and r.get("field") == field]
    requests = [r for r in timeline["requests"] if isinstance(r, dict) and r.get("agency_key") == agency_id]
    row = rows[0] if len(rows) == 1 else None
    request = requests[0] if len(requests) == 1 else None
    if not requests:
        reasons.append("NO_REQUEST_FOR_AGENCY")
    if len(requests) > 1:
        reasons.append("INPUT_NOT_RECOGNISED")
    if not rows:
        reasons.append("NO_COMPARISON_ROW")
    if len(rows) > 1:
        reasons.append("AMBIGUOUS_COMPARISON_ROW")
    evidence = []
    if row is not None:
        if row.get("status") != "gap":
            reasons.append("NOT_A_DIFFERENCE")
        evidence = _policy_evidence(row)
        if not evidence:
            reasons.append("POLICY_LOCATOR_MISSING")
    located, events = [], []
    if request is not None:
        request_id = request.get("request_id")
        items = [i for i in received["items"] if isinstance(i, dict) and request_id and i.get("request_id") == request_id]
        if not items:
            reasons.append("RECEIVED_ITEM_MISSING")
        if any(i.get("agency_id") != agency_id for i in items):
            reasons.append("RECEIVED_ITEM_AGENCY_MISMATCH")
        elif items:
            ids = [i.get("item_id") for i in items]
            if not all(_is_text(x) for x in ids) or len(set(ids)) != len(ids):
                reasons.append("RECEIVED_ITEM_ID_INVALID")
            else:
                located = [loc for loc in map(_received_locator, items) if loc is not None]
                if not located:
                    reasons.append("RECEIVED_LOCATOR_MISSING")
                else:
                    wanted = {loc["item_id"] for loc in located}
                    for event in request.get("events") or []:
                        source = event.get("source") if isinstance(event, dict) else None
                        item_id = source.get("item_id") if isinstance(source, dict) else None
                        if isinstance(item_id, str) and item_id in wanted:
                            events.append({"date": event.get("date"), "type": event.get("type"), "item_id": item_id})
                    if wanted - {e["item_id"] for e in events}:
                        # a located item no timeline event cites is an incomplete chain: nothing is exposed
                        reasons.append("NO_TIMELINE_EVENT_FOR_ITEM")
    if reasons or row is None or request is None:
        return _withheld(agency_id, field, reasons or ["INPUT_NOT_RECOGNISED"])
    citation = row.get("citation") if isinstance(row.get("citation"), dict) else {}
    return {"schema": SCHEMA, "status": OPEN, "legal_conclusion": False, "note": NOTE, "agency_id": agency_id,
            "field": field, "reasons": [],
            "stage_10_difference": {"status": row["status"], "reason": row.get("reason"),
                                    "citation": citation.get("label"), "expected": row.get("expected"),
                                    "observed": row.get("observed"), "policy_locators": evidence,
                                    "comparison_run_id": comparison.get("run_id")},
            "stage_12_case": {"request_id": request["request_id"], "stage": request.get("stage"),
                              "issues": list(request.get("issues") or []), "timeline_run_id": timeline.get("run_id"),
                              "received_locators": located, "timeline_events": events}}


def _line(value):
    """One printable line for a cited value, so a quoted record cannot start new markdown lines."""
    text = value if isinstance(value, str) else json.dumps(value, sort_keys=True, ensure_ascii=True)
    return " ".join(text.split())


def render(exhibit):
    out = ["# Stage case exhibit (review-only)", "", exhibit["note"], "",
           "Agency: %s  Field: %s  Status: %s" % (exhibit["agency_id"], exhibit["field"], exhibit["status"])]
    if exhibit["status"] != OPEN:
        out += ["", "WITHHELD: no comparison content is shown. Reasons: " + ", ".join(exhibit["reasons"])]
        return "\n".join(out)
    diff, case = exhibit["stage_10_difference"], exhibit["stage_12_case"]
    out += ["", "## Stage 10 difference (%s)" % diff["reason"], "Citation: %s" % diff["citation"],
            "Expected: %s" % _line(diff["expected"]), "Observed: %s" % _line(diff["observed"])]
    out += ["Policy source: %s @ %s" % (e["locator"], e["source_id"]) for e in diff["policy_locators"]]
    out += ["", "## Stage 12 case: request %s, stage %s" % (case["request_id"], case["stage"])]
    if case["issues"]:
        out.append("Timeline issues: " + ", ".join(case["issues"]))
    for loc in case["received_locators"]:
        out += ["Received item %s (%s): %s, original %s" % (loc["item_id"], loc["kind"], loc["locator"], loc["original_sha256"]),
                "Quote: %s" % _line(loc["quote"])]
    out += ["Timeline: %s %s (item %s)" % (e["date"], e["type"], e["item_id"]) for e in case["timeline_events"]]
    return "\n".join(out)


def _load(path):
    info = os.lstat(path)
    if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_INPUT:
        raise ValueError("input is not a regular file within the size limit")
    with open(path, "rb") as handle:
        return json.loads(handle.read().decode("utf-8"))


def _write_new(path, text):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(text)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Open one synthetic stage case exhibit (review only).")
    for name in ("comparison", "timeline", "received", "agency", "field", "out"):
        parser.add_argument("--" + name, required=True)
    args = parser.parse_args(argv)
    try:
        documents = [_load(args.comparison), _load(args.timeline), _load(args.received)]
    except (OSError, ValueError):
        documents = [None, None, None]
    exhibit = open_case(documents[0], documents[1], documents[2], args.agency, args.field)
    out = Path(args.out)
    out.mkdir(mode=0o700, parents=True, exist_ok=False)
    _write_new(out / "exhibit.json", json.dumps(exhibit, indent=2, sort_keys=True) + "\n")
    _write_new(out / "EXHIBIT.md", render(exhibit) + "\n")
    print("%s %s %s" % (exhibit["status"], exhibit["agency_id"], ",".join(exhibit["reasons"]) or "-"))
    return 0 if exhibit["status"] == OPEN else 1


if __name__ == "__main__":
    sys.exit(main())
