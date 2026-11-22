"""Offline runtime contracts for the stages 13 and 14 drafts rehearsal (C1-C3)."""

from contextlib import ExitStack, redirect_stderr, redirect_stdout
import hashlib
from io import StringIO
import json
import os
from pathlib import Path
import shutil
import socket
import stat
import subprocess
import tempfile
import unittest
from unittest import mock

from campaign_tool.records import approval_queue, e2e_proof, followup_clock, native_extract
from campaign_tool.records import rehearse_drafts as driver
from campaign_tool.records.run_stages import SANDBOX_MARKER

HAVE_PARSERS = not native_extract.missing_parsers()
NEEDS_PARSERS = "UNSUPPORTED_RUNTIME: pypdf/openpyxl not in this interpreter"
JOURNAL = "replay/project/approvals/queue.jsonl"
STEP_OUTPUTS = {
    "replay_reply": ["inputs/reply.eml", "inputs/seed.json", "campaign/content/agencies.json",
                     "replay/proof-report.json", "replay/project/run-report.json", "engine-resources.json"],
    "reply_draft_queued": [JOURNAL], "followup_due": [], "followup_draft_queued": [JOURNAL], "no_sends": [JOURNAL],
}
PACKAGE_LAW = ("campaign_tool/_resources/jurisdictions/us-ca/package.json",
               "campaign_tool/_resources/schemas/law-package.schema.json")


def _digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _physical_tmp(test):
    # The records readers reject symlink ancestry, so use the physical temp root.
    return Path(test.enterContext(tempfile.TemporaryDirectory(dir=Path(tempfile.gettempdir()).resolve())))


def _expected(sandbox, failure_at=None, code=None, manual=()):
    steps = []
    for name in driver.STEP_NAMES:
        if name == failure_at:
            steps.append({"name": name, "code": code, "outputs": []})
            break
        paths = list(STEP_OUTPUTS[name])
        if name == "followup_due":
            paths = [next(p.relative_to(sandbox).as_posix() for p in
                          (sandbox / "replay" / "project" / "run-outputs").glob("*/" + followup_clock.REGISTER_NAME))]
        steps.append({"name": name, "code": "OK", "outputs": [
            {"path": rel, "sha256": _digest(sandbox / rel)} for rel in paths]})
    return {"stages": ["13", "14"], "steps": steps, "manual_steps": list(manual), "codes": [code] if code else []}


def _fake_replay(test, *, status="ok", code=None, report_code=None, result="PASS", manual=(), entries=1, action=None):
    """A stand-in for ``e2e_proof.replay`` writing only the two report files the replay step reads."""
    def replay(eml, out, campaign, *, label, now, seed):
        test.assertEqual((Path(out), label), (test.sandbox, driver.LABEL))
        project = Path(out) / label / "project"
        project.mkdir(parents=True, mode=0o700)
        stages = [{"name": "outputs", "status": status, "error_code": report_code}] * entries
        (project / "run-report.json").write_text(json.dumps({"stages": stages}), encoding="utf-8")
        (Path(out) / label / "proof-report.json").write_text("{}\n", encoding="utf-8")
        if action:
            action()
        return {"run_id": "run_fake", "result": result, "manual_steps": list(manual),
                "stages": [{"stage": "outputs", "status": status, "code": code, "detail": {"outcomes": []}}]}
    return replay


class _Base(unittest.TestCase):
    def setUp(self):
        self.root = _physical_tmp(self)
        self.sandbox = self.root / "sandbox"
        self.sandbox.mkdir(mode=0o700)
        self.outside = self.root / "outside.txt"
        self.outside.write_text("not sandbox data\n", encoding="utf-8")


