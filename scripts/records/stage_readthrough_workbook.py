#!/usr/bin/env python3
"""Offline read-through workbook for stages 10-14, built from the stage review agenda (display only).

usage: stage_readthrough_workbook.py make AGENDA.json --out DIR
       stage_readthrough_workbook.py check WORKBOOK.json AGENDA.json [--prior PRIOR-WORKBOOK.json]

``make`` turns the agenda that the five-driver runner feeds (``stage-review-agenda-v1``) into one workbook: for each
of stages 10 to 14 the engine, the receipt, the driver hash, every step output (path and sha256) the human is to open,
the next human check, and BLANK fields for the reviewer, the date, the finding and the outcome. It refuses to make a
workbook unless all five stages are READY_FOR_HUMAN_CHECK on the same engine as the main commit.

``check`` reads a (partly) filled workbook against the current agenda and prints flags; it never fills a field and never
says a stage is accepted. Flags: a stage missing, duplicated or unknown, an entry whose evidence is not the current
run's (a copied entry from a prior run carries the older digest), fields edited after the digest was stamped, a
half-filled human block, the same finding text under two stages, and (with ``--prior``) a finding copied word for
word from a prior run's entry for the same stage whose evidence differs. Exit 0 = no flags, 1 = flags, 2 = usage or
unreadable input.

Review aid only. It signs nothing, sends nothing, writes no index or ledger, opens no source and makes no network call.
It is not a second gate: the existing sitting evaluator stays the only verdict. Standard library only.
"""
import argparse
import hashlib
import json
import os
import stat
import sys
from pathlib import Path

SCHEMA = "stage-readthrough-workbook-v1"
AGENDA_SCHEMA = "stage-review-agenda-v1"
READY = "READY_FOR_HUMAN_CHECK"
STAGES = ("10", "11", "12", "13", "14")
HUMAN_FIELDS = ("reviewer", "checked_on", "finding", "outcome")
NOTE = ("Review aid for an attended read-through. The human fields are blank on purpose and are filled only by the "
        "person who opened the evidence. Nothing here is acceptance, a signature, a booking or a legal conclusion.")
MAX_INPUT = 8 * 1024 * 1024


class Refused(Exception):
    """Fixed UPPER_SNAKE code describing why a workbook or check could not be produced."""


def _sha256_hex(data):
    return hashlib.sha256(data).hexdigest()


def _load(path):
    try:
        info = os.lstat(path)
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_INPUT:
            raise Refused("INPUT_NOT_A_REGULAR_FILE")
        raw = Path(path).read_bytes()
        return json.loads(raw.decode("utf-8")), _sha256_hex(raw)
    except (OSError, ValueError) as exc:
        raise Refused("INPUT_UNREADABLE") from exc


def _digest(entry):
    """Digest of the evidence a human is asked to open, never of the human fields."""
    basis = {key: entry.get(key) for key in ("stage", "engine", "receipt_sha256", "driver_source_sha256", "finished_at")}
    basis["references"] = [{key: ref.get(key) for key in ("step", "path", "sha256")} for ref in entry.get("references") or []]
    return _sha256_hex(json.dumps(basis, sort_keys=True, separators=(",", ":")).encode())


def _entry_from_row(row, main_commit):
    cite = row.get("citation") if isinstance(row.get("citation"), dict) else {}
    refs = []
    for step in cite.get("steps") or []:
        for out in step.get("outputs") or []:
            refs.append({"step": step.get("step"), "code": step.get("code"), "path": out.get("path"),
                         "sha256": out.get("sha256")})
    if not refs or any(not (ref["step"] and ref["path"] and isinstance(ref["sha256"], str) and len(ref["sha256"]) == 64)
                       for ref in refs):
        raise Refused("STAGE_%s_EVIDENCE_REFERENCES_INCOMPLETE" % row.get("stage"))
    if cite.get("receipt_sha256_in_set") != cite.get("receipt_sha256_on_disk") or cite.get("engine") != main_commit:
        raise Refused("STAGE_%s_RECEIPT_OR_ENGINE_MISMATCH" % row.get("stage"))
    entry = {"stage": row["stage"], "readiness": row["readiness"], "engine": cite["engine"],
             "driver_modules": list(cite.get("driver_modules") or []), "driver_source_sha256": cite.get("driver_source_sha256"),
             "receipt": os.path.basename(str(cite.get("receipt"))), "receipt_sha256": cite.get("receipt_sha256_on_disk"),
             "finished_at": cite.get("finished_at"), "references": refs,
             "next_human_check": row.get("next_human_check"), "human": {field: "" for field in HUMAN_FIELDS}}
    entry["evidence_digest"] = _digest(entry)
    return entry


def make(agenda, agenda_sha):
    if not isinstance(agenda, dict) or agenda.get("schema") != AGENDA_SCHEMA or not isinstance(agenda.get("rows"), list):
        raise Refused("AGENDA_NOT_RECOGNISED")
    main_commit = agenda.get("main_commit")
    if not (isinstance(main_commit, str) and len(main_commit) == 40) or agenda.get("set_engine") != main_commit:
        raise Refused("AGENDA_ENGINE_NOT_THE_MAIN_COMMIT")
    rows = {}
    for row in agenda["rows"]:
        stage = row.get("stage") if isinstance(row, dict) else None
        if stage in rows:
            raise Refused("AGENDA_DUPLICATE_STAGE_" + str(stage))
        rows[stage] = row
    if tuple(sorted(rows)) != STAGES:
        raise Refused("AGENDA_DOES_NOT_COVER_STAGES_10_TO_14")
    not_ready = [stage for stage in STAGES if rows[stage].get("readiness") != READY]
    if not_ready:
        raise Refused("STAGES_NOT_READY_" + "_".join(not_ready))
    return {"schema": SCHEMA, "note": NOTE, "accepted": False, "agenda_sha256": agenda_sha, "main_commit": main_commit,
            "entries": [_entry_from_row(rows[stage], main_commit) for stage in STAGES]}


