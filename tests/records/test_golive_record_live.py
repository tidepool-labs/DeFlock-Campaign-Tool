"""Recording adapter live rows as go-live evidence (golive_record live)."""

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest

from campaign_tool.records import golive_evidence, golive_ledger


PAST = "2026-01-05T20:30:00Z"


def row(stage, *, result="pass", codes=(), kind="live", actor="pipeline", recorded_at=PAST, name="out.json"):
    return {"schema": "records-golive-evidence-v1", "stage": stage, "kind": kind, "result": result,
            "recorded_at": recorded_at, "engine": None, "actor": actor, "codes": list(codes),
            "source": {"schema": "records-agency-factsheets-v1",
                       "sha256": hashlib.sha256(f"{stage}{name}".encode()).hexdigest(), "name": name}}


class GoLiveRecordLiveTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory(dir=Path.cwd())))
        self.evidence = self.root / "golive-evidence"
        self.evidence.mkdir()

    def save(self, value, name="adapter.json"):
        path = self.root / name
        path.write_text(value if isinstance(value, str) else json.dumps(value), encoding="utf-8")
        return path

    def cli(self, path, *args):
        result = subprocess.run([sys.executable, "-B", "-m", "campaign_tool.records.golive_record",
                                 "live", str(path), "--evidence", str(self.evidence), *args],
                                capture_output=True, text=True, timeout=10, check=False)
        return result.returncode, result.stdout.splitlines(), result.stderr

    def test_records_every_row_private_and_reader_compatible(self):
        rows = [row("9"), row("10")]
        status, names, err = self.cli(self.save(rows))
        self.assertEqual((status, err), (0, ""))
        self.assertEqual(sorted(names), sorted(golive_evidence._filename(r) for r in rows))
        for path in self.evidence.iterdir():
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(sorted(golive_evidence.load(self.evidence), key=lambda r: r["stage"]),
                         sorted(rows, key=lambda r: r["stage"]))

    def test_stage_selects_one_row_or_refuses(self):
        path = self.save([row("11"), row("12", result="fail", codes=["REGISTER_FILE_REGISTER_RUN"])])
        self.assertEqual(self.cli(path, "--stage", "9"),
                         (2, [], "golive_record: stage is not declared in the live input\n"))
        self.assertEqual(list(self.evidence.iterdir()), [])
        status, names, err = self.cli(path, "--stage", "11")
        self.assertEqual((status, len(names), err), (0, 1, ""))
        self.assertEqual([r["stage"] for r in golive_evidence.load(self.evidence)], ["11"])

    def test_fail_row_written_with_exit_1_and_blocks_the_stage(self):
        status, names, err = self.cli(self.save([row("12", result="fail", codes=["REGISTER_FILE_REGISTER_RUN"])]))
        self.assertEqual((status, len(names), err), (1, 1, ""))
        rows = golive_ledger.build([], golive_evidence.load(self.evidence), datetime.now(timezone.utc))
        stage12, = [r for r in rows if r["stage"] == "12"]
        self.assertEqual((stage12["state"], stage12["live"]["status"]), ("BLOCKED", "missing"))

    def test_replay_is_a_duplicate_refusal(self):
        path = self.save([row("9")])
        self.assertEqual(self.cli(path)[0], 0)
        self.assertEqual(self.cli(path), (2, [], "golive_record: EVIDENCE_DUPLICATE\n"))
        self.assertEqual(len(list(self.evidence.iterdir())), 1)

    def test_live_row_is_proof_only_not_a_go_live(self):
        # Stage 9 has live proof but no rehearsal: LIVE with a gap under Next.
        self.assertEqual(self.cli(self.save([row("9")]))[0], 0)
        rows = golive_ledger.build([], golive_evidence.load(self.evidence), datetime.now(timezone.utc))
        stage9, = [r for r in rows if r["stage"] == "9"]
        self.assertEqual((stage9["state"], stage9["live"]["status"], stage9["next"]), ("LIVE", "evidence", "rehearsal"))

    def test_refusals_write_nothing(self):
        extra = dict(row("10"), note="x")
        cases = {
            "object": ({"rows": [row("9")]}, "live input must be a non-empty JSON array of evidence rows"),
            "empty": ([], "live input must be a non-empty JSON array of evidence rows"),
            "rehearsal kind": ([row("9"), row("10", kind="rehearsal", actor="rehearsal")],
                               "live input row is not kind live"),
            "gate kind": ([row("9", kind="gate", actor="operator")], "live input row is not kind live"),
            "rehearsal actor": ([row("9", actor="rehearsal")], "live input row has a rehearsal actor"),
            "future": ([row("9"), row("10", recorded_at="2099-01-01T00:00:00Z")], "live input row is future-dated"),
            "pass with codes": ([row("9", codes=["SOMETHING_WRONG"])], "live input pass row carries failure codes"),
            "stage twice": ([row("9"), row("9", name="other.json")], "live input names a stage twice"),
            "invalid row after a valid one": ([row("9"), extra], "$: unexpected field"),
            "too many rows": ([row("9", name=f"o{i}.json") for i in range(29)], "live input has more than 28 rows"),
            "not JSON": ("[{", "invalid receipt JSON"),
            "duplicate key": ('[{"stage": "9", "stage": "10"}]', "invalid receipt JSON"),
        }
        for label, (value, message) in cases.items():
            with self.subTest(label):
                self.assertEqual(self.cli(self.save(value)), (2, [], f"golive_record: {message}\n"))
                self.assertEqual(list(self.evidence.iterdir()), [])

    def test_oversized_later_row_refused_before_any_write(self):
        # Both rows pass the schema; the second is over the writer's 64 KiB
        # limit once serialized. Nothing may be written, with or without
        # --stage selecting only the first row.
        big = row("10", name="o" * (golive_evidence._MAX_SIZE + 1))
        golive_evidence.validate(big)
        path = self.save([row("9"), big])
        for args in ((), ("--stage", "9")):
            with self.subTest(args=args):
                self.assertEqual(self.cli(path, *args), (2, [], "golive_record: evidence file exceeds 64 KiB\n"))
                self.assertEqual(list(self.evidence.iterdir()), [])

    def test_symlinked_input_refused(self):
        target = self.save([row("9")])
        link = self.root / "link.json"
        link.symlink_to(target)
        status, names, err = self.cli(link)
        self.assertEqual((status, names), (2, []))
        self.assertTrue(err.startswith("golive_record: "))
        self.assertEqual(list(self.evidence.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
