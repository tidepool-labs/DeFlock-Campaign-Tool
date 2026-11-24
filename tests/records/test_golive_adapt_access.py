import hashlib
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from campaign_tool.records import (console_edge_smoke, golive_adapt_access as adapter, golive_adapt_console,
                                   golive_evidence, golive_ledger)

NOW = datetime(2026, 1, 2, 12, tzinfo=timezone.utc)
OBSERVED = datetime(2026, 1, 2, 10, tzinfo=timezone.utc)
STAMP = "2026-01-02T10:00:00Z"
HOST = "console.example.org"
ANON = "ACCESS_SWEEP_ANONYMOUS_ONLY"


def healthy(scheme, host, method, path, body, extra, timeout):
    """Stub sender: Access refuses https probes; plain http redirects to the same https URL."""
    if scheme == "http":
        return 301, [("location", f"https://{host}{path}")]
    return 302, [("location", "https://team.cloudflareaccess.com/cdn-cgi/access/login/x")]


def exposed_board(scheme, host, method, path, body, extra, timeout):
    """Stub sender: one https page answers 200 without Access."""
    if scheme == "https" and method == "GET" and path == "/board" and not extra:
        return 200, []
    return healthy(scheme, host, method, path, body, extra, timeout)


def sweep(sender=healthy, mode="access"):
    """The text the real sweep prints, driven through a stubbed sender (no socket)."""
    out = io.StringIO()
    with patch.object(console_edge_smoke, "_send", side_effect=sender):
        console_edge_smoke.sweep(HOST, mode, 1, out)
    return out.getvalue()


def blob(text):
    return text.encode()


def lines_of(text):
    return text.rstrip("\n").split("\n")


def expected(raw, result="pass", codes=(), *, name="edge-sweep.txt"):
    return {"schema": "records-golive-evidence-v1", "stage": "A4", "kind": "live", "result": result,
            "recorded_at": STAMP, "engine": None, "actor": "operator", "codes": [*codes, ANON],
            "source": {"schema": "records-console-edge-sweep-v1", "sha256": hashlib.sha256(raw).hexdigest(),
                       "name": name}}


def refusal(raw, observed=OBSERVED, now=NOW):
    with unittest.TestCase().assertRaises(adapter.AccessSweepRefused) as caught:
        adapter.records(raw, observed, now)
    return caught.exception.code