@unittest.skipUnless(HAVE_PARSERS, NEEDS_PARSERS)
class FullRehearsalTests(_Base):
    """One real run through every records stage, then exhaustive draft-count branches on copies of it."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(dir=Path(tempfile.gettempdir()).resolve())
        cls.real = Path(cls.tmp.name) / "real"
        out, err = StringIO(), StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            cls.code = driver.main(["--sandbox", str(cls.real)])
        cls.stdout, cls.stderr = out.getvalue(), err.getvalue()
        cls.project = cls.real / "replay" / "project"
        cls.queue = approval_queue.load_queue(str(cls.project))

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def copy(self):
        target = self.root / "copy"
        shutil.copytree(self.real, target, symlinks=True)
        rehearsal = driver._Rehearsal(target, None)
        rehearsal.report = {"run_id": json.loads((target / "replay" / "proof-report.json").read_bytes())["run_id"]}
        return rehearsal

    def fails(self, method, code):
        with self.assertRaises(driver._StepFailed) as failed:
            method()
        self.assertIs(type(failed.exception), driver._StepFailed)
        self.assertEqual(failed.exception.args, (code,))

    def add(self, rehearsal, kind, request_id, run_id=None, body="Synthetic extra draft.\n"):
        approval_queue.add_draft(str(rehearsal.project), item_id="test:" + kind + ":" + request_id, kind=kind,
                                 agency_id=driver.AGENCY, request_id=request_id, body=body, evidence={"test": True},
                                 source_run_id=run_id or rehearsal.report["run_id"])

    def drop(self, rehearsal, kind):
        row, = [r for r in approval_queue.load_queue(str(rehearsal.project)) if r["kind"] == kind]
        approval_queue.reject(str(rehearsal.project), row["id"], "synthetic", "synthetic rejection")

    def test_c1_one_reply_draft_one_followup_draft_zero_sends(self):
        """C1: the real stages queue exactly one reply draft and one follow-up draft; nothing is sent."""
        self.assertEqual((self.code, self.stderr), (0, ""))
        self.assertEqual(json.loads(self.stdout), _expected(self.real))
        run_id = json.loads((self.real / "replay" / "proof-report.json").read_bytes())["run_id"]
        # Exhaustive: every queue row, its state and its request. The message_link row is the untied .eml itself.
        self.assertEqual(sorted((r["kind"], r["status"], r["agency_id"], r["request_id"], r["source_run_id"])
                                for r in self.queue),
                         [("fee-reply", "pending", driver.AGENCY, driver.REPLIED, run_id),
                          ("message_link", "pending", "unmatched", "unmatched", run_id),
                          ("reminder-no-response", "pending", driver.AGENCY, driver.SILENT, run_id)])
        events = [json.loads(line) for line in (self.real / JOURNAL).read_bytes().splitlines()]
        self.assertEqual(sorted((e["event"], e["revision"]) for e in events), [("draft", 0)] * 3)
        self.assertFalse((self.project / "approvals" / "mail-send.jsonl").exists())
        report = json.loads((self.real / "replay" / "proof-report.json").read_bytes())
        self.assertEqual((report["result"], report["sent"], report["published"], report["manual_steps"]),
                         ("PASS", False, False, []))
        followups = next(s for s in report["stages"] if s["stage"] == "followups")
        self.assertEqual((followups["status"], followups["detail"]["requests"][0]["queued_by_kind"]),
                         ("ok", {"fee-reply": 1}))
        marker = self.real / SANDBOX_MARKER
        self.assertEqual((marker.read_bytes(), stat.S_IMODE(marker.stat().st_mode)), (b"", 0o600))

    def test_c1_reply_draft_count_branches(self):
        """C1: zero, two, a foreign run's or another request's reply draft each fail reply_draft_queued."""
        self.assertEqual(self.copy().reply_draft_queued(), [self.root / "copy" / JOURNAL])
        shutil.rmtree(self.root / "copy")
        for label, change, code in (
                ("missing", lambda r: self.drop(r, "fee-reply"), "REPLY_DRAFT_MISSING"),
                ("second", lambda r: self.add(r, "appeal-denial", driver.REPLIED), "REPLY_DRAFT_COUNT_INVALID"),
                ("same kind twice", lambda r: self.add(r, "fee-reply", driver.REPLIED), "REPLY_DRAFT_COUNT_INVALID"),
                ("other run", lambda r: (self.drop(r, "fee-reply"), self.add(r, "fee-reply", driver.REPLIED, "run_x")),
                 "REPLY_DRAFT_MISSING"),
                ("wrong request", lambda r: (self.drop(r, "fee-reply"), self.add(r, "fee-reply", driver.SILENT)),
                 "REPLY_DRAFT_WRONG_REQUEST")):
            with self.subTest(label):
                rehearsal = self.copy()
                change(rehearsal)
                self.fails(rehearsal.reply_draft_queued, code)
                shutil.rmtree(self.root / "copy")

    def test_c1_followup_due_branches(self):
        """C1: the silent request alone is past its clock; drafting off, a lost register or a second due clock fail."""
        rehearsal = self.copy()
        register, = (rehearsal.project / "run-outputs").glob("*/" + followup_clock.REGISTER_NAME)
        self.assertEqual(rehearsal.followup_due(), [register])
        row, = [r for r in json.loads(register.read_bytes())["requests"] if r["request_id"] == driver.SILENT]
        self.assertEqual(row["deadline"]["state"], "past_due_reminder")
        settings = rehearsal.project / followup_clock.SETTINGS_FILE
        settings.write_text(json.dumps({"schema": followup_clock.SETTINGS_SCHEMA, "default": {"autonomy": "off"}}),
                            encoding="utf-8")
        self.fails(rehearsal.followup_due, "FOLLOWUP_NOT_DUE")
        shutil.rmtree(self.root / "copy")
        rehearsal = self.copy()
        rehearsal.report = {"run_id": "run_other"}
        self.fails(rehearsal.followup_due, "FOLLOWUP_REGISTER_MISSING")
        shutil.rmtree(self.root / "copy")
        rehearsal = self.copy()
        register, = (rehearsal.project / "run-outputs").glob("*/" + followup_clock.REGISTER_NAME)
        doc = json.loads(register.read_bytes())
        for row in doc["requests"]:  # make the replied request look silent and overdue too
            silent = next(r for r in doc["requests"] if r["request_id"] == driver.SILENT)
            row.update(deadline=silent["deadline"], issues=[], answer_arrived=silent["answer_arrived"])
        register.write_text(json.dumps(doc), encoding="utf-8")
        self.fails(rehearsal.followup_due, "FOLLOWUP_DUE_UNEXPECTED")

    def test_c1_followup_draft_count_branches(self):
        """C1: zero, two (reminder, notice or engine draft) or another request's follow-up draft each fail."""
        self.assertEqual(self.copy().followup_draft_queued(), [self.root / "copy" / JOURNAL])
        shutil.rmtree(self.root / "copy")
        for label, change, code in (
                ("missing", lambda r: self.drop(r, "reminder-no-response"), "FOLLOWUP_DRAFT_MISSING"),
                ("notice too", lambda r: self.add(r, followup_clock.NOTICE, driver.SILENT),
                 "FOLLOWUP_DRAFT_COUNT_INVALID"),
                ("promised too", lambda r: self.add(r, "reminder-promised-date", driver.SILENT),
                 "FOLLOWUP_DRAFT_COUNT_INVALID"),
                ("engine too", lambda r: self.add(r, driver.ENGINE_FOLLOWUP, driver.REPLIED),
                 "FOLLOWUP_DRAFT_COUNT_INVALID"),
                ("other run", lambda r: (self.drop(r, "reminder-no-response"),
                                         self.add(r, "reminder-no-response", driver.SILENT, "run_x")),
                 "FOLLOWUP_DRAFT_MISSING"),
                ("wrong request", lambda r: (self.drop(r, "reminder-no-response"),
                                             self.add(r, "reminder-no-response", driver.REPLIED)),
                 "FOLLOWUP_DRAFT_WRONG_REQUEST"),
                ("wrong kind", lambda r: (self.drop(r, "reminder-no-response"),
                                          self.add(r, followup_clock.NOTICE, driver.SILENT)),
                 "FOLLOWUP_DRAFT_WRONG_REQUEST")):
            with self.subTest(label):
                rehearsal = self.copy()
                change(rehearsal)
                self.fails(rehearsal.followup_draft_queued, code)
                shutil.rmtree(self.root / "copy")

    def test_c1_no_sends_branches(self):
        """C1: a sent event in the queue journal or any mail-send log fails no_sends."""
        self.assertEqual(self.copy().no_sends(), [self.root / "copy" / JOURNAL])
        shutil.rmtree(self.root / "copy")
        rehearsal = self.copy()
        with (rehearsal.project / "approvals" / "queue.jsonl").open("a", encoding="utf-8") as journal:
            journal.write(json.dumps({"event": "sent", "id": "x"}) + "\n")
        self.fails(rehearsal.no_sends, "SEND_RECORDED")
        shutil.rmtree(self.root / "copy")
        rehearsal = self.copy()
        (rehearsal.project / "approvals" / "mail-send.jsonl").write_text("", encoding="utf-8")
        self.fails(rehearsal.no_sends, "SEND_RECORDED")

    def test_c2_real_outputs_stage_reached_the_queue(self):
        """C2: the real run report's outputs stage carries neither approval-queue code."""
        stages = json.loads((self.project / "run-report.json").read_bytes())["stages"]
        outputs, = [s for s in stages if s["name"] == "outputs"]
        self.assertEqual((outputs["status"], outputs["error_code"]), ("ok", None))
        proof = json.loads((self.real / "replay" / "proof-report.json").read_bytes())
        self.assertEqual([(s["status"], s["code"]) for s in proof["stages"] if s["stage"] == "outputs"], [("ok", None)])

    def test_c2_queue_failure_in_the_real_outputs_stage_fails_the_replay(self):
        """C2: a real outputs stage that cannot write or read the queue fails replay_reply with that code."""
        for code, error, manual in (
                ("OUTPUTS_APPROVAL_QUEUE_UNWRITABLE", approval_queue.ApprovalQueueError("queue_unwritable"),
                 ["followups:FOLLOWUP_DRAFT_MISSING"]),
                ("OUTPUTS_APPROVAL_QUEUE_UNAVAILABLE", approval_queue.ApprovalQueueError("queue_invalid"),
                 ["outputs:OUTPUTS_APPROVAL_QUEUE_UNAVAILABLE", "followups:FOLLOWUP_DRAFT_MISSING"])):
            with self.subTest(code), mock.patch.object(approval_queue, "queue_received_items", side_effect=error):
                result = driver.run(self.sandbox)
                # The replay's own follow-up check also names the reply draft the queue never received.
                self.assertEqual(result, _expected(self.sandbox, "replay_reply", code, manual=manual))
                outputs, = [s for s in json.loads((self.sandbox / "replay/project/run-report.json").read_bytes())
                            ["stages"] if s["name"] == "outputs"]
                self.assertEqual((outputs["status"], outputs["error_code"]), ("warn", code))
                shutil.rmtree(self.sandbox)
                self.sandbox.mkdir(mode=0o700)

    def test_c3_outputs_are_hashes_and_carry_no_draft_text(self):
        """C3: every output is a sandbox path and its sha256; no draft line appears in stdout or the reports."""
        result = json.loads(self.stdout)
        for step in result["steps"]:
            self.assertEqual(set(step), {"name", "code", "outputs"})
            for output in step["outputs"]:
                self.assertEqual(set(output), {"path", "sha256"})
                self.assertRegex(output["sha256"], r"^[0-9a-f]{64}$")
                self.assertEqual(output["sha256"], _digest(self.real / output["path"]))
                self.assertFalse(Path(output["path"]).is_absolute() or ".." in Path(output["path"]).parts)
        lines = {line.strip() for row in self.queue if row["kind"] != "message_link"
                 for line in row["body"].splitlines() if len(line.strip()) > 12}
        self.assertTrue(lines)
        reports = [self.stdout] + [(self.real / rel).read_text(encoding="utf-8") for rel in (
            "replay/proof-report.json", "replay/project/run-report.json", driver.RESOURCES_FILE)]
        for text in reports:
            for line in lines:
                self.assertNotIn(line, text)

    def test_c3_engine_resources_are_recorded_with_their_hashes(self):
        """C3: each engine resource the stages read is listed relative to the engine root with its sha256."""
        record = json.loads((self.real / driver.RESOURCES_FILE).read_bytes())
        self.assertEqual(set(record), {"schema", "resources"})
        self.assertEqual(record["schema"], driver.RESOURCES_SCHEMA)
        paths = [row["path"] for row in record["resources"]]
        self.assertEqual(paths, sorted(set(paths)))
        for row in record["resources"]:
            self.assertTrue(row["path"].startswith(("campaign_tool/", "schemas/")), row["path"])
            self.assertFalse(row["path"].endswith((".py", ".pyc")))
            self.assertEqual(row["sha256"], _digest(driver._ENGINE / row["path"]))
        for path in PACKAGE_LAW:
            self.assertIn(path, paths)

    def test_c3_a_real_stage_reading_outside_the_sandbox_fails_the_run(self):
        """C3: a non-package read outside the sandbox inside the real outputs stage is refused and fails the run."""
        real_settings, seen = followup_clock.load_settings, []

        def settings(project):
            try:
                seen.append(self.outside.read_bytes())
            except PermissionError:
                seen.append(PermissionError)
            return real_settings(project)

        with mock.patch.object(followup_clock, "load_settings", side_effect=settings):
            result = driver.run(self.sandbox)
        self.assertEqual(result, _expected(self.sandbox, "replay_reply", driver.BOUNDARY))
        self.assertTrue(seen)
        self.assertEqual(set(seen), {PermissionError})
        self.assertIsNone(driver._GUARD)
        self.assertEqual(self.outside.read_bytes(), b"not sandbox data\n")  # inert after the run


