"""Stage 7 rehearsal driver: the real delta and adoption stages on one synthetic mail export, in a sandbox."""
import hashlib
import io
import json
import os
import re
import smtplib
import socket
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing, redirect_stderr, redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from campaign_tool.records import e2e_proof, rehearsal_receipt, rehearse, rehearse_adoption as driver
from campaign_tool.records import run_orchestrator, run_stages

STEPS = ["prepare_export", "delta", "adoption", "message_held", "tampered_export_refused", "no_sends"]


def canned(status="ok", code=None, **counts):
    """A stage result that did not come from the real stage; used only to test the driver's own judgment of it."""
    return status, code, {"counts": {"input": 1, "output": 1, "failed": 0, "skipped": 0, **counts}, "outcomes": []}


def last(result):
    return result["steps"][-1]["name"], result["steps"][-1]["code"]


def count(sandbox, project, table):
    with closing(sqlite3.connect("file:%s?mode=ro" % (sandbox / project / "processing-v1/ledger.sqlite"), uri=True)) as db:
        return db.execute("SELECT COUNT(*) FROM " + table).fetchone()[0]


class Base(unittest.TestCase):
    def setUp(self):
        self.parent = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.sandbox = self.fresh("sandbox")

    def fresh(self, label):
        path = self.parent / re.sub(r"\W+", "-", label)
        path.mkdir(mode=0o700)
        return path

    def replacing(self, label, replace, before=None):
        """Run the driver; ``replace(name, project)`` gives a canned stage result or None (real); ``before(name)`` runs first."""
        real = e2e_proof._adapter
        def adapter(name, project, *rest):
            before and before(name)
            return replace(name, Path(project)) or real(name, project, *rest)
        with patch.object(e2e_proof, "_adapter", adapter):
            return driver.run(self.fresh(label))


class RealRun(Base):
    @classmethod
    def setUpClass(cls):
        cls.parent = Path(cls.enterClassContext(tempfile.TemporaryDirectory())).resolve()
        cls.sandbox = cls.parent / "sandbox"
        cls.sandbox.mkdir(mode=0o700)
        cls.result = driver.run(cls.sandbox)

    def setUp(self):
        pass

    def test_c1_steps_in_order_all_ok_with_hashed_outputs(self):
        """C1: the five contract steps plus the control, each OK. Bug: a step dropped or renamed, or a wrong hash."""
        result = self.result
        self.assertEqual((result["stages"], result["manual_steps"], result["codes"]), (["7"], [], []))
        self.assertEqual([(s["name"], s["code"]) for s in result["steps"]], [(name, "OK") for name in STEPS])

    def test_c2_ledger_holds_exactly_the_synthetic_message_by_sha(self):
        """C2: read the ledger independently of the driver. Bug: adoption reported OK while the ledger is empty."""
        (eml,) = [p for p in (self.sandbox / "project/mail-history/objects").rglob("*")
                  if p.is_file() and b"Synthetic reply" in p.read_bytes()]
        self.assertEqual(hashlib.sha256(eml.read_bytes()).hexdigest(), eml.name)
        with closing(sqlite3.connect("file:%s?mode=ro" % (self.sandbox / "project/processing-v1/ledger.sqlite"), uri=True)) as db:
            self.assertEqual(db.execute("SELECT eml_sha256 FROM mail_messages").fetchall(), [(eml.name,)])
        self.assertEqual(count(self.sandbox, "project", "originals"), 2)

    def test_c3_control_changes_one_byte_after_the_receipt_and_adopts_nothing(self):
        """C3: the control's message differs from its receipt hash by one byte. Bug: a control that never tampers."""
        (receipt,) = (self.sandbox / "control/project/mail-history/receipts").glob("*.json")
        sha = json.loads(receipt.read_text(encoding="utf-8"))["original_eml"]["sha256"]
        tampered = (self.sandbox / "control/project/mail-history/objects" / sha[:2] / sha).read_bytes()
        good = (self.sandbox / "project/mail-history/objects" / sha[:2] / sha).read_bytes()
        self.assertNotEqual(hashlib.sha256(tampered).hexdigest(), sha)
        self.assertEqual((len(tampered), sum(a != b for a, b in zip(tampered, good))), (len(good), 1))
        self.assertEqual(count(self.sandbox, "control/project", "mail_messages"), 0)

    def test_c4_only_fictional_identities(self):
        """C4: e2e_proof's fictional account and folder, no real mailbox. Bug: a real address in the export."""
        (receipt,) = (self.sandbox / "project/mail-history/receipts").glob("*.json")
        document = json.loads(receipt.read_text(encoding="utf-8"))
        self.assertEqual((document["account_id"], document["folder"]), (e2e_proof.ADOPTION_ACCOUNT, e2e_proof.ADOPTION_FOLDER))
        domains = set(re.findall(r"@([A-Za-z0-9.-]+)", driver._message().decode("ascii")))
        self.assertTrue(domains and all(d.endswith(".example.invalid") for d in domains), domains)

    def test_receipt_qualifies_and_the_driver_is_discovered_for_stage_7(self):
        """DONE: discover() finds it and the receipt qualifies. Bug: wrong STAGES, or a sandbox without its marker."""
        self.assertIn(driver, rehearse.discover())
        when = datetime(2026, 11, 24, tzinfo=timezone.utc)
        receipt = rehearsal_receipt.build("rehearse_adoption", self.result, self.sandbox, when, when, "a" * 40)
        self.assertEqual((receipt["qualified"], receipt["qualify_codes"]), (True, []))


