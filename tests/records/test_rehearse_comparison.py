"""Stage 10 rehearsal driver contracts (C1-C5 of the card), on synthetic inputs in private temporary sandboxes.

The happy run is one real run; each control breaks one real input or writer and must stop the driver at its named step."""
import builtins
import contextlib
import copy
import hashlib
import io
import json
import os
import re
import smtplib
import socket
import sqlite3
import stat
import tempfile
import unittest
import urllib.request
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from campaign_tool import outbox
from campaign_tool.records import e2e_proof, mail_send, native_extract, rehearsal_receipt, rehearse, run_orchestrator, run_stages
from campaign_tool.records import rehearse_comparison as driver
from campaign_tool.records.outcomes import comparison, factsheets

STEPS = ["save_mail", "extract_text", "write_factsheets", "write_comparison", "comparison_rows_cite_facts",
         "single_agency_is_not_a_comparison", "no_sends"]
VALLEY, HARBOR = "ca-valley-pd", "ca-harbor-pd"
needs_parsers = unittest.skipIf(native_extract.missing_parsers(), "UNSUPPORTED_RUNTIME: pypdf/openpyxl missing")


class Fence:
    """Record every write, socket connect and send-capable call made while the driver runs."""

    def __init__(self, sandbox):
        self.sandbox, self.writes, self.outside, self.connects, self.sends = sandbox, [], [], [], []

    def write(self, path, fd=None):
        target = Path(os.fsdecode(path))
        if fd is not None and not target.is_absolute():  # relative to an open directory: resolve through /proc
            target = Path(os.readlink("/proc/self/fd/%d" % fd)) / target
        target = Path(os.path.abspath(target))
        if target != Path(os.devnull):  # subprocess pipes are not file writes
            self.writes.append(target)
            if not target.is_relative_to(self.sandbox):
                self.outside.append(target)

    @contextlib.contextmanager
    def __call__(self):
        real_io, real_os, real_connect, real_sqlite = io.open, os.open, socket.socket.connect, sqlite3.connect

        def file_open(file, mode="r", *args, **kwargs):
            if not isinstance(file, int) and any(flag in mode for flag in "wax+"):
                self.write(file)
            return real_io(file, mode, *args, **kwargs)

        def fd_open(path, flags, *args, dir_fd=None, **kwargs):
            if flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC):
                self.write(path, dir_fd)
            return real_os(path, flags, *args, dir_fd=dir_fd, **kwargs)

        def connect(sock, address):
            self.connects.append(address)
            return real_connect(sock, address)

        def database(name, *args, **kwargs):
            text = os.fsdecode(name)
            path, _, query = text[5:].partition("?") if text.startswith("file:") else (text, "", "")
            if path != ":memory:" and "mode=ro" not in query:
                self.write(path)
            return real_sqlite(name, *args, **kwargs)

        def watch(owner, name, index=0):
            real = getattr(owner, name)

            def wrapper(*args, **kwargs):
                self.write(args[index], kwargs.get("dir_fd", kwargs.get("dst_dir_fd")))
                return real(*args, **kwargs)
            return patch.object(owner, name, wrapper)

        senders = [patch.object(owner, name, side_effect=AssertionError) for owner, name in (
            (smtplib, "SMTP"), (smtplib, "SMTP_SSL"), (outbox.Outbox, "send"), (mail_send, "send_one"),
            (run_orchestrator, "cmd_notify_fallback"), (urllib.request, "urlopen"))]
        with contextlib.ExitStack() as stack:
            for fence in (patch.object(io, "open", file_open), patch.object(builtins, "open", file_open),
                          patch.object(os, "open", fd_open), patch.object(socket.socket, "connect", connect),
                          patch.object(sqlite3, "connect", database), watch(os, "mkdir"), watch(os, "replace", 1),
                          watch(os, "link", 1)):
                stack.enter_context(fence)
            self.sends = [stack.enter_context(sender) for sender in senders]
            yield self