class DriverContractTests(_Base):
    def run_with(self, replay):
        with mock.patch.object(e2e_proof, "replay", side_effect=replay):
            return driver.run(self.sandbox)

    def test_interface_shape(self):
        """C1: the shared driver interface: STAGES, TITLE, run and main."""
        self.assertEqual((driver.STAGES, type(driver.TITLE)), (("13", "14"), str))
        self.assertTrue(callable(driver.run) and callable(driver.main))
        self.assertEqual(driver.STEP_NAMES,
                         ("replay_reply", "reply_draft_queued", "followup_due", "followup_draft_queued", "no_sends"))

    def test_c1_replay_refusal_is_a_coded_failure(self):
        """C1: a refused replay fails replay_reply with REPLAY_<code> and runs nothing later."""
        later = mock.patch.object(driver._Rehearsal, "reply_draft_queued", side_effect=AssertionError("later step"))
        with later, mock.patch.object(e2e_proof, "replay", side_effect=e2e_proof.ProofError("OUT_EXISTS")):
            result = driver.run(self.sandbox)
        self.assertEqual(result, {"stages": ["13", "14"], "steps": [
            {"name": "replay_reply", "code": "REPLAY_OUT_EXISTS", "outputs": []}],
            "manual_steps": [], "codes": ["REPLAY_OUT_EXISTS"]})
        self.assertEqual(sorted(p.name for p in self.sandbox.iterdir()), [SANDBOX_MARKER, "campaign", "inputs"])

    def test_c1_replay_branches(self):
        """C1: outputs not run, a run report without the outputs stage and a failed proof each fail replay_reply."""
        manual = ({"stage": "extract", "why": "extract:PARSER_MISSING"},
                  {"stage": "followups", "why": "FOLLOWUP_DRAFT_MISSING"})
        for label, fake, code, steps in (
                ("not run", dict(status="not_run"), "REPLAY_OUTPUTS_NOT_RUN", []),
                ("failed", dict(status="fail"), "REPLAY_OUTPUTS_NOT_RUN", []),
                ("no report entry", dict(entries=0), "RUN_REPORT_OUTPUTS_MISSING", []),
                ("two report entries", dict(entries=2), "RUN_REPORT_OUTPUTS_MISSING", []),
                ("proof failed", dict(result="FAIL", manual=manual), "REPLAY_FAILED",
                 ["extract:PARSER_MISSING", "followups:FOLLOWUP_DRAFT_MISSING"]),
                ("manual steps", dict(manual=manual[:1]), "REPLAY_FAILED", ["extract:PARSER_MISSING"])):
            with self.subTest(label):
                result = self.run_with(_fake_replay(self, **fake))
                self.assertEqual(result, {"stages": ["13", "14"], "steps": [
                    {"name": "replay_reply", "code": code, "outputs": []}], "manual_steps": steps, "codes": [code]})
                shutil.rmtree(self.sandbox)
                self.sandbox.mkdir(mode=0o700)

    def test_c2_queue_codes_fail_the_replay_from_either_report(self):
        """C2: either approval-queue code, in the proof stage or the run report, fails replay_reply with that code."""
        for code in driver.QUEUE_CODES:
            for label, fake in (("proof", dict(status="warn", code=code)),
                                ("run report", dict(status="warn", report_code=code))):
                with self.subTest(code=code, source=label):
                    result = self.run_with(_fake_replay(self, **fake))
                    self.assertEqual(result, {"stages": ["13", "14"], "steps": [
                        {"name": "replay_reply", "code": code, "outputs": []}], "manual_steps": [], "codes": [code]})
                    shutil.rmtree(self.sandbox)
                    self.sandbox.mkdir(mode=0o700)

    def test_c1_failed_later_step_stops_the_run(self):
        """C1: after a passing replay, a failing step is the last one reported; earlier outputs stay hashed."""
        nothing = mock.patch.object(driver._Rehearsal, "followup_due",
                                    side_effect=driver._StepFailed("FOLLOWUP_NOT_DUE"))
        never = mock.patch.object(driver._Rehearsal, "followup_draft_queued", side_effect=AssertionError("later"))
        queued = mock.patch.object(driver._Rehearsal, "reply_draft_queued", return_value=[])
        with nothing, never, queued:
            result = self.run_with(_fake_replay(self))
        replay_outputs = [{"path": rel, "sha256": _digest(self.sandbox / rel)} for rel in STEP_OUTPUTS["replay_reply"]]
        self.assertEqual(result, {"stages": ["13", "14"], "manual_steps": [], "codes": ["FOLLOWUP_NOT_DUE"], "steps": [
            {"name": "replay_reply", "code": "OK", "outputs": replay_outputs},
            {"name": "reply_draft_queued", "code": "OK", "outputs": []},
            {"name": "followup_due", "code": "FOLLOWUP_NOT_DUE", "outputs": []}]})

    def test_c3_a_resource_read_after_the_record_fails_the_step(self):
        """C3: an engine resource first read after the replay is not in the record, so that step fails."""
        resource = driver._ENGINE / PACKAGE_LAW[0]

        def reads():
            resource.read_bytes()
            return []
        with mock.patch.object(driver._Rehearsal, "reply_draft_queued", side_effect=reads):
            result = self.run_with(_fake_replay(self))
        self.assertEqual([(s["name"], s["code"]) for s in result["steps"]],
                         [("replay_reply", "OK"), ("reply_draft_queued", "ENGINE_RESOURCE_UNRECORDED")])
        self.assertEqual((result["codes"], result["steps"][1]["outputs"]), (["ENGINE_RESOURCE_UNRECORDED"], []))
        record = json.loads((self.sandbox / driver.RESOURCES_FILE).read_bytes())
        self.assertEqual(record, {"schema": driver.RESOURCES_SCHEMA, "resources": []})

    def test_c3_dependency_error_is_sanitized(self):
        """C3: a dependency exception becomes a fixed DRAFTS_<STEP>_ERROR code; its text never reaches output."""
        private = "synthetic private draft text"
        for index, name in enumerate(driver.STEP_NAMES):
            with self.subTest(name), ExitStack() as stack:
                for earlier in driver.STEP_NAMES[:index]:
                    stack.enter_context(mock.patch.object(driver._Rehearsal, earlier, return_value=[]))
                stack.enter_context(mock.patch.object(driver._Rehearsal, name, side_effect=OSError(private)))
                out = stack.enter_context(redirect_stdout(StringIO()))
                err = stack.enter_context(redirect_stderr(StringIO()))
                code = driver.main(["--sandbox", str(self.sandbox)])
                expected = "DRAFTS_" + name.upper() + "_ERROR"
                record = [{"path": driver.RESOURCES_FILE, "sha256": _digest(self.sandbox / driver.RESOURCES_FILE)}] \
                    if index else []
                steps = [{"name": n, "code": "OK", "outputs": record if n == "replay_reply" else []}
                         for n in driver.STEP_NAMES[:index]]
                self.assertEqual((code, json.loads(out.getvalue())), (1, {
                    "stages": ["13", "14"], "steps": steps + [{"name": name, "code": expected, "outputs": []}],
                    "manual_steps": [], "codes": [expected]}))
                self.assertNotIn(private, out.getvalue() + err.getvalue())
            shutil.rmtree(self.sandbox)
            self.sandbox.mkdir(mode=0o700)

    def test_c3_boundary_refuses_outside_reads_writes_processes_and_sockets(self):
        """C3: outside reads and writes, a foreign child process and non-loopback sockets are refused and fail."""
        written = self.root / "written.txt"

        def attempts():
            return (lambda: self.outside.read_bytes(), lambda: written.write_text("x", encoding="utf-8"),
                    lambda: subprocess.run(["true"], check=False),
                    lambda: socket.create_connection(("192.0.2.1", 9), timeout=0.1),
                    lambda: socket.create_server(("127.0.0.1", 8)))
        for index in range(len(attempts())):
            with self.subTest(index=index):
                outcome = []

                def act():
                    try:
                        attempts()[index]()
                        outcome.append("done")
                    except PermissionError:
                        outcome.append(PermissionError)
                result = self.run_with(_fake_replay(self, action=act))
                self.assertEqual(outcome, [PermissionError])
                self.assertEqual(result, {"stages": ["13", "14"], "steps": [
                    {"name": "replay_reply", "code": driver.BOUNDARY, "outputs": []}],
                    "manual_steps": [], "codes": [driver.BOUNDARY]})
                self.assertFalse(written.exists())
                self.assertIsNone(driver._GUARD)
                shutil.rmtree(self.sandbox)
                self.sandbox.mkdir(mode=0o700)

    def test_c3_engine_resource_and_loopback_port_zero_are_allowed_and_recorded(self):
        """C3: a packaged resource read is recorded with its sha256; a loopback port-0 listener is allowed."""
        resource = driver._ENGINE / PACKAGE_LAW[0]

        def act():
            resource.read_bytes()
            socket.create_server(("127.0.0.1", 0)).close()
        result = self.run_with(_fake_replay(self, action=act))
        self.assertEqual(result["codes"], ["REPLY_DRAFT_MISSING"])  # the fake queued nothing
        self.assertEqual([s["code"] for s in result["steps"]], ["OK", "REPLY_DRAFT_MISSING"])
        record = json.loads((self.sandbox / driver.RESOURCES_FILE).read_bytes())
        self.assertIn({"path": PACKAGE_LAW[0], "sha256": _digest(resource)}, record["resources"])

    def test_c3_guard_cannot_nest(self):
        """C3: a second guarded run inside a running one is refused with ValueError."""
        with driver._guarded(self.sandbox):
            with self.assertRaisesRegex(ValueError, "^REHEARSAL_ALREADY_RUNNING$") as refused:
                with driver._guarded(self.root):
                    pass
        self.assertIs(type(refused.exception), ValueError)
        self.assertIsNone(driver._GUARD)

    def test_c3_refuse_unsafe_destinations(self):
        """C3: main exits 2 for a nonempty, public, non-directory or symlinked sandbox; run raises SANDBOX_INVALID."""
        sentinel = self.sandbox / "keep"
        sentinel.write_bytes(b"unchanged")
        empty = self.root / "empty"
        empty.mkdir(mode=0o700)
        alias = self.root / "alias"
        alias.symlink_to(empty, target_is_directory=True)
        wide = self.root / "wide"
        wide.mkdir()
        wide.chmod(0o755)
        for path in (self.sandbox, sentinel, alias, alias / "child", wide):
            with self.subTest(path=path.name), redirect_stderr(StringIO()), redirect_stdout(StringIO()) as output:
                with self.assertRaises(SystemExit) as refused:
                    driver.main(["--sandbox", str(path)])
                self.assertEqual((type(refused.exception), refused.exception.code), (SystemExit, 2))
                self.assertEqual(output.getvalue(), "")
        with self.assertRaisesRegex(ValueError, "^SANDBOX_INVALID$") as refused:
            driver.run(self.sandbox)
        self.assertIs(type(refused.exception), ValueError)
        self.assertEqual(list(self.sandbox.iterdir()), [sentinel])
        self.assertEqual((list(empty.iterdir()), list(wide.iterdir())), ([], []))


if __name__ == "__main__":
    unittest.main()
