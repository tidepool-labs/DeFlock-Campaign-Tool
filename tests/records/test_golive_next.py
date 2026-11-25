"""Runtime contracts for the read-only go-live plan."""

import io
import json
import re
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timezone
from itertools import permutations
from pathlib import Path
from unittest.mock import patch

from campaign_tool.records import golive_catalog, golive_ledger, golive_next


NOW = datetime(2026, 1, 20, tzinfo=timezone.utc)
FUTURE = "2026-03-01T00:00:00Z"


def receipt(stage, kind, *, stamp="2026-01-19T00:00:00Z", engine="a", result="pass"):
    return dict(schema="records-golive-evidence-v1", stage=stage, kind=kind, result=result,
                recorded_at=stamp, engine=engine * 40, actor="operator", codes=[],
                source=dict(schema="synthetic-v1", sha256="c" * 64, name="synthetic.json"))


# One synthetic case per reason string the ledger can emit: (reason, stage, evidence).
_R, _G = receipt("18", "rehearsal"), receipt("18", "gate")
REASON_CASES = (
    ("REHEARSAL_MISSING", "18", []),
    ("REHEARSAL_FAIL", "18", [receipt("18", "rehearsal", result="fail")]),
    ("STALE", "18", [receipt("18", "rehearsal", stamp="2026-01-05T00:00:00Z")]),
    ("ENGINE_CHANGED", "18", [_R, receipt("1", "live", engine="b")]),
    ("FUTURE_DATED", "18", [receipt("18", "rehearsal", stamp=FUTURE)]),
    ("FUTURE_DATED", "18", [_R, receipt("18", "gate", stamp=FUTURE)]),
    ("FUTURE_DATED", "1", [receipt("1", "live", stamp=FUTURE)]),
    ("GATE_MISSING", "18", [_R]),
    ("GATE_FAIL", "18", [_R, receipt("18", "gate", result="fail")]),
    ("LIVE_MISSING", "18", [_R, _G]),
    ("LIVE_FAILED", "18", [_R, _G, receipt("18", "live", result="fail")]),
    ("AGGREGATE_ONLY", "9", [receipt("9", "rehearsal")]),
    ("NOT_POSITIVE", "8", [receipt("8", "rehearsal")]),
)


