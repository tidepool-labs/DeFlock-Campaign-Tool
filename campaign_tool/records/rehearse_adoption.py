"""Stage 7: rehearse catalogue and adoption in a sandbox, from one synthetic mail export to one held message.

The real ``delta`` and ``adoption`` stage adapters run on a fictional export (e2e_proof's account and folder), then the
sandbox ledger is checked by SHA-256. A control changes one byte of a second copy after its receipt is written; the real
adoption stage must refuse it (ADOPTION_NOT_REFUSED if it adopts). A send or any socket connection (loopback included) fails the driver.

The stages start ``adopt-mail`` child interpreters, which the in-process fence cannot see. Every child (and its own children) therefore runs
under an audit hook found through PYTHONPATH: it refuses and records a socket use or a write outside the sandbox (an open, a rename, a
SQLite connection, and an ATTACH or VACUUM INTO on any connection, which SQLite opens in native code and which therefore go through a
connection authorizer the child cannot lift), and records each start in ``child-audit.jsonl``; ``no_sends`` fails on any such record and when
no child was audited. The fence is for the repository's own ``adopt-mail`` children (their start is checked against ``CHILD_MODULES``): it
catches an accidental write or send, it is not isolation from deliberately hostile code (ctypes or a native extension are not judged).
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
from urllib.parse import quote
from unittest.mock import patch

from . import e2e_proof, run_orchestrator
from .run_stages import SANDBOX_MARKER

STAGES = ("7",)
TITLE = "Catalogue and adoption: one synthetic mail export through delta and adoption"

_RUN = "run_20261115T120000Z_00000001"
_NOW = datetime(2026, 11, 15, 12, 0, 0, tzinfo=timezone.utc)
_MARKER_TEXT = b"e2e-proof sandbox\n"  # the text e2e_proof's own sandbox guard requires
_ATTACHMENT = b"Synthetic attachment.\n"
CHILD_LOG = "child-audit.jsonl"
CHILD_MODULES = ("campaign_tool.records",)  # the only interpreters a stage may start: ``-m campaign_tool.records adopt-mail``
# Runs first in every child interpreter (PYTHONPATH); refuses and records a socket use or a write outside CHILD_FENCE_ROOT.
_CHILD_HOOK = """
import json, os, sys
from urllib.parse import parse_qs, unquote
_log, _root = os.environ.get("CHILD_FENCE_LOG"), os.environ.get("CHILD_FENCE_ROOT")
if _log and _root:
    _fd = os.open(_log, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    _mask = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND
    _paths = {"os.mkdir": (0,), "os.rename": (0, 1), "os.remove": (0,), "os.rmdir": (0,), "os.link": (0, 1),
              "os.symlink": (1,), "shutil.copyfile": (1,), "shutil.move": (1,)}
    _sockets = ("socket.connect", "socket.bind", "socket.getaddrinfo", "socket.sendto", "socket.sendmsg")

    def _emit(kind, value=None):
        os.write(_fd, (json.dumps([kind, value]) + "\\n").encode())

    def _outside(path):
        target = os.path.realpath(os.path.join(os.getcwd(), os.fsdecode(path)))
        return not (target == os.devnull or target == _root or target.startswith(_root + os.sep))

    def _target(value):
        # SQLite opens and journals its files in native code, with no ``open`` audit event: the database file a connection or an
        # ATTACH / VACUUM INTO would write, when it lies outside the sandbox; None for read-only, in-memory and in-sandbox files.
        name = os.fsdecode(value) if isinstance(value, (str, bytes, os.PathLike)) else ""
        if name.startswith("file:"):
            path, _, query = name[5:].partition("?")
            options = parse_qs(query)
            if options.get("mode") == ["ro"] or options.get("immutable") == ["1"]:
                return None  # read-only: nothing is written or journalled
            if path.startswith("//"):
                path = path[2:]
                path = path[path.index("/"):] if "/" in path else ""
            name = unquote(path)
        return name if name and name != ":memory:" and _outside(name) else None

    def _database(value):
        name = _target(value)
        if name:
            _emit("outside", name)
            raise PermissionError("CHILD_WRITE_OUTSIDE_SANDBOX")

    def _hook(event, args):
        if event == "sqlite3.connect":
            _database(args[0])
        elif event == "sqlite3.enable_load_extension":
            _emit("outside", "sqlite3 load_extension")  # native code the hook cannot judge
            raise PermissionError("CHILD_WRITE_OUTSIDE_SANDBOX")
        elif event == "open" and not isinstance(args[0], int):
            if ((args[2] or 0) & _mask or any(flag in (args[1] or "") for flag in "wax+")) and _outside(args[0]):
                _emit("outside", os.fsdecode(args[0]))
                raise PermissionError("CHILD_WRITE_OUTSIDE_SANDBOX")
        elif event in _paths:
            for index in _paths[event]:
                if isinstance(args[index], (str, bytes, os.PathLike)) and _outside(args[index]):
                    _emit("outside", os.fsdecode(args[index]))
                    raise PermissionError("CHILD_WRITE_OUTSIDE_SANDBOX")
        elif event in _sockets:
            _emit("socket", event)
            raise OSError("CHILD_SOCKET")

    _emit("start", sys.orig_argv[1:6])
    sys.addaudithook(_hook)

    # A connection may open a second database later (ATTACH, VACUUM INTO): SQLite does that in native code with no audit event, but
    # it asks the connection's authorizer first. Every connection a child makes therefore carries an authorizer that refuses an
    # outside file; a child's own set_authorizer is kept and consulted after ours, so it cannot lift the fence.
    import sqlite3

    class _Guard:
        _user = None

        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            super().set_authorizer(self._judge)

        def _judge(self, action, arg1, arg2, database, source):
            if action == sqlite3.SQLITE_ATTACH and _target(arg1):
                _emit("outside", _target(arg1))
                return sqlite3.SQLITE_DENY
            return self._user(action, arg1, arg2, database, source) if self._user else sqlite3.SQLITE_OK

        def set_authorizer(self, callback):
            self._user = callback

    _made, _plain, _connect = {}, sqlite3.Connection, sqlite3.connect

    def _guarded(factory):
        factory = factory or _plain
        return factory if issubclass(factory, _Guard) else _made.setdefault(factory, type("Guarded", (_Guard, factory), {}))

    def _guarded_connect(database, *args, **kwargs):
        if len(args) > 4:  # factory given by position
            args = args[:4] + (_guarded(args[4]),) + args[5:]
        else:
            kwargs["factory"] = _guarded(kwargs.get("factory"))
        return _connect(database, *args, **kwargs)

    sqlite3.connect = sqlite3.dbapi2.connect = _guarded_connect
    sqlite3.Connection = sqlite3.dbapi2.Connection = _guarded(_plain)
"""


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


def _claim(path):
    """The sandbox, created only below real directories: no path component may be a symlink, and nothing is made before that is known."""
    path = Path(os.path.abspath(path))
    if any(part.is_symlink() for part in (path, *path.parents)):
        raise ValueError("SANDBOX_INVALID")
    if not path.exists():
        if not path.parent.is_dir():
            raise ValueError("SANDBOX_INVALID")
        path.mkdir(mode=0o700)
    return _empty_sandbox(path)


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
    # A URI path must be percent-encoded: a bare ``#`` or ``?`` in a valid sandbox path would name another file or drop mode=ro.
    db = sqlite3.connect("file:%s?mode=ro" % quote(str(project / "processing-v1" / "ledger.sqlite"), safe="/"), uri=True)
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


def _children(sandbox):
    """The child audit records ([kind, value] rows), or [] when no child has been audited."""
    path = sandbox / CHILD_LOG
    if not path.is_file():
        return []
    try:
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    except ValueError:
        raise _StepFailed("CHILD_AUDIT_UNREADABLE") from None


def _child_violation(sandbox):
    """The fixed code for the first refused child action in the audit, else None."""
    try:
        kinds = [kind for kind, *_ in _children(sandbox)]
    except _StepFailed as failed:
        return failed.args[0]
    return "SEND_ATTEMPTED" if "socket" in kinds else "CHILD_WROTE_OUTSIDE_SANDBOX" if "outside" in kinds else None


def _no_sends(c):
    if c.attempts or _child_violation(c.sandbox) == "SEND_ATTEMPTED":
        raise _StepFailed("SEND_ATTEMPTED")
    if _child_violation(c.sandbox):
        raise _StepFailed(_child_violation(c.sandbox))
    starts = [value for kind, value in _children(c.sandbox) if kind == "start"]
    if not starts:
        raise _StepFailed("CHILD_NOT_AUDITED")  # the stages ran no audited child: the child boundary was not observed
    if any(argv[:2] != ["-B", "-m"] or argv[2] not in CHILD_MODULES or "adopt-mail" not in argv for argv in starts):
        raise _StepFailed("CHILD_UNEXPECTED")
    return [c.sandbox / CHILD_LOG]


@contextmanager
def _fenced(attempts, sandbox):
    """Record and refuse any notification or socket connection (loopback and local SMTP relays included); keep temp files and HOME inside."""
    def refuse(label):
        attempts.append(label)
        raise OSError(label)
    def connect(self, address, *args):
        refuse("connect")  # every address: a loopback or local-socket SMTP relay would send mail just the same
    def resolve(host, *args, **kwargs):
        if host not in (None, "localhost", "127.0.0.1", "::1"):
            refuse("resolve")
        return real_resolve(host, *args, **kwargs)

    real_resolve, tmp = socket.getaddrinfo, sandbox / "tmp"
    tmp.mkdir(mode=0o700)
    hook = tmp / "child-hook"
    hook.mkdir(mode=0o700)
    (hook / "sitecustomize.py").write_text(_CHILD_HOOK, encoding="utf-8")
    path = os.pathsep.join(filter(None, (str(hook), os.environ.get("PYTHONPATH"))))
    with (patch.object(socket.socket, "connect", connect), patch.object(socket, "getaddrinfo", resolve),
          patch.object(run_orchestrator, "cmd_notify_fallback", lambda *a, **k: refuse("notify")),
          patch.object(tempfile, "tempdir", str(tmp)),
          patch.dict(os.environ, TMPDIR=str(tmp), HOME=str(sandbox), PYTHONPATH=path,
                     CHILD_FENCE_LOG=str(sandbox / CHILD_LOG), CHILD_FENCE_ROOT=str(sandbox.resolve()))):
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
                step["code"] = (error.args[0] if name == "no_sends" else _child_violation(sandbox)) or error.args[0]
            except (OSError, ValueError, KeyError, TypeError, sqlite3.Error, e2e_proof.ProofError):
                # Do not print a dependency exception: it may carry message text or private paths.
                step["code"] = "SEND_ATTEMPTED" if c.attempts else _child_violation(sandbox) or "ADOPTION_" + name.upper() + "_ERROR"
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
        sandbox = _claim(args.sandbox)
    except (OSError, ValueError):
        parser.error("--sandbox must be an absent path or an empty private directory")
    result = run(sandbox)
    print(json.dumps(result, sort_keys=True))
    return int(bool(result["codes"] or result["manual_steps"]))


if __name__ == "__main__":
    raise SystemExit(main())
