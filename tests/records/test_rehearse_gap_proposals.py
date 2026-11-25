"""Stage 11 rehearsal driver: one real synthetic replay, then broken-input controls (nothing here sends or publishes)."""
import hashlib
import io
import itertools
import json
import os
import re
import shutil
import smtplib
import stat
import sys
import tempfile
import unittest
import urllib.request
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from campaign_tool.records import e2e_proof, rehearse, rehearsal_receipt, run_orchestrator, run_stages
from campaign_tool.records import rehearse_gap_proposals as driver
from campaign_tool.records.outcomes import gap_proposals

WRITE = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND
SPY = {"on": False, "writes": [], "sockets": []}
os_open = os.open
OUT, GAP = "outcomes/received-items.json", "outcomes/gap-proposals.json"


def spy_open(path, flags, mode=0o777, *, dir_fd=None):  # names opened relative to a directory fd show only here
    if SPY["on"] and flags & WRITE:
        base = os.readlink("/proc/self/fd/%d" % dir_fd) if dir_fd is not None else os.getcwd()
        SPY["writes"].append(os.path.join(base, os.fsdecode(path)))
    return os_open(path, flags, mode, dir_fd=dir_fd)


def _audit(event, args):  # an audit hook cannot be removed; it records only while SPY["on"] is set
    if SPY["on"] and event == "open" and args[2] & WRITE and isinstance(args[0], (str, bytes)) and os.path.isabs(args[0]):
        SPY["writes"].append(os.fsdecode(args[0]))
    elif SPY["on"] and event in ("socket.connect", "socket.bind"):
        SPY["sockets"].append((event, args[1]))
    elif SPY["on"] and event == "socket.getaddrinfo":
        SPY["sockets"].append((event, (args[0], args[1])))


def edit_json(name, change):  # ``change`` edits the parsed file, or returns the raw text to write instead
    def edit(run_dir):
        document = json.loads((run_dir / name).read_text())
        raw = change(document)
        (run_dir / name).write_text(raw if isinstance(raw, str) else json.dumps(document))
    return edit


def first(document):
    return document["agencies"][0]["requests"][0]["proposals"][0]


def set_first(**fields):
    return lambda document: first(document).update(fields)


def drop_proposals(document):
    document["agencies"][0]["requests"][0]["proposals"] = []


def duplicate(document):
    rows = document["agencies"][0]["requests"][0]["proposals"]
    rows.append(dict(rows[0], proposal_id="x" * 64))


FILE_BREAKS = (  # (code, the believable bug, edit of a copied run directory)
    ("GAP_PROPOSALS_NOT_STRICT_JSON", "writer emits NaN", edit_json(GAP, lambda d: d.update(x=float("nan")))),
    ("GAP_PROPOSALS_SCHEMA", "wrong schema", edit_json(GAP, lambda d: d.update(schema="x"))),
    ("GAP_PROPOSALS_RUN_MISMATCH", "stale run id", edit_json(GAP, lambda d: d.update(run_id="run_x"))),
    ("GAP_PROPOSALS_INPUT_STALE", "built from another received-items file", edit_json(GAP, lambda d: d.update(received_items_sha256="0" * 64))),
    ("GAP_PROPOSAL_NOT_ONCE", "no proposal for the denial", edit_json(GAP, drop_proposals)),
    ("GAP_PROPOSAL_NOT_ONCE", "a second proposal of the same type", edit_json(GAP, duplicate)),
    ("GAP_PROPOSAL_UNTIED", "proposal on another request", edit_json(GAP, set_first(request_id="other"))),
    ("GAP_PROPOSAL_UNTIED", "proposal on another item", edit_json(GAP, set_first(item_id="other"))),
    ("GAP_PROPOSAL_UNCITED", "no quote", edit_json(GAP, set_first(quote=""))),
    ("GAP_PROPOSAL_UNCITED", "no page", edit_json(GAP, set_first(page=None))),
    ("GAP_COUNTS_DISAGREE", "counts omit the proposal", edit_json(GAP, lambda d: d["counts"].update(exemption_without_citation=0))),
    ("GAP_DOCUMENT_NOT_REVIEW_ONLY", "document marked publishable", edit_json(GAP, lambda d: d.update(publishable=True))),
    ("GAP_DOCUMENT_NOT_REVIEW_ONLY", "document states a legal conclusion", edit_json(GAP, lambda d: d.update(legal_conclusion=True))),
    ("GAP_PROPOSAL_NOT_REVIEW_ONLY", "proposal states a legal conclusion", edit_json(GAP, set_first(legal_conclusion=True))),
    ("GAP_SCORER_USED", "a model scored it in the sandbox", edit_json(GAP, lambda d: first(d)["score"].update(source="model"))),
    ("GAP_DIAGNOSTICS_PRESENT", "a diagnostic row", edit_json(GAP, lambda d: d.update(diagnostics=[{"reason": "quote_or_page_missing"}]))),
    ("GAP_ITEM_UNLINKED", "an unlinked item", edit_json(GAP, lambda d: d.update(unlinked_items=1))),
    ("RECEIVED_ITEM_KIND_WRONG", "denial classified as a response",
     edit_json(OUT, lambda d: [i.update(kind="response") for i in d["items"]])),
    ("RECEIVED_ITEM_UNTIED", "denial not tied to its request", edit_json(OUT, lambda d: [i.update(request_id=None) for i in d["items"]])),
)
STEPS = ("received_denial", "proposal_written", "proposal_is_review_only")


class RehearseGapProposalsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.parent = Path(cls.enterClassContext(tempfile.TemporaryDirectory())).resolve()
        cls.count = itertools.count()
        sys.addaudithook(_audit)
        cls.senders = [cls.enterClassContext(patch.object(module, name, side_effect=AssertionError(name)))
                       for module, name in ((run_orchestrator, "cmd_notify_fallback"), (run_orchestrator, "execute"),
                                            (smtplib, "SMTP"), (smtplib, "SMTP_SSL"), (urllib.request, "urlopen"))]
        cls.sandbox = cls.fresh()
        SPY["on"] = True
        try:
            with patch.object(os, "open", spy_open):
                cls.result = driver.run(cls.sandbox)
        finally:
            SPY["on"] = False
        cls.run_id = json.loads((cls.sandbox / "replay" / "proof-report.json").read_text())["run_id"]

    @classmethod
    def fresh(cls):
        path = cls.parent / ("sandbox%d" % next(cls.count))
        path.mkdir(mode=0o700)
        return path

    def verdict(self, root, steps, edit=None):
        """The code of the first failing step among ``steps`` run on a copy of ``root`` (after ``edit``), else OK."""
        copy = self.parent / ("copy%d" % next(self.count))
        shutil.copytree(root, copy)
        if edit:
            edit(copy / "replay" / "project" / "run-outputs" / self.run_id)
        rehearsal = driver._GapRehearsal(copy, datetime.now(timezone.utc))
        rehearsal.report = {"run_id": self.run_id}
        try:
            for name in steps:
                getattr(rehearsal, name)()
        except driver._Failed as failed:
            return failed.args[0]
        return "OK"

    def test_c1_six_named_steps_pass_with_hashed_outputs(self):
        """C1: steps in order, all OK, hashes of the final bytes; bug: a step skipped or renamed, a stale hash."""
        self.assertEqual([s["name"] for s in self.result["steps"]], list(driver.STEP_NAMES))
        self.assertEqual((self.result["stages"], self.result["codes"], self.result["manual_steps"]), (["11"], [], []))
        self.assertEqual({s["code"] for s in self.result["steps"]}, {"OK"})
        self.assertEqual(stat.S_IMODE((self.sandbox / run_stages.SANDBOX_MARKER).stat().st_mode), 0o600)
        outputs = [o for s in self.result["steps"] for o in s["outputs"]]
        self.assertGreater(len(outputs), 10)
        for output in outputs:
            self.assertEqual(hashlib.sha256((self.sandbox / output["path"]).read_bytes()).hexdigest(), output["sha256"])

    def test_c2_the_proposal_is_one_cited_review_only_question_about_the_denial(self):
        """C2: what the real gap-proposals writer produced for the uncited denial, read back from the file itself."""
        document = json.loads((self.sandbox / "replay/project/run-outputs" / self.run_id / GAP).read_text())
        rows = [p for a in document["agencies"] for r in a["requests"] for p in r["proposals"]]
        self.assertEqual([p["type"] for p in rows], ["exemption_without_citation"])
        self.assertEqual((rows[0]["agency_id"], rows[0]["request_id"]), (driver.AGENCY, driver.REQUEST))
        self.assertTrue(rows[0]["quote"] and isinstance(rows[0]["page"], int) and rows[0]["question"].endswith("?"))
        self.assertEqual((document["publishable"], document["legal_conclusion"], rows[0]["legal_conclusion"]), (False, False, False))

    def test_c3_broken_file_fails_its_named_step(self):
        """C3: each broken file or disagreement gets its own code; bug: any of these passing as OK."""
        self.assertEqual(self.verdict(self.sandbox, STEPS), "OK")
        for code, bug, edit in FILE_BREAKS:
            with self.subTest(code=code, bug=bug):
                self.assertEqual(self.verdict(self.sandbox, STEPS, edit), code)

    def test_c4_the_cited_denial_control_is_real(self):
        """C4: the control denial cites a provision. Bugs: a writer that never sees a provision proposes anyway (so the proposal
        would come from the kind alone); counts disagree on the control; the control carries no provision (proves nothing)."""
        original = gap_proposals.propose

        with patch.object(gap_proposals, "EXEMPT_CITE", re.compile("(?!)")):  # a rule that never sees a provision
            result = driver.run(self.fresh())
        self.assertEqual((result["codes"], result["steps"][-1]["name"]), (["CONTROL_PROPOSED"], "cited_denial_proposes_nothing"))
        calls = []

        def counted(*args, **kwargs):
            report = original(*args, **kwargs)
            calls.append(report)
            if len(calls) == 2:  # the control replay
                report["counts"]["exemption_without_citation"] = 1
            return report
        with patch.object(gap_proposals, "propose", counted):
            result = driver.run(self.fresh())
        self.assertEqual((result["codes"], result["steps"][-1]["name"]), (["CONTROL_COUNTS_DISAGREE"], "cited_denial_proposes_nothing"))
        with patch.object(driver, "CITED_LINE", "Your request is denied for reasons of policy."):
            result = driver.run(self.fresh())
        self.assertEqual(result["codes"], ["CONTROL_PROPOSED"])
        self.assertNotEqual(driver.denial_pdf("a", True), driver.denial_pdf("a", False))

    def test_c5_send_or_extra_draft_in_a_journal_fails_no_sends(self):
        """C5: no send and no draft beyond the one appeal draft; bug: a next-step or reminder draft, or a send record."""
        queue, draft = "approvals/queue.jsonl", b'{"event": "draft", "revision": 0, "snapshot": {"kind": "%s"}}\n'
        for code, name, body in (("SEND_RECORDED", "approvals/mail-send.jsonl", b"{}\n"), ("SEND_RECORDED", queue, b'{"event": "sent"}\n'),
                                 ("DRAFT_QUEUED", queue, draft % b"next-social-post"), ("DRAFT_QUEUED", queue, draft % b"reminder-no-response"),
                                 ("DRAFT_QUEUED", queue, draft % b"appeal-denial" * 2), ("DRAFT_QUEUED", queue, draft % b"fee-reply"),
                                 ("OK", queue, draft % b"appeal-denial")):
            with self.subTest(code=code, name=name, body=body):
                root = self.fresh()
                shutil.copytree(self.sandbox / "replay", root / "replay")
                shutil.copytree(self.sandbox / "stray", root / "stray")
                (root / "replay/project/approvals").mkdir(parents=True, exist_ok=True)
                (root / "stray/project/approvals").mkdir(parents=True, exist_ok=True)
                (root / "stray/project" / queue).write_bytes(draft % b"appeal-denial")
                (root / "replay/project" / name).write_bytes(body)
                self.assertEqual(self.verdict(root, ("no_sends",)), code)
        root = self.fresh()  # the denial's own draft is required: a missing journal is a failure, not a pass
        self.assertEqual(self.verdict(root, ("no_sends",)), "DENIAL_DRAFT_MISSING")

    def test_c1_failed_replay_stops_the_rehearsal_with_a_named_code(self):
        """C1: seed_denial names why the replay failed; bug: a failed or sent replay reported as a pass."""
        failed = {"stages": [{"stage": "outputs", "status": "ok"}], "result": "FAIL", "manual_steps": [{}],
                  "sent": False, "published": False, "run_id": "r"}
        for code, effect in (("REPLAY_ARRIVAL_NOT_SEEN", e2e_proof.ProofError("ARRIVAL_NOT_SEEN")), ("REPLAY", failed),
                             ("SEND_RECORDED", dict(failed, result="PASS", manual_steps=[], sent=True))):
            with self.subTest(code=code), patch.object(e2e_proof, "replay", side_effect=[effect]):
                result = driver.run(self.fresh())
                self.assertEqual((result["codes"], [s["name"] for s in result["steps"]]), ([code], ["seed_denial"]))

    def test_sandbox_only_loopback_only_and_no_sender(self):
        """Interface: every write is inside the sandbox, sockets are loopback and port 0, no notifier or mailer runs."""
        root = str(self.sandbox) + os.sep
        self.assertGreater(len(SPY["writes"]), 20)
        self.assertEqual([w for w in SPY["writes"] if not (w.startswith(root) or w == os.devnull)], [])
        for event, address in SPY["sockets"]:
            self.assertIn(address[0], ("127.0.0.1", "::1", "localhost"), address)
            self.assertTrue(event != "socket.bind" or address[1] == 0, address)
        self.assertEqual([m.call_count for m in self.senders], [0] * len(self.senders))

    def test_receipt_qualifies_and_the_driver_is_discovered_for_stage_11_only(self):
        """Done-when: stage 11 resolves to this driver alone and its receipt qualifies; bug: a driver `rehearse` cannot reach."""
        self.assertEqual([d.__name__ for d in rehearse.discover() if "11" in d.STAGES], [driver.__name__])
        finished = datetime.now(timezone.utc)
        receipt = rehearsal_receipt.build("rehearse_gap_proposals", self.result, self.sandbox, finished - timedelta(seconds=9),
                                          finished, "a" * 40)
        self.assertEqual((receipt["qualified"], receipt["qualify_codes"]), (True, []))

    def test_main_exit_code_and_sandbox_refusal(self):
        """Interface: exit 0 only when every step is OK; a used --sandbox is refused with exit 2."""
        for result, exit_code in ((self.result, 0), (dict(self.result, codes=["X"]), 1), (dict(self.result, manual_steps=["m"]), 1)):
            out = io.StringIO()
            with patch.object(driver, "run", return_value=result), redirect_stdout(out):
                self.assertEqual(driver.main(["--sandbox", str(self.parent / ("main%d" % next(self.count)))]), exit_code)
            self.assertEqual(json.loads(out.getvalue()), result)
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as refused:
            driver.main(["--sandbox", str(self.sandbox)])
        self.assertEqual(refused.exception.code, 2)
