"""Stages 13 and 14: one synthetic agency reply and one silent request reach the approval queue as drafts.

Everything runs in a disposable sandbox with synthetic data only. The real e2e-proof replay carries one fictional
reply (a fee letter in an attached text-layer PDF) through every records stage, including the real outputs stage. Its
seed names two requests at one fictional agency: the replied one, and one submitted long ago that never got an
answer. The checks then read what the outputs stage queued: exactly one pending reply draft, a passed request clock,
exactly one pending no-response follow-up draft, and no send anywhere. Nothing is sent, posted or published; the
result carries step names, fixed codes and output path/sha256 pairs, never draft text.

While ``run`` works, a process audit hook holds the sandbox boundary. Campaign and host data are read and written only
inside the sandbox. Read-only access to the engine's own shipped code and resources (``campaign_tool/`` and
``schemas/``) and to the Python interpreter's own library is allowed as code. Every engine resource the replay read that
is not Python source is recorded once, with its sha256, in ``engine-resources.json``; a later step that reads one more
fails with ``ENGINE_RESOURCE_UNRECORDED``. Files are created only through the replay's own sandbox writer (new 0600
files; an existing path is refused). The only child process allowed is the engine's
own extraction worker on sandbox paths; the only sockets are loopback, and a listening socket binds port 0. Anything
else is refused before it happens and fails the step with ``SANDBOX_BOUNDARY_VIOLATION``.
"""

import argparse
import contextlib
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from email.policy import SMTP
import errno
import hashlib
import io
import json
import mimetypes
import os
from pathlib import Path
import site
import sqlite3
import stat
import sys
import sysconfig
import threading
import urllib.parse
import zoneinfo

from . import approval_queue, e2e_proof, followup_clock
from .run_stages import SANDBOX_MARKER

STAGES = ("13", "14")
TITLE = "Reply and no-response follow-up drafts reach the approval queue"

AGENCY = "ca-synthetic-valley-pd"
REPLIED = "sv-req-replied"
SILENT = "sv-req-silent"
LABEL = "replay"
REPLY_KINDS = e2e_proof.RECEIVED_DRAFT_KINDS  # drafts the outputs stage queues for a received reply (stage 13)
FOLLOWUP_KINDS = followup_clock.REMINDERS + (followup_clock.NOTICE,)  # no-response drafts (stage 14)
ENGINE_FOLLOWUP = "followup-playbook"  # an engine follow-up would also count as a follow-up draft
QUEUE_CODES = ("OUTPUTS_APPROVAL_QUEUE_UNAVAILABLE", "OUTPUTS_APPROVAL_QUEUE_UNWRITABLE")
SEND_EVENTS = ("sent", "send")
BOUNDARY = "SANDBOX_BOUNDARY_VIOLATION"
SILENT_AGE_DAYS = 60  # far past its determination clock and the follow-up day after it
REPLIED_AGE_DAYS = 3  # recent: its own clock has not passed
RESOURCES_FILE = "engine-resources.json"
RESOURCES_SCHEMA = "records-rehearsal-engine-resources-v1"
STEP_NAMES = ("replay_reply", "reply_draft_queued", "followup_due", "followup_draft_queued", "no_sends")
_JOURNAL = Path("approvals") / "queue.jsonl"
_SEND_LOG = Path("approvals") / "mail-send.jsonl"
_ENGINE = Path(__file__).resolve().parents[2]  # the directory holding campaign_tool/ and schemas/
_WORKER = ("-B", "-m", "campaign_tool.records.extraction_routes", "_worker")
_LOOPBACK = ("127.0.0.1", "::1", "localhost")
_REFUSED = ("os.system", "os.exec", "os.posix_spawn", "os.spawn", "os.fork", "os.forkpty", "pty.spawn",
            "smtplib.connect", "smtplib.send", "imaplib.open", "poplib.connect", "ftplib.connect", "urllib.Request")
_WRITE_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND
# The fictional fee letter (one line per text-layer line of the attached PDF); the outputs stage selects a fee reply.
REPLY_LINES = ("Fictional Synthetic Valley Records Unit", "The duplication fee is $12.00 for the requested records.",
               "Please remit payment before we release them.")


class _StepFailed(Exception):
    """A semantic check failed; the sole argument is a fixed evidence code."""


