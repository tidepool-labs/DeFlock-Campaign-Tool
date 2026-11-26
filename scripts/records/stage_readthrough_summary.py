#!/usr/bin/env python3
"""One source-backed summary of the stage 10-14 read-through: each stage is a recorded human finding or unreviewed.

usage: stage_readthrough_summary.py WORKBOOK.json AGENDA.json SOURCE-INDEX.json [--prior PRIOR.json] [--out DIR]

Joins three things that must describe the same run: the read-through workbook (``stage_readthrough_workbook.py``),
the agenda it was made from, and the source index (``stage_source_index.py``) built from that agenda. It refuses
(exit 2) before saying anything about a stage when they do not:

  WORKBOOK_HEADER_NOT_CURRENT    the workbook's main commit differs from the agenda's, or it claims acceptance
  WORKBOOK_AGENDA_MISMATCH       the workbook was made from different agenda bytes
  INDEX_NOT_RECOGNISED / INDEX_ENGINE_MISMATCH / INDEX_AGENDA_MISMATCH
  INDEX_NOT_REPRODUCIBLE         rebuilding the index from the agenda and its on-disk evidence gives something else
  plus every refusal of the workbook check and of the index build (changed receipt or evidence, engine mix, ...)

Then each stage gets exactly one status:

  UNREVIEWED        the human fields are blank. Blank is never turned into a finding, a pass or acceptance.
  FINDING_RECORDED  a person filled every field, the workbook check raised no structural flag, ``outcome`` is one of
                    ``matches`` / ``differs`` / ``cannot_tell``, ``checked_on`` is a real ISO date, and every entry
                    of the structured ``cited`` field resolves exactly to the stage's own LINKED sources. The
                    recorded finding IS ``checked_on`` + ``outcome`` + ``cited``; still not acceptance.
  WITHHELD          anything flagged: the workbook check's structural flags (swapped, missing, duplicated or edited
                    entry, evidence not current, partial entry), the stage has no LINKED source
                    (FINDING_WITHOUT_LINKED_SOURCE), a structured field out of its form (FINDING_OUTCOME_NOT_IN_LIST,
                    FINDING_DATE_NOT_ISO), the same ``outcome`` + ``cited`` copied from a ``--prior`` run whose
                    evidence differs (CITED_COPIED_FROM_PRIOR_RUN), or a citation rule below.

Free text never reaches a status or the output. The prose ``finding`` (the reviewer's own note) and ``reviewer`` must be
non-empty (a blank one is a partial entry: presence only), but their content is NEVER parsed, compared or carried into
the summary. So the workbook check's two content comparisons of the note (SAME_FINDING_UNDER_TWO_STAGES,
COPIED_FROM_PRIOR_RUN) are ignored here; the structured copy check above replaces the second. Readers open the
workbook for the note and the reviewer's name.

Citations are read ONLY from ``cited``. ``cited`` is a ``;``-separated list; each entry is exactly one of (keywords
case-insensitive, single spaces):

  document <12-64 hex>                 a LINKED document of this stage, named by a unique prefix
  document <12-64 hex> page <N>        ... and N is a page that document's own locators state
  document <12-64 hex> part <P>        ... and P (the whole token, up to 64 non-space characters) is a message
                                       part that document's own locators state; ``2`` never matches ``2.1``
  request <ID>                         a request this stage's LINKED sources are tied to

  FINDING_WITHOUT_CITATION          no ``document`` entry at all (a finding must point at a source)
  FINDING_CITATION_UNPARSED         an entry in no listed form (trailing punctuation, extra words, page 0, ...)
  FINDING_CITES_UNLINKED_DOCUMENT   the prefix is no LINKED document of this stage (cross-agency, unattributed, ...)
  FINDING_CITATION_AMBIGUOUS        the prefix fits two LINKED documents
  FINDING_CITES_MISSING_PAGE / FINDING_CITES_MISSING_PART   that document does not state that page or part
  FINDING_CITES_OTHER_REQUEST       a request of another stage or source of this run
  FINDING_CITES_UNKNOWN_REQUEST     a request that appears nowhere in this run

Sources that are not LINKED (no locator, unattributed, cross-agency, cross-request) are listed as unreviewed for every
stage. Exit 0 = nothing withheld, 1 = at least one stage withheld, 2 = refused or usage. ``accepted`` is always false.

Review aid only: it signs nothing, sends nothing, writes no ledger, index or store, opens no network connection and
draws no legal conclusion. The sitting evaluator stays the only verdict. Standard library only.
"""
import argparse
import datetime
import importlib.util
import json
import re
import sys
from pathlib import Path

