"""An empty readable subset must not be reported as an empty analysis root."""
import unittest

from campaign_tool.records.console_page_findings import render_body


class FindingsSourceCoverageTests(unittest.TestCase):
    def test_complete_empty_can_claim_no_findings(self):
        # An unconditional uncertainty notice would misstate a fully readable result.
        body = render_body({"state": "ok", "rows": [], "broken": 0})
        self.assertIn("The analysis root holds no findings.", body)
        self.assertNotIn("could not be read", body)
        self.assertNotIn("total for the analysis root is unknown", body)

    def test_partial_empty_only_describes_readable_agencies(self):
        # The reader can succeed with no readable rows and still count broken agencies.
        body = render_body({"state": "ok", "rows": [], "broken": 2})
        self.assertIn("2 agency reviews could not be read", body)
        self.assertNotIn("holds no findings", body)
        self.assertNotIn("0 findings in every review state", body)
        self.assertIn("No findings could be listed from the readable agencies.", body)
        self.assertIn("The total for the analysis root is unknown.", body)

    def test_partial_nonempty_keeps_readable_findings(self):
        # A branch on broken agencies alone could hide the surviving row.
        row = {"agency": "example-agency", "finding_id": "N1", "claim": "Synthetic readable finding.",
               "review_state": "unreviewed", "hold_code": "FINDING_UNREVIEWED"}
        body = render_body({"state": "ok", "rows": [row], "broken": 1})
        self.assertIn("1 agency review could not be read", body)
        self.assertIn("Synthetic readable finding.", body)
        self.assertNotIn("holds no findings", body)
        self.assertNotIn("No findings could be listed", body)