def reply_pdf(lines=REPLY_LINES):
    """A one-page text-layer PDF built in memory (no binary fixture), one line per ``Tj``."""
    stream = ("BT /F1 12 Tf 72 720 Td 16 TL " + " T* ".join("(%s) Tj" % line for line in lines) + " ET").encode()
    objects = [b"<< /Type /Catalog /Pages 2 0 R >>", b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
               b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
               b"/Resources << /Font << /F1 5 0 R >> >> >>",
               b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream",
               b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    out, offsets = bytearray(b"%PDF-1.4\n"), []
    for number, body in enumerate(objects, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % number + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    out += b"".join(b"%010d 00000 n \n" % offset for offset in offsets)
    return bytes(out + b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objects) + 1, xref))


# ---- sandbox boundary ------------------------------------------------------------------------------------------

_GUARD = None  # the active boundary; the process-wide audit hook does nothing while this is None
_HOOKED = False
_REENTRY = threading.local()  # the hook's own path checks are not themselves checked (per thread)


def _audit(event, args):
    guard = _GUARD
    if guard is not None and not getattr(_REENTRY, "on", False):
        _REENTRY.on = True
        try:
            guard.check(event, args)
        finally:
            _REENTRY.on = False


def _inside(path, roots):
    return any(path == root or path.startswith(root.rstrip(os.sep) + os.sep) for root in roots)


def _real(path):
    """The one decision path: absolute and symlink-free, or None when a relative name climbs with '..'."""
    path = os.fsdecode(path)
    if not os.path.isabs(path):
        if ".." in path.split(os.sep):
            return None
        path = os.path.join(os.getcwd(), path)  # the guarded window runs with cwd at the sandbox root
    return os.path.realpath(path)


def _both(paths):
    return tuple(sorted({form for p in paths if p for form in (os.path.normpath(os.path.abspath(p)),
                                                              os.path.realpath(p))}))


class _Boundary:
    """What ``run`` may touch. Violations are recorded as event kinds only (never a path) and refused."""

    def __init__(self, sandbox):
        self.sandbox = _both([str(sandbox)])
        self.engine = _both([str(_ENGINE / "campaign_tool"), str(_ENGINE / "schemas")])
        library = {sysconfig.get_paths(vars=v).get(k) for v in (None, {"base": sys.base_prefix,
                                                                        "platbase": sys.base_exec_prefix})
                   for k in ("stdlib", "platstdlib", "purelib", "platlib")}
        library.update(site.getsitepackages() if hasattr(site, "getsitepackages") else ())
        prefixes = _both([sys.prefix, sys.base_prefix, sys.exec_prefix, sys.base_exec_prefix])
        library.update(p for p in sys.path if p and ("site-packages" in p or "dist-packages" in p
                                                     or _inside(os.path.abspath(p), prefixes)))
        self.library = _both(library | set(zoneinfo.TZPATH))
        self.files = _both([sys.executable, os.devnull, "/dev/urandom", "/etc/localtime", *mimetypes.knownfiles])
        self.read = set()  # engine resources read (absolute), hashed into RESOURCES_FILE
        self.violations = []

    def refuse(self, kind):
        self.violations.append(kind)
        raise PermissionError(errno.EACCES, "outside the rehearsal sandbox boundary")

    def check(self, event, args):
        if event == "open":
            self.check_open(*args[:3])
        elif event == "sqlite3.connect":
            name = os.fsdecode(args[0]) if isinstance(args[0], (str, bytes, os.PathLike)) else ""
            if not self.sqlite_allowed(name):
                self.refuse("sqlite")
        elif event == "subprocess.Popen":
            argv = [os.fsdecode(a) for a in args[1]] if isinstance(args[1], (list, tuple)) else []
            if not (len(argv) in (10, 11) and argv[0] == sys.executable and tuple(argv[1:5]) == _WORKER
                    and all(self.in_sandbox(argv[i]) for i in (5, 8, 9))
                    and argv[10:] in ([], ["wrap-image"]) and args[2] is None):
                self.refuse("subprocess")
        elif event in ("socket.connect", "socket.bind"):
            address = args[1]
            host = address[0] if isinstance(address, tuple) and address else None
            if host not in _LOOPBACK or (event == "socket.bind" and address[1] != 0):
                self.refuse(event)
        elif event == "socket.getaddrinfo":
            host = os.fsdecode(args[0]) if isinstance(args[0], (str, bytes)) else None
            if host not in _LOOPBACK:
                self.refuse(event)
        elif event == "http.client.connect":
            if args[1] not in _LOOPBACK:
                self.refuse(event)
        elif event in _REFUSED:
            self.refuse(event)

    def in_sandbox(self, path):
        """The one real-path boundary decision for a path the guard must judge (sqlite, worker argv)."""
        real = _real(path)
        return real is not None and _inside(real, self.sandbox)

    def sqlite_allowed(self, name):
        if name == "":  # unchanged behaviour: the cwd (the sandbox root) is judged lexically
            return _inside(os.path.normpath(os.path.abspath(name)), self.sandbox)
        if name == ":memory:":
            return True
        if name.startswith("file:"):
            split = urllib.parse.urlsplit(name)
            if split.netloc not in ("", "localhost"):
                return False
            path = urllib.parse.unquote(split.path)
            if path in ("", ":memory:") and "memory" in urllib.parse.parse_qs(split.query).get("mode", []):
                return True
            return self.in_sandbox(path)
        return self.in_sandbox(name)

    def check_open(self, path, mode, flags):
        if not isinstance(path, (str, bytes, os.PathLike)):
            return  # an already-open descriptor
        flags = flags if type(flags) is int else 0
        write = bool(flags & _WRITE_FLAGS) or (isinstance(mode, str) and any(c in mode for c in "wax+"))
        candidate = _real(path)
        if candidate is None:
            self.refuse("open_write" if write else "open_read")
        for candidate in (candidate,):  # the real path alone decides, never the lexical form
            if _inside(candidate, self.sandbox):
                return
            if write:
                if candidate == os.devnull or (os.path.basename(os.path.dirname(candidate)) == "__pycache__"
                                               and _inside(candidate, self.engine + self.library)):
                    return  # the null device, or Python's own bytecode cache
                continue
            if _inside(candidate, self.engine):
                if not candidate.endswith((".py", ".pyc")):
                    self.read.add(candidate)
                return
            if candidate in self.files or _inside(candidate, self.library):
                return
            if flags & os.O_DIRECTORY and any(root.startswith(candidate.rstrip(os.sep) + os.sep)
                                              for root in self.sandbox):
                return  # a no-follow directory walk from / down to the sandbox
        self.refuse("open_write" if write else "open_read")


