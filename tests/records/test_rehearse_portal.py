"""Runtime contract for stage-4 synthetic rehearsal."""

import builtins
import hashlib
import io
import json
import os
import socket
import sqlite3
import stat
import tempfile
import unittest
from contextlib import ExitStack, closing, redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch
from urllib.parse import unquote, urlsplit

from campaign_tool.records import rehearse, rehearse_portal as driver, run_stages
from campaign_tool.records.portal import download, policy


NAMES = ("start_stub", "policy", "fetch", "ledger", "off_list_refused",
         "redirect_refused", "stop_stub")
DOCUMENTS = (b"Synthetic portal document one.\n", b"Synthetic portal document two.\n")
DIGESTS = sorted(hashlib.sha256(body).hexdigest() for body in DOCUMENTS)


def report(sandbox, *, failed=None):
    paths = {
        "start_stub": [run_stages.SANDBOX_MARKER],
        "fetch": [f"portal-queue/objects/{digest}" for digest in DIGESTS],
        "ledger": ["processing-v1/ledger.sqlite",
                   *(f"processing-v1/intake/blobs/{digest}" for digest in DIGESTS)],
        "stop_stub": ["portal-queue/portal.sqlite"],
    }
    steps, codes = [], []
    for name in NAMES:
        if name == failed:
            codes.append(name.upper() + "_FAILED")
            steps.append({"name": name, "code": codes[0], "outputs": []})
            break
        steps.append({"name": name, "code": "OK", "outputs": [
            {"path": path, "sha256": hashlib.sha256((sandbox / path).read_bytes()).hexdigest()}
            for path in paths.get(name, ())
        ]})
    return {"stages": ["4"], "steps": steps, "manual_steps": [], "codes": codes}


