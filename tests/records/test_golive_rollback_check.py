"""Contract tests for the stage 15 rollback-drill receipt check (C1-C4)."""

import builtins
import contextlib
import io
import json
import os
import tempfile
import unittest
from copy import deepcopy
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

from campaign_tool.records import golive_rollback_check as check_mod
from campaign_tool.records import rehearsal_receipt as rr
from campaign_tool.records import rehearse_engine_switch as driver

START = datetime.fromisoformat("2026-01-02T03:04:04Z")
FINISH = datetime.fromisoformat("2026-01-02T03:04:05Z")
NOW = FINISH + timedelta(days=1)
ENGINE = "a" * 40
MODULE = "rehearse_engine_switch"
FULL = ["render_pairs", "preswitch", "switch", "stub_run", "rollback", "verify_rollback"]


class RollbackCheckTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Private 0700 root; sandboxes stay alive so mutated receipts re-hash real outputs.
        fixture = tempfile.TemporaryDirectory(dir=Path.cwd())
        cls.addClassCleanup(fixture.cleanup)
        cls.root = Path(fixture.name).resolve()
        cls.counter = 0
        cls.result, cls.sandbox = cls.run_driver()
        cls.receipt = rr.build(MODULE, cls.result, cls.sandbox, START, FINISH, ENGINE)

    @classmethod
    def run_driver(cls):
        cls.counter += 1
        sandbox = cls.root / f"sandbox{cls.counter}"
        sandbox.mkdir(mode=0o700)
        return driver.run(sandbox), sandbox

    def mutated(self, mutate, engine: str | None = ENGINE):
        """Rebuild through rr.build so the mutated receipt is still schema-valid."""
        result = deepcopy(self.result)
        mutate(result)
        return rr.build(MODULE, result, self.sandbox, START, FINISH, engine)

    def drill(self, receipt):
        return check_mod.check(receipt, NOW)

    def written(self, receipt):
        out = self.root / f"out{self.counter}"
        self.__class__.counter += 1
        out.mkdir(mode=0o700)
        return rr.write(receipt, out)

    def cli(self, path, *extra):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(check_mod, "_now", return_value=NOW), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = check_mod.main([str(path), *extra])
        return code, out.getvalue(), err.getvalue()

    # ---- C1: verdict shape and the passing case ----

    def test_c1_real_driver_receipt_passes_and_qualifies(self):
        """C1: A complete driver run is drill PASS and, with an engine, qualified.

        Bug caught: a check that rejects the real driver's own receipt."""
        self.assertEqual([step["name"] for step in self.receipt["steps"]], FULL)
        self.assertEqual(self.drill(self.receipt), {
            "drill": "PASS", "drill_codes": [], "qualified": True, "qualify_codes": []})

    def test_c1_drill_and_qualification_are_independent(self):
        """C1: Missing engine or a stale receipt leaves the drill PASS; they never merge.

        Bug caught: folding qualification codes into the drill verdict (or vice versa)."""
        unknown = rr.build(MODULE, self.result, self.sandbox, START, FINISH, None)
        self.assertEqual(self.drill(unknown), {
            "drill": "PASS", "drill_codes": [], "qualified": False,
            "qualify_codes": ["REHEARSAL_ENGINE_UNKNOWN"]})
        stale = check_mod.check(self.receipt, FINISH + timedelta(days=30))
        self.assertEqual((stale["drill"], stale["qualified"], stale["qualify_codes"]),
                         ("PASS", False, ["REHEARSAL_STALE"]))
        broken = self.mutated(lambda r: r["steps"].__delitem__(slice(4, None)))
        verdict = self.drill(broken)
        self.assertEqual((verdict["drill"], verdict["qualified"]), ("FAIL", True))

    def test_c1_invalid_receipt_is_refused_not_judged(self):
        """C1: A receipt that fails rehearsal_receipt validation raises ValueError.

        Bug caught: judging a hand-edited receipt whose qualification is inconsistent."""
        forged = deepcopy(self.receipt)
        forged["steps"][-1]["code"] = "SYNTHETIC_FAILED"  # qualify_codes left stale
        with self.assertRaises(ValueError):
            check_mod.check(forged, NOW)

    # ---- C2: one test per drill code ----

    def test_c2_stage_mismatch(self):
        """C2: A receipt that does not declare stage 15 gets ROLLBACK_STAGE_MISMATCH only.

        Bug caught: trusting step names without the stage declaration."""
        other = self.mutated(lambda r: r.update(stages=["16"]))
        self.assertEqual(self.drill(other)["drill_codes"], ["ROLLBACK_STAGE_MISMATCH"])
        self.assertEqual(self.drill(other)["drill"], "FAIL")

    def test_c2_step_missing_each_drill_step_and_real_early_failure(self):
        """C2: Removing any drill step, or a real early stop, gives ROLLBACK_STEP_MISSING.

        Bug caught: a receipt without rollback or verify_rollback passing because every
        remaining step is OK (rehearsal_receipt.qualify alone still says qualified)."""
        for name in check_mod.DRILL:
            with self.subTest(removed=name):
                receipt = self.mutated(lambda r: r.update(
                    steps=[s for s in r["steps"] if s["name"] != name]))
                self.assertTrue(rr.qualify(receipt, now=NOW)[0])
                self.assertEqual(self.drill(receipt)["drill_codes"], ["ROLLBACK_STEP_MISSING"])
        result, sandbox = self.run_driver_with(driver, "_preswitch",
                                               side_effect=driver._Failed("PRESWITCH_FAILED"))
        receipt = rr.build(MODULE, result, sandbox, START, FINISH, ENGINE)
        self.assertEqual([s["name"] for s in receipt["steps"]], ["render_pairs", "preswitch"])
        verdict = self.drill(receipt)
        self.assertEqual((verdict["drill"], verdict["drill_codes"], verdict["qualified"]),
                         ("FAIL", ["ROLLBACK_STEP_MISSING"], False))

    def test_c2_step_failed_real_and_mutated(self):
        """C2: Any drill step with a non-OK code gives ROLLBACK_STEP_FAILED.

        Bug caught: checking only that the names are present."""
        result, sandbox = self.run_driver_with(driver, "_verify_rollback",
                                               side_effect=driver._Failed("PAIR_MISMATCH"))
        real = rr.build(MODULE, result, sandbox, START, FINISH, ENGINE)
        self.assertEqual([s["name"] for s in real["steps"]], FULL)
        self.assertEqual(self.drill(real)["drill_codes"], ["ROLLBACK_STEP_FAILED"])
        for name in check_mod.DRILL:
            with self.subTest(failed=name):
                def fail(r, name=name):
                    next(s for s in r["steps"] if s["name"] == name).update(code="SYNTHETIC_FAILED")
                self.assertEqual(self.drill(self.mutated(fail))["drill_codes"],
                                 ["ROLLBACK_STEP_FAILED"])

    def test_c2_failed_non_drill_step_is_not_a_drill_failure(self):
        """C2: A failed step outside the drill is reported by qualification, not the drill.

        Bug caught: blaming the drill for a failure of render_pairs or stub_run."""
        receipt = self.mutated(lambda r: r["steps"][3].update(code="STUB_FAILED"))
        verdict = self.drill(receipt)
        self.assertEqual((verdict["drill"], verdict["qualify_codes"]),
                         ("PASS", ["REHEARSAL_FAILED_STEP"]))

    def test_c2_order(self):
        """C2: The three drill steps out of relative order give ROLLBACK_ORDER.

        Bug caught: set membership instead of order; also order among present steps
        when another drill step is missing."""
        def swap(first, second):
            def mutate(r):
                steps = r["steps"]
                i, j = (next(n for n, s in enumerate(steps) if s["name"] == x) for x in (first, second))
                steps[i], steps[j] = steps[j], steps[i]
            return mutate
        # A swap with the final step also makes verify_rollback non-final (two codes).
        for first, second, expected in (
                ("rollback", "verify_rollback", ["ROLLBACK_ORDER", "ROLLBACK_NOT_FINAL"]),
                ("switch", "rollback", ["ROLLBACK_ORDER"]),
                ("switch", "verify_rollback", ["ROLLBACK_ORDER", "ROLLBACK_NOT_FINAL"])):
            with self.subTest(swapped=(first, second)):
                self.assertEqual(self.drill(self.mutated(swap(first, second)))["drill_codes"], expected)

        # Switch moved after rollback, final step still verify_rollback: ORDER alone.
        def move_switch(r):
            steps = r["steps"]
            steps.append(steps.pop(2))
            steps.append(steps.pop(-2))
        # steps: render, preswitch, stub, rollback, verify -> switch appended, verify last again
        receipt = self.mutated(move_switch)
        self.assertEqual([s["name"] for s in receipt["steps"]][-3:], ["rollback", "switch", "verify_rollback"])
        self.assertEqual(self.drill(receipt)["drill_codes"], ["ROLLBACK_ORDER"])
        # Order among the steps that remain when switch is absent.
        absent = self.mutated(lambda r: r.update(steps=[
            s for s in reversed(r["steps"]) if s["name"] != "switch"]))
        self.assertEqual(self.drill(absent)["drill_codes"],
                         ["ROLLBACK_STEP_MISSING", "ROLLBACK_ORDER", "ROLLBACK_NOT_FINAL"])

    def test_c2_not_final_extra_step_after_verify(self):
        """C2: Any step after verify_rollback gives ROLLBACK_NOT_FINAL.

        Bug caught: accepting a receipt whose rollback verification is not the last word."""
        receipt = self.mutated(lambda r: r["steps"].append({"name": "extra", "code": "OK", "outputs": []}))
        self.assertEqual(self.drill(receipt)["drill_codes"], ["ROLLBACK_NOT_FINAL"])

    def test_c2_all_codes_once_in_fixed_order(self):
        """C2: Every code appears at most once, in the documented order.

        Bug caught: duplicate or reordered codes when several faults combine."""
        def everything(r):
            r["stages"] = ["16"]
            r["steps"] = [
                {"name": "verify_rollback", "code": "BAD", "outputs": []},
                {"name": "rollback", "code": "BAD", "outputs": []},
                {"name": "extra", "code": "OK", "outputs": []},
            ]
        codes = self.drill(self.mutated(everything))["drill_codes"]
        self.assertEqual(codes, ["ROLLBACK_STAGE_MISMATCH", "ROLLBACK_STEP_MISSING",
                                 "ROLLBACK_STEP_FAILED", "ROLLBACK_ORDER", "ROLLBACK_NOT_FINAL"])
        self.assertEqual(codes, list(check_mod.CODES))

    # ---- C3: command line ----

    def test_c3_exit_0_pass_and_qualified(self):
        """C3: Drill PASS and qualified prints both lines and exits 0.

        Bug caught: wrong exit code or a merged single line."""
        code, out, err = self.cli(self.written(self.receipt))
        self.assertEqual((code, out, err), (0, "ROLLBACK DRILL: PASS\nQUALIFIED: yes\n", ""))

    def test_c3_exit_1_pass_but_not_qualified(self):
        """C3: Drill PASS with a missing engine exits 1, qualification on its own line.

        Bug caught: reporting a not-qualified receipt as a failed drill (or exit 0)."""
        path = self.written(rr.build(MODULE, self.result, self.sandbox, START, FINISH, None))
        code, out, err = self.cli(path)
        self.assertEqual(
            (code, out, err),
            (1, "ROLLBACK DRILL: PASS\nQUALIFIED: no (REHEARSAL_ENGINE_UNKNOWN)\n", ""))

    def test_c3_exit_3_drill_fails_and_still_reports_qualification(self):
        """C3: A failed drill exits 3 and both lines are still printed.

        Bug caught: hiding the qualification line when the drill fails."""
        path = self.written(self.mutated(lambda r: r["steps"].pop(), engine=None))
        code, out, err = self.cli(path)
        self.assertEqual(code, 3)
        self.assertEqual(out, "ROLLBACK DRILL: FAIL (ROLLBACK_STEP_MISSING)\n"
                              "QUALIFIED: no (REHEARSAL_ENGINE_UNKNOWN)\n")
        # Drill failure wins over qualification even when the receipt is qualified.
        path = self.written(self.mutated(lambda r: r["steps"].pop()))
        code, out, _ = self.cli(path)
        self.assertEqual((code, out), (3, "ROLLBACK DRILL: FAIL (ROLLBACK_STEP_MISSING)\nQUALIFIED: yes\n"))

    def test_c3_json_output_matches_check(self):
        """C3: --json prints the C1 verdict and keeps the exit code.

        Bug caught: JSON that differs from check() or changes the exit code."""
        path = self.written(rr.build(MODULE, self.result, self.sandbox, START, FINISH, None))
        code, out, _ = self.cli(path, "--json")
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(out), check_mod.check(rr.read(path), NOW))
        self.assertEqual(json.loads(out)["drill"], "PASS")

    def test_c3_exit_2_unreadable_or_invalid(self):
        """C3: A missing, directory, non-JSON or schema-invalid receipt exits 2, no verdict.

        Bug caught: a traceback, or a verdict printed for a receipt that cannot be trusted."""
        bad = self.root / "bad"
        bad.mkdir(mode=0o700, exist_ok=True)
        text, binary, schema, tampered = (bad / name for name in ("t", "b", "s", "x"))
        text.write_text("not json")
        binary.write_bytes(b"\xff\xfe")
        schema.write_text('{"schema": "other"}')
        forged = deepcopy(self.receipt)
        forged["steps"][-1]["code"] = "SYNTHETIC_FAILED"  # qualify_codes left stale
        tampered.write_text(json.dumps(forged))
        for path in (bad / "missing", bad, text, binary, schema, tampered):
            with self.subTest(path=path.name):
                code, out, err = self.cli(path)
                self.assertEqual((code, out), (2, ""))
                self.assertIn("invalid rehearsal receipt", err)

    # ---- C4: read-only ----

    def test_c4_reads_through_receipt_read_and_writes_nothing(self):
        """C4: The receipt is loaded via rehearsal_receipt.read and no file is written.

        Bug caught: a private parser, or a --fix/cache write path."""
        path = self.written(self.receipt)
        before = {p: p.read_bytes() for p in path.parent.rglob("*") if p.is_file()}
        writes = []
        real_open, real_os_open = builtins.open, os.open

        def watch_open(file, mode="r", *args, **kwargs):
            if set(mode) & set("wax+"):
                writes.append(file)
            return real_open(file, mode, *args, **kwargs)

        def watch_os_open(file, flags, *args, **kwargs):
            if flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC):
                writes.append(file)
            return real_os_open(file, flags, *args, **kwargs)

        with mock.patch.object(rr, "read", wraps=rr.read) as read, \
                mock.patch.object(builtins, "open", watch_open), \
                mock.patch.object(os, "open", watch_os_open):
            code, _, _ = self.cli(path)
        self.assertEqual(code, 0)
        read.assert_called_once_with(str(path))
        self.assertEqual(writes, [])
        self.assertEqual({p: p.read_bytes() for p in path.parent.rglob("*") if p.is_file()}, before)

    # ---- helpers needing patches ----

    def run_driver_with(self, target, name, **kwargs):
        with mock.patch.object(target, name, **kwargs):
            return self.run_driver()


if __name__ == "__main__":
    unittest.main()
