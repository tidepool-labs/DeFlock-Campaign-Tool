"""Refuse malformed flag lists at the public follow-up model boundary."""
import copy
import unittest

from campaign_tool.records.console_followups import validate


def _model(flags):
    return {
        "state": "ok",
        "run_id": "run_20300201T000000Z_00000000",
        "as_of": "2030-02-01",
        "requests": [{
            "agency_key": "example-agency",
            "request_id": "req-1",
            "ledger_request_id": None,
            "state_source": "ok",
            "display_state": "partial",
            "flags": flags,
            "item_needs": {
                "produced": [1],
                "withheld": [],
                "audit_no_records": [],
                "other_no_records": [],
                "not_addressed": [2],
            },
            "route_source": "unavailable",
            "pattern": None,
            "reason": None,
        }],
    }


class MalformedFlagsTests(unittest.TestCase):
    def test_object_and_array_members_return_none(self):
        # Hashing flag members before validating them raises instead of refusing.
        for member in ({"flag": "open_tier1"}, ["open_tier1"]):
            with self.subTest(member_type=type(member).__name__):
                self.assertIsNone(validate(_model(["open_tier1", member])))

    def test_duplicate_and_unknown_strings_still_return_none(self):
        for flags in (["open_tier1", "open_tier1"], ["open_tier1", "unknown"]):
            with self.subTest(flags=flags):
                self.assertIsNone(validate(_model(flags)))

    def test_empty_and_valid_flag_lists_return_model_unchanged(self):
        for flags in ([], ["register_issues", "open_tier1"]):
            with self.subTest(flags=flags):
                model = _model(flags)
                before = copy.deepcopy(model)
                self.assertIs(validate(model), model)
                self.assertEqual(model, before)
