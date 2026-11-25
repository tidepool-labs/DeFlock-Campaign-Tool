"""Request correspondence summary: exact-request progress, one count per original, placement is never sent."""
import importlib.util
import io
import json
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "records" / "request_correspondence_summary.py"
_spec = importlib.util.spec_from_file_location("request_correspondence_summary", SCRIPT)
assert _spec is not None and _spec.loader is not None
rcs = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rcs)

A, B, C = "a" * 64, "b" * 64, "c" * 64
OLD = {"agency_id": "alpha", "request_id": "R-OLD"}
NEW = {"agency_id": "alpha", "request_id": "R-NEW"}
LINK = OLD | {"queue_id": "q-synthetic"}


def chain(link=LINK, source=A):
    return [link | {"kind": "draft", "revision": 1, "original_sha256": source},
            link | {"kind": "approval_applied", "from_revision": 1, "revision": 2, "decision_id": "D-test"},
            link | {"kind": "drafts_placement", "revision": 2, "decision_id": "D-test", "message_id": "M-test"}]


def fixture(events=None):
    # R-OLD: one original in two parts (one count). R-NEW: same agency, nothing received.
    return {"schema": rcs.INPUT_SCHEMA, "requests": [OLD, NEW],
            "received": [OLD | {"original_sha256": A, "item_id": "item-a1"},
                         OLD | {"original_sha256": A, "item_id": "item-a2"}],
            "events": chain() if events is None else events}


class SummaryTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="correspondence-summary-")
        self.path = Path(self._tmp.name) / "input.json"

    def tearDown(self):
        self._tmp.cleanup()

    def summary(self, data):
        self.path.write_text(json.dumps(data), encoding="utf-8")
        return {(r["agency_id"], r["request_id"]): r for r in rcs.build(self.path)["requests"]}

    def test_each_stage_is_distinct_and_keeps_its_evidence_ids(self):
        events = chain()
        states = []
        for n in range(4):
            rows = self.summary(fixture(events[:n]))
            replies = rows[("alpha", "R-OLD")]["replies"]
            states.append(replies[0]["state"] if replies else "NONE")
        self.assertEqual(states, ["NONE", rcs.PENDING, rcs.APPROVED, rcs.PLACED])
        old = self.summary(fixture())[("alpha", "R-OLD")]
        self.assertEqual(old["incoming"], rcs.RECEIVED)
        self.assertEqual(old["originals"], [{"original_sha256": A, "item_ids": ["item-a1", "item-a2"]}])
        self.assertEqual(old["replies"], [{"queue_id": "q-synthetic", "state": rcs.PLACED, "revision": 2,
                                           "decision_id": "D-test", "message_id": "M-test"}])

    def test_neighbouring_request_to_the_same_agency_inherits_nothing(self):
        rows = self.summary(fixture())
        new = rows[("alpha", "R-NEW")]
        self.assertEqual((new["incoming"], new["originals"], new["replies"]), (rcs.AWAITING, [], []))
        # An approval or placement naming the neighbour cannot advance R-OLD's draft, and vice versa.
        events = chain()
        moved = [events[0], events[1] | {"request_id": "R-NEW"}, events[2] | {"request_id": "R-NEW"}]
        rows = self.summary(fixture(moved))
        self.assertEqual(rows[("alpha", "R-OLD")]["replies"][0]["state"], rcs.PENDING)
        self.assertEqual(rows[("alpha", "R-NEW")]["replies"], [])
        for patch in ({"queue_id": "q-other"}, {"agency_id": "beta"}):
            rows = self.summary(fixture([events[0], events[1] | patch, events[2] | patch]))
            self.assertEqual(rows[("alpha", "R-OLD")]["replies"][0]["state"], rcs.PENDING)

    def test_placement_is_never_sent(self):
        data = fixture(chain() + [LINK | {"kind": "mock_send_receipt", "revision": 2, "message_id": "M-test",
                                          "send_receipt_id": "S"}, LINK | {"kind": "sent", "revision": 2}])
        self.path.write_text(json.dumps(data), encoding="utf-8")
        out = rcs.build(self.path)
        self.assertEqual(out["sent_state"], "NOT_REPORTED")
        self.assertEqual(out["requests"][0]["replies"][0]["state"], rcs.PLACED)
        self.assertNotIn("SENT", json.dumps(out))
        self.assertIn("PLACED_IN_DRAFTS (not sent)", rcs.render(out))

    def test_stale_revision_or_wrong_decision_does_not_advance(self):
        events = chain()
        edited = LINK | {"kind": "draft", "revision": 3, "original_sha256": A}
        rows = self.summary(fixture(events + [edited]))
        self.assertEqual(rows[("alpha", "R-OLD")]["replies"][0]["state"], rcs.PENDING)
        rows = self.summary(fixture([events[0], events[1], edited, events[2]]))
        self.assertEqual(rows[("alpha", "R-OLD")]["replies"][0]["state"], rcs.PENDING)
        rows = self.summary(fixture([events[0], events[1], events[2] | {"decision_id": "other"}]))
        self.assertEqual(rows[("alpha", "R-OLD")]["replies"][0]["state"], rcs.APPROVED)
        rows = self.summary(fixture([events[0], events[1] | {"from_revision": 0}, events[2]]))
        self.assertEqual(rows[("alpha", "R-OLD")]["replies"][0]["state"], rcs.PENDING)

    def test_draft_answering_another_requests_original_is_unattributed(self):
        data = fixture(chain(link=NEW | {"queue_id": "q-new"}, source=A))
        rows = self.summary(data)
        self.assertEqual(rows[("alpha", "R-NEW")]["replies"][0]["state"], rcs.UNATTRIBUTED)
        self.assertEqual(rows[("alpha", "R-NEW")]["replies"][0]["decision_id"], None)

    def test_original_claimed_by_two_requests_is_withheld(self):
        data = fixture()
        data["received"].append(NEW | {"original_sha256": A, "item_id": "item-a3"})
        data["received"].append(NEW | {"original_sha256": B, "item_id": "item-b1"})
        rows = self.summary(data)
        old, new = rows[("alpha", "R-OLD")], rows[("alpha", "R-NEW")]
        self.assertEqual((old["incoming"], old["originals"], old["conflicting_originals"]), (rcs.NEEDS_ATTRIBUTION, [], [A]))
        self.assertEqual(new["incoming"], rcs.NEEDS_ATTRIBUTION)
        self.assertEqual(new["originals"], [{"original_sha256": B, "item_ids": ["item-b1"]}])
        # With the original withheld, R-OLD's draft answering it cannot progress.
        self.assertEqual(old["replies"][0]["state"], rcs.UNATTRIBUTED)

    def test_rows_outside_the_ledger_are_counted_not_attached(self):
        data = fixture(chain() + chain(link={"agency_id": "alpha", "request_id": "R-GONE", "queue_id": "q"}))
        data["received"].append({"agency_id": "alpha", "request_id": "R-GONE", "original_sha256": C, "item_id": "x"})
        self.path.write_text(json.dumps(data), encoding="utf-8")
        out = rcs.build(self.path)
        self.assertEqual((out["unmatched_received_rows"], out["unmatched_events"]), (1, 3))
        self.assertEqual([r["request_id"] for r in out["requests"]], ["R-OLD", "R-NEW"])

    def test_malformed_input_is_refused(self):
        bad = [{"schema": "other"}, fixture() | {"requests": [OLD, OLD]}, fixture() | {"events": "x"},
               fixture() | {"received": [OLD | {"original_sha256": "short", "item_id": "i"}]},
               fixture() | {"requests": [{"agency_id": "alpha"}]}]
        for data in bad:
            with self.subTest(data=str(data)[:60]):
                self.path.write_text(json.dumps(data), encoding="utf-8")
                with self.assertRaises(rcs.Refused):
                    rcs.build(self.path)

    def test_cli_text_and_refusal_exit_codes_and_no_writes(self):
        self.path.write_text(json.dumps(fixture()), encoding="utf-8")
        before = sorted(p.name for p in Path(self._tmp.name).iterdir())
        out = subprocess.run([sys.executable, str(SCRIPT), str(self.path), "--format", "text"],
                             capture_output=True, text=True, check=False)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("alpha / R-OLD: RECEIVED, 1 original(s)", out.stdout)
        self.assertIn("alpha / R-NEW: AWAITING_REPLY, 0 original(s)", out.stdout)
        self.assertIn("reply q-synthetic: PLACED_IN_DRAFTS (not sent) (revision 2, decision D-test, message M-test)",
                      out.stdout)
        self.assertEqual(sorted(p.name for p in Path(self._tmp.name).iterdir()), before)
        self.path.write_text("{", encoding="utf-8")
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            self.assertEqual(rcs.main([str(self.path)]), 2)
        self.assertEqual(stdout.getvalue(), "")
        self.assertIn("refused", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
