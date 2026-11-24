import hashlib
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
from unittest import mock

from campaign_tool import approval
from campaign_tool.records import golive_adapt_packets as adapter
from campaign_tool.records import golive_evidence, golive_ledger, public_packets as pp, run_stages

T0 = 1_793_000_000
SHA = "a" * 64
OBSERVED = datetime.fromtimestamp(T0 + 3600, timezone.utc)
OBSERVED_TEXT = OBSERVED.strftime("%Y-%m-%dT%H:%M:%SZ")


def fake_verify(data, signature):
    # stand-in verifier (as in test_public_packets): "SIG:<principal>:<hex of exact payload>"
    if not signature.startswith("SIG:") or signature.split(":")[2] != data.hex():
        raise approval.ApprovalError("bad")
    return signature.split(":")[1]


def doc(**extra):
    return {"kind": "document", "agency_key": "ex-pd", "request_id": "r1", "source": "doc-1", "title": "Policy released",
            "text": "Example PD released its ALPR policy on 2026-11-03.",
            "documents": [{"sha256": SHA, "date_received": "2026-11-03", "redaction_ids": ["red-1"]}], **extra}


class Base(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()  # mkdtemp roots are private (0700)
        self.addCleanup(tmp.cleanup)
        self.root = tmp.name
        patcher = mock.patch.object(pp.approval, "verify", fake_verify)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.project = os.path.join(self.root, "p")
        pp.init(self.project)

    def propose(self, now=T0, **extra):
        return pp.propose(self.project, doc(**extra), now=now)["id"]

    def approve(self, ident, now=T0):
        payload = pp.payload(self.project, ident)
        pp.approve(self.project, ident, f"SIG:g@x:{payload.hex()}", now=now)

    def written(self):
        ident = self.propose()
        self.approve(ident)
        payload = pp.switch_payload(self.project)
        pp.enable(self.project, f"SIG:g@x:{payload.hex()}", now=T0)
        pp.write_change(self.project, ident, now=T0 + 60)
        return ident

    def raw(self):
        with open(os.path.join(self.project, pp.JOURNAL), "rb") as stream:
            return stream.read()

    def copy(self, raw, name="copy"):
        directory = os.path.join(self.root, name)
        os.makedirs(directory, mode=0o700)
        path = os.path.join(directory, "public-packets.jsonl")
        with open(path, "wb") as stream:
            stream.write(raw)
        return path

    def run_main(self, path, observed=OBSERVED_TEXT, *extra):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            status = adapter.main([path, "--observed-at", observed, *extra])
        return status, out.getvalue(), err.getvalue()

    def refused(self, path, observed=OBSERVED_TEXT):
        self.assertEqual(self.run_main(path, observed, "--json"), (2, "", adapter.CODE + "\n"))


class WrittenPacketTests(Base):
    def test_c1_c2_written_signed_packet_is_stage_17_live(self):
        """C1/C2 (a): a journal built by the module's own writers yields the pass row the ledger reads as stage 17 live.
        Bug caught: wrong stage/kind/actor/source schema, or hashing something other than the copied bytes."""
        self.written()
        raw = self.raw()
        row = adapter.records(raw, OBSERVED)
        self.assertEqual(row, {
            "schema": "records-golive-evidence-v1", "stage": "17", "kind": "live", "result": "pass",
            "recorded_at": "2026-10-26T07:34:20Z", "engine": None, "actor": "operator", "codes": [],
            "source": {"schema": "records-public-packets-v1", "sha256": hashlib.sha256(raw).hexdigest(),
                       "name": "public-packets.jsonl"}})
        self.assertEqual(datetime.fromtimestamp(T0 + 60, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), row["recorded_at"])
        golive_evidence.validate(row)
        stage = next(r for r in golive_ledger.build([], [row], OBSERVED) if r["stage"] == "17")
        self.assertEqual((stage["state"], stage["live"]["status"]), ("LIVE", "evidence"))
        status, out, err = self.run_main(self.copy(raw), OBSERVED_TEXT, "--json")
        self.assertEqual((status, json.loads(out), err), (0, [row], ""))

    def test_c2_unwritten_packets_are_refused(self):
        """C2/C3 (b): proposed, approved-only, rejected and cancelled packets are not stage 17 proof.
        Bug caught: treating any approved/proposed packet (or any journal at all) as a written one."""
        self.propose(source="p1")
        approved = self.propose(source="p2")
        self.approve(approved)
        stopped = self.propose(source="p3")
        pp.decide(self.project, stopped, "rejected", "g", now=T0)
        withdrawn = self.propose(source="p4")
        pp.decide(self.project, withdrawn, "cancelled", "g", now=T0)
        self.assertEqual({r["status"] for r in pp.fold(adapter._events(self.raw())).values()},
                         {"proposed", "approved", "rejected", "cancelled"})
        self.refused(self.copy(self.raw()))
        with self.assertRaises(ValueError):
            adapter.records(self.raw(), OBSERVED)

    def test_c2_superseded_written_revision_is_refused(self):
        """C2: a later revision of a written packet voids it; its old written event must not count.
        Bug caught: scanning raw `written` events instead of folding the journal."""
        self.written()
        pp.propose(self.project, doc(text="Example PD released an updated ALPR policy on 2026-11-04."), now=T0 + 90)
        self.refused(self.copy(self.raw()))

    def test_c2_future_event_is_refused_and_older_one_passes(self):
        """C2: recorded time must not be in the future; the same journal passes once observed-at has caught up.
        Bug caught: no future check, or comparing against the real clock only (a forged observed-at hides it)."""
        self.written()
        path = self.copy(self.raw())
        early = datetime.fromtimestamp(T0 + 59, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        self.refused(path, early)
        self.assertEqual(self.run_main(path, OBSERVED_TEXT)[0], 0)
        self.refused(path, "2999-01-01T00:00:00Z")
        self.refused(path, "yesterday")


class RefusalTests(Base):
    def test_c3_sandbox_marker_is_refused_and_control_passes(self):
        """C3 (c): a journal copied beside the rehearsal marker is refused; the identical bytes elsewhere pass.
        Bug caught: rehearsal sandbox journals counted as live evidence."""
        self.written()
        raw = self.raw()
        path = self.copy(raw, "sandbox")
        self.assertEqual(self.run_main(path)[0], 0)
        with open(os.path.join(os.path.dirname(path), run_stages.SANDBOX_MARKER), "w"):
            pass
        self.refused(path)

    def test_c3_tampered_or_gapped_journal_is_refused(self):
        """C3 (d): any edited, reformatted, gapped or truncated journal is refused.
        Bug caught: trusting lines without the schema/seq/digest check, so a forged `written` line passes."""
        self.written()
        raw = self.raw()
        lines = raw.split(b"\n")[:-1]
        by_event = {json.loads(line)["event"]: i for i, line in enumerate(lines)}

        def edit(index, **changes):
            event = json.loads(lines[index])
            event.update(changes)
            return lines[:index] + [adapter._canon(event).encode()] + lines[index + 1:]

        def packet_text(index):
            event = json.loads(lines[index])
            event["packet"]["text"] = "Example PD released nothing."
            return lines[:index] + [adapter._canon(event).encode()] + lines[index + 1:]

        cases = {
            "gap in seq": lines[:by_event["approved"]] + lines[by_event["approved"] + 1:],
            "duplicate seq": lines + [lines[-1]],
            "edited packet text": packet_text(by_event["packet"]),
            "wrong schema": edit(by_event["packet"], schema="records-public-packets-v2"),
            "bad file hash": edit(by_event["written"], file_sha256="G" * 64),
            "missing file hash": edit(by_event["written"], file_sha256=None),
            "empty signature": edit(by_event["approved"], signature=""),
            "wrong time label": edit(by_event["written"], at="2026-10-26T06:14:21Z"),
            "unknown event": edit(by_event["written"], event="deployed"),
            "reformatted line": lines[:1] + [lines[1].replace(b":", b": ", 1)] + lines[2:],
            "not json": lines[:-1] + [b"{nope"],
        }
        for label, edited in cases.items():
            with self.subTest(label):
                self.refused(self.copy(b"\n".join(edited) + b"\n", "t-" + label.replace(" ", "-")))
        for label, damaged in {"no newline": raw[:-1], "empty": b"", "blank line": raw + b"\n"}.items():
            with self.subTest(label):
                self.refused(self.copy(damaged, "t-" + label.replace(" ", "-")))

    def test_c3_c4_refusal_echoes_nothing_and_writes_nothing(self):
        """C3/C4: refusals print only the one code, even for odd paths and arguments; the input is never modified.
        Bug caught: echoing a path/journal text from OSError, argparse or ValueError messages."""
        self.written()
        secret = os.path.join(self.root, "secret-agency-name")
        before = self.raw()
        cases = [[secret, "--observed-at", OBSERVED_TEXT], [self.copy(b"{secret-text\n", "junk"), "--observed-at", OBSERVED_TEXT],
                 [secret + "/x", "--bogus-" + secret], [self.copy(before, "short"), "--observed-at"], []]
        os.symlink(self.copy(before, "real"), os.path.join(self.root, "link.jsonl"))
        cases.append([os.path.join(self.root, "link.jsonl"), "--observed-at", OBSERVED_TEXT])
        cases.append([os.path.dirname(self.copy(before, "dir")), "--observed-at", OBSERVED_TEXT])
        for argv in cases:
            with self.subTest(argv=len(argv)):
                out, err = io.StringIO(), io.StringIO()
                with redirect_stdout(out), redirect_stderr(err):
                    try:
                        status = adapter.main(argv)
                    except SystemExit as exc:
                        status = exc.code
                self.assertEqual((status, out.getvalue(), err.getvalue()), (2, "", adapter.CODE + "\n"))
        self.assertEqual(self.raw(), before)

    def test_c4_read_only_never_signs_approves_or_writes(self):
        """C4: running the adapter leaves the copy and its directory untouched and never calls the approval layer.
        Bug caught: reusing pp.status/approve/write_change (which lock, sign or write) instead of a pure fold."""
        self.written()
        path = self.copy(self.raw(), "ro")
        def state():
            with open(path, "rb") as stream:
                return stream.read(), sorted(os.listdir(os.path.dirname(path)))
        before = state()
        with mock.patch.object(pp.approval, "verify", side_effect=AssertionError("approval used")), \
                mock.patch.object(pp, "_Locked", side_effect=AssertionError("project opened")):
            self.assertEqual(self.run_main(path)[0], 0)
        self.assertEqual(state(), before)


if __name__ == "__main__":
    unittest.main()