SCHEMA = "stage-readthrough-summary-v2"
NOTE = ("Summary of an attended read-through of synthetic rehearsal evidence. A FINDING_RECORDED stage shows only "
        "structured fields a person filled (date, outcome and checked citations); the free-text note and the "
        "reviewer's name stay in the workbook and are not carried here. Nothing here is acceptance, a signature, a "
        "booking or a legal conclusion. UNREVIEWED and WITHHELD stages have no finding.")
OUTCOMES = ("matches", "differs", "cannot_tell")
_CITE_DOCUMENT = re.compile(r"document ([0-9a-f]{12,64})(?: page ([1-9][0-9]{0,5})| part (\S{1,64}))?",
                            re.IGNORECASE | re.ASCII)
_CITE_REQUEST = re.compile(r"request (\S{1,128})", re.IGNORECASE | re.ASCII)
_ISO_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
CARRIED_FIELDS = ("checked_on", "outcome", "cited")  # never "finding" or "reviewer": free text is not carried
PROSE_DERIVED_FLAGS = ("SAME_FINDING_UNDER_TWO_STAGES", "COPIED_FROM_PRIOR_RUN")  # workbook flags that read the note


def _load_sibling(name):
    path = Path(__file__).resolve().with_name(name + ".py")
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:  # pragma: no cover - siblings ship with this script
        raise ImportError(path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


index_mod = _load_sibling("stage_source_index")
wb = index_mod.wb
Refused = wb.Refused


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _form_flags(human):
    """Flags for structured human fields outside their form. Free-text fields are never inspected."""
    flags = []
    if human["outcome"] not in OUTCOMES:
        flags.append("FINDING_OUTCOME_NOT_IN_LIST")
    try:
        if not _ISO_DATE.fullmatch(human["checked_on"]):
            raise ValueError
        datetime.date.fromisoformat(human["checked_on"])
    except ValueError:
        flags.append("FINDING_DATE_NOT_ISO")
    return flags


def _structured(human):
    return human.get("outcome", ""), " ".join(human.get("cited", "").split()).lower()


def _human(entry):
    human = (entry or {}).get("human")
    if not isinstance(human, dict):
        return {field: "" for field in wb.HUMAN_FIELDS}
    return {field: str(human.get(field) or "").strip() for field in wb.HUMAN_FIELDS}


def _finding_flags(cited, stage_links, all_links):
    """Flags for ``cited`` entries that are not exactly this stage's LINKED sources (see module doc)."""
    linked = [link for link in stage_links if link["status"] == "LINKED"]
    if not linked:
        return ["FINDING_WITHOUT_LINKED_SOURCE"]
    own_requests = {req.lower() for link in linked for req in link["requests"]}
    known_requests = {req.lower() for link in all_links for req in link["requests"]}
    flags = []
    documents = 0
    for raw in cited.split(";"):
        entry = " ".join(raw.split())
        if not entry:
            continue
        request = _CITE_REQUEST.fullmatch(entry)
        document = _CITE_DOCUMENT.fullmatch(entry)
        if request:
            value = request.group(1).lower()
            if value not in own_requests:
                flags.append("FINDING_CITES_OTHER_REQUEST" if value in known_requests
                             else "FINDING_CITES_UNKNOWN_REQUEST")
            continue
        if not document:
            flags.append("FINDING_CITATION_UNPARSED")
            continue
        documents += 1
        prefix, page, part = document.group(1).lower(), document.group(2), document.group(3)
        targets = [link for link in linked if link["document_id"].startswith(prefix)]
        if not targets:
            flags.append("FINDING_CITES_UNLINKED_DOCUMENT")
        elif len(targets) > 1:
            flags.append("FINDING_CITATION_AMBIGUOUS")
        elif page is not None and not any(loc["kind"] == "page" and loc["value"] == page
                                          for loc in targets[0]["locators"]):
            flags.append("FINDING_CITES_MISSING_PAGE")
        elif part is not None and not any(loc["kind"] == "part" and loc["value"].lower() == part.lower()
                                          for loc in targets[0]["locators"]):
            flags.append("FINDING_CITES_MISSING_PART")
    if not documents:
        flags.append("FINDING_WITHOUT_CITATION")
    return sorted(set(flags))


def summarize(workbook, agenda, agenda_sha, index, prior=None):
    """Return the summary dict, or raise Refused when the three inputs do not describe one run."""
    if not isinstance(workbook, dict) or workbook.get("schema") != wb.SCHEMA:
        raise Refused("WORKBOOK_NOT_RECOGNISED")
    if not isinstance(agenda, dict) or workbook.get("main_commit") != agenda.get("main_commit") \
            or workbook.get("accepted") is not False:
        raise Refused("WORKBOOK_HEADER_NOT_CURRENT")
    if workbook.get("agenda_sha256") != agenda_sha:
        raise Refused("WORKBOOK_AGENDA_MISMATCH")
    if not isinstance(index, dict) or index.get("schema") != index_mod.SCHEMA or not isinstance(index.get("stages"), list):
        raise Refused("INDEX_NOT_RECOGNISED")
    if index.get("main_commit") != agenda.get("main_commit"):
        raise Refused("INDEX_ENGINE_MISMATCH")
    if index.get("agenda_sha256") != agenda_sha:
        raise Refused("INDEX_AGENDA_MISMATCH")
    rebuilt = index_mod.build(agenda, agenda_sha)  # same refusals as the index: evidence must still be on disk, unchanged
    if _canonical(rebuilt) != _canonical(index):
        raise Refused("INDEX_NOT_REPRODUCIBLE")
    flags, _states = wb.check(workbook, agenda, prior)  # refusals and structural flags; note-content flags dropped
    by_stage = {}
    for stage, code in flags:
        if code not in PROSE_DERIVED_FLAGS:
            by_stage.setdefault(stage, []).append(code)
    expected = {entry["stage"]: entry["evidence_digest"] for entry in wb.make(agenda, "-")["entries"]}
    prior_by_stage = {}
    for entry in (prior or {}).get("entries") or []:
        if isinstance(entry, dict) and entry.get("stage") in wb.STAGES:
            prior_by_stage.setdefault(entry["stage"], entry)
    entries = {}
    for entry in workbook["entries"]:
        if isinstance(entry, dict) and entry.get("stage") in wb.STAGES:
            entries.setdefault(entry["stage"], entry)
    all_links = [link for stage in rebuilt["stages"] for link in stage["links"]]
    stages = []
    for stage in rebuilt["stages"]:
        name = stage["stage"]
        human = _human(entries.get(name))
        filled = any(human.values())
        stage_flags = list(by_stage.get(name, []))
        if filled and not stage_flags:
            stage_flags += _form_flags(human) + _finding_flags(human["cited"], stage["links"], all_links)
            old = prior_by_stage.get(name)
            if old is not None and old.get("evidence_digest") != expected[name] \
                    and all(_structured(_human(old))) and _structured(_human(old)) == _structured(human):
                stage_flags.append("CITED_COPIED_FROM_PRIOR_RUN")
        if stage_flags:
            status = "WITHHELD"
        elif filled:
            status = "FINDING_RECORDED"
        else:
            status = "UNREVIEWED"
        linked = [link for link in stage["links"] if link["status"] == "LINKED"]
        stages.append({"stage": name, "status": status, "flags": sorted(set(stage_flags)),
                       "evidence_digest": stage["evidence_digest"], "engine": stage["engine"],
                       "receipt_sha256": stage["receipt_sha256"],
                       "human": {field: human[field] for field in CARRIED_FIELDS}
                                if status == "FINDING_RECORDED" else None,
                       "linked_sources": linked,
                       "unreviewed_sources": [link for link in stage["links"] if link["status"] != "LINKED"]})
    return {"schema": SCHEMA, "note": NOTE, "accepted": False, "main_commit": agenda["main_commit"],
            "agenda_sha256": agenda_sha, "workbook_flags": sum(len(codes) for codes in by_stage.values()), "stages": stages}


def render(summary):
    out = ["# Stage 10-14 read-through summary", "", summary["note"], "",
           "Engine / main commit: " + summary["main_commit"], "Agenda sha256: " + summary["agenda_sha256"], ""]
    for stage in summary["stages"]:
        out.append("## Stage %s: %s%s" % (stage["stage"], stage["status"],
                                          ("  (" + ", ".join(stage["flags"]) + ")") if stage["flags"] else ""))
        if stage["human"]:
            out += ["Checked on %s, outcome %s" % (stage["human"]["checked_on"], stage["human"]["outcome"]),
                    "Cited (each entry checked against the linked sources): " + stage["human"]["cited"],
                    "Reviewer's name and note: in the workbook only (free text is not carried or checked here)"]
        for title, key in (("Linked sources", "linked_sources"), ("Unreviewed sources", "unreviewed_sources")):
            out.append(title + ":" + ("" if stage[key] else " none"))
            for link in stage[key]:
                where = ", ".join("%s %s" % ("page" if loc["kind"] == "page" else "message part", loc["value"])
                                  for loc in link["locators"]) or "no page or part stated"
                out.append("  - %s request %s agency %s document %s (%s)" % (
                    link["status"], ", ".join(link["requests"]) or "(none)", ", ".join(link["agencies"]) or "(none)",
                    link["document_id"][:16], where))
        out.append("")
    return "\n".join(out)


def main(argv=None):
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n")[0])
    parser.add_argument("workbook")
    parser.add_argument("agenda")
    parser.add_argument("index")
    parser.add_argument("--prior")
    parser.add_argument("--out")
    args = parser.parse_args(argv)
    try:
        agenda, agenda_sha = wb._load(args.agenda)
        workbook = wb._load(args.workbook)[0]
        index = wb._load(args.index)[0]
        prior = wb._load(args.prior)[0] if args.prior else None
        summary = summarize(workbook, agenda, agenda_sha, index, prior)
        if args.out:
            out = Path(args.out)
            try:
                out.mkdir(mode=0o700, parents=True, exist_ok=False)
            except OSError as exc:
                raise Refused("OUTPUT_DIRECTORY_NOT_NEW") from exc
            wb._write_new(out / "summary.json", json.dumps(summary, indent=2, sort_keys=True) + "\n")
            wb._write_new(out / "SUMMARY.md", render(summary) + "\n")
    except Refused as exc:
        print("REFUSED " + str(exc), file=sys.stderr)
        return 2
    for stage in summary["stages"]:
        for code in stage["flags"]:
            print("FLAG stage %s %s" % (stage["stage"], code))
    withheld = sum(1 for stage in summary["stages"] if stage["status"] == "WITHHELD")
    print("SUMMARY " + " ".join("%s=%s" % (stage["stage"], stage["status"]) for stage in summary["stages"])
          + " withheld=%d (no acceptance inferred)" % withheld)
    return 1 if withheld else 0


if __name__ == "__main__":
    sys.exit(main())
