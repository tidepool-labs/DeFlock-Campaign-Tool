"""Stage 16: rehearse the analysis lane in a sandbox, from one synthetic record to one pending finding packet.

The steps follow deploy/records-runtime/run-analysis.sh.in. The driver calls the real verbs in-process: analysis-export,
analysis-sync (receive, then replay) and analysis-packets-import. It then reads back the packet journal and the console's
/packets and /findings handlers. The review DAG (analysis-tick) waits for model and human stages, so the driver does not
run it. The packet step writes two stand-ins for its result: the analysis files for the one synthetic finding, and the
host outbox file a reviewed finding stages. Every byte is synthetic and stays inside the sandbox. That includes the
sandbox's own extraction ledger, which is not the canonical ledger. Nothing is approved, signed, sent or published, the
publication switch stays OFF and no site-change file is written. The result carries only paths, hashes and fixed codes,
never record or finding text.
"""

import argparse
from contextlib import redirect_stderr, redirect_stdout
import csv
import hashlib
from io import StringIO
import json
import os
from pathlib import Path
import sqlite3
import stat
import sys
import tempfile
from types import SimpleNamespace

from .. import law
from . import analysis_bridge, analysis_config, analysis_export, analysis_packets, console_findings
from . import console_packets, console_page_findings, extraction_routes, finding_review, public_packets
from .extraction_ledger import ExtractionLedgerAdapter, canonical, sha
from .ledger import store
from .outcomes import analysis_ready, received_items
from .run_stages import SANDBOX_MARKER

STAGES = ("16",)
TITLE = "Analysis lane: one synthetic record to one pending finding packet"

_RUN = "run_20261102T180000Z_5ced0016"
_AGENCY = "synthetic-agency"
_NAME = "Synthetic Agency Police"
_SLUG = "synthetic-agency-police"
_REQUEST = "SYN-REQ-16"
_RECEIVED = "2026-11-02T18:00:00Z"
_PAGES = ("Synthetic record page one.", "Synthetic record page two.")
_LAW = "us-ca"
_FINDING = "SYN-F1"
_TOPIC = "Synthetic rehearsal topic"
_CLAIM = "Synthetic rehearsal claim for the stage 16 sandbox."
_FACT = {"fact_type": "synthetic_observation", "value": "synthetic value", "page": "2", "verbatim_quote": _PAGES[1]}
_HOLD = "EVIDENCE_FACT_UNREVIEWED"  # the facts are unreviewed and no human reviewed the finding


class _StepFailed(Exception):
    """A semantic check failed; the sole argument is a fixed evidence code."""


def _sandbox_path(path):
    path = Path(os.path.abspath(path))
    if any(parent.is_symlink() for parent in (*path.parents, path)):
        raise ValueError("SANDBOX_INVALID")
    return path


