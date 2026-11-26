"""Offline regressions for registered page coverage at the anonymous edge."""
import io
import unittest
from types import SimpleNamespace
from unittest import mock

from campaign_tool.records import console_edge_smoke as smoke, console_pages


class RegisteredPageSweepTests(unittest.TestCase):
    def setUp(self):
        # Replace only transport: keep the real probe builder, sender and judge.
        self.reply = lambda method, path, headers: (403, [])
        self.requests = []
        connect = mock.patch.object(smoke, "_connect", side_effect=self.connect)
        self.addCleanup(connect.stop)
        connect.start()

    def connect(self, scheme, host, timeout):
        response = None

        def request(method, path, *, body, headers):
            nonlocal response
            self.requests.append((scheme, method, path, body, headers))
            status, fields = self.reply(method, path, headers)
            response = SimpleNamespace(status=status, read=lambda _: b"", getheaders=lambda: fields)

        return SimpleNamespace(request=request, getresponse=lambda: response, close=lambda: None)

    def run_sweep(self, mode):
        out = io.StringIO()
        code = smoke.sweep("admin.example.test", mode, 5, out)
        return code, out.getvalue().splitlines()

    def assert_get_rounds(self, paths):
        self.assertEqual(
            [row for row in self.requests if row[:2] == ("https", "GET")],
            [("https", "GET", path, None, headers) for headers in ({}, smoke.JUNK) for path in paths],
        )

    def test_c1_real_registry_reaches_both_rounds(self):
        """C1: Every registered path, including non-nav aliases, reaches both GET rounds."""
        self.run_sweep("access")
        self.assert_get_rounds((*smoke.GET_PATHS, *sorted(console_pages._ROUTES)))

    def test_c1_overlap_preserves_fixed_order(self):
        """C1: An overlapping route is probed once per round, without reordering fixed paths."""
        with mock.patch.object(console_pages, "_ROUTES", dict.fromkeys(("/zzz", "/board", "/aaa"))):
            self.run_sweep("access")
        self.assert_get_rounds((*smoke.GET_PATHS, "/aaa", "/zzz"))

    def test_c2_only_one_page_served_anonymously_fails(self):
        """C2: An anonymous 200 on any one registered page fails either sweep mode."""
        for mode in ("access", "isolation"):
            for page in console_pages._ROUTES:
                with self.subTest(mode=mode, page=page):
                    self.requests.clear()
                    self.reply = lambda m, p, h: (200 if (m, p, h) == ("GET", page, {}) else 403, [])
                    code, lines = self.run_sweep(mode)
                    self.assertEqual((code, [line for line in lines if line.endswith(" FAIL")], lines[-1]),
                                     (1, [f"GET {page} 200 - FAIL"], "FAIL 1 probes"))

    def test_c2_same_judging_rules_apply_to_added_paths(self):
        """C2: Added GETs cannot skip Access checks or use isolation rules in Access mode."""
        login = "team.cloudflareaccess.com"
        for mode, status, fields, location, verdict in (
            ("access", 401, [], "-", "ok"),
            ("access", 403, [], "-", "ok"),
            ("access", 302, [("Location", f"https://{login}/login")], login, "ok"),
            ("access", 302, [("Location", "https://elsewhere.example.test/")], "elsewhere.example.test", "FAIL"),
            ("access", 404, [], "-", "FAIL"),
            ("isolation", 404, [], "-", "ok"),
            ("isolation", 503, [], "-", "unreachable"),
            ("access", 403, [("Server", "RecordsPanel")], "-", "FAIL"),
            ("isolation", 403, [("Server", "RecordsPanel")], "-", "FAIL"),
        ):
            with self.subTest(mode=mode, status=status, fields=fields):
                self.reply = lambda m, p, h: (status, fields) if p == "/golive" else (403, [])
                code, lines = self.run_sweep(mode)
                outcome = (1, "FAIL 2 probes") if verdict == "FAIL" else (0, "PASS")
                self.assertEqual((code, lines[-1]), outcome)
                self.assertEqual([line for line in lines if line.startswith("GET /golive ")],
                                 [f"GET /golive {status} {location} {verdict}"] * 2)

    def test_c2_transport_refusal_on_registered_page(self):
        """C2: A connection refusal on an added path fails Access but is isolated."""
        def refuse_golive(method, path, headers):
            if path == "/golive":
                raise ConnectionRefusedError
            return 403, []

        self.reply = refuse_golive
        for mode, verdict, outcome in (("access", "FAIL", (1, "FAIL 2 probes")),
                                       ("isolation", "unreachable", (0, "PASS"))):
            with self.subTest(mode=mode):
                code, lines = self.run_sweep(mode)
                self.assertEqual((code, lines[-1]), outcome)
                self.assertEqual([line for line in lines if line.startswith("GET /golive ")],
                                 [f"GET /golive ERR:ConnectionRefusedError - {verdict}"] * 2)

    def test_c3_sweep_runs_with_sockets_disabled(self):
        """C3: The in-memory transport exercises a complete sweep without opening a socket."""
        with mock.patch("socket.socket", side_effect=AssertionError("unexpected socket")) as socket:
            code, lines = self.run_sweep("access")
        socket.assert_not_called()
        self.assertEqual((code, lines[-1]), (0, "PASS"))
