"""Stage 12: one synthetic agency reply reaches the received items, the request register and the timeline.

The real e2e-proof replay carries one fictional acknowledgement (a text-layer PDF) for one seeded request through every
records stage, including the real outputs stage that writes ``received-items.json``, ``CURRENT-REQUEST-REGISTER.json``
and ``timeline.json``; the steps compare their identifiers (run id, agency, request, item id, sha256, stage, date), never
prose. A second, unseeded replay carries a message that matches no request: it must be listed as unmatched and attached
to no agency. Nothing is sent or published and this driver queues no draft; the approval journals are read only to prove it.
"""
import argparse
import contextlib
import hashlib
import io
import json
import os
import stat
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from email.policy import SMTP
from pathlib import Path

from . import e2e_proof, followup_clock
from .outcomes import received_items, register, timeline, unmatched
from .run_stages import SANDBOX_MARKER

STAGES = ("12",)
TITLE = "Received items, request register and timeline agree on one agency reply"
AGENCY, REQUEST = "ca-synthetic-valley-pd", "sv-req-1"
STEP_NAMES = ("seed_reply", "received_items", "register", "timeline", "outputs_agree",
              "unmatched_is_not_attached", "no_sends")
DRAFT_KINDS = (e2e_proof.RECEIVED_DRAFT_KINDS + followup_clock.REMINDERS + (followup_clock.NOTICE, "followup-playbook")
               + e2e_proof.NEXT_KINDS)
_PROJECT, _STRAY = Path("replay/project"), Path("stray/project")
_JOURNAL, _SEND_LOG = Path("approvals/queue.jsonl"), Path("approvals/mail-send.jsonl")


class _Failed(Exception):
    """A check failed; the sole argument is a fixed UPPER_SNAKE evidence code."""


def _need(condition, code):
    if not condition:
        raise _Failed(code)


def reply_pdf(sender):
    """The fictional acknowledgement as a one-page text-layer PDF built in memory; it classifies as a ``response``."""
    lines = (sender, "We have received your public records request.")
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


def _load(path, code):
    """Strict JSON object: UTF-8, no duplicate key and no NaN/Infinity; anything else is the named failure."""
    def pairs(items):
        _need(len({key for key, _ in items}) == len(items), code)
        return dict(items)
    try:
        document = json.loads(path.read_bytes().decode("utf-8"), object_pairs_hook=pairs,
                              parse_constant=lambda _: _need(False, code))
    except (OSError, ValueError, RecursionError):
        raise _Failed(code) from None
    _need(type(document) is dict, code)
    return document


