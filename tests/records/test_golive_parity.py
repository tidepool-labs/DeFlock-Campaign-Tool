"""Numbered C1-C4 checks for the saved-page versus ledger comparison; synthetic rows only."""
import ast
import contextlib
import copy
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from campaign_tool.records import console_page_golive, golive_catalog, golive_evidence, golive_ledger
from campaign_tool.records import golive_parity
from tests.records.test_golive_ledger import NOW

STAGES = list(golive_catalog.STAGES)
SENTINEL = "SENTINEL-LABEL-TEXT"


def base_rows():
    rows = golive_ledger.build([], [], NOW)
    for row in rows:
        if row["stage"] == "2":
            row["label"] = SENTINEL
    return rows


def render_page(rows):
    """The real /golive handler, with only the ledger inputs stubbed."""
    sent = []
    handler = SimpleNamespace(path="/golive", options={"run_report_path": "/synthetic/run-report.json"},
                              _send=lambda status, body: sent.append((status, body)))
    with mock.patch.object(console_page_golive, "_reports", return_value=[]), \
            mock.patch.object(golive_evidence, "load", return_value=[]), \
            mock.patch.object(golive_ledger, "build", return_value=rows):
        assert console_page_golive.serve(handler)
    assert sent[0][0] == 200
    return sent[0][1]


def ledger_json(rows):
    """The real `golive_ledger --json` output for the same rows."""
    out = io.StringIO()
    with mock.patch.object(golive_ledger, "build", return_value=rows), \
            mock.patch.object(golive_ledger.golive_stages, "load_reports", return_value=[]), \
            mock.patch.object(golive_ledger.golive_evidence, "load", return_value=[]), \
            contextlib.redirect_stdout(out):
        assert golive_ledger.main(["--reports", "r", "--evidence", "e", "--json"]) == 0
    return out.getvalue()


def mixed():
    rows = base_rows()
    for stage, state in (("2", "LIVE"), ("A2", "LIVE"), ("A1", "LIVE"), ("3", "GATED"), ("4", "BLOCKED"),
                         ("5", "REHEARSED")):
        next(row for row in rows if row["stage"] == stage)["state"] = state
    return rows


