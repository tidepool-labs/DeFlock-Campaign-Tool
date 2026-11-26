"""Offline acceptance for the before/after ledger comparison (synthetic documents only).

Each test names its contract item (C1-C4) and the believable bug that fails it.
"""

import gc
import io
import json
import os
import tempfile
import unittest
from typing import Any
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from campaign_tool.records import golive_catalog, golive_diff

STAGES = list(golive_catalog.STAGES)
PROBE = "LEAK-PROBE-9f3"
# Written out here, not imported, so a changed ranking in the module fails.
EXPECTED_RANK = {"BLOCKED": 0, "BUILT": 1, "REHEARSED": 2, "GATED": 3, "LIVE": 4}


def doc(default="BUILT", **overrides) -> dict[str, Any]:
    """Build a ledger document; overrides map a stage ID with '_' for none, e.g. s3='LIVE'."""
    states = {key[1:]: value for key, value in overrides.items()}
    return {"rows": [dict(stage=stage, state=states.get(stage, default), pipeline=True, next=None)
                     for stage in STAGES],
            "live_count": 0, "summary": "x"}


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        os.chmod(self.root, 0o700)

    def write(self, name, content):
        path = self.root / name
        path.write_bytes(content if isinstance(content, bytes) else json.dumps(content).encode())
        return str(path)

    def run_cli(self, *args):
        # Collect garbage left by earlier tests in this process first. On Python
        # 3.13 an unclosed sqlite3 connection warns when it is collected, and an
        # automatic collection inside the capture below would put that unrelated
        # text in the captured stderr (main 223543e7: "151 != 1" in full 3.13).
        gc.collect()
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            try:
                code = golive_diff.main(list(args))
            except SystemExit as exc:  # argparse usage errors
                code = exc.code
        return code, out.getvalue(), err.getvalue()


class DiffTests(Base):
    def test_c1_advance_regress_same(self):
        # C1: move is ADVANCED/REGRESSED/SAME-only-unlisted, in catalogue order, with live counts.
        # Bug: listing unchanged stages, or ordering by input order instead of catalogue order.
        before = doc(s3="BUILT", s15="LIVE", s18="GATED")
        after = doc(s3="LIVE", s15="REHEARSED", s18="GATED")
        after["rows"].reverse()
        result = golive_diff.diff(before, after)
        self.assertEqual(result["changes"], [
            dict(stage="3", before="BUILT", after="LIVE", move="ADVANCED"),
            dict(stage="15", before="LIVE", after="REHEARSED", move="REGRESSED")])
        self.assertEqual((result["live_before"], result["live_after"]), (1, 1))
        self.assertEqual(result["regressed"], ["15"])
        same = golive_diff.diff(before, before)
        self.assertEqual((same["changes"], same["regressed"]), ([], []))

    def test_c1_rank_order_is_exact(self):
        # C1: BLOCKED<BUILT<REHEARSED<GATED<LIVE for every ordered pair of states.
        # Bug: swapping adjacent ranks (e.g. BLOCKED above BUILT) mislabels a regression.
        self.assertEqual(golive_diff.RANK, EXPECTED_RANK)
        for old, old_rank in EXPECTED_RANK.items():
            for new, new_rank in EXPECTED_RANK.items():
                result = golive_diff.diff(doc(s3=old), doc(s3=new))
                if old == new:
                    self.assertEqual(result["changes"], [], (old, new))
                    continue
                move = "ADVANCED" if new_rank > old_rank else "REGRESSED"
                self.assertEqual(result["changes"][0]["move"], move, (old, new))

    def test_c1_live_count_uses_catalogue_pipeline_flag(self):
        # C1: live counts follow the catalogue, not a row's own pipeline field or live_count.
        # Bug: trusting a document-supplied live_count.
        console = next(s for s, r in golive_catalog.STAGES.items() if not r.pipeline)
        before = doc()
        after = doc(**{"s" + console: "LIVE", "s3": "LIVE"})
        after["live_count"] = 99
        self.assertEqual(golive_diff.diff(before, after)["live_after"], 1)

    def test_c1_invalid_documents_raise(self):
        # C1: anything but one valid row per catalogue stage is ValueError, without content echo.
        # Bug: a short, padded, duplicated or unknown-state document yielding a partial comparison.
        good = doc()
        cases = {}
        cases["not a dict"] = [good["rows"]]
        cases["rows missing"] = {"live_count": 1}
        cases["rows not list"] = {"rows": {"3": "LIVE"}}
        cases["too few"] = {"rows": good["rows"][:-1]}
        cases["too many"] = {"rows": good["rows"] + [good["rows"][0]]}
        dup = doc()
        dup["rows"][1] = dict(dup["rows"][0])
        cases["duplicate stage, right count"] = dup
        unknown = doc()
        unknown["rows"][0]["stage"] = PROBE
        cases["unknown stage"] = unknown
        for bad_stage in (None, 3, ["3"], {"a": 1}):
            weird = doc()
            weird["rows"][0]["stage"] = bad_stage
            cases[f"stage {bad_stage!r}"] = weird
        for bad_state in ("live", "DONE", "", None, 4, True, ["LIVE"], PROBE):
            weird = doc()
            weird["rows"][2]["state"] = bad_state
            cases[f"state {bad_state!r}"] = weird
        nostate = doc()
        del nostate["rows"][2]["state"]
        cases["state missing"] = nostate
        cases["row not object"] = {"rows": ["x"] + good["rows"][1:]}
        for name, bad in cases.items():
            for side in ("before", "after"):
                args = (bad, good) if side == "before" else (good, bad)
                with self.subTest(case=name, side=side):
                    with self.assertRaises(ValueError) as caught:
                        golive_diff.diff(*args)
                    self.assertNotIn(PROBE, str(caught.exception))


