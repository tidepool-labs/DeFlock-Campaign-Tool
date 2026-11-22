"""Stage 17: stage and withdraw one signed synthetic document packet, offline.

Run in a dedicated host process, never alongside live approval work. The
projection below is rehearsal data, not the attended site-content bridge.
"""
import argparse
from contextlib import contextmanager
from functools import partial
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import tempfile
from types import SimpleNamespace
from unittest.mock import patch

from .. import approval
from . import public_packets, publish, redaction_record, rehearse_anchor, rehearsal_trust, run_stages
from .ledger import store
from .run import RunError

STAGES = ('17',)
TITLE = 'Document packet to staging and rollback'
_AGENCY = 'example-agency'
_STAMP = '2026-01-01T00:00:00Z'
_ORIGINAL = b'Synthetic record\nContact rehearsal@example.invalid\n'
_REDACTED = b'Synthetic record\nContact [email:25 bytes removed]\n'
_BASELINE = b'Synthetic existing site page\n'
_ROLLBACK_REASON = 'Synthetic stage 17 rehearsal'


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def _put(path, data):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with os.fdopen(os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600), 'wb') as out:
        out.write(data)
    return path


def _json(path, value):
    return _put(path, publish.encoded(value))


def _sign(sandbox, payload):
    return rehearse_anchor._ssh(sandbox, '-Y', 'sign', '-n', approval.NAMESPACE,
                               '-f', str(sandbox / 'trusted'), data=payload).decode('ascii')


@contextmanager
def _anchor(sandbox, signers):
    # Scope the verifier's temp files to this run as in the Stage 20 driver.
    workspace = SimpleNamespace(TemporaryDirectory=partial(tempfile.TemporaryDirectory, dir=sandbox))
    with rehearsal_trust.sandbox_anchor(sandbox, signers), patch.object(approval, 'tempfile', workspace), \
            patch.dict(os.environ, HOME=str(sandbox)):
        yield


def _fixture_stage(db, root, subject, stage, **binding):
    receipt = _json(root / f'synthetic-{stage}.json', {'stage': stage, 'subject': subject, **binding})
    digest = _sha(receipt.read_bytes())
    db.execute('INSERT INTO receipts(sha256,stage,subject_sha256,reviewer_id,role,verdict,coverage,locators,rationale,'
               "model_or_tool,input_hashes,created_at_tz,path) VALUES (?,?,?,'stage17','synthetic','accept','{}',"
               "'[]','rehearsal fixture','stage17','{}',?,?)", (digest, stage, subject, _STAMP, str(receipt)))
    db.execute("INSERT INTO stage_state VALUES (?,?,'done',?,'stage17',?,'stage17',NULL)",
               (subject, stage, digest, _STAMP))
    return digest


def _seed_original(project, ledger):
    original = _put(project / 'original.txt', _ORIGINAL)
    subject = _sha(_ORIGINAL)
    # store.initialize sets umask for its CLI. Restore the caller's umask.
    mask = os.umask(0o077)
    try:
        store.initialize(ledger)
    finally:
        os.umask(mask)
    with store.ledger(ledger) as db:
        db.execute("INSERT INTO runs(run_id,kind,started_at,engine_version,status,summary) "
                   "VALUES ('stage17','rehearsal',?,'stage17','completed','{}')", (_STAMP,))
        db.execute('INSERT INTO originals(sha256,bytes,first_seen_at,role,scope,storage_path,preservation_status,'
                   "provenance_json) VALUES (?,?,?,'agency_original','in_scope',?,'preserved','{}')",
                   (subject, len(_ORIGINAL), _STAMP, str(original)))
        for stage in ('preserve', 'extract'):
            _fixture_stage(db, ledger.parent, subject, stage)
        db.commit()
    _json(project / 'run-outputs/run_20260101T000000Z_01234567/outcomes/received-items.json', {'items': [
        {'original_sha256': subject, 'agency_id': _AGENCY, 'request_id': 'example-request',
         'extraction_status': 'current', 'received_at': _STAMP}]})
    return subject


