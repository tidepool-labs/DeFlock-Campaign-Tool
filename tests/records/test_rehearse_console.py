"""Runtime coverage for the A3 console rehearsal."""

import errno
import hashlib
import http.client
import io
import json
import socket
import stat
import tempfile
import threading
import unittest
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from datetime import datetime, timedelta
from pathlib import Path
from threading import current_thread
from unittest.mock import patch

from campaign_tool import approval
from campaign_tool.records import admin_panel, console_access, console_check, console_export, rehearse_console, run_stages


PAGES = {
    "/runs": b"Run history", "/documents": b"Documents and findings", "/findings": b"All findings",
    "/drafts": b"Draft history", "/decisions": b"Decisions", "/health": b"System health", "/stages": b"Go-live stages",
    "/golive": b"Go-live",
}
GOLIVE_NOTICE = b"Go-live evidence is not configured on this console."


class RehearseConsoleTests(unittest.TestCase):
    def setUp(self):
        self.sandbox = Path(self.enterContext(tempfile.TemporaryDirectory(dir=Path.cwd()))).resolve()

    @contextmanager
    def listen(self, entered=None):
        build = admin_panel.build_server
        self.servers = []
        self.workers = []

        def capture(**kwargs):
            self.assertEqual(kwargs["port"], 0)
            server = build(**kwargs)
            self.addCleanup(server.server_close)
            self.servers.append(server)
            self.assertEqual(server.server_address, ("127.0.0.1", server.server_port))
            self.assertGreater(server.server_port, 0)
            serve = server.serve_forever

            def record_worker(poll_interval=0.5):
                self.workers.append(current_thread())
                if entered is not None:
                    entered.set()
                serve(poll_interval=poll_interval)

            server.serve_forever = record_worker
            return server

        with patch.object(admin_panel, "build_server", side_effect=capture):
            yield

    def assert_stopped(self, started=True):
        server, = self.servers
        self.assertEqual(server.socket.fileno(), -1)
        with socket.socket() as probe:
            probe.settimeout(1)
            self.assertEqual(probe.connect_ex(("127.0.0.1", server.server_port)), errno.ECONNREFUSED)
        self.assertEqual(len(self.workers), int(started))
        self.assertFalse(any(worker.is_alive() for worker in self.workers))

    def output(self, path):
        return {"path": path.relative_to(self.sandbox).as_posix(), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}

    def receipt(self, route):
        return self.sandbox / (route[1:] + "-http.json")

    def export_step(self):
        return ("build_export", "OK", [self.output((self.sandbox / "console/current").resolve() / "EXPORT.json")])

    def expected(self, *rows):
        return {"stages": ["A3"], "steps": [{"name": n, "code": c, "outputs": o} for n, c, o in rows],
                "manual_steps": [], "codes": [c for _, c, _ in rows if c != "OK"]}

    def test_c1_build_export(self):
        """C1: The fixture installs a fresh export without consulting the host anchor."""
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as tmp:
            sandbox = Path(tmp).resolve()
            marker = sandbox / run_stages.SANDBOX_MARKER
            build = console_export.build

            def after_marker(*args):
                self.assertEqual(marker.read_bytes(), b'{"sandbox_marker_version":1}\n')
                self.assertEqual(stat.S_IMODE(marker.stat().st_mode), 0o600)
                return build(*args)

            # Detect an unmarked build, a live trust probe or a fixture that
            # writes a manifest but never makes it readable by the console.
            with patch.object(console_export, "build", side_effect=after_marker), \
                    patch.object(approval, "anchor_status", side_effect=AssertionError("host approval probe")):
                root, manifest = rehearse_console._build_export(sandbox)
            self.assertEqual(manifest, (root / "current").resolve() / "EXPORT.json")
            self.assertEqual(json.loads(manifest.read_text())["approval_anchor"],
                             {"state": "unavailable", "reason": "synthetic_rehearsal"})
            self.assertEqual(console_check._export_check(root, 36), "PASS")

    def test_c1_c2_c3_real_http_and_cli_success(self):
        """C1: Missing or misrouted pages fail the real HTTP rehearsal.

        C2: Compare every digest with the bytes read from the wire.
        C3: Fail on a non-loopback Host or any Access identity lookup.
        """
        client_responses = []
        request_headers = []
        getresponse = http.client.HTTPConnection.getresponse
        begin = admin_panel._Handler._begin

        def record_response(connection):
            response = getresponse(connection)
            read = response.read

            def record_read(amt=None):
                body = read(amt)
                client_responses.append((response.status, body))
                return body

            response.read = record_read
            return response

        def record_headers(handler):
            request_headers.append((handler.command, handler.path, handler.headers.get_all("Host")))
            return begin(handler)

        stdout = io.StringIO()
        relative = self.sandbox.relative_to(Path.cwd().resolve())
        with self.listen(), redirect_stdout(stdout), patch.object(http.client.HTTPConnection, "getresponse", record_response), \
                patch.object(admin_panel._Handler, "_begin", record_headers), \
                patch.object(console_access, "identity", side_effect=AssertionError("Access was exercised")) as identity:
            self.assertEqual(rehearse_console.main(["--sandbox", str(relative)]), 0)
        identity.assert_not_called()
        self.assert_stopped()
        routes = [*PAGES, "/send"]
        host = f"127.0.0.1:{self.servers[0].server_port}"
        self.assertEqual(request_headers, [("GET", route, [host]) for route in routes])
        self.assertEqual(len(client_responses), len(routes))
        for route, (status, body) in zip(routes, client_responses):
            with self.subTest(route=route):
                self.assertEqual(status, 200 if route in PAGES else 404)
                if route in PAGES:
                    self.assertIn(b"<h1>" + PAGES[route] + b"</h1>", body)
                if route == "/golive":
                    self.assertIn(GOLIVE_NOTICE, body)
                self.assertEqual(json.loads(self.receipt(route).read_bytes()), {
                    "route": route, "status": status, "body_sha256": hashlib.sha256(body).hexdigest(),
                })
        self.assertEqual(json.loads(stdout.getvalue()), self.expected(
            self.export_step(), ("start_console", "OK", []),
            ("pages", "OK", [self.output(self.receipt(route)) for route in PAGES]),
            ("send_disabled", "OK", [self.output(self.receipt("/send"))]),
            ("console_check", "OK", []), ("stop", "OK", []),
        ))

    def test_c2_page_failures_close_server(self):
        """C2: A failed page probe hashes any body received, stops probing, and closes its real listener."""
        get = admin_panel._Handler.do_GET
        error = b"Synthetic error body"
        # A dropped route or a fabricated live count must not certify the ledger page.
        for route, status, body, code in (
            ("/documents", 503, error, "PAGE_STATUS_UNEXPECTED"),
            ("/documents", 200, error, "PAGE_HEADING_MISSING"),
            ("/documents", None, b"", "PAGE_PROBE_FAILED"),
            ("/golive", 404, error, "PAGE_STATUS_UNEXPECTED"),
            ("/golive", 200, b"<h1>Go-live stages</h1><p>" + GOLIVE_NOTICE + b"</p>", "PAGE_HEADING_MISSING"),
            ("/golive", 200, b"<h1>Go-live</h1><p>0 of 22 live</p>", "GOLIVE_NOTICE_MISSING"),
        ):
            with self.subTest(route=route, code=code), tempfile.TemporaryDirectory(dir=self.sandbox) as tmp:
                original = self.sandbox
                self.sandbox = Path(tmp)
                seen = []

                def respond(handler):
                    seen.append(handler.path)
                    if handler.path == route:
                        if status is not None:
                            handler._send(status, body.decode())
                    else:
                        get(handler)

                try:
                    with self.listen(), patch.object(admin_panel._Handler, "do_GET", respond):
                        result = rehearse_console.run(self.sandbox)
                    self.assert_stopped()
                    self.assertEqual(result["codes"], [code])
                    prior = list(PAGES)[:list(PAGES).index(route)]
                    self.assertEqual(seen, [*prior, route])
                    outputs = [self.output(self.receipt(page)) for page in prior]
                    if status is not None:
                        self.assertEqual(json.loads(self.receipt(route).read_bytes()), {
                            "route": route, "status": status,
                            "body_sha256": hashlib.sha256(body).hexdigest(),
                        })
                        outputs.append(self.output(self.receipt(route)))
                    self.assertEqual(result, self.expected(self.export_step(), ("start_console", "OK", []),
                                                           ("pages", code, outputs)))
                finally:
                    self.sandbox = original

    def test_c2_send_failures_close_server(self):
        """C2: Send exposure or a dropped send probe cannot pass or leave the console running."""
        get = admin_panel._Handler.do_GET
        for status, code in ((200, "SEND_NOT_DISABLED"), (None, "SEND_PROBE_FAILED")):
            with self.subTest(code=code), tempfile.TemporaryDirectory(dir=self.sandbox) as tmp:
                original = self.sandbox
                self.sandbox = Path(tmp)

                def respond(handler):
                    if handler.path != "/send":
                        get(handler)
                    elif status is not None:
                        handler._send(status, "Synthetic send exposure")

                try:
                    with self.listen(), patch.object(admin_panel._Handler, "do_GET", respond), \
                            patch.object(console_check, "_export_check") as freshness:
                        result = rehearse_console.run(self.sandbox)
                    freshness.assert_not_called()
                    self.assert_stopped()
                    outputs = []
                    if status is not None:
                        self.assertEqual(json.loads(self.receipt("/send").read_bytes()), {
                            "route": "/send", "status": status,
                            "body_sha256": hashlib.sha256(b"Synthetic send exposure").hexdigest(),
                        })
                        outputs.append(self.output(self.receipt("/send")))
                    self.assertEqual(result, self.expected(*self.through_pages(), ("send_disabled", code, outputs)))
                finally:
                    self.sandbox = original

    def through_pages(self):
        return (self.export_step(), ("start_console", "OK", []),
                ("pages", "OK", [self.output(self.receipt(route)) for route in PAGES]))

    def test_c1_stale_export_is_not_fresh(self):
        """C1: Successful pages do not hide staleness in the real check; cleanup still runs."""
        class Later(datetime):
            @classmethod
            def now(cls, tz=None):
                return datetime.now(tz) + timedelta(days=3)

        stdout = io.StringIO()
        with self.listen(), redirect_stdout(stdout), patch.object(console_check, "datetime", Later):
            self.assertEqual(rehearse_console.main(["--sandbox", str(self.sandbox / "new")]), 1)
        self.sandbox /= "new"
        self.assert_stopped()
        self.assertEqual(json.loads(stdout.getvalue()), self.expected(
            *self.through_pages(), ("send_disabled", "OK", [self.output(self.receipt("/send"))]),
            ("console_check", "EXPORT_STALE", []),
        ))

    def test_c2_interrupt_after_start_closes_listener(self):
        """C2: An interrupt, not just a classified HTTP error, releases the listener and worker."""
        with self.listen(), patch.object(http.client.HTTPConnection, "request", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt) as raised:
                rehearse_console.run(self.sandbox)
        self.assertIs(type(raised.exception), KeyboardInterrupt)
        self.assert_stopped()
        self.assertFalse(any(w in threading.enumerate() for w in self.workers))

    def test_c2_start_failure_closes_bound_listener(self):
        """C2: A thread start failure leaves no socket open despite an already successful bind."""
        with self.listen(), patch.object(rehearse_console.Thread, "start", side_effect=RuntimeError):
            result = rehearse_console.run(self.sandbox)
        self.assert_stopped(started=False)
        self.assertEqual(result, self.expected(self.export_step(), ("start_console", "CONSOLE_START_FAILED", [])))

    def test_c2_interrupt_during_thread_start_stops_started_worker(self):
        """C2: An interrupt raised by Thread.start() after the worker began still stops it before the socket closes."""
        real_start = rehearse_console.Thread.start
        entered = threading.Event()

        def start_then_interrupt(thread):
            real_start(thread)
            # Thread.start() does not guarantee the target ran; wait until the worker is in serve_forever.
            assert entered.wait(5), "worker never entered serve_forever"
            raise KeyboardInterrupt

        with self.listen(entered), patch.object(rehearse_console.Thread, "start", start_then_interrupt):
            with self.assertRaises(KeyboardInterrupt):
                rehearse_console.run(self.sandbox)
        self.assertEqual(len(self.workers), 1)
        self.assertFalse(self.workers[0].is_alive())
        self.assert_stopped()

    def test_c2_interrupt_before_worker_bootstraps_never_serves(self):
        """C2: An interrupt after the native launch but before the worker runs or has an ident must leave
        no server running; cleanup may not depend on Thread.ident (a worker that sees `stopped` never serves)."""
        release = threading.Event()
        launched = []
        real_thread = rehearse_console.Thread

        class LateWorker(real_thread):
            def __init__(self, *args, target=None, **kwargs):
                def held(*a, **kw):
                    release.wait(10)
                    target(*a, **kw)
                super().__init__(*args, target=held, **kwargs)
                launched.append(self)

            @property
            def ident(self):  # still "bootstrapping": no ident visible to cleanup
                return None if not release.is_set() else super().ident

            def start(self):
                super().start()
                raise KeyboardInterrupt

        with self.listen(), patch.object(rehearse_console, "Thread", LateWorker):
            with self.assertRaises(KeyboardInterrupt):
                rehearse_console.run(self.sandbox)
            worker, = launched
            release.set()
            worker.join(5)
            self.assertFalse(worker.is_alive(), "worker still running after the listener closed")
            self.assertEqual(self.workers, [], "worker entered serve_forever after cleanup")
            self.assert_stopped(started=False)

    def test_c2_failure_before_start_leaves_no_thread(self):
        """C2: An exception before the worker starts leaves no thread and a closed listener."""
        before = set(threading.enumerate())
        with self.listen(), patch.object(rehearse_console.Thread, "start", side_effect=RuntimeError):
            rehearse_console.run(self.sandbox)
        self.assertEqual(set(threading.enumerate()), before)
        self.assert_stopped(started=False)

    def test_c1_bind_failure_stops_probes(self):
        """C1: Bind failure is classified at startup, not reported as a failing HTTP page."""
        with patch.object(admin_panel, "build_server", side_effect=OSError(errno.EADDRINUSE, "synthetic")):
            result = rehearse_console.run(self.sandbox)
        self.assertEqual(result, self.expected(self.export_step(), ("start_console", "CONSOLE_START_FAILED", [])))

    def test_c1_export_failure_never_binds(self):
        """C1: An export refusal cannot start the console on partial input."""
        with patch.object(console_export, "build", side_effect=console_export.ExportError("SYNTHETIC")), \
                patch.object(admin_panel, "build_server") as build:
            result = rehearse_console.run(self.sandbox)
        build.assert_not_called()
        self.assertEqual(result, self.expected(("build_export", "EXPORT_BUILD_FAILED", [])))

    def test_c1_stop_requires_connection_refusal(self):
        """C1: A listener that still accepts connections cannot produce a passing stop step."""
        with self.listen(), patch.object(socket.socket, "connect_ex", return_value=0):
            result = rehearse_console.run(self.sandbox)
        self.assert_stopped()
        self.assertEqual(result, self.expected(*self.through_pages(),
            ("send_disabled", "OK", [self.output(self.receipt("/send"))]), ("console_check", "OK", []),
            ("stop", "CONSOLE_PORT_NOT_CLOSED", []),
        ))

    def test_c1_invalid_cli_sandbox_is_refused(self):
        """C1: Missing flags, files, symlinks, nonempty and nonprivate directories do not enter the driver."""
        nonempty = self.sandbox / "nonempty"
        nonempty.mkdir(mode=0o700)
        file = nonempty / "keep"
        file.write_bytes(b"unchanged")
        unsafe = self.sandbox / "unsafe"
        unsafe.mkdir()
        unsafe.chmod(0o755)
        empty = self.sandbox / "empty"
        empty.mkdir(mode=0o700)
        link = self.sandbox / "link"
        link.symlink_to(empty, target_is_directory=True)
        for argv in ([], *(["--sandbox", str(path)] for path in (nonempty, unsafe, file, link))):
            with self.subTest(argv=argv), redirect_stderr(io.StringIO()), patch.object(rehearse_console, "run") as run:
                with self.assertRaises(SystemExit) as raised:
                    rehearse_console.main(argv)
                self.assertIs(type(raised.exception), SystemExit)
                self.assertEqual(raised.exception.code, 2)
                run.assert_not_called()
        self.assertEqual(file.read_bytes(), b"unchanged")
        self.assertEqual(list(empty.iterdir()), [])

    def test_c1_direct_run_refuses_invalid_sandbox(self):
        """C1: The caller-facing run API refuses occupied or nonexistent sandboxes without writing."""
        file = self.sandbox / "keep"
        file.write_bytes(b"unchanged")
        for path in (self.sandbox, self.sandbox / "absent"):
            with self.subTest(path=path):
                self.assertEqual(rehearse_console.run(path), self.expected(("build_export", "SANDBOX_INVALID", [])))
        self.assertEqual(list(self.sandbox.iterdir()), [file])
        self.assertEqual(file.read_bytes(), b"unchanged")

    def test_c1_cli_manual_steps_are_not_success(self):
        """C1: Even an OK step cannot turn a manual action into a successful CLI rehearsal."""
        report = {**self.expected(("build_export", "OK", [])), "manual_steps": ["synthetic manual step"]}
        stdout = io.StringIO()
        with redirect_stdout(stdout), patch.object(rehearse_console, "run", return_value=report):
            self.assertEqual(rehearse_console.main(["--sandbox", str(self.sandbox)]), 1)
        self.assertEqual(json.loads(stdout.getvalue()), report)
