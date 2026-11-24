"""Offline acceptance for the go-live acceptance evaluator, using synthetic rows and evidence."""

import copy
import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from campaign_tool.records import golive_acceptance, golive_catalog, golive_ledger
from campaign_tool.records import rehearsal_receipt as rr
from campaign_tool.records import rehearse_engine_switch as driver

PASS = dict(status="pass", recorded_at="2026-01-02T00:00:00Z")
MISSING = dict(status="missing", recorded_at=None)
UNNEEDED = dict(status="not needed", recorded_at=None)
LIVE = dict(status="evidence", recorded_at="2026-01-02T00:00:00Z")
NOT_LIVE = dict(status="missing")
PIPELINE = [stage for stage, rule in golive_catalog.STAGES.items() if rule.pipeline]


def satisfied(stage):
    """A ledger row that meets every catalogue requirement for this stage."""
    rule = golive_catalog.STAGES[stage]
    return dict(stage=stage, label=rule.label, pipeline=rule.pipeline,
                rehearsed=PASS if rule.needs_rehearsal else UNNEEDED,
                gate=PASS if rule.needs_gate else UNNEEDED,
                live=LIVE, state="LIVE", next=None)


def all_satisfied():
    return [satisfied(stage) for stage in golive_catalog.STAGES]


def codes(result):
    return [(b["stage"], b["code"]) for b in result["blockers"]]

MODULE = "rehearse_engine_switch"
ENGINE = "a" * 40
OTHER_HUMAN = ["FLAWLESS check", "stages 7 and 8 proven on new documents"]
# Golden output of the unchanged command for one blocked ledger (stage 3 gate missing).
GOLDEN_TEXT = (
    "{n} of {n} pipeline stages live\nBlockers:\n  3 Arrival trigger: GATE_NOT_PASS\n"
    "Human-only items (not machine-checked):\n  - rollback drill\n  - FLAWLESS check\n"
    "  - stages 7 and 8 proven on new documents\nNOT READY: 1 blockers\n")