class PortalRehearsalTests(unittest.TestCase):
    def setUp(self):
        self.sandbox = self.new_sandbox()

    def new_sandbox(self):
        return Path(self.enterContext(tempfile.TemporaryDirectory(
            dir=Path(tempfile.gettempdir()).resolve())))

    def test_c1_fetch_and_refuse(self):
        """C1: complete both enrollments, persist exact refusal receipts, and stop the stub."""
        # A policy check skipped or a redirect followed changes the socket trace
        # or the persisted attempt, even if the driver returns OK anyway.
        bind, connect = socket.socket.bind, socket.socket.connect
        servers, calls = [], []

        def bound(sock, address):
            self.assertEqual(address, ("127.0.0.1", 0))
            marker = self.sandbox / run_stages.SANDBOX_MARKER
            self.assertEqual(list(self.sandbox.iterdir()), [marker])
            self.assertEqual(stat.S_IMODE(marker.stat().st_mode), 0o600)
            bind(sock, address)
            servers.append(sock)

        def dial(sock, address):
            calls.append(address)
            self.assertEqual(len(servers), 1)
            self.assertEqual(address, servers[0].getsockname())
            connect(sock, address)

        with patch.object(socket.socket, "bind", bound), patch.object(socket.socket, "connect", dial):
            actual = driver.run(self.sandbox)
        self.assertEqual(actual, report(self.sandbox))
        self.assertEqual(len(servers), 1)
        self.assertEqual(servers[0].fileno(), -1)
        self.assertEqual(len(calls), 3)  # two downloads and the initial redirect response only
        self.verify_receipts()

    def verify_receipts(self):
        database = self.sandbox / "processing-v1/ledger.sqlite"
        with closing(sqlite3.connect(database)) as con:
            self.assertEqual(con.execute(
                "SELECT portal_host, original_sha256, retrieval_status FROM portal_items "
                "ORDER BY original_sha256").fetchall(), [
                    ("127.0.0.1", digest, "bytes_verified_stage_pending") for digest in DIGESTS])
            self.assertEqual(con.execute("SELECT status, count(*) FROM stage_state GROUP BY status").fetchall(),
                             [("pending", 14)])
        database = self.sandbox / "portal-queue/portal.sqlite"
        with closing(sqlite3.connect(database)) as con:
            attempts = con.execute(
                "SELECT host, reason, evidence_json FROM portal_items JOIN portal_attempts "
                "ON portal_items.id = portal_attempts.item WHERE state = 'blocked' ORDER BY host").fetchall()
        self.assertEqual([(host, reason, json.loads(ev)) for host, reason, ev in attempts], [
            ("127.0.0.1", "redirect_not_approved", [
                {"host": "127.0.0.1", "path_sha256": hashlib.sha256(b"/documents/redirect").hexdigest(),
                 "http_status": 302, "content_type": "unknown", "bytes": 0},
                {"host": "off-list.invalid", "http_status": None, "content_type": None, "bytes": 0},
            ]),
            ("off-list.invalid", "portal_not_approved", []),
        ])

    def test_c2_no_host_policy_or_egress(self):
        """C2: deny file opens outside the sandbox, host policy loaders, DNS and non-loopback sockets."""
        # Count every forbidden attempt, even when a helper catches its exception.
        forbidden = []

        def deny(*args, **kwargs):
            forbidden.append(True)
            raise AssertionError("outside the synthetic rehearsal boundary")

        def guard_open(real_open):
            def guarded(path, *args, **kwargs):
                if path == ":memory:" or isinstance(path, int):
                    return real_open(path, *args, **kwargs)
                filename = os.fsdecode(path)
                if filename.startswith("file:"):
                    filename = unquote(urlsplit(filename).path)
                if not Path(filename).resolve().is_relative_to(self.sandbox):
                    deny()
                return real_open(path, *args, **kwargs)
            return guarded

        real_connect = socket.socket.connect
        real_download = driver.run_download

        def inspect_policy(*args, **kwargs):
            approval = kwargs["approval_loader"]()
            self.assertEqual(approval.portals, frozenset({"127.0.0.1"}))
            self.assertEqual(approval.redirects, frozenset())
            self.assertEqual(approval.scopes, {"127.0.0.1": ("/documents/",)})
            self.assertEqual(kwargs["egress_loader"]().hosts, frozenset({"127.0.0.1"}))
            return real_download(*args, **kwargs)

        def dial(sock, address):
            if not isinstance(address, tuple) or address[0] != "127.0.0.1":
                deny()
            return real_connect(sock, address)

        with ExitStack() as stack:
            for module, name in ((os, "open"), (io, "open"), (builtins, "open"), (sqlite3, "connect")):
                stack.enter_context(patch.object(module, name, guard_open(getattr(module, name))))
            for module, name in ((socket, "getaddrinfo"), (socket, "gethostbyaddr"),
                                 (socket.socket, "connect_ex"), (policy, "load_approval"),
                                 (download, "load_approval"), (download, "load_egress")):
                stack.enter_context(patch.object(module, name, deny))
            stack.enter_context(patch.object(socket.socket, "connect", dial))
            stack.enter_context(patch.object(driver, "run_download", side_effect=inspect_policy))
            actual = driver.run(self.sandbox)
        self.assertEqual(forbidden, [])
        self.assertEqual(actual, report(self.sandbox))

    def test_c3_cli_success_with_hashes(self):
        """C3: CLI returns the actual digest for every reported artifact, including both copies of each document."""
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(driver.main(["--sandbox", str(self.sandbox)]), 0)
        self.assertEqual(json.loads(out.getvalue()), report(self.sandbox))
        for folder in ("portal-queue/objects", "processing-v1/intake/blobs"):
            self.assertEqual({path.read_bytes() for path in (self.sandbox / folder).iterdir()}, set(DOCUMENTS))

    def test_runner_requires_byte_preserving_enrollment(self):
        # A same-length corruption of the enrolled copy leaves its filename,
        # ledger row and fetched object intact. None can prove the copied bytes.
        real_fetch = driver._Rehearsal.fetch
        real_bind, real_connect = socket.socket.bind, socket.socket.connect
        for changed_digest in (None, *DIGESTS):
            with self.subTest(changed_digest=changed_digest):
                servers = []

                def bind(sock, address):
                    self.assertEqual(address, ("127.0.0.1", 0))
                    real_bind(sock, address)
                    servers.append(sock)

                def connect(sock, address):
                    self.assertEqual(len(servers), 1)
                    self.assertEqual(address, servers[0].getsockname())
                    real_connect(sock, address)

                def fetch(rehearsal):
                    paths = real_fetch(rehearsal)
                    if changed_digest is not None:
                        blob = rehearsal.blobs / changed_digest
                        info = blob.lstat()
                        self.assertTrue(stat.S_ISREG(info.st_mode))
                        self.assertEqual(info.st_nlink, 1)
                        body = blob.read_bytes()
                        self.assertEqual(hashlib.sha256(body).hexdigest(), changed_digest)
                        mode = stat.S_IMODE(info.st_mode)
                        blob.chmod(mode | stat.S_IWUSR)
                        try:
                            blob.write_bytes(bytes([body[0] ^ 1]) + body[1:])
                        finally:
                            blob.chmod(mode)
                    return paths

                with (patch.object(driver._Rehearsal, "fetch", fetch),
                      patch.object(socket.socket, "bind", bind),
                      patch.object(socket.socket, "connect", connect)):
                    receipt, path = rehearse._run_driver(driver, self.sandbox, "1" * 40)

                self.assertEqual([sock.fileno() for sock in servers], [-1])
                healthy = changed_digest is None
                expected = report(path.parent, failed=None if healthy else "ledger")
                self.assertEqual(receipt["qualified"], healthy)
                self.assertEqual(receipt["codes"], expected["codes"])
                self.assertEqual(receipt["steps"], expected["steps"])

    def test_c2_cli_refuses_unqualified_sandboxes(self):
        """C2: CLI rejects absent, missing, nonempty, aliased or not owner-only sandboxes."""
        occupied = self.new_sandbox()
        sentinel = occupied / run_stages.SANDBOX_MARKER
        sentinel.write_bytes(b"existing")
        public = self.new_sandbox()
        public.chmod(0o750)
        alias = self.sandbox / "alias"
        alias.symlink_to(public, target_is_directory=True)
        for argv in ([], ["--sandbox", str(self.sandbox / "missing")],
                     *(["--sandbox", str(path)] for path in (occupied, public, alias))):
            with self.subTest(argv=argv), redirect_stderr(io.StringIO()), redirect_stdout(io.StringIO()) as out:
                with self.assertRaises(SystemExit) as refusal:
                    driver.main(argv)
                self.assertIs(type(refusal.exception), SystemExit)
                self.assertEqual(refusal.exception.code, 2)
                self.assertEqual(out.getvalue(), "")
        self.assertEqual(sentinel.read_bytes(), b"existing")
        self.assertEqual(list(public.iterdir()), [])

    def test_c1_failed_refusal_stops_steps_and_socket(self):
        """C1: a bypassed or misclassified refusal is a failed step, not synthetic GO evidence."""
        # Run the real engine; perturb only the report at its consumer boundary
        # to exercise both early-stop verdict checks without allowing egress.
        real = driver.run_download
        for step, call_number in (
            ("off_list_refused", 2), ("redirect_refused", 3),
        ):
            with self.subTest(step=step):
                sandbox = self.new_sandbox()
                called, servers = [], []
                original_bind = socket.socket.bind

                def bind(sock, address):
                    original_bind(sock, address)
                    servers.append(sock)

                def replay(*args, **kwargs):
                    found: dict = real(*args, **kwargs)
                    called.append(True)
                    if len(called) == call_number:
                        if step == "off_list_refused":
                            found["blocked"] = 0
                        else:
                            found["receipts"][0]["reason"] = "egress_denied"
                    return found

                out = io.StringIO()
                with (patch.object(driver, "run_download", side_effect=replay),
                      patch.object(socket.socket, "bind", bind), redirect_stdout(out)):
                    self.assertEqual(driver.main(["--sandbox", str(sandbox)]), 1)
                self.assertEqual(json.loads(out.getvalue()), report(sandbox, failed=step))
                self.assertEqual(len(called), call_number)
                self.assertEqual([sock.fileno() for sock in servers], [-1])

    def test_c2_transport_rejects_non_stub_authority(self):
        """C2: transport independently refuses an off-list host or another port, before dialing."""
        transport = driver._LoopbackTransport(32123)
        with patch.object(socket, "socket") as socket_factory:
            for url, reason in (("https://off-list.invalid/documents/a", "stub_host_mismatch"),
                                ("https://127.0.0.1:32124/documents/a", "invalid_url")):
                with self.subTest(url=url), self.assertRaises(policy.PortalError) as refusal:
                    transport.open(url, timeout=1)
                self.assertIs(type(refusal.exception), policy.PortalError)
                self.assertEqual(refusal.exception.args, (reason,))
            socket_factory.assert_not_called()