class Controls(Base):
    def test_c2_adoption_ok_needs_positive_equal_counts_and_nothing_failed_or_skipped(self):
        """C2: a non-positive report fails ADOPTION_COUNTS_UNEXPECTED. Bug: any non-fail status passes."""
        for label, counts in (("nothing adopted", dict(output=0)), ("one skipped", dict(skipped=1)),
                              ("one failed", dict(failed=1)), ("more out than in", dict(output=2)),
                              ("empty export", dict(input=0, output=0))):
            with self.subTest(label):
                result = self.replacing(label, lambda name, project, c=counts: canned(**c) if name == "adoption" else None)
                self.assertEqual((last(result), result["codes"]),
                                 (("adoption", "ADOPTION_COUNTS_UNEXPECTED"), ["ADOPTION_COUNTS_UNEXPECTED"]))
                self.assertEqual([s["name"] for s in result["steps"]], STEPS[:3])  # a failed step stops later steps

    def test_c2_message_held_compares_the_ledger_by_sha(self):
        """C2: another message, an extra original or none is MESSAGE_NOT_HELD. Bug: counting rows only."""
        real = driver._ledger
        for label, change in (("other message", lambda rows, o: ([(rows[0][0], rows[0][1], "0" * 64)], o)),
                              ("extra original", lambda rows, o: (rows, o | {"1" * 64}))):
            with self.subTest(label):
                calls = []
                def ledger(project, change=change):  # the first read is message_held's
                    calls.append(project)
                    return change(*real(project)) if len(calls) == 1 else real(project)
                with patch.object(driver, "_ledger", ledger):
                    self.assertEqual(last(driver.run(self.fresh(label))), ("message_held", "MESSAGE_NOT_HELD"))

    def test_c3_a_tamper_that_changes_nothing_is_adopted_and_fails_adoption_not_refused(self):
        """C3: if the real stage adopts the control, the driver fails ADOPTION_NOT_REFUSED. Bug: the control always passes."""
        with patch.object(driver, "_flip", lambda path: None):
            result = driver.run(self.sandbox)
        self.assertEqual((last(result), result["codes"]),
                         (("tampered_export_refused", "ADOPTION_NOT_REFUSED"), ["ADOPTION_NOT_REFUSED"]))

    def test_c3_a_refusal_for_another_reason_is_not_the_control(self):
        """C3: nothing adopted but no hash-mismatch gap is ADOPTION_REFUSAL_UNEXPECTED. Bug: any failure passes."""
        for label, other in (("other code", canned("fail", "ADOPTION_REFUSED", output=0)),
                             ("blocked without the gap", canned("fail", "ADOPTION_BLOCKED", output=0, failed=1)),
                             ("not configured", canned("skipped", "ADOPTION_NOT_CONFIGURED", output=0))):
            with self.subTest(label):
                result = self.replacing(label, lambda name, project, o=other: o if "control" in project.parts else None)
                self.assertEqual(last(result), ("tampered_export_refused", "ADOPTION_REFUSAL_UNEXPECTED"))


