#!/usr/bin/env python3
"""Source index for the stage 10-14 read-through: receipt -> request -> document -> locator (display only).

usage: stage_source_index.py build AGENDA.json --out DIR

``build`` takes the same agenda the read-through workbook is made from (``stage-review-agenda-v1``; the same refusals:
all five stages READY_FOR_HUMAN_CHECK on one engine equal to the main commit) and, for every stage, follows the
receipt to the step outputs it names. Before reading anything it re-hashes the receipt and every referenced output
under the receipt's own directory; a changed, missing, linked or out-of-directory file refuses the whole index.

From the JSON / JSONL outputs it lists, per run and document, which request the document is tied to (for agency-level
sources such as policy documents, which agency) and where in the document the citation points (``page N`` or
``message part P``). Each link gets exactly one status:

  LINKED         one request and/or one agency, at least one page or message-part locator
  NO_LOCATOR     attributed, but nothing says which page or part (stays unreviewed; a page is never invented)
  UNATTRIBUTED   the output names the document but ties it to no request or agency (stays unreviewed)
  CROSS_AGENCY   the same document is tied to two or more agencies in one run (withheld; never attributed)
  CROSS_REQUEST  the same document is tied to two or more requests in one run (withheld; never attributed)

Only identifiers, locators, paths and digests are copied; quotes, bodies and other text are never copied. Document
identities are sha256 digests (``item_id``, ``source_id``, ``document_sha256``). Every stage also carries the
workbook's evidence digest for that stage, so the index cannot be paired with another run's workbook.

Review aid only. It reads saved synthetic rehearsal outputs; it signs nothing, sends nothing, writes no ledger, index
or store, opens no network connection and draws no legal conclusion. Exit 0 = index written, 2 = refused or usage.
Standard library only.
"""
import argparse
import hashlib
import importlib.util
import json
import os
import re
import stat
import sys
from pathlib import Path, PurePosixPath

SCHEMA = "stage-source-index-v1"
STATUSES = ("LINKED", "NO_LOCATOR", "UNATTRIBUTED", "CROSS_AGENCY", "CROSS_REQUEST")
DOC_KEYS = ("item_id", "source_id", "document_sha256")
RUN_KEYS = ("run_id", "source_run_id")
AGENCY_KEYS = ("agency_id", "agency_key")
UNMATCHED = ("unmatched",)
MAX_EVIDENCE = 32 * 1024 * 1024
NOTE = ("Display only. Shows which request (or, for agency-level sources, which agency) each stage's saved evidence "
        "ties a document to, and which page or message part it cites. Statuses other than LINKED stay unreviewed. "
        "Not acceptance, not a gate, not a legal conclusion; the sitting evaluator stays the only verdict.")
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_PAGE_TEXT = re.compile(r"^page (\d+)$")
_PART_TEXT = re.compile(r"^message part (\S+?)(?: \(no page(?: number)?\))?$")


