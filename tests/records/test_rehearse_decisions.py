"""Offline controls for A5's real apply command and its outer run lock."""

import fcntl
import hashlib
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from campaign_tool.records import approval_queue, console_apply, console_journal, rehearse_decisions, run_stages


STEPS = (
    ("init_project", (run_stages.SANDBOX_MARKER,)),
    ("queue_draft", ()),
    ("journal_decision", ("decisions/decisions.jsonl",)),
    ("apply", ()),
    ("verify_state", ("project/approvals/queue.jsonl", "project/approvals/decisions-applied.jsonl")),
    ("reapply_idempotent", ()),
)


def expected(root, failed=None):
    result = {"stages": ["A5"], "steps": [], "manual_steps": [], "codes": []}
    for name, paths in STEPS:
        if name == failed:
            code = f"{name.upper()}_FAILED"
            result["steps"].append({"name": name, "code": code, "outputs": []})
            result["codes"].append(code)
            break
        result["steps"].append({"name": name, "code": "OK", "outputs": [
            {"path": path, "sha256": hashlib.sha256((root / path).read_bytes()).hexdigest()} for path in paths
        ]})
    return result


class DriverTests(unittest.TestCase):
    def sandbox(self):
        return Path(self.enterContext(tempfile.TemporaryDirectory(prefix="a5-"))).resolve()

    def test_c1_c3_cli_applies_once_under_lock_and_hashes_outputs_offline(self):
        """C1: A signed decision lands once. C3: Hashes bind the evidence; nothing is sent."""
        root = self.sandbox() / "new"
        calls = []
        apply = rehearse_decisions._apply_locked

        def observe(project, journal):
            result = apply(project, journal)
            calls.append((result, (project / "approvals" / "queue.jsonl").read_bytes(),
                          (project / "approvals" / console_apply.FILE).read_bytes(), journal.read_bytes()))
            return result

        out = io.StringIO()
        # Refuse both transports before any connection; assert no attempt even if caught.
        with redirect_stdout(out), patch.object(rehearse_decisions, "_apply_locked", side_effect=observe), \
                patch("socket.socket", side_effect=AssertionError("network forbidden")) as network, \
                patch("campaign_tool.outbox.send_email", side_effect=AssertionError("mail forbidden")) as mail:
            self.assertEqual(rehearse_decisions.main(["--sandbox", str(root)]), 0)
        network.assert_not_called()
        mail.assert_not_called()
        self.assertEqual(json.loads(out.getvalue()), expected(root))
        project = root / "project"
        (entry,) = console_journal.read(root / "decisions")
        self.assertEqual((entry["action"], entry["revision_seen"], entry["route"]),
                         ("approve", 0, "/approvals/approve"))
        prefix = f"decisions-apply: {entry['entry_id'][:12]} approve "
        first, second = calls
        self.assertEqual(first[0], (0, prefix + "APPLIED\n", ""))
        self.assertEqual(second, ((0, prefix + "ALREADY\n", ""), *first[1:]))
        self.assertEqual(console_apply.ledger(project), ({entry["entry_id"]: "APPLIED"}, {}))
        _, approved = map(json.loads, first[1].splitlines())
        self.assertEqual((approved["id"], approved["revision"], approved["event"], approved["approval"]["signature"]),
                         (entry["target_id"], 1, "approve", entry["value"]))

    def test_c1_stops_at_failed_step(self):
        """C1: No later step may run after a failed writer, apply, or receipt check."""
        driver = "campaign_tool.records.rehearse_decisions."
        for step, fault in (
            ("init_project", patch(driver + "rehearse_anchor._write_private", side_effect=OSError)),
            ("queue_draft", patch(driver + "aq.add_draft", side_effect=approval_queue.ApprovalQueueError("queue_io"))),
            ("journal_decision", patch(driver + "console_journal.append", side_effect=console_journal.JournalError)),
            ("apply", patch(driver + "console_apply.settle", return_value=None)),
            ("verify_state", patch(driver + "console_apply.ledger", side_effect=[({}, {}), ({}, {})])),
        ):
            with self.subTest(step=step), fault:
                root = self.sandbox()
                self.assertEqual(rehearse_decisions.run(root), expected(root, step))

    def test_c1_cli_rejects_replay_that_rewrites_receipt(self):
        """C1: Even an ALREADY replay with unchanged bytes must not rewrite the receipt."""
        apply = rehearse_decisions._apply_locked

        def rewrite(project, journal):
            result = apply(project, journal)
            if result[1].endswith(" ALREADY\n"):
                path = project / "approvals" / console_apply.FILE
                info = path.stat()
                os.utime(path, ns=(info.st_atime_ns, info.st_mtime_ns + 1_000_000_000))
            return result

        root, out = self.sandbox(), io.StringIO()
        with patch.object(rehearse_decisions, "_apply_locked", side_effect=rewrite), redirect_stdout(out):
            self.assertEqual(rehearse_decisions.main(["--sandbox", str(root)]), 1)
        self.assertEqual(json.loads(out.getvalue()), expected(root, "reapply_idempotent"))

    def test_c3_cli_refuses_existing_data_without_touching_it(self):
        """C3: Neither a populated nor an unsafe directory may receive rehearsal writes."""
        root = self.sandbox()
        occupied = root / "occupied"
        occupied.mkdir(mode=0o700)
        (occupied / "keep").write_bytes(b"synthetic existing data")
        unsafe = root / "unsafe"
        unsafe.mkdir(mode=0o755)
        unsafe.chmod(0o755)  # Do not let a private worker umask turn this into a valid sandbox.
        alias = root / "alias"
        alias.symlink_to(unsafe)
        for argv in ([], *(["--sandbox", str(path)] for path in (occupied, unsafe, alias))):
            with self.subTest(argv=argv), redirect_stderr(io.StringIO()), redirect_stdout(io.StringIO()) as out:
                with self.assertRaises(SystemExit) as caught:
                    rehearse_decisions.main(argv)
                self.assertEqual((type(caught.exception), caught.exception.code, out.getvalue()), (SystemExit, 2, ""))
        self.assertEqual({p.name: p.read_bytes() for p in occupied.iterdir()}, {"keep": b"synthetic existing data"})
        self.assertEqual(list(unsafe.iterdir()), [])