class DriverTestCase(unittest.TestCase):
    def sandbox_dir(self):
        parent = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        (parent / "sandbox").mkdir(mode=0o700)
        return parent / "sandbox"

    def rows(self, sandbox, label="pair"):
        directory = next((sandbox / "proof" / label / "project" / "run-outputs").glob("run_*/"))
        return json.loads((directory / "comparison.json").read_text())["rows"]


@needs_parsers
class HappyRunTests(DriverTestCase):
    @classmethod
    def setUpClass(cls):
        cls.parent = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.parent.cleanup)
        cls.sandbox = Path(cls.parent.name).resolve() / "sandbox"
        cls.sandbox.mkdir(mode=0o700)
        cls.fence, cls.touched, touch = Fence(cls.sandbox), [], Path.touch

        def first_touch(path, *args, **kwargs):
            cls.touched.append((path, sorted(p.name for p in cls.sandbox.iterdir())))
            return touch(path, *args, **kwargs)

        previous = os.umask(0o022)
        try:
            with cls.fence(), patch.object(Path, "touch", first_touch):
                cls.result = driver.run(cls.sandbox)
        finally:
            cls.umask_after = os.umask(previous)

    def test_01_c1_seven_named_steps_with_hashed_sandbox_outputs(self):
        """C1: seven steps in order, output hashes match sandbox files, marker first, umask restored.

        Bug caught: a skipped or renamed step, outputs outside the sandbox, replay's process-wide umask 077 left behind."""
        result, marker = self.result, self.sandbox / run_stages.SANDBOX_MARKER
        self.assertEqual((result["stages"], result["manual_steps"], result["codes"]), (["10"], [], []))
        self.assertEqual([(s["name"], s["code"]) for s in result["steps"]], [(name, "OK") for name in STEPS])
        for out in (out for step in result["steps"] for out in step["outputs"]):
            self.assertEqual(hashlib.sha256((self.sandbox / out["path"]).read_bytes()).hexdigest(), out["sha256"])
        self.assertTrue(all(step["outputs"] for step in result["steps"]))
        self.assertEqual((driver.STAGES, stat.S_IMODE(marker.stat().st_mode), self.touched[0], self.umask_after),
                         (("10",), 0o600, (marker, []), 0o022))

    def test_02_c2_both_agencies_are_compared_with_different_statuses(self):
        """C2: comparison.json (real writer) rates both agencies on the same fields and the ratings differ.

        Bug caught: a writer that rates one agency only, or both alike, still prints a file."""
        status = {(r["agency_id"], r["field"]): r["status"] for r in self.rows(self.sandbox)}
        self.assertEqual([status[VALLEY, "retention"], status[HARBOR, "retention"]], ["meets", "gap"])
        self.assertEqual([status[VALLEY, "sharing_limits"], status[HARBOR, "sharing_limits"]], ["gap", "meets"])
        sheets = next((self.sandbox / "proof/pair/project/run-outputs").glob("run_*/agency-factsheets.json"))
        self.assertEqual(json.loads(sheets.read_text())["schema"], factsheets.SCHEMA)

    def test_03_c3_each_compared_value_cites_its_own_sandbox_document(self):
        """C3: every observed row cites a document of the sandbox ledger that belongs to that agency.

        Bug caught: a citation copied from the other agency, or a made-up document id."""
        ledger = sqlite3.connect("file:%s?mode=ro" % (self.sandbox / "proof/pair/project/processing-v1/ledger.sqlite"), uri=True)
        known = {r[0] for r in ledger.execute("SELECT sha256 FROM originals")}
        ledger.close()
        pdfs = {a[0]: hashlib.sha256(driver._pdf(a[2])).hexdigest() for a in driver._AGENCIES}
        cited = [r for r in self.rows(self.sandbox) if "observed" in r]
        self.assertEqual({r["agency_id"] for r in cited}, {VALLEY, HARBOR})
        for row in cited:
            self.assertEqual([c["source_id"] for c in row["evidence"]], ["sha256:" + pdfs[row["agency_id"]]])
            self.assertIn(pdfs[row["agency_id"]], known)

    def test_04_c4_single_agency_run_has_no_peer(self):
        """C4: with one agency the writer rates only that agency; the other agency appears nowhere.

        Bug caught: a writer that pads a missing peer or reuses another run's agency list."""
        directory = next((self.sandbox / "proof/single/project/run-outputs").glob("run_*/"))
        self.assertEqual({r["agency_id"] for r in self.rows(self.sandbox, "single")}, {VALLEY})
        self.assertEqual(json.loads((directory / "comparison.json").read_text())["agency_count"], 1)
        for name in ("comparison.json", "comparison.md"):
            self.assertNotIn(HARBOR, (directory / name).read_text())

    def test_05_c5_fictional_inputs_and_nothing_leaves_the_sandbox(self):
        """C5: fictional names and .invalid addresses; writes stay in the sandbox, connects are loopback, no send.

        Bug caught: a pasted real agency or address, a stray write, a remote connection, a mail or notifier call."""
        text = "".join(p.read_text(errors="replace") for p in (self.sandbox / "inputs").rglob("*") if p.is_file())
        self.assertEqual(set(re.findall(r"@([\w.-]+)", text)), {"fictional-valley.example.invalid", "campaign.example.invalid"})
        self.assertTrue(all(name.startswith("Fictional ") for _, name, *_ in driver._AGENCIES))
        self.assertGreater(len(self.fence.writes), 20)  # the spy saw real activity, not nothing
        self.assertEqual(self.fence.outside, [])
        self.assertTrue(self.fence.connects and all(a[0] in ("127.0.0.1", "::1") for a in self.fence.connects))
        self.assertEqual([sender.call_count for sender in self.fence.sends], [0] * 6)

    def test_06_receipt_qualifies_and_the_runner_selects_the_driver(self):
        """C1: a receipt built from this run qualifies, and `records rehearse --stage 10` selects this driver.

        Bug caught: an output hash or marker the receipt cannot confirm, or a driver the runner skips."""
        stamp = datetime.now(timezone.utc)
        receipt = rehearsal_receipt.build("rehearse_comparison", self.result, self.sandbox, stamp, stamp, "a" * 40)
        self.assertEqual((receipt["qualified"], receipt["qualify_codes"]), (True, []))
        self.assertEqual(rehearsal_receipt.qualify(receipt, now=stamp, target_engine="a" * 40), (True, []))
        with redirect_stdout(io.StringIO()) as listing:
            self.assertEqual(rehearse.main(["--stage", "10", "--list"]), 0)
        self.assertIn("rehearse_comparison 10:", listing.getvalue())


