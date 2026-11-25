"""Stage 10: rehearse the policy comparison on two fictional agencies, in a sandbox.

One real ``e2e_proof.replay`` saves and extracts two one-page policies from a synthetic message. The sandbox seed
binds each attachment digest to its own agency and request, and separately supplies cited comparison inputs.
The native writers produce both exhibits; the driver checks that they cite the same policy page for each agency.
A second replay with one agency is the control: no peer may appear.
Nothing is sent or published. Output is review-required evidence, not a legal conclusion.
"""
import argparse
import hashlib
import json
import os
import sqlite3
import stat
from email.message import EmailMessage
from email.policy import SMTP
from pathlib import Path

from . import e2e_proof, run_stages

STAGES = ("10",)
TITLE = "Policy comparison: two synthetic agency policies, cited rows, no invented peer"

# id, name, policy lines, retention length documented, sharing restrictions documented (custodian-style facts).
_AGENCIES = (
    ("ca-valley-pd", "Fictional Valley Police Department",
     ("Fictional Valley ALPR Policy", "Retention: plate reads are deleted after 30 days.",
      "Sharing: plate reads are shared with any outside agency that asks."), True, False),
    ("ca-harbor-pd", "Fictional Harbor Police Department",
     ("Fictional Harbor ALPR Policy", "Retention: plate reads are kept until storage is needed.",
      "Sharing: plate reads are shared only with named county agencies for named purposes."), False, True),
)
_SHA = "sha256:"


class _Refused(Exception):
    """A check failed; the sole argument is a fixed UPPER_SNAKE evidence code."""


def _need(condition, code):
    if not condition:
        raise _Refused(code)