def _load_workbook_module():
    path = Path(__file__).resolve().with_name("stage_readthrough_workbook.py")
    spec = importlib.util.spec_from_file_location("stage_readthrough_workbook", path)
    if spec is None or spec.loader is None:  # pragma: no cover - the sibling ships with this script
        raise ImportError(path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


wb = _load_workbook_module()
Refused = wb.Refused


def _read_regular(path, limit):
    """Bytes of a regular, non-symlink file no larger than ``limit``; None when it is not one."""
    try:
        info = os.lstat(path)
    except OSError:
        return None
    if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
        return None
    try:
        return Path(path).read_bytes()
    except OSError:
        return None


def _evidence_path(base, relative):
    """Resolve a step-output path strictly inside the receipt directory; None for anything else."""
    if not isinstance(relative, str) or not relative or "\\" in relative or "\x00" in relative:
        return None
    if any(part in ("", ".", "..") for part in relative.split("/")):
        return None
    pure = PurePosixPath(relative)
    if pure.is_absolute():
        return None
    current = base
    for part in pure.parts[:-1]:
        current = current / part
        try:
            if not stat.S_ISDIR(os.lstat(current).st_mode):
                return None
        except OSError:
            return None
    return current / pure.parts[-1]


def _doc_id(value):
    """A document identity is a sha256 digest (``sha256:`` prefix allowed); clocks and other ids are not documents."""
    if not isinstance(value, str):
        return None
    value = value.strip().lower()
    if value.startswith("sha256:"):
        value = value[len("sha256:"):]
    return value if _HEX64.match(value) else None


def _positive_int(value):
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 1 else None


def _part(value):
    if _positive_int(value) is not None:
        return str(value)
    if isinstance(value, str) and value.strip() and len(value) <= 64:
        return value.strip()
    return None


def _locators(node):
    """Page / message-part locators stated by one document reference (never invented)."""
    found = set()
    page = _positive_int(node.get("page"))
    if page is not None:
        found.add(("page", str(page)))
    part = _part(node.get("part"))
    if part is not None:
        found.add(("part", part))
    basis = node.get("kind_basis")
    if isinstance(basis, dict):
        page = _positive_int(basis.get("page"))
        if page is not None:
            found.add(("page", str(page)))
        part = _part(basis.get("part"))
        if part is not None:
            found.add(("part", part))
    text = node.get("locator")
    if isinstance(text, str):
        match = _PAGE_TEXT.match(text.strip())
        if match and int(match.group(1)) >= 1:
            found.add(("page", str(int(match.group(1)))))
        match = _PART_TEXT.match(text.strip())
        if match:
            found.add(("part", match.group(1)))
    return found


def _owner(value):
    """A request or agency identifier, or None for absent, empty or the pipeline's ``unmatched`` placeholder."""
    if not isinstance(value, str) or not value.strip() or value.strip().lower() in UNMATCHED:
        return None
    return value.strip()


def _walk(node, context, out, path):
    """Collect (run, request, agency, document, locators, evidence path) from one JSON value.

    The nearest enclosing ``run_id`` / ``request_id`` / ``agency_id`` (or ``agency_key``) applies; an explicit null or
    ``unmatched`` value clears what an outer object said, so an unmatched item never inherits a neighbour's request.
    """
    if isinstance(node, list):
        for value in node:
            _walk(value, context, out, path)
        return
    if not isinstance(node, dict):
        return
    context = dict(context)
    for key in RUN_KEYS:
        if isinstance(node.get(key), str) and node[key]:
            context["run"] = node[key]
            break
    if "request_id" in node:
        context["request"] = _owner(node["request_id"])
    for key in AGENCY_KEYS:
        if key in node:
            context["agency"] = _owner(node[key])
            break
    docs = {_doc_id(node.get(key)) for key in DOC_KEYS} - {None}
    if docs:
        locators = _locators(node)
        for doc in docs:
            out.append((context.get("run"), context.get("request"), context.get("agency"), doc, locators, path))
    for value in node.values():
        if isinstance(value, (dict, list)):
            _walk(value, context, out, path)


def _parse(raw, path):
    text = raw.decode("utf-8")
    if path.endswith(".jsonl"):
        return [json.loads(line) for line in text.splitlines() if line.strip()]
    return json.loads(text)


def _stage_links(entry, receipt):
    """Verify the receipt and its outputs byte-for-byte, then extract the stage's links."""
    stage = entry["stage"]
    raw = _read_regular(receipt, MAX_EVIDENCE)
    if raw is None:
        raise Refused("STAGE_%s_RECEIPT_MISSING" % stage)
    if wb._sha256_hex(raw) != entry["receipt_sha256"]:
        raise Refused("STAGE_%s_RECEIPT_CHANGED" % stage)
    base = Path(receipt).parent
    refs = []
    for ref in entry["references"]:
        target = _evidence_path(base, ref["path"])
        if target is None:
            raise Refused("STAGE_%s_EVIDENCE_PATH_OUTSIDE_RECEIPT" % stage)
        data = _read_regular(target, MAX_EVIDENCE)
        if data is None:
            raise Refused("STAGE_%s_EVIDENCE_MISSING" % stage)
        if wb._sha256_hex(data) != ref["sha256"]:
            raise Refused("STAGE_%s_EVIDENCE_CHANGED" % stage)
        refs.append((ref["path"], ref["sha256"], data))
    seen, raw_links, opaque = set(), [], 0
    for path, sha, data in refs:
        if (path, sha) in seen:
            continue
        seen.add((path, sha))
        if not (path.endswith(".json") or path.endswith(".jsonl")):
            opaque += 1
            continue
        try:
            value = _parse(data, path)
        except ValueError as exc:
            raise Refused("STAGE_%s_EVIDENCE_NOT_JSON" % stage) from exc
        _walk(value, {}, raw_links, {"path": path, "sha256": sha})
    return _classify(raw_links), opaque


def _classify(raw_links):
    """One link per (run, document). A document tied to two requests (or two agencies) in one run is never attributed."""
    merged = {}
    for run_id, request, agency, doc, locators, evidence in raw_links:
        slot = merged.setdefault((run_id or "", doc), {"requests": set(), "agencies": set(), "locators": set(),
                                                         "evidence": {}})
        if request is not None:
            slot["requests"].add(request)
        if agency is not None:
            slot["agencies"].add(agency)
        slot["locators"] |= locators
        slot["evidence"][(evidence["path"], evidence["sha256"])] = evidence
    links = []
    for (run_id, doc), slot in merged.items():
        if len(slot["requests"]) > 1:
            status = "CROSS_REQUEST"
        elif len(slot["agencies"]) > 1:
            status = "CROSS_AGENCY"
        elif not slot["requests"] and not slot["agencies"]:
            status = "UNATTRIBUTED"
        elif not slot["locators"]:
            status = "NO_LOCATOR"
        else:
            status = "LINKED"
        links.append({"run_id": run_id or None, "document_id": doc, "status": status,
                      "requests": sorted(slot["requests"]), "agencies": sorted(slot["agencies"]),
                      "locators": [{"kind": kind, "value": value} for kind, value in sorted(slot["locators"])],
                      "evidence": [slot["evidence"][key] for key in sorted(slot["evidence"])]})
    links.sort(key=lambda link: (link["run_id"] or "", link["requests"], link["agencies"], link["document_id"]))
    return links


def build(agenda, agenda_sha):
    """Return the source index dict for a ready agenda, or raise Refused."""
    workbook = wb.make(agenda, agenda_sha)
    rows = {row["stage"]: row for row in agenda["rows"]}
    stages = []
    for entry in workbook["entries"]:
        links, opaque = _stage_links(entry, str(rows[entry["stage"]]["citation"]["receipt"]))
        counts = {status: sum(1 for link in links if link["status"] == status) for status in STATUSES}
        stages.append({"stage": entry["stage"], "evidence_digest": entry["evidence_digest"], "engine": entry["engine"],
                       "receipt": entry["receipt"], "receipt_sha256": entry["receipt_sha256"],
                       "runs": sorted({link["run_id"] for link in links if link["run_id"]}),
                       "counts": counts, "opaque_outputs": opaque, "links": links})
    return {"schema": SCHEMA, "note": NOTE, "accepted": False, "main_commit": workbook["main_commit"],
            "agenda_sha256": agenda_sha, "stages": stages}


def render(index):
    out = ["# Stage 10-14 source index (display only)", "", index["note"], "",
           "Engine / main commit: " + index["main_commit"], "Agenda sha256: " + index["agenda_sha256"], ""]
    for stage in index["stages"]:
        out += ["## Stage %s  (evidence digest %s, receipt %s)" % (stage["stage"], stage["evidence_digest"][:12],
                                                                   stage["receipt_sha256"][:12]),
                "Counts: " + ", ".join("%s %d" % (status, stage["counts"][status]) for status in STATUSES)]
        for link in stage["links"]:
            where = ", ".join("%s %s" % ("page" if loc["kind"] == "page" else "message part", loc["value"])
                              for loc in link["locators"]) or "no page or part stated"
            out.append("  - %s  request %s  agency %s  document %s  (%s)  run %s  from %s" % (
                link["status"], ", ".join(link["requests"]) or "(none)", ", ".join(link["agencies"]) or "(none)",
                link["document_id"][:16], where, link["run_id"] or "(none)",
                ", ".join(ev["path"] for ev in link["evidence"])))
        out.append("")
    return "\n".join(out)


def counts_line(index):
    return " ".join("%s=%s" % (stage["stage"], ",".join("%s:%d" % (status.lower(), stage["counts"][status])
                                                         for status in STATUSES)) for stage in index["stages"])


def main(argv=None):
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    bd = sub.add_parser("build")
    bd.add_argument("agenda")
    bd.add_argument("--out", required=True)
    args = parser.parse_args(argv)
    try:
        agenda, agenda_sha = wb._load(args.agenda)
        index = build(agenda, agenda_sha)
        out = Path(args.out)
        try:
            out.mkdir(mode=0o700, parents=True, exist_ok=False)
        except OSError as exc:
            raise Refused("OUTPUT_DIRECTORY_NOT_NEW") from exc
        wb._write_new(out / "source-index.json", json.dumps(index, indent=2, sort_keys=True) + "\n")
        wb._write_new(out / "SOURCE-INDEX.md", render(index) + "\n")
    except Refused as exc:
        print("REFUSED " + str(exc), file=sys.stderr)
        return 2
    print("INDEX engine=%s %s (display only)" % (index["main_commit"][:8], counts_line(index)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
