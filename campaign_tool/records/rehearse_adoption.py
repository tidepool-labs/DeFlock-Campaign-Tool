"""Stage 7: rehearse catalogue and adoption in a sandbox, from one synthetic mail export to one held message.

The real ``delta`` and ``adoption`` stage adapters run on a fictional export (e2e_proof's account and folder), then the
sandbox ledger is checked by SHA-256. A control changes one byte of a second copy after its receipt is written; the real
adoption stage must refuse it (ADOPTION_NOT_REFUSED if it adopts). A send or non-loopback connection fails the driver.
"""

import argparse
from contextlib import closing, contextmanager
from datetime import datetime, timezone
from email.message import EmailMessage
from email.policy import SMTP
import hashlib
import json
import os
from pathlib import Path
import socket
import sqlite3
import stat
import tempfile
from types import SimpleNamespace
from unittest.mock import patch

from . import e2e_proof, run_orchestrator
from .run_stages import SANDBOX_MARKER

STAGES = ("7",)
TITLE = "Catalogue and adoption: one synthetic mail export through delta and adoption"

_RUN = "run_20261115T120000Z_00000001"
_NOW = datetime(2026, 11, 15, 12, 0, 0, tzinfo=timezone.utc)
_MARKER_TEXT = b"e2e-proof sandbox\n"  # the text e2e_proof's own sandbox guard requires
_ATTACHMENT = b"Synthetic attachment.\n"


class _StepFailed(Exception):
    """A semantic check failed; the sole argument is a fixed evidence code."""


def _empty_sandbox(path):
    """The absolute sandbox path, once it is a symlink-free, empty, owner-only directory."""
    path = Path(os.path.abspath(path))
    info = path.stat()
    if (any(p.is_symlink() for p in (*path.parents, path)) or not stat.S_ISDIR(info.st_mode)
            or stat.S_IMODE(info.st_mode) != 0o700 or info.st_uid != os.geteuid() or any(path.iterdir())):
        raise ValueError("SANDBOX_INVALID")
    return path


def _message():
    """The one synthetic message: fictional addresses, a short body and one small text attachment."""
    message = EmailMessage(policy=SMTP)
    message["From"], message["To"] = "agency@sandbox.example.invalid", e2e_proof.ADOPTION_ACCOUNT
    message["Subject"], message["Date"] = "Synthetic reply", "Sat, 14 Nov 2026 12:00:00 +0000"
    message["Message-ID"] = "<synthetic-1@sandbox.example.invalid>"
    message.set_content("Synthetic body.\n")
    message.add_attachment(_ATTACHMENT, maintype="text", subtype="plain", filename="synthetic-record.txt")
    return message.as_bytes()


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def _marker(directory):
    path = directory / SANDBOX_MARKER
    path.touch(mode=0o600, exist_ok=False)
    path.write_bytes(_MARKER_TEXT)
    return path


def _export(base, raw):
    """A marked ``<base>/project`` (the e2e guard needs that name) holding e2e_proof's own export of ``raw``.

    Returns (project, marker, [config, receipt, message object, attachment object])."""
    project = base / "project"
    project.mkdir(mode=0o700, parents=True)
    marker = _marker(project)
    try:
        e2e_proof._prepare_adoption(project, raw, _NOW)
    except e2e_proof.ProofError as error:
        raise _StepFailed(error.code) from None
    mail = project / "mail-history"
    return project, marker, [project / e2e_proof.ADOPTION_CONFIG, *(mail / "receipts").glob("*.json"),
                             *(mail / "objects" / _sha(data)[:2] / _sha(data) for data in (raw, _ATTACHMENT))]


def _stage(project, name, *extra):
    """Run one real stage adapter in-process behind e2e_proof's sandbox guard -> (status, code, detail)."""
    try:
        e2e_proof._adoption_guard(project)
        return e2e_proof._adapter(name, project, _RUN, *extra)
    except e2e_proof.ProofError as error:
        raise _StepFailed(error.code) from None


def _ledger(project):
    """(mail_messages rows, original hashes) read from the sandbox ledger without writing."""
    db = sqlite3.connect("file:%s?mode=ro" % (project / "processing-v1" / "ledger.sqlite"), uri=True)
    with closing(db):
        rows = db.execute("SELECT account, folder, eml_sha256 FROM mail_messages").fetchall()
        return rows, {row[0] for row in db.execute("SELECT sha256 FROM originals")}


def _counted(project, name, *extra):
    """OK only when the stage reports output equal to input above zero, and no failed or skipped item."""
    status, code, detail = _stage(project, name, *extra)
    if status != "ok":
        raise _StepFailed(code or name.upper() + "_NOT_OK")
    counts = detail["counts"]
    if not counts["input"] == counts["output"] > 0 or counts["failed"] or counts["skipped"]:
        raise _StepFailed(name.upper() + "_COUNTS_UNEXPECTED")