class Fences(Base):
    def test_c1_every_write_stays_inside_the_sandbox_the_marker_is_first_and_nothing_connects(self):
        """C1: marker first, then only sandbox paths, no socket at all. Bug: a temp or HOME file, or a stage that polls."""
        seen, sockets = [], []

        def watch(real, writes):
            """Wrap ``real``: when ``writes(*args)`` says it writes, the path must be inside the sandbox."""
            def wrapper(target, *a, **k):
                if writes(target, *a):
                    path = Path(os.path.abspath(str(target))).resolve()
                    if path != Path(os.devnull):  # subprocess opens it for the stage's child process
                        seen.append(path)
                        self.assertTrue(self.sandbox in (path, *path.parents), "write outside sandbox: %s" % path)
                return real(target, *a, **k)
            return wrapper
        connect = socket.socket.connect
        for each in (patch.object(io, "open", watch(io.open, lambda f, mode="r", *a: not isinstance(f, int) and set(mode) & set("wax+"))),
                     patch.object(os, "open", watch(os.open, lambda p, flags, *a: flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT))),
                     patch.object(sqlite3, "connect", watch(sqlite3.connect, lambda d, *a: "mode=ro" not in str(d))),
                     patch.object(socket.socket, "connect", lambda s, a, *r: sockets.append(a) or connect(s, a, *r))):
            self.enterContext(each)
        result = driver.run(self.sandbox)
        self.assertEqual((result["codes"], sockets), ([], []))
        self.assertEqual(seen[0], self.sandbox / run_stages.SANDBOX_MARKER)
        self.assertTrue(any(p.name == "ledger.sqlite" for p in seen) and len(seen) > 10, len(seen))

    def test_c1_a_send_a_notify_or_a_remote_connection_fails_send_attempted_and_never_leaves(self):
        """C1: each attempt is refused and named. Bug: a fence that misses a path, so a rehearsal could really send."""
        def raw():
            with socket.socket() as sock:
                sock.connect(("203.0.113.9", 80))
        def swallowed(host="mail.example.invalid"):  # a stage that hides the refusal: only the fence record, read by no_sends, remembers it
            try:
                smtplib.SMTP(host, 25)
            except OSError:
                pass
        def loopback(address, family=socket.AF_INET):
            def attempt():
                with socket.socket(family) as sock:
                    sock.connect(address)
            return attempt
        # Bug: a fence that waves loopback or local-socket relays through would let a stage send mail and still pass no_sends.
        attempts = {"loopback smtp": (lambda: smtplib.SMTP("127.0.0.1", 25), "delta"),
                    "loopback ipv6": (loopback(("::1", 25), socket.AF_INET6), "delta"),
                    "unix relay": (loopback("/nonexistent/relay.sock", socket.AF_UNIX), "delta"),
                    "swallowed loopback": (lambda: swallowed("127.0.0.1"), "adoption"),
                    "smtp": (lambda: smtplib.SMTP("mail.example.invalid", 25), "delta"),
                    "name lookup": (lambda: socket.create_connection(("remote.example.invalid", 80), timeout=1), "delta"),
                    "raw connect": (raw, "delta"), "notify": (lambda: run_orchestrator.cmd_notify_fallback(None), "delta"),
                    "swallowed": (swallowed, "adoption")}
        for label, (attempt, stage) in attempts.items():
            with self.subTest(label):
                connects = []
                with patch.object(socket.socket, "connect", lambda s, a, *r: connects.append(a)):
                    result = self.replacing(label, lambda name, project: None,
                                            before=lambda name, a=attempt, s=stage: name == s and a())
                self.assertEqual((last(result), connects),
                                 (("no_sends" if label.startswith("swallowed") else stage, "SEND_ATTEMPTED"), []))


