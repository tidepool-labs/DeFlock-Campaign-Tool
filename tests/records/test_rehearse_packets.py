"""Runtime acceptance for the offline Stage 17 rehearsal."""
from contextlib import redirect_stderr, redirect_stdout
import hashlib
import io
import json
from pathlib import Path
import smtplib
import socket
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from campaign_tool import approval
from campaign_tool.records import public_packets, publish, rehearse_packets as driver, run_stages

_STEPS = ('propose', 'approve_signed', 'stage_release', 'staged_files', 'rollback', 'verify_rollback')


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load(path):
    return json.loads(path.read_bytes())


class PacketsRehearsalTests(unittest.TestCase):
    def setUp(self):
        self.temp = self.enterContext(tempfile.TemporaryDirectory())
        self.sandbox = Path(self.temp).resolve() / 's17'

    def fresh(self):
        self.sandbox.mkdir(mode=0o700)
        return driver._Rehearsal(self.sandbox)

    def expected(self, failed=None):
        """Build the exact receipt from the expected file inventory, not the driver's report."""
        root = self.sandbox
        steps = []
        for name in _STEPS:
            if failed == name:
                code = name.upper() + '_FAILED'
                steps.append({'name': name, 'code': code, 'outputs': []})
                return {'stages': ['17'], 'steps': steps, 'manual_steps': [], 'codes': [code]}
            project = root / 'project'
            packet = load(root / 'proposed.json')['id']
            proposal = 'prop_' + packet
            proposals = project / 'processing-v1/proposals'
            manifests = {load(p)['action']: p for p in (root / 'site/releases').glob('*/manifest.json')}
            paths = {
                'propose': (*project.glob('agencies/example-agency/redaction/*/ELIGIBILITY.json'), root / 'proposed.json'),
                'approve_signed': (project / f'site-changes/{packet}-r0.json', proposals / f'public/{proposal}.md',
                                   proposals / f'private/{proposal}.approved.json', root / 'signed-approval.json'),
                'stage_release': (manifests.get('publish'),),
                'staged_files': (manifests['publish'].with_name(proposal + '.md'), root / 'staged-current.json')
                if 'publish' in manifests else (),
                'rollback': (manifests.get('rollback'),),
                'verify_rollback': (root / 'site/current.json', root / 'site/index.txt'),
            }
            steps.append({'name': name, 'code': 'OK', 'outputs': [
                {'path': path.relative_to(root).as_posix(), 'sha256': digest(path)} for path in paths[name]]})
        return {'stages': ['17'], 'steps': steps, 'manual_steps': [], 'codes': []}

    def test_c1_full_rehearsal_restores_active_site(self):
        """C1: require disk readback of signed projection, exact staging and withdrawal."""
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(driver.main(['--sandbox', str(self.sandbox)]), 0)
        report = json.loads(out.getvalue())
        self.assertEqual(report, self.expected())
        proposed = load(self.sandbox / 'proposed.json')
        packet = proposed['id']
        proposal = 'prop_' + packet
        self.assertEqual(proposed, {'id': packet, 'revision': 0, 'result': 'proposed'})
        project = self.sandbox / 'project'
        original = digest(project / 'original.txt')
        folder = project / 'agencies/example-agency/redaction' / original
        change = load(project / f'site-changes/{packet}-r0.json')
        self.assertEqual(change['kind'], 'document')
        self.assertEqual(change['documents'], [{'sha256': original, 'date_received': '2026-01-01',
                                               'source_request': 'example-request',
                                               'redaction_ids': [digest(folder / 'ELIGIBILITY.json')],
                                               'use_restriction': False}])
        self.assertEqual((project / 'redacted.txt').read_bytes(), b'Synthetic record\nContact [email:25 bytes removed]\n')
        recorded = load(self.sandbox / 'signed-approval.json')
        self.assertEqual(recorded['packet_approval'], {'id': packet, 'signed_by': change['signed_by']})
        current = load(self.sandbox / 'staged-current.json')['proposals'][proposal]
        site = self.sandbox / 'site'
        staged = site / 'releases' / current['version']
        manifest = load(staged / 'manifest.json')
        content = (staged / (proposal + '.md')).read_bytes()
        self.assertEqual(content, f"# {change['title']}\n\n{change['text']}\n".encode())
        content_hash = hashlib.sha256(content).hexdigest()
        self.assertEqual(manifest['authority']['approved_content_sha256'], content_hash)
        self.assertEqual(current['content_sha256'], content_hash)
        self.assertEqual(manifest['schema'], 'records-staging-release-v2')
        payload = approval.payload('stage17-rehearsal-publication', proposal, content.decode())
        with driver._anchor(self.sandbox, self.sandbox / 'rehearsal.allowed_signers'):
            self.assertEqual(approval.verify(payload, recorded['publisher_signature']), change['signed_by'])
        after = load(site / 'current.json')
        withdrawal = after['withdrawn'][proposal]
        self.assertEqual(after, {'schema': 'records-staging-current-v1', 'proposals': {}, 'withdrawn': {proposal: {
            'version': withdrawal['version'], 'rolled_back_from': current['version'], 'reason': 'Synthetic stage 17 rehearsal'}}})
        self.assertEqual(list((site / 'content').iterdir()), [])
        self.assertEqual((site / 'index.txt').read_bytes(), b'Synthetic existing site page\n')

    def test_c2_unsigned_packet_refused_with_switch_on(self):
        """C2: refusing solely because the switch is OFF would not prove the packet gate."""
        rehearsal = self.fresh()
        rehearsal.propose()
        rehearsal.enable_switch()
        with driver._anchor(self.sandbox, rehearsal.signers):
            self.assertEqual(public_packets.switch_state(rehearsal.project), 'ON')
        with self.assertRaises(public_packets.PacketError) as refused:
            rehearsal.project_approved_change()
        self.assertIs(type(refused.exception), public_packets.PacketError)
        self.assertEqual(refused.exception.code, 'packet_not_approved')
        self.assertFalse((rehearsal.project / 'site-changes').exists())
        self.assertFalse((rehearsal.root / 'proposals').exists())
        self.assertEqual(list(rehearsal.site.iterdir()), [rehearsal.baseline])

    def test_c2_signed_packet_does_not_approve_publisher_proposal(self):
        """C2: a real verified change alone must not authorize the separate publisher."""
        rehearsal = self.fresh()
        rehearsal.propose()
        rehearsal.approve_packet()
        change = rehearsal.project_approved_change()
        self.assertEqual(load(change)['schema'], 'records-site-change-v1')
        with self.assertRaises(publish.PublishError) as refused:
            rehearsal.stage_release()
        self.assertIs(type(refused.exception), publish.PublishError)
        self.assertEqual(refused.exception.args, ('owner_approval_required',))
        self.assertEqual(list((rehearsal.site / 'releases').iterdir()), [])
        self.assertFalse((rehearsal.site / 'current.json').exists())

    def test_c1_wrong_signed_bytes_fail_closed_at_cli(self):
        """C1: a genuine signature over other bytes cannot approve this packet."""
        real_sign = driver._sign

        def wrong_packet(sandbox, payload):
            proposed = sandbox / 'proposed.json'
            if proposed.exists() and payload == public_packets.payload(sandbox / 'project', load(proposed)['id']):
                payload += b'other bytes'
            return real_sign(sandbox, payload)

        out = io.StringIO()
        with patch.object(driver, '_sign', side_effect=wrong_packet), redirect_stdout(out):
            self.assertEqual(driver.main(['--sandbox', str(self.sandbox)]), 1)
        self.assertEqual(json.loads(out.getvalue()), self.expected('approve_signed'))
        self.assertFalse((self.sandbox / 'project/site-changes').exists())
        self.assertFalse((self.sandbox / 'site/releases').exists())

    def test_c1_step_failures_stop_the_receipt(self):
        """C1: do not certify missing setup, rejected writes or a failed site readback."""
        scenarios = ('propose', 'stage_release', 'staged_files', 'rollback', 'verify_rollback')
        for step in scenarios:
            with self.subTest(step=step):
                self.sandbox = Path(self.temp).resolve() / step
                self.sandbox.mkdir(mode=0o700)
                with self.inject_failure(step):
                    self.assertEqual(driver.run(self.sandbox), self.expected(step))

    @staticmethod
    def inject_failure(step):
        rehearsal = driver._Rehearsal
        if step == 'propose':
            return patch.object(driver.rehearse_anchor, 'make_signer', side_effect=OSError('synthetic failure'))
        original = getattr(rehearsal, step)

        def damaged(state):
            if step == 'stage_release':
                # Change neither the reported approved file nor its digest: send the
                # actual publisher an unapproved proposal id and require a hard failure.
                state.proposal = 'prop_absent'
            elif step == 'staged_files':
                (state.site / 'content' / (state.proposal + '.md')).write_bytes(b'wrong active bytes')
            elif step == 'rollback':
                (state.site / 'current.json').write_bytes(b'wrong pointer')
            else:
                state.baseline.write_bytes(b'not restored')
            return original(state)

        return patch.object(rehearsal, step, damaged)

    def test_c3_only_local_ssh_and_no_network_or_mail(self):
        """C3: deny unexpected child tools and outbound transports during an actual run."""
        self.fresh()
        real_run = subprocess.run
        marker = self.sandbox / run_stages.SANDBOX_MARKER

        def ssh_only(argv, *args, **kwargs):
            self.assertEqual(argv[0], str(approval.SSH_KEYGEN))
            self.assertEqual(load(marker), {'sandbox_marker_version': 1})
            return real_run(argv, *args, **kwargs)

        with patch.object(subprocess, 'run', side_effect=ssh_only) as ssh, \
                patch.object(socket, 'socket', side_effect=AssertionError('network attempted')) as net, \
                patch.object(smtplib, 'SMTP', side_effect=AssertionError('mail attempted')) as mail:
            report = driver.run(self.sandbox)
        self.assertGreater(ssh.call_count, 0)
        net.assert_not_called()
        mail.assert_not_called()
        self.assertEqual(report, self.expected())
        self.assertEqual(list((self.sandbox / 'site').glob('.git')), [])

    def test_c3_unsafe_or_nonempty_sandboxes_refused(self):
        """C3: refuse reuse, writable modes and aliases without touching an existing site."""
        target = self.sandbox.parent / 'occupied'
        target.mkdir(mode=0o700)
        sentinel = target / 'index.txt'
        sentinel.write_bytes(b'keep')
        loose = target.parent / 'loose'
        loose.mkdir()
        loose.chmod(0o755)
        self.sandbox.symlink_to(target, target_is_directory=True)
        for path, reason in ((target, 'sandbox must be empty'), (loose, 'sandbox must be an owner-only 0700 directory'),
                             (self.sandbox, 'unsafe sandbox path')):
            with self.subTest(path=path.name):
                with self.assertRaises(ValueError) as refused:
                    driver.run(path)
                self.assertIs(type(refused.exception), ValueError)
                self.assertEqual(refused.exception.args, (reason,))
                with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as cli:
                    driver.main(['--sandbox', str(path)])
                self.assertEqual(cli.exception.code, 2)
        self.assertEqual({p.name: p.read_bytes() for p in target.iterdir()}, {'index.txt': b'keep'})
        self.assertEqual(list(loose.iterdir()), [])
