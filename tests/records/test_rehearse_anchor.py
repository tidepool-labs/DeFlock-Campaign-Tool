"""DFRM-10: exercise the stage 20 rehearsal with real OpenSSH and no live inputs."""
import hashlib
import io
import json
import os
import stat
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from campaign_tool import approval
from campaign_tool.records import public_packets, rehearse_anchor as driver, rehearsal_trust, run_stages


class RehearseAnchorTests(unittest.TestCase):
    def setUp(self):
        tree = self.enterContext(tempfile.TemporaryDirectory())
        self.root = Path(tree).resolve()
        self.sandbox = self.root / 'rehearsal'
        self.sandbox.mkdir(mode=0o700)
        self.enterContext(mock.patch.dict(os.environ, HOME=str(self.sandbox)))
        self.enterContext(mock.patch('socket.socket', side_effect=AssertionError('unexpected network')))
        self.real_run = subprocess.run

    def test_c1_c2_c3_real_rehearsal(self):
        # Catch skipped verification, a wrong namespace, any misplaced verifier scratch,
        # non-marker first input, and reports that don't bind the output bytes.
        commands = []
        tempdir = tempfile.TemporaryDirectory

        def ssh(argv, **kwargs):
            if not commands:
                self.assertEqual(list(self.sandbox.iterdir()), [self.sandbox / run_stages.SANDBOX_MARKER])
            self.assertEqual(argv[0], str(approval.SSH_KEYGEN))
            self.assertIn(argv[1:3], [['-q', '-t'], ['-Y', 'sign'], ['-Y', 'find-principals'], ['-Y', 'verify']])
            self.assertEqual(kwargs.get('env', os.environ)['HOME'], str(self.sandbox))
            for flag in {'-f', '-s'} & set(argv):
                self.assertTrue(Path(argv[argv.index(flag) + 1]).is_relative_to(self.sandbox))
            commands.append(tuple(argv[1:3]))
            return self.real_run(argv, **kwargs)

        def scratch(*args, **kwargs):
            self.assertEqual(kwargs.get('dir'), self.sandbox)
            return tempdir(*args, **kwargs)

        with mock.patch.object(subprocess, 'run', side_effect=ssh), \
                mock.patch.object(tempfile, 'TemporaryDirectory', side_effect=scratch) as workspaces, \
                mock.patch.object(public_packets, '_anchor_ready', wraps=public_packets._anchor_ready) as ready, \
                redirect_stdout(io.StringIO()) as stdout, redirect_stderr(io.StringIO()) as stderr:
            result = driver.run(self.sandbox)
        self.assertEqual(stdout.getvalue() + stderr.getvalue(), '')
        self.assertEqual(commands, [('-q', '-t'), ('-Y', 'sign'), ('-Y', 'find-principals'), ('-Y', 'verify'),
                                    ('-q', '-t'), ('-Y', 'sign'), ('-Y', 'find-principals')])
        self.assertEqual(workspaces.call_count, 2)
        ready.assert_called_once_with()
        self.assertEqual(driver.STAGES, ('20',))
        self.assertTrue(isinstance(driver.TITLE, str) and driver.TITLE)
        self.assertEqual(set(result), {'stages', 'steps', 'manual_steps', 'codes'})
        self.assertEqual((result['stages'], result['manual_steps'], result['codes']), (['20'], [], []))
        self.assertEqual([(step['name'], step['code']) for step in result['steps']],
                         [(name, 'OK') for name in ('make_signer', 'approve_signed', 'approve_wrong_key', 'anchor_missing')])
        for step, (filename, expected) in zip(result['steps'], (
                ('rehearsal.allowed_signers', None), ('verified-principal.txt', b'stage20-rehearsal\n'),
                ('wrong-key.txt', b'WRONG_KEY_REFUSED\n'), ('missing-anchor.txt', b'APPROVAL_ANCHOR_UNAVAILABLE\n'))):
            self.assertEqual(set(step), {'name', 'code', 'outputs'})
            data = (self.sandbox / filename).read_bytes()
            self.assertEqual(step['outputs'], [{'path': filename, 'sha256': hashlib.sha256(data).hexdigest()}])
            if expected is not None:
                self.assertEqual(data, expected)
        signers = (self.sandbox / 'rehearsal.allowed_signers').read_text()
        self.assertTrue(signers.startswith('stage20-rehearsal namespaces="deflock-approval" ssh-ed25519 '))
        self.assertFalse((self.sandbox / 'absent.allowed_signers').exists())
        self.assertIsNone(run_stages.sandbox_refusal(self.sandbox))

    def test_c1_unavailable_is_not_wrong_key_rejection(self):
        # TrustUnavailable is an ApprovalError subclass: a broad catch could certify an outage.
        with mock.patch.object(driver, '_new_key'), \
                mock.patch.object(driver, '_verify', side_effect=approval.TrustUnavailable):
            with self.assertRaises(approval.TrustUnavailable):
                driver.approve_wrong_key(self.sandbox)
        self.assertFalse((self.sandbox / 'wrong-key.txt').exists())

    def test_c3_cli_error_and_fail_fast(self):
        # A failed generation must stop before any signing attempt, and never echo diagnostics.
        error = subprocess.CalledProcessError(1, ['ssh-keygen'], stderr=b'SYNTHETIC-SIGNATURE-CONTENT')
        with mock.patch.object(subprocess, 'run', side_effect=error) as ssh, \
                redirect_stdout(io.StringIO()) as stdout, redirect_stderr(io.StringIO()) as stderr:
            self.assertEqual(driver.main(['--sandbox', str(self.sandbox)]), 1)
            ssh.assert_called_once()
            with self.assertRaises(SystemExit) as refused:
                driver.main(['--sandbox', str(self.sandbox)])
            self.assertEqual(refused.exception.code, 2)
            ssh.assert_called_once()
        result = json.loads(stdout.getvalue())
        self.assertEqual(result['steps'], [{'name': 'make_signer', 'code': 'MAKE_SIGNER_FAILED', 'outputs': []}])
        self.assertEqual(result['codes'], ['MAKE_SIGNER_FAILED'])
        self.assertNotIn('SYNTHETIC-SIGNATURE-CONTENT', stdout.getvalue() + stderr.getvalue())

    def test_c3_cli_creates_sandbox_and_requires_no_manual_steps(self):
        # A CLI that doesn't create the directory, or ignores manual steps, misreports status.
        sandbox = self.sandbox / 'new'
        with redirect_stdout(io.StringIO()) as stdout:
            self.assertEqual(driver.main(['--sandbox', str(sandbox)]), 0)
        self.assertEqual(stat.S_IMODE(sandbox.stat().st_mode), 0o700)
        result = json.loads(stdout.getvalue())
        self.assertEqual(len(result['steps']), 4)
        manual_result = {**result, 'manual_steps': ['SYNTHETIC_MANUAL_STEP']}
        with mock.patch.object(driver, 'run', return_value=manual_result), redirect_stdout(io.StringIO()):
            self.assertEqual(driver.main(['--sandbox', str(self.sandbox / 'manual')]), 1)

    def _mark(self):
        (self.sandbox / run_stages.SANDBOX_MARKER).touch(mode=0o600)

    def test_c4_requires_marker_and_containment(self):
        # Neither a symlinked marker, an escape nor a prefix-only sibling admits a signer.
        signer = self.sandbox / 'signers'
        marker = self.sandbox / run_stages.SANDBOX_MARKER
        outside = self.root / 'unrelated'
        outside.touch(mode=0o600)
        with self.assertRaises(ValueError), rehearsal_trust.sandbox_anchor(self.sandbox, signer):
            self.fail('unmarked sandbox accepted')
        marker.symlink_to(outside)
        with self.assertRaises(ValueError), rehearsal_trust.sandbox_anchor(self.sandbox, signer):
            self.fail('symlinked marker accepted')
        marker.unlink()
        self._mark()
        signer.symlink_to(outside)
        for path in (signer, self.sandbox / '..' / 'unrelated', self.root / 'rehearsal-other' / 'signers'):
            with self.subTest(path=path), self.assertRaises(ValueError), \
                    rehearsal_trust.sandbox_anchor(self.sandbox, path):
                self.fail('outside signer accepted')

    def test_c4_refuses_live_anchor_path_before_inspection(self):
        # Linux treats a double leading slash as an alias of the same live tree.
        with mock.patch.object(rehearsal_trust, '_has_symlink', side_effect=AssertionError('live path inspected')):
            for path in ('/etc/deflock', '//etc/deflock', '/etc/deflock/sandbox'):
                with self.subTest(path=path), self.assertRaises(ValueError):
                    rehearsal_trust.sandbox_path(path)

    def test_c4_exact_exception_delegation_and_restoration(self):
        # A broad path exception or leaked globals would weaken the production checks.
        self._mark()
        signer, other = self.sandbox / 'signers', self.sandbox / 'other'
        for path in (signer, other):
            path.write_text('stage20-rehearsal namespaces="deflock-approval"')
        original = approval.TRUSTED_SIGNERS, approval._require_root
        with self.assertRaisesRegex(RuntimeError, 'unwind rehearsal'):
            with rehearsal_trust.sandbox_anchor(self.sandbox, signer):
                self.assertEqual(approval.anchor_status(), {'state': 'ok'})
                with self.assertRaises(approval.TrustUnavailable):
                    approval._require_root(other)
                raise RuntimeError('unwind rehearsal')
        self.assertEqual((approval.TRUSTED_SIGNERS, approval._require_root), original)
        # On root-run CI, model the non-root signer UID; use the real production predicate.
        lstat = os.lstat

        def nonroot_signer(path, *args, **kwargs):
            info = list(lstat(path, *args, **kwargs))
            if Path(path) in (signer, other):
                info[stat.ST_UID] = 1000
            return os.stat_result(info)

        with mock.patch.object(approval, 'TRUSTED_SIGNERS', signer), mock.patch.object(os, 'lstat', nonroot_signer):
            with self.assertRaisesRegex(approval.TrustUnavailable, 'not root-owned and protected'):
                approval.anchor_status()
