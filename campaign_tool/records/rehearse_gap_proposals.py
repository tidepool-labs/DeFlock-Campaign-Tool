"""Stage 11: one synthetic denial becomes a reviewable gap proposal, and only the denial that lacks a provision does.

The real e2e-proof replay carries one fictional denial (a text-layer PDF that names no Public Records Act section) for one
seeded request through every records stage, including the real outputs stage that writes ``received-items.json`` and
``gap-proposals.json``. The steps compare identifiers and fixed fields (run id, agency, request, item id, sha256, type,
page, quote presence), never prose. A control replay carries the same denial with a cited provision: it must yield no
proposal, so the proposal comes from the text and not merely from the kind. Every proposal stays a question for a human
reviewer (never a legal finding, never publishable). Nothing is sent or published and this driver queues no draft; the
approval journals are read only to prove it.
"""
import argparse
import hashlib
import json
import os
import stat
from datetime import datetime, timezone
from pathlib import Path

from . import e2e_proof
from .outcomes import gap_proposals, received_items
from .rehearse_received import AGENCY, DRAFT_KINDS, REQUEST, _JOURNAL, _PROJECT, _SEND_LOG, _STRAY, _Failed, _Rehearsal, _load, _need
from .run_stages import SANDBOX_MARKER

STAGES = ("11",)
TITLE = "A denial without a cited provision becomes one reviewable gap proposal, and a cited denial does not"
STEP_NAMES = ("seed_denial", "received_denial", "proposal_written", "proposal_is_review_only", "cited_denial_proposes_nothing",
              "no_sends")
PROPOSAL_TYPE = "exemption_without_citation"
ALLOWED_DRAFT = "appeal-denial"
CITED_LINE = "This denial relies on Government Code section 7923.600."


