"""Synthetic regressions for malformed roles in same-scope policy versions."""
import unittest

from campaign_tool.records.understanding import policy
from tests.records.test_understanding import AGENCY, policy_versions


class MalformedPolicyRolesTests(unittest.TestCase):
    def test_malformed_roles_are_reported_without_comparing_them(self):
        peer = policy_versions()[0]
        issue = {"code": "regulated_roles_required", "version_id": "malformed"}
        # Nested roles used to crash set conversion; hashable invalid entries
        # must not let the otherwise valid "operator" create a spurious overlap.
        for roles in ([["operator"]], ["operator", ""], ["operator", 42]):
            malformed = dict(peer, version_id="malformed", regulated_roles=roles)
            for order in ([malformed, peer], [peer, malformed]):
                with self.subTest(roles=roles, first=order[0]["version_id"]):
                    result = policy.validate_versions(order)
                    self.assertFalse(result["valid"])
                    self.assertEqual(result["issues"], [issue])
                    self.assertEqual(result["versions"], 2)

    def test_null_roles_return_invalid_versions_in_both_orders(self):
        peer = policy_versions()[0]
        malformed = dict(peer, version_id="malformed", regulated_roles=None)
        issue = {"code": "regulated_roles_required", "version_id": "malformed"}
        # Selection must reach its normal refusal rather than attempting set(None).
        for order in ([malformed, peer], [peer, malformed]):
            with self.subTest(first=order[0]["version_id"]):
                result = policy.select_version(
                    order, agency_id=AGENCY, jurisdiction="example-state",
                    regulated_role="operator", event_at="2030-01-15",
                )
                self.assertEqual(result["status"], "invalid_versions")
                self.assertIsNone(result["version"])
                self.assertEqual(result["candidates"], [])
                self.assertEqual(result["gaps"], [issue])
                self.assertFalse(result["validation"]["valid"])
                self.assertEqual(result["validation"]["issues"], [issue])

    def test_valid_overlap_is_still_reported_with_a_malformed_third_version(self):
        first = policy_versions()[0]
        second = dict(first, version_id="v2", effective_from="2030-01-01")
        malformed = dict(first, version_id="malformed", regulated_roles=[["operator"]])
        # Refusing the bad row must not stop the scan before the valid pair.
        for index in range(3):
            order = [first, second]
            order.insert(index, malformed)
            with self.subTest(malformed_index=index):
                result = policy.validate_versions(order)
                self.assertFalse(result["valid"])
                self.assertEqual(result["versions"], 3)
                self.assertEqual(result["issues"], [
                    {"code": "regulated_roles_required", "version_id": "malformed"},
                    {"code": "overlapping_versions", "version_ids": ["v1", "v2"],
                     "regulated_roles": ["operator"]},
                ])
