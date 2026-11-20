"""Record-specific links in a synthetic agency fact sheet."""
import unittest

from campaign_tool.records import console_page_facts as page
from tests.records import test_console_facts as fixture


class FactSheetDocumentLinks(unittest.TestCase):
    # Catch citations that send both records to the same agency listing instead
    # of their own metadata, or that expose a full digest in the rendered HTML.
    def test_two_citations_link_to_distinct_record_details(self):
        agency = fixture.AGENCIES[0]
        sheet = fixture.sheet(agency, {"cost": [
            fixture.statement("First quoted fee.", doc=0, page_no=3),
            fixture.statement("Second quoted fee.", doc=1, page_no=7),
        ]})
        status, html = page.render({"state": "ok", "report": fixture.doc(agencies=[sheet])}, agency)

        self.assertEqual(status, 200)
        for digest, number in zip(fixture.DOC[:2], (3, 7)):
            prefix = digest[:12]
            with self.subTest(page=number):
                self.assertRegex(html, rf'{prefix} page {number}[^<]*<a href="/document\?id={prefix}">Record details</a>')
        self.assertIn(f'<a href="/documents?agency={agency}">Agency library</a>', html)
        self.assertNotRegex(html, r"[0-9a-fA-F]{64}")
        self.assertNotIn('href="/page?', html)