def _require_empty(sandbox):
    info = sandbox.stat()
    if (not stat.S_ISDIR(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o700
            or info.st_uid != os.geteuid() or any(sandbox.iterdir())):
        raise ValueError("SANDBOX_INVALID")


def _put(path, data):
    """Write one private synthetic file under the sandbox (parents 0700, file 0600)."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.write_bytes(data)
    path.chmod(0o600)
    return path


def _json_bytes(document):
    return (json.dumps(document, sort_keys=True) + "\n").encode("utf-8")


def _csv_bytes(header, rows):
    out = StringIO()
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(header)
    writer.writerows([[row.get(name, "") for name in header] for row in rows])
    return out.getvalue().encode("utf-8")


def _call(main, argv):
    """-> (exit status, stdout, stderr) of one real command entry point, run in-process."""
    out, err = StringIO(), StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        status = main(argv)
    return status, out.getvalue(), err.getvalue()


def _paths(sandbox):
    analysis = sandbox / "analysis"
    project = sandbox / "project"
    return SimpleNamespace(
        sandbox=sandbox, project=project, analysis=analysis, method=sandbox / "method", export=sandbox / "export",
        conflict=sandbox / "conflict", tmp=sandbox / "tmp", processing=project / "processing-v1",
        outcomes=project / "run-outputs" / _RUN / "outcomes", profile=analysis / "profiles" / (_SLUG + ".json"),
        config=analysis / "agencies" / _SLUG / "review-config.json",
        outbox=analysis / ".analysis" / "packet-outbox", journal=project / public_packets.JOURNAL,
        receipt=project / analysis_packets.RECEIPT, ledger=analysis / ".analysis" / "analysis-ledger.jsonl")


def _digest():
    return sha(_original_bytes())


def _original_bytes():
    return ("SYNTHETIC STAGE 16 RECORD\n" + "\n".join(_PAGES)).encode("utf-8")


def _enroll(p, source):
    """Bind the synthetic two-page extraction of `source` in the sandbox ledger (the shape the records pipeline enrolls)."""
    run = p.project / "extraction-1"
    units = [{"kind": "pdf_page", "locator": {"page": n}, "text": text, "data": {}} for n, text in enumerate(_PAGES, 1)]
    parser, version = "synthetic-parser", "stage-16"
    metadata = {"stage": "complete", "parser": parser, "parser_version": version,
                "counts": {"pages_expected": len(_PAGES)}, "children": []}
    _put(run / "input", source.read_bytes())
    _put(run / "derived" / "digest.json", canonical(metadata).encode())
    _put(run / "derived" / "units.jsonl", b"".join((canonical(u) + "\n").encode() for u in units))
    receipt = {"schema": "format-extraction-v1", "original_sha256": _digest(), "route": extraction_routes.route("pdf"),
               "parser_adapter": extraction_routes.VERSION, "status": "partial", "units": units,
               "pages": extraction_routes.page_manifest(units, len(_PAGES), parser, version), "issues": [],
               "review_status": "not_reviewed", "original_changed": False, "run_path": str(run), "parser": parser,
               "parser_version": version, "parser_components": {}, "children": []}
    path = _put(run / "extraction.json", (canonical(receipt) + "\n").encode())
    adapter = ExtractionLedgerAdapter(database=p.processing / "ledger.sqlite", evidence_root=p.processing / "artifacts" / "evidence")
    adapter.enroll(original_path=source, receipt_path=path, receipt_sha256=sha(path.read_bytes()))


def _seed_project(p):
    p.processing.mkdir(mode=0o700, parents=True)
    database = p.processing / "ledger.sqlite"
    store.initialize(database)
    (p.processing / "artifacts").mkdir(mode=0o700)
    source = _put(p.project / "original-1", _original_bytes())
    subject = _digest()
    with store.ledger(database) as con:
        con.execute("INSERT INTO runs VALUES('stage-16','stage-16','2026-11-02',NULL,'stage-16',NULL,NULL,NULL,'stage-16','{}')")
        con.execute("INSERT INTO agencies VALUES(?,?,NULL,NULL)", (_AGENCY, _NAME))
        con.execute("INSERT INTO originals VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (subject, source.stat().st_size, None, None, "agency_original", "in_scope", str(source), "pending", None, "{}"))
        con.execute("INSERT INTO occurrences VALUES(?,?,?,?,?,?,?,?)", (subject, subject, "local", "synthetic", None, None, "stage-16", "{}"))
        con.execute("INSERT INTO joins VALUES(?,?,?,?,?,?,?,?)", (subject, subject, _AGENCY, None, "synthetic_production", "{}", "typed", None))
        for name in store.STAGES:
            con.execute("INSERT INTO stage_state VALUES(?,?,?,?,?,?,?,?)", (subject, name, "pending", None, "stage-16", "2026-11-02", "stage-16", None))
        con.commit()
    _enroll(p, source)
    status = law.load_package(_LAW)["status"]  # the shipped package's own status, so the evidence join sees no law mismatch
    ready = {"schema": analysis_ready.SCHEMA, "run_id": _RUN, "review_required": True, "inputs": {}, "agencies": [
        {"agency_id": _AGENCY, "request_id": _REQUEST, "records": [{"sha256": subject, "text_state": "text_reviewed"}],
         "comparison_rows": [], "gap_proposal_ids": [], "law_package": {"id": _LAW, "status": status, "revision": None}}]}
    items = {"schema": received_items.SCHEMA, "run_id": _RUN, "items": [
        {"original_sha256": subject, "agency_id": _AGENCY, "request_id": _REQUEST, "received_at": _RECEIVED,
         "received_at_source": "mail_received_at"}]}
    return [_put(p.outcomes / "analysis-ready.json", _json_bytes(ready)), _put(p.outcomes / "received-items.json", _json_bytes(items))]


def _seed_profile(p):
    for rel in analysis_config._flock_review().METHOD.values():  # present, never run: the DAG is not part of this stage
        _put(p.method / rel, b"")
    inputs = p.analysis / "inputs"
    profile = {"schema": analysis_config.SCHEMA, "records_agency_id": _AGENCY, "slug": _SLUG, "fact_agency": _NAME,
               "agency_name": _NAME, "portal": "records.example.invalid",
               "review": {"method_dir": str(p.method), "method_python": os.path.realpath(sys.executable),
                          "added_by": "synthetic-analysis"},
               "rules_intake": {"law_specs": [str(inputs / "law.spec.csv")], "policy_specs": [str(inputs / "policy.spec.csv")],
                                "extra_specs": [], "f2_law_map": str(inputs / "law.f2map.csv"), "sources": []},
               "compare_f3": {"applicability": "Synthetic agency; synthetic local policy applies"}}
    return _put(p.profile, _json_bytes(profile))


def _seed(p):
    marker = p.sandbox / SANDBOX_MARKER
    os.close(os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600))
    for folder in (p.analysis, p.tmp, p.export):  # an existing --out keeps analysis-export from holding the project
        folder.mkdir(mode=0o700)
    outputs = _seed_project(p)
    profile = _seed_profile(p)
    status, _, err = _call(analysis_export.main, ["--project", str(p.project), "--after", "-", "--max-runs", "1", "--out", str(p.export)])
    if (status, err.splitlines()) != (0, ["EXPORT_RUN %s analysis_ready=present records=1 exported=1" % _RUN, "EXPORT_OK runs=1"]):
        raise _StepFailed("FINDINGS_EXPORT_FAILED")
    return [marker, profile, *outputs, p.export / _RUN / "EXPORT.json"]


def _conflicting_copy(p):
    """The same run id with one member re-sent with other bytes; EXPORT.json is consistent, so only the retained copy differs."""
    files = analysis_bridge._load(p.export / _RUN)
    files["analysis-ready.json"] += b"\n"
    meta = json.loads(files["EXPORT.json"])
    meta["files"]["analysis-ready.json"] = hashlib.sha256(files["analysis-ready.json"]).hexdigest()
    files["EXPORT.json"] = (json.dumps(meta, sort_keys=True, indent=2) + "\n").encode()
    for name, data in files.items():
        _put(p.conflict / _RUN / name, data)


def _analysis_sync(p):
    root = str(p.analysis)
    if _call(analysis_bridge.sync_main, ["--root", root, "--receive", str(p.export)])[:2] != (0, "RECEIVE %s RECEIVED\n" % _RUN):
        raise _StepFailed("FINDINGS_RECEIVE_REFUSED")
    retained = p.analysis / ".analysis" / "runs" / _RUN
    kept = analysis_bridge._load(retained)
    _conflicting_copy(p)
    refused = _call(analysis_bridge.sync_main, ["--root", root, "--receive", str(p.conflict)])[:2]
    if refused != (1, "RECEIVE %s EXPORT_CONFLICT\n" % _RUN) or analysis_bridge._load(retained) != kept:
        raise _StepFailed("FINDINGS_CONFLICT_NOT_REFUSED")
    if _call(analysis_bridge.sync_main, ["--root", root])[:2] != (0, "SYNC %s %s SYNCED new_texts=1\nSYNC_DONE pending=0\n" % (_RUN, _AGENCY)):
        raise _StepFailed("FINDINGS_SYNC_PENDING")
    return [retained / "EXPORT.json", p.analysis / "agencies" / _SLUG / "review" / "inputs" / "SOURCES.csv", p.config, p.ledger]


def _analysis_result(p, config):
    """Stand-in for the finished review DAG: one unreviewed record fact and one PUBLIC-OK finding citing it."""
    facts_tool, compare, verify = (analysis_bridge._tool(name) for name in ("flock_facts", "flock_compare", "flock_verify"))
    fact = dict(_FACT, agency=_NAME, request=_REQUEST, source_sha256=_digest(), kind="record", effective_from="", effective_to="",
                added_by="synthetic-analysis", added_at=_RECEIVED[:10], status="unreviewed")
    fact["fact_id"] = facts_tool.fact_id(fact)
    finding = {"finding_id": _FINDING, "topic": _TOPIC, "claim_class": "synthetic", "rating": "synthetic", "claim": _CLAIM,
               "fact_ids": fact["fact_id"], "rule_ids": "", "policy_refs": ""}
    run = Path(config["compare"]["f3"]["store"]) / "agencies" / _SLUG / "runs" / config["review"]["run_date"]
    return fact, [_put(Path(config["compare"]["facts"]), facts_tool.serialize([fact])),
                  _put(run / "FINDINGS.csv", _csv_bytes(compare.HEADER, [finding])),
                  _put(run / "PUBLIC-OK.csv", _csv_bytes(verify.PUBLIC, [finding]))]


def _outbox_body(fact):
    """The analysis-packet body the host stages for the finding once it is reviewed (the shape analysis_bridge writes)."""
    digest = _digest()
    packet = {"kind": "finding", "agency_key": _AGENCY, "request_id": _REQUEST,
              "source": "fr-" + hashlib.sha256((_SLUG + "\x1f" + _FINDING).encode()).hexdigest()[:24], "title": _TOPIC,
              "text": _CLAIM, "finding_state": public_packets.PUBLIC_STATE,
              "documents": [{"sha256": digest, "date_received": _RECEIVED[:10], "source_request": _REQUEST, "redaction_ids": [],
                             "use_restriction": False}],
              "citations": [{"ref": "record %s page %s (%s)" % (digest[:12], fact["page"], fact["fact_id"]), "verified": True}]}
    provenance = {"finding_id": _FINDING, "fact_ids": [fact["fact_id"]], "rule_ids": [], "withheld_rule_ids": [],
                  "withheld_policy_refs": [], "documents": [{"sha256": digest, "run_id": _RUN, "received_at": _RECEIVED,
                                                              "received_at_source": "mail_received_at"}]}
    body = {"schema": analysis_packets.SCHEMA, "agency_id": _AGENCY, "slug": _SLUG, "packets": [{"packet": packet, "provenance": provenance}],
            "held": []}
    return dict(body, key=hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest())


def _packet_import(p):
    fact, written = _analysis_result(p, json.loads(p.config.read_text(encoding="utf-8")))
    body = _outbox_body(fact)
    name = "%s-%s.json" % (_SLUG, body["key"][:12])
    staged = _put(p.outbox / name, (json.dumps(body, indent=1, sort_keys=True) + "\n").encode())
    status, out, _ = _call(analysis_packets.main, ["--project", str(p.project), "--from", str(p.outbox)])
    if (status, out.splitlines()[-1:]) != (0, ["analysis-packets-import: proposed=1 invalid=0"]):
        raise _StepFailed("FINDINGS_IMPORT_REFUSED")
    sent = p.outbox / "sent" / name  # as run-analysis.sh moves an imported outbox file
    sent.parent.mkdir(mode=0o700)
    os.replace(staged, sent)
    return [*written, sent, p.journal, p.receipt]


def _packet_state(p):
    """Exactly one finding packet, proposed at revision 0, unsigned; switch OFF; no site-change file; one journal event."""
    project = str(p.project)
    rows = public_packets.pending(project, read_only=True)
    state = public_packets.status(project, read_only=True)
    if len(rows) != 1 or len(state["packets"]) != 1:
        raise _StepFailed("FINDINGS_PACKET_COUNT")
    (row,), (view,) = rows, state["packets"]
    (sent,) = (p.outbox / "sent").glob("*.json")
    packet = json.loads(sent.read_text(encoding="utf-8"))["packets"][0]["packet"]
    if ((row["id"], row["kind"], row["agency_key"], row["request_id"], row["source"], row["revision"])
            != (public_packets.packet_id(packet), "finding", _AGENCY, _REQUEST, packet["source"], 0)):
        raise _StepFailed("FINDINGS_PACKET_IDENTITY")
    if row["status"] != "proposed" or view["status"] != "proposed":
        raise _StepFailed("FINDINGS_PACKET_NOT_PENDING")
    if row["signed_by"] is not None or row["approved_at"] is not None or "signed_by" in view or "file" in view:
        raise _StepFailed("FINDINGS_PACKET_SIGNED")
    if state["switch"] != "OFF":
        raise _StepFailed("FINDINGS_SWITCH_NOT_OFF")
    if os.path.lexists(p.project / public_packets.OUT):
        raise _StepFailed("FINDINGS_SITE_CHANGE_WRITTEN")
    events = [json.loads(line) for line in p.journal.read_bytes().splitlines()]
    receipts = [json.loads(line) for line in p.receipt.read_bytes().splitlines()]
    if ([e["event"] for e in events] != ["packet"] or events[0]["id"] != row["id"]
            or [r["result"] for r in receipts] != ["proposed r0"]):
        raise _StepFailed("FINDINGS_JOURNAL_UNEXPECTED")
    return [p.journal, p.receipt]


class _Handler:
    """The request surface the console page handlers use; read-only (no token, no decisions journal)."""

    def __init__(self, path, project, analysis_root):
        self.path, self.pin, self.sent = path, None, []
        self.options = {"project": project, "source": None}
        self.server = SimpleNamespace(analysis_root=analysis_root, decisions_dir=None)

    def csrf_token(self):
        return None

    def _send(self, status, body, **_):
        self.sent.append((status, body))

    def _fail(self, status, *_):
        self.sent.append((status, None))


def _tree(sandbox):
    return {str(path.relative_to(sandbox)): path.read_bytes() for path in sorted(sandbox.rglob("*")) if path.is_file()}


def _console_findings(p):
    """(a) /packets lists the one pending packet, switch OFF, no form; (b) /findings lists the source finding, not the packet."""
    before = _tree(p.sandbox)
    project, root = str(p.project), os.path.realpath(p.analysis)
    (row,) = public_packets.pending(project, read_only=True)
    packets = _Handler("/packets", project, root)
    if not console_packets.serve(packets) or len(packets.sent) != 1:
        raise _StepFailed("FINDINGS_PACKETS_PAGE")
    status, page = packets.sent[0]
    if (status != 200 or page.count("<section>") != 1 or "Packets switch: OFF." not in page or "<form" in page
            or "<dd>%s</dd>" % row["id"] not in page or "<dt>status</dt><dd>proposed</dd>" not in page):
        raise _StepFailed("FINDINGS_PACKETS_PAGE")
    model = console_findings.load_all(root)
    listed = [(r.get("agency"), r.get("finding_id"), r.get("topic"), r.get("review_state"), r.get("hold_code"))
              for r in model.get("rows", [])]
    findings = _Handler("/findings", project, root)
    if (model.get("state"), model.get("broken"), listed) != ("ok", 0, [(_SLUG, _FINDING, _TOPIC, "unreviewed", _HOLD)]):
        raise _StepFailed("FINDINGS_FINDINGS_PAGE")
    if not console_page_findings.serve(findings) or len(findings.sent) != 1:
        raise _StepFailed("FINDINGS_FINDINGS_PAGE")
    status, page = findings.sent[0]
    if status != 200 or "1 finding in every review state." not in page or _FINDING not in page or row["id"] in page:
        raise _StepFailed("FINDINGS_FINDINGS_PAGE")
    if _tree(p.sandbox) != before or os.path.lexists(p.project / public_packets.OUT):
        raise _StepFailed("FINDINGS_CONSOLE_WROTE")
    return [p.journal]


def run(sandbox):
    """Write synthetic fixtures and return path/hash evidence, never record or finding text."""
    sandbox = _sandbox_path(sandbox)
    _require_empty(sandbox)
    p = _paths(sandbox)
    result = {"stages": list(STAGES), "steps": [], "manual_steps": [], "codes": []}
    saved = tempfile.tempdir
    tempfile.tempdir = str(p.tmp)  # analysis_config's flock_review --check copy stays inside the sandbox
    try:
        for name, action in (("seed", _seed), ("analysis_sync", _analysis_sync), ("packet_import", _packet_import),
                             ("packet_state", _packet_state), ("console_findings", _console_findings)):
            step = {"name": name, "code": "OK", "outputs": []}
            try:
                step["outputs"] = [{"path": path.relative_to(sandbox).as_posix(),
                                    "sha256": hashlib.sha256(path.read_bytes()).hexdigest()} for path in action(p)]
            except _StepFailed as error:
                step["code"] = error.args[0]
            except (OSError, ValueError, KeyError, TypeError, sqlite3.Error,
                    public_packets.PacketError, finding_review.FindingReviewError):
                # Do not print a dependency exception: it may carry record text or private paths.
                step["code"] = "FINDINGS_" + name.upper() + "_ERROR"
            result["steps"].append(step)
            if step["code"] != "OK":
                result["codes"].append(step["code"])
                break
    finally:
        tempfile.tempdir = saved
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sandbox", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        sandbox = _sandbox_path(args.sandbox)
        sandbox.mkdir(mode=0o700, exist_ok=True)
        _require_empty(sandbox)
    except (OSError, ValueError):
        parser.error("--sandbox must be an absent path or an empty private directory")
    result = run(sandbox)
    print(json.dumps(result, sort_keys=True))
    return int(bool(result["codes"] or result["manual_steps"]))


if __name__ == "__main__":
    raise SystemExit(main())