class Children(Base):
    """The stages start adopt-mail child interpreters; the audit hook must reach them and refuse what the parent fence cannot see."""

    def test_c1_every_child_is_audited_and_none_wrote_outside_or_used_a_socket(self):
        """C1: starts are logged for the real children and nothing is refused. Bug: a fence that covers only the parent process."""
        sandbox = self.sandbox
        result = driver.run(sandbox)
        self.assertEqual(result["codes"], [])
        events = [json.loads(line) for line in (sandbox / driver.CHILD_LOG).read_text().splitlines()]
        starts = [value for kind, value in events if kind == "start"]
        self.assertGreaterEqual(len(starts), 3)  # delta, adoption and the control's adoption
        self.assertTrue(all(argv[:3] == ["-B", "-m", "campaign_tool.records"] and "adopt-mail" in argv for argv in starts), starts)
        self.assertEqual([kind for kind, _ in events if kind != "start"], [])
        self.assertEqual(result["steps"][-1]["outputs"][0]["path"], driver.CHILD_LOG)

    def test_c1_a_child_that_writes_outside_or_connects_is_refused_recorded_and_named(self):
        """C1: a real child interpreter under the stage's environment cannot write outside or open a socket, and the driver fails.
        Bug: only the parent was fenced, so a child could send or write anywhere and no_sends stayed green."""
        outside = self.parent / "child-escape.txt"
        database = self.parent / "child-escape.db"
        code = {"write": "open(%r, 'w').write('x')" % str(outside),
                "socket": "import socket; socket.socket().connect(('127.0.0.1', 9))",
                "sqlite": "import sqlite3; sqlite3.connect(%r).execute('create table t(x)')" % str(database),
                "sqlite uri": "import sqlite3; sqlite3.connect('file:%s?mode=rwc', uri=True).execute('create table t(x)')" % database,
                "sqlite uri path": "import sqlite3; sqlite3.connect('file://%s', uri=True).execute('create table t(x)')" % database}
        wanted = (("write", "CHILD_WROTE_OUTSIDE_SANDBOX"), ("socket", "SEND_ATTEMPTED"), ("sqlite", "CHILD_WROTE_OUTSIDE_SANDBOX"),
                  ("sqlite uri", "CHILD_WROTE_OUTSIDE_SANDBOX"), ("sqlite uri path", "CHILD_WROTE_OUTSIDE_SANDBOX"))
        for label, want in wanted:
            with self.subTest(label):
                def attempt(name, source=code[label]):
                    if name == "adoption":
                        subprocess.run([sys.executable, "-B", "-c", source], check=False, stderr=subprocess.DEVNULL)
                result = self.replacing(label, lambda name, project: None, before=attempt)
                self.assertEqual(last(result), ("no_sends", want))
                self.assertEqual(result["codes"], [want])
                self.assertEqual((outside.exists(), database.exists(), (self.parent / "child-escape.db-journal").exists()),
                                 (False, False, False))

    def test_c1_a_child_may_open_sqlite_inside_the_sandbox_and_read_outside_read_only(self):
        """C1: the real children keep working: an in-sandbox database and a read-only outside one are not refused. Bug: a hook so
        broad it fails every adopt-mail child, or one that counts a read as a write."""
        readable = self.parent / "readable.db"
        with closing(sqlite3.connect(readable)) as db:
            db.execute("CREATE TABLE t(x)")
        inside = "import sqlite3; c = sqlite3.connect(%r); c.execute('create table if not exists t(x)'); c.commit()" % str(self.parent / "allowed" / "inside.db")
        read_only = "import sqlite3; sqlite3.connect('file:%s?mode=ro', uri=True).execute('select * from t').fetchall()" % readable

        def attempt(name):
            if name == "adoption":
                for source in (inside, read_only):
                    self.assertEqual(subprocess.run([sys.executable, "-B", "-c", source], stderr=subprocess.DEVNULL).returncode, 0)
        result = self.replacing("allowed", lambda name, project: None, before=attempt)
        self.assertEqual(last(result), ("no_sends", "CHILD_UNEXPECTED"))  # the probes are not adopt-mail children, but nothing was refused
        kinds = [json.loads(line)[0] for line in (self.parent / "allowed" / driver.CHILD_LOG).read_text().splitlines()]
        self.assertEqual(sorted(set(kinds)), ["start"])
        self.assertTrue((self.parent / "allowed" / "inside.db").exists())

    def test_c1_a_run_with_no_audited_child_is_not_a_pass(self):
        """C1: if no child was audited the boundary was not observed. Bug: an empty audit counted as a clean one."""
        real = driver._children
        with patch.object(driver, "_children", lambda sandbox: [row for row in real(sandbox) if row[0] != "start"]):
            self.assertEqual(last(driver.run(self.sandbox)), ("no_sends", "CHILD_NOT_AUDITED"))
        with patch.object(driver, "_children", lambda sandbox: [[k, ["-B", "-m", "os", "x"]] if k == "start" else [k, v] for k, v in real(sandbox)]):
            self.assertEqual(last(driver.run(self.fresh("other"))), ("no_sends", "CHILD_UNEXPECTED"))


