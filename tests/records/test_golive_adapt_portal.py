import hashlib
import io
import json
import os
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from campaign_tool.records import golive_adapt_portal as adapter, golive_evidence, golive_ledger
from campaign_tool.records.portal.store import Queue

NOW = datetime(2026, 11, 24, 12, tzinfo=timezone.utc)
OBSERVED = "2026-11-24T03:30:00-08:00"  # 11:30Z, one half hour before NOW
OBSERVED_Z = "2026-11-24T11:30:00Z"
HOST = "records.city-fixture.org"  # invented, not on any allowlist
DIGEST = hashlib.sha256(b"synthetic document").hexdigest()
CODE = "PORTAL_EVIDENCE_BAD_INPUT\n"


def make_status(tmp, items, pending=False):
    """Status from a fresh real Queue; items are (host, received) pairs."""
    queue = Queue(Path(tempfile.mkdtemp(dir=tmp)) / "queue")
    try:
        for number, (host, received) in enumerate(items):
            key = queue.inventory(host, f"R-{number}", f"I-{number}", f"https://{host}/f/{number}",
                                  hashlib.sha256(str(number).encode()).hexdigest())
            if received:  # as engine.py marks a fetched item
                queue.db.execute("UPDATE portal_items SET state='received',current_sha256=? WHERE id=?",
                                 (DIGEST, key))
                if pending:
                    queue.db.execute("INSERT INTO portal_attempts VALUES(1,?,1,1.0,2.0,'received','{}')", (key,))
                    queue.db.execute("INSERT INTO portal_versions(id,item,sha256,byte_count,attempt,provenance_json)"
                                     " VALUES('v1',?,?,18,1,'{}')", (key, DIGEST))
                queue.db.commit()
        return json.loads(json.dumps(queue.status()))
    finally:
        queue.close()


def expected(codes=(), result="pass", raw=None, name="portal-status.json", status=None):
    if raw is None:
        raw = json.dumps(status, sort_keys=True, separators=(",", ":")).encode()
    return {"schema": "records-golive-evidence-v1", "stage": "4", "kind": "live", "result": result,
            "recorded_at": OBSERVED_Z, "engine": None, "actor": "operator", "codes": list(codes),
            "source": {"schema": "records-portal-status-v1", "sha256": hashlib.sha256(raw).hexdigest(),
                       "name": name}}


class PortalRowTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(dir=".")
        self.addCleanup(tmp.cleanup)
        self.tmp = tmp.name

    def refused(self, status, observed: object = OBSERVED, now=NOW):
        with self.assertRaises(ValueError) as caught:
            adapter.records(status, observed, now)
        self.assertEqual(str(caught.exception), "PORTAL_EVIDENCE_BAD_INPUT")

    def test_c2_received_real_host_passes(self):
        """C1/C2: a Queue.status() copy with a received public-host item passes.

        Fails if the adapter requires fields Queue.status() does not emit, drops the
        pending-ledger note, or stamps now instead of the operator's observation time."""
        status = make_status(self.tmp, [(HOST, True)], pending=True)
        self.assertEqual(status["ledger_pending"], 1)
        row = adapter.records(status, OBSERVED, NOW)
        self.assertEqual(row, expected(["PORTAL_LEDGER_PENDING"], status=status))
        self.assertIsNone(golive_evidence.validate(row))
        # Healthy control: same shape without a pending delivery has no codes.
        clean = make_status(self.tmp, [(HOST, True)])
        self.assertEqual(adapter.records(clean, OBSERVED, NOW)["codes"], [])

    def test_c3_loopback_and_reserved_only_is_refused(self):
        """C3: a rehearsal-style status (host 127.0.0.1) is sandbox evidence, not live.

        Fails if a received item with a valid hash passes without checking the host."""
        status = make_status(self.tmp, [("127.0.0.1", True)])
        self.assertEqual(status["items"][0]["state"], "received")
        self.refused(status)
        for host in ("localhost", "a.localhost", "x.invalid", "x.test", "x.example", "::1", "10.0.0.1", "127.1"):
            with self.subTest(host=host):
                self.refused({"items": [{"host": host, "state": "received", "current_sha256": DIGEST}],
                              "ledger_pending": 0})
        # Only-sandbox is the refusal; a sandbox item beside a real pending one is a plain fail.
        mixed = make_status(self.tmp, [("127.0.0.1", True), (HOST, False)])
        self.assertEqual(adapter.records(mixed, OBSERVED, NOW)["codes"], ["PORTAL_NO_RECEIVED_ITEM"])

    def test_c3_no_qualifying_item_is_a_fail_row(self):
        """C2/C3: pending, hashless, unqualified-host or empty status is a fail row.

        Fails if any non-received item, a received item without a 64-hex hash or a
        non-DNS host name is counted as downloaded evidence."""
        pending = make_status(self.tmp, [(HOST, False)])
        row = adapter.records(pending, OBSERVED, NOW)
        self.assertEqual(row, expected(["PORTAL_NO_RECEIVED_ITEM"], "fail", status=pending))
        self.assertIsNone(golive_evidence.validate(row))
        item = {"host": HOST, "state": "received", "current_sha256": DIGEST}
        for change in ({"current_sha256": None}, {"current_sha256": DIGEST.upper()}, {"state": "retry"},
                       {"host": "Records.City-Fixture.org"}, {"host": "intranet"}, {"host": "a..b"}):
            with self.subTest(change=change):
                status = {"items": [dict(item, **change)], "ledger_pending": 2}
                self.assertEqual(adapter.records(status, OBSERVED, NOW)["codes"],
                                 ["PORTAL_NO_RECEIVED_ITEM", "PORTAL_LEDGER_PENDING"])
        self.assertEqual(adapter.records({"items": [], "ledger_pending": 0}, OBSERVED, NOW)["result"], "fail")

    def test_c3_invalid_shapes_and_times_are_refused(self):
        """C3: malformed status or an unusable observation time never becomes a row.

        Fails if a missing ledger_pending, a bool count, a naive, future or older-than-24h
        time is accepted."""
        good = {"items": [{"host": HOST, "state": "received", "current_sha256": DIGEST}], "ledger_pending": 0}
        for bad in ([], {"items": {}, "ledger_pending": 0}, {"items": []}, dict(good, ledger_pending=True),
                    dict(good, ledger_pending=-1), {"items": [None], "ledger_pending": 0},
                    {"items": [{"host": 1, "state": "received"}], "ledger_pending": 0}):
            with self.subTest(status=str(bad)[:30]):
                self.refused(bad)
        for observed in ("2026-11-24T11:30:00", "not a time", None, "2026-11-24T12:00:01Z",
                         "2026-11-23T11:59:59Z"):
            with self.subTest(observed=observed):
                self.refused(good, observed)
        self.assertEqual(adapter.records(good, "2026-11-23T12:00:00Z", NOW)["result"], "pass")

    def test_c1_ledger_accepts_stage_4_row(self):
        """C1: the row is storable evidence and counts as stage 4 live proof.

        Fails if the stage, kind or schema fields differ from what the ledger reader accepts."""
        status = make_status(self.tmp, [(HOST, True)])
        row = adapter.records(status, OBSERVED, NOW)
        directory = Path(self.tmp) / "evidence"
        directory.mkdir(mode=0o700)
        path = directory / golive_evidence._filename(row)
        path.write_text(json.dumps(row))
        path.chmod(0o600)
        loaded = golive_evidence.load(directory)
        self.assertEqual(loaded, [row])
        stage = next(item for item in golive_ledger.build([], loaded, NOW) if item["stage"] == "4")
        self.assertEqual(stage["live"], {"status": "evidence", "recorded_at": OBSERVED_Z})
        failing = adapter.records(make_status(self.tmp, [(HOST, False)]), OBSERVED, NOW)
        stage = next(item for item in golive_ledger.build([], [failing], NOW) if item["stage"] == "4")
        self.assertEqual(stage["live"]["status"], "missing")