class ExpectTests(Base):
    def test_c2_parse_expected(self):
        # C2: a comma list of catalogue IDs; unknown, empty or blank entries are usage errors.
        # Bug: accepting a typo such as "31" and silently never checking it.
        self.assertEqual(golive_diff.parse_expected("3,15,18,19"), ["3", "15", "18", "19"])
        self.assertEqual(golive_diff.parse_expected(" 17b , 3,3"), ["17b", "3"])
        for bad in ("", "3,", ",3", "3,,15", "999", "3,x", "A9"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                golive_diff.parse_expected(bad)

    def test_c2_expected_stage_missing(self):
        # C2: expected stage not LIVE in AFTER is reported even when nothing regressed.
        # Bug: checking the before document, or only checking stages that changed.
        before = doc(s3="LIVE", s15="BUILT")
        after = doc(s3="LIVE", s15="GATED")
        self.assertEqual(golive_diff.not_live(after, ["3", "15", "18"]), ["15", "18"])
        self.assertEqual(golive_diff.not_live(before, ["3"]), [])
        self.assertEqual(golive_diff.not_live(after, []), [])


class CliTests(Base):
    def test_c3_exit_zero_when_clean(self):
        # C3: advance with every expected stage LIVE exits 0 and prints one line per change.
        before = self.write("b.json", doc())
        after = self.write("a.json", doc(s3="LIVE", s15="LIVE", s18="LIVE", s19="LIVE", s4="GATED"))
        code, out, err = self.run_cli(before, after, "--expect-live", "3,15,18,19")
        self.assertEqual((code, err), (0, ""))
        lines = out.splitlines()
        self.assertEqual(lines[0], "stage 3: BUILT -> LIVE ADVANCED")
        self.assertEqual(len([l for l in lines if l.startswith("stage ")]), 5)
        self.assertIn("5 changed (5 advanced, 0 regressed); pipeline live 0 -> 4", lines[-1])

    def test_c3_no_change_exit_zero(self):
        # C3: SAME ledgers are clean. Bug: treating "no change" as failure.
        path = self.write("b.json", doc(s3="LIVE"))
        code, out, _ = self.run_cli(path, path)
        self.assertEqual(code, 0)
        self.assertIn("0 changed", out)

    def test_c3_regression_exit_one_names_stage(self):
        # C3: a stage that slipped back exits 1 and is named, even if others advanced.
        # Bug: reporting only net live_count, which hides one stage up and another down.
        before = self.write("b.json", doc(s15="LIVE"))
        after = self.write("a.json", doc(s3="LIVE", s15="GATED"))
        code, out, err = self.run_cli(before, after)
        self.assertEqual((code, err), (1, ""))
        self.assertIn("stage 15: LIVE -> GATED REGRESSED", out)
        self.assertIn("REGRESSED: 15", out)
        self.assertIn("pipeline live 1 -> 1", out)

    def test_c3_expectation_failure_exit_one_names_stage(self):
        # C3: an expected stage still not LIVE exits 1 and is named, even with no regression.
        before = self.write("b.json", doc())
        after = self.write("a.json", doc(s3="LIVE", s15="GATED"))
        code, out, _ = self.run_cli(before, after, "--expect-live", "3,15,18")
        self.assertEqual(code, 1)
        self.assertIn("NOT LIVE: 15, 18", out)
        self.assertNotIn("REGRESSED:", out)

    def test_c3_json_output(self):
        # C3: --json carries the C1 result plus the expectation verdict and the same exit code.
        before = self.write("b.json", doc(s3="LIVE"))
        after = self.write("a.json", doc())
        code, out, _ = self.run_cli(before, after, "--json", "--expect-live", "3")
        report = json.loads(out)
        self.assertEqual(code, 1)
        self.assertEqual((report["regressed"], report["not_live"], report["ok"]), (["3"], ["3"], False))
        self.assertEqual(report["changes"][0]["move"], "REGRESSED")

    def test_c3_usage_error_unknown_expected_stage(self):
        # C2/C3: unknown expected ID is exit 2 before any file is read.
        # Bug: exit 1 (looks like a failed sitting) or a traceback.
        path = self.write("b.json", doc())
        code, out, err = self.run_cli(path, path, "--expect-live", "3,999")
        self.assertEqual((code, out), (2, ""))
        self.assertEqual(len(err.splitlines()), 1)
        self.assertNotIn("Traceback", err)
        code, _, _ = self.run_cli(path)  # missing AFTER argument
        self.assertEqual(code, 2)

    def test_c3_malformed_input_exit_two_without_echo(self):
        # C3: unreadable/invalid input exits 2 with one stderr line and no file content.
        # Bug: a traceback, or json's own message quoting the offending text.
        good = self.write("good.json", doc())
        wrong_state = doc()
        wrong_state["rows"][0]["state"] = PROBE
        short = doc()
        short["rows"] = short["rows"][:-1]
        bad = {
            "garbage.json": f"{PROBE} not json".encode(),
            "binary.json": b"\xff\xfe" + PROBE.encode(),
            "nan.json": b'{"rows": NaN, "x": "' + PROBE.encode() + b'"}',
            "deep.json": b"[" * 100000,
            "state.json": wrong_state,
            "short.json": short,
            "empty.json": b"",
            "list.json": [PROBE],
        }
        for name, content in bad.items():
            for order in ("before", "after"):
                path = self.write(name, content)
                args = (path, good) if order == "before" else (good, path)
                with self.subTest(name=name, order=order):
                    code, out, err = self.run_cli(*args)
                    self.assertEqual((code, out), (2, ""))
                    self.assertEqual(len(err.splitlines()), 1)
                    self.assertTrue(err.startswith("golive_diff: " + order + ":"))
                    self.assertNotIn(PROBE, err)
                    self.assertNotIn("Traceback", err)
        code, _, err = self.run_cli(str(self.root / "absent.json"), good)
        self.assertEqual(code, 2)
        self.assertNotIn(str(self.root), err)

    def test_c4_duplicate_json_field_refused(self):
        # C4: duplicate fields at any depth are refused, not last-value-wins.
        # Bug: json.loads default silently keeping the later "state".
        good = self.write("good.json", doc())
        text = json.dumps(doc(s3="LIVE"))
        top = text.replace('"live_count": 0', '"live_count": 0, "live_count": 5')
        nested = text.replace('"state": "LIVE"', '"state": "BUILT", "state": "LIVE"', 1)
        rows_twice = '{"rows": [], ' + text[1:]
        self.assertNotEqual(top, text)
        self.assertNotEqual(nested, text)
        for name, content in (("top", top), ("nested", nested), ("rows", rows_twice)):
            path = self.write(name + ".json", content.encode())
            with self.subTest(name=name):
                code, out, err = self.run_cli(good, path)
                self.assertEqual((code, out), (2, ""))
                self.assertEqual(err, "golive_diff: after: duplicate JSON field\n")

    def test_c4_wrong_row_count(self):
        # C4/C1: one row too few or too many is exit 2 naming the side, never a partial diff.
        good = self.write("good.json", doc())
        for name, rows in (("few", doc()["rows"][:-1]), ("many", doc()["rows"] * 2), ("none", [])):
            path = self.write(name + ".json", {"rows": rows})
            with self.subTest(name=name):
                code, out, err = self.run_cli(path, good)
                self.assertEqual((code, out), (2, ""))
                self.assertEqual(err, f"golive_diff: before: expected {len(STAGES)} rows\n")

    def test_c4_size_limit_is_one_mib(self):
        # C4: exactly 1 MiB is read; one byte more is refused before parsing.
        # Bug: an off-by-one limit, or reading the whole file into memory first.
        good = self.write("good.json", doc())
        body = json.dumps(doc(s3="LIVE")).encode()
        exact = body + b" " * (golive_diff.MAX_BYTES - len(body))
        self.assertEqual(len(exact), 1024 * 1024)
        code, out, _ = self.run_cli(good, self.write("exact.json", exact))
        self.assertEqual(code, 0)
        self.assertIn("stage 3: BUILT -> LIVE ADVANCED", out)
        code, out, err = self.run_cli(good, self.write("over.json", exact + b" "))
        self.assertEqual((code, out, err), (2, "", "golive_diff: after: file exceeds 1 MiB\n"))

    def test_c4_only_regular_non_symlink_files(self):
        # C4: symlinks, directories and FIFOs are refused. Bug: following a link to another file.
        good = self.write("good.json", doc())
        link = self.root / "link.json"
        link.symlink_to(good)
        fifo = self.root / "fifo.json"
        os.mkfifo(fifo)
        for target in (link, self.root, fifo):
            with self.subTest(target=target.name):
                code, out, err = self.run_cli(good, str(target))
                self.assertEqual((code, out), (2, ""))
                self.assertEqual(len(err.splitlines()), 1)
        code, _, err = self.run_cli(str(link), good)
        self.assertEqual(code, 2)
        self.assertTrue(err.startswith("golive_diff: before:"))

    def test_c4_nothing_is_written(self):
        # C4: the inputs and their directory are unchanged after a run.
        before = self.write("b.json", doc())
        after = self.write("a.json", doc(s3="LIVE"))
        snapshot = {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in self.root.iterdir()}
        self.run_cli(before, after)
        self.assertEqual(snapshot, {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in self.root.iterdir()})


if __name__ == "__main__":
    unittest.main()
