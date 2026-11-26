"""A6: rehearse phone enrolment and journal approval in a private synthetic sandbox.

Run serially in a dedicated host process, never alongside live approvals.
"""
import argparse
import hashlib
import json
import os
import secrets
import subprocess
import tempfile
import time
from functools import partial
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from .. import approval, phone_approval
from . import approval_queue, console_apply, console_journal, console_phone, phone_enrol, rehearsal_trust, run_stages
from .rehearse_anchor import _empty_sandbox, _new_key, _write_private

STAGES = ("A6",)
TITLE = "Phone passkey enrolment and approval"
_RP_ID = "admin.example.invalid"
_PRINCIPAL = "rehearsal-phone"
_SIGNERS = "rehearsal.allowed_signers"
_PROJECT = "project"
_JOURNAL = "journal"

# The synthetic P-256 assertion approach from tests/phone_signing.py. The
# installed package excludes tests. This is only a software stand-in for a
# phone, not a production signer: scalars stay in memory for this rehearsal.
_P = 0xffffffff00000001000000000000000000000000ffffffffffffffffffffffff
_N = 0xffffffff00000000ffffffffffffffffbce6faada7179e84f3b9cac2fc632551
_G = (0x6b17d1f2e12c4247f8bce6e563a440f277037d812deb33a0f4a13945d898c296,
      0x4fe342e2fe1a7f9b8ee7eb4a7c0f9e162bce33576b315ececbb6406837bf51f5)


def _add(a, b):
    if a is None:
        return b
    if b is None:
        return a
    if a[0] == b[0] and (a[1] + b[1]) % _P == 0:
        return None
    if a == b:
        slope = (3 * a[0] * a[0] - 3) * pow(2 * a[1], -1, _P)
    else:
        slope = (b[1] - a[1]) * pow(b[0] - a[0], -1, _P)
    slope %= _P
    x = (slope * slope - a[0] - b[0]) % _P
    return x, (slope * (a[0] - x) - a[1]) % _P


def _mul(k):
    out, point = None, _G
    while k:
        if k & 1:
            out = _add(out, point)
        point = _add(point, point)
        k >>= 1
    if out is None:
        raise ValueError("invalid synthetic scalar")
    return out