class PortalCLITests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(dir=".")
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.observed = (datetime.now(timezone.utc) - timedelta(hours=1)).replace(microsecond=0)

    def invoke(self, path, *args, observed=True):
        out, err = io.StringIO(), io.StringIO()
        extra = ["--observed-at", self.observed.isoformat()] if observed else []
        with redirect_stdout(out), redirect_stderr(err):
            status = adapter.main([str(path), *extra, *args])
        return status, out.getvalue(), err.getvalue()

    def save(self, value, name="saved-status.json"):
        path = self.tmp / name
        path.write_bytes(raw := (json.dumps(value, indent=2) + "\n").encode())
        return path, raw

    def test_c1_cli_binds_saved_bytes(self):
        """C1: JSON output carries the file's own hash and basename; text is the summary row.

        Fails if the hash is of re-serialised JSON or the full path is stored as the name."""
        path, raw = self.save(make_status(self.tmp, [(HOST, True)]))
        status, out, err = self.invoke(path, "--json")
        row = json.loads(out)[0]
        self.assertEqual((status, err, len(json.loads(out))), (0, "", 1))
        self.assertEqual(row["source"], {"schema": "records-portal-status-v1", "name": "saved-status.json",
                                         "sha256": hashlib.sha256(raw).hexdigest()})
        self.assertEqual(row["recorded_at"], self.observed.isoformat().replace("+00:00", "Z"))
        self.assertEqual(self.invoke(path), (0, "4\tlive\tpass\t" + row["recorded_at"]
                                             + "\trecords-portal-status-v1\t\n", ""))

    def test_c3_fail_row_exits_two_but_prints(self):
        """C3: a no-received-item status prints its fail row (unlike a refusal) with exit 2."""
        path, _ = self.save(make_status(self.tmp, [(HOST, False)]))
        status, out, err = self.invoke(path, "--json")
        self.assertEqual((status, err, json.loads(out)[0]["codes"]), (2, "", ["PORTAL_NO_RECEIVED_ITEM"]))

    def test_c3_refusals_echo_only_the_fixed_code(self):
        """C3: refusals print one fixed code on stderr; no host, hash, path or argument text.

        Fails if an exception message, a path or an argparse error repeats caller input."""
        sandbox, _ = self.save(make_status(self.tmp, [("127.0.0.1", True)]), "rehearsal.json")
        oversize = self.tmp / "big.json"
        oversize.write_bytes(b" " * (adapter.MAX_BYTES + 1))
        link = self.tmp / "link.json"
        link.symlink_to(sandbox)
        dup = self.tmp / "dup.json"
        dup.write_bytes(b'{"items":[],"items":[],"ledger_pending":0}')
        invalid = self.tmp / "invalid.json"
        invalid.write_bytes(b"{")
        old, _ = self.save({"items": [], "ledger_pending": 0}, "old.json")
        cases = [(sandbox,), (oversize,), (link,), (dup,), (invalid,), (self.tmp,), (self.tmp / "missing-secret-name",),
                 (sandbox, "--bogus-secret-arg"), (old, "--observed-at", "2020-01-01T00:00:00Z"),
                 (old, "--observed-at", (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat())]
        for case in cases:
            with self.subTest(case=str(case[0].name)):
                args = [str(item) for item in case]
                status, out, err = self.invoke(args[0], *args[1:], observed="--observed-at" not in args)
                self.assertEqual((status, out, err), (2, "", CODE))
        self.assertEqual(self.invoke(old, observed=False), (2, "", CODE))
        self.assertNotIn("127.0.0.1", CODE)

    def test_c4_reads_only_the_status_file(self):
        """C4: no queue database, fetch, approval or egress access.

        Fails if the adapter opens any other file, or touches sqlite or the portal engine."""
        path, _ = self.save(make_status(self.tmp, [(HOST, True)]))
        opened = []
        real_open = os.open

        def spy(file, *args, **kwargs):
            opened.append(str(file))
            return real_open(file, *args, **kwargs)

        with patch.object(os, "open", spy), patch.object(sqlite3, "connect", side_effect=AssertionError):
            self.assertEqual(self.invoke(path, "--json")[0], 0)
        self.assertEqual(opened, [str(path)])
        self.assertFalse(any(name in vars(adapter) for name in ("Queue", "fetch_queue", "sqlite3", "portal")))


if __name__ == "__main__":
    unittest.main()