def render(workbook):
    out = ["# Stage 10-14 read-through workbook (attended; fields blank until a person fills them)", "", workbook["note"], "",
           "Engine / main commit: " + workbook["main_commit"], "Agenda sha256: " + workbook["agenda_sha256"], ""]
    for entry in workbook["entries"]:
        out += ["## Stage %s  (evidence digest %s)" % (entry["stage"], entry["evidence_digest"][:12]),
                "Driver %s, receipt %s (%s), finished %s" % (", ".join(entry["driver_modules"]), entry["receipt"],
                                                             entry["receipt_sha256"][:12], entry["finished_at"]),
                "Open:"]
        out += ["  - %s %s: %s  sha256 %s" % (ref["step"], ref["code"], ref["path"], ref["sha256"][:12])
                for ref in entry["references"]]
        out += ["Next human check: " + str(entry["next_human_check"]),
                "Reviewer: ____   Date: ____   Outcome: ____", "Finding: ____", ""]
    return "\n".join(out)


def check(workbook, agenda, prior=None):
    """Return (flags, states). A flag is (stage, CODE). states maps each stage to BLANK, FILLED or FLAGGED."""
    if not isinstance(workbook, dict) or workbook.get("schema") != SCHEMA or not isinstance(workbook.get("entries"), list):
        raise Refused("WORKBOOK_NOT_RECOGNISED")
    current = make(agenda, "-")  # the same refusals as make: only a ready, same-engine agenda is a reference
    expected = {entry["stage"]: entry["evidence_digest"] for entry in current["entries"]}
    flags, seen = [], {}
    for entry in workbook["entries"]:
        stage = entry.get("stage") if isinstance(entry, dict) else None
        if stage not in expected:
            flags.append((str(stage), "UNKNOWN_STAGE"))
        elif stage in seen:
            flags.append((stage, "DUPLICATE_STAGE"))
        else:
            seen[stage] = entry
    flags += [(stage, "MISSING_STAGE") for stage in STAGES if stage not in seen]
    prior_by_stage = {}
    if prior is not None:
        if not isinstance(prior, dict) or prior.get("schema") != SCHEMA:
            raise Refused("PRIOR_WORKBOOK_NOT_RECOGNISED")
        prior_by_stage = {e.get("stage"): e for e in prior.get("entries") or [] if isinstance(e, dict)}
    findings = {}
    for stage, entry in seen.items():
        human = entry.get("human") if isinstance(entry.get("human"), dict) else {}
        values = {field: str(human.get(field) or "").strip() for field in HUMAN_FIELDS}
        if _digest(entry) != entry.get("evidence_digest"):
            flags.append((stage, "ENTRY_FIELDS_ALTERED"))
        if entry.get("evidence_digest") != expected[stage]:
            flags.append((stage, "EVIDENCE_NOT_CURRENT"))
        if any(values.values()) and not all(values.values()):
            flags.append((stage, "PARTIAL_HUMAN_ENTRY"))
        text = " ".join(values["finding"].split()).lower()
        if text:
            findings.setdefault(text, []).append(stage)
            old = prior_by_stage.get(stage)
            old_text = " ".join(str(((old or {}).get("human") or {}).get("finding") or "").split()).lower()
            if old and old_text == text and old.get("evidence_digest") != expected[stage]:
                flags.append((stage, "COPIED_FROM_PRIOR_RUN"))
    flags += [(stage, "SAME_FINDING_UNDER_TWO_STAGES") for stages in findings.values() if len(stages) > 1 for stage in stages]
    flagged = {stage for stage, _ in flags}
    states = {}
    for stage in STAGES:
        entry = seen.get(stage) or {}
        human = entry.get("human") if isinstance(entry.get("human"), dict) else {}
        filled = any(str(human.get(field) or "").strip() for field in HUMAN_FIELDS)
        states[stage] = "FLAGGED" if stage in flagged else ("FILLED" if filled else "BLANK")
    return sorted(set(flags)), states


def _write_new(path, text):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(text)


def main(argv=None):
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    mk = sub.add_parser("make")
    mk.add_argument("agenda")
    mk.add_argument("--out", required=True)
    ck = sub.add_parser("check")
    ck.add_argument("workbook")
    ck.add_argument("agenda")
    ck.add_argument("--prior")
    args = parser.parse_args(argv)
    try:
        agenda, agenda_sha = _load(args.agenda)
        if args.command == "make":
            workbook = make(agenda, agenda_sha)
            out = Path(args.out)
            out.mkdir(mode=0o700, parents=True, exist_ok=False)
            _write_new(out / "workbook.json", json.dumps(workbook, indent=2, sort_keys=True) + "\n")
            _write_new(out / "WORKBOOK.md", render(workbook) + "\n")
            print("WORKBOOK stages=%s engine=%s fields=blank" % (",".join(STAGES), workbook["main_commit"][:8]))
            return 0
        workbook, _ = _load(args.workbook)
        prior = _load(args.prior)[0] if args.prior else None
        flags, states = check(workbook, agenda, prior)
    except Refused as exc:
        print("REFUSED " + str(exc), file=sys.stderr)
        return 2
    for stage, code in flags:
        print("FLAG stage %s %s" % (stage, code))
    print("CHECK " + " ".join("%s=%s" % (stage, states[stage]) for stage in STAGES) + " flags=%d (no acceptance inferred)" % len(flags))
    return 1 if flags else 0


if __name__ == "__main__":
    sys.exit(main())