def _der_int(n):
    raw = n.to_bytes((n.bit_length() + 8) // 8, "big")
    return b"\x02" + bytes([len(raw)]) + raw


def _sign(secret, data):
    # Fresh nonces and a throwaway key; nothing here imports or accepts key files.
    nonce = secrets.randbelow(_N - 1) + 1
    r = _mul(nonce)[0] % _N
    z = int.from_bytes(hashlib.sha256(data).digest(), "big")
    s = pow(nonce, -1, _N) * (z + r * secret) % _N
    body = _der_int(r) + _der_int(s)
    return b"\x30" + bytes([len(body)]) + body


def _client(kind, challenge):
    return json.dumps({"type": kind, "challenge": phone_enrol.b64url(challenge),
                       "origin": "https://" + _RP_ID, "crossOrigin": False}, separators=(",", ":")).encode()


def _auth(flags):
    return hashlib.sha256(_RP_ID.encode()).digest() + bytes([flags]) + b"\0" * 4


def _registration(secret, challenge, cred_id):
    # Fixed strict-CBOR layout from the enrolment test: fmt=none, empty attStmt,
    # and a COSE ES256 public point. Only x and y vary in the COSE_Key map.
    x, y = _mul(secret)
    cose = b"\xa5\x01\x02\x03\x26\x20\x01\x21\x58\x20" + x.to_bytes(32, "big") + b"\x22\x58\x20" + y.to_bytes(32, "big")
    auth = (_auth(phone_enrol.UP | phone_enrol.UV | phone_enrol.AT) + b"\0" * 16
            + len(cred_id).to_bytes(2, "big") + cred_id + cose)
    attestation = b"\xa3\x63fmt\x64none\x67attStmt\xa0\x68authData\x58" + bytes([len(auth)]) + auth
    return attestation, _client("webauthn.create", challenge)


def _tap(secret, row, payload):
    envelope = phone_approval.envelope(row["id"], payload, int(time.time()), secrets.token_hex(16),
                                       phone_approval.DEFAULT_TTL)
    client = _client("webauthn.get", phone_approval.challenge(envelope))
    auth = _auth(phone_enrol.UP | phone_enrol.UV)
    signature = _sign(secret, auth + hashlib.sha256(client).digest())
    return {"envelope": envelope, "authenticator_data": phone_enrol.b64url(auth),
            "client_data": phone_enrol.b64url(client), "signature": phone_enrol.b64url(signature)}


def _enrol(sandbox, secret):
    challenge, cred_id = secrets.token_bytes(32), secrets.token_bytes(20)
    attestation, client = _registration(secret, challenge, cred_id)
    name, _ = phone_enrol.store(str(sandbox), _RP_ID, challenge, cred_id, attestation, client)
    record = sandbox / name
    # The anchor readiness check also requires the ordinary approval namespace.
    # A separate synthetic public key satisfies it without widening the phone line.
    typed_pub = _new_key(sandbox, sandbox / "typed-rehearsal")
    kind, key, _ = typed_pub.read_text(encoding="ascii").split(maxsplit=2)
    line = phone_enrol.line(phone_enrol.load(record), _PRINCIPAL)
    signers = _write_private(sandbox / _SIGNERS,
                             f'typed-rehearsal namespaces="deflock-approval" {kind} {key}\n{line}\n')
    return record, signers


def _draft(project, label):
    return approval_queue.add_draft(project, item_id=label, kind="reply-extension", agency_id="example",
                                    request_id="r1", body="Synthetic draft; no action is authorized.",
                                    evidence={}, source_run_id="synthetic")


def _post(journal, project, row, payload, tap):
    """Drive the real POST handler with an in-process synthetic transport and pinned pending row."""
    ident = row["id"]
    fields = {**tap, "id": ident, "expected_revision": str(row["revision"]), "csrf_token": "rehearsal"}
    responses = []
    handler = SimpleNamespace(
        path=console_phone.ROUTE, actor="rehearsal-phone",
        pin=SimpleNamespace(source_project_id=approval_queue._project_id(project), run_id="synthetic", name="synthetic"),
        server=SimpleNamespace(decisions_dir=str(journal), csrf_token="rehearsal"),
        options={"source": SimpleNamespace(queue_rows=lambda: [row], queue_payload=lambda ident, choice: payload)},
        _body=lambda: fields, _stale=lambda ident: responses.append((409, "stale_revision")),
        _fail=lambda status, code=None, **kwargs: responses.append((status, code)),
        _send=lambda status, html: responses.append((status, None)),
    )
    # This pinned view has no outstanding edits. The real nonce check, journal
    # append and apply logic still run; no browser, export tree or server is needed.
    with patch.object(console_journal, "pending_edit", return_value=False):
        console_phone.post(handler)
    return tuple(responses)


def _apply(sandbox, project, entry):
    workspace = SimpleNamespace(TemporaryDirectory=partial(tempfile.TemporaryDirectory, dir=sandbox))
    # Redirect only the verifier's workspace, as in the stage 20 rehearsal.
    with rehearsal_trust.sandbox_anchor(sandbox, sandbox / _SIGNERS), \
            patch.object(approval, "tempfile", workspace), patch.dict(os.environ, HOME=str(sandbox)):
        outcome = console_apply.outcome(project, entry)
        rows = approval_queue.load_queue(project)
    return outcome, {row["id"]: (row["status"], row.get("approved_by")) for row in rows}[entry["target_id"]]


def run(sandbox: Path):
    """Return only relative public artifact paths, their hashes, and rehearsal codes."""
    sandbox = rehearsal_trust.sandbox_path(sandbox)
    _empty_sandbox(sandbox)
    _write_private(sandbox / run_stages.SANDBOX_MARKER, '{"sandbox_marker_version":1}\n')
    result = {"stages": list(STAGES), "steps": [], "manual_steps": [], "codes": []}

    def step(name, check):
        try:
            ok, paths, observed = check()
            outputs = [{"path": path.relative_to(sandbox).as_posix(),
                        "sha256": hashlib.sha256(path.read_bytes()).hexdigest()} for path in paths]
        except (OSError, ValueError, subprocess.SubprocessError, approval.ApprovalError,
                approval_queue.ApprovalQueueError, console_journal.JournalError):
            ok, outputs, observed = False, [], None
        code = "OK" if ok else name.upper() + "_FAILED"
        result["steps"].append({"name": name, "code": code, "outputs": outputs})
        if observed is not None or not ok:
            result["codes"].append(observed if ok else code)
        return ok

    project, journal = sandbox / _PROJECT, sandbox / _JOURNAL
    secret = secrets.randbelow(_N - 1) + 1
    row = payload = tap = None

    def enrol():
        return True, _enrol(sandbox, secret), None

    def phone_approve():
        nonlocal row, payload, tap
        project.mkdir(mode=0o700)
        journal.mkdir(mode=0o700)
        row = _draft(project, "enrolled")
        payload = approval_queue.approval_payload(project, row["id"])
        tap = _tap(secret, row, payload)
        response = _post(journal, project, row, payload, tap)
        entries = console_journal.read(journal)
        ok = response == ((200, None),) and len(entries) == 1 and entries[0]["action"] == "phone_approve" \
            and _apply(sandbox, project, entries[0]) == ("APPLIED", ("approved", _PRINCIPAL))
        return ok, (), None

    def replay_refused():
        before = console_journal.read(journal)
        response = _post(journal, project, row, payload, tap)
        ok = response == ((409, "phone_replay"),) and console_journal.read(journal) == before
        return ok, (), "PHONE_REPLAY"

    def unenrolled_refused():
        other_row = _draft(project, "unenrolled")
        other_payload = approval_queue.approval_payload(project, other_row["id"])
        # A distinct key, without even a theoretical random equality with the enrolled key.
        offset = secrets.randbelow(_N - 2) + 1
        other = (secret - 1 + offset) % (_N - 1) + 1
        response = _post(journal, project, other_row, other_payload, _tap(other, other_row, other_payload))
        entries = console_journal.read(journal)
        ok = response == ((200, None),) and len(entries) == 2 and entries[1]["target_id"] == other_row["id"] \
            and _apply(sandbox, project, entries[1]) == ("SIGNATURE_REFUSED", ("pending", None))
        return ok, (journal / console_journal.FILE,) if ok else (), "SIGNATURE_REFUSED"

    for check in (enrol, phone_approve, replay_refused, unenrolled_refused):
        if not step(check.__name__, check):
            break
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sandbox", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        sandbox = rehearsal_trust.sandbox_path(args.sandbox)
        sandbox.mkdir(mode=0o700, exist_ok=True)
        _empty_sandbox(sandbox)
    except (OSError, ValueError):
        parser.error("--sandbox must be a new or empty private 0700 directory")
    result = run(sandbox)
    print(json.dumps(result))
    return int(bool(result["manual_steps"] or any(step["code"] != "OK" for step in result["steps"])))


if __name__ == "__main__":
    raise SystemExit(main())