class GoLiveAcceptanceTests(unittest.TestCase):
    def invoke(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = golive_acceptance.main(list(args))
        return code, out.getvalue(), err.getvalue()

    def dirs(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        root = Path(folder.name).resolve()
        reports, evidence = root / "reports", root / "golive-evidence"
        reports.mkdir()
        evidence.mkdir()
        return reports, evidence

    def test_fixture_matches_catalogue_shape(self):
        """Fixture guard: the tests below need stages of each flag kind, so catalogue drift fails loudly here."""
        rules = golive_catalog.STAGES
        self.assertTrue(rules["3"].needs_gate and rules["3"].needs_rehearsal and rules["3"].pipeline)
        self.assertFalse(rules["1"].needs_rehearsal)
        self.assertFalse(rules["2"].needs_gate)
        self.assertFalse(rules["A1"].pipeline)

    def test_c1_all_satisfied_has_no_blockers(self):
        """C1: Every pipeline stage live with required proof is the only path to zero blockers."""
        result = golive_acceptance.evaluate(all_satisfied())
        self.assertEqual((result["live"], result["total"], result["blockers"]), (len(PIPELINE), len(PIPELINE), []))

    def test_c1_each_code_alone_on_every_stage(self):
        """C1: Each code appears alone and only where the catalogue flag asks for it, for every pipeline stage."""
        # A hard-coded stage list, or a code that fires without its flag, breaks one of these cases.
        for stage in PIPELINE:
            rule = golive_catalog.STAGES[stage]
            for column, flagged, code in (("rehearsed", rule.needs_rehearsal, "REHEARSAL_NOT_PASS"),
                                          ("gate", rule.needs_gate, "GATE_NOT_PASS")):
                for status in ("missing", "fail"):
                    with self.subTest(stage=stage, column=column, status=status):
                        rows = all_satisfied()
                        target = next(r for r in rows if r["stage"] == stage)
                        target[column] = dict(status=status, recorded_at=None)
                        result = golive_acceptance.evaluate(rows)
                        self.assertEqual(codes(result), [(stage, code)] if flagged else [])
                        self.assertEqual(result["live"], len(PIPELINE))
            for state in ("BUILT", "REHEARSED", "GATED", "BLOCKED"):
                with self.subTest(stage=stage, state=state):
                    rows = all_satisfied()
                    next(r for r in rows if r["stage"] == stage).update(state=state, live=NOT_LIVE)
                    result = golive_acceptance.evaluate(rows)
                    self.assertEqual(codes(result), [(stage, "NOT_LIVE")])
                    self.assertEqual(result["live"], len(PIPELINE) - 1)

    def test_c1_derives_from_catalogue_flags(self):
        """C1: Flipping a catalogue flag changes the blockers, so no stage list is hard-coded."""
        rows = all_satisfied()
        next(r for r in rows if r["stage"] == "2").update(rehearsed=MISSING)
        self.assertEqual(codes(golive_acceptance.evaluate(rows)), [("2", "REHEARSAL_NOT_PASS")])
        relaxed = {**golive_catalog.STAGES, "2": golive_catalog.Row("x", True, False, False, "run_report", "pipeline")}
        with patch.object(golive_catalog, "STAGES", relaxed):
            self.assertEqual(golive_acceptance.evaluate(rows)["blockers"], [])
        plain = all_satisfied()
        strict = {**golive_catalog.STAGES, "2": golive_catalog.Row("x", True, True, True, "run_report", "pipeline")}
        with patch.object(golive_catalog, "STAGES", strict):
            self.assertEqual(codes(golive_acceptance.evaluate(plain)), [("2", "GATE_NOT_PASS")])

    def test_c1_multiple_codes_and_catalogue_order(self):
        """C1: One stage can carry several codes, and output order follows the catalogue, not the input."""
        rows = all_satisfied()
        for stage in ("4", "2"):
            next(r for r in rows if r["stage"] == stage).update(
                rehearsed=MISSING, gate=MISSING, live=NOT_LIVE, state="BUILT")
        result = golive_acceptance.evaluate(list(reversed(rows)))
        self.assertEqual(codes(result), [("2", "NOT_LIVE"), ("2", "REHEARSAL_NOT_PASS"),
                                         ("4", "NOT_LIVE"), ("4", "REHEARSAL_NOT_PASS"), ("4", "GATE_NOT_PASS")])

    def test_c1_console_only_rows_never_count(self):
        """C1: Console-only rows add neither blockers nor total/live, even when every column is bad."""
        rows = all_satisfied()
        console = [r for r in rows if not r["pipeline"]]
        self.assertTrue(console)
        for r in console:
            r.update(rehearsed=MISSING, gate=MISSING, live=NOT_LIVE, state="BLOCKED")
        result = golive_acceptance.evaluate(rows)
        self.assertEqual((result["live"], result["total"], result["blockers"]),
                         (len(PIPELINE), len(PIPELINE), []))
        self.assertEqual(result["live"], golive_ledger.live_count(all_satisfied()))

    def test_c1_live_total_match_ledger_live_count(self):
        """C1: live matches golive_ledger.live_count, counting live pipeline rows only."""
        rows = all_satisfied()
        for r in rows[:3]:
            r.update(live=NOT_LIVE, state="BUILT")
        result = golive_acceptance.evaluate(rows)
        self.assertEqual(result["live"], golive_ledger.live_count(rows))

    def test_c1_invalid_rows_raise_value_error(self):
        """C1: Unknown stages and malformed rows fail closed instead of reading as ready."""
        # Every case starts from a complete, valid ledger and breaks exactly one thing, so the
        # ValueError must come from that field's own check and not from the missing-stage check.
        # If a missing or malformed field stopped raising, the matching case would evaluate cleanly.
        self.assertEqual(golive_acceptance.evaluate(all_satisfied())["blockers"], [])

        def broken(stage, change):
            rows = all_satisfied()
            row = next(r for r in rows if r["stage"] == stage)
            change(row)
            return rows

        def drop(key):
            return lambda row: row.pop(key)

        for label, rows in (
            ("unknown stage", all_satisfied() + [dict(satisfied("1"), stage="999")]),
            ("row without a stage", all_satisfied() + [{"pipeline": True}]),
            ("stage 3 without gate", broken("3", drop("gate"))),
            ("stage 3 without rehearsed", broken("3", drop("rehearsed"))),
            ("stage 3 gate has no status", broken("3", lambda r: r.update(gate={}))),
            ("stage 3 gate is not a mapping", broken("3", lambda r: r.update(gate=None))),
            ("stage 3 without state", broken("3", drop("state"))),
            ("stage 3 without pipeline flag", broken("3", drop("pipeline"))),
            ("stage 1 reduced to its stage key", broken("1", lambda r: [r.pop(k) for k in list(r) if k != "stage"])),
            ("console stage without pipeline flag", broken("A1", drop("pipeline"))),
        ):
            with self.subTest(label):
                with self.assertRaises(ValueError):
                    golive_acceptance.evaluate(rows)

    def test_c1_incomplete_duplicate_or_misclassified_rows_raise(self):
        """C1: A ledger missing a pipeline stage, repeating a stage or misflagging one must not read as ready.

        Without the completeness check, [] reports 0 of 0 live and READY; without the duplicate and
        membership checks, a repeated or pipeline=False row silently skips or double counts a stage.
        """
        for label, rows in (
            ("empty", []),
            ("malformed stage 3", [{"stage": "3", "pipeline": False}]),
            ("one pipeline stage absent", [r for r in all_satisfied() if r["stage"] != "3"]),
            ("duplicate stage", all_satisfied() + [satisfied("3")]),
            ("duplicate console stage", all_satisfied() + [satisfied("A1")]),
            ("pipeline stage marked console", [dict(r, pipeline=False) if r["stage"] == "3" else r
                                               for r in all_satisfied()]),
            ("console stage marked pipeline", [dict(r, pipeline=True) if r["stage"] == "A1" else r
                                               for r in all_satisfied()]),
            ("pipeline flag not a bool", [dict(r, pipeline=1) if r["stage"] == "3" else r
                                          for r in all_satisfied()]),
        ):
            with self.subTest(label):
                with self.assertRaises(ValueError):
                    golive_acceptance.evaluate(rows)

    def test_c1_console_rows_may_be_absent(self):
        """C1: Only pipeline stages are required, so a ledger without console-only rows still evaluates."""
        rows = [r for r in all_satisfied() if r["pipeline"]]
        self.assertEqual(golive_acceptance.evaluate(rows)["blockers"], [])

    def test_c2_manual_items_always_present(self):
        """C2: The human-only list is fixed and appears in ready, not-ready and JSON output."""
        expected = ["rollback drill", "FLAWLESS check", "stages 7 and 8 proven on new documents"]
        self.assertEqual(golive_acceptance.evaluate(all_satisfied())["manual"], expected)
        self.assertEqual(golive_acceptance.MANUAL, tuple(expected))
        reports, evidence = self.dirs()
        args = ["--reports", str(reports), "--evidence", str(evidence)]
        with patch.object(golive_ledger, "build", return_value=all_satisfied()):
            _, ready_text, _ = self.invoke(*args)
        _, blocked_text, _ = self.invoke(*args)
        _, blocked_json, _ = self.invoke(*args, "--json")
        for text in (ready_text, blocked_text):
            for item in expected:
                self.assertIn(f"- {item}", text)
        self.assertEqual(json.loads(blocked_json)["manual"], expected)

    def test_c3_ready_cli(self):
        """C3: Zero blockers prints READY FOR HUMAN ACCEPTANCE last and exits 0."""
        reports, evidence = self.dirs()
        with patch.object(golive_ledger, "build", return_value=all_satisfied()):
            code, out, err = self.invoke("--reports", str(reports), "--evidence", str(evidence))
        lines = out.splitlines()
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(lines[0], f"{len(PIPELINE)} of {len(PIPELINE)} pipeline stages live")
        self.assertEqual(lines[-1], "READY FOR HUMAN ACCEPTANCE")

    def test_c3_not_ready_cli_on_empty_inputs(self):
        """C3: Empty evidence reports blockers grouped by stage in catalogue order and exits 1."""
        reports, evidence = self.dirs()
        code, out, err = self.invoke("--reports", str(reports), "--evidence", str(evidence))
        lines = out.splitlines()
        self.assertEqual((code, err), (1, ""))
        self.assertEqual(lines[0], f"0 of {len(PIPELINE)} pipeline stages live")
        groups = [line.split()[0] for line in lines if line.startswith("  ") and ":" in line]
        self.assertEqual(groups, PIPELINE)
        self.assertRegex(lines[-1], r"^NOT READY: \d+ blockers$")
        self.assertIn("  3 Arrival trigger: NOT_LIVE, REHEARSAL_NOT_PASS, GATE_NOT_PASS", lines)
        self.assertNotIn("  1 Request sent: NOT_LIVE, REHEARSAL_NOT_PASS", lines)

    def test_c3_json_and_text_agree_and_never_say_accepted(self):
        """C3: JSON and text carry the same counts and blockers, and neither contains the word ACCEPTED."""
        reports, evidence = self.dirs()
        args = ["--reports", str(reports), "--evidence", str(evidence)]
        rows = all_satisfied()
        next(r for r in rows if r["stage"] == "3").update(gate=MISSING)
        next(r for r in rows if r["stage"] == "5").update(rehearsed=MISSING, live=NOT_LIVE, state="BUILT")
        outputs = {}
        with patch.object(golive_ledger, "build", return_value=rows):
            for name, extra in (("text", []), ("json", ["--json"])):
                outputs[name] = self.invoke(*args, *extra)
        self.assertEqual({outputs["text"][0], outputs["json"][0]}, {1})
        data = json.loads(outputs["json"][1])
        text = outputs["text"][1].splitlines()
        count = len(data["blockers"])
        self.assertEqual(count, 3)
        self.assertEqual(text[-1], data["verdict"])
        self.assertEqual(text[-1], f"NOT READY: {count} blockers")
        self.assertEqual(text[0], data["summary"])
        self.assertEqual(text[0], f"{data['live']} of {data['total']} pipeline stages live")
        from_text = [(line.split()[0], code.strip()) for line in text if line.startswith("  ") and ":" in line
                     for code in line.split(": ", 1)[1].split(",")]
        self.assertEqual(from_text, [(b["stage"], b["code"]) for b in data["blockers"]])
        for out in (outputs["text"][1], outputs["json"][1], golive_acceptance.render(data)):
            self.assertNotIn("accepted", out.lower())

    def test_c3_invalid_input_exits_two(self):
        """C3: A missing or unreadable directory exits 2 with one stderr line and no stdout."""
        reports, evidence = self.dirs()
        for args in (["--reports", str(reports / "nope"), "--evidence", str(evidence)],
                     ["--reports", str(reports), "--evidence", str(evidence / "nope")]):
            code, out, err = self.invoke(*args)
            self.assertEqual((code, out), (2, ""))
            self.assertRegex(err, r"^golive_acceptance: [^\n]+\n$")

    def test_c4_read_only_and_pure(self):
        """C4: Evaluation does not mutate its rows, and a CLI run leaves both input directories unchanged."""
        rows = all_satisfied()
        next(r for r in rows if r["stage"] == "2").update(rehearsed=MISSING)
        before = copy.deepcopy(rows)
        golive_acceptance.evaluate(rows)
        self.assertEqual(rows, before)
        reports, evidence = self.dirs()
        listing = sorted(p.name for p in (*reports.iterdir(), *evidence.iterdir()))
        self.invoke("--reports", str(reports), "--evidence", str(evidence))
        self.assertEqual(sorted(p.name for p in (*reports.iterdir(), *evidence.iterdir())), listing)


class RollbackReceiptTests(unittest.TestCase):
    """C1-C4 of the optional --rollback-receipt argument, using a real synthetic driver receipt."""

    @classmethod
    def setUpClass(cls):
        # Private 0700 root inside cwd; the sandbox stays alive so mutated receipts re-hash real outputs.
        fixture = tempfile.TemporaryDirectory(dir=Path.cwd())
        cls.addClassCleanup(fixture.cleanup)
        cls.root = Path(fixture.name).resolve()
        cls.count = 0
        sandbox = cls.root / "sandbox"
        sandbox.mkdir(mode=0o700)
        cls.result, cls.sandbox = driver.run(sandbox), sandbox
        cls.finish = datetime.now(timezone.utc).replace(microsecond=0)
        cls.start = cls.finish.replace(second=0)

    def receipt(self, mutate=lambda result: None, engine=ENGINE):
        """Write a schema-valid receipt (mutated before building) and return its path."""
        result = deepcopy(self.result)
        mutate(result)
        built = rr.build(MODULE, result, self.sandbox, self.start, self.finish, engine)
        type(self).count += 1
        out = self.root / f"out{self.count}"
        out.mkdir(mode=0o700)
        return rr.write(built, out)

    def run_cli(self, path, *extra, rows=None):
        folder = tempfile.TemporaryDirectory(dir=self.root)
        self.addCleanup(folder.cleanup)
        reports, evidence = Path(folder.name) / "reports", Path(folder.name) / "evidence"
        reports.mkdir()
        evidence.mkdir()
        argv = ["--reports", str(reports), "--evidence", str(evidence), *extra]
        if path is not None:
            argv += ["--rollback-receipt", str(path)]
        out, err = io.StringIO(), io.StringIO()
        with patch.object(golive_ledger, "build", return_value=rows or all_satisfied()), \
                redirect_stdout(out), redirect_stderr(err):
            code = golive_acceptance.main(argv)
        return code, out.getvalue(), err.getvalue()

    def test_c1_without_flag_output_is_unchanged(self):
        """C1: No flag means the exact historic text and JSON keys, with no rollback line or key.

        Bug caught: always emitting a rollback section (or dropping the drill from the list) when the
        flag is absent, which would silently change every existing consumer's output.
        """
        rows = all_satisfied()
        next(r for r in rows if r["stage"] == "3").update(gate=MISSING)
        code, text, err = self.run_cli(None, rows=rows)
        self.assertEqual((code, err, text), (1, "", GOLDEN_TEXT.format(n=len(PIPELINE))))
        _, raw, _ = self.run_cli(None, "--json", rows=rows)
        data = json.loads(raw)
        self.assertEqual(list(data), ["live", "total", "blockers", "manual", "summary", "verdict"])
        self.assertEqual(data["manual"], list(golive_acceptance.MANUAL))

    def test_c2_pass_receipt_removes_only_the_drill(self):
        """C2/C4: A passed drill on a qualified receipt is shown as machine-checked and leaves the human list.

        Bug caught: reading the receipt but leaving the drill human-only, or dropping other human items.
        """
        path = self.receipt()
        code, text, err = self.run_cli(path)
        self.assertEqual((code, err), (0, ""))
        lines = text.splitlines()
        self.assertIn("Rollback drill (machine-checked): PASS", lines)
        self.assertNotIn("  - rollback drill", lines)
        self.assertEqual([line for line in lines if line.startswith("  - ")], [f"  - {i}" for i in OTHER_HUMAN])
        self.assertEqual(lines[-1], "READY FOR HUMAN ACCEPTANCE")
        _, raw, _ = self.run_cli(path, "--json")
        data = json.loads(raw)
        self.assertEqual(data["rollback"], {"status": "PASS", "codes": []})
        self.assertEqual(data["manual"], OTHER_HUMAN)
        self.assertNotIn("accepted", (text + raw).lower())

    def test_c3_failed_or_unqualified_receipt_is_not_ready_with_no_blockers(self):
        """C3: A drill FAIL or an unqualified receipt makes NOT READY and exit 1 despite zero stage blockers.

        Bug caught: judging readiness from stage blockers alone, so a failed drill still reads READY;
        or treating a drill-PASS receipt that does not qualify (no engine) as a pass.
        """
        broken = self.receipt(lambda result: result["steps"].__delitem__(slice(4, None)))
        unknown_engine = self.receipt(engine=None)
        for label, path, code_name in (("drill failed", broken, "ROLLBACK_STEP_MISSING"),
                                       ("unqualified", unknown_engine, "REHEARSAL_ENGINE_UNKNOWN")):
            with self.subTest(label):
                code, text, err = self.run_cli(path)
                lines = text.splitlines()
                self.assertEqual((code, err), (1, ""))
                line = next(l for l in lines if l.startswith("Rollback drill (machine-checked): FAIL ("))
                self.assertIn(code_name, line)
                self.assertIn("  - rollback drill", lines)
                self.assertEqual(lines[-1], "NOT READY: rollback drill FAIL")
                _, raw, _ = self.run_cli(path, "--json")
                data = json.loads(raw)
                self.assertEqual((data["blockers"], data["rollback"]["status"]), ([], "FAIL"))
                self.assertIn(code_name, data["rollback"]["codes"])
                self.assertEqual(data["verdict"], "NOT READY: rollback drill FAIL")
                self.assertEqual(data["manual"], list(golive_acceptance.MANUAL))

    def test_c3_fail_and_stage_blockers_are_both_counted(self):
        """C3/C4: Stage blockers and a failed drill show together and never read as accepted.

        Bug caught: a failed drill hiding the stage blocker count, or the verdict losing NOT READY.
        """
        rows = all_satisfied()
        next(r for r in rows if r["stage"] == "3").update(gate=MISSING)
        code, text, _ = self.run_cli(self.receipt(engine=None), rows=rows)
        self.assertEqual(code, 1)
        self.assertEqual(text.splitlines()[-1], "NOT READY: 1 blockers, rollback drill FAIL")
        self.assertNotIn("accepted", text.lower())

    def test_c3_unreadable_receipt_exits_two_with_one_message_and_no_verdict(self):
        """C3: A missing, malformed or non-receipt file exits 2 with one stderr line and no stdout.

        Bug caught: letting the error fall through to a verdict (exit 0/1), or leaking the path.
        """
        garbage = self.root / "garbage.json"
        garbage.write_text("{not json", encoding="utf-8")
        wrong = self.root / "wrong.json"
        wrong.write_text("{}", encoding="utf-8")
        for path in (self.root / "missing.json", garbage, wrong):
            with self.subTest(path=path.name):
                code, out, err = self.run_cli(path)
                self.assertEqual((code, out), (2, ""))
                self.assertEqual(err, "golive_acceptance: unreadable or invalid rehearsal receipt\n")


if __name__ == "__main__":
    unittest.main()