def _redaction(sandbox, project, subject):
    folder = redaction_record.record_dir(project, _AGENCY, subject)
    # The verifier requires each ancestor inside the store to be owner-only.
    for part in reversed((folder, *folder.parents)):
        if project in part.parents:
            part.mkdir(mode=0o700, exist_ok=True)
    derivative = _put(project / 'redacted.txt', _REDACTED)
    common = dict(schema_version=1, document_id=subject, source_sha256=subject)
    span = {'category': 'email', 'locator': {'line': 2, 'offset': 8, 'length': 25}}
    proposal = _json(folder / 'PROPOSAL.json', dict(
        common, redaction_required=True, proposed=[span],
        not_detected_note='', scan_receipt=_sha(_ORIGINAL), created_at=_STAMP)).read_bytes()
    signature = _sign(sandbox, approval.payload('flock-redaction', subject, proposal.decode()))
    signed = dict(common, proposal_sha256=_sha(proposal), signed_by=rehearse_anchor._PRINCIPAL, signed_at=_STAMP)
    _json(folder / 'SIGNOFF.json', dict(signed, scope='flock-redaction', signature=signature))
    return _json(folder / 'ELIGIBILITY.json', dict(signed, derivative_path=str(derivative),
                 derivative_sha256=_sha(_REDACTED), applied_at=_STAMP))


class _Rehearsal:
    def __init__(self, sandbox):
        self.sandbox = sandbox
        self.project = sandbox / 'project'
        self.root = self.project / 'processing-v1'
        self.ledger = self.root / 'ledger.sqlite'
        self.site = sandbox / 'site'
        self.baseline = self.site / 'index.txt'

    def propose(self):
        _put(self.sandbox / run_stages.SANDBOX_MARKER, b'{"sandbox_marker_version":1}\n')
        self.signers, = rehearse_anchor.make_signer(self.sandbox)
        self.project.mkdir(mode=0o700)
        self.root.mkdir(mode=0o700)
        self.subject = _seed_original(self.project, self.ledger)
        record = _redaction(self.sandbox, self.project, self.subject)
        with _anchor(self.sandbox, self.signers):
            proposed = public_packets.propose_document(self.project, self.subject, _sha(record.read_bytes()))
        self.packet = proposed['id']
        _put(self.baseline, _BASELINE)
        return record, _json(self.sandbox / 'proposed.json', proposed)

    def enable_switch(self):
        public_packets.init(self.project)
        payload = public_packets.switch_payload(self.project)
        with _anchor(self.sandbox, self.signers):
            public_packets.enable(self.project, _sign(self.sandbox, payload))

    def approve_packet(self):
        self.enable_switch()
        payload = public_packets.payload(self.project, self.packet)
        with _anchor(self.sandbox, self.signers):
            return public_packets.approve(self.project, self.packet, _sign(self.sandbox, payload), revision=0)

    def project_approved_change(self):
        """Seed a sandbox-only publisher proposal; never trust an external change file."""
        with _anchor(self.sandbox, self.signers):
            written = public_packets.write_change(self.project, self.packet)
        change_path = self.project / written['file']
        raw = change_path.read_bytes()
        change = json.loads(raw)
        if (written['result'], change['schema'], change['packet'], change['kind']) != (
                'written', 'records-site-change-v1', self.packet, 'document'):
            raise ValueError('unexpected packet change')
        self.proposal = 'prop_' + self.packet
        self.public = _put(self.root / 'proposals/public' / (self.proposal + '.md'),
                           f"# {change['title']}\n\n{change['text']}\n".encode())
        manifest = _json(self.root / 'proposals/private' / (self.proposal + '.manifest.json'), {
            'original_sha256': self.subject, 'site_change_sha256': _sha(raw), 'synthetic_rehearsal': True})
        with store.ledger(self.ledger) as db:
            digest = _sha(self.public.read_bytes())
            receipt = _fixture_stage(db, self.root, self.subject, 'privacy', public_sha256=digest)
            db.execute("INSERT INTO proposals(id,tier,public_content_sha256,manifest_sha256,privacy_receipt_sha256,"
                       "owner_approval) VALUES (?,'A',?,?,?,'none')",
                       (self.proposal, digest, _sha(manifest.read_bytes()), receipt))
            db.commit()
        return change_path

    def approve_signed(self):
        signed = self.approve_packet()
        change = self.project_approved_change()
        # The publisher API takes a principal, not a signature: verify a separate
        # sandbox signature over the exact projected bytes before recording approval.
        content = self.public.read_bytes()
        payload = approval.payload('stage17-rehearsal-publication', self.proposal, content.decode())
        signature = _sign(self.sandbox, payload)
        with _anchor(self.sandbox, self.signers):
            principal = approval.verify(payload, signature)
        decision = publish.approve(self.root, self.proposal, owner_id=principal)
        if decision['content_sha256'] != _sha(content):
            raise ValueError('publisher approval differs from signed bytes')
        return (change, self.public, Path(decision['receipt']),
                _json(self.sandbox / 'signed-approval.json', {'packet_approval': signed, 'publisher_signature': signature}))

    def stage_release(self):
        self.release = publish.publish(self.root, self.proposal, staging=self.site)
        return self._release_manifest(self.release['version']),

    def _release_manifest(self, version):
        return self.site / 'releases' / version / 'manifest.json'

    def staged_files(self):
        version = self.release['version']
        manifest_path = self._release_manifest(version)
        manifest = json.loads(manifest_path.read_bytes())
        current_raw = (self.site / 'current.json').read_bytes()
        current = json.loads(current_raw)['proposals'][self.proposal]
        archived = manifest_path.with_name(self.proposal + '.md')
        live = self.site / 'content' / archived.name
        digest = self.release['content_sha256']
        if (manifest['schema'], manifest['action'], manifest['proposal_id'], manifest['version']) != (
                'records-staging-release-v2', 'publish', self.proposal, version) or (
                current['version'] != version or any(value != digest for value in (
                    manifest['content_sha256'], manifest['authority']['approved_content_sha256'],
                    current['content_sha256'], _sha(archived.read_bytes()), _sha(live.read_bytes()),
                    _sha(self.public.read_bytes())))):
            raise ValueError('unexpected staged files')
        # current.json changes during rollback; preserve its staged bytes for readback.
        return archived, _put(self.sandbox / 'staged-current.json', current_raw)

    def rollback(self):
        self.rolled = publish.rollback(self.root, self.proposal, staging=self.site, reason=_ROLLBACK_REASON)
        return self._release_manifest(self.rolled['version']),

    def verify_rollback(self):
        version = self.rolled['version']
        staged = self.release['version']
        manifest = json.loads(self._release_manifest(version).read_bytes())
        current_path = self.site / 'current.json'
        current = json.loads(current_path.read_bytes())
        if (self.rolled != {'proposal_id': self.proposal, 'rolled_back': staged, 'restored': None,
                            'version': version, 'withdrawn': True}
                or (manifest['schema'], manifest['action'], manifest['proposal_id'], manifest['version'],
                    manifest['previous_version'], manifest['content_sha256']) != (
                        'records-staging-release-v2', 'rollback', self.proposal, version, staged, None)
                or current != {'schema': 'records-staging-current-v1', 'proposals': {}, 'withdrawn': {
                    self.proposal: {'version': version, 'rolled_back_from': staged, 'reason': _ROLLBACK_REASON}}}
                or any((self.site / 'content').iterdir()) or self.baseline.read_bytes() != _BASELINE):
            raise ValueError('sandbox site not restored')
        return current_path, self.baseline


