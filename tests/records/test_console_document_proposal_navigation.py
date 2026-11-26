"""Existing /documents handler: proposal IDs identify review-only rows."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from campaign_tool.records import console_documents
from tests.records.test_console_documents import H1, RUN, build, put

P1, P2 = 'a' * 64, 'b' * 64


class ProposalNavigation(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.project = Path(directory.name)
        build(self.project)
        self.proposals = self.project / 'run-outputs' / RUN / 'outcomes' / 'gap-proposals.json'

    def page(self, agency=None):
        handler = Mock(path='/documents' + ('?agency=' + agency if agency else ''),
                       options={'project': self.project, 'run_id': RUN})
        self.assertTrue(console_documents.serve(handler))
        handler._fail.assert_not_called()
        handler._send.assert_called_once()
        status, html = handler._send.call_args.args
        self.assertEqual(status, 200)
        return html

    def set_proposals(self, *sections):
        put(self.proposals, {'schema': 'records-gap-proposals-v1', 'run_id': RUN, 'agencies': [
            {'agency_id': agency, 'requests': [{'proposals': rows}]} for agency, rows in sections
        ]})

    def test_unique_id_links_to_its_review_row_with_existing_source(self):
        self.set_proposals(('alpha-pd', [{'proposal_id': P1, 'question': 'First review question?',
                                         'review_required': True, 'evidence_kind': 'document',
                                         'item_id': 'i-a', 'page': 2}]))
        page = self.page('alpha-pd')
        self.assertIn('id="proposal-' + P1 + '"', page)
        self.assertIn('href="#proposal-' + P1 + '">' + P1 + '</a>', page)
        row = page.split('id="proposal-' + P1 + '"', 1)[1].split('</tr>', 1)[0]
        self.assertIn('First review question?', row)
        self.assertIn('review required, not publishable', row)
        self.assertIn(f'href="/page?doc={H1[:12]}&amp;p=2"', row)
        self.assertIn('id="proposal-' + P1 + '"', self.page())

    def test_duplicate_ids_are_text_only_even_after_agency_filtering(self):
        self.set_proposals(('alpha-pd', [{'proposal_id': P1, 'question': 'Alpha?'}]),
                           ('beta-so', [{'proposal_id': P1, 'question': 'Beta?'}]))
        for agency in (None, 'alpha-pd'):
            with self.subTest(agency=agency):
                page = self.page(agency)
                self.assertIn(P1, page)
                self.assertNotIn('id="proposal-' + P1 + '"', page)
                self.assertNotIn('href="#proposal-' + P1 + '"', page)

    def test_missing_id_and_other_agency_do_not_get_a_target(self):
        self.set_proposals(('alpha-pd', [{'question': 'Unidentified question?'}]),
                           ('beta-so', [{'proposal_id': P2, 'question': 'Beta only?'}]))
        page = self.page('alpha-pd')
        self.assertIn('ID unavailable', page)
        self.assertIn('Unidentified question?', page)
        self.assertNotIn('id="proposal-', page)
        self.assertNotIn(P2, page)
        self.assertNotIn('Beta only?', page)
        self.assertIn('id="proposal-' + P2 + '"', self.page('beta-so'))
