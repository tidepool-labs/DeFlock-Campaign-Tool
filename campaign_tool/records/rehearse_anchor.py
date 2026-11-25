"""Stage 20: rehearse SSH approval and anchor readiness using synthetic inputs only.

Run on the host in a dedicated process, never alongside live approval work.
"""
import argparse
import hashlib
import json
import os
import subprocess
import tempfile
from functools import partial
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from .. import approval
from . import public_packets, rehearsal_trust, run_stages

STAGES = ('20',)
TITLE = 'Approval trust anchor'
_PRINCIPAL = 'stage20-rehearsal'
_SIGNERS = 'rehearsal.allowed_signers'
_MISSING_CODE = 'APPROVAL_ANCHOR_UNAVAILABLE'


class _Failed(Exception):
    """A fixed, content-free rehearsal failure code."""


def _write_private(path, text):
    with os.fdopen(os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600), 'w', encoding='utf-8') as out:
        out.write(text)
    return path


def _ssh(sandbox, *args, data=b''):
    # Apply the real verifier binary checks before using it for fixture generation too.
    approval._require_root(approval.SSH_KEYGEN)
    return subprocess.run([str(approval.SSH_KEYGEN), *args], input=data, cwd=sandbox,
                          env={'HOME': str(sandbox), 'LC_ALL': 'C'}, capture_output=True,
                          check=True, timeout=30).stdout


def _new_key(sandbox, key):
    _ssh(sandbox, '-q', '-t', 'ed25519', '-N', '', '-C', _PRINCIPAL, '-f', str(key))
    return key.with_suffix('.pub')


def make_signer(sandbox):
    pub = _new_key(sandbox, sandbox / 'trusted')
    kind, public_key, _comment = pub.read_text(encoding='ascii').split(maxsplit=2)
    return (_write_private(sandbox / _SIGNERS,
                           f'{_PRINCIPAL} namespaces="deflock-approval" {kind} {public_key}\n'),)


def _verify(sandbox, key):
    data = approval.payload('rehearsal', 'stage-20', 'Synthetic approval rehearsal. No action is authorized.\n')
    signature = _ssh(sandbox, '-Y', 'sign', '-n', 'deflock-approval', '-f', str(key), data=data).decode('ascii')
    # Redirect only approval.verify's workspace; leave the global tempfile cache alone.
    workspace = SimpleNamespace(TemporaryDirectory=partial(tempfile.TemporaryDirectory, dir=sandbox))
    with rehearsal_trust.sandbox_anchor(sandbox, sandbox / _SIGNERS), \
            patch.object(approval, 'tempfile', workspace), patch.dict(os.environ, HOME=str(sandbox)):
        return approval.verify(data, signature, namespace='deflock-approval')


def approve_signed(sandbox):
    if _verify(sandbox, sandbox / 'trusted') != _PRINCIPAL:
        raise _Failed('UNEXPECTED_APPROVAL_PRINCIPAL')
    return (_write_private(sandbox / 'verified-principal.txt', _PRINCIPAL + '\n'),)


def approve_wrong_key(sandbox):
    key = sandbox / 'wrong'
    _new_key(sandbox, key)
    try:
        _verify(sandbox, key)
    except approval.TrustUnavailable:
        raise  # An unavailable verifier is not proof that the wrong key was refused.
    except approval.ApprovalError:
        return (_write_private(sandbox / 'wrong-key.txt', 'WRONG_KEY_REFUSED\n'),)
    raise _Failed('WRONG_KEY_ACCEPTED')


def anchor_missing(sandbox):
    with rehearsal_trust.sandbox_anchor(sandbox, sandbox / 'absent.allowed_signers'):
        try:
            public_packets._anchor_ready()
        except public_packets.PacketError as exc:
            if exc.code == _MISSING_CODE:
                return (_write_private(sandbox / 'missing-anchor.txt', exc.code + '\n'),)
    raise _Failed('MISSING_ANCHOR_NOT_REPORTED')


def _empty_sandbox(path):
    rehearsal_trust.require_private(path)
    if any(path.iterdir()):
        raise ValueError('sandbox must be empty')


def run(sandbox: Path):
    """Rehearse in a caller-created empty 0700 directory; report only codes and hashes."""
    sandbox = rehearsal_trust.sandbox_path(sandbox)
    _empty_sandbox(sandbox)
    _write_private(sandbox / run_stages.SANDBOX_MARKER, '{"sandbox_marker_version":1}\n')
    result = {'stages': list(STAGES), 'steps': [], 'manual_steps': [], 'codes': []}
    for name, step in (('make_signer', make_signer), ('approve_signed', approve_signed),
                       ('approve_wrong_key', approve_wrong_key), ('anchor_missing', anchor_missing)):
        code, outputs = 'OK', []
        try:
            outputs = [{'path': path.relative_to(sandbox).as_posix(),
                        'sha256': hashlib.sha256(path.read_bytes()).hexdigest()} for path in step(sandbox)]
        except _Failed as exc:
            code = str(exc)
        except approval.TrustUnavailable:
            code = _MISSING_CODE
        except approval.ApprovalError:
            code = 'SIGNED_APPROVAL_REJECTED'
        except (OSError, ValueError, subprocess.SubprocessError):
            code = name.upper() + '_FAILED'
        result['steps'].append({'name': name, 'code': code, 'outputs': outputs})
        if code != 'OK':
            result['codes'].append(code)
            break
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
