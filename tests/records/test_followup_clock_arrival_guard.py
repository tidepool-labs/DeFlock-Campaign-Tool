"""Unreadable reply status must not be treated as evidence of no response."""
import unittest

from campaign_tool.records import followup_clock as fc
from tests.records.test_followup_clock import AS_OF, doc, row


class ArrivalGuardTests(unittest.TestCase):
    def test_overdue_reminder_requires_readable_arrival_when_present(self):
        settings = {"default": {"autonomy": "draft"}, "agencies": {}}
        unclear = "Reply status unclear; check the source"
        cases = [
            ("legacy", {}, None),
            ("none", {"answer_arrived": {"state": "none"}}, None),
            ("confirmed", {"answer_arrived": {"state": "arrived_unrecorded"}}, "reply arrived; record it first"),
            ("unknown", {"answer_arrived": {"state": "unknown"}}, unclear),
        ]
        # A dict-only check silently lets these rows plan reminders; fixing only the
        # predicate can also leave the status view indexing a malformed field.
        for marker in ("unknown", None, False, 3, [], {}, {"state": None},
                       {"state": 3}, {"state": ["unknown"]}, {"state": {}}):
            cases.append((repr(marker), {"answer_arrived": marker}, unclear))

        for label, fields, hold in cases:
            register = doc([dict(row(), **fields)])
            with self.subTest(marker=label, view="planner"):
                candidates = fc.plan(register, settings, [], as_of=AS_OF)
                self.assertEqual([item["kind"] for item in candidates],
                                 [] if hold else ["reminder-no-response"])
            with self.subTest(marker=label, view="status"):
                status, = fc.reminder_status(register, settings, [], as_of=AS_OF)
                self.assertEqual(status["reason"], hold or "No reminder draft queued")
                self.assertIsNone(status["state"])
                self.assertNotIn("queue_id", status)
