"""A bound delivery remains visible when the clock rejects a determination."""
from unittest import TestCase

from campaign_tool.records.outcomes.register import build_register
from tests.records.test_outcome_register import TODAY, data, join, occurrence, original, request


class UnadmittedDeterminationTests(TestCase):
    def register_row(self, determination):
        digest = "a" * 64
        source = data()
        source["requests"] = [request(asked="Audit logs", determination_at=determination)]
        source["originals"] = [original(digest)]
        source["occurrences"] = [occurrence("delivery-1", digest, kind="portal")]
        source["joins"] = [join(digest)]
        row, = build_register(source, as_of=TODAY)["requests"]
        self.assertEqual([item["occurrence_id"] for item in row["arrived"]], ["delivery-1"])
        return row

    def test_rejected_determination_preserves_delivery_and_clarify_action(self):
        # Truthy raw text must not hide a bound arrival when the clock admits no date.
        for value, issue in (
            ("not a date", "invalid_date:determination_at"),
            ("2029-12-31", "determination_date_inconsistent"),
            ("2030-01-13", "determination_date_inconsistent"),
        ):
            with self.subTest(determination=value):
                row = self.register_row(value)
                self.assertIsNone(row["deadline"]["determination_at"])
                self.assertEqual(row["issues"], [issue])
                self.assertEqual(row["next_action"], "clarify")
                self.assertEqual(row["answer_arrived"], {
                    "state": "arrived_unrecorded",
                    "first_arrival": "2030-01-05T18:00:00Z",
                    "occurrence_ids": ["delivery-1"],
                })

    def test_admitted_determination_still_suppresses_unrecorded_arrival(self):
        # The same delivery must not be called unrecorded if the clock accepts the date.
        row = self.register_row("2030-01-05")
        self.assertEqual(row["deadline"]["determination_at"], "2030-01-05")
        self.assertEqual(row["issues"], [])
        self.assertEqual(row["answer_arrived"], {
            "state": "none", "first_arrival": None, "occurrence_ids": [],
        })