@contextlib.contextmanager
def _guarded(sandbox):
    global _GUARD, _HOOKED
    if not _HOOKED:  # an audit hook cannot be removed; it stays inert outside a rehearsal
        sys.addaudithook(_audit)
        _HOOKED = True
    if _GUARD is not None:
        raise ValueError("REHEARSAL_ALREADY_RUNNING")
    guard = _Boundary(sandbox)
    previous = os.getcwd()
    path_env = os.environ.get("PYTHONPATH")
    real_open = os.open

    def open_at(path, flags, mode=0o777, *, dir_fd=None):
        # The audit "open" event carries no dir_fd, so a name relative to a held descriptor is judged here, on the
        # real path of that descriptor's directory (fail closed when it cannot be resolved).
        if dir_fd is not None and isinstance(path, (str, bytes, os.PathLike)) and not os.path.isabs(os.fsdecode(path)):
            name = os.fsdecode(path)
            try:
                base = os.path.realpath(os.readlink("/proc/self/fd/%d" % dir_fd))
            except (OSError, TypeError, ValueError):
                base = None
            if base is None or not os.path.isabs(base):
                guard.refuse("open_write" if flags & _WRITE_FLAGS else "open_read")
            if "." in name.split(os.sep) or ".." in name.split(os.sep):
                guard.refuse("open_write" if flags & _WRITE_FLAGS else "open_read")
            guard.check_open(os.path.join(base, name), None, flags)
        return real_open(path, flags, mode, dir_fd=dir_fd)
    supports = os.supports_dir_fd
    added = open_at not in supports
    entered = False
    try:
        os.open = open_at
        supports.add(open_at)  # keep "os.open in os.supports_dir_fd" true while the wrapper is installed
        os.chdir(os.path.realpath(sandbox))  # relative opens resolve inside the sandbox, not the host cwd
        entered = True
        # the replay worker child inherits that cwd, so it must find the engine by path rather than the old cwd
        os.environ["PYTHONPATH"] = os.pathsep.join(filter(None, (str(_ENGINE), path_env)))
        _GUARD = guard
        yield guard
    finally:  # every setup failure restores the process state it had already changed
        _GUARD = None
        os.open = real_open
        if added:
            supports.discard(open_at)
        if entered:
            os.chdir(previous)
        if path_env is None:
            os.environ.pop("PYTHONPATH", None)
        else:
            os.environ["PYTHONPATH"] = path_env


def _resources_record(sandbox, guard):
    """Write the engine resources the replay read (path relative to the engine root, sha256); return its path."""
    rows = [{"path": Path(os.path.relpath(path, _ENGINE)).as_posix(),
             "sha256": hashlib.sha256(Path(path).read_bytes()).hexdigest()} for path in sorted(guard.read)]
    target = sandbox / RESOURCES_FILE
    _new_file(target, _json({"schema": RESOURCES_SCHEMA, "resources": rows}))
    return target


