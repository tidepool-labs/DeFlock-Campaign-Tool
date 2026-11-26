"""golive_step9: READY only when D1, anchor, D5 and D6 all name the one current seal (synthetic, no network)."""
import io
import json
import os
import socket
import tempfile
import threading
import http.client
import urllib.parse
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from campaign_tool import approval
from campaign_tool.records import admin_panel, console_apply, console_export, console_journal
from campaign_tool.records import approval_queue as aq
from campaign_tool.records import console_page_golive, golive_ledger
from campaign_tool.records import golive_phone_check_packet as packet
from campaign_tool.records import golive_step9 as step9
from tests.records import test_mail_drafts as tmd
from tests.records import test_golive_ledger as tgl

RUN1, RUN2, RUN3 = "run_20261125T070000Z_0000d521", "run_20261125T071000Z_0000d522", "run_20261125T072000Z_0000d523"
HEAD = "a" * 40
DENIAL = {"signed_out": {"/": "ACCESS_LOGIN", "/golive": "DENIED"}, "unauthorized": {"/": "DENIED", "/golive": "DENIED"}}


def sha(raw):
    return step9._sha(raw)


class Step9(tmd.Base):
    def setUp(self):
        super().setUp()
        self.base = Path(tempfile.mkdtemp(prefix="step9-", dir=self.root)).resolve()
        self.console = self.base / "console"
        self.console.mkdir(mode=0o700)
        self.j = self.base / "journal"
        self.j.mkdir(mode=0o700)
        self.ledger_fx = tgl.GoLiveLedgerTests()
        self.ledger_fx.setUp()
        self.addCleanup(self.ledger_fx.doCleanups)

    def seal(self, run, anchor_unavailable=False):
        (self.project / "run-outputs" / run).mkdir(parents=True, mode=0o700, exist_ok=True)
        (self.project / "run-outputs" / run / "text-state.json").write_text("{}\n")
        dest = self.base / ("build-" + run)
        dest.mkdir(mode=0o700)
        if anchor_unavailable:
            with mock.patch.object(approval, "_require_root", side_effect=approval.TrustUnavailable("missing")):
                console_export.build(str(self.project), run, str(dest))
        else:
            console_export.build(str(self.project), run, str(dest))
        stream = io.BytesIO()
        console_export.write_tar(str(dest), stream)
        stream.seek(0)
        name = console_export.install(str(self.console), stream)
        top = self.console / "exports" / name
        return name, (top / console_export.MANIFEST).read_bytes(), (top / console_export.MODEL_DIR / "queue.json").read_bytes()

    def post(self, port, path, fields):
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)  # create_connection is a tripwire in the fixture
        sock.settimeout(10)
        sock.connect(("127.0.0.1", port))
        conn.sock = sock
        try:
            conn.request("POST", path, urllib.parse.urlencode(fields),
                         {"Origin": "http://127.0.0.1:%d" % port, "Content-Type": "application/x-www-form-urlencoded"})
            resp = conn.getresponse()
            return resp.status, resp.read()
        finally:
            conn.close()

    def cli_bytes(self, fx):
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(golive_ledger.main([*fx.args, "--json"]), 0)
        return out.getvalue().encode()

    def page_bytes(self, fx):
        sent = []
        handler = SimpleNamespace(path="/golive", options={"run_report_path": fx.reports.parent / "run-report.json"},
                                  _send=lambda status, html: sent.append((status, html.encode())))
        self.assertTrue(console_page_golive.serve(handler))
        return sent[0][1]

    def receipts(self, seal, cli, page, capture_seal=None, head=HEAD):
        binding = {"origin": packet.ORIGIN, "seal_sha256": seal, "console_head": head}
        capture = {"scope": "hosted_protected", "binding": {**binding, "seal_sha256": capture_seal or seal},
                   "cli_sha256": sha(cli), "page_sha256": sha(page)}
        return capture, {"scope": "hosted_protected", "binding": binding, **DENIAL}

    def evaluate(self, ident, revision, decision_id, journal, cli, page, capture, denial, head=HEAD):
        return step9.evaluate(export_root=str(self.console), queue_id=ident, revision=revision, decision_id=decision_id,
                              journal_bytes=journal, console_head=head, cli_bytes=cli, page_bytes=page,
                              capture=capture, denial=denial)

    def test_one_current_seal_ready_and_every_mismatch_holds(self):
        self.ledger_fx.write_record(tgl.record("1", "live"))
        self.ledger_fx.write_record(tgl.record("2", "live"))
        cli, page = self.cli_bytes(self.ledger_fx), self.page_bytes(self.ledger_fx)

        self.build_ledger()
        row = self.draft(approve=False)
        ident, n = row["id"], row["revision"]
        name1, man1, q1 = self.seal(RUN1)
        seal1 = sha(man1)

        server = admin_panel.build_server(export_root=str(self.console), run_id="auto", actor="operator", system=False,
                                          decisions_dir=str(self.j))
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        thread.start()
        try:
            signature = self.signer.sign(aq.approval_payload(self.project, ident))
            erow = next(r for r in json.loads(q1)["rows"] if r["id"] == ident)
            status, _ = self.post(server.server_port, "/approvals/approve",
                                  {"csrf_token": server.csrf_token, "id": ident, "signature": signature,
                                   "body": erow["body"], "expected_revision": str(erow["revision"])})
        finally:
            server.shutdown()
            thread.join(10)
            server.server_close()
        self.assertEqual(status, 303)
        journal = (self.j / console_journal.FILE).read_bytes()
        decision_id = json.loads(journal.splitlines()[0])["entry_id"]

        # Seal 1: decision journaled, nothing applied or placed: the reply is not placed on the current seal.
        cap1, den1 = self.receipts(seal1, cli, page)
        held = self.evaluate(ident, n, decision_id, journal, cli, page, cap1, den1)
        self.assertEqual((held["verdict"], held["reason"], held["seal_sha256"]), ("HOLD", "D1_NOT_PASS", seal1))

        with mock.patch("sys.stdout", io.StringIO()):
            self.assertEqual(console_apply.main(["--project", str(self.project), "--from-journal",
                                                 str(self.j / console_journal.FILE)]), 0)
        self.assertEqual(self.place()["placed"], 1)
        self.assertEqual(len(self.fake.messages), 1)

        name2, man2, _ = self.seal(RUN2)
        seal2 = sha(man2)
        cap2, den2 = self.receipts(seal2, cli, page)
        ready = self.evaluate(ident, n + 1, decision_id, journal, cli, page, cap2, den2)
        self.assertEqual(ready["verdict"], "READY", ready)
        self.assertEqual((ready["seal_sha256"], ready["export"], ready["queue_id"], ready["revision"],
                          ready["signed_revision"], ready["decision_id"]), (seal2, name2, ident, n + 1, n, decision_id))
        self.assertEqual(ready["phone_check"]["live_stage_ids"], ["1", "2"])
        self.assertEqual((ready["phone_check"]["pipeline_live"], ready["phone_check"]["pipeline_total"]), (2, 22))
        self.assertNotIn(tmd.CANARY, json.dumps(ready))

        def reason(**kw):
            args = dict(ident=ident, revision=n + 1, decision_id=decision_id, journal=journal, cli=cli, page=page,
                        capture=cap2, denial=den2)
            args.update(kw)
            out = self.evaluate(**args)
            self.assertEqual(out["verdict"], "HOLD")
            return out["reason"]

        # Pending revision N is not the placed revision on this seal.
        self.assertEqual(reason(revision=n), "D1_NOT_PASS")
        self.assertEqual(reason(decision_id="f" * 64), "DECISION_NOT_FOUND")
        self.assertEqual(reason(journal=journal + journal), "DECISION_AMBIGUOUS")
        # The journal entry bytes changed: its entry id no longer binds the body.
        tampered = json.loads(journal.splitlines()[0])
        tampered["revision_seen"] = n + 1
        self.assertEqual(reason(journal=json.dumps(tampered).encode() + b"\n"), "D5_NOT_APPLIED")
        # Captures and denial receipts taken for seal 1, for another console head, or missing.
        self.assertEqual(reason(capture=cap1, denial=den1), "PACKET_HOLD")
        self.assertEqual(reason(capture=self.receipts(seal2, cli, page, capture_seal=seal1)[0]), "PACKET_HOLD")
        self.assertEqual(reason(denial=None), "PACKET_HOLD")
        other_cap, other_den = self.receipts(seal2, cli, page, head="b" * 40)
        self.assertEqual(reason(capture=other_cap, denial=other_den), "PACKET_HOLD")
        # Same count, different IDs on the Go-live page.
        alt = tgl.GoLiveLedgerTests()
        alt.setUp()
        try:
            alt.write_record(tgl.record("1", "live"))
            alt.write_record(tgl.record("3", "live"))
            alt_page = self.page_bytes(alt)
        finally:
            alt.doCleanups()
        alt_cap, _ = self.receipts(seal2, cli, alt_page)
        self.assertEqual(reason(page=alt_page, capture=alt_cap), "PACKET_HOLD")
        # `current` moves while the checks run.
        names = iter([name2, name1])
        with mock.patch.object(step9, "current_name", side_effect=lambda root: next(names)):
            self.assertEqual(reason(), "CURRENT_MOVED")

        # CLI on saved files: READY exits 0 and prints no draft text.
        files = {}
        for key, raw in (("journal", journal), ("cli", cli), ("page", page), ("capture", json.dumps(cap2).encode()),
                         ("denial", json.dumps(den2).encode())):
            files[key] = self.base / ("in-" + key)
            files[key].write_bytes(raw)
        argv = ["--export-root", str(self.console), "--queue-id", ident, "--revision", str(n + 1),
                "--decision-id", decision_id, "--journal", str(files["journal"]), "--console-head", HEAD,
                "--cli-json", str(files["cli"]), "--page", str(files["page"]), "--capture", str(files["capture"]),
                "--denial", str(files["denial"])]
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(step9.main(argv), 0)
        self.assertEqual(json.loads(out.getvalue())["verdict"], "READY")
        self.assertNotIn(tmd.CANARY, out.getvalue())
        (self.base / "link").symlink_to(files["denial"])
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(step9.main([*argv[:-1], str(self.base / "link")]), 1)
        self.assertEqual(json.loads(out.getvalue())["reason"], "INPUT_UNREADABLE")
        # Review t_c41323fd item 1: over-nested JSON is a HOLD, never a traceback; an over-nested journal line
        # beside the real decision is skipped like a torn line.
        deep = b"[" * 30000 + b"]" * 30000
        files["capture"].write_bytes(deep)
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(step9.main(argv), 1)
        self.assertEqual(json.loads(out.getvalue())["reason"], "INPUT_UNREADABLE")
        files["capture"].write_bytes(json.dumps(cap2).encode())
        self.assertEqual(self.evaluate(ident, n + 1, decision_id, deep + b"\n" + journal, cli, page, cap2,
                                       den2)["verdict"], "READY")
        with mock.patch.object(step9.golive_phone_check_packet, "build", side_effect=RecursionError):
            self.assertEqual(reason(), "INPUT_UNREADABLE")
        # Review t_c41323fd item 2: a FIFO with no writer is refused at once instead of blocking the check.
        fifo = self.base / "fifo"
        os.mkfifo(fifo)
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(step9.main([*argv[:-1], str(fifo)]), 1)
        self.assertEqual(json.loads(out.getvalue())["reason"], "INPUT_UNREADABLE")

        # A later seal exported while the approval anchor is unavailable becomes current: withheld.
        _, man3, _ = self.seal(RUN3, anchor_unavailable=True)
        cap3, den3 = self.receipts(sha(man3), cli, page)
        self.assertEqual(reason(capture=cap3, denial=den3), "ANCHOR_UNAVAILABLE")

    def test_unreadable_current_holds(self):
        out = step9.evaluate(export_root=str(self.console), queue_id="q", revision=1, decision_id="f" * 64,
                             journal_bytes=b"", console_head=HEAD, cli_bytes=b"{}", page_bytes=b"x", capture={},
                             denial={})
        self.assertEqual((out["verdict"], out["reason"], out["seal_sha256"]), ("HOLD", "CURRENT_UNREADABLE", None))
