import hashlib
import json
import unittest

from campaign_tool.records import console_export, console_journal, golive_approval_receipt


def encode(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':')).encode()


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


class ApprovalReceiptTests(unittest.TestCase):
    def setUp(self):
        self.project = '1' * 16
        self.queue_id = '2' * 64
        self.proof = 'synthetic-private-proof'
        self.body = {'schema': console_journal.SCHEMA, 'source_project_id': self.project,
                     'export_run_id': 'sandbox-before', 'export_name': '20261124T000000Z-sandbox-before',
                     'target_id': self.queue_id, 'revision_seen': 0, 'action': 'approve', 'value': self.proof,
                     'actor': 'synthetic-private-actor', 'at': '2026-11-24T00:01:00Z', 'route': '/approvals/approve'}
        self.draft = {'code': None, 'queue_id': self.queue_id, 'revision': 1, 'kind': 'reply-extension',
                      'state': 'placed', 'run_id': 'sandbox-after', 'export': '20261124T000200Z-sandbox-after'}
        evidence = {'signed_revision': 0, 'project': self.project, 'signature_sha256': digest(self.proof.encode())}
        self.row = {'id': self.queue_id, 'kind': self.draft['kind'], 'revision': 1,
                    'status': 'approved', 'approval_evidence': evidence, 'body': 'synthetic-private-body'}

    def inputs(self, *, body=None, rows=None, manifest_delta=None):
        body = self.body if body is None else body
        decision_id = digest(encode(body))
        queue = encode({'schema': console_export.MODEL_SCHEMA, 'state': 'ok', 'source_project_id': self.project,
                        'rows': [self.row] if rows is None else rows})
        current = encode({'schema': console_export.SCHEMA, 'run_id': self.draft['run_id'],
                          'source_project_id': self.project, 'applied_journal_ids': [decision_id],
                          'apply_outcomes': {decision_id: {'outcome': 'APPLIED'}},
                          'files': {'console-model/queue.json': digest(queue)}, **(manifest_delta or {})})
        return {'draft': {**self.draft, 'manifest_sha256': digest(current)}, 'current_manifest': current,
                'current_seal': digest(current), 'queue_model': queue,
                'decision': {**body, 'entry_id': decision_id}, 'decision_id': decision_id}

    def test_applied_uses_the_placed_revision_and_keeps_private_fields_out(self):
        # The console tap sees the pending revision; apply advances it before placement.
        for action in ('approve', 'phone_approve'):
            with self.subTest(action=action):
                args = self.inputs(body={**self.body, 'action': action})
                result = golive_approval_receipt.join(**args)
                self.assertEqual(result, {'state': 'APPLIED', 'queue_id': self.queue_id,
                                         'revision': 1, 'signed_revision': 0,
                                         'decision_id': args['decision_id'], 'current_seal': args['current_seal']})

    def test_unbound_or_non_approval_entry_stays_pending_even_if_applied(self):
        # Another action, project, export, item, revision or private proof cannot prove this reply.
        for field, value in [('action', 'edit'), ('target_id', '3' * 64), ('revision_seen', 1),
                             ('revision_seen', False), ('source_project_id', '3' * 16),
                             ('export_run_id', 'unrelated-run'), ('value', 'different-signature')]:
            with self.subTest(field=field, value=value):
                args = self.inputs(body={**self.body, field: value})
                self.assertEqual(golive_approval_receipt.join(**args), {'state': 'PENDING'})
        args = self.inputs()
        for changed in ({'decision': {**args['decision'], 'actor': 'changed-private-actor'}},
                        {'decision_id': '3' * 64}, {'decision': None}):
            self.assertEqual(golive_approval_receipt.join(**(args | changed)), {'state': 'PENDING'})

    def test_only_current_bound_manifest_and_queue_bytes_count(self):
        # An older D1 receipt, corrupt model bytes, or a duplicate manifest key must not join.
        args = self.inputs()
        for changed in ({'current_seal': None}, {'current_seal': '3' * 64}, {'queue_model': b'{}'},
                        {'draft': {**args['draft'], 'manifest_sha256': '3' * 64}},
                        {'draft': {**args['draft'], 'code': 'REPLY_NOT_PLACED'}}):
            with self.subTest(fields=list(changed)):
                self.assertEqual(golive_approval_receipt.join(**(args | changed)), {'state': 'PENDING'})
        duplicate = b'{"schema":"invalid",' + args['current_manifest'][1:]
        args.update(current_manifest=duplicate, current_seal=digest(duplicate))
        args['draft']['manifest_sha256'] = args['current_seal']
        self.assertEqual(golive_approval_receipt.join(**args), {'state': 'PENDING'})

    def test_both_applied_markers_and_the_current_approved_queue_row_must_agree(self):
        # A recorded decision can be stale, refused or unrelated to the current approved row.
        ident = self.inputs()['decision_id']
        for delta in ({'applied_journal_ids': []}, {'apply_outcomes': {}},
                      {'apply_outcomes': {ident: {'outcome': 'DEFERRED'}}},
                      {'apply_outcomes': {ident: {'outcome': 'APPLIED', 'refusal': 'synthetic-private'}}},
                      {'applied_journal_ids': [ident, ident]}):
            with self.subTest(delta=delta):
                self.assertEqual(golive_approval_receipt.join(**self.inputs(manifest_delta=delta)), {'state': 'PENDING'})
        stale = {**self.row['approval_evidence'], 'signed_revision': 1}
        for rows in ([], [self.row, self.row], [{**self.row, 'status': 'pending'}],
                     [{**self.row, 'approval_evidence': stale}]):
            with self.subTest(count=len(rows)):
                self.assertEqual(golive_approval_receipt.join(**self.inputs(rows=rows)), {'state': 'PENDING'})
