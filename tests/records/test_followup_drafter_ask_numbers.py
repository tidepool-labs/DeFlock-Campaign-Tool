"""Displayed numbers identify asks without renumbering the source case."""
import unittest

from campaign_tool.records import followup_drafter as fd
from campaign_tool.records import playbook as pb


class AskNumberTests(unittest.TestCase):
    def setUp(self):
        self.book = pb.load()
        pb.pattern(self.book, "no_records")["questions"] = [
            "Were the audits completed? Was the contract signed?"
        ]
        self.case = {
            "schema": fd.SCHEMA,
            "agency": {"id": "example-pd", "name": "Example PD"},
            "request_id": "R-1",
            "response_type": "no_records",
            "asks": [
                {"text": "Completed audits", "rating": "answered"},
                {"text": "Vendor contract", "rating": "no_records_stated"},
            ],
        }

    def test_refuses_ambiguous_displayed_numbers(self):
        # Raw-value comparisons miss the mixed-type pair; explicit-only checks miss fallbacks.
        for first, second in [(7, 7), (2, None), (2, 0), (1, "1")]:
            with self.subTest(first=first, second=second):
                self.case["asks"][0]["number"] = first
                if second is None:
                    self.case["asks"][1].pop("number", None)
                else:
                    self.case["asks"][1]["number"] = second
                with self.assertRaises(pb.PlaybookError) as raised:
                    fd.draft(self.book, self.case)
                self.assertEqual(raised.exception.code, "case_invalid")

    def test_refuses_non_label_numbers(self):
        # Truthy booleans and containers leak into the preview as labels;
        # falsey ones must not be silently replaced with a positional number.
        for number in (True, False, [7], [], {"item": 7}, {}):
            with self.subTest(number=number):
                self.case["asks"][1]["number"] = number
                with self.assertRaises(pb.PlaybookError) as raised:
                    fd.draft(self.book, self.case)
                self.assertEqual(raised.exception.code, "case_invalid")

    def test_distinct_displayed_numbers_preserve_open_and_dropped_asks(self):
        # A raw-value filter would still conflate 2 and 2.0, despite distinct labels.
        for dropped_number, open_fields in [("7", {}), (2.0, {}), ("7", {"number": None}), ("7", {"number": 0})]:
            with self.subTest(dropped_number=dropped_number, open_fields=open_fields):
                self.case["asks"][0]["number"] = dropped_number
                self.case["asks"][1].update(open_fields)
                result = fd.draft(self.book, self.case)
                self.assertEqual(result["dropped"], [
                    {"number": dropped_number, "text": "Completed audits", "reason": "already produced"},
                ])
                self.assertIs(type(result["dropped"][0]["number"]), type(dropped_number))
                self.assertEqual([ask["number"] for ask in result["open_asks"]], [2])
                letter = result["body"].split(fd.NOTES_RULE)[0]
                self.assertIn("Item 2 asked for", letter)
                self.assertNotIn("Were the audits completed?", letter)
                self.assertIn("Was the contract signed?", letter)