class PlanTests(unittest.TestCase):
    def test_c1_order_is_independent_of_input_order(self):
        """C1: Prioritize work by prerequisite and break ties by catalogue order."""
        # The early human/live rows and non-numeric IDs distinguish all sort keys.
        evidence = [receipt("4", "rehearsal"), receipt("15", "rehearsal"), receipt("4", "gate"),
                    receipt("18", "rehearsal"), receipt("A4", "rehearsal")]
        stages = {"1", "4", "9", "10", "15", "17b", "18", "A2", "A4", "A7"}
        rows = [r for r in golive_ledger.build([], evidence, NOW) if r["stage"] in stages]
        expected = [
            dict(stage="9", action="rehearse", owner="pipeline", reason="REHEARSAL_MISSING"),
            dict(stage="10", action="rehearse", owner="pipeline", reason="REHEARSAL_MISSING"),
            dict(stage="A2", action="rehearse", owner="pipeline", reason="REHEARSAL_MISSING"),
            dict(stage="A7", action="rehearse", owner="operator", reason="REHEARSAL_MISSING"),
            dict(stage="15", action="gate", owner="operator", reason="GATE_MISSING"),
            dict(stage="18", action="gate", owner="operator", reason="GATE_MISSING"),
            dict(stage="A4", action="gate", owner="G", reason="GATE_MISSING"),
            dict(stage="1", action="go live", owner="G", reason="LIVE_MISSING"),
            dict(stage="4", action="go live", owner="operator", reason="LIVE_MISSING"),
            dict(stage="17b", action="go live", owner="operator", reason="LIVE_MISSING"),
        ]
        # Reverse and interleave the catalogue, then permute both halves.
        for group in (rows, reversed(rows), rows[1::2] + rows[::2]):
            self.assertEqual(golive_next.plan(group), expected)
        for group in (rows[:5], rows[5:]):
            other = [r for r in rows if r not in group]
            for shuffled in permutations(group):
                self.assertEqual(golive_next.plan([*shuffled, *other]), expected)

    def test_future_c5_every_ledger_reason_has_plan_and_text(self):
        """Future C5: Each reason the ledger can emit plans and has plain text, so none raises KeyError."""
        # A new ledger reason without _REASON_TEXT passes plan() and then breaks the text view.
        emitted = set()
        for reason, stage, evidence in REASON_CASES:
            with self.subTest(reason=reason, stage=stage):
                rows = [r for r in golive_ledger.build([], evidence, NOW) if r["stage"] == stage]
                (item,) = golive_next.plan(rows)
                self.assertEqual(item["reason"], reason)
                self.assertIn(reason, golive_next._REASON_TEXT)
                emitted.add(reason)
        source = Path(golive_ledger.__file__).read_text(encoding="utf-8")
        literals = set(re.findall(r'reason="([A-Z_]+)"', source))
        # Every literal the ledger source can emit has a case above, and no text entry is dead.
        self.assertEqual(literals - emitted, set())
        self.assertEqual(set(golive_next._REASON_TEXT), emitted)
        self.assertEqual(golive_next._REASON_TEXT["FUTURE_DATED"], "the evidence is dated in the future")

    def test_future_c5_future_dated_stage_18_plans_the_first_missing_column(self):
        """Future C5: The reproduced stage-18 case asks for a new rehearsal, not the live step."""
        # Before the fix the ledger said GATED/pass and this plan asked for live proof.
        evidence = [receipt("18", "rehearsal", stamp=FUTURE), receipt("18", "gate", stamp=FUTURE)]
        rows = [r for r in golive_ledger.build([], evidence, NOW) if r["stage"] == "18"]
        self.assertEqual(golive_next.plan(rows),
                         [dict(stage="18", action="rehearse", owner="operator", reason="FUTURE_DATED")])
        control = [receipt("18", "rehearsal"), receipt("18", "gate")]
        rows = [r for r in golive_ledger.build([], control, NOW) if r["stage"] == "18"]
        self.assertEqual(golive_next.plan(rows),
                         [dict(stage="18", action="go live", owner="operator", reason="LIVE_MISSING")])

    def test_c1_only_first_gap_counts_and_live_always_drops_out(self):
        """C1: LIVE wins even with gaps; failed, stale and wrong-engine receipts do not."""
        evidence = [receipt("1", "live"), receipt("3", "live"),
                    receipt("4", "rehearsal", stamp="2026-01-05T00:00:00Z"), receipt("4", "gate"),
                    receipt("9", "rehearsal"), receipt("8", "rehearsal"),
                    receipt("15", "rehearsal"), receipt("15", "gate", engine="b"),
                    receipt("19", "rehearsal", result="fail"),
                    receipt("18", "rehearsal"), receipt("18", "gate", result="fail"),
                    receipt("17b", "live", result="fail")]
        rows = [r for r in golive_ledger.build([], evidence, NOW)
                if r["stage"] in {"1", "3", "4", "8", "9", "15", "17b", "18", "19"}]
        self.assertEqual(golive_next.plan(rows), [
            dict(stage="4", action="rehearse", owner="operator", reason="STALE"),
            dict(stage="19", action="rehearse", owner="operator", reason="REHEARSAL_FAIL"),
            dict(stage="15", action="gate", owner="operator", reason="ENGINE_CHANGED"),
            dict(stage="18", action="gate", owner="operator", reason="GATE_FAIL"),
            dict(stage="8", action="go live", owner="pipeline", reason="NOT_POSITIVE"),
            dict(stage="9", action="go live", owner="pipeline", reason="AGGREGATE_ONLY"),
            dict(stage="17b", action="go live", owner="operator", reason="LIVE_FAILED"),
        ])
        self.assertEqual(golive_next.plan([r for r in rows if r["stage"] in {"1", "3"}]), [])

    def test_c3_human_and_coordinator_work_keeps_its_owner(self):
        """C3: A human-owned rehearsal is not promoted into the fleet group."""
        rows = [r for r in golive_ledger.build([], [], NOW) if r["stage"] in {"1", "3", "5", "9", "A4"}]
        self.assertEqual(golive_next.plan(rows), [
            dict(stage="9", action="rehearse", owner="pipeline", reason="REHEARSAL_MISSING"),
            dict(stage="3", action="rehearse", owner="operator", reason="REHEARSAL_MISSING"),
            dict(stage="5", action="rehearse", owner="flock-coordinator", reason="REHEARSAL_MISSING"),
            dict(stage="A4", action="rehearse", owner="G", reason="REHEARSAL_MISSING"),
            dict(stage="1", action="go live", owner="G", reason="LIVE_MISSING"),
        ])


class CliTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        root = Path(folder.name).resolve()
        self.reports, self.evidence = root / "reports", root / "evidence"
        self.reports.mkdir()
        self.evidence.mkdir()

    def invoke(self, *args):
        run = subprocess.run([sys.executable, "-B", "-m", "campaign_tool.records.golive_next",
                              "--reports", str(self.reports), "--evidence", str(self.evidence), *args],
                             capture_output=True, text=True, check=False)
        return run.returncode, run.stdout, run.stderr

    def test_future_c5_every_reason_renders_as_plain_text(self):
        """Future C5: The numbered text view prints each emitted reason's text without KeyError."""
        for reason, stage, evidence in REASON_CASES:
            with self.subTest(reason=reason, stage=stage):
                rows = [r for r in golive_ledger.build([], evidence, NOW) if r["stage"] == stage]
                out = io.StringIO()
                with patch.object(golive_next.golive_ledger, "build", return_value=rows), redirect_stdout(out):
                    code = golive_next.main(["--reports", str(self.reports), "--evidence", str(self.evidence)])
                self.assertEqual(code, 0)
                label = golive_catalog.STAGES[stage].label
                self.assertRegex(out.getvalue(), rf"^1\. \w[\w-]*: .+ for stage {stage} \({re.escape(label)}\) — "
                                                 rf"{re.escape(golive_next._REASON_TEXT[reason])}\.\n$")

    def test_future_c5_future_dated_evidence_on_disk_prints_plain_text(self):
        """Future C5: A real future-dated receipt gives the plain sentence through the actual CLI."""
        # The CLI reads the real clock, so use a year no clock reaches.
        stamp = "2999-01-01T00:00:00Z"
        path = self.evidence / "29990101T000000Z-18-rehearsal-cccccccc.json"
        path.touch(mode=0o600)
        path.write_text(json.dumps(receipt("18", "rehearsal", stamp=stamp)), encoding="utf-8")
        self.assertEqual(self.invoke("--stage", "18"), (0,
            "1. operator: Rehearse offline for stage 18 (Timers + monitoring) — the evidence is dated in the future.\n",
            ""))
        code, out, err = self.invoke("--stage", "18", "--json")
        self.assertEqual((code, json.loads(out), err), (0, [
            dict(stage="18", action="rehearse", owner="operator", reason="FUTURE_DATED")], ""))

    def test_c2_numbered_plain_text_and_exact_owner_filter(self):
        """C2: Number the filtered list from one and keep human labels intact."""
        self.assertEqual(self.invoke("--owner", "G"), (0,
            "1. G: Rehearse offline for stage A4 (Console access) — a qualified rehearsal is missing.\n"
            "2. G: Record live proof for stage 1 (Request sent) — live proof is missing.\n", ""))
        self.assertEqual(self.invoke("--owner", "g"), (0, "", ""))

    def test_c2_json_filters_and_reads_evidence_from_disk(self):
        """C2: Loading a live receipt removes that stage from the JSON action list."""
        path = self.evidence / "20260119T000000Z-1-live-cccccccc.json"
        path.touch(mode=0o600)
        path.write_text(json.dumps(receipt("1", "live")), encoding="utf-8")
        # With no filters the CLI must still emit the entire ordered plan, not
        # limit it to a default stage or change the row shape or order.
        code, out, err = self.invoke("--json")
        expected = golive_next.plan(golive_ledger.build([], [receipt("1", "live")], NOW))
        self.assertEqual((code, json.loads(out), err), (0, expected, ""))
        code, out, err = self.invoke("--json", "--owner", "G")
        self.assertEqual((code, json.loads(out), err), (0, [
            dict(stage="A4", action="rehearse", owner="G", reason="REHEARSAL_MISSING"),
        ], ""))

        # A known LIVE stage must not reappear when explicitly requested.
        self.assertEqual(self.invoke("--stage", "1", "--json"), (0, "[]\n", ""))
        self.assertEqual(self.invoke("--stage", "1"), (0, "No pending step for stage 1.\n", ""))

    def test_stage_filter_keeps_exact_ids_and_intersects_owner(self):
        for stage, owner, action, reason, text in (
            ("17b", "operator", "go live", "LIVE_MISSING",
             "Record live proof for stage 17b (Public site) — live proof is missing."),
            ("A2", "pipeline", "rehearse", "REHEARSAL_MISSING",
             "Rehearse offline for stage A2 (Console export) — a qualified rehearsal is missing."),
        ):
            with self.subTest(stage=stage):
                # Catch numeric coercion, lost suffixes and filtering only JSON.
                self.assertEqual(self.invoke("--stage", stage), (0, f"1. {owner}: {text}\n", ""))
                # Matching owner alone would return other stages owned by it.
                code, out, err = self.invoke("--stage", stage, "--owner", owner, "--json")
                self.assertEqual((code, json.loads(out), err), (0, [
                    dict(stage=stage, action=action, owner=owner, reason=reason),
                ], ""))
        # Catch OR semantics or ignoring --owner in the presence of --stage.
        self.assertEqual(self.invoke("--stage", "A2", "--owner", "operator", "--json"), (0, "[]\n", ""))
        self.assertEqual(self.invoke("--stage", "A2", "--owner", "operator"),
                         (0, "No pending step for stage A2.\n", ""))

    def test_c2_refuses_invalid_input_in_either_directory(self):
        """C2: An invalid report or receipt cannot produce a partial action list."""
        (self.evidence / "bad.json").write_text("{}", encoding="utf-8")
        self.assertEqual(self.invoke("--json"), (2, "", "golive_next: bad.json: invalid evidence filename\n"))
        (self.reports / "bad.json").write_text("{}", encoding="utf-8")
        self.assertEqual(self.invoke(), (2, "", "golive_next: 'bad.json': invalid report\n"))
        # Even a valid stage cannot bypass full input validation.
        self.assertEqual(self.invoke("--stage", "A2"), (2, "", "golive_next: 'bad.json': invalid report\n"))
        for invalid in ("a2", "99"):
            with self.subTest(invalid=invalid):
                # Catch normalization, unknown-as-empty and loading bad paths
                # before recognizing a usage error.
                code, out, err = self.invoke("--stage", invalid)
                self.assertEqual((code, out), (2, ""))
                self.assertIn("usage:", err)
                self.assertIn(f"argument --stage: invalid choice: {invalid!r}", err)


