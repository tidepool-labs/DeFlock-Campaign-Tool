"""Offline runtime contracts for the stages 13 and 14 drafts rehearsal (C1-C3)."""

from contextlib import ExitStack, redirect_stderr, redirect_stdout
import hashlib
from io import StringIO
import json
import os
from pathlib import Path
import shutil
import socket
import sqlite3
import stat
import subprocess
import sys
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


def _stand_in_roots(parent):
    """Synthetic neighbours, not paths taken from live records configuration."""
    roots = parent / "records", parent / "state"
    for root in roots:
        (root / "nested").mkdir(parents=True, mode=0o700)
        marker = root / "nested" / "keep.txt"
        marker.write_bytes(root.name.encode())
        marker.chmod(0o600)
    return roots


def _fingerprint(root):
    entries = {}
    for path in sorted((root, *root.rglob("*"))):
        mode = path.lstat().st_mode
        if stat.S_ISLNK(mode):
            digest = hashlib.sha256(os.fsencode(os.readlink(path))).hexdigest()
        else:
            digest = _digest(path) if stat.S_ISREG(mode) else None
        entries[path.relative_to(root).as_posix()] = (digest, mode)
    return entries


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
        cls.stand_ins = _stand_in_roots(Path(cls.tmp.name))
        cls.before = tuple(map(_fingerprint, cls.stand_ins))
        out, err = StringIO(), StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            cls.code = driver.main(["--sandbox", str(cls.real)])
        cls.after = tuple(map(_fingerprint, cls.stand_ins))
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

    def test_c3_real_worker_launch_is_isolated_from_operator_environment(self):
        """C3: inherited interpreter settings must not reach the actual worker launch."""
        fork_exec, launches = getattr(subprocess, "_fork_exec"), []
        expected_env = {
            b"PATH": os.fsencode(os.defpath), b"HOME": os.fsencode(self.sandbox),
            b"TMPDIR": os.fsencode(self.sandbox), b"LANG": b"C.UTF-8", b"LC_ALL": b"C.UTF-8",
        }

        def observe(*args):
            # Stop an unsafe launch before exec, including when this test runs against the old driver.
            argv, env = args[0], args[5]
            actual_env = None if env is None else dict(item.split(b"=", 1) for item in env)
            isolated = os.fsdecode(argv[1]) == "-I"
            launches.append((actual_env, isolated))
            if (actual_env, isolated) != (expected_env, True):
                raise PermissionError("unsafe test worker launch")
            return fork_exec(*args)

        with mock.patch.dict(os.environ, {"PYTHONPATH": str(self.root)}), \
                mock.patch.object(subprocess, "_fork_exec", observe):
            result = driver.run(self.sandbox)
        self.assertTrue(launches, "the real extraction stage must start its worker")
        for env, isolated in launches:
            self.assertIsNotNone(env, "worker must receive an explicit environment")
            self.assertEqual((env, isolated), (expected_env, True))
        self.assertEqual(result, _expected(self.sandbox))

    def test_c3_full_rehearsal_preserves_stand_in_roots(self):
        """C3: independent byte/mode and path preservation across the same successful full run."""
        self.assertEqual(self.code, 0)
        self.assertEqual(self.before, self.after)


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

    def test_c3_relative_dotdot_open_is_refused(self):
        """C3: a relative ../ read or write that would reach a host file is refused and the file is unchanged."""
        with driver._guarded(self.sandbox) as guard:
            with self.assertRaises(PermissionError):
                open("../outside.txt", "rb")
            with self.assertRaises(PermissionError):
                open("../outside.txt", "w")
        self.assertEqual(guard.violations, ["open_read", "open_write"])
        self.assertEqual(self.outside.read_text(encoding="utf-8"), "not sandbox data\n")

    def test_c3_host_cwd_file_is_unreachable_by_a_relative_open(self):
        """C3: a file only in the pre-rehearsal host cwd is not read; the cwd is the sandbox and is restored."""
        host = self.root / "host_cwd"
        host.mkdir()
        (host / "only_host.txt").write_text("host", encoding="utf-8")
        before = os.getcwd()
        os.chdir(host)
        try:
            with driver._guarded(self.sandbox):
                self.assertEqual(os.path.realpath(os.getcwd()), os.path.realpath(self.sandbox))
                with self.assertRaises(FileNotFoundError):
                    open("only_host.txt", "rb")
                (self.sandbox / "inside.txt").write_text("ok", encoding="utf-8")
                with open("inside.txt", "rb") as handle:  # a relative open inside the sandbox still works
                    self.assertEqual(handle.read(), b"ok")
            self.assertEqual(os.path.realpath(os.getcwd()), os.path.realpath(host))
        finally:
            os.chdir(before)

    def test_c3_symlink_inside_sandbox_to_a_host_file_is_refused(self):
        """C3: an absolute sandbox path that is a symlink to a host file is refused for read and write."""
        link = self.sandbox / "link.txt"
        link.symlink_to(self.outside)
        with driver._guarded(self.sandbox) as guard:
            with self.assertRaises(PermissionError):
                open(str(link), "rb")
            with self.assertRaises(PermissionError):
                open(str(link), "w")
            with self.assertRaises(PermissionError):
                open("link.txt", "rb")  # and the same link reached by a relative name
        self.assertEqual(guard.violations, ["open_read", "open_write", "open_read"])
        self.assertEqual(self.outside.read_text(encoding="utf-8"), "not sandbox data\n")

    def _outside_db(self):
        """An outside SQLite file with one empty table, and a symlink to it inside the sandbox."""
        db = self.root / "outside.db"
        connection = sqlite3.connect(str(db))
        connection.execute("create table t (v integer)")
        connection.commit()
        connection.close()
        (self.sandbox / "db.lnk").symlink_to(db)
        return db

    @staticmethod
    def _rows(db):
        connection = sqlite3.connect(str(db))
        try:
            return connection.execute("select count(*) from t").fetchone()[0]
        finally:
            connection.close()

    def test_c3_sqlite_symlink_to_an_outside_database_is_refused(self):
        """C3: sqlite3.connect decides on the real path: plain, file: URI and percent-encoded forms are refused.

        Bug caught: the sqlite branch judged the lexical name, so a sandbox symlink to an outside database passed
        and a write landed outside with no violation.
        """
        db = self._outside_db()
        link = str(self.sandbox / "db.lnk")
        encoded = "file:" + link.replace("/", "%2F").replace("db.lnk", "db%2Elnk") + "?mode=rw"
        cases = ((link, {}), ("file:%s?mode=rw" % link, {"uri": True}), (encoded, {"uri": True}),
                 ("file:db.lnk?mode=rw", {"uri": True}), ("db.lnk", {}))
        for name, kwargs in cases:
            with self.subTest(name=name):
                with driver._guarded(self.sandbox) as guard:
                    with self.assertRaises(PermissionError):
                        connection = sqlite3.connect(name, **kwargs)
                        connection.execute("insert into t values (1)")
                        connection.commit()
                self.assertEqual(guard.violations, ["sqlite"])
                self.assertEqual(self._rows(db), 0)

    def test_c3_sqlite_readonly_symlink_uri_is_refused(self):
        """C3: a read-only file: URI through the symlink is refused too (the boundary covers reads)."""
        self._outside_db()
        with driver._guarded(self.sandbox) as guard:
            with self.assertRaises(PermissionError):
                sqlite3.connect("file:%s?mode=ro" % (self.sandbox / "db.lnk"), uri=True)
        self.assertEqual(guard.violations, ["sqlite"])

    def test_c3_sqlite_memory_and_sandbox_files_are_still_allowed(self):
        """C3: in-memory databases (both spellings) and a real file inside the sandbox are not refused."""
        with driver._guarded(self.sandbox) as guard:
            sqlite3.connect(":memory:").close()
            sqlite3.connect("file::memory:?mode=memory", uri=True).close()
            sqlite3.connect(str(self.sandbox / "own.db")).close()
        self.assertEqual(guard.violations, [])

    def test_c3_worker_argv_symlink_to_an_outside_directory_is_refused(self):
        """C3: a worker argv path that is a sandbox symlink to an outside directory is refused before exec.

        Bug caught: argv 5/8/9 were judged lexically, so the unaudited child could read and write outside.
        """
        outside_dir = self.root / "outside_dir"
        outside_dir.mkdir()
        (self.sandbox / "real").mkdir()
        (self.sandbox / "dir.lnk").symlink_to(outside_dir, target_is_directory=True)

        def argv(first):
            return [sys.executable, *driver._WORKER, first, "x", "y", str(self.sandbox / "real"),
                    str(self.sandbox / "real")]
        with driver._guarded(self.sandbox) as guard:
            safe = [sys.executable, *driver._ISOLATED_WORKER, *argv(str(self.sandbox / "real"))[5:]]
            guard.check("subprocess.Popen", (sys.executable, safe, None, guard.worker_env))  # allowed
            self.assertEqual(guard.violations, [])
            with self.assertRaises(PermissionError):
                subprocess.Popen(argv(str(self.sandbox / "dir.lnk")))
        self.assertEqual(guard.violations, ["subprocess"])

    def _worker_argv(self):
        """A worker argv the boundary accepts on its own (sandbox paths, no wrap-image)."""
        (self.sandbox / "real").mkdir(exist_ok=True)
        real = str(self.sandbox / "real")
        return [sys.executable, *driver._WORKER, real, "x", "y", real, real]

    def _escape(self):
        """An outside marker path and an outside executable script that creates it when run."""
        marker = self.root / "escaped.marker"
        script = self.root / "escape.sh"
        script.write_text("#!/bin/sh\n: > '%s'\n" % marker, encoding="utf-8")
        script.chmod(0o700)
        return marker, script

    def test_c3_worker_argv_with_a_foreign_executable_is_refused(self):
        """C3: Popen(valid worker argv, executable=<other program>) is refused; the other program never runs.

        Bug caught: only argv was judged, but POSIX execs ``executable`` when given, so a valid worker argv
        carried an arbitrary outside program past the boundary.
        """
        marker, script = self._escape()
        for executable in (str(script), os.fsencode(script), script):
            with self.subTest(executable=type(executable).__name__):
                with driver._guarded(self.sandbox) as guard:
                    with self.assertRaises(PermissionError):
                        subprocess.Popen(self._worker_argv(), executable=executable).wait()
                self.assertEqual(guard.violations, ["subprocess"])
                self.assertFalse(marker.exists())

    def test_c3_worker_with_a_custom_environment_or_cwd_is_refused(self):
        """C3: caller-supplied env= or cwd= cannot override the driver's worker startup contract.

        Bug caught: env was not judged, so PYTHONPATH (a sitecustomize) or PYTHONSTARTUP made the trusted
        interpreter run outside code beyond the hook.
        """
        marker, _ = self._escape()
        hooks = self.root / "hooks"
        hooks.mkdir()
        (hooks / "sitecustomize.py").write_text("open(%r, 'w').close()\n" % str(marker), encoding="utf-8")
        cases = ({"env": {**os.environ, "PYTHONSTARTUP": str(hooks / "sitecustomize.py")}},
                 {"env": {**os.environ, "PYTHONPATH": str(hooks)}}, {"env": {}},
                 {"cwd": str(self.sandbox)})
        for kwargs in cases:
            with self.subTest(kwargs=sorted(kwargs)):
                with driver._guarded(self.sandbox) as guard:
                    with self.assertRaises(PermissionError):
                        subprocess.Popen(self._worker_argv(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                         **kwargs).wait()
                self.assertEqual(guard.violations, ["subprocess"])
                self.assertFalse(marker.exists())

    def test_c3_interpreter_environment_cannot_be_changed_for_an_inherited_child(self):
        """C3: inside the window only the replay's own trigger variable may be set; PYTHONPATH and LD_PRELOAD not.

        Bug caught: env=None inherits the process environment, so setting it first would carry the same escape.
        """
        before = dict(os.environ)
        with driver._guarded(self.sandbox) as guard:
            for name in ("PYTHONPATH", "PYTHONHOME", "LD_PRELOAD"):
                with self.assertRaises(PermissionError):
                    os.environ[name] = str(self.root)
                with self.assertRaises(PermissionError):
                    os.putenv(name, str(self.root))
            with self.assertRaises(PermissionError):
                os.unsetenv("PYTHONPATH")  # direct unsets are refused even when the key is absent
            os.environ["RECORDS_RUN_TRIGGER"] = "arrival"  # control: what the real replay sets and clears
            del os.environ["RECORDS_RUN_TRIGGER"]
        self.assertEqual(guard.violations, ["environment"] * 7)
        self.assertEqual(dict(os.environ), before)

    def test_c3_minimal_environment_restores_exact_bytes_on_every_exit(self):
        """C3: replacing the operator environment cannot drop bytes or retain the replay's trigger."""
        operator = {b"PYTHONPATH": os.fsencode(self.root), b"SYNTHETIC_BYTES": b"\xff=", b"RECORDS_RUN_TRIGGER": b"old"}
        with mock.patch.dict(os.environb, operator):
            before = tuple(os.environb.items())
            for raises in (False, True):
                with self.subTest(raises=raises):
                    with ExitStack() as stack:
                        if raises:
                            stack.enter_context(self.assertRaisesRegex(RuntimeError, "fixture error"))
                        with driver._guarded(self.sandbox) as guard:
                            self.assertEqual(dict(os.environ), guard.worker_env)
                            os.environ["RECORDS_RUN_TRIGGER"] = "arrival"
                            if raises:
                                raise RuntimeError("fixture error")
                    self.assertEqual(tuple(os.environb.items()), before)

    def test_c3_rename_checks_both_paths_and_directory_descriptors(self):
        """C3: neither outside rename operand nor descriptor anchoring is allowed before effect."""
        dst = self.root / "other.txt"
        dst.write_bytes(b"second marker")
        inside = self.sandbox / self.outside.name
        inside.write_bytes(b"inside")
        fd = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY)
        try:
            with driver._guarded(self.sandbox) as guard:
                for src, target, kwargs in (
                    (self.outside, dst, {}), (self.outside, inside, {}), (inside, dst, {}),
                    (self.outside.name, "new.txt", {"src_dir_fd": fd}),
                    (inside, dst.name, {"dst_dir_fd": fd}),
                ):
                    with self.assertRaises(PermissionError):
                        os.rename(src, target, **kwargs)
                os.rename(inside, "new.txt")
        finally:
            os.close(fd)
        self.assertEqual(guard.violations, ["os.rename"] * 5)
        self.assertEqual((self.outside.read_bytes(), dst.read_bytes()), (b"not sandbox data\n", b"second marker"))
        self.assertEqual((self.sandbox / "new.txt").read_bytes(), b"inside")

    def test_c3_proven_dir_fds_allow_approval_creation_and_temp_cleanup(self):
        """C3: blanket refusal of dir_fd would break the queue writer and secure temporary-tree cleanup."""
        with driver._guarded(self.sandbox) as guard:
            fd = os.open(self.sandbox, os.O_RDONLY | os.O_DIRECTORY)
            try:
                approvals = approval_queue._open_approvals(fd, create=True)
                assert approvals is not None
                os.close(approvals)
            finally:
                os.close(fd)
            with tempfile.TemporaryDirectory(dir=self.sandbox) as temporary:
                temp = Path(temporary)
                (temp / "nested").mkdir()
                (temp / "nested" / "file").write_bytes(b"fixture")
            self.assertFalse(temp.exists())
        self.assertTrue((self.sandbox / "approvals").is_dir())
        self.assertEqual(guard.violations, [])

    def test_c3_dir_fd_requires_a_single_name_and_current_directory_identity(self):
        """C3: a held dir_fd is not authority for a multipiece name or for a directory moved outside."""
        held = self.sandbox / "held"
        held.mkdir()
        (held / "nested").mkdir()
        fd = os.open(held, os.O_RDONLY | os.O_DIRECTORY)
        try:
            with driver._guarded(self.sandbox) as guard:
                for name in ("nested/new", "../new", "..", "."):
                    with self.subTest(name=name), self.assertRaises(PermissionError):
                        os.mkdir(name, dir_fd=fd)
            self.assertEqual(guard.violations, ["os.mkdir"] * 4)
            self.assertFalse((held / "nested" / "new").exists())
            self.assertFalse((self.sandbox / "new").exists())

            moved = self.root / "moved"
            held.rename(moved)  # prepare the stale-descriptor fixture before reentering the guard
            held.mkdir()  # an old name is not evidence of the held descriptor's current location
            before = _fingerprint(moved)
            with driver._guarded(self.sandbox) as guard:
                with self.assertRaises(PermissionError):
                    os.mkdir("new", dir_fd=fd)
            self.assertEqual(guard.violations, ["os.mkdir"])
            self.assertEqual(_fingerprint(moved), before)
        finally:
            os.close(fd)

    def test_c3_direct_file_descriptor_mutation_needs_proven_target(self):
        """C3: outputs' in-sandbox fchmod works, but a held outside file is not writable by descriptor."""
        inside = self.sandbox / "fixture"
        inside.write_bytes(b"fixture")
        before = self.outside.stat().st_mode
        with self.outside.open("rb") as outer, inside.open("rb") as inner:
            with driver._guarded(self.sandbox) as guard:
                with self.assertRaises(PermissionError):
                    os.fchmod(outer.fileno(), 0o400)
                os.fchmod(inner.fileno(), 0o400)
        self.assertEqual(guard.violations, ["os.chmod"])
        self.assertEqual(self.outside.stat().st_mode, before)
        self.assertEqual(stat.S_IMODE(inside.stat().st_mode), 0o400)

    def test_c3_mutations_refused_outside_and_allowed_inside(self):
        """C3: mutation events protect data and metadata; the same ordinary operations still work inside."""
        def fixture(root):
            root.mkdir()
            for name in ("remove", "chmod", "truncate", "utime"):
                (root / name).write_bytes(b"keep")
                (root / name).chmod(0o600)
            (root / "tree").mkdir()

        def actions(root):
            return (
                ("os.remove", lambda: os.remove(root / "remove")),
                ("os.mkdir", lambda: os.mkdir(root / "new")),
                ("os.symlink", lambda: os.symlink("chmod", root / "link")),
                ("os.chmod", lambda: os.chmod(root / "chmod", 0o400)),
                ("os.truncate", lambda: os.truncate(root / "truncate", 1)),
                ("os.utime", lambda: os.utime(root / "utime", ns=(1_000_000_000, 1_000_000_000))),
                ("shutil.rmtree", lambda: shutil.rmtree(root / "tree")),
            )

        outside, inside = self.root / "markers", self.sandbox / "markers"
        for root in (outside, inside):
            fixture(root)
        before, mtime = _fingerprint(outside), (outside / "utime").stat().st_mtime_ns
        with driver._guarded(self.sandbox) as guard:
            for event, action in actions(outside):
                with self.subTest(event=event), self.assertRaises(PermissionError):
                    action()
            for event, action in actions(inside):
                with self.subTest(event=event):
                    action()
        self.assertEqual(guard.violations, [event for event, _ in actions(outside)])
        self.assertEqual(_fingerprint(outside), before)
        self.assertEqual((outside / "utime").stat().st_mtime_ns, mtime)
        self.assertFalse((inside / "remove").exists())
        self.assertTrue((inside / "new").is_dir())
        self.assertEqual(os.readlink(inside / "link"), "chmod")
        self.assertEqual(stat.S_IMODE((inside / "chmod").stat().st_mode), 0o400)
        self.assertEqual((inside / "truncate").read_bytes(), b"k")
        self.assertEqual((inside / "utime").stat().st_mtime_ns, 1_000_000_000)
        self.assertFalse((inside / "tree").exists())

    def test_c3_unaudited_fork_exec_is_judged_like_popen(self):
        """C3: multiprocessing's spawn path (_posixsubprocess.fork_exec raises no audit event) is refused.

        Bug caught: fork_exec bypassed the subprocess.Popen event, so a spawn/forkserver start ran any program.
        """
        import _posixsubprocess
        from multiprocessing import util
        marker, script = self._escape()
        originals = (_posixsubprocess.fork_exec, subprocess._fork_exec)
        with driver._guarded(self.sandbox) as guard:
            with self.assertRaises(PermissionError):
                pid = util.spawnv_passfds(os.fsencode(script), [os.fsencode(script)], [])
                os.waitpid(pid, 0)
        self.assertEqual(guard.violations, ["subprocess"])
        self.assertFalse(marker.exists())
        self.assertEqual((_posixsubprocess.fork_exec, subprocess._fork_exec), originals)  # restored after

    def test_c3_native_code_loads_and_foreign_calls_are_refused(self):
        """C3: a named ctypes library, a symbol lookup, the low-level foreign call and an outside extension module
        are refused.

        Bug caught: native code runs beyond the hook; none of these routes was judged.
        """
        import _ctypes
        import ctypes
        import importlib.util
        origin = importlib.util.find_spec("_lzma").origin
        copied = self.sandbox / os.path.basename(origin)
        shutil.copyfile(origin, copied)
        libc = ctypes.CDLL(None)
        address = ctypes.cast(libc.getpid, ctypes.c_void_p).value
        actions = ((lambda: ctypes.CDLL("libc.so.6"), "ctypes.dlopen"), (lambda: libc.getppid, "ctypes.dlsym"),
                   (lambda: _ctypes.call_function(address, ()), "ctypes.call_function"),
                   (lambda: importlib.util.module_from_spec(importlib.util.spec_from_file_location("_lzma", copied)),
                    "native"))
        for action, kind in actions:
            with self.subTest(kind=kind):
                with driver._guarded(self.sandbox) as guard:
                    with self.assertRaises(PermissionError):
                        action()
                    ctypes.CDLL(None)  # control: a handle on the already-loaded process (what ctypes' import does)
                self.assertEqual(guard.violations, [kind])

    def test_c4_failed_guard_entry_restores_process_state(self):
        """C4: a sandbox that vanishes before the chdir leaves os.open, cwd, PYTHONPATH and dir_fd support untouched.

        Bug caught: the os.open wrapper was installed before the try/finally, so a failed entry left it in place.
        """
        real_open, cwd = os.open, os.getcwd()
        path_env, supported = os.environ.get("PYTHONPATH"), os.open in os.supports_dir_fd
        with self.assertRaises(FileNotFoundError):
            with driver._guarded(self.root / "missing_sandbox"):
                self.fail("the guarded window must not open")
        self.assertIs(os.open, real_open)
        self.assertEqual(os.getcwd(), cwd)
        self.assertEqual(os.environ.get("PYTHONPATH"), path_env)
        self.assertEqual(os.open in os.supports_dir_fd, supported)
        self.assertIsNone(driver._GUARD)
        sandbox = self.root / "ok_sandbox"
        sandbox.mkdir()
        with driver._guarded(sandbox):
            self.assertTrue(os.open in os.supports_dir_fd)
        self.assertIs(os.open, real_open)
        self.assertEqual(os.open in os.supports_dir_fd, supported)

    def test_c3_dir_fd_relative_open_of_a_host_directory_is_refused(self):
        """C3: os.open(name, dir_fd=<host dir>) resolves against that descriptor, not the cwd: read and write refused.

        Bug caught: judging a dir_fd-relative name against the cwd approved sandbox/victim yet opened host/victim.
        """
        host = self.root / "host_dir"
        host.mkdir()
        victim = host / "victim.txt"
        victim.write_text("host data", encoding="utf-8")
        (self.sandbox / "victim.txt").write_text("decoy", encoding="utf-8")  # the cwd-relative name exists inside
        host_fd = os.open(str(host), os.O_RDONLY | os.O_DIRECTORY)
        sandbox_fd = os.open(str(self.sandbox), os.O_RDONLY | os.O_DIRECTORY)
        original_open = os.open
        try:
            with driver._guarded(self.sandbox) as guard:
                with self.assertRaises(PermissionError):
                    os.open("victim.txt", os.O_RDONLY, dir_fd=host_fd)
                with self.assertRaises(PermissionError):
                    os.open("victim.txt", os.O_WRONLY | os.O_TRUNC, dir_fd=host_fd)
                with self.assertRaises(PermissionError):
                    os.open("../victim.txt", os.O_RDONLY, dir_fd=sandbox_fd)  # climbing out of a sandbox descriptor
                fd = os.open("victim.txt", os.O_RDONLY, dir_fd=sandbox_fd)  # the same name below the sandbox is fine
                self.assertEqual(os.read(fd, 10), b"decoy")
                os.close(fd)
            self.assertEqual(guard.violations, ["open_read", "open_write", "open_read"])
            self.assertIs(os.open, original_open)  # the wrapper is removed with the guard
            self.assertEqual(victim.read_text(encoding="utf-8"), "host data")
        finally:
            os.close(host_fd)
            os.close(sandbox_fd)

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
