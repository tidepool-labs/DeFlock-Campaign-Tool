"""Runtime contracts for the read-only go-live plan."""

import json
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from itertools import permutations
from pathlib import Path

from campaign_tool.records import golive_ledger, golive_next


NOW = datetime(2026, 1, 20, tzinfo=timezone.utc)


def receipt(stage, kind, *, stamp="2026-01-19T00:00:00Z", engine="a", result="pass"):
    return dict(schema="records-golive-evidence-v1", stage=stage, kind=kind, result=result,
                recorded_at=stamp, engine=engine * 40, actor="operator", codes=[],
                source=dict(schema="synthetic-v1", sha256="c" * 64, name="synthetic.json"))


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
        code, out, err = self.invoke("--json", "--owner", "G")
        self.assertEqual((code, json.loads(out), err), (0, [
            dict(stage="A4", action="rehearse", owner="G", reason="REHEARSAL_MISSING"),
        ], ""))

    def test_c2_refuses_invalid_input_in_either_directory(self):
        """C2: An invalid report or receipt cannot produce a partial action list."""
        (self.evidence / "bad.json").write_text("{}", encoding="utf-8")
        self.assertEqual(self.invoke("--json"), (2, "", "golive_next: bad.json: invalid evidence filename\n"))
        (self.reports / "bad.json").write_text("{}", encoding="utf-8")
        self.assertEqual(self.invoke(), (2, "", "golive_next: 'bad.json': invalid report\n"))
