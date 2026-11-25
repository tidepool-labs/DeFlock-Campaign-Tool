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
  FINDING_RECORDED  a person filled every field, the workbook check raised nothing and the finding names nothing
                    outside the stage's own LINKED sources. Shown verbatim; still not acceptance.
  WITHHELD          anything flagged: the workbook check's flags (copied or swapped entry, edited evidence, partial
                    entry, ...) or the finding cites another request (FINDING_CITES_OTHER_REQUEST), any hex token of
                    12+ characters that is not a prefix of a LINKED document of this stage
                    (FINDING_CITES_UNLINKED_DOCUMENT), a page or message part that the document it is explicitly
                    attached to ("<document> page N" or "page N of <document>") does not have
                    (FINDING_CITES_MISSING_PAGE / FINDING_CITES_MISSING_PART; with no document named, none of the
                    stage's LINKED documents has it), a locator attached to no named document or to two
                    (FINDING_LOCATOR_NOT_BOUND / FINDING_LOCATOR_AMBIGUOUS), or the stage has no LINKED source at
                    all (FINDING_WITHOUT_LINKED_SOURCE).

Sources that are not LINKED (no locator, unattributed, cross-agency, cross-request) are listed as unreviewed for every
stage. Exit 0 = nothing withheld, 1 = at least one stage withheld, 2 = refused or usage. ``accepted`` is always false.

Review aid only: it signs nothing, sends nothing, writes no ledger, index or store, opens no network connection and
draws no legal conclusion. The sitting evaluator stays the only verdict. Standard library only.
"""
import argparse
import importlib.util
import json
import re
import sys
from pathlib import Path

SCHEMA = "stage-readthrough-summary-v1"
NOTE = ("Summary of an attended read-through of synthetic rehearsal evidence. FINDING_RECORDED repeats what a person "
        "wrote; it is not acceptance, a signature, a booking or a legal conclusion. UNREVIEWED and WITHHELD stages "
        "have no finding.")
_PAGE_MENTION = re.compile(r"\bpage\s+(\d+)\b")
_PART_MENTION = re.compile(r"\bmessage part\s+([0-9a-z]+)\b")
_HEX_TOKEN = re.compile(r"(?<![0-9a-z])[0-9a-f]{12,}(?![0-9a-z])")
_ATTACHED_AFTER = re.compile(r"[ \t,:(]*(?:(?:on|at)\s+)?")  # "<document> page N", "<document>, on page N"
_ATTACHED_BEFORE = re.compile(r"\s+(?:of|in|from)\s+(?:(?:item|document)\s+)?")  # "page N of item <document>"


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


def _mentions(text, identifier):
    return re.search(r"(?<![\w-])" + re.escape(identifier.lower()) + r"(?![\w-])", text) is not None


def _finding_flags(text, stage_links, all_links):
    """Flags for a filled finding that names something outside this stage's LINKED sources.

    Every hex token of 12 or more characters must be a prefix of a LINKED document of this stage (other digests do not
    belong in a finding). A ``page N`` / ``message part P`` mention is checked only against the document it is
    explicitly attached to, written either as ``<document> page N`` (``<document>, on page N`` and similar) or as
    ``page N of <document>`` (``of``/``in``/``from``, optionally ``item``/``document``). When the finding names a
    document, a locator attached to none (FINDING_LOCATOR_NOT_BOUND) or to two different documents
    (FINDING_LOCATOR_AMBIGUOUS) is withheld; no pairing is guessed across clauses. With no document named at all, a
    locator must be one of some LINKED document of the stage.
    """
    flags = []
    linked = [link for link in stage_links if link["status"] == "LINKED"]
    if not linked:
        return ["FINDING_WITHOUT_LINKED_SOURCE"]
    own_requests = {req.lower() for link in linked for req in link["requests"]}
    known_requests = {req.lower() for link in all_links for req in link["requests"]}
    if any(_mentions(text, req) for req in known_requests - own_requests):
        flags.append("FINDING_CITES_OTHER_REQUEST")
    named = []  # (start, end, linked links the token names)
    for match in _HEX_TOKEN.finditer(text):
        targets = [link for link in linked if link["document_id"].startswith(match.group(0))]
        if not targets:
            flags.append("FINDING_CITES_UNLINKED_DOCUMENT")
            return flags  # a locator next to an unlinked document cannot be checked against anything
        named.append((match.start(), match.end(), targets))
    for pattern, kind, code in ((_PAGE_MENTION, "page", "FINDING_CITES_MISSING_PAGE"),
                                (_PART_MENTION, "part", "FINDING_CITES_MISSING_PART")):
        for match in pattern.finditer(text):
            value = str(int(match.group(1))) if match.group(1).isdigit() else match.group(1)
            if named:
                bound = [(start, targets) for start, end, targets in named
                         if (end <= match.start() and _ATTACHED_AFTER.fullmatch(text[end:match.start()]))
                         or (start >= match.end() and _ATTACHED_BEFORE.fullmatch(text[match.end():start]))]
                if not bound:
                    flags.append("FINDING_LOCATOR_NOT_BOUND")
                    continue
                if len(bound) > 1:
                    flags.append("FINDING_LOCATOR_AMBIGUOUS")
                    continue
                targets = bound[0][1]
            else:
                targets = linked
            if not any(loc["kind"] == kind and loc["value"].lower() == value
                       for link in targets for loc in link["locators"]):
                flags.append(code)
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
    flags, states = wb.check(workbook, agenda, prior)
    by_stage = {}
    for stage, code in flags:
        by_stage.setdefault(stage, []).append(code)
    entries = {}
    for entry in workbook["entries"]:
        if isinstance(entry, dict) and entry.get("stage") in wb.STAGES:
            entries.setdefault(entry["stage"], entry)
    all_links = [link for stage in rebuilt["stages"] for link in stage["links"]]
    stages = []
    for stage in rebuilt["stages"]:
        name = stage["stage"]
        human = (entries.get(name) or {}).get("human")
        human = {field: str((human or {}).get(field) or "").strip() for field in wb.HUMAN_FIELDS} \
            if isinstance(human, dict) else {field: "" for field in wb.HUMAN_FIELDS}
        stage_flags = list(by_stage.get(name, []))
        if states[name] == "FILLED" and not stage_flags:
            stage_flags += _finding_flags(" ".join(human["finding"].split()).lower(), stage["links"], all_links)
        if stage_flags:
            status = "WITHHELD"
        elif states[name] == "FILLED":
            status = "FINDING_RECORDED"
        else:
            status = "UNREVIEWED"
        linked = [link for link in stage["links"] if link["status"] == "LINKED"]
        stages.append({"stage": name, "status": status, "flags": sorted(set(stage_flags)),
                       "evidence_digest": stage["evidence_digest"], "engine": stage["engine"],
                       "receipt_sha256": stage["receipt_sha256"],
                       "human": human if status == "FINDING_RECORDED" else None,
                       "linked_sources": linked,
                       "unreviewed_sources": [link for link in stage["links"] if link["status"] != "LINKED"]})
    return {"schema": SCHEMA, "note": NOTE, "accepted": False, "main_commit": agenda["main_commit"],
            "agenda_sha256": agenda_sha, "workbook_flags": len(flags), "stages": stages}


def render(summary):
    out = ["# Stage 10-14 read-through summary", "", summary["note"], "",
           "Engine / main commit: " + summary["main_commit"], "Agenda sha256: " + summary["agenda_sha256"], ""]
    for stage in summary["stages"]:
        out.append("## Stage %s: %s%s" % (stage["stage"], stage["status"],
                                          ("  (" + ", ".join(stage["flags"]) + ")") if stage["flags"] else ""))
        if stage["human"]:
            out += ["Reviewer %s on %s, outcome %s" % (stage["human"]["reviewer"], stage["human"]["checked_on"],
                                                       stage["human"]["outcome"]),
                    "Finding (as written): " + stage["human"]["finding"]]
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
