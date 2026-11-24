"""Synthetic copied journals built with the real public_packets writer; nothing here touches a live project."""

import ast
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from io import StringIO
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import time
import unittest
from unittest import mock

from campaign_tool.records import golive_adapt_findings as adapter, golive_evidence, golive_ledger
from campaign_tool.records import public_packets as pp

T0 = 1_793_000_000
DAY = 86400
BAD = "FINDINGS_EVIDENCE_BAD_INPUT"
SHA = "a" * 64


def utc(ts):
    return datetime.fromtimestamp(ts, timezone.utc)


def packet(kind="finding", source="f1", **extra):
    base = {"kind": kind, "agency_key": "ex-pd", "request_id": "r1", "source": source, "title": "Retention period",
            "text": "Example PD's released policy sets a 30 day retention period.",
            "documents": [{"sha256": SHA, "date_received": "2026-11-03", "redaction_ids": ["red-1"]}],
            "citations": [{"ref": "doc-1 p2", "verified": True}]}
    if kind == "finding":
        base["finding_state"] = pp.PUBLIC_STATE
    if kind == "site_update":
        base.pop("documents"), base.pop("citations")
    return {**base, **extra}


def fake_verify(data, signature):
    return signature.split(":")[1]


class FindingsAdapterTests(unittest.TestCase):
    def setUp(self):
        root = Path(self.enterContext(TemporaryDirectory()))
        self.project = str(root / "project")
        self.copy = root / "copy"
        self.copy.mkdir(mode=0o700)
        self.enterContext(mock.patch.object(pp.approval, "verify", fake_verify))

    def fresh(self):
        self.project += "x"

    def journal(self, *packets, now=T0, how=None, approve=False):
        """Propose each packet in a scratch project, optionally settle the last one, then copy the journal."""
        got = None
        for item in packets:
            got = pp.propose(self.project, item, now=now)
        if got and approve:
            pp.approve(self.project, got["id"], "SIG:g@x:00", now=now)
        if got and how:
            pp.decide(self.project, got["id"], how, "g@x", now=now)
        raw = Path(self.project, pp.JOURNAL).read_bytes()
        target = self.copy / pp.JOURNAL
        target.write_bytes(raw)
        return target

    def refused(self, journal, at=T0 + 60):
        with self.assertRaises(ValueError) as caught:
            adapter.records(journal, utc(at))
        self.assertIs(type(caught.exception), ValueError)
        self.assertEqual(str(caught.exception), BAD)

    def test_c2_pending_finding_gives_a_stage_16_pass_row_the_ledger_reads_live(self):
        """C2/C1: a bug stamping the wrong stage/result/source, or a row the ledger cannot read as LIVE, fails here."""
        path = self.journal(packet())
        (row,) = adapter.records(path, utc(T0 + 60))
        self.assertEqual(row, {
            "schema": "records-golive-evidence-v1", "stage": "16", "kind": "live", "result": "pass",
            "recorded_at": utc(T0 + 60).strftime("%Y-%m-%dT%H:%M:%SZ"), "engine": None, "actor": "operator",
            "codes": [], "source": {"schema": "records-public-packets-v1", "name": pp.JOURNAL,
                                    "sha256": sha256(path.read_bytes()).hexdigest()}})
        golive_evidence.validate(row)
        stages = {r["stage"]: r for r in golive_ledger.build([], [row], utc(T0 + 120))}
        self.assertEqual((stages["16"]["state"], stages["16"]["live"]["status"]), ("LIVE", "evidence"))
        self.fresh()  # an approved finding counts too, and so does one written afterwards
        path = self.journal(packet(), approve=True)
        self.assertEqual(adapter.records(path, utc(T0 + 60))[0]["result"], "pass")
        written = {"schema": pp.SCHEMA, "seq": 2, "at": "x", "ts": T0, "event": "written", "id": pp.packet_id(packet()),
                   "revision": 0, "file": "site-changes/x.json", "file_sha256": SHA}
        with path.open("a") as out:
            out.write(json.dumps(written) + "\n")
        self.assertEqual(adapter.records(path, utc(T0 + 60))[0]["result"], "pass")
        self.assertEqual(adapter.records(path, utc(T0 + 30 * DAY))[0]["result"], "pass")  # written never expires

    def test_c2_document_summary_and_site_update_packets_never_qualify(self):
        """C2: a bug counting any open packet (not just kind finding) would pass a journal with no finding."""
        others = (packet("document"), packet("summary", source="s1"), packet("site_update", source="u1"))
        self.refused(self.journal(*others))

    def test_c2_expired_rejected_or_cancelled_finding_is_refused(self):
        """C2: a bug that ignores expiry or dead statuses, or lets a live finding mask a dead one, passes stale proof."""
        self.refused(self.journal(packet()), at=T0 + 14 * DAY + 1)
        path = self.journal(packet())  # healthy control: exactly at the expiry limit is still pending
        self.assertEqual(adapter.records(path, utc(T0 + 14 * DAY))[0]["result"], "pass")
        for how in ("rejected", "cancelled"):
            with self.subTest(how):
                self.fresh()
                self.refused(self.journal(packet(), how=how))
                self.fresh()  # f1 stays pending while f2 is dead: a live finding must not mask a dead one
                path = self.journal(packet(), now=T0)
                pp.propose(self.project, packet(source="f2"), now=T0)
                pp.decide(self.project, pp.packet_id(packet(source="f2")), how, "g@x", now=T0)
                path.write_bytes(Path(self.project, pp.JOURNAL).read_bytes())
                self.refused(path)

    def test_c3_sandbox_marker_and_malformed_journals_are_refused(self):
        """C3: a bug skipping the marker check turns a rehearsal journal into live evidence; bad bytes must not pass."""
        path = self.journal(packet())
        good = path.read_bytes()
        first = json.loads(good.splitlines()[0])
        cases = {
            "empty": b"", "unterminated": good.rstrip(b"\n"), "not json": b"{\n", "nan": b'{"ts": NaN}\n',
            "wrong schema": good.replace(pp.SCHEMA.encode(), b"records-other-v1"),
            "after observed_at": good.replace(str(T0).encode(), str(T0 + 3600).encode()),
            "id mismatch": good.replace(first["id"].encode(), b"0" * 16),
            "uncited finding": good.replace(b'"citations":[{"ref":"doc-1 p2","verified":true}]', b'"citations":[]'),
            "approved lacks fields": good + json.dumps(dict(first, event="approved")).encode() + b"\n",
        }
        for name, data in cases.items():
            with self.subTest(name):
                path.write_bytes(data)
                self.refused(path)
        path.write_bytes(good)
        link = self.copy / "link.jsonl"
        link.symlink_to(path)
        self.refused(link)
        self.refused(self.copy / "missing.jsonl")
        marker = self.copy / ".t12-sandbox-v1"
        marker.write_bytes(b'{"sandbox_marker_version":1}\n')
        self.refused(path)

    def test_c3_malformed_non_packet_events_and_transitions_are_refused(self):
        """C3: fold() skips events it cannot place, so a bug that trusted it would turn a tampered copy (a valid
        finding plus a rejected event with no revision, an orphan, stale or out-of-order transition) into a live pass."""
        path = self.journal(packet(), approve=True)
        good = path.read_bytes()
        ident = pp.packet_id(packet())
        signature = json.loads(good.splitlines()[1])["signature"]
        self.assertEqual(adapter.records(path, utc(T0 + 60))[0]["result"], "pass")  # control: the intact copy passes
        gone = object()

        def extra(**fields):
            base = {"schema": pp.SCHEMA, "seq": 2, "at": "x", "ts": T0, "event": "rejected", "id": ident,
                    "revision": 0, "actor": "g@x"}
            event = {k: v for k, v in {**base, **fields}.items() if v is not gone}
            return good + json.dumps(event).encode() + b"\n"

        # control: a well-formed rejection passes the structural check (the finding is then dead, a separate refusal)
        self.assertEqual(len(adapter._events(extra(), T0 + 60)), 3)
        self.refused(self.write(path, extra()))
        cases = {
            "rejected missing revision": extra(revision=gone), "rejected bad revision type": extra(revision=True),
            "rejected stale revision": extra(revision=1), "rejected orphan id": extra(id="0" * 16),
            "rejected missing id": extra(id=gone), "rejected missing actor": extra(actor=gone),
            "rejected bad actor": extra(actor="../x"), "rejected bad reason": extra(reason=""),
            "unknown event kind": extra(event="mystery"), "seq gap": extra(seq=7), "missing at": extra(at=gone),
            "approved twice": extra(event="approved", principal="g@x", signature=signature),
            "approved without principal": extra(event="approved", principal=gone, signature="s"),
            "written without file": extra(event="written", file=gone, file_sha256=SHA),
            "written bad digest": extra(event="written", file="a.json", file_sha256="zz"),
            "bad switch state": extra(event="switch", state="maybe", principal="g@x", round=0, id=gone, revision=gone),
            "switch without principal": extra(event="switch", state="off", round=0, id=gone, revision=gone, actor=gone),
            "packet revision skipped": extra(event="packet", revision=5, packet=packet(), digest="d"),
        }
        for name, data in cases.items():
            with self.subTest(name):
                with self.assertRaises(ValueError):
                    adapter._events(data, T0 + 60)
                self.refused(self.write(path, data))

    def write(self, path, data):
        path.write_bytes(data)
        return path

    def test_c1_cli_prints_rows_and_exits_two_with_only_the_code(self):
        """C1/C3: a CLI that echoes paths or text, or exits 0 on refusal, would leak or hide a bad input."""
        path = self.journal(packet(), now=int(time.time()) - 60)
        for flags in ([], ["--json"]):
            out, err = StringIO(), StringIO()
            with redirect_stdout(out), redirect_stderr(err):
                self.assertEqual(adapter.main([str(path), *flags]), 0)
            shown = json.loads(out.getvalue()) if flags else [json.loads(out.getvalue())]
            self.assertEqual([r["stage"] for r in shown], ["16"])
        future = (datetime.now(timezone.utc) + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        bad = [[str(path), "--observed-at", future], [str(path), "--observed-at", "2026-01-01T00:00:00"],
               [str(self.copy / "missing.jsonl")]]
        for args in bad:
            self.assertEqual(self.run_cli(args), (2, "", BAD + "\n"))
        self.fresh()  # the same copy name now holds a journal with no finding
        self.assertEqual(self.run_cli([str(self.journal(packet("summary", source="s9")))]), (2, "", BAD + "\n"))

    def run_cli(self, args):
        out, err = StringIO(), StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = adapter.main(args)
        return code, out.getvalue(), err.getvalue()

    def test_c4_read_only_and_independent_of_the_stage_17_adapter(self):
        """C4/C1: a bug taking the packets lock or writing beside the copy, or importing a sibling adapter, fails."""
        path = self.journal(packet())
        before = {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in self.copy.iterdir()}
        adapter.records(path, utc(T0 + 60))
        self.assertEqual({p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in self.copy.iterdir()}, before)
        tree = ast.parse(Path(adapter.__file__).read_text(encoding="utf-8"))
        local = {alias.name for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.level
                 for alias in node.names}
        self.assertEqual(local, {"golive_evidence", "public_packets"})
        self.assertFalse(any(isinstance(node, ast.Import) and any("adapt" in a.name for a in node.names)
                             for node in ast.walk(tree)))


if __name__ == "__main__":
    unittest.main()