def _pdf(lines):
    stream = ("BT /F1 12 Tf 72 720 Td 16 TL " + " T* ".join("(%s) Tj" % line for line in lines) + " ET").encode()
    objects = [b"<< /Type /Catalog /Pages 2 0 R >>", b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
               b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
               b"/Resources << /Font << /F1 5 0 R >> >> >>",
               b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream",
               b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    out, offsets = bytearray(b"%PDF-1.4\n"), []
    for number, body in enumerate(objects, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % number + body + b"\nendobj\n"
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    out += b"".join(b"%010d 00000 n \n" % offset for offset in offsets)
    return bytes(out + b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n"
                 % (len(objects) + 1, out.index(b"xref")))


def _fact(document, flags):
    return {"state": "observed", "applicability_confirmed": True, "value": flags,
            "citations": [{"source_id": _SHA + document, "locator": "page 1"}]}


def _inputs(base, agencies):
    """Write the synthetic message, seed and campaign content for ``agencies``; return {agency_id: document sha256}."""
    documents = {a[0]: _pdf(a[2]) for a in agencies}
    message = EmailMessage()
    message["From"], message["To"] = "records@fictional-valley.example.invalid", "requests@campaign.example.invalid"
    message["Subject"], message["Date"] = "Policies for the fictional agencies", "Tue, 15 Sep 2026 10:00:00 -0700"
    message["Message-ID"] = "<synthetic-policies@fictional-valley.example.invalid>"
    message.set_content("The responsive policies are attached.\n")
    for agency_id, raw in documents.items():
        message.add_attachment(raw, maintype="application", subtype="pdf", filename=agency_id + ".pdf")
    digests = {agency_id: hashlib.sha256(raw).hexdigest() for agency_id, raw in documents.items()}
    sheets = [{"agency_id": i, "name": name, "jurisdiction": "us-ca", "regulated_roles": ["operator"],
               "requests": [{"request_id": i + "-req"}],
               "facts": {"retention": _fact(digests[i], {"data_layer": "plate_reads", "length_documented": retention,
                                                         "destruction_process_documented": retention}),
                         "sharing_limits": _fact(digests[i], {"purposes_documented": True, "process_documented": True,
                                                              "restrictions_documented": sharing})}}
              for i, name, _, retention, sharing in agencies]
    seed = {"schema": e2e_proof.SEED_SCHEMA,
            "agencies": [{"id": i, "name": name, "jurisdiction": "us-ca"} for i, name, *_ in agencies],
            "requests": [{"id": i + "-req", "agency_id": i, "external_ref": i + "-1", "submitted_at": "2026-09-01",
                          "status": "open"} for i, *_ in agencies],
            "document_joins": [{"document_sha256": digest, "agency_id": agency, "request_id": agency + "-req"}
                               for agency, digest in digests.items()],
            "fact_sheets": {"schema": "records-agency-fact-sheets-v1", "agencies": sheets}}
    content = base / "campaign" / "content"
    content.mkdir(parents=True)
    (content / "agencies.json").write_text(json.dumps([
        {"agency_id": i, "name": name, "profile": {"status": "Records requested", "summary": "Fictional agency."}}
        for i, name, *_ in agencies]), encoding="utf-8")
    (base / "message.eml").write_bytes(message.as_bytes(policy=SMTP))
    (base / "seed.json").write_text(json.dumps(seed), encoding="utf-8")
    return digests


class _Run:
    """One real replay: its report, project and the output directory the outputs stage wrote."""

    def __init__(self, sandbox, label, agencies):
        base = sandbox / "inputs" / label
        self.documents, self.ids = _inputs(base, agencies), [a[0] for a in agencies]
        umask = os.umask(0o077)  # replay sets a process-wide umask and does not restore it
        try:
            self.report = e2e_proof.replay(base / "message.eml", sandbox / "proof", base / "campaign", label=label,
                                           seed=base / "seed.json")
        finally:
            os.umask(umask)
        self.root = sandbox / "proof" / label
        self.project = self.root / "project"
        self.directory = self.project / "run-outputs" / self.report["run_id"]

    def stage(self, name):
        return next((s for s in self.report["stages"] if s["stage"] == name), {})

    def query(self, sql):
        db = sqlite3.connect("file:%s?mode=ro" % (self.project / "processing-v1" / "ledger.sqlite"), uri=True)
        try:
            return db.execute(sql).fetchall()
        finally:
            db.close()


def _strict(path, code):
    """Strict JSON object from ``path``: no NaN/Infinity, no duplicate keys, nothing but a mapping."""
    def unique(pairs):
        _need(len({key for key, _ in pairs}) == len(pairs), code)
        return dict(pairs)

    def refuse(constant):
        raise _Refused(code)

    try:
        value = json.loads(path.read_bytes(), object_pairs_hook=unique, parse_constant=refuse)
    except (OSError, ValueError):
        raise _Refused(code) from None
    _need(type(value) is dict, code)
    return value


def _comparison(run, code):
    stage = run.stage("outputs")
    _need(stage.get("status") == "ok", code)
    report = _strict(run.directory / "comparison.json", code)
    _need(report.get("schema") == "records-agency-comparison-v1" and report.get("run_id") == run.report["run_id"]
          and report.get("input_status") == "fact_sheets_supplied" and report.get("review_required") is True
          and report.get("legal_conclusion") is False and type(report.get("rows")) is list
          and all(type(row) is dict for row in report["rows"]), code)
    return report


def _differing(rows, ids):
    """Fields on which every agency in ``ids`` has a decided row (meets/gap) and the decisions differ."""
    decided = {}
    for row in rows:
        if row.get("status") in ("meets", "gap"):
            decided.setdefault(row.get("field"), {})[row.get("agency_id")] = row["status"]
    return sorted(field for field, by in decided.items() if set(by) == set(ids) and len(set(by.values())) > 1)


def _save_mail(ctx):
    run = ctx["pair"] = _Run(ctx["sandbox"], "pair", _AGENCIES)
    _need(run.stage("save_mail").get("status") == "ok", "SAVE_MAIL_NOT_OK")
    held = run.query("SELECT COUNT(*) FROM mail_messages")[0][0], run.query("SELECT COUNT(*) FROM originals")[0][0]
    _need(held == (1, 1 + len(_AGENCIES)), "SAVE_MAIL_NOT_OK")
    _need(run.stage("seed").get("status") == "ok", "FIXTURE_SEED_REFUSED")
    return [run.root / "inbox" / "00.eml"]


def _extract_text(ctx):
    run = ctx["pair"]
    stage = run.stage("extract")
    outcomes = {o.get("code"): o.get("count") for o in (stage.get("detail") or {}).get("outcomes") or []}
    _need(stage.get("status") == "ok" and outcomes == {"EXTRACTED": 1 + len(_AGENCIES)}, "EXTRACT_TEXT_INCOMPLETE")
    return [run.root / "proof-report.json"]


def _write_factsheets(ctx):
    run = ctx["pair"]
    path = run.directory / "agency-factsheets.json"
    sheets = _strict(path, "FACTSHEETS_INVALID")
    _need(sheets.get("schema") == "records-agency-factsheets-v1" and sheets.get("run_id") == run.report["run_id"]
          and sheets.get("review_required") is True and sheets.get("semantic_acceptance") is False
          and sheets.get("publication_ready") is False and type(sheets.get("agencies")) is list, "FACTSHEETS_INVALID")
    by_id = {a.get("agency_id"): a for a in sheets["agencies"] if type(a) is dict}
    _need(len(sheets["agencies"]) == len(run.ids) and set(by_id) == set(run.ids), "FACTSHEET_AGENCY_MISSING")
    joins = run.query("SELECT original_sha256, agency_id, request_id FROM joins WHERE status='typed'")
    expected = [(digest, agency, agency + "-req") for agency, digest in run.documents.items()]
    _need(len(joins) == len(expected) and set(joins) == set(expected), "FIXTURE_JOIN_MISMATCH")
    for agency, digest in run.documents.items():
        sheet = by_id[agency]
        _need(sheet.get("document_sha256s") == [digest], "FACTSHEET_NOT_FROM_DOCUMENT")
        for field in ("retention", "outside_sharing"):
            citations = [s["citation"] for s in sheet["fields"][field]["statements"]]
            _need(citations and all(c.get("document_sha256") == digest and type(c.get("page")) is int
                                    and c["page"] == 1 for c in citations), "FACTSHEET_DOCUMENT_PAGE_MISMATCH")
    return [path]


def _write_comparison(ctx):
    run = ctx["pair"]
    report = _comparison(run, "COMPARISON_JSON_INVALID")
    _need(report.get("agency_count") == len(_AGENCIES), "COMPARISON_JSON_INVALID")
    _need(_differing(report["rows"], run.ids) == ["retention", "sharing_limits"], "COMPARISON_PAIR_MISSING")
    return [run.directory / "comparison.json"]


def _comparison_rows_cite_facts(ctx):
    run = ctx["pair"]
    known = {row[0] for row in run.query("SELECT sha256 FROM originals")}
    rows = _comparison(run, "COMPARISON_JSON_INVALID")["rows"]
    seen = set()
    for row in rows:
        if row.get("status") not in ("meets", "gap") and "observed" not in row:
            continue
        evidence = row.get("evidence")
        _need(type(evidence) is list and evidence, "COMPARISON_ROW_UNCITED")
        for cite in evidence:
            if type(cite) is not dict:
                raise _Refused("COMPARISON_ROW_UNCITED")
            source = cite.get("source_id")
            _need(type(source) is str and source.startswith(_SHA)
                  and type(cite.get("locator")) is str and cite["locator"].strip(), "COMPARISON_ROW_UNCITED")
            _need(source[len(_SHA):] in known, "COMPARISON_CITES_OUTSIDE_SANDBOX")
            _need(source[len(_SHA):] == run.documents.get(row.get("agency_id")), "COMPARISON_CITES_OTHER_AGENCY")
            _need(cite["locator"] == "page 1", "COMPARISON_DOCUMENT_PAGE_MISMATCH")
        seen.add(row.get("agency_id"))
    _need(seen == set(run.ids), "COMPARISON_ROW_UNCITED")
    return [run.directory / "comparison.json"]


def _single_agency_is_not_a_comparison(ctx):
    solo = ctx["solo"] = _Run(ctx["sandbox"], "single", _AGENCIES[:1])
    report = _comparison(solo, "SINGLE_AGENCY_COMPARISON_INVALID")
    peer = _AGENCIES[1][0]
    texts = [(solo.directory / name).read_text(encoding="utf-8") for name in ("comparison.json", "comparison.md")]
    _need(report.get("agency_count") == 1 and {r.get("agency_id") for r in report["rows"]} == set(solo.ids)
          and not _differing(report["rows"], solo.ids + [peer]) and not any(peer in text for text in texts),
          "SINGLE_AGENCY_PEER_INVENTED")
    return [solo.directory / "comparison.json"]


def _no_sends(ctx):
    runs = (ctx["pair"], ctx["solo"])
    for run in runs:
        details = [(s.get("detail") or {}).get("sent") for s in run.report["stages"]]
        queued = [p for p in run.project.rglob("*") if "outbox" in p.name and (p.is_file() or any(p.iterdir()))]
        _need(run.report["sent"] is False and run.report["published"] is False
              and all(sent in (None, False) for sent in details) and not queued, "SEND_OR_PUBLISH_DETECTED")
    return [ctx["pair"].root / "telegram-summary.txt"]


_STEPS = (("save_mail", _save_mail), ("extract_text", _extract_text), ("write_factsheets", _write_factsheets),
          ("write_comparison", _write_comparison), ("comparison_rows_cite_facts", _comparison_rows_cite_facts),
          ("single_agency_is_not_a_comparison", _single_agency_is_not_a_comparison), ("no_sends", _no_sends))


def run(sandbox):
    """Run the steps in an existing empty 0700 directory, stopping at the first failed step."""
    info = sandbox.lstat()
    if not stat.S_ISDIR(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o700 or any(sandbox.iterdir()):
        raise ValueError("sandbox must be an empty mode-0700 directory")
    sandbox = sandbox.resolve()
    (sandbox / run_stages.SANDBOX_MARKER).touch(mode=0o600, exist_ok=False)
    ctx = {"sandbox": sandbox}
    result = {"stages": list(STAGES), "steps": [], "manual_steps": [], "codes": []}
    for name, step in _STEPS:
        try:
            paths = step(ctx)
            code = "OK"
            outputs = [{"path": p.relative_to(sandbox).as_posix(), "sha256": hashlib.sha256(p.read_bytes()).hexdigest()}
                       for p in paths]
        except _Refused as refusal:
            code, outputs = refusal.args[0], []
        except (OSError, ValueError, KeyError, TypeError, sqlite3.Error, e2e_proof.ProofError):
            code, outputs = name.upper() + "_FAILED", []
        result["steps"].append({"name": name, "code": code, "outputs": outputs})
        if code != "OK":
            result["codes"].append(code)
            break
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=TITLE)
    parser.add_argument("--sandbox", required=True, type=Path)
    sandbox = parser.parse_args(argv).sandbox
    try:
        if sandbox.is_symlink():
            raise ValueError
        sandbox.mkdir(mode=0o700, exist_ok=True)
        if any(sandbox.iterdir()):
            raise ValueError
        sandbox.chmod(0o700)
    except (OSError, ValueError):
        parser.error("--sandbox requires a new or empty directory")
    result = run(sandbox)
    print(json.dumps(result, sort_keys=True))
    return int(any(step["code"] != "OK" for step in result["steps"]) or bool(result["manual_steps"]))


if __name__ == "__main__":
    raise SystemExit(main())
