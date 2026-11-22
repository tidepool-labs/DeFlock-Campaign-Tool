"""Only final finding decision receipts can suppress settlement."""
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from campaign_tool.records import console_journal as cj, finding_review_apply as fa


class FindingReceiptOutcomeGuard(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.receipts = fa.receipts_path(self.root)
        self.receipts.parent.mkdir(mode=0o700)
        self.journal = self.root / 'journal'
        self.journal.mkdir(mode=0o700)

    def append_decision(self, target):
        pin = SimpleNamespace(source_project_id='sample', run_id='sample-run', name='sample')
        value = cj._canon({'analysis_root_sha256': fa.root_binding(self.root), 'reason': 'Synthetic reason.'})
        return cj.append(self.journal, pin, 'operator', '/findings/review/reject', 'finding_reject',
                         target, 'a' * 64, value, kind=cj.FINDING_KIND)

    def test_invalid_outcome_refuses_the_whole_pass_before_settling(self):
        # Corruption on the second entry must not let the first pending decision settle.
        self.append_decision('sample:N1')
        ident = self.append_decision('sample:N2')
        raw = (self.journal / cj.FILE).read_bytes()
        for outcome in ('DEFERRED', 'synthetic private text', 'ALREADY', 'WOULD_APPLY',
                        'REFUSED', 'REFUSED ', 'REFUSED finding_unknown\n',
                        'REFUSED finding_unknown extra', 'REFUSED finding_inputs_unavailable'):
            with self.subTest(outcome=outcome):
                receipt = (json.dumps({'entry_id': ident, 'at': '2026-01-01T00:00:00Z', 'outcome': outcome}) + '\n').encode()
                self.receipts.write_bytes(receipt)
                self.receipts.chmod(0o600)
                with mock.patch.object(fa, '_settle', return_value='APPLIED') as settle:
                    with self.assertRaises(fa.ApplyError) as refused:
                        fa.apply_journal(self.root, raw)
                    settle.assert_not_called()
                self.assertEqual(refused.exception.code, 'finding_receipts_unreadable')
                self.assertEqual(str(refused.exception), 'finding_receipts_unreadable')
                self.assertEqual(self.receipts.read_bytes(), receipt)

    def test_final_receipts_round_trip_and_return_already(self):
        # The reader must not narrow the format enough to reject existing final receipts.
        expected = {}
        for index, outcome in enumerate(('APPLIED', 'STALE_FINDING', 'SIGNATURE_REFUSED', 'SUPERSEDED',
                                         'REFUSED finding_unknown', 'REFUSED finding_ambiguous'), start=1):
            ident = self.append_decision(f'sample:N{index}')
            fa._receipt(self.root, ident, outcome)
            expected[ident] = outcome
        raw = (self.journal / cj.FILE).read_bytes()
        self.assertEqual(fa.read_receipts(self.root), expected)
        with mock.patch.object(fa, '_settle') as settle:
            self.assertEqual(fa.apply_journal(self.root, raw),
                             [(ident, 'finding_reject', 'ALREADY') for ident in expected])
            settle.assert_not_called()