class _Rehearsal:
    def __init__(self, sandbox, now):
        self.sandbox, self.now, self.report, self.docs = sandbox, now, {}, {}
        self.pdf = reply_pdf("Fictional Synthetic Valley Records Unit")
        self.item_sha = hashlib.sha256(self.pdf).hexdigest()

    def _replay(self, label, pdf, with_seed, code):
        inputs, campaign = self.sandbox / ("inputs-" + label), self.sandbox / ("campaign-" + label)
        for directory in (inputs, campaign, campaign / "content"):
            directory.mkdir(mode=0o700)
        message = EmailMessage()
        message["From"], message["To"] = "records@synthetic-valley.example.invalid", "requests@campaign.example.invalid"
        message["Subject"] = "Your records request " + REQUEST
        message["Date"] = (self.now - timedelta(days=1)).strftime("%a, %d %b %Y %H:%M:%S +0000")
        message["Message-ID"] = "<%s-%s@synthetic-valley.example.invalid>" % (label, REQUEST)
        message.set_content("The acknowledgement is attached.\n")
        message.add_attachment(pdf, maintype="application", subtype="pdf", filename="acknowledgement.pdf")
        name = "Synthetic Valley Police Department"
        eml, agencies, seed = inputs / "reply.eml", campaign / "content" / "agencies.json", inputs / "seed.json"
        try:
            # New 0600 files through the replay's own sandbox writer (it refuses an existing path), as rehearse_drafts does.
            e2e_proof._seed_file(eml, message.as_bytes(policy=SMTP))
            e2e_proof._seed_file(agencies, json.dumps([{"agency_id": AGENCY, "name": name, "profile": {
                "status": "Records requested", "summary": "Fictional."}}]).encode())
            if with_seed:
                e2e_proof._seed_file(seed, json.dumps({
                    "schema": e2e_proof.SEED_SCHEMA, "agencies": [{"id": AGENCY, "name": name, "jurisdiction": "us-ca"}],
                    "requests": [{"id": REQUEST, "agency_id": AGENCY, "status": "open",
                                  "submitted_at": (self.now - timedelta(days=3)).date().isoformat()}],
                    "join": {"agency_id": AGENCY, "request_id": REQUEST}}).encode())
            with contextlib.redirect_stdout(io.StringIO()):
                report = e2e_proof.replay(eml, self.sandbox, campaign, label=label, now=self.now, seed=seed if with_seed else None)
        except e2e_proof.ProofError as error:
            raise _Failed(code + "_" + error.code) from None
        row = next((s for s in report["stages"] if s["stage"] == "outputs"), None)
        _need(row is not None and row["status"] in ("ok", "warn"), code + "_OUTPUTS_NOT_RUN")
        _need(report["result"] == "PASS" and not report["manual_steps"], code)
        _need(report["sent"] is False and report["published"] is False, "SEND_RECORDED")
        run_dir = self.sandbox / label / "project" / "run-outputs" / report["run_id"]
        _need(run_dir.is_dir() and not run_dir.is_symlink(), code + "_RUN_OUTPUTS_MISSING")
        return report, run_dir, [p for p in (eml, agencies, seed) if p.exists()]

    def _run_dir(self):
        return self.sandbox / _PROJECT / "run-outputs" / self.report["run_id"]

    def _doc(self, key, path, schema, listed, code):
        doc = self.docs[key] = _load(path, code + "_NOT_STRICT_JSON")
        _need(doc.get("schema") == schema and type(doc.get(listed)) is list, code + "_SCHEMA")
        _need(key == "register" or doc.get("run_id") == self.report["run_id"], code + "_RUN_MISMATCH")
        return [path]

    # ---- the steps ---------------------------------------------------------------------------------------------

    def seed_reply(self):
        self.report, _, files = self._replay("replay", self.pdf, True, "REPLAY")
        return [*files, self.sandbox / "replay" / "proof-report.json", self.sandbox / _PROJECT / "run-report.json"]

    def received_items(self):
        path = self._run_dir() / "outcomes" / "received-items.json"
        paths = self._doc("items", path, received_items.SCHEMA, "items", "RECEIVED_ITEMS")
        items = self.docs["items"]["items"]
        found = [i for i in items if i.get("original_sha256") == self.item_sha]
        _need(found, "RECEIVED_ITEM_MISSING")
        _need(len(found) == 1 and len({i["item_id"] for i in items}) == len(items), "RECEIVED_ITEM_DUPLICATED")
        _need((found[0]["agency_id"], found[0]["request_id"]) == (AGENCY, REQUEST), "RECEIVED_ITEM_UNTIED")
        _need(found[0]["kind"] == "response", "RECEIVED_ITEM_KIND_WRONG")
        return paths

    def register(self):
        # The register file does not name its run: the run directory that holds it does, and only one may.
        held = sorted(p.parent.name for p in (self.sandbox / _PROJECT / "run-outputs").glob("*/CURRENT-REQUEST-REGISTER.json"))
        _need(held == [self.report["run_id"]], "REGISTER_RUN_MISMATCH")
        return self._doc("register", self._run_dir() / "CURRENT-REQUEST-REGISTER.json", register.SCHEMA, "requests", "REGISTER")

    def timeline(self):
        return self._doc("timeline", self._run_dir() / "outcomes" / "timeline.json", timeline.SCHEMA, "requests", "TIMELINE")

    def outputs_agree(self):
        item = next(i for i in self.docs["items"]["items"] if i["original_sha256"] == self.item_sha)
        rows = [r for r in self.docs["register"]["requests"] if (r.get("agency_key"), r.get("request_id")) == (AGENCY, REQUEST)]
        _need(len(rows) == 1 and self.docs["register"]["request_count"] == 1, "REGISTER_REQUEST_MISSING")
        _need(rows[0].get("status") == "open", "REGISTER_STATUS_WRONG")  # an acknowledgement answers nothing
        _need(self.item_sha in [d.get("original_sha256") for d in rows[0]["arrived"]], "REGISTER_REPLY_NOT_ATTACHED")
        lines = [r for r in self.docs["timeline"]["requests"] if (r.get("agency_key"), r.get("ledger_request_id")) == (AGENCY, REQUEST)]
        _need(len(lines) == 1 and self.docs["timeline"]["request_count"] == 1, "TIMELINE_REQUEST_MISSING")
        events = [e for e in lines[0]["events"] if (e.get("source") or {}).get("item_id") == item["item_id"]]
        _need(len(events) == 1, "OUTPUTS_ITEM_NOT_ONCE")
        _need(events[0].get("type") == "acknowledged" and lines[0].get("stage") == "acknowledged", "OUTPUTS_STAGE_DISAGREES")
        _need(events[0].get("date") == (item["received_at"] or "")[:10] != "", "OUTPUTS_REPLY_DATE_MISSING")
        _need(self.docs["timeline"]["unmatched"].get("item") == 0, "OUTPUTS_ITEM_UNMATCHED")
        path = self._run_dir() / "outcomes" / "unmatched-messages.json"
        listed = _load(path, "UNMATCHED_NOT_STRICT_JSON")
        _need(listed.get("run_id") == self.report["run_id"] and listed.get("messages") == [], "OUTPUTS_MATCHED_LISTED")
        return [path]

    def unmatched_is_not_attached(self):
        pdf = reply_pdf("Fictional Unknown Sender Unit")  # another original: its sha cannot be the matched reply's
        report, run_dir, files = self._replay("stray", pdf, False, "STRAY_REPLAY")
        outcomes = run_dir / "outcomes"
        items = _load(outcomes / "received-items.json", "UNMATCHED_NOT_STRICT_JSON")
        listed = _load(outcomes / "unmatched-messages.json", "UNMATCHED_NOT_STRICT_JSON")
        shas = {i["original_sha256"] for i in items["items"]}
        _need(hashlib.sha256(pdf).hexdigest() in shas, "UNMATCHED_ITEM_MISSING")
        _need(listed.get("schema") == unmatched.SCHEMA and listed.get("run_id") == report["run_id"], "UNMATCHED_SCHEMA")
        _need(all(i["agency_id"] is None and i["request_id"] is None for i in items["items"]), "UNMATCHED_ATTACHED")
        _need({m.get("message_sha256") for m in listed["messages"]} == shas, "UNMATCHED_NOT_LISTED")
        _need(not shas & {i["original_sha256"] for i in self.docs["items"]["items"]}, "UNMATCHED_ATTACHED")
        _need(_load(outcomes / "timeline.json", "UNMATCHED_NOT_STRICT_JSON")["requests"] == [], "UNMATCHED_ATTACHED")
        return [*files, outcomes / "received-items.json", outcomes / "unmatched-messages.json"]

    def no_sends(self):
        paths = []
        for project in (self.sandbox / _PROJECT, self.sandbox / _STRAY):
            _need(not os.path.lexists(project / _SEND_LOG), "SEND_RECORDED")
            if os.path.lexists(project / _JOURNAL):
                paths.append(project / _JOURNAL)
                for line in (project / _JOURNAL).read_bytes().splitlines():
                    event = json.loads(line)
                    _need(event.get("event") not in ("sent", "send"), "SEND_RECORDED")
                    _need((event.get("snapshot") or {}).get("kind") not in DRAFT_KINDS, "DRAFT_QUEUED")
        return paths


def run(sandbox, *, now=None):
    """Rehearse stage 12 in an empty 0700 ``sandbox``; return step codes and output path/sha256 pairs."""
    if not stat.S_ISDIR(sandbox.lstat().st_mode) or stat.S_IMODE(sandbox.lstat().st_mode) != 0o700 or any(sandbox.iterdir()):
        raise ValueError("sandbox must be an empty mode-0700 directory")
    sandbox = sandbox.resolve()
    (sandbox / SANDBOX_MARKER).touch(mode=0o600, exist_ok=False)
    rehearsal = _Rehearsal(sandbox, (now or datetime.now(timezone.utc)).astimezone(timezone.utc))
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