class AccessSweepEvidenceTests(unittest.TestCase):
    def test_c1_healthy_sweep_is_one_a4_pass_row(self):
        """C1: A real healthy sweep is one valid A4 pass row the ledger reads as LIVE (a bad adapter never emits one)."""
        raw = blob(sweep())
        row = adapter.records(raw, OBSERVED, NOW)
        self.assertEqual(row, expected(raw))
        self.assertIsNone(golive_evidence.validate(row))
        a4 = next(item for item in golive_ledger.build([], [row], NOW) if item["stage"] == "A4")
        self.assertEqual((a4["state"], a4["live"]["status"]), ("LIVE", "evidence"))

    def test_c1_failed_sweep_is_a_fail_row(self):
        """C1/C3: A real sweep that ends `FAIL 1 probes`, or a PASS footer pasted over a FAIL verdict, is a fail row."""
        text = sweep(exposed_board)
        self.assertEqual(lines_of(text)[-1], "FAIL 1 probes")
        pasted = "\n".join([*lines_of(text)[:-1], "PASS"]) + "\n"
        for case in (text, pasted):
            with self.subTest(footer=lines_of(case)[-1]):
                raw = blob(case)
                row = adapter.records(raw, OBSERVED, NOW)
                self.assertEqual(row, expected(raw, "fail", ["ACCESS_SWEEP_FAILED"]))
                a4 = next(i for i in golive_ledger.build([], [row], NOW) if i["stage"] == "A4")
                self.assertNotEqual(a4["live"]["status"], "evidence")

    def test_c1_recorded_failure_with_any_location_host_is_a_fail_row(self):
        """C1: A redirect the real sweep records as FAIL (IPv6 literal or odd host) is a fail row, not BAD_TRANSCRIPT."""
        for target in ("https://[::1]/login", "https://[2001:db8::1]:8443/x", "https://127.0.0.1/", "https://exa_mple.test/"):
            def redirect(scheme, host, method, path, body, extra, timeout, target=target):
                if scheme == "https" and method == "GET" and path == "/" and not extra:
                    return 302, [("location", target)]
                return healthy(scheme, host, method, path, body, extra, timeout)

            with self.subTest(target=target):
                text = sweep(redirect)
                self.assertRegex(lines_of(text)[1], r"^GET / 302 \S+ FAIL$")
                self.assertRegex(lines_of(text)[-1], r"^FAIL [1-9][0-9]* probes$")
                raw = blob(text)
                self.assertEqual(adapter.records(raw, OBSERVED, NOW), expected(raw, "fail", ["ACCESS_SWEEP_FAILED"]))

    def test_c5_extra_text_in_host_column_of_an_ok_line_is_refused(self):
        """C5: Extra text between status and verdict on an `ok` line never becomes a pass (the widened host rule is FAIL-only)."""
        lines = lines_of(sweep())
        self.assertTrue(lines[1].endswith(" team.cloudflareaccess.com ok"))
        for label, host in {"words": "team.cloudflareaccess.com arbitrary extra text", "ipv6": "::1", "unicode": "é.test"}.items():
            with self.subTest(host=label):
                bent = lines[1].rsplit(" ", 2)[0] + " " + host + " ok"
                text = "\n".join([lines[0], bent, *lines[2:]]) + "\n"
                self.assertEqual(refusal(blob(text)), "ACCESS_SWEEP_BAD_TRANSCRIPT")

    def test_c2_truncated_transcript_with_pass_footer_is_refused(self):
        """C2: Any probe line dropped, or one extra, under a pasted `PASS` footer is a count refusal."""
        lines = lines_of(sweep())
        probes = len(lines) - 2
        self.assertEqual(probes, len(console_edge_smoke._probes()))
        for drop in range(1, probes + 1):
            with self.subTest(drop=drop):
                cut = [line for index, line in enumerate(lines) if index != drop]
                self.assertEqual(refusal(blob("\n".join(cut) + "\n")), "ACCESS_SWEEP_PROBE_COUNT")
        doubled = [*lines[:-1], lines[-2], lines[-1]]
        self.assertEqual(refusal(blob("\n".join(doubled) + "\n")), "ACCESS_SWEEP_PROBE_COUNT")
        self.assertEqual(refusal(blob("ACCESS CHECK\nPASS\n")), "ACCESS_SWEEP_PROBE_COUNT")

    def test_c2_cut_off_without_footer_is_refused(self):
        """C2: A transcript cut mid-run (no `PASS` last line) is never accepted, even with all-ok lines."""
        lines = lines_of(sweep())
        for cut in (lines[:-1], lines[:5]):
            with self.subTest(lines=len(cut)):
                self.assertEqual(refusal(blob("\n".join(cut) + "\n")), "ACCESS_SWEEP_BAD_TRANSCRIPT")

    def test_c2_probe_count_comes_from_the_current_code(self):
        """C2: A sweep that gains a probe makes the old transcript stale-by-version; a literal count would not notice."""
        old = blob(sweep())
        extra = ("https", "GET", "/new-page", None, {})
        with patch.object(console_edge_smoke, "_probes", return_value=[*console_edge_smoke._probes(), extra]):
            self.assertEqual(refusal(old), "ACCESS_SWEEP_PROBE_COUNT")
            self.assertEqual(adapter.records(blob(sweep()), OBSERVED, NOW)["result"], "pass")
        with patch.object(console_edge_smoke, "_probes", return_value=console_edge_smoke._probes()[:-1]):
            self.assertEqual(refusal(old), "ACCESS_SWEEP_PROBE_COUNT")

    def test_c2_same_count_different_probes_is_refused(self):
        """C2: A transcript from an older sweep with the same count but other probes (or reordered) never passes."""
        lines = lines_of(sweep())
        renamed = [lines[0], *[line.replace(" /board ", " /boards ", 1) for line in lines[1:-1]], lines[-1]]
        swapped = [lines[0], lines[2], lines[1], *lines[3:]]
        wrong_method = [lines[0], lines[1].replace("GET", "HEAD", 1), *lines[2:]]
        for case in (renamed, swapped, wrong_method):
            with self.subTest(first=case[1][:14]):
                self.assertEqual(refusal(blob("\n".join(case) + "\n")), "ACCESS_SWEEP_PROBE_SET")

    def test_c2_exact_header_footer_and_verdicts(self):
        """C2: Header, footer and verdict words must match exactly; a footer that disagrees with the verdicts is refused."""
        lines = lines_of(sweep())
        bad = {
            "no header": lines[1:], "header case": ["Access check", *lines[1:]], "header space": ["ACCESS CHECK ", *lines[1:]],
            "footer case": [*lines[:-1], "pass"], "footer words": [*lines[:-1], "PASS 0 probes"],
            "fail zero": [*lines[:-1], "FAIL 0 probes"], "fail count wrong": [*lines[:-1], "FAIL 3 probes"],
            "isolation verdict": [lines[0], lines[1].replace(" ok", " unreachable"), *lines[2:]],
            "bad status": [lines[0], lines[1].replace(" 302 ", " 3o2 ", 1), *lines[2:]],
            "blank line": [*lines[:3], "", *lines[4:]],
        }
        for label, case in bad.items():
            with self.subTest(case=label):
                self.assertEqual(refusal(blob("\n".join(case) + "\n")), "ACCESS_SWEEP_BAD_TRANSCRIPT")

    def test_c3_isolation_transcript_is_never_an_access_pass(self):
        """C3: A real isolation sweep, or an isolation header over Access-looking lines, is refused as not-Access."""
        def blocked(scheme, host, method, path, body, extra, timeout):
            return 404, []
        iso = sweep(blocked, "isolation")
        self.assertEqual(lines_of(iso)[0], "ISOLATION CHECK (not an Access check)")
        self.assertEqual(lines_of(iso)[-1], "PASS")
        relabelled = "\n".join(["ISOLATION CHECK (not an Access check)", *lines_of(sweep())[1:]]) + "\n"
        for raw in (blob(iso), blob(relabelled)):
            self.assertEqual(refusal(raw), "ACCESS_SWEEP_NOT_ACCESS_MODE")

    def test_c3_freshness_follows_the_console_adapter(self):
        """C3: Over-limit is stale, future and zone-less are refused, exactly the limit is kept (shared with A3)."""
        self.assertIs(adapter.MAX_AGE, golive_adapt_console.MAX_AGE)
        raw = blob(sweep())
        self.assertEqual(adapter.records(raw, NOW - adapter.MAX_AGE, NOW)["result"], "pass")
        self.assertEqual(refusal(raw, NOW - adapter.MAX_AGE - timedelta(seconds=1)), "ACCESS_SWEEP_STALE")
        self.assertEqual(refusal(raw, NOW + timedelta(seconds=1)), "ACCESS_SWEEP_FUTURE")
        self.assertEqual(refusal(raw, datetime(2026, 1, 2, 10)), "ACCESS_SWEEP_BAD_INPUT")
        self.assertEqual(refusal(raw, OBSERVED, datetime(2026, 1, 2, 12)), "ACCESS_SWEEP_BAD_INPUT")
        # A failing transcript does not dodge the clock checks either.
        self.assertEqual(refusal(blob(sweep(exposed_board)), NOW - adapter.MAX_AGE - timedelta(seconds=1)),
                         "ACCESS_SWEEP_STALE")

    def test_c4_row_binds_bytes_and_carries_codes_only(self):
        """C4: Source is the transcript SHA-256 and name; the row never carries a hostname."""
        raw = blob(sweep())
        self.assertIn(HOST.encode(), raw)  # the transcript itself names the host (insecure probes)
        row = adapter.records(raw, OBSERVED, NOW, name="friday.txt")
        self.assertEqual(row["source"], {"schema": "records-console-edge-sweep-v1",
                                         "sha256": hashlib.sha256(raw).hexdigest(), "name": "friday.txt"})
        self.assertNotIn(HOST, json.dumps(row))
        other = row["source"]["sha256"]
        self.assertNotEqual(adapter.records(blob(sweep(exposed_board)), OBSERVED, NOW)["source"]["sha256"], other)

    def test_c4_never_proves_phone_login(self):
        """C4: Every row, pass or fail, says it is anonymous-only so it is never read as G's phone login."""
        for text in (sweep(), sweep(exposed_board)):
            self.assertEqual(adapter.records(blob(text), OBSERVED, NOW)["codes"][-1], ANON)

    def test_c5_bounded_strict_and_grammar_only(self):
        """C5: Oversize, non-UTF-8, non-bytes and any extra content never become evidence."""
        text = sweep()
        lines = lines_of(text)
        extras = {"note": [*lines[:-1], "note: looked fine", lines[-1]], "cr": [line + "\r" for line in lines],
                  "after footer": [*lines, "PASS"], "unicode path": [lines[0], lines[1].replace("/", "/é", 1), *lines[2:]]}
        for label, case in extras.items():
            with self.subTest(case=label):
                self.assertIn(refusal(blob("\n".join(case) + "\n")),
                              {"ACCESS_SWEEP_BAD_TRANSCRIPT", "ACCESS_SWEEP_PROBE_COUNT"})
        self.assertEqual(refusal(b"x" * (adapter.MAX_BYTES + 1)), "ACCESS_SWEEP_BAD_INPUT")
        self.assertEqual(refusal(blob(text).replace(b"ACCESS", b"\xffACCESS", 1)), "ACCESS_SWEEP_BAD_INPUT")
        self.assertEqual(refusal(text), "ACCESS_SWEEP_BAD_INPUT")  # str, not bytes
        self.assertEqual(refusal(b""), "ACCESS_SWEEP_BAD_TRANSCRIPT")
        self.assertLess(len(blob(text)), adapter.MAX_BYTES)


