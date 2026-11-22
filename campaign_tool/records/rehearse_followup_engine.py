"""Stage 15: exercise real follow-up drafting in a disposable synthetic project."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat

from . import approval_queue, case_builder, followup_clock, followup_engine, playbook, project_init
from .outcomes import received_items, response_items, timeline
from .run_stages import SANDBOX_MARKER

STAGES = ("15",)
TITLE = "Engine-owned follow-up drafts"

_RUN = "stage-15"
_DAY = "2026-11-06"
_AGENCY = "synthetic-agency"
_DUE = "SYN-DUE"
_WAITING = "SYN-WAITING"
_JOURNAL = Path("approvals/queue.jsonl")


class _StepFailed(Exception):
    """A semantic check failed; the sole argument is a fixed evidence code."""


def _sandbox_path(path):
    path = Path(os.path.abspath(path))
    if any(parent.is_symlink() for parent in (*path.parents, path)):
        raise ValueError("SANDBOX_INVALID")
    return path


def _require_empty(sandbox):
    info = sandbox.stat()
    if (not stat.S_ISDIR(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o700
            or info.st_uid != os.geteuid() or any(sandbox.iterdir())):
        raise ValueError("SANDBOX_INVALID")


def _write_json(path, document):
    path.write_text(json.dumps(document, sort_keys=True) + "\n", encoding="utf-8")


def _init_project(sandbox, project):
    marker = sandbox / SANDBOX_MARKER
    fd = os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    os.close(fd)
    campaign = sandbox / "campaign"
    campaign.mkdir(mode=0o700)
    config = campaign / "campaign.json"
    _write_json(config, {"schema_version": 1, "name": "Synthetic Stage 15", "county": "Fictland", "state": "CA"})
    project_init.init_project(project, campaign, "synthetic-stage-15")
    return [marker, config, project / "campaign.json", project / ".campaign-root"]


def _seed_inputs(project):
    root = project / "run-outputs" / _RUN
    outcomes = root / "outcomes"
    outcomes.mkdir(parents=True)
    # A closed request with a missing tier-one ask is ready now (new request,
    # no mail thread needed). The other has an outstanding ask but is still waiting.
    common = {"agency_key": _AGENCY, "agency": "Synthetic Agency"}
    due = dict(common, request_id=_DUE, closed=True)
    waiting = dict(common, request_id=_WAITING, closed=False,
                   clocks={"state": "running", "due": "2026-11-12"})
    historic_receipt = {"agency_id": _AGENCY, "request_id": _DUE, "received_at": "2026-11-03T12:00:00Z",
                        "item_id": received_items.item_id(hashlib.sha256(b"Synthetic receipt").hexdigest())}
    ratings = [dict(agency_key=_AGENCY, request_id=row["request_id"],
                    asks=[{"ask_id": "ask-1", "text": "ALPR search logs", "rating": "missing"}])
               for row in (due, waiting)]
    inputs = {
        project / followup_clock.SETTINGS_FILE: {
            "schema": followup_clock.SETTINGS_SCHEMA, "default": {"autonomy": "draft"}},
        root / followup_clock.REGISTER_NAME: {
            "schema": followup_clock.REGISTER_SCHEMA, "as_of": _DAY, "requests": [due, waiting]},
        outcomes / "timeline.json": {
            "schema": timeline.SCHEMA, "run_id": _RUN, "as_of": _DAY, "requests": [due, waiting]},
        outcomes / "response-items.json": {
            "schema": response_items.SCHEMA, "run_id": _RUN, "requests": ratings},
        outcomes / "received-items.json": {
            "schema": received_items.SCHEMA, "run_id": _RUN, "items": [historic_receipt]},
    }
    # The candidate engine loads its own shipped playbook; don't substitute a mock or a copy.
    for path, document in inputs.items():
        _write_json(path, document)
    return list(inputs)


def _draft(project):
    result = followup_engine.run(project, _RUN, apply=True)
    if result["queued"] == 0:
        raise _StepFailed("FOLLOWUP_DRAFT_MISSING")
    if result["queued"] != 1:
        raise _StepFailed("FOLLOWUP_DRAFT_COUNT")
    return [project / _JOURNAL]


def _queue_check(project):
    journal = project / _JOURNAL
    rows = approval_queue.load_queue(project)
    actual = [(r["kind"], r["status"], r["agency_id"], r["request_id"], r["source_run_id"]) for r in rows]
    events = [(e["event"], e["id"]) for e in map(json.loads, journal.read_bytes().splitlines())]
    # A lone draft event also rules out edits, approvals and sends.
    if (actual != [("followup-playbook", "pending", _AGENCY, _DUE, _RUN)]
            or events != [("draft", rows[0]["id"])]):
        raise _StepFailed("FOLLOWUP_QUEUE_UNEXPECTED")
    return [journal]


def _rerun(project):
    journal = project / _JOURNAL
    before = journal.read_bytes()
    result = followup_engine.run(project, _RUN, apply=True)
    if (result["queued"], result["duplicate"], journal.read_bytes()) != (0, 1, before):
        raise _StepFailed("FOLLOWUP_NOT_IDEMPOTENT")
    return [journal]


def run(sandbox):
    """Write synthetic fixtures and return path/hash evidence, never draft text."""
    sandbox = _sandbox_path(sandbox)
    _require_empty(sandbox)
    project = sandbox / "project"
    result = {"stages": list(STAGES), "steps": [], "manual_steps": [], "codes": []}
    for name, action, args in (
        ("init_project", _init_project, (sandbox, project)),
        ("seed_inputs", _seed_inputs, (project,)),
        ("draft", _draft, (project,)),
        ("queue_check", _queue_check, (project,)),
        ("rerun_idempotent", _rerun, (project,)),
    ):
        step = {"name": name, "code": "OK", "outputs": []}
        try:
            step["outputs"] = [{"path": path.relative_to(sandbox).as_posix(),
                                "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
                               for path in action(*args)]
        except _StepFailed as error:
            step["code"] = error.args[0]
        except (OSError, ValueError, KeyError, sqlite3.Error, project_init.Refused,
                approval_queue.ApprovalQueueError, case_builder.CaseError, playbook.PlaybookError):
            # Do not print a dependency exception: it may contain draft text or paths.
            step["code"] = "FOLLOWUP_" + name.upper() + "_ERROR"
        result["steps"].append(step)
        if step["code"] != "OK":
            result["codes"].append(step["code"])
            break
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sandbox", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        sandbox = _sandbox_path(args.sandbox)
        sandbox.mkdir(mode=0o700, exist_ok=True)
        _require_empty(sandbox)
    except (OSError, ValueError):
        parser.error("--sandbox must be an absent path or an empty private directory")
    result = run(sandbox)
    print(json.dumps(result, sort_keys=True))
    return int(bool(result["codes"] or result["manual_steps"]))


if __name__ == "__main__":
    raise SystemExit(main())
