"""Read-only join of a placed reply and its applied console approval.

D7 supplies the successful golive_draft_join result, current manifest bytes and
queue model from the verified current pin, its independently chosen digest,
and the saved journal entry for the expected decision ID. An approval is
recorded against the pending revision on an earlier export; it advances the
queue revision by one before mail_drafts can place it. This helper does not
select a pin, open a store, verify a signature, or authorize any action.
"""
import hashlib
import re

from . import approval_queue, console_export, console_journal

_SHA256 = re.compile(r'[0-9a-f]{64}')
_PROJECT = re.compile(r'[0-9a-f]{16}')
_QUEUE_MODEL = f'{console_export.MODEL_DIR}/queue.json'
_INVALID = 'approval_receipt_invalid'


def _digest(raw):
    return hashlib.sha256(raw).hexdigest()


def _matches(pattern, value):
    return isinstance(value, str) and pattern.fullmatch(value) is not None


def _sealed_object(raw, seal):
    if not (isinstance(raw, bytes) and _matches(_SHA256, seal) and _digest(raw) == seal):
        return {}
    value = approval_queue._strict_load(raw, _INVALID)
    return value if isinstance(value, dict) else {}


def _bound_decision(entry, ident, draft, project):
    if not (isinstance(entry, dict) and entry.get('schema') == console_journal.SCHEMA
            and entry.get('entry_id') == ident and entry.get('action') in ('approve', 'phone_approve')
            and 'kind' not in entry and entry.get('source_project_id') == project
            and _matches(console_export.RUN_ID, entry.get('export_run_id'))
            and _matches(console_export.NAME, entry.get('export_name'))
            and entry['export_name'][17:] == entry['export_run_id']
            and entry.get('target_id') == draft['queue_id'] and type(entry.get('revision_seen')) is int
            and entry['revision_seen'] == draft['revision'] - 1):
        return False
    body = {key: value for key, value in entry.items() if key != 'entry_id'}
    return _digest(console_journal._canon(body).encode()) == ident


def _bound_queue_row(queue, draft, decision, project):
    rows = queue.get('rows')
    if not (queue.get('schema') == console_export.MODEL_SCHEMA and queue.get('state') == 'ok'
            and queue.get('source_project_id') == project and isinstance(rows, list)):
        return False
    matches = [row for row in rows if isinstance(row, dict) and row.get('id') == draft['queue_id']]
    if len(matches) != 1:
        return False
    row = matches[0]
    evidence = row.get('approval_evidence')
    if not (row.get('status') == 'approved' and row.get('kind') == draft.get('kind')
            and type(row.get('revision')) is int and row['revision'] == draft['revision']
            and isinstance(evidence, dict) and evidence.get('project') == project
            and type(evidence.get('signed_revision')) is int
            and evidence['signed_revision'] == decision['revision_seen']
            and _matches(_SHA256, evidence.get('signature_sha256'))):
        return False
    if decision['action'] == 'approve':
        # The typed path is idempotent on the exact same verified signature.
        sig = decision.get('value')
        return isinstance(sig, str) and _digest(sig.encode()) == evidence['signature_sha256']
    # The phone writer consumes a nonce and cannot approve an already approved row;
    # APPLIED and the one authoritative signed revision bind this entry to the row.
    return True


def join(*, draft, current_manifest, current_seal, queue_model, decision, decision_id):
    """Return APPLIED with allowlisted evidence, or PENDING without input details."""
    pending = {'state': 'PENDING'}
    try:
        if not (isinstance(draft, dict) and 'code' in draft and draft['code'] is None
                and _matches(_SHA256, draft.get('queue_id')) and type(draft.get('revision')) is int
                and draft['revision'] >= 1 and draft.get('manifest_sha256') == current_seal
                and _matches(_SHA256, decision_id)):
            return pending
        current = _sealed_object(current_manifest, current_seal)
        project = current.get('source_project_id')
        run = current.get('run_id')
        if not (current.get('schema') == console_export.SCHEMA and _matches(_PROJECT, project)
                and _matches(console_export.RUN_ID, run) and run == draft.get('run_id')
                and _bound_decision(decision, decision_id, draft, project)):
            return pending
        ids, outcomes, files = (current.get(key) for key in ('applied_journal_ids', 'apply_outcomes', 'files'))
        if not (isinstance(ids, list) and ids.count(decision_id) == 1 and isinstance(outcomes, dict)
                and outcomes.get(decision_id) == {'outcome': 'APPLIED'} and isinstance(files, dict)
                and _bound_queue_row(_sealed_object(queue_model, files.get(_QUEUE_MODEL)), draft, decision, project)):
            return pending
    except (approval_queue.ApprovalQueueError, TypeError, ValueError, RecursionError):
        return pending
    return {'state': 'APPLIED', 'queue_id': draft['queue_id'], 'revision': draft['revision'],
            'signed_revision': decision['revision_seen'], 'decision_id': decision_id, 'current_seal': current_seal}
