"""Round-trip the comparison summary's agency links using in-memory rows."""
import html
import re
import unittest

from campaign_tool.records import console_page_comparison as page


AGENCY = 'Synthetic A & "<West>" / +'
OTHER = "Synthetic B"
AGENCY_LINK = "/comparison?agency=Synthetic%20A%20%26%20%22%3CWest%3E%22%20%2F%20%2B"
AGENCY_LABEL = "Synthetic A &amp; &quot;&lt;West&gt;&quot; / +"


def fixture():
    return {
        "state": "ok",
        "report": {
            "disclaimer": "Synthetic comparison",
            "input_status": "fact_sheets_supplied",
            "inventory_gaps": [],
            "as_of": "2026-04-30",
            "rows": [
                {"agency_id": agency, "status": status, "field": "access_logs",
                 "expected": marker, "reason": "", "citation": {"label": "", "url": ""}, "evidence": []}
                for agency, status, marker in (
                    (AGENCY, "gap", "west-gap-detail"),
                    (AGENCY, "meets", "west-meets-detail"),
                    (OTHER, "gap", "other-gap-detail"),
                    (OTHER, "meets", "other-meets-detail"),
                )
            ],
        },
    }


class AgencyLinks(unittest.TestCase):
    def follow_agency(self, loaded, rendered, expected_status):
        match = re.search(r'<a href="([^"]+)">' + re.escape(AGENCY_LABEL) + r"</a>", rendered)
        if match is None:
            self.fail("missing escaped agency link")
        path = match[1]
        filters = page.parse_filters(html.unescape(path))
        if filters is None:
            self.fail(f"invalid agency link: {path}")
        self.assertEqual(filters, (AGENCY, expected_status))
        suffix = "" if expected_status is None else "&amp;status=" + expected_status
        self.assertEqual(path, AGENCY_LINK + suffix)
        narrowed = page.render(loaded, *filters)
        self.assertIn("</a></td><td>1</td><td>1</td><td>0</td></tr>", narrowed)
        self.assertNotIn(OTHER, narrowed)
        self.assertIn("west-gap-detail", narrowed)
        return narrowed

    def test_agency_link_preserves_gap_filter(self):
        # A link built from the agency alone silently reopens the meets rows.
        loaded = fixture()
        narrowed = self.follow_agency(loaded, page.render(loaded, status="gap"), "gap")
        self.assertNotIn("west-meets-detail", narrowed)

    def test_all_status_agency_link_remains_agency_only(self):
        # Adding an empty/default status would break or narrow the all-status link.
        loaded = fixture()
        narrowed = self.follow_agency(loaded, page.render(loaded), None)
        self.assertIn("west-meets-detail", narrowed)