class AccessSweepCLITests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.path = self.root / "capture.txt"

    def invoke(self, path=None, observed="2026-01-02T02:00:00-08:00", *args):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err), patch.object(adapter, "_now", return_value=NOW):
            status = adapter.main([str(path or self.path), "--observed-at", observed, *args])
        return status, out.getvalue(), err.getvalue()

    def test_c1_modes_and_exit_status(self):
        """C1: Pass exits 0 in both modes; a recorded failure exits 2 but still prints its row."""
        raw = blob(sweep())
        self.path.write_bytes(raw)
        status, out, err = self.invoke(None, "2026-01-02T02:00:00-08:00", "--json")
        self.assertEqual((status, json.loads(out), err), (0, [expected(raw, name="capture.txt")], ""))
        self.assertEqual(self.invoke(), (0, f"A4\tlive\tpass\t{STAMP}\trecords-console-edge-sweep-v1\t{ANON}\n", ""))
        failed = blob(sweep(exposed_board))
        self.path.write_bytes(failed)
        status, out, err = self.invoke(None, STAMP, "--json")
        self.assertEqual((status, json.loads(out), err),
                         (2, [expected(failed, "fail", ["ACCESS_SWEEP_FAILED"], name="capture.txt")], ""))
        self.assertEqual(self.path.read_bytes(), failed)

    def test_c2_c3_refusals_print_a_stable_code_and_no_row(self):
        """C2/C3: Truncated, isolation, stale and future captures exit 2 with their code on stderr and no stdout."""
        lines = lines_of(sweep())
        cases = {
            "ACCESS_SWEEP_PROBE_COUNT": (blob("\n".join([*lines[:3], lines[-1]]) + "\n"), STAMP),
            "ACCESS_SWEEP_NOT_ACCESS_MODE": (blob("ISOLATION CHECK (not an Access check)\nPASS\n"), STAMP),
            "ACCESS_SWEEP_STALE": (blob(sweep()), "2026-01-01T11:59:59Z"),
            "ACCESS_SWEEP_FUTURE": (blob(sweep()), "2026-01-02T12:00:01Z"),
            "ACCESS_SWEEP_BAD_INPUT": (b"\xff\xfe", STAMP),
        }
        for code, (raw, observed) in cases.items():
            with self.subTest(code=code):
                self.path.write_bytes(raw)
                self.assertEqual(self.invoke(None, observed), (2, "", code + "\n"))

    def test_c5_unreadable_input_is_refused(self):
        """C5: Missing file, directory, symlink, oversize file or an unparsable time exits 2 without a row."""
        refused = (2, "", "ACCESS_SWEEP_BAD_INPUT\n")
        good = self.root / "good.txt"
        good.write_bytes(blob(sweep()))
        link = self.root / "link.txt"
        os.symlink(good, link)
        for name, path in {"missing": self.path, "directory": self.root, "symlink": link}.items():
            with self.subTest(case=name):
                self.assertEqual(self.invoke(path, STAMP), refused)
        self.path.write_bytes(b" " * (adapter.MAX_BYTES + 1))
        self.assertEqual(self.invoke(None, STAMP), refused)
        for observed in ("yesterday", "2026-01-02T10:00:00"):
            with self.subTest(observed=observed):
                self.assertEqual(self.invoke(good, observed), refused)

    def test_c4_never_runs_the_sweep(self):
        """C4: The adapter reads a saved file only; no probe is sent and no connection opens."""
        self.path.write_bytes(blob(sweep()))
        boom = AssertionError("adapter must not run the sweep or open a connection")
        with patch.object(console_edge_smoke, "_send", side_effect=boom), \
                patch.object(console_edge_smoke, "_connect", side_effect=boom), \
                patch.object(console_edge_smoke, "sweep", side_effect=boom), \
                patch.object(console_edge_smoke, "main", side_effect=boom), \
                patch("socket.socket.connect", side_effect=boom):
            self.assertEqual(self.invoke(None, STAMP)[0], 0)


if __name__ == "__main__":
    unittest.main()