class CommandsTests(unittest.TestCase):
    """C1-C4: the optional --commands hint names what to run and never changes default output."""

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        root = Path(folder.name).resolve()
        self.args = ["--reports", str(root / "reports"), "--evidence", str(root / "evidence")]
        (root / "reports").mkdir()
        (root / "evidence").mkdir()

    def run_main(self, evidence, *flags):
        # Patch the ledger so the rows are exact; the hints come from the real driver and gate registries.
        rows = golive_ledger.build([], evidence, NOW)
        out, err = io.StringIO(), io.StringIO()
        with patch.object(golive_next.golive_ledger, "build", return_value=rows), \
                redirect_stdout(out), patch.object(sys, "stderr", err):
            code = golive_next.main([*self.args, *flags])
        return code, out.getvalue(), err.getvalue()

    # A qualified stage-4 rehearsal makes the gate the next step for that stage.
    GATE_4 = [receipt("4", "rehearsal")]

    def test_c1_default_text_and_json_are_unchanged_by_the_new_flag(self):
        """C1: Without --commands there is no run: line and no command key.

        Bug caught: hints always printed, which would also change every caller of the plain view.
        """
        code, text, err = self.run_main(self.GATE_4, "--stage", "4")
        self.assertEqual((code, text, err), (0,
            "1. operator: Check the gate for stage 4 (Portal downloads) — a passing gate receipt is missing.\n", ""))
        code, out, err = self.run_main(self.GATE_4, "--stage", "4", "--json")
        self.assertEqual((code, json.loads(out), err), (0, [
            dict(stage="4", action="gate", owner="operator", reason="GATE_MISSING")], ""))
        # plan() is what console Home reads; its rows must never carry a command.
        rows = golive_ledger.build([], [], NOW)
        self.assertTrue(all("command" not in item for item in golive_next.plan(rows)))
        # The hinted text is the default text with one indented line after each step.
        _, plain, _ = self.run_main([], "--owner", "operator")
        _, hinted, _ = self.run_main([], "--owner", "operator", "--commands")
        kept = [line for line in hinted.splitlines() if not line.startswith("   run: ")]
        self.assertEqual("\n".join(kept) + "\n", plain)

    def test_c2_rehearse_names_the_command_only_for_a_covered_stage(self):
        """C2: Stage 4 has a driver; stage 2 says so instead of printing a command that would fail.

        Bug caught: printing `records rehearse --stage 2` for every stage, which the CLI rejects.
        """
        code, out, err = self.run_main([], "--stage", "4", "--commands")
        self.assertEqual((code, err), (0, ""))
        self.assertTrue(out.endswith("\n   run: records rehearse --stage 4 --out <DIR>\n"))
        code, out, err = self.run_main([], "--stage", "2", "--commands")
        self.assertEqual((code, err), (0, ""))
        self.assertTrue(out.endswith("\n   run: no rehearsal driver covers stage 2\n"))

    def test_c2_gate_names_the_covering_pack_or_says_none(self):
        """C2: Stage 4's gate hint names c5-portal-apply; a stage no pack covers says so.

        Bug caught: a hard-coded or off-by-one lookup that returns another stage's gate.
        """
        _, out, _ = self.run_main(self.GATE_4, "--stage", "4", "--commands")
        self.assertTrue(out.endswith("\n   run: gate pack IDs: c5-portal-apply\n"))
        # Every real gate stage has a pack today, so show the uncovered case with a registry that omits stage 4.
        other = {"s5-engine-switch": dict(stages=["15"])}
        with patch("campaign_tool.records.golive_gate_order.registry", return_value=other):
            _, out, _ = self.run_main(self.GATE_4, "--stage", "4", "--commands")
        self.assertTrue(out.endswith("\n   run: no gate pack covers stage 4\n"))

    def test_c2_live_items_point_at_the_evidence_adapter_only(self):
        """C2: A go-live item gets the fixed pointer, with no invented command.

        Bug caught: a static stage-to-adapter table that drifts from the real adapters.
        """
        _, out, _ = self.run_main([], "--stage", "1", "--commands")
        self.assertTrue(out.endswith("\n   run: see the stage's evidence adapter\n"))

    def test_c2_c4_gate_ids_from_two_packs_keep_registry_order(self):
        """C2/C4: A stage covered by two packs lists both IDs, in registry (not sorted) order.

        Bug caught: keeping only the first match, sorting the IDs, or reading a fixed table.
        """
        registry = {"zz-first": dict(stages=["4"]), "other": dict(stages=["15"]), "aa-second": dict(stages=["4", "15"])}
        with patch("campaign_tool.records.golive_gate_order.registry", return_value=registry):
            hints = golive_next.commands([dict(stage="4", action="gate"), dict(stage="15", action="gate")])
        self.assertEqual(hints, ["gate pack IDs: zz-first, aa-second", "gate pack IDs: other, aa-second"])

    def test_c4_a_new_driver_appears_without_editing_the_module(self):
        """C4: The covered-stage set comes from rehearse.discover() on every call.

        Bug caught: a cached or literal stage list that misses a driver added later.
        """
        item = [dict(stage="2", action="rehearse")]
        self.assertEqual(golive_next.commands(item), ["no rehearsal driver covers stage 2"])
        driver = type("Driver", (), {"STAGES": ("2",)})
        with patch("campaign_tool.records.rehearse.discover", return_value=(driver,)):
            self.assertEqual(golive_next.commands(item), ["records rehearse --stage 2 --out <DIR>"])

    def test_c3_json_command_key_exists_only_with_the_flag(self):
        """C3: --json adds one command string per item only with --commands, keeping the other keys.

        Bug caught: adding the key to plan() output, or dropping it in the JSON view.
        """
        _, out, _ = self.run_main([], "--stage", "4", "--json")
        self.assertEqual(json.loads(out), [dict(stage="4", action="rehearse", owner="operator",
                                                reason="REHEARSAL_MISSING")])
        _, out, _ = self.run_main([], "--stage", "4", "--json", "--commands")
        self.assertEqual(json.loads(out), [dict(stage="4", action="rehearse", owner="operator",
                                                reason="REHEARSAL_MISSING",
                                                command="records rehearse --stage 4 --out <DIR>")])

    def test_c4_a_broken_driver_is_a_clean_error_not_a_traceback(self):
        """C4: A driver that fails validation exits 2 with the module's message.

        Bug caught: letting the discover() ValueError escape as a traceback.
        """
        with patch("campaign_tool.records.rehearse.discover", side_effect=ValueError("Invalid rehearsal driver x")):
            self.assertEqual(self.run_main([], "--commands"),
                             (2, "", "golive_next: Invalid rehearsal driver x\n"))
