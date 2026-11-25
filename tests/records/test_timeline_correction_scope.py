"""Manual corrections affect only earlier evidence for the matched request."""
import unittest

from tests.records.test_timeline import build, clock, manual, reg, row


def flags(request):
    return [(e["source"]["event_id"], e["superseded"], e["corrects"]) for e in request["events"]]


class CorrectionScopeTests(unittest.TestCase):
    def setUp(self):
        self.first = row(deadline=clock(state="past_due_reminder", due="2026-10-20"))
        self.closure = manual("closure", "closed", "2026-11-02")

    def test_other_request_or_unmatched_note_cannot_reopen_request(self):
        # Global reference resolution wrongly applies even a note never attached to this row.
        for agency, request_id, matched in (("ag1", "L-2", True), ("ag2", "R-1", True),
                                            ("ag1", "missing", False)):
            with self.subTest(agency=agency, request_id=request_id):
                note = manual("note", "hand note", "2026-11-03", corrects="closure",
                              agency_id=agency, request_id=request_id)
                other = row("R-2", ledger="L-2") if agency == "ag1" else row(agency="ag2")
                report = build(reg(self.first, other), manual=[self.closure, note])
                first, second = report["requests"]
                self.assertEqual(flags(second), [("note", False, "closure")] if matched else [])
                self.assertEqual(report["unmatched"]["manual"], 0 if matched else 1)
                self.assertEqual(flags(first), [("closure", False, None)])
                self.assertTrue(first["closed"])
                self.assertEqual(first["overdue_marks"], [])

    def test_forward_reference_cannot_reopen_request_even_if_target_is_backdated(self):
        note = manual("note", "hand note", "2026-11-03", corrects="closure")
        report = build(reg(self.first), manual=[note, self.closure])
        [request] = report["requests"]
        self.assertEqual(flags(request), [("closure", False, None), ("note", False, "closure")])
        self.assertTrue(request["closed"])
        self.assertEqual(request["overdue_marks"], [])
        self.assertEqual(report["unmatched"]["manual"], 0)

    def test_backdated_later_entry_can_correct_earlier_entry_via_other_request_id(self):
        # Match the same row via its ledger ID and its external ID, not raw ID equality.
        note = manual("note", "hand note", "2026-11-01", corrects="closure", request_id="L-1")
        report = build(reg(self.first), manual=[self.closure, note])
        [request] = report["requests"]
        self.assertEqual(flags(request), [("note", False, "closure"), ("closure", True, None)])
        self.assertFalse(request["closed"])
        self.assertEqual([m["kind"] for m in request["overdue_marks"]], ["statutory_date_passed"])
        self.assertEqual(report["unmatched"]["manual"], 0)

    def test_missing_or_self_reference_stays_visible_without_superseding_event(self):
        # Register the current event only after lookup, so a self-reference never takes effect.
        for target in ("missing", "closure"):
            with self.subTest(target=target):
                closure = {**self.closure, "corrects": target}
                report = build(reg(self.first), manual=[closure])
                [request] = report["requests"]
                self.assertEqual(flags(request), [("closure", False, target)])
                self.assertTrue(request["closed"])
                self.assertEqual(report["unmatched"]["manual"], 0)
