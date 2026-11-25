"""Stage 12 rehearsal driver: one real synthetic replay, then broken-input controls (nothing here sends or publishes)."""
import hashlib
import io
import itertools
import json
import os
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
from campaign_tool.records import rehearse_received as driver
from campaign_tool.records.outcomes import received_items, timeline, unmatched

WRITE = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND
SPY = {"on": False, "writes": [], "sockets": []}
os_open = os.open


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


def each(key, change):
    return lambda document: [change(row) for row in document[key]]


OUT, REG, TL, UN = "outcomes/received-items.json", "CURRENT-REQUEST-REGISTER.json", "outcomes/timeline.json", "outcomes/unmatched-messages.json"
FILE_BREAKS = (  # (code, the believable bug, edit of a copied run directory)
    ("RECEIVED_ITEMS_NOT_STRICT_JSON", "writer emits NaN", edit_json(OUT, lambda d: d.update(x=float("nan")))),
    ("REGISTER_NOT_STRICT_JSON", "duplicate key", edit_json(REG, lambda d: json.dumps(d)[:-1] + ', "schema": "x"}')),
    ("RECEIVED_ITEMS_RUN_MISMATCH", "stale run id", edit_json(OUT, lambda d: d.update(run_id="run_x"))),
    ("TIMELINE_RUN_MISMATCH", "stale run id", edit_json(TL, lambda d: d.update(run_id="run_x"))),
    ("REGISTER_RUN_MISMATCH", "register under another run", lambda run_dir: shutil.copytree(run_dir, run_dir.parent / "run_other")),
    ("RECEIVED_ITEM_DUPLICATED", "item listed twice", edit_json(OUT, lambda d: d["items"].append(dict(d["items"][0])))),
    ("RECEIVED_ITEM_UNTIED", "reply not tied to its request", edit_json(OUT, each("items", lambda i: i.update(request_id=None)))),
    ("REGISTER_REQUEST_MISSING", "request absent", edit_json(REG, lambda d: d.update(requests=[]))),
    ("REGISTER_STATUS_WRONG", "acknowledgement closes the request", edit_json(REG, each("requests", lambda r: r.update(status="closed")))),
    ("REGISTER_REPLY_NOT_ATTACHED", "reply not among deliveries", edit_json(REG, each("requests", lambda r: r.update(arrived=[])))),
    ("OUTPUTS_ITEM_NOT_ONCE", "timeline repeats the item", edit_json(TL, each("requests", lambda r: r.update(events=r["events"] * 2)))),
    ("OUTPUTS_STAGE_DISAGREES", "stage ignores the reply", edit_json(TL, each("requests", lambda r: r.update(stage="sent")))),
    ("OUTPUTS_REPLY_DATE_MISSING", "wrong reply date",
     edit_json(TL, each("requests", lambda r: [e.update(date="2000-01-01") for e in r["events"]]))),
    ("OUTPUTS_MATCHED_LISTED", "tied reply also unmatched", edit_json(UN, lambda d: d.update(messages=[{"message_sha256": "x"}]))),
)