class RehearseDecisionsTests(unittest.TestCase):
    def setUp(self):
        tmp = self.enterContext(tempfile.TemporaryDirectory(prefix="rehearse-decisions-"))
        self.sandbox = Path(tmp).resolve()
        (self.sandbox / run_stages.SANDBOX_MARKER).touch(mode=0o600)
        self.project = self.sandbox / "project"
        self.project.mkdir(mode=0o700)
        self.journal = self.sandbox / "journal"
        self.journal.mkdir(mode=0o700)
        pin = SimpleNamespace(source_project_id=approval_queue._project_id(self.project),
                              run_id="synthetic-run", name="synthetic-export")
        # The target lookup must refuse even before verification is relevant.
        self.entry_id = console_journal.append(self.journal, pin, "rehearsal", "/approvals/approve", "approve",
                                               "0" * 64, 0, "synthetic signature placeholder")

    def apply(self):
        return rehearse_decisions._apply_locked(self.project, self.journal / console_journal.FILE)

    def test_c1_busy_lock_prevents_apply(self):
        """C1: A busy run lock must prevent even a refusal receipt from being written."""
        with (self.sandbox / "run-records.lock").open("ab") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaises(BlockingIOError) as caught:
                self.apply()
            self.assertIs(type(caught.exception), BlockingIOError)
            self.assertEqual(list(self.project.iterdir()), [])

    def test_c2_unknown_target_is_refused(self):
        """C2: Unknown target must get the unchanged refusal, not success on CLI exit 0."""
        self.assertEqual(self.apply(), (0, f"decisions-apply: {self.entry_id[:12]} approve REFUSED unknown_id\n", ""))
        self.assertEqual(console_apply.ledger(self.project), ({self.entry_id: "REFUSED unknown_id"}, {}))
        self.assertEqual(approval_queue.load_queue(self.project), [])
