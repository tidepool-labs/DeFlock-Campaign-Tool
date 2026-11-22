"""Rehearse A5's decision apply offline, in a dedicated host process.

The synthetic trust context must never share a process with live approvals.
"""

import argparse
import fcntl
import hashlib
import io
import json
import os
import subprocess
import tempfile
from contextlib import redirect_stderr, redirect_stdout
from functools import partial
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from .. import approval
from . import approval_queue as aq
from . import console_apply, console_journal, rehearse_anchor, rehearsal_trust, run_stages


STAGES = ("A5",)
TITLE = "Console decision journal apply"
_RUN_LOCK = "run-records.lock"
_RUN = "synthetic-a5-run"


def _apply_locked(project, journal):
    """Use the runtime wrapper's nonblocking flock protocol on a sandbox lock.

    The live wrapper, not console_apply, owns this outer lock. Exercise the
    unchanged apply command without invoking the deployment wrapper or Docker.
    """
    fd = os.open(project.parent / _RUN_LOCK,
                 os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            rc = console_apply.main(["--project", str(project), "--from-journal", str(journal)])
        return rc, stdout.getvalue(), stderr.getvalue()
    finally:
        os.close(fd)


def _empty_sandbox(path):
    rehearsal_trust.require_private(path)
    if any(path.iterdir()):
        raise ValueError("sandbox must be empty")


def _require(condition):
    if not condition:
        raise ValueError("unexpected rehearsal state")


def _snapshot(directory):
    # Ignore atime (reads can change it), but catch even byte-identical rewrites.
    return {path: (path.stat().st_mtime_ns, path.read_bytes() if path.is_file() else None)
            for path in (directory, *directory.rglob("*"))}


def run(sandbox: Path):
    """Apply one signed synthetic decision and replay it without changing state."""
    sandbox = rehearsal_trust.sandbox_path(sandbox)
    _empty_sandbox(sandbox)
    marker = sandbox / run_stages.SANDBOX_MARKER
    project = sandbox / "project"
    decisions = sandbox / "decisions"
    journal = decisions / console_journal.FILE
    queue = project / "approvals" / "queue.jsonl"
    receipt = queue.with_name(console_apply.FILE)
    row: dict
    entry_id: str
    signers: Path

    def init_project():
        rehearse_anchor._write_private(marker, '{"sandbox_marker_version":1}\n')
        project.mkdir(mode=0o700)
        decisions.mkdir(mode=0o700)
        return (marker,)

    def queue_draft():
        nonlocal row
        row = aq.add_draft(project, item_id="synthetic-a5", kind="draft-agency-summary",
                           agency_id="synthetic-agency", request_id="synthetic-request",
                           body="Synthetic rehearsal only. Nothing is sent or authorized.\n",
                           evidence={"source": "synthetic"}, source_run_id=_RUN)
        _require((row["status"], row["revision"]) == ("pending", 0))
        return ()  # The queue file is mutable until apply has run.

    def journal_decision():
        nonlocal entry_id, signers
        (signers,) = rehearse_anchor.make_signer(sandbox)
        payload = aq.approval_payload(project, row["id"])
        signature = rehearse_anchor._ssh(sandbox, "-Y", "sign", "-n", approval.NAMESPACE,
                                         "-f", str(sandbox / "trusted"), data=payload).decode("ascii")
        pin = SimpleNamespace(source_project_id=aq._project_id(project), run_id=_RUN, name="synthetic-a5-export")
        entry_id = console_journal.append(decisions, pin, "rehearsal", "/approvals/approve", "approve",
                                          row["id"], row["revision"], signature)
        return (journal,)

    def expect_apply(outcome):
        _require(_apply_locked(project, journal) == (0, f"decisions-apply: {entry_id[:12]} approve {outcome}\n", ""))

    def verify_trust(step):
        # Only redirect the verifier's scratch data, not its binary or trust policy.
        workspace = SimpleNamespace(TemporaryDirectory=partial(tempfile.TemporaryDirectory, dir=sandbox))
        with rehearsal_trust.sandbox_anchor(sandbox, signers), patch.object(approval, "tempfile", workspace):
            return step()

    def apply():
        verify_trust(partial(expect_apply, "APPLIED"))
        return ()

    def verify_state():
        (applied,) = verify_trust(lambda: aq.load_queue(project))
        _require((applied["id"], applied["status"], applied["revision"], applied.get("approved_by")) ==
                 (row["id"], "approved", row["revision"] + 1, rehearse_anchor._PRINCIPAL))
        _require(console_apply.ledger(project) == ({entry_id: "APPLIED"}, {}))
        return (queue, receipt)

    def reapply_idempotent():
        before = _snapshot(sandbox)
        # Keep even a faulty replay within the synthetic trust boundary.
        verify_trust(partial(expect_apply, "ALREADY"))
        _require(_snapshot(sandbox) == before)
        return ()

    result = {"stages": list(STAGES), "steps": [], "manual_steps": [], "codes": []}
    for step in (init_project, queue_draft, journal_decision, apply, verify_state, reapply_idempotent):
        name = step.__name__
        try:
            outputs = [{"path": path.relative_to(sandbox).as_posix(), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
                       for path in step()]
        except (OSError, ValueError, subprocess.SubprocessError, approval.ApprovalError,
                aq.ApprovalQueueError, console_journal.JournalError):
            code = name.upper() + "_FAILED"
            result["steps"].append({"name": name, "code": code, "outputs": []})
            result["codes"].append(code)
            break
        result["steps"].append({"name": name, "code": "OK", "outputs": outputs})
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sandbox", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        sandbox = rehearsal_trust.sandbox_path(args.sandbox)
        sandbox.mkdir(mode=0o700, exist_ok=True)
        _empty_sandbox(sandbox)
    except (OSError, ValueError):
        parser.error("--sandbox must be a new or empty private 0700 directory")
    result = run(sandbox)
    print(json.dumps(result))
    return int(bool(result["codes"] or result["manual_steps"]))


if __name__ == "__main__":
    raise SystemExit(main())