def _empty_sandbox(sandbox):
    rehearsal_trust.require_private(sandbox)
    if any(sandbox.iterdir()):
        raise ValueError('sandbox must be empty')


def run(sandbox: Path):
    sandbox = rehearsal_trust.sandbox_path(sandbox)
    _empty_sandbox(sandbox)
    rehearsal = _Rehearsal(sandbox)
    result = {'stages': list(STAGES), 'steps': [], 'manual_steps': [], 'codes': []}
    for name in ('propose', 'approve_signed', 'stage_release', 'staged_files', 'rollback', 'verify_rollback'):
        try:
            outputs = [{'path': path.relative_to(sandbox).as_posix(), 'sha256': _sha(path.read_bytes())}
                       for path in getattr(rehearsal, name)()]
        except (OSError, ValueError, KeyError, approval.ApprovalError, public_packets.PacketError, RunError,
                sqlite3.Error, subprocess.SubprocessError):
            code = name.upper() + '_FAILED'
            result['steps'].append({'name': name, 'code': code, 'outputs': []})
            result['codes'].append(code)
            break
        result['steps'].append({'name': name, 'code': 'OK', 'outputs': outputs})
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--sandbox', required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        sandbox = rehearsal_trust.sandbox_path(args.sandbox)
        sandbox.mkdir(mode=0o700, exist_ok=True)
        _empty_sandbox(sandbox)
    except (OSError, ValueError):
        parser.error('--sandbox must be a new or empty private 0700 directory')
    result = run(sandbox)
    print(json.dumps(result))
    return int(bool(result['manual_steps'] or any(step['code'] != 'OK' for step in result['steps'])))


if __name__ == '__main__':
    raise SystemExit(main())