# ---- the rehearsal -----------------------------------------------------------------------------------------------

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


def _new_file(path, data):
    """A new 0600 file through the replay's own sandbox writer, which refuses any existing path."""
    e2e_proof._seed_file(path, data)


def _json(document):
    return (json.dumps(document, indent=2, sort_keys=True) + "\n").encode("utf-8")


def reply_eml(now):
    """The fictional agency reply: a short body and the fee letter as a PDF attachment."""
    message = EmailMessage()
    message["From"] = "records@synthetic-valley.example.invalid"
    message["To"] = "requests@campaign.example.invalid"
    message["Subject"] = "Reply to records request " + REPLIED
    message["Date"] = (now - timedelta(days=1)).strftime("%a, %d %b %Y %H:%M:%S +0000")
    message["Message-ID"] = "<%s@synthetic-valley.example.invalid>" % REPLIED
    message.set_content("The reply is attached.\n")
    message.add_attachment(reply_pdf(), maintype="application", subtype="pdf", filename="reply-fee.pdf")
    return message.as_bytes(policy=SMTP)


def seed(now):
    """The records-e2e-seed-v1 document: one agency, the replied request and a silent one; drafting on for it."""
    day = lambda age: (now - timedelta(days=age)).date().isoformat()  # noqa: E731
    return {
        "schema": e2e_proof.SEED_SCHEMA,
        "agencies": [{"id": AGENCY, "name": "Synthetic Valley Police Department", "jurisdiction": "us-ca"}],
        "requests": [{"id": REPLIED, "agency_id": AGENCY, "submitted_at": day(REPLIED_AGE_DAYS), "status": "open"},
                     {"id": SILENT, "agency_id": AGENCY, "submitted_at": day(SILENT_AGE_DAYS), "status": "open"}],
        "join": {"agency_id": AGENCY, "request_id": REPLIED},
        # The seed writes the settings file inside the sandbox: drafting is on for this fictional agency only.
        "followup": {"asked": "1. ALPR search logs", "autonomy": "draft",
                     "source_sha256": hashlib.sha256(reply_pdf()).hexdigest()},
    }


class _Rehearsal:
    def __init__(self, sandbox, now):
        self.sandbox, self.now = sandbox, now
        self.project = sandbox / LABEL / "project"
        self.report = None

    def _run_id(self):
        if not self.report:
            raise _StepFailed("REPLAY_NOT_RUN")
        return self.report["run_id"]

    def _pending(self, kinds):
        run_id = self._run_id()
        return [r for r in approval_queue.load_queue(str(self.project))
                if r["status"] == "pending" and r["kind"] in kinds and r["source_run_id"] == run_id]

    def replay_reply(self):
        inputs, campaign = self.sandbox / "inputs", self.sandbox / "campaign"
        for directory in (inputs, campaign, campaign / "content"):
            directory.mkdir(mode=0o700)
        eml, seed_path, agencies = inputs / "reply.eml", inputs / "seed.json", campaign / "content" / "agencies.json"
        _new_file(eml, reply_eml(self.now))
        _new_file(seed_path, _json(seed(self.now)))
        _new_file(agencies, _json([{"agency_id": AGENCY, "name": "Synthetic Valley Police Department",
                                    "profile": {"status": "Records requested",
                                                "summary": "Fictional agency for the drafts rehearsal."}}]))
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                self.report = e2e_proof.replay(eml, self.sandbox, campaign, label=LABEL, now=self.now, seed=seed_path)
        except e2e_proof.ProofError as error:
            raise _StepFailed("REPLAY_" + error.code) from None
        outputs = next((s for s in self.report["stages"] if s["stage"] == "outputs"), None)
        if outputs is None or outputs["status"] in ("fail", "not_run"):
            raise _StepFailed("REPLAY_OUTPUTS_NOT_RUN")
        run_report = self.project / "run-report.json"
        entries = [s for s in json.loads(run_report.read_bytes())["stages"] if s.get("name") == "outputs"]
        if len(entries) != 1:
            raise _StepFailed("RUN_REPORT_OUTPUTS_MISSING")
        held = [outputs["code"], *(o.get("code") for o in outputs["detail"].get("outcomes") or []),
                *(s.get("error_code") for s in entries)]
        for code in QUEUE_CODES:  # C2: the outputs stage reached a readable, writable approval queue
            if code in held:
                raise _StepFailed(code)
        if self.report["result"] != "PASS" or self.report["manual_steps"]:
            raise _StepFailed("REPLAY_FAILED")
        return [eml, seed_path, agencies, self.sandbox / LABEL / "proof-report.json", run_report]

    def reply_draft_queued(self):
        rows = self._pending(REPLY_KINDS)
        if not rows:
            raise _StepFailed("REPLY_DRAFT_MISSING")
        if len(rows) != 1:
            raise _StepFailed("REPLY_DRAFT_COUNT_INVALID")
        if (rows[0]["agency_id"], rows[0]["request_id"]) != (AGENCY, REPLIED):
            raise _StepFailed("REPLY_DRAFT_WRONG_REQUEST")
        return [self.project / _JOURNAL]

    def followup_due(self):
        register = followup_clock.read_register(str(self.project), self._run_id())
        if register is None:
            raise _StepFailed("FOLLOWUP_REGISTER_MISSING")
        settings = followup_clock.load_settings(str(self.project))
        due = [(c["kind"], c["agency_key"], c["request_id"])
               for c in followup_clock.plan(register, settings, [], as_of=register["as_of"])]
        if not due:
            raise _StepFailed("FOLLOWUP_NOT_DUE")
        if due != [(followup_clock.REMINDERS[0], AGENCY, SILENT)]:
            raise _StepFailed("FOLLOWUP_DUE_UNEXPECTED")
        return [self.project / "run-outputs" / self._run_id() / followup_clock.REGISTER_NAME]

    def followup_draft_queued(self):
        rows = self._pending(FOLLOWUP_KINDS + (ENGINE_FOLLOWUP,))
        if not rows:
            raise _StepFailed("FOLLOWUP_DRAFT_MISSING")
        if len(rows) != 1:
            raise _StepFailed("FOLLOWUP_DRAFT_COUNT_INVALID")
        if (rows[0]["kind"], rows[0]["agency_id"], rows[0]["request_id"]) != (
                followup_clock.REMINDERS[0], AGENCY, SILENT):
            raise _StepFailed("FOLLOWUP_DRAFT_WRONG_REQUEST")
        return [self.project / _JOURNAL]

    def no_sends(self):
        journal = self.project / _JOURNAL
        events = [json.loads(line).get("event") for line in journal.read_bytes().splitlines()]
        if any(event in SEND_EVENTS for event in events) or os.path.lexists(self.project / _SEND_LOG):
            raise _StepFailed("SEND_RECORDED")
        return [journal]