class Base(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()  # mkdtemp: private 0700, not a symlink
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.rows = mixed()
        self.html = render_page(self.rows)
        self.doc = json.loads(ledger_json(self.rows))
        self.page = golive_parity.parse_page(self.html)

    def save(self, name, text):
        path = self.root / name
        path.write_text(text, encoding="utf-8")
        return str(path)

    def run_cli(self, html, cli_text, *extra):
        argv = ["--page", self.save("page.html", html), "--cli", self.save("cli.json", cli_text), *extra]
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            status = golive_parity.main(argv)
        return status, out.getvalue(), err.getvalue()

    def cli_with(self, stage, state):
        doc = copy.deepcopy(self.doc)
        next(row for row in doc["rows"] if row["stage"] == stage)["state"] = state
        return doc


class ParsePageTests(Base):
    def test_c1_real_page_rows_and_summary(self):
        """C1: Every stage and state is read from the real tables, whatever table order the page uses."""
        # Catches a parser keyed on catalogue order, a dropped table, or a wrong State column.
        self.assertEqual(self.page["rows"], {row["stage"]: row["state"] for row in self.rows})
        self.assertEqual(self.page["summary"], golive_ledger._summary(self.rows))
        self.assertEqual(self.page["summary"], "2 of 22 pipeline stages live")
        self.assertNotEqual(list(self.page["rows"]), STAGES)  # A2 sits in the pipeline table, ahead of A1

    def test_c1_markup_not_text(self):
        """C1: Escaped cell text that looks like markup does not change a row."""
        # Catches regex-over-markup parsing that a label such as '</td><td>LIVE' could steer.
        rows = copy.deepcopy(self.rows)
        rows[0]["label"] = "</td><td>LIVE</td></tr><tr><td>99"
        page = golive_parity.parse_page(render_page(rows))
        self.assertEqual(page["rows"], self.page["rows"])

    def test_c1_rejects_non_pages(self):
        """C1: Missing tables, repeats, unknown IDs or states and altered structure raise ValueError."""
        # Catches a parser that tolerates a partial page and so reports a false agree.
        html = self.html
        pipeline_cut = html.index("<h2>Console stages</h2>")
        row_3 = f"<tr><td>3</td><td>{self.rows[2]['label']}</td>"
        self.assertIn(row_3, html)
        cases = {
            "no tables": "<html><body><h1>Go-live</h1></body></html>",
            "no console table": html[:pipeline_cut],
            "no summary": html.replace('<p role="status">', "<p>"),
            "two summaries": html.replace("<h2>Pipeline", '<p role="status">1 of 22 pipeline stages live</p><h2>Pipeline', 1),
            "duplicate stage": html.replace(row_3, row_3.replace("<td>3</td>", "<td>2</td>"), 1),
            "unknown stage": html.replace(row_3, row_3.replace("<td>3</td>", "<td>ZZ9</td>"), 1),
            "unknown state": html.replace("<td>LIVE</td>", "<td>DONE</td>", 1),
            "lowercase state": html.replace("<td>GATED</td>", "<td>gated</td>", 1),
            "tables swapped": html.replace("Pipeline stages", "@").replace("Console stages", "Pipeline stages")
                                  .replace("@", "Console stages"),
            "renamed column": html.replace(">State</th>", ">Status</th>", 1),
            "short row": html.replace("<td>3</td>", "", 1),
            "truncated": html[:html.index("</table>", pipeline_cut)],
            # Cut just after the last table, before the shell's closing markup: every row is present.
            "cut after console table": html[:html.rindex("</table>") + len("</table>")],
            "cut after main": html[:html.rindex("</body>")],
            "cut after body": html[:html.rindex("</html>")],
        }
        for name, text in cases.items():
            with self.subTest(case=name), self.assertRaises(ValueError) as raised:
                golive_parity.parse_page(text)
            self.assertNotIn("ZZ9", str(raised.exception))


class CompareTests(Base):
    def test_c2_clean_agree(self):
        """C2: A page and ledger rendered from the same rows agree, with matching counts."""
        # The healthy control: any false difference here would make every attended check fail.
        result = golive_parity.compare(self.page, self.doc)
        self.assertEqual(result, {"agree": True, "differences": [], "page_count": 2, "cli_count": 2})

    def test_c2_changed_state(self):
        """C2: One changed state is one difference naming the stage with both states."""
        # Catches comparing only counts, which stay equal when a LIVE console row changes.
        for stage, state in (("A1", "BUILT"), ("3", "LIVE"), ("4", "GATED")):
            with self.subTest(stage=stage):
                doc = self.cli_with(stage, state)
                doc.update(live_count=golive_ledger.live_count(doc["rows"]))
                doc["summary"] = f"{doc['live_count']} of 22 pipeline stages live"
                result = golive_parity.compare(self.page, doc)
                diff = next(item for item in result["differences"] if item["stage"] == stage)
                self.assertEqual(diff, {"stage": stage, "page": self.page["rows"][stage], "cli": state})
                self.assertFalse(result["agree"])

    def test_c2_missing_and_extra_stage(self):
        """C2: A stage absent from either side, or an unknown or repeated CLI row, is a difference."""
        # Catches a set comparison that checks only the stages both sides happen to share.
        doc = copy.deepcopy(self.doc)
        doc["rows"] = [row for row in doc["rows"] if row["stage"] != "A3"]
        self.assertIn({"stage": "A3", "page": "BUILT", "cli": None}, golive_parity.compare(self.page, doc)["differences"])
        page = copy.deepcopy(self.page)
        del page["rows"]["7"]
        self.assertIn({"stage": "7", "page": None, "cli": "BUILT"}, golive_parity.compare(page, self.doc)["differences"])
        doc = copy.deepcopy(self.doc)
        doc["rows"].append(dict(doc["rows"][0]))
        doc["rows"].append(dict(doc["rows"][0], stage="ZZ9"))
        diffs = golive_parity.compare(self.page, doc)["differences"]
        self.assertIn({"stage": doc["rows"][0]["stage"], "page": "BUILT", "cli": "DUPLICATE"}, diffs)
        self.assertIn({"stage": "ZZ9", "page": None, "cli": "BUILT"}, diffs)

    def test_c2_stage_missing_from_both_is_a_difference(self):
        """C2: A catalogue stage absent from both inputs is a difference, never a silent match."""
        # Catches `None != None` being False: dropping the same non-LIVE stage from both would agree.
        page = copy.deepcopy(self.page)
        del page["rows"]["7"]
        doc = copy.deepcopy(self.doc)
        doc["rows"] = [row for row in doc["rows"] if row["stage"] != "7"]
        result = golive_parity.compare(page, doc)
        self.assertFalse(result["agree"])
        self.assertEqual(result["differences"], [{"stage": "7", "page": None, "cli": None}])

    def test_c2_printed_count_checked_against_own_rows(self):
        """C2: A count that disagrees with its own side's rows is a difference even if both sides print the same."""
        # Catches trusting two equal summary lines while the rows underneath say otherwise.
        page = copy.deepcopy(self.page)
        page["rows"]["7"] = "LIVE"  # a third LIVE pipeline row; the page still prints 2
        result = golive_parity.compare(page, self.doc)
        self.assertEqual(result["page_count"], result["cli_count"])
        self.assertFalse(result["agree"])
        self.assertIn({"stage": "(page summary vs rows)", "page": 2, "cli": 3}, result["differences"])
        doc = copy.deepcopy(self.doc)
        doc["summary"] = "5 of 22 pipeline stages live"
        result = golive_parity.compare(self.page, doc)
        self.assertEqual({item["stage"] for item in result["differences"]},
                         {"(cli summary vs rows)", "(count)"})
        doc = copy.deepcopy(self.doc)
        doc["live_count"] = 9
        self.assertEqual([item["stage"] for item in golive_parity.compare(self.page, doc)["differences"]],
                         ["(cli live_count vs rows)"])

    def test_c2_total_and_console_rows(self):
        """C2: A wrong printed total is a difference, and console LIVE rows are not counted."""
        # Catches counting every LIVE row, or ignoring the 'of M' part of the summary.
        page = dict(self.page, summary="2 of 21 pipeline stages live")
        self.assertEqual(golive_parity.compare(page, self.doc)["differences"],
                         [{"stage": "(page total)", "page": 21, "cli": 22}])
        self.assertEqual(self.page["rows"]["A1"], "LIVE")  # console row, outside the printed 2

    def test_c2_rejects_malformed_cli_document(self):
        """C2: A ledger document without valid rows, states, live_count or summary is invalid, not a pass."""
        # Catches a comparison that treats a wrong-shaped document as an empty ledger.
        good = self.doc
        bad = (None, [], dict(good, rows="x"), dict(good, rows=[{"stage": "1"}]),
               dict(good, rows=[{"stage": 1, "state": "LIVE"}]),
               dict(good, rows=[dict(good["rows"][0], state="DONE")]),
               dict(good, rows=[dict(good["rows"][0], state=[])]),
               dict(good, rows=[dict(good["rows"][0], state={})]),
               dict(good, live_count=True), dict(good, live_count=None),
               dict(good, summary="many"), {k: v for k, v in good.items() if k != "summary"})
        for index, doc in enumerate(bad):
            with self.subTest(case=index), self.assertRaises(ValueError):
                golive_parity.compare(self.page, doc)


class CliTests(Base):
    def test_c3_agree_exit_zero(self):
        """C3: Matching page and ledger exit 0 and print only counts."""
        # The healthy control for the attended check.
        status, out, err = self.run_cli(self.html, json.dumps(self.doc))
        self.assertEqual((status, err), (0, ""))
        self.assertEqual(out, "agree: 28 stages match; 2 of 22 pipeline stages live on both\n")
        status, out, _ = self.run_cli(self.html, json.dumps(self.doc), "--json")
        self.assertEqual((status, json.loads(out)["agree"]), (0, True))

    def test_c3_difference_exit_one_names_stage(self):
        """C3: Any difference exits 1 and prints each stage; echoes no page text."""
        # Catches exit 0 on a changed state, or a report that leaves the stage unnamed.
        doc = self.cli_with("A1", "BUILT")
        status, out, _ = self.run_cli(self.html, json.dumps(doc))
        self.assertEqual(status, 1)
        self.assertIn("A1: page=LIVE cli=BUILT", out)
        self.assertNotIn(SENTINEL, out)
        status, out, _ = self.run_cli(self.html, json.dumps(doc), "--json")
        self.assertEqual((status, json.loads(out)["differences"][0]["stage"]), (1, "A1"))
        doc = copy.deepcopy(self.doc)
        doc["rows"][0]["stage"] = "<b>X</b>"
        status, out, _ = self.run_cli(self.html, json.dumps(doc))
        self.assertEqual(status, 1)
        self.assertNotIn("<b>", out)

    def test_c3_unrecognised_stage_text_is_never_echoed(self):
        """C3: A CLI stage that is not a catalogue ID, even one starting with '(', is never printed."""
        # Catches echoing any '('-prefixed stage string verbatim in text or JSON output.
        for private in ("(" + SENTINEL + ")", SENTINEL):
            doc = copy.deepcopy(self.doc)
            doc["rows"].append(dict(doc["rows"][0], stage=private))
            for extra in ((), ("--json",)):
                with self.subTest(stage=private, extra=extra):
                    status, out, err = self.run_cli(self.html, json.dumps(doc), *extra)
                    self.assertEqual(status, 1)
                    self.assertNotIn(SENTINEL, out + err)
                    self.assertIn("(unrecognised stage)", out)

    def test_c3_unhashable_state_is_invalid_input(self):
        """C3: A ledger row whose state is a list or object exits 2 with fixed text, not a traceback."""
        # Catches testing `state in STATES` before the type check (TypeError: unhashable).
        for bad in ([], {}):
            doc = copy.deepcopy(self.doc)
            doc["rows"][0]["state"] = bad
            with self.subTest(state=bad):
                status, out, err = self.run_cli(self.html, json.dumps(doc))
                self.assertEqual((status, out), (2, ""))
                self.assertIn("golive_parity:", err)

    def test_c3_invalid_input_exit_two(self):
        """C3: Missing, non-regular, symlinked, oversize or invalid inputs exit 2 with fixed text."""
        # Catches following a symlink, reading an unbounded file, or echoing file content in an error.
        good_page, good_cli = self.save("good.html", self.html), self.save("good.json", json.dumps(self.doc))
        link = self.root / "link.html"
        link.symlink_to(good_page)
        huge = self.save("huge.html", " " * (golive_parity.MAX_BYTES + 1))
        binary = self.root / "binary.html"
        binary.write_bytes(b"\xff\xfe" + self.html.encode())
        junk = self.save("junk.json", SENTINEL)
        # Well under 2 MiB, yet json.loads exhausts the stack; a traceback would exit 1, the difference code.
        deep = self.save("deep.json", "[" * 100000 + "]" * 100000)
        for name, page, cli in (("missing", str(self.root / "none.html"), good_cli),
                                ("directory", str(self.root), good_cli),
                                ("symlink", str(link), good_cli), ("oversize page", huge, good_cli),
                                ("oversize ledger", good_page, huge), ("not utf-8", str(binary), good_cli),
                                ("not json", good_page, junk),
                                ("too deeply nested", good_page, deep)):
            before = sorted(os.listdir(self.root))
            err = io.StringIO()
            with self.subTest(case=name), contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
                self.assertEqual(golive_parity.main(["--page", page, "--cli", cli]), 2)
                self.assertNotIn(SENTINEL, err.getvalue())
            self.assertEqual(sorted(os.listdir(self.root)), before)  # nothing written
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as stop:
            golive_parity.main(["--page", good_page])
        self.assertEqual(stop.exception.code, 2)

    def test_c3_read_failure_after_open_exit_two(self):
        """C3: An I/O error after a successful open (read or fstat) exits 2 with fixed text, not a traceback."""
        # Catches catching OSError only around os.open: an EIO from read() would escape and exit 1 (the difference code).
        good_page, good_cli = self.save("good.html", self.html), self.save("good.json", json.dumps(self.doc))
        real_fdopen = os.fdopen

        class FailingHandle:
            def __init__(self, fd, fail):
                self.fd, self.fail = fd, fail

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                os.close(self.fd)
                return False

            def fileno(self):
                if self.fail == "fstat":
                    raise OSError(5, "Input/output error")
                return self.fd

            def read(self, size=-1):
                raise OSError(5, "Input/output error " + SENTINEL)

        for target in ("page", "cli"):
            for fail in ("read", "fstat"):
                calls = []

                def fdopen(fd, mode="r", *rest, target=target, fail=fail, calls=calls):
                    calls.append(fd)
                    failing = (target == "page") == (len(calls) == 1)
                    return FailingHandle(fd, fail) if failing else real_fdopen(fd, mode, *rest)

                err = io.StringIO()
                with self.subTest(target=target, fail=fail), \
                        mock.patch.object(golive_parity.os, "fdopen", side_effect=fdopen), \
                        contextlib.redirect_stdout(io.StringIO()) as out, contextlib.redirect_stderr(err):
                    self.assertEqual(golive_parity.main(["--page", good_page, "--cli", good_cli]), 2)
                    self.assertEqual(out.getvalue(), "")
                    self.assertEqual(err.getvalue(), "golive_parity: unreadable file\n")
                    self.assertNotIn(SENTINEL, err.getvalue())
                    self.assertEqual(len(calls), 1 if target == "page" else 2)

    def test_c3_size_limit_is_two_mib(self):
        """C3: Exactly 2 MiB is read; one byte more is refused."""
        # Catches an off-by-one in the size bound.
        exact = self.save("exact.txt", "a" * golive_parity.MAX_BYTES)
        self.assertEqual(len(golive_parity._read(exact)), 2 * 1024 * 1024)
        over = self.save("over.txt", "a" * (golive_parity.MAX_BYTES + 1))
        with self.assertRaises(ValueError):
            golive_parity._read(over)

    def test_c3_no_network_or_login_code(self):
        """C3: The module imports no network, subprocess or credential machinery."""
        # Catches a later 'fetch the page for me' or login convenience slipping into this offline tool.
        tree = ast.parse(Path(golive_parity.__file__).read_text(encoding="utf-8"))
        names = {alias.name.split(".")[0] for node in ast.walk(tree) if isinstance(node, ast.Import)
                 for alias in node.names}
        names |= {(node.module or "").split(".")[0] for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
        self.assertEqual(names - {""}, {"argparse", "json", "os", "re", "stat", "sys", "html"})


class DenialPageTests(Base):
    def test_c4_denial_and_login_pages_are_refused(self):
        """C4: A saved login, denial or unconfigured page is 'not a go-live page' and never a pass."""
        # Catches treating an anonymous page as an empty ledger, or claiming it proves anonymous denial.
        unconfigured = SimpleNamespace(path="/golive", options={}, _send=lambda status, body: sent.append(body))
        sent = []
        console_page_golive.serve(unconfigured)
        pages = {
            "unconfigured console": sent[0],
            "access denied": "<html><body><h1>403 Forbidden</h1><p>Access denied</p></body></html>",
            "login form": '<html><form action="/login"><input name="user"><input type="password"></form></html>',
            "diag only": '<main><p role="status" class="diag">Go-live reports unavailable.</p></main>',
        }
        for name, text in pages.items():
            with self.subTest(page=name):
                with self.assertRaisesRegex(ValueError, "^not a go-live page"):
                    golive_parity.parse_page(text)
                status, out, err = self.run_cli(text, json.dumps(self.doc))
                self.assertEqual((status, out), (2, ""))
                self.assertTrue(err.startswith("golive_parity: not a go-live page"))
                for word in ("anonymous", "authenticat", "denied", "login", "proves", "Access"):
                    self.assertNotIn(word, err)


if __name__ == "__main__":
    unittest.main()