class RehearseReceivedTests(unittest.TestCase):
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
        rehearsal = driver._Rehearsal(copy, datetime.now(timezone.utc))
        rehearsal.report = {"run_id": self.run_id}
        try:
            for name in steps:
                getattr(rehearsal, name)()
        except driver._Failed as failed:
            return failed.args[0]
        return "OK"

    def test_c1_seven_named_steps_pass_with_hashed_outputs(self):
        """C1: steps in order, all OK, hashes of the final bytes; bug: a step skipped or renamed, a stale hash."""
        self.assertEqual([s["name"] for s in self.result["steps"]], list(driver.STEP_NAMES))
        self.assertEqual((self.result["stages"], self.result["codes"], self.result["manual_steps"]), (["12"], [], []))
        self.assertEqual({s["code"] for s in self.result["steps"]}, {"OK"})
        self.assertEqual(stat.S_IMODE((self.sandbox / run_stages.SANDBOX_MARKER).stat().st_mode), 0o600)
        outputs = [o for s in self.result["steps"] for o in s["outputs"]]
        self.assertGreater(len(outputs), 10)
        for output in outputs:
            self.assertEqual(hashlib.sha256((self.sandbox / output["path"]).read_bytes()).hexdigest(), output["sha256"])

    def test_c2_c3_broken_file_fails_its_named_step(self):
        """C2/C3: each broken file or disagreement gets its own code; bug: any of these passing as OK."""
        steps = ("received_items", "register", "timeline", "outputs_agree")
        self.assertEqual(self.verdict(self.sandbox, steps), "OK")
        for code, bug, edit in FILE_BREAKS:
            with self.subTest(code=code, bug=bug):
                self.assertEqual(self.verdict(self.sandbox, steps, edit), code)

    def test_c5_send_or_draft_in_a_journal_fails_no_sends(self):
        """C5: no draft and no send; bug: a reply or follow-up draft, or a send record, going unnoticed."""
        queue, draft = "approvals/queue.jsonl", b'{"event": "draft", "snapshot": {"kind": "%s"}}\n'
        for code, name, body in (("SEND_RECORDED", "approvals/mail-send.jsonl", b"{}\n"), ("SEND_RECORDED", queue, b'{"event": "sent"}\n'),
                                 ("DRAFT_QUEUED", queue, draft % b"fee-reply"), ("DRAFT_QUEUED", queue, draft % b"reminder-no-response"),
                                 ("OK", queue, draft % b"message_link")):  # the decision to tie a message is not a draft
            with self.subTest(code=code, name=name, body=body):
                root = self.fresh()
                (root / "replay/project/approvals").mkdir(parents=True)
                (root / "replay/project" / name).write_bytes(body)
                self.assertEqual(self.verdict(root, ("no_sends",)), code)

    def test_c3_c4_real_stage_output_spoiled_fails_the_named_step(self):
        """C3/C4: the real stage code runs, only its output is spoiled. Bugs: the timeline repeats the item (not "once");
        an untied message is attached to the only agency; the writer drops an untied message from the unmatched list."""
        def repeat(document, *args):
            for row in document["requests"]:
                row["events"] = row["events"] * 2

        def attach(report, *args):
            for item in report["items"]:
                item["agency_id"] = item["agency_id"] or driver.AGENCY

        def drop(value, project, run_id, *args):
            path = Path(project) / "run-outputs" / run_id / UN
            path.write_text(json.dumps(dict(json.loads(path.read_text()), messages=[])))
        for module, name, change, code, step in (
                (timeline, "build_timeline", repeat, "OUTPUTS_ITEM_NOT_ONCE", "outputs_agree"),
                (received_items, "collect", attach, "UNMATCHED_ATTACHED", "unmatched_is_not_attached"),
                (unmatched, "write_run_unmatched", drop, "UNMATCHED_NOT_LISTED", "unmatched_is_not_attached")):
            original = getattr(module, name)

            def wrapper(*args, original=original, change=change, **kwargs):
                value = original(*args, **kwargs)
                change(value, *args)
                return value
            with self.subTest(code=code), patch.object(module, name, wrapper):
                result = driver.run(self.fresh())
                self.assertEqual((result["codes"], result["steps"][-1]["name"]), ([code], step))

    def test_c4_stray_register_that_ties_the_reply_fails_unmatched_is_not_attached(self):
        """C4: only the stray replay's register file is spoiled, after every real stage ran (received items, unmatched list
        and timeline stay clean). Bug: a register writer attaches the unmatched reply to an agency/request."""
        original = e2e_proof.replay

        def replay(*args, **kwargs):
            report = original(*args, **kwargs)
            if kwargs.get("label") == "stray":
                run_dir = args[1] / "stray" / "project" / "run-outputs" / report["run_id"]
                document = json.loads((run_dir / REG).read_text())
                sha = json.loads((run_dir / OUT).read_text())["items"][0]["original_sha256"]
                document.update(request_count=1, requests=[{"agency_key": driver.AGENCY, "request_id": driver.REQUEST,
                                                            "status": "open", "arrived": [{"original_sha256": sha}]}])
                (run_dir / REG).write_text(json.dumps(document))
            return report
        with patch.object(e2e_proof, "replay", replay):
            result = driver.run(self.fresh())
        self.assertEqual((result["codes"], result["steps"][-1]["name"]), (["UNMATCHED_ATTACHED"], "unmatched_is_not_attached"))
        for body, code in (("NaN", "UNMATCHED_NOT_STRICT_JSON"), ('{"schema": "x", "requests": []}', "UNMATCHED_SCHEMA")):
            def spoil(*args, body=body, **kwargs):
                report = original(*args, **kwargs)
                if kwargs.get("label") == "stray":
                    (args[1] / "stray" / "project" / "run-outputs" / report["run_id"] / REG).write_text(body)
                return report
            with self.subTest(code=code), patch.object(e2e_proof, "replay", spoil):
                self.assertEqual(driver.run(self.fresh())["codes"], [code])

    def test_c1_failed_replay_stops_the_rehearsal_with_a_named_code(self):
        """C1: seed_reply names why the replay failed; bug: a failed or sent replay reported as a pass."""
        failed = {"stages": [{"stage": "outputs", "status": "ok"}], "result": "FAIL", "manual_steps": [{}],
                  "sent": False, "published": False, "run_id": "r"}
        for code, effect in (("REPLAY_ARRIVAL_NOT_SEEN", e2e_proof.ProofError("ARRIVAL_NOT_SEEN")), ("REPLAY", failed),
                             ("SEND_RECORDED", dict(failed, result="PASS", manual_steps=[], sent=True))):
            with self.subTest(code=code), patch.object(e2e_proof, "replay", side_effect=[effect]):
                result = driver.run(self.fresh())
                self.assertEqual((result["codes"], [s["name"] for s in result["steps"]]), ([code], ["seed_reply"]))

    def test_sandbox_only_loopback_only_and_no_sender(self):
        """Interface: every write is inside the sandbox, sockets are loopback and port 0, no notifier or mailer runs."""
        root = str(self.sandbox) + os.sep
        self.assertGreater(len(SPY["writes"]), 20)
        self.assertEqual([w for w in SPY["writes"] if not (w.startswith(root) or w == os.devnull)], [])
        for event, address in SPY["sockets"]:
            self.assertIn(address[0], ("127.0.0.1", "::1", "localhost"), address)
            self.assertTrue(event != "socket.bind" or address[1] == 0, address)
        self.assertEqual([m.call_count for m in self.senders], [0] * len(self.senders))

    def test_receipt_qualifies_and_the_driver_is_discovered_for_stage_12(self):
        """Done-when: stage 12 resolves to this driver and its receipt qualifies; bug: a driver `rehearse` cannot reach."""
        self.assertEqual([d.__name__ for d in rehearse.discover() if "12" in d.STAGES], [driver.__name__])
        finished = datetime.now(timezone.utc)
        receipt = rehearsal_receipt.build("rehearse_received", self.result, self.sandbox, finished - timedelta(seconds=9),
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