def _hashed(sandbox, paths):
    return [{"path": path.relative_to(sandbox).as_posix(), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
            for path in paths]


def run(sandbox, *, now=None):
    """Rehearse stages 13 and 14 in ``sandbox``; return step codes and output path/sha256 pairs, never draft text."""
    sandbox = _sandbox_path(sandbox)
    _require_empty(sandbox)
    rehearsal = _Rehearsal(sandbox, (now or datetime.now(timezone.utc)).astimezone(timezone.utc))
    result = {"stages": list(STAGES), "steps": [], "manual_steps": [], "codes": []}
    umask = os.umask(0o077)
    try:
        with _guarded(sandbox) as guard:
            _new_file(sandbox / SANDBOX_MARKER, b"")
            recorded = None
            for name in STEP_NAMES:
                step, paths = {"name": name, "code": "OK", "outputs": []}, []
                try:
                    paths = getattr(rehearsal, name)()
                    if recorded is not None and guard.read != recorded:
                        raise _StepFailed("ENGINE_RESOURCE_UNRECORDED")  # the record covers the whole run
                except _StepFailed as error:
                    step["code"] = error.args[0]
                except (OSError, ValueError, KeyError, TypeError, AttributeError, sqlite3.Error,
                        approval_queue.ApprovalQueueError, e2e_proof.ProofError):
                    # Never print a dependency exception: it may carry draft text or private paths.
                    step["code"] = "DRAFTS_" + name.upper() + "_ERROR"
                if guard.violations:
                    step["code"] = BOUNDARY
                if step["code"] == "OK":
                    if recorded is None:  # after the replay: every engine resource the stages read, hashed
                        recorded = frozenset(guard.read)
                        paths = [*paths, _resources_record(sandbox, guard)]
                    step["outputs"] = _hashed(sandbox, paths)
                result["steps"].append(step)
                if step["code"] != "OK":
                    result["codes"].append(step["code"])
                    if name == "replay_reply" and rehearsal.report:  # the replay's own manual steps, codes only
                        result["manual_steps"] = [m["why"] if m["why"].startswith(m["stage"] + ":")
                                                  else "%s:%s" % (m["stage"], m["why"])
                                                  for m in rehearsal.report["manual_steps"]]
                    break
    finally:
        os.umask(umask)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=TITLE)
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
