"""Recorded deadline values at the public reply renderer boundary."""
import unittest

from campaign_tool.records import draft_templates


def _extension(**recorded_date):
    return {
        "item_id": "a" * 64,
        "kind": "extension",
        "kind_basis": {"page": 1, "quote": "We need more time to respond."},
        "agency_id": "example-agency",
        "request_id": "example-request",
        "findings": [],
        "deadlines": [{"page": 2, "quote": "We expect to respond by November 30.",
                       "date_text": "November 30", **recorded_date}],
    }


class RecordedDeadlineDateTests(unittest.TestCase):
    def test_non_string_dates_are_refused(self):
        # A valid citation must not let malformed values be printed or treated as unresolved.
        for date in ({"day": 30}, {}, ["2026-11-30"], [], True, False, 30, 0):
            with self.subTest(date=date):
                with self.assertRaisesRegex(draft_templates.DraftSourceError, r"^deadline$"):
                    draft_templates.render(_extension(date=date))

    def test_recorded_date_is_rendered_verbatim(self):
        # Render the recorded value, not date_text, and do not parse or reformat it.
        date = "30 November 2026"
        result = draft_templates.render(_extension(date=date))
        assert result is not None
        kind, body, evidence = result
        self.assertEqual(kind, "reply-extension")
        self.assertIn(f"(date as the item recorded it: {date})", body)
        self.assertEqual(evidence["deadlines"][0]["date"], date)

    def test_unresolved_dates_keep_the_existing_note(self):
        # Requiring a nonempty string would wrongly refuse unresolved but cited deadlines.
        for fields in ({}, {"date": None}, {"date": ""}):
            with self.subTest(fields=fields):
                result = draft_templates.render(_extension(**fields))
                assert result is not None
                kind, body, evidence = result
                self.assertEqual(kind, "reply-extension")
                self.assertIn("(date not resolved; check the page)", body)
                self.assertNotIn("date as the item recorded it:", body)
                self.assertEqual(evidence["deadlines"][0]["date"], fields.get("date"))