def _prepare_export(c):
    c.project, marker, files = _export(c.sandbox, c.raw)
    return [c.root_marker, marker, *files]


def _delta(c):
    _counted(c.project, "delta")
    return []


def _adoption(c):
    _counted(c.project, "adoption", "--config", str(c.project / e2e_proof.ADOPTION_CONFIG))
    return [c.project / "processing-v1" / "ledger.sqlite"]


def _message_held(c):
    rows, originals = _ledger(c.project)
    if rows != [(e2e_proof.ADOPTION_ACCOUNT, e2e_proof.ADOPTION_FOLDER, _sha(c.raw))] \
            or originals != {_sha(c.raw), _sha(_ATTACHMENT)}:
        raise _StepFailed("MESSAGE_NOT_HELD")
    return [c.project / "processing-v1" / "ledger.sqlite"]


def _flip(path):
    data = bytearray(path.read_bytes())
    data[0] ^= 0x01
    path.write_bytes(bytes(data))


def _tampered_export_refused(c):
    """Control: change one byte of the exported message after its receipt is written; the real stage must refuse."""
    project, _, (_, receipt, eml, _) = _export(c.sandbox / "control", c.raw)
    _flip(eml)
    status, code, detail = _stage(project, "adoption", "--config", str(project / e2e_proof.ADOPTION_CONFIG))
    if detail["counts"]["output"] or _ledger(project)[0]:
        raise _StepFailed("ADOPTION_NOT_REFUSED")  # the stage adopted bytes that no longer match their receipt
    if (status, code) != ("fail", "ADOPTION_BLOCKED") \
            or "GB_SOURCE_HASH_MISMATCH" not in [row["code"] for row in detail["outcomes"]]:  # the stage's own refusal
        raise _StepFailed("ADOPTION_REFUSAL_UNEXPECTED")  # nothing adopted, but not refused for the changed byte
    return [receipt, eml]


def _no_sends(c):
    if c.attempts:
        raise _StepFailed("SEND_ATTEMPTED")
    return []


@contextmanager
def _fenced(attempts, sandbox):
    """Record and refuse any notification or non-loopback connection (SMTP included); keep temp files and HOME inside."""
    def refuse(label):
        attempts.append(label)
        raise OSError(label)
    def connect(self, address, *args):
        if self.family != socket.AF_UNIX and not (isinstance(address, tuple) and address[0] in ("127.0.0.1", "::1")):
            refuse("connect")
        return real_connect(self, address, *args)
    def resolve(host, *args, **kwargs):
        if host not in (None, "localhost", "127.0.0.1", "::1"):
            refuse("resolve")
        return real_resolve(host, *args, **kwargs)

    real_connect, real_resolve, tmp = socket.socket.connect, socket.getaddrinfo, sandbox / "tmp"
    tmp.mkdir(mode=0o700)
    with (patch.object(socket.socket, "connect", connect), patch.object(socket, "getaddrinfo", resolve),
          patch.object(run_orchestrator, "cmd_notify_fallback", lambda *a, **k: refuse("notify")),
          patch.object(tempfile, "tempdir", str(tmp)), patch.dict(os.environ, TMPDIR=str(tmp), HOME=str(sandbox))):
        yield


def run(sandbox):
    """Write synthetic fixtures and return path/hash evidence, never message text."""
    sandbox = _empty_sandbox(sandbox)
    c = SimpleNamespace(sandbox=sandbox, raw=_message(), attempts=[], project=None, root_marker=_marker(sandbox))
    result = {"stages": list(STAGES), "steps": [], "manual_steps": [], "codes": []}
    with _fenced(c.attempts, sandbox):
        for action in (_prepare_export, _delta, _adoption, _message_held, _tampered_export_refused, _no_sends):
            name = action.__name__.lstrip("_")
            step = {"name": name, "code": "OK", "outputs": []}
            try:
                step["outputs"] = [{"path": path.relative_to(sandbox).as_posix(), "sha256": _sha(path.read_bytes())}
                                   for path in action(c)]
            except _StepFailed as error:
                step["code"] = error.args[0]
            except (OSError, ValueError, KeyError, TypeError, sqlite3.Error, e2e_proof.ProofError):
                # Do not print a dependency exception: it may carry message text or private paths.
                step["code"] = "SEND_ATTEMPTED" if c.attempts else "ADOPTION_" + name.upper() + "_ERROR"
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
        args.sandbox.mkdir(mode=0o700, exist_ok=True)
        sandbox = _empty_sandbox(args.sandbox)
    except (OSError, ValueError):
        parser.error("--sandbox must be an absent path or an empty private directory")
    result = run(sandbox)
    print(json.dumps(result, sort_keys=True))
    return int(bool(result["codes"] or result["manual_steps"]))


if __name__ == "__main__":
    raise SystemExit(main())
