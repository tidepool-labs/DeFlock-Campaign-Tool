"""Offline acceptance for the go-live acceptance evaluator, using synthetic rows and evidence."""

import copy
import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from campaign_tool.records import golive_acceptance, golive_catalog, golive_ledger

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
        with self.assertRaises(ValueError):
            golive_acceptance.evaluate([dict(satisfied("1"), stage="999")])
        broken = satisfied("3")
        del broken["gate"]
        with self.assertRaises(ValueError):
            golive_acceptance.evaluate([broken])
        with self.assertRaises(ValueError):
            golive_acceptance.evaluate([{"stage": "1"}])

    def test_c2_manual_items_always_present(self):
        """C2: The human-only list is fixed and appears in ready, not-ready and JSON output."""
        expected = ["rollback drill", "FLAWLESS check", "stages 7 and 8 proven on new documents"]
        self.assertEqual(golive_acceptance.evaluate([])["manual"], expected)
        self.assertEqual(golive_acceptance.evaluate(all_satisfied())["manual"], expected)
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


if __name__ == "__main__":
    unittest.main()