class FenceTests(DriverTestCase):
    def test_07_fence_flags_a_write_outside_the_sandbox(self):
        """C5 meta: the write spy must fail loudly on an outside write; otherwise test 05 proves nothing."""
        sandbox = self.sandbox_dir()
        with Fence(sandbox)() as fence:
            (sandbox.parent / "outside.txt").write_text("x")
            (sandbox / "inside.txt").write_text("x")
        self.assertEqual(fence.outside, [sandbox.parent / "outside.txt"])


@needs_parsers
class ControlTests(DriverTestCase):
    """Each control breaks one real input or writer; the driver must stop at the named step with its named code."""

    def drive(self, *patches):
        sandbox = self.sandbox_dir()
        with contextlib.ExitStack() as stack:
            for item in patches:
                stack.enter_context(item)
            result = driver.run(sandbox)
        return result, sandbox

    def assertStopsAt(self, result, name, code):
        steps = [(s["name"], s["code"]) for s in result["steps"]]
        self.assertEqual(steps, [(n, "OK") for n in STEPS[:STEPS.index(name)]] + [(name, code)], result)
        self.assertEqual((result["codes"], result["manual_steps"]), ([code], []))

    def test_10_save_mail_control(self):
        """C1: a mail store that reports a failure fails save_mail and no later step runs.

        Bug caught: a driver that ignores a failed stage row and goes on to read stale files."""
        result, sandbox = self.drive(patch("campaign_tool.records.run.Pipeline.ingest_inbox", return_value={"failures": 1}))
        self.assertStopsAt(result, "save_mail", "SAVE_MAIL_NOT_OK")
        self.assertFalse((sandbox / "proof" / "single").exists())

    def test_11_extract_text_control(self):
        """C1: attachments that cannot be read fail extract_text, not a later step.

        Bug caught: unread documents quietly turning into an empty comparison."""
        junk = lambda lines: b"not a pdf: " + lines[0].encode()  # noqa: E731 - distinct bytes per agency
        self.assertStopsAt(self.drive(patch.object(driver, "_pdf", side_effect=junk))[0],
                           "extract_text", "EXTRACT_TEXT_INCOMPLETE")

    def test_12_factsheets_control(self):
        """C1: an outputs stage whose fact-sheet writer wrote nothing fails write_factsheets.

        Bug caught: the stage passing on counts alone while agency-factsheets.json is absent."""
        self.assertStopsAt(self.drive(patch.object(factsheets, "write_run_factsheets"))[0],
                           "write_factsheets", "FACTSHEETS_INVALID")

    def test_13_comparison_json_control(self):
        """C2: comparison.json that is not strict JSON fails write_comparison.

        Bug caught: NaN/Infinity (a division in a new field) accepted because Python's json reads them."""
        real = e2e_proof.replay

        def corrupt(*args, **kwargs):
            report = real(*args, **kwargs)
            path = Path(args[1]) / kwargs["label"] / "project/run-outputs" / report["run_id"] / "comparison.json"
            path.write_text(path.read_text().replace('"agency_count": 2', '"agency_count": NaN'))
            return report

        self.assertStopsAt(self.drive(patch.object(e2e_proof, "replay", side_effect=corrupt))[0],
                           "write_comparison", "COMPARISON_JSON_INVALID")

    def test_14_comparison_pair_control(self):
        """C2: rating one agency as nothing, or both alike, is not a comparison of the two.

        Bug caught: the writer dropping an agency's facts, or the seed giving both agencies the same facts."""
        real = comparison.compare

        for name, facts_for in (("blank", lambda facts: {}), ("alike", lambda facts: facts)):
            def fake(agencies, facts_for=facts_for, **kw):
                valley = next(a["facts"] for a in agencies if a["agency_id"] == VALLEY)
                return real([dict(a, facts=facts_for(valley)) if a["agency_id"] == HARBOR else a for a in agencies], **kw)

            with self.subTest(name):
                self.assertStopsAt(self.drive(patch.object(comparison, "compare", side_effect=fake))[0],
                                   "write_comparison", "COMPARISON_PAIR_MISSING")

    def test_15_citation_controls(self):
        """C3: a compared value with no citation, another agency's document or a document not in the sandbox fails.

        Bug caught: the writer dropping evidence, mixing up agencies, or citing an id nothing in the ledger holds."""
        real = comparison.compare

        def uncited(report):
            next(r for r in report["rows"] if r["status"] == "meets")["evidence"] = []

        def swapped(report):
            theirs = next(r for r in report["rows"] if r["agency_id"] == VALLEY and r["evidence"])["evidence"]
            for row in (r for r in report["rows"] if r["agency_id"] == HARBOR and r["evidence"]):
                row["evidence"] = copy.deepcopy(theirs)

        def invented(report):
            for cite in (c for row in report["rows"] for c in row["evidence"]):
                cite["source_id"] = "sha256:" + "0" * 64

        for edit, code in ((uncited, "COMPARISON_ROW_UNCITED"), (swapped, "COMPARISON_CITES_OTHER_AGENCY"),
                           (invented, "COMPARISON_CITES_OUTSIDE_SANDBOX")):
            def fake(agencies, edit=edit, **kw):
                report = real(agencies, **kw)
                edit(report)
                return report

            with self.subTest(code):
                self.assertStopsAt(self.drive(patch.object(comparison, "compare", side_effect=fake))[0],
                                   "comparison_rows_cite_facts", code)

    def test_16_single_agency_control(self):
        """C4: a writer that invents a differently rated peer for a lone agency fails the control.

        Bug caught: padding a one-agency run so it looks like a comparison."""
        real = comparison.compare

        def padded(agencies, **kw):
            report = real(agencies, **kw)
            if len(agencies) == 1:
                flip = {"meets": "gap", "gap": "meets"}
                report["rows"] += [dict(copy.deepcopy(r), agency_id="ca-invented-peer-pd", status=flip.get(r["status"], r["status"]))
                                   for r in report["rows"]]
                report["agency_count"] = 2
            return report

        self.assertStopsAt(self.drive(patch.object(comparison, "compare", side_effect=padded))[0],
                           "single_agency_is_not_a_comparison", "SINGLE_AGENCY_PEER_INVENTED")

    def test_17_no_sends_control(self):
        """C5: a queued outgoing file, or a replay report that says something was sent, fails no_sends.

        Bug caught: a stage that queues or sends mail while the proof still prints PASS."""
        real = e2e_proof.replay

        def queued(*args, **kwargs):
            report = real(*args, **kwargs)
            (Path(args[1]) / kwargs["label"] / "project/processing-v1/outbox/queued.json").write_text("{}")
            return report

        def said_sent(*args, **kwargs):
            return dict(real(*args, **kwargs), sent=True)

        for name, fake in (("queued", queued), ("said_sent", said_sent)):
            with self.subTest(name):
                self.assertStopsAt(self.drive(patch.object(e2e_proof, "replay", side_effect=fake))[0],
                                   "no_sends", "SEND_OR_PUBLISH_DETECTED")


