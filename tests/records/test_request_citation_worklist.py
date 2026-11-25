"""Contracts for the read-only citation worklist on saved run outputs."""
import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from scripts.records import request_citation_worklist as worklist


def arrival(c, kind='attachment', mime: str | None = 'application/pdf'):
    return {'original_sha256': c * 64, 'occurrence_id': 'occ-' + c, 'kind': kind, 'mime': mime}


def request(rid, *rows):
    return {'agency_key': 'alpha', 'ledger_request_id': rid, 'arrived': list(rows)}


def received(c, rid='req-1', basis=None, occurrence=None):
    return {'original_sha256': c * 64, 'agency_id': 'alpha', 'request_id': rid,
            'occurrence_ids': [occurrence or 'occ-' + c], 'kind_basis': basis}


class CitationWorklist(unittest.TestCase):
    def invoke(self, requests, items, item_run='run_1'):
        with tempfile.TemporaryDirectory() as tmp:
            run = Path(tmp) / 'run-outputs' / 'run_1'
            (run / 'outcomes').mkdir(parents=True)
            files = {run / 'CURRENT-REQUEST-REGISTER.json': {
                         'schema': 'records-request-register-v1', 'requests': requests},
                     run / 'outcomes' / 'received-items.json': {
                         'schema': 'records-received-item-v1', 'run_id': item_run, 'items': items}}
            before = {path: json.dumps(doc).encode() for path, doc in files.items()}
            for path, raw in before.items():
                path.write_bytes(raw)
            out, err = io.StringIO(), io.StringIO()
            with redirect_stdout(out), redirect_stderr(err):
                rc = worklist.main(['--run-dir', str(run)])
            self.assertEqual({path: path.read_bytes() for path in files}, before)
            return rc, out.getvalue(), err.getvalue()

    def test_exact_request_groups_and_counts_parts_once(self):
        # A part-row count or agency-only grouping doubles/moves missing sources.
        rc, text, _ = self.invoke([
            request('req-1', arrival('a', 'mail', 'message/rfc822'), arrival('b')),
            request('req-2', arrival('c'))], [received('a'), received('a'), received('b'),
            received('c', 'req-2', {'page': 1, 'quote': 'Private fixture text'})])
        self.assertEqual(rc, 0)
        data = json.loads(text)
        self.assertEqual(data['requests'], [{'agency': 'alpha', 'request': 'req-1',
            'mail_part_missing': ['a' * 64], 'pdf_page_missing': ['b' * 64]}])
        self.assertEqual(data['unassigned'], [])
        self.assertNotIn('Private fixture text', text)

    def test_conflict_unknown_type_and_wrong_occurrence_do_not_inherit_citations(self):
        rc, text, _ = self.invoke([
            request('req-1', arrival('a'), arrival('b', mime=None), arrival('c'))], [
            received('a', 'req-2', {'page': 2, 'quote': 'Q'}), received('b'),
            received('c', basis={'page': 4, 'quote': 'Q'}, occurrence='not-registered')])
        self.assertEqual(rc, 0)
        data = json.loads(text)
        self.assertEqual(data['requests'][0]['pdf_page_missing'], ['c' * 64])
        self.assertEqual(data['unassigned'], [
            {'original': 'a' * 64, 'reason': 'conflicting_request'},
            {'original': 'b' * 64, 'reason': 'unknown_type'}])

    def test_mail_part_and_pdf_page_are_not_interchangeable(self):
        mail = lambda c: arrival(c, 'mail', 'message/rfc822')
        rc, text, _ = self.invoke([request('req-1', mail('a'), mail('b'), arrival('c'))], [
            received('a', basis={'page': None, 'part': '1.2', 'quote': 'Q'}),
            received('b', basis={'page': 3, 'quote': 'Q'}),
            received('c', basis={'page': None, 'part': '2', 'quote': 'Q'})])
        self.assertEqual(rc, 0)
        data = json.loads(text)
        self.assertEqual(data['requests'][0]['mail_part_missing'], ['b' * 64])
        self.assertEqual(data['requests'][0]['pdf_page_missing'], ['c' * 64])

    def test_unknown_mime_with_a_page_citation_stays_unassigned(self):
        # Review t_8acd6ba8 item 1: a page locator does not establish PDF type.
        rc, text, _ = self.invoke([
            request('req-1', arrival('a', mime=None), arrival('b', mime='image/png'),
                    arrival('c', mime=None), arrival('c', mime='application/pdf') | {'occurrence_id': 'occ-c2'})], [
            received('a', basis={'page': 1, 'quote': 'Q'}),
            received('b', basis={'page': 2, 'quote': 'Q'}),
            received('c', basis={'page': 3, 'quote': 'Q'})])
        self.assertEqual(rc, 0)
        data = json.loads(text)
        # c is known to be a PDF through its other registered arrival and is cited, so it is neither listed nor lost.
        self.assertEqual(data['requests'], [])
        self.assertEqual(data['unassigned'], [
            {'original': 'a' * 64, 'reason': 'unknown_type'},
            {'original': 'b' * 64, 'reason': 'unknown_type'}])

    def test_wrong_run_is_not_an_empty_success(self):
        rc, text, error = self.invoke([], [], item_run='run_2')
        self.assertEqual((rc, text), (2, ''))
        self.assertIn('inputs unavailable', error)
