"""Stage 19 rehearsal on an invented project; never a live quiescence attestation."""

import argparse
import hashlib
import io
import json
import os
import re
import sqlite3
import stat
import time
from contextlib import closing, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from . import approval_queue, quiescence_audit, records_backup
from .recovery import backup_v2, records_profile
from .run_stages import SANDBOX_MARKER

STAGES = ("19",)
TITLE = "Backup, verify and isolated restore"


class _Refused(ValueError):
    pass


def _mark_sandbox(sandbox):
    """Create the owner-only marker before seeding any inputs."""
    fd = os.open(sandbox / SANDBOX_MARKER, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.close(fd)


def _write(path, data):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as out:
        out.write(data)


def _sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _seed_project(project):
    project.mkdir(mode=0o700)
    raw = b"synthetic-stage-19-original\n"
    digest = hashlib.sha256(raw).hexdigest()
    blob = project / "processing-v1/intake/blobs" / digest
    receipt_bytes = b'{"synthetic":true}\n'
    receipt_hash = hashlib.sha256(receipt_bytes).hexdigest()
    receipt = project / "processing-v1/intake/document-receipts" / receipt_hash
    run_id, timestamp = "run_20260101T060000Z_0a1b2c3d", "2026-01-01T06:00:00Z"
    for directory in (project / "processing-v1", blob.parent.parent, blob.parent, receipt.parent):
        directory.mkdir(mode=0o700)
    _write(blob, raw)
    _write(receipt, receipt_bytes)

    for rel in (records_profile.LEDGER, records_profile.INTAKE):
        path = project / rel
        _write(path, b"")
        with closing(sqlite3.connect(path)) as con:
            scripts = [sql for _, required, group in records_profile.SCHEMAS[rel]
                       if required or rel == records_profile.LEDGER for sql in group]
            con.executescript("BEGIN;\n" + "\n".join(scripts))
            if rel == records_profile.LEDGER:
                con.execute("INSERT INTO runs(run_id,kind,started_at,engine_version,status,summary) "
                            "VALUES(?,'synthetic',?,'rehearsal','done','{}')", (run_id, timestamp))
                con.execute("INSERT INTO originals(sha256,bytes,role,scope,storage_path,preservation_status,"
                            "provenance_json) VALUES(?,?,'document','records',?,'preserved','{}')",
                            (digest, len(raw), str(blob)))
                manifest = json.dumps({"artifacts": {"source_receipt": {"path": str(receipt)}}})
                con.execute("INSERT INTO extraction_adapter_imports VALUES(?,?,?,?,?,?,?)",
                            ("synthetic", digest, receipt_hash, hashlib.sha256(manifest.encode()).hexdigest(),
                             manifest, run_id, timestamp))
            # SQLite's backup into a fresh database sets the schema cookie to 1.
            # Seed in one transaction with that same cookie, so this closed fixture
            # permits a strict byte comparison without post-snapshot normalization.
            con.execute("PRAGMA schema_version=1")
            con.commit()

    approval_queue.add_draft(str(project), item_id="1" * 64, kind="reply-extension",
                             agency_id="example-agency", request_id="synthetic-19",
                             body="Synthetic stage 19 unsent draft", evidence={"findings": [], "deadlines": []},
                             source_run_id=run_id)
    return {rel: _sha(project / rel) for rel in (records_profile.LEDGER, records_profile.INTAKE,
            "approvals/queue.jsonl", str(blob.relative_to(project)), str(receipt.relative_to(project)))}


def _capture_units(path):
    blocks = [f"Captured={int(time.time())}", *(f"Id={unit}\nActiveState=inactive\nSubState=dead"
                                                for unit in quiescence_audit.WRITER_UNITS)]
    _write(path, ("\n\n".join(blocks) + "\n").encode())


def _backup_command(command, args):
    out = io.StringIO()
    with redirect_stdout(out):
        status = command(args)
    report = json.loads(out.getvalue())
    code = report["code"]
    if status != 0 and isinstance(code, str) and code != "OK" and re.fullmatch("[A-Z][A-Z0-9_]*", code):
        raise _Refused(code)
    if (status, code) != (0, "OK"):
        raise ValueError("unexpected backup status")
    return report


def _tree_hashes(root):
    return {path.relative_to(root).as_posix(): None if path.is_dir() else _sha(path)
            for path in root.rglob("*")}


def _compare(project, declared, manifest, source, restored):
    rows = json.loads(manifest.read_bytes())["files"]
    live_paths = {row["source"] for row in rows}
    if len(rows) != len(declared) or live_paths != {str(project / rel) for rel in declared}:
        raise _Refused("RESTORE_MISMATCH")
    expected = {"manifest.json": _sha(manifest), "payload": None}
    for row in rows:
        rel = Path(row["source"]).relative_to(project).as_posix()
        expected[row["path"]] = declared[rel]
        if _sha(project / rel) != declared[rel]:
            raise _Refused("RESTORE_MISMATCH")
    if _tree_hashes(source) != expected or _tree_hashes(restored) != expected:
        raise _Refused("RESTORE_MISMATCH")


def run(sandbox):
    """Exercise the real adapters with only sandbox-local writer discovery inputs."""
    sandbox = sandbox.resolve()
    project, runtime, proc, home, units, evidence, sets, restored = (
        sandbox / name for name in ("project", "runtime", "proc", "home", "units.txt", "quiescence.json", "sets", "restored"))
    digests = sets / records_backup.DIGESTS
    declared, source, trusted = {}, sets, ""

    def seed():
        nonlocal declared
        _mark_sandbox(sandbox)
        declared = _seed_project(project)
        for directory in (runtime, proc, home):
            directory.mkdir(mode=0o700)
        _capture_units(units)
        return [sandbox / SANDBOX_MARKER]

    def quiescence():
        # Substitute only the proc filesystem. Audit still checks the capture,
        # sandbox roots, runtime lock and arrival flag, and writes real evidence.
        with patch.object(quiescence_audit, "PROC_ROOT", str(proc)), redirect_stdout(io.StringIO()):
            status = quiescence_audit.main(["--project", str(project), "--runtime", str(runtime),
                                            "--units-file", str(units), "--out", str(evidence)])
        # Exit 1 means the audit completed and wrote negative evidence. Pass it
        # unchanged so the backup adapter, not the rehearsal, decides admission.
        if status not in (0, 1):
            raise _Refused("QUIESCENCE_AUDIT_FAILED")
        return [units, evidence]

    def backup():
        nonlocal source, trusted
        _backup_command(records_backup.backup_main, ["--project", str(project), "--into", str(sets),
                        "--quiescence", str(evidence), "--quiescence-sha256", _sha(evidence)])
        (source,) = sets.glob("set-*")
        entry = json.loads(digests.read_bytes())
        if entry["set"] != str(source):
            raise _Refused("BACKUP_FAILED")
        trusted = entry["manifest_sha256"]
        return [digests, source / "manifest.json"]

    def verify():
        report = _backup_command(records_backup.verify_main, ["--set", str(source), "--digests", str(digests)])
        receipt = sandbox / "backup-verify.json"
        _write(receipt, (json.dumps({key: report[key] for key in ("code", "files", "sqlite_snapshots")},
                                     sort_keys=True) + "\n").encode())
        return [receipt]

    def restore():
        backup_v2.restore_v2(str(source), str(restored), manifest_sha256=trusted)
        return [restored / "manifest.json"]

    def compare():
        _compare(project, declared, source / "manifest.json", source, restored)
        return []

    result = {"stages": list(STAGES), "steps": [], "manual_steps": [], "codes": []}
    # Both attended adapters discover off-project roots via the account's home.
    # Point that lookup at an empty synthetic home, without changing the environment.
    with patch.object(records_profile, "_home", lambda: str(home)):
        for name, action in (("seed_project", seed), ("quiescence", quiescence), ("backup", backup),
                             ("backup_verify", verify), ("restore", restore), ("compare", compare)):
            try:
                outputs = [{"path": path.relative_to(sandbox).as_posix(), "sha256": _sha(path)} for path in action()]
                code = "OK"
            except _Refused as error:
                code, outputs = str(error), []
            except (OSError, ValueError, KeyError, sqlite3.Error, approval_queue.ApprovalQueueError):
                code, outputs = f"{name.upper()}_FAILED", []
            result["steps"].append({"name": name, "code": code, "outputs": outputs})
            if code != "OK":
                result["codes"].append(code)
                break
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sandbox", type=Path, required=True)
    sandbox = parser.parse_args(argv).sandbox
    try:
        sandbox.mkdir(mode=0o700, exist_ok=True)
        info = sandbox.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or any(sandbox.iterdir()):
            parser.error("--sandbox must be absent or an empty, owner-controlled directory")
        sandbox.chmod(0o700)
    except OSError:
        parser.error("cannot prepare --sandbox directory")
    result = run(sandbox)
    print(json.dumps(result, sort_keys=True))
    return int(bool(result["manual_steps"] or any(step["code"] != "OK" for step in result["steps"])))


if __name__ == "__main__":
    raise SystemExit(main())