class InterfaceTests(DriverTestCase):
    def test_20_run_and_cli_refuse_unusable_sandboxes_and_report_status(self):
        """Interface: run needs an empty 0700 directory; main prints the result and exits 0 only when all steps are OK.

        Bug caught: running in a shared or dirty directory; exit 0 on a failed step or manual work."""
        sandbox = self.sandbox_dir()
        (sandbox / "keep").write_text("keep")
        with self.assertRaises(ValueError):
            driver.run(sandbox)
        (sandbox / "keep").unlink()
        sandbox.chmod(0o755)
        with self.assertRaises(ValueError):
            driver.run(sandbox)
        with self.assertRaises(FileNotFoundError):
            driver.run(sandbox.parent / "absent")
        self.assertEqual(list(sandbox.iterdir()), [])
        ok = {"stages": ["10"], "steps": [{"name": "save_mail", "code": "OK", "outputs": []}], "manual_steps": [], "codes": []}
        failed = dict(ok, steps=[{"name": "save_mail", "code": "SAVE_MAIL_NOT_OK", "outputs": []}])
        for outcome, status in ((ok, 0), (failed, 1), (dict(ok, manual_steps=["by hand"]), 1)):
            with redirect_stdout(io.StringIO()) as out, patch.object(driver, "run", return_value=outcome):
                self.assertEqual((driver.main(["--sandbox", str(sandbox.parent / "new")]), json.loads(out.getvalue())),
                                 (status, outcome))
            (sandbox.parent / "new").rmdir()
        (sandbox / "keep").write_text("keep")
        for argv in ([], ["--sandbox", str(sandbox)]):
            with redirect_stderr(io.StringIO()), patch.object(driver, "run") as launch, self.assertRaises(SystemExit) as caught:
                driver.main(argv)
            self.assertEqual((caught.exception.code, launch.call_count), (2, 0))


if __name__ == "__main__":
    unittest.main()