def denial_pdf(sender, cited):
    """The fictional denial as a one-page text-layer PDF built in memory; it classifies as a ``denial``."""
    lines = (sender, "Your public records request is denied.") + ((CITED_LINE,) if cited else ())
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
    xref = len(out)
    out += b"xref\n0 6\n0000000000 65535 f \n" + b"".join(b"%010d 00000 n \n" % o for o in offsets)
    return bytes(out + b"trailer\n<< /Size 6 /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % xref)


def _proposals(document):
    return [p for a in document["agencies"] for r in a["requests"] for p in r["proposals"]]


class _GapRehearsal(_Rehearsal):
    def __init__(self, sandbox, now):
        super().__init__(sandbox, now)
        self.pdf = denial_pdf("Fictional Synthetic Valley Records Unit", False)
        self.item_sha = hashlib.sha256(self.pdf).hexdigest()

    def _gap_doc(self, run_dir, report, code):
        path = run_dir / "outcomes" / gap_proposals.OUTPUT
        document = _load(path, code + "_NOT_STRICT_JSON")
        _need(document.get("schema") == gap_proposals.SCHEMA and type(document.get("agencies")) is list, code + "_SCHEMA")
        _need(document.get("run_id") == report["run_id"], code + "_RUN_MISMATCH")
        return path, document

    # ---- the steps ---------------------------------------------------------------------------------------------

    def seed_denial(self):
        self.report, _, files = self._replay("replay", self.pdf, True, "REPLAY")
        return [*files, self.sandbox / "replay" / "proof-report.json", self.sandbox / _PROJECT / "run-report.json"]

    def received_denial(self):
        path = self._run_dir() / "outcomes" / "received-items.json"
        paths = self._doc("items", path, received_items.SCHEMA, "items", "RECEIVED_ITEMS")
        items = self.docs["items"]["items"]
        found = [i for i in items if i.get("original_sha256") == self.item_sha]
        _need(len(found) == 1 and len({i["item_id"] for i in items}) == len(items), "RECEIVED_ITEM_NOT_ONCE")
        _need((found[0]["agency_id"], found[0]["request_id"]) == (AGENCY, REQUEST), "RECEIVED_ITEM_UNTIED")
        _need(found[0]["kind"] == "denial", "RECEIVED_ITEM_KIND_WRONG")
        self.item = found[0]
        return paths

    def proposal_written(self):
        path, document = self._gap_doc(self._run_dir(), self.report, "GAP_PROPOSALS")
        self.docs["gap"] = document
        # The proposals must be built from this run's received-items file, byte for byte as the outputs stage wrote it.
        _need(document.get("received_items_sha256") == hashlib.sha256((gap_proposals._canon(self.docs["items"]) + "\n").encode()).hexdigest(),
              "GAP_PROPOSALS_INPUT_STALE")
        made = [p for p in _proposals(document) if p.get("type") == PROPOSAL_TYPE]
        _need(len(made) == 1, "GAP_PROPOSAL_NOT_ONCE")
        _need(len(_proposals(document)) == 1, "GAP_PROPOSAL_UNEXPECTED")
        proposal = made[0]
        _need((proposal.get("agency_id"), proposal.get("request_id"), proposal.get("item_id")) ==
              (AGENCY, REQUEST, self.item["item_id"]), "GAP_PROPOSAL_UNTIED")
        _need(type(proposal.get("page")) is int and proposal.get("quote") and proposal.get("question"), "GAP_PROPOSAL_UNCITED")
        _need(proposal["proposal_id"] in {p["proposal_id"] for p in _proposals(document)}, "GAP_PROPOSAL_ID_MISSING")
        _need(document["counts"].get(PROPOSAL_TYPE) == 1, "GAP_COUNTS_DISAGREE")
        self.proposal = proposal
        return [path]

    def proposal_is_review_only(self):
        document, proposal = self.docs["gap"], self.proposal
        _need(document.get("publishable") is False and document.get("legal_conclusion") is False
              and document.get("review_required") is True, "GAP_DOCUMENT_NOT_REVIEW_ONLY")
        _need(proposal.get("review_required") is True and proposal.get("legal_conclusion") is False, "GAP_PROPOSAL_NOT_REVIEW_ONLY")
        _need(all(s.get("source") == "deterministic" for s in [proposal["score"]]), "GAP_SCORER_USED")  # no model in a sandbox
        _need(not document.get("diagnostics"), "GAP_DIAGNOSTICS_PRESENT")
        _need(document["unlinked_items"] == 0, "GAP_ITEM_UNLINKED")
        return []

    def cited_denial_proposes_nothing(self):
        pdf = denial_pdf("Fictional Synthetic Valley Records Unit", True)
        self.item_sha_cited = hashlib.sha256(pdf).hexdigest()
        _need(self.item_sha_cited != self.item_sha, "CONTROL_NOT_DISTINCT")
        report, run_dir, files = self._replay("stray", pdf, True, "CONTROL_REPLAY")
        items = _load(run_dir / "outcomes" / "received-items.json", "CONTROL_NOT_STRICT_JSON")
        mine = [i for i in items.get("items", []) if i.get("original_sha256") == self.item_sha_cited]
        _need(len(mine) == 1 and mine[0].get("kind") == "denial", "CONTROL_ITEM_NOT_DENIAL")
        path, document = self._gap_doc(run_dir, report, "CONTROL_GAP_PROPOSALS")
        _need(not [p for p in _proposals(document) if p.get("type") == PROPOSAL_TYPE], "CONTROL_PROPOSED")
        _need(document["counts"].get(PROPOSAL_TYPE, 0) == 0, "CONTROL_COUNTS_DISAGREE")
        return [*files, run_dir / "outcomes" / "received-items.json", path]

    def no_sends(self):
        # A denial legitimately queues ONE not-sent appeal draft (the stage 13 reply draft); stage 11 adds no draft of its
        # own, so the journals may hold that single revision-0 draft and nothing else: no send, no next-step draft, no reminder.
        paths = []
        for project in (self.sandbox / _PROJECT, self.sandbox / _STRAY):
            _need(not os.path.lexists(project / _SEND_LOG), "SEND_RECORDED")
            _need(os.path.lexists(project / _JOURNAL), "DENIAL_DRAFT_MISSING")
            paths.append(project / _JOURNAL)
            events = [json.loads(line) for line in (project / _JOURNAL).read_bytes().splitlines()]
            _need(all(e.get("event") not in ("sent", "send") for e in events), "SEND_RECORDED")
            _need([(e.get("event"), e.get("revision"), (e.get("snapshot") or {}).get("kind")) for e in events]
                  == [("draft", 0, ALLOWED_DRAFT)], "DRAFT_QUEUED")
            _need(ALLOWED_DRAFT in DRAFT_KINDS, "DRAFT_KIND_UNKNOWN")
        return paths


def run(sandbox, *, now=None):
    """Rehearse stage 11 in an empty 0700 ``sandbox``; return step codes and output path/sha256 pairs."""
    if not stat.S_ISDIR(sandbox.lstat().st_mode) or stat.S_IMODE(sandbox.lstat().st_mode) != 0o700 or any(sandbox.iterdir()):
        raise ValueError("sandbox must be an empty mode-0700 directory")
    sandbox = sandbox.resolve()
    (sandbox / SANDBOX_MARKER).touch(mode=0o600, exist_ok=False)
    rehearsal = _GapRehearsal(sandbox, (now or datetime.now(timezone.utc)).astimezone(timezone.utc))
    result = {"stages": list(STAGES), "steps": [], "manual_steps": [], "codes": []}
    for name in STEP_NAMES:
        step = {"name": name, "code": "OK", "outputs": []}
        try:
            step["outputs"] = [{"path": p.relative_to(sandbox).as_posix(), "sha256": hashlib.sha256(p.read_bytes()).hexdigest()}
                               for p in dict.fromkeys(getattr(rehearsal, name)())]
        except _Failed as error:
            step["code"] = error.args[0]
        except (OSError, ValueError, KeyError, TypeError, AttributeError, IndexError, StopIteration, e2e_proof.ProofError):
            step["code"] = name.upper() + "_ERROR"  # never print a dependency exception: it may carry private paths
        result["steps"].append(step)
        if step["code"] != "OK":
            result["codes"].append(step["code"])
            break
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=TITLE)
    parser.add_argument("--sandbox", required=True, type=Path)
    sandbox = parser.parse_args(argv).sandbox
    if sandbox.is_symlink() or (sandbox.exists() and (not sandbox.is_dir() or any(sandbox.iterdir()))):
        parser.error("--sandbox requires a new or empty directory")
    sandbox.mkdir(mode=0o700, exist_ok=True)
    sandbox.chmod(0o700)
    result = run(sandbox)
    print(json.dumps(result, sort_keys=True))
    return int(bool(result["codes"] or result["manual_steps"]))


if __name__ == "__main__":
    raise SystemExit(main())
