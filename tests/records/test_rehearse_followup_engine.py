"""Offline runtime contracts for the stage 15 rehearsal."""

from contextlib import redirect_stderr, redirect_stdout
import hashlib
from io import StringIO
import json
from pathlib import Path
import stat
import tempfile
import unittest
from unittest import mock

from campaign_tool.records import approval_queue, followup_route, mail_drafts, mail_send
from campaign_tool.records import rehearse_followup_engine as driver
from campaign_tool.records.run_stages import SANDBOX_MARKER


RUN = "project/run-outputs/stage-15"
JOURNAL = "project/approvals/queue.jsonl"
OUTPUTS = {
    "init_project": [SANDBOX_MARKER, "campaign/campaign.json", "project/campaign.json", "project/.campaign-root"],
    "seed_inputs": ["project/followup-settings.json", f"{RUN}/CURRENT-REQUEST-REGISTER.json",
                    f"{RUN}/outcomes/timeline.json", f"{RUN}/outcomes/response-items.json",
                    f"{RUN}/outcomes/received-items.json"],
    "preview": [],
    "draft": [JOURNAL], "queue_check": [JOURNAL], "rerun_idempotent": [JOURNAL],
}


class RehearsalTests(unittest.TestCase):
    def setUp(self):
        # Use the physical temp root, as the records readers reject symlink ancestry.
        root = self.enterContext(tempfile.TemporaryDirectory(dir=Path(tempfile.gettempdir()).resolve()))
        self.root = Path(root)
        self.sandbox = self.root / "sandbox"
        for target in ("socket.socket", "subprocess.Popen"):
            self.enterContext(mock.patch(target, side_effect=AssertionError(f"unexpected {target}")))
        for module, name in ((mail_drafts, "place"), (mail_send, "send_one")):
            self.enterContext(mock.patch.object(module, name, side_effect=AssertionError(f"unexpected {name}")))

    def expected(self, failure_at=None, code=None):
        steps = []
        for name, paths in OUTPUTS.items():
            if name == failure_at:
                steps.append(dict(name=name, code=code, outputs=[]))
                break
            steps.append(dict(name=name, code="OK", outputs=[
                dict(path=rel, sha256=hashlib.sha256((self.sandbox / rel).read_bytes()).hexdigest())
                for rel in paths]))
        return dict(stages=["15"], steps=steps, manual_steps=[], codes=[code] if code else [])

    def test_c1_real_routes_and_journal(self):
        """C1: a due request queues once; the waiting request and rerun never append."""
        self.sandbox.mkdir(mode=0o700)
        real_init = driver.project_init.init_project
        real_engine = driver.followup_engine.run

        def init(project, campaign, *args):
            marker = self.sandbox / SANDBOX_MARKER
            self.assertEqual(stat.S_IMODE(marker.stat().st_mode), 0o600)
            self.assertEqual((project, campaign), (self.sandbox / "project", self.sandbox / "campaign"))
            return real_init(project, campaign, *args)

        with mock.patch.object(driver.project_init, "init_project", side_effect=init), \
                mock.patch.object(driver.followup_engine, "run", wraps=real_engine) as engine:
            report = driver.run(self.sandbox)
        self.assertEqual(report, self.expected())
        project = self.sandbox / "project"
        self.assertEqual(engine.call_args_list, [mock.call(project, "stage-15", apply=False),
                                                 mock.call(project, "stage-15", apply=True),
                                                 mock.call(project, "stage-15", apply=True)])
        self.assertEqual([(r["request_id"], r["handled_by"], r["planned_date"])
                          for r in followup_route.route_run(project, "stage-15")],
                         [("SYN-DUE", "engine", "2026-11-06"), ("SYN-WAITING", "none", None)])
        draft, = approval_queue.load_queue(project)
        self.assertEqual((draft["request_id"], draft["kind"], draft["status"], draft["revision"]),
                         ("SYN-DUE", "followup-playbook", "pending", 0))
        event, = map(json.loads, (self.sandbox / JOURNAL).read_bytes().splitlines())
        self.assertEqual((event["event"], event["id"], event["revision"]), ("draft", draft["id"], 0))
        # Folded rows alone could hide an extra journal mutation on the second pass.
        outputs = {step["name"]: step["outputs"] for step in report["steps"]}
        self.assertEqual(outputs["draft"], outputs["rerun_idempotent"])
        evidence = draft["evidence"]
        self.assertEqual((evidence["asks_for"], evidence["pattern"], evidence["vehicle"],
                          evidence["planned_send_date"], evidence["source"]),
                         ([1], "closed_new_request", "new_request", "2026-11-06", "followup_engine"))

        # Regression: a valid pending draft with the right IDs and one event can still
        # evidence the wrong ask. Change only that field before the real queue readback.
        self.sandbox = self.root / "wrong-ask"
        self.sandbox.mkdir(mode=0o700)
        journal = self.sandbox / JOURNAL

        def replace_ask_after_draft(*args, **kwargs):
            summary = real_engine(*args, **kwargs)
            if summary["queued"]:
                event, = map(json.loads, journal.read_bytes().splitlines())
                event["snapshot"]["evidence"]["asks_for"] = [2]
                journal.write_text(json.dumps(event) + "\n", encoding="utf-8")
            return summary

        with mock.patch.object(driver.followup_engine, "run", side_effect=replace_ask_after_draft) as engine:
            report = driver.run(self.sandbox)
        self.assertEqual(report["codes"], ["FOLLOWUP_DRAFT_EVIDENCE_MISMATCH"])
        self.assertEqual(report, self.expected("queue_check", "FOLLOWUP_DRAFT_EVIDENCE_MISMATCH"))
        project = self.sandbox / "project"
        self.assertEqual(engine.call_args_list, [mock.call(project, "stage-15", apply=False),
                                                 mock.call(project, "stage-15", apply=True)])

    def test_c1_preview_stops_before_draft(self):
        """C1: a healthy preview count must not conceal an actual journal append."""
        real_engine = driver.followup_engine.run
        real_seed = driver._seed_inputs

        for starting_queue in ("absent", "empty"):
            with self.subTest(starting_queue=starting_queue):
                self.sandbox = self.root / starting_queue
                self.sandbox.mkdir(mode=0o700)
                journal = self.sandbox / JOURNAL

                def seed(project):
                    paths = real_seed(project)
                    if starting_queue == "empty":
                        journal.parent.mkdir(exist_ok=True)
                        journal.touch(mode=0o600)
                    return paths

                def append_on_preview(project, run_id, *, apply):
                    summary = real_engine(project, run_id, apply=apply)
                    if not apply:
                        self.assertEqual((summary["would_queue"], summary["queued"]), (1, 0))
                        # Simulate an ungated append that leaves the preview counters alone.
                        self.assertEqual(real_engine(project, run_id, apply=True)["queued"], 1)
                    return summary

                with mock.patch.object(driver, "_seed_inputs", side_effect=seed), \
                        mock.patch.object(driver.followup_engine, "run", side_effect=append_on_preview) as engine:
                    report = driver.run(self.sandbox)
                self.assertEqual(report["codes"], ["FOLLOWUP_PREVIEW_QUEUE_CHANGED"])
                self.assertEqual(report, self.expected("preview", "FOLLOWUP_PREVIEW_QUEUE_CHANGED"))
                engine.assert_called_once_with(self.sandbox / "project", "stage-15", apply=False)
                self.assertEqual(len(journal.read_bytes().splitlines()), 1)

    def test_c2_remove_due_case(self):
        """C2: losing the only due case gives zero would-queue; never enter draft."""
        real_seed = driver._seed_inputs

        def seed(project):
            paths = real_seed(project)
            for path in paths[1:]:
                doc = json.loads(path.read_bytes())
                key = "items" if path.name == "received-items.json" else "requests"
                doc[key] = [row for row in doc[key] if row["request_id"] != "SYN-DUE"]
                path.write_text(json.dumps(doc), encoding="utf-8")
            return paths

        output = StringIO()
        with mock.patch.object(driver, "_seed_inputs", side_effect=seed), \
                mock.patch.object(driver.followup_engine, "run", wraps=driver.followup_engine.run) as engine, \
                redirect_stdout(output):
            self.assertEqual(driver.main(["--sandbox", str(self.sandbox)]), 1)
        self.assertEqual(json.loads(output.getvalue()), self.expected("preview", "FOLLOWUP_PREVIEW_COUNT"))
        engine.assert_called_once_with(self.sandbox / "project", "stage-15", apply=False)
        self.assertEqual(approval_queue.load_queue(self.sandbox / "project"), [])
        self.assertFalse((self.sandbox / JOURNAL).exists())

    def test_c1_refuse_engine_count_regression(self):
        """C1: a candidate reporting two queued drafts cannot pass the draft step."""
        self.sandbox.mkdir(mode=0o700)
        real_engine = driver.followup_engine.run

        def wrong_draft_count(project, run_id, *, apply):
            return {"queued": 2} if apply else real_engine(project, run_id, apply=False)

        with mock.patch.object(driver.followup_engine, "run", side_effect=wrong_draft_count):
            self.assertEqual(driver.run(self.sandbox), self.expected("draft", "FOLLOWUP_DRAFT_COUNT"))

    def test_c1_refuse_extra_journal_event(self):
        """C1: even a pending row fails if the journal contains more than the draft event."""
        self.sandbox.mkdir(mode=0o700)
        real_engine = driver.followup_engine.run

        def edit_after_draft(project, *args, **kwargs):
            summary = real_engine(project, *args, **kwargs)
            if summary["queued"]:
                row, = approval_queue.load_queue(project)
                approval_queue.edit(project, row["id"], "synthetic", "synthetic edit")
            return summary

        with mock.patch.object(driver.followup_engine, "run", side_effect=edit_after_draft) as engine:
            self.assertEqual(driver.run(self.sandbox), self.expected("queue_check", "FOLLOWUP_QUEUE_UNEXPECTED"))
        self.assertEqual(engine.call_count, 2)

    def test_c1_refuse_changed_journal_on_rerun(self):
        """C1: zero new drafts in the summary cannot conceal a rerun journal mutation."""
        self.sandbox.mkdir(mode=0o700)
        real_engine = driver.followup_engine.run
        previous_outputs = []

        def append_on_rerun(project, *args, **kwargs):
            summary = real_engine(project, *args, **kwargs)
            if summary["duplicate"]:
                row, = approval_queue.load_queue(project)
                approval_queue.reject(project, row["id"], "synthetic", "synthetic rejection")
            elif summary["queued"]:
                previous_outputs.extend(self.expected()["steps"][:-1])
            return summary

        with mock.patch.object(driver.followup_engine, "run", side_effect=append_on_rerun):
            report = driver.run(self.sandbox)
        self.assertEqual(report, dict(stages=["15"], manual_steps=[], codes=["FOLLOWUP_NOT_IDEMPOTENT"],
                                      steps=[*previous_outputs, dict(name="rerun_idempotent",
                                                                     code="FOLLOWUP_NOT_IDEMPOTENT", outputs=[])]))

    def test_c3_cli_json_only(self):
        """C3: CLI returns only path/hash evidence, not the actual rendered letter."""
        output, errors = StringIO(), StringIO()
        with redirect_stdout(output), redirect_stderr(errors):
            self.assertEqual(driver.main(["--sandbox", str(self.sandbox)]), 0)
        self.assertEqual(json.loads(output.getvalue()), self.expected())
        self.assertEqual(errors.getvalue(), "")
        self.assertEqual(stat.S_IMODE(self.sandbox.stat().st_mode), 0o700)
        draft, = approval_queue.load_queue(self.sandbox / "project")
        self.assertTrue(draft["body"].strip())
        self.assertNotIn(draft["body"].splitlines()[0], output.getvalue())

    def test_c3_step_error_is_sanitized(self):
        """C3: a dependency error cannot leak arbitrary text or run later steps."""
        self.sandbox.mkdir(mode=0o700)
        output, errors = StringIO(), StringIO()
        with mock.patch.object(driver.project_init, "init_project", side_effect=OSError("synthetic private text")), \
                redirect_stdout(output), redirect_stderr(errors):
            self.assertEqual(driver.main(["--sandbox", str(self.sandbox)]), 1)
        self.assertEqual(json.loads(output.getvalue()), self.expected("init_project", "FOLLOWUP_INIT_PROJECT_ERROR"))
        self.assertEqual(errors.getvalue(), "")
        self.assertFalse((self.sandbox / "project").exists())

    def test_c3_refuse_unsafe_destinations(self):
        """C3: do not write to nonempty, public, non-directory or symlink paths."""
        self.sandbox.mkdir(mode=0o700)
        sentinel = self.sandbox / "keep"
        sentinel.write_bytes(b"unchanged")
        empty = self.root / "empty"
        empty.mkdir(mode=0o700)
        alias = self.root / "alias"
        alias.symlink_to(empty, target_is_directory=True)
        wide = self.root / "wide"
        wide.mkdir()
        wide.chmod(0o755)  # Do not let the test runner's umask turn the refusal into a valid sandbox.
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
        self.assertEqual(sentinel.read_bytes(), b"unchanged")
        self.assertEqual(list(empty.iterdir()), [])
        self.assertEqual(list(wide.iterdir()), [])