class Paths(Base):
    def test_c2_the_ledger_is_read_from_a_sandbox_path_holding_uri_characters(self):
        """C2: a path with ``#``, ``?`` or ``%`` reads its own ledger read-only. Bug: an unescaped SQLite URI names another file."""
        for name in ("a#b", "c?d=1", "e%41f"):
            with self.subTest(name):
                project = self.parent / name / "project"
                (project / "processing-v1").mkdir(parents=True)
                with closing(sqlite3.connect(project / "processing-v1" / "ledger.sqlite")) as db:
                    db.executescript("CREATE TABLE mail_messages(account, folder, eml_sha256); CREATE TABLE originals(sha256);"
                                     "INSERT INTO mail_messages VALUES ('a', 'f', 'x'); INSERT INTO originals VALUES ('y');")
                self.assertEqual(driver._ledger(project), ([("a", "f", "x")], {"y"}))

    def test_c3_the_cli_creates_nothing_below_a_symlink(self):
        """C3: a rejected --sandbox leaves no directory behind. Bug: mkdir ran before the symlink check and filled the target."""
        target = self.fresh("target")
        link = self.parent / "link"
        link.symlink_to(target)
        dangling = self.parent / "dangling"
        dangling.symlink_to(self.parent / "never-made")
        for path in (link / "new", link, dangling, dangling / "inner", self.parent / "absent" / "deep"):
            with self.subTest(str(path.relative_to(self.parent))), redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as caught:
                driver.main(["--sandbox", str(path)])
            self.assertEqual(caught.exception.code, 2)
        self.assertEqual((list(target.iterdir()), (self.parent / "never-made").exists(), (self.parent / "absent").exists()),
                         ([], False, False))


class Entry(Base):
    def test_interface_exit_codes_and_sandbox_refusals(self):
        """Interface: main exits 0 only when clean, 2 for a bad sandbox. Bug: exit 0 on a failed step, or a reused dirty dir."""
        ok = {"stages": ["7"], "steps": [], "manual_steps": [], "codes": []}
        for label, result, status in (("clean", ok, 0), ("failed", {**ok, "codes": ["X_FAILED"]}, 1),
                                      ("manual", {**ok, "manual_steps": ["do it"]}, 1)):
            with self.subTest(label), patch.object(driver, "run", return_value=result):
                out = io.StringIO()
                with redirect_stdout(out):
                    self.assertEqual(driver.main(["--sandbox", str(self.parent / label)]), status)
                self.assertEqual(json.loads(out.getvalue()), result)
        (self.sandbox / "stray").write_text("x")
        wide = self.fresh("wide")
        wide.chmod(0o755)
        for path in (self.sandbox, wide):
            with self.assertRaises(ValueError):
                driver.run(path)
            with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as caught:
                driver.main(["--sandbox", str(path)])
            self.assertEqual(caught.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
