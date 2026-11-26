"""Navigation contract for the private cited-page renderer."""
import unittest

from campaign_tool.records.console_page_page import _render
from tests.records.test_console_documents import H1, H2


class CitedPageReturnTests(unittest.TestCase):
    def test_viewed_and_omitted_pages_return_to_the_same_document(self):
        row = {
            "agency": "alpha-pd", "received_at": "2026-09-01T06:30:00Z",
            "route": "native", "confidence_band": "not_applicable",
            "review_state": "not_applicable", "citations": [],
            "text": "Synthetic page text",
        }
        # A link added only in the text branch strands readers of known omitted pages.
        # Use a second document to catch a fixed destination instead of the selected prefix.
        for digest in (H1, H2):
            prefix = digest[:12]
            cases = ((2, dict(row, original_sha256=digest), None), (5, None, "page_bytes"))
            for page, exported_row, reason in cases:
                with self.subTest(prefix=prefix, page=page):
                    html = _render(prefix, page, exported_row, reason)
                    self.assertIn(f'<a href="/document?id={prefix}">Back to this document</a>', html)
                    self.assertIn('<a href="/documents">Back to documents</a>', html)
