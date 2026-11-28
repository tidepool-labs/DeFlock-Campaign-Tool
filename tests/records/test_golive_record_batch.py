"""Contracts C1-C4 of the one-pass go-live receipt recorder (synthetic receipts only)."""

from datetime import datetime, timedelta, timezone
import errno
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from campaign_tool.records import (
    golive_evidence, golive_gate, golive_record, golive_record_batch as batch, rehearsal_receipt,
)

NOW = datetime(2026, 1, 5, 20, 30, tzinfo=timezone.utc)
LATER = NOW + timedelta(minutes=5)
ENGINE = "ab" * 20
CLOSED = dict(type="window", label="Closed", days=["tue"], start="11:00", end="13:00")  # NOW is a Monday


class BatchTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory(dir=Path.cwd())))
        self.receipts = self.root / "receipts"
        self.evidence = self.root / "evidence"
        self.receipts.mkdir()
        self.evidence.mkdir()
        (self.root / rehearsal_receipt.SANDBOX_MARKER).touch(mode=0o600)

    def rehearsal(self, stages=("18",), time=NOW, engine=ENGINE, **overrides):
        outcome = dict(stages=list(stages), steps=[dict(name="observe", code="OK", outputs=[])],
                       manual_steps=[], codes=[])
        outcome.update(overrides)
        return rehearsal_receipt.build("fake_driver", outcome, self.root, time, time, engine)

    def gate(self, stages=("18",), checks=None, now=NOW, gate_id="sample-gate"):
        return golive_gate.evaluate({
            "schema": "records-golive-gate-spec-v1", "gate_id": gate_id, "title": "Sample",
            "stages": list(stages),
            "checks": checks or [dict(type="window", label="Window", days=list(golive_gate.DAYS),
                                      start="00:00", end="23:59")],
        }, {}, now)

    def put(self, rel, document):
        path = self.receipts / rel
        path.parent.mkdir(exist_ok=True)
        raw = document if isinstance(document, bytes) else (json.dumps(document, indent=1) + "\n").encode()
        path.write_bytes(raw)
        return path

    def rows(self):
        return golive_evidence.load(self.evidence)

    def kinds(self):
        return {c.name: c.kind for c in batch.scan(self.receipts)}

    def run_batch(self, now=LATER, **kwargs):
        return batch.record_all(self.receipts, self.evidence, now, **kwargs)

    # C1 -----------------------------------------------------------------------------------
    def test_c1_scan_depth_hidden_and_order(self):
        """C1: Only root and one subdirectory level count; hidden/non-JSON skipped; sorted.

        Bug caught: recursing deeper, reading dotfiles or scratch files, or listing in disk order.
        """
        gate = self.gate()
        for rel in ("z.json", "b/y.json", "a/x.json", "a/.hidden.json", ".h/x.json", "a/b/deep.json"):
            (self.receipts / rel).parent.mkdir(parents=True, exist_ok=True)
            (self.receipts / rel).write_text(json.dumps(gate))
        (self.receipts / "notes.txt").write_text("{}")
        (self.receipts / "link-dir").symlink_to(self.receipts / "a")
        self.assertEqual([c.name for c in batch.scan(self.receipts)], ["a/x.json", "b/y.json", "z.json"])

    def test_c1_classifies_by_schema_and_ignores_other_json(self):
        """C1: Rehearsal and gate by top-level schema; any other valid JSON is ignored, silently.

        Bug caught: treating every .json (evidence rows, lists, outputs) as a receipt to refuse.
        """
        self.put("1-rehearsal.json", self.rehearsal())
        self.put("2-gate.json", self.gate())
        self.put("3-other.json", {"schema": "something-else"})
        self.put("4-list.json", [1, 2])
        self.put("5-no-schema.json", {"a": 1})
        self.put("6-bad-schema-type.json", {"schema": ["records-golive-gate-v1"]})
        self.put("7-evidence.json", {"schema": "records-golive-evidence-v1"})
        self.assertEqual(self.kinds(), {
            "1-rehearsal.json": "rehearsal", "2-gate.json": "gate", "3-other.json": "ignored",
            "4-list.json": "ignored", "5-no-schema.json": "ignored",
            "6-bad-schema-type.json": "ignored", "7-evidence.json": "ignored"})
        report = self.run_batch()
        self.assertEqual((len(report.recorded), report.already_recorded, report.refused, report.ignored),
                         (2, 0, [], 5))

    def test_c1_symlink_oversize_corrupt_and_special_files_refused(self):
        """C1: A link, over-1-MiB, corrupt, duplicate-key or non-regular file is refused by name.

        Bug caught: following a link out of the folder, reading an unbounded file, or letting a
        file the operator expected to be recorded vanish without a trace.
        """
        outside = self.root / "outside.json"
        outside.write_text(json.dumps(self.gate()))
        (self.receipts / "link.json").symlink_to(outside)
        self.put("big.json", b" " * (batch.MAX_BYTES + 1))
        self.put("corrupt.json", b"{not json")
        self.put("dup.json", b'{"schema": "a", "schema": "b"}')
        self.put("binary.json", b"\xff\xfe")
        os.mkfifo(self.receipts / "fifo.json")
        edge = b'{"schema": "other"}'
        self.put("edge.json", edge + b" " * (batch.MAX_BYTES - len(edge)))  # exactly 1 MiB
        found = {c.name: (c.kind, c.message) for c in batch.scan(self.receipts)}
        self.assertEqual(found, {
            "big.json": ("refused", "receipt exceeds 1 MiB"),
            "binary.json": ("refused", "invalid receipt JSON"),
            "corrupt.json": ("refused", "invalid receipt JSON"),
            "dup.json": ("refused", "invalid receipt JSON"),
            "edge.json": ("ignored", ""),
            "fifo.json": ("refused", "not a regular file"),
            "link.json": ("refused", "symlink is not followed"),
        })
        report = self.run_batch()
        self.assertEqual([name for name, _ in report.refused],
                         ["big.json", "binary.json", "corrupt.json", "dup.json", "fifo.json", "link.json"])
        self.assertEqual((report.recorded, report.ignored), ([], 1))

    def test_c1_directory_named_json_is_refused_and_still_traversed(self):
        """C1: A folder named *.json is refused by name (not silently skipped) and still descended.

        Bug caught: `x.json/` vanishing from the report because directory entries were skipped
        before the *.json check, or the refusal stopping the immediate-subdirectory traversal.
        """
        self.put("top.json", self.gate())
        self.put("directory.json/inner.json", self.gate(gate_id="inner-gate"))
        (self.receipts / "a" / "nested.json").mkdir(parents=True)
        (self.receipts / "a" / "nested.json" / "deeper.json").write_text(json.dumps(self.gate()))
        (self.receipts / "plain-dir").mkdir()
        (self.receipts / ".hidden.json").mkdir()
        self.assertEqual(self.kinds(), {
            "a/nested.json": "refused", "directory.json": "refused",
            "directory.json/inner.json": "gate", "top.json": "gate"})
        report = self.run_batch()
        self.assertEqual(report.refused, [("a/nested.json", "not a regular file"),
                                          ("directory.json", "not a regular file")])
        self.assertEqual(len(report.recorded), 2)

    def test_c1_candidate_cap_and_unusable_root(self):
        """C1: More than 500 files stops the run before any write; an unusable root is refused.

        Bug caught: silently dropping the files past the cap, or following a symlinked root.
        """
        with patch.object(batch, "MAX_FILES", 2):
            for i in range(2):
                self.put(f"{i}.json", {"schema": "x"})
            self.assertEqual(len(batch.scan(self.receipts)), 2)
            self.put("2.json", self.gate())
            with self.assertRaisesRegex(ValueError, "more than 2 receipt files"):
                self.run_batch()
        self.assertEqual(list(self.evidence.iterdir()), [])
        (self.root / "file").touch()
        (self.root / "linked").symlink_to(self.receipts)
        for bad in (self.root / "missing", self.root / "file", self.root / "linked"):
            with self.subTest(bad=bad.name), self.assertRaisesRegex(ValueError, "unusable receipts directory"):
                batch.scan(bad)

    # C2 -----------------------------------------------------------------------------------
    def test_c2_single_stage_rehearsal_one_row(self):
        """C2: A single-stage rehearsal yields one row bound to its stage and bytes."""
        path = self.put("one.json", self.rehearsal(("18",)))
        report = self.run_batch()
        row, = self.rows()
        self.assertEqual((row["stage"], row["kind"], row["result"], row["source"]["name"]),
                         ("18", "rehearsal", "pass", "one.json"))
        self.assertEqual(row, golive_record.from_rehearsal(path, "18", LATER))
        self.assertEqual((len(report.recorded), report.already_recorded, report.refused), (1, 0, []))

    def test_c2_multi_stage_rehearsal_one_row_per_declared_stage(self):
        """C2: A 13/14 rehearsal records both stages, not just the first or an ambiguity error.

        Bug caught: calling from_rehearsal with stage=None, which refuses every multi-stage receipt.
        """
        self.put("drafts.json", self.rehearsal(("13", "14")))
        report = self.run_batch()
        self.assertEqual(sorted((r["stage"], r["kind"], r["result"]) for r in self.rows()),
                         [("13", "rehearsal", "pass"), ("14", "rehearsal", "pass")])
        self.assertEqual((len(report.recorded), report.refused), (2, []))

    def test_c2_gate_go_and_no_go_record_every_row(self):
        """C2: GO and NO-GO gates are both evidence, one row per distinct stage; NO-GO is not refused."""
        self.put("go.json", self.gate(("19", "18", "19"), gate_id="go-gate"))
        self.put("nogo.json", self.gate(("13",), checks=[CLOSED], gate_id="nogo-gate"))
        report = self.run_batch()
        self.assertEqual(sorted((r["stage"], r["result"], r["codes"]) for r in self.rows()),
                         [("13", "fail", ["GATE_WINDOW_CLOSED"]), ("18", "pass", []), ("19", "pass", [])])
        self.assertEqual((len(report.recorded), report.refused), (3, []))

    def test_c2_unqualified_rehearsal_is_recorded_as_fail_not_refused(self):
        """C2: A well-formed but unqualified rehearsal is truthful fail evidence, not a refusal."""
        self.put("noengine.json", self.rehearsal(engine=None))
        report = self.run_batch()
        row, = self.rows()
        self.assertEqual((row["result"], row["codes"], report.refused),
                         ("fail", ["REHEARSAL_ENGINE_UNKNOWN"], []))

    def test_c2_second_run_records_nothing(self):
        """C2: Rerunning later (new clock, a renamed copy) is already_recorded, never a duplicate row.

        Bug caught: identity depending on the recording timestamp or file name.
        """
        path = self.put("drafts.json", self.rehearsal(("13", "14")))
        self.put("go.json", self.gate())
        first = self.run_batch(NOW + timedelta(minutes=1))
        (self.receipts / "copy.json").write_bytes(path.read_bytes())
        before = sorted(p.name for p in self.evidence.iterdir())
        second = self.run_batch(NOW + timedelta(minutes=9))
        self.assertEqual((len(first.recorded), first.already_recorded), (3, 0))
        self.assertEqual((second.recorded, second.already_recorded, second.refused), ([], 5, []))
        self.assertEqual(sorted(p.name for p in self.evidence.iterdir()), before)

    def test_c2_partially_recorded_receipt_completes_missing_stage(self):
        """C2: With stage 13 already on file, a rerun adds only stage 14."""
        path = self.put("drafts.json", self.rehearsal(("13", "14")))
        golive_record.append(golive_record.from_rehearsal(path, "13", NOW), self.evidence)
        report = self.run_batch()
        self.assertEqual((len(report.recorded), report.already_recorded), (1, 1))
        self.assertEqual(sorted(r["stage"] for r in self.rows()), ["13", "14"])

    def test_c2_refused_files_name_the_module_message_and_batch_continues(self):
        """C2/C4: Each bad receipt is refused with its own message; good ones before and after record.

        Bug caught: stopping at the first problem (the behaviour of golive_record itself).
        """
        forged = dict(self.rehearsal(engine=None), qualified=True, qualify_codes=[])
        self.put("a-forged.json", forged)
        self.put("b-good.json", self.gate(gate_id="b-good"))
        self.put("c-future.json", self.gate(now=LATER + timedelta(hours=1), gate_id="c-future"))
        self.put("d-verdict.json", dict(self.gate(gate_id="d-verdict"), verdict="NO-GO"))
        self.put("e-missing-stage.json", dict(self.rehearsal(), stages=[]))
        self.put("f-good.json", self.rehearsal(("19",)))
        report = self.run_batch()
        self.assertEqual(report.refused, [
            ("a-forged.json", "qualification_inconsistent"),
            ("c-future.json", "gate timestamp is in the future"),
            ("d-verdict.json", "inconsistent gate verdict"),
            ("e-missing-stage.json", "rehearsal stages must be a nonempty list of nonblank strings"),
        ])
        self.assertEqual(sorted((r["stage"], r["kind"]) for r in self.rows()),
                         [("18", "gate"), ("19", "rehearsal")])

    def test_c2_unexpected_failure_in_one_file_does_not_stop_the_rest(self):
        """C2: Even an unforeseen exception on one receipt is a refusal of that file only."""
        self.put("a.json", self.gate(gate_id="a-gate"))
        self.put("b.json", self.rehearsal())
        with patch.object(golive_record, "from_gate", side_effect=KeyError("boom")):
            report = self.run_batch()
        self.assertEqual(report.refused, [("a.json", "unexpected KeyError")])
        self.assertEqual([r["kind"] for r in self.rows()], ["rehearsal"])

    # C3 -----------------------------------------------------------------------------------
    def cli(self, *args, receipts=None, evidence=None):
        cmd = [sys.executable, "-B", "-m", "campaign_tool.records.golive_record_batch",
               "--receipts", str(receipts or self.receipts), "--evidence", str(evidence or self.evidence), *args]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30, check=False)
        return result.returncode, result.stdout.splitlines(), result.stderr

    def fresh(self):
        # The CLI judges freshness against the real clock.
        now = datetime.now(timezone.utc)
        self.put("drafts.json", self.rehearsal(("13", "14"), time=now))
        self.put("go.json", self.gate(now=now))
        self.put("nogo.json", self.gate(("19",), checks=[CLOSED], now=now, gate_id="nogo-gate"))
        self.put("other.json", {"schema": "other"})

    def test_c3_cli_records_then_second_run_records_zero(self):
        """C3: Names for each new row, a summary line, exit 0 (NO-GO rows included); rerun is a no-op."""
        self.fresh()
        status, out, err = self.cli()
        names = sorted(p.name for p in self.evidence.iterdir())
        self.assertEqual((status, err, sorted(out[:-1]), out[-1]),
                         (0, "", names, "recorded=4 already_recorded=0 refused=0 ignored=1"))
        status, out, err = self.cli()
        self.assertEqual((status, err, out), (0, "", ["recorded=0 already_recorded=4 refused=0 ignored=1"]))
        self.assertEqual(sorted(p.name for p in self.evidence.iterdir()), names)

    def test_c3_cli_dry_run_writes_nothing(self):
        """C3: --dry-run validates and classifies but leaves the evidence directory empty."""
        self.fresh()
        self.put("bad.json", b"{")
        status, out, err = self.cli("--dry-run")
        self.assertEqual(list(self.evidence.iterdir()), [])
        self.assertEqual(status, 1)
        self.assertEqual(err, "golive_record_batch: refused bad.json: invalid receipt JSON\n")
        self.assertEqual(out[-1], "dry-run: recorded=0 would_record=4 already_recorded=0 refused=1 ignored=1")
        self.assertEqual(len([line for line in out if line.startswith("would-record ")]), 4)

    def test_c3_cli_dry_run_sees_existing_evidence(self):
        """C3: A dry run after a real run reports already_recorded, so it predicts the real rerun."""
        self.fresh()
        self.assertEqual(self.cli()[0], 0)
        before = sorted(p.name for p in self.evidence.iterdir())
        self.assertEqual(self.cli("--dry-run")[1],
                         ["dry-run: recorded=0 would_record=0 already_recorded=4 refused=0 ignored=1"])
        self.assertEqual(sorted(p.name for p in self.evidence.iterdir()), before)

    def test_c3_dry_run_models_evidence_directory_capacity(self):
        """C3: A dry run refuses what a real run would refuse once the evidence directory is full.

        Bug caught: the dry run tracking only row identities, predicting would-record rows (exit 0)
        for a directory `append` rejects with "evidence directory is full" - any entry counts.
        """
        self.put("a.json", self.rehearsal(("13", "14")))
        self.put("b.json", self.gate(("19",), gate_id="b-gate"))
        for limit, seeded, expected_rows in ((3, 1, 2), (1, 1, 0)):  # partially full, then full
            with self.subTest(limit=limit), patch.object(golive_evidence, "_MAX_FILES", limit):
                reports = []
                for dry_run in (True, False):
                    evidence = self.root / f"evidence-{limit}-{dry_run}"
                    evidence.mkdir()
                    (evidence / "notes.txt").write_text("not json")  # counts against capacity
                    reports.append(batch.record_all(self.receipts, evidence, LATER, dry_run=dry_run))
                    self.assertEqual(len(list(evidence.iterdir())), seeded + (0 if dry_run else expected_rows))
                dry, real = reports
                self.assertEqual(dry, real)
                self.assertEqual(len(real.recorded), expected_rows)
                self.assertTrue(real.refused)
                self.assertTrue(all(message.endswith("evidence directory is full") for _, message in real.refused))

    def test_c3_cli_exit_1_when_refused_but_others_recorded(self):
        """C3: A refusal exits 1 yet the good receipts are on file; the message names the file."""
        self.fresh()
        self.put("a-corrupt.json", b"[")
        status, out, err = self.cli()
        self.assertEqual((status, err), (1, "golive_record_batch: refused a-corrupt.json: invalid receipt JSON\n"))
        self.assertEqual(out[-1], "recorded=4 already_recorded=0 refused=1 ignored=1")
        self.assertEqual(len(self.rows()), 4)

    def test_c3_cli_exit_2_for_unusable_directories(self):
        """C3: A missing or file-valued receipts/evidence directory exits 2 and writes nothing."""
        self.fresh()
        (self.root / "file").touch()
        for kwargs, message in (
            (dict(receipts=self.root / "missing"), "unusable receipts directory"),
            (dict(receipts=self.root / "file"), "unusable receipts directory"),
            (dict(evidence=self.root / "missing"), "unusable evidence directory"),
            (dict(evidence=self.root / "file"), "unusable evidence directory"),
        ):
            with self.subTest(kwargs=kwargs):
                self.assertEqual(self.cli(**kwargs), (2, [], f"golive_record_batch: {message}\n"))
        (self.evidence / "stray.json").write_text("{}")
        self.assertEqual(self.cli(), (2, [], "golive_record_batch: unusable evidence directory\n"))
        self.assertEqual([p.name for p in self.evidence.iterdir()], ["stray.json"])

    # C4 -----------------------------------------------------------------------------------
    def test_c4_results_do_not_depend_on_listing_order(self):
        """C4: Reversed directory listings give the same scan, report and evidence names."""
        self.put("b.json", self.rehearsal(("13", "14")))
        self.put("a/x.json", self.gate(gate_id="x-gate"))
        self.put("c/y.json", self.gate(("19",), gate_id="y-gate"))
        self.put("z.json", {"schema": "other"})
        real = os.scandir

        class Reversed:
            def __init__(self, *args, **kwargs):
                self.inner = real(*args, **kwargs)

            def __enter__(self):
                return reversed(list(self.inner.__enter__()))

            def __exit__(self, *exc):
                return self.inner.__exit__(*exc)

        normal = batch.scan(self.receipts)
        with patch.object(batch.os, "scandir", Reversed):
            flipped = batch.scan(self.receipts)
        self.assertEqual(normal, flipped)
        self.assertEqual([c.name for c in normal], ["a/x.json", "b.json", "c/y.json", "z.json"])
        other = self.root / "evidence2"
        other.mkdir()
        first = self.run_batch()
        with patch.object(batch.os, "scandir", Reversed):
            second = batch.record_all(self.receipts, other, LATER)
        self.assertEqual(first, second)

    def test_c4_failed_append_leaves_no_partial_file_and_rerun_completes(self):
        """C4: A publish failure mid-receipt leaves no scratch/partial row; a rerun heals it.

        Bug caught: a write path that stops the batch or leaves a half-written evidence file.
        """
        self.put("a.json", self.rehearsal(("13", "14")))
        self.put("b.json", self.gate(("19",), gate_id="b-gate"))
        publish = golive_record._rename_new

        def fail_stage_14(dir_fd, scratch, name):
            if "-14-" in name:
                raise OSError(errno.ENOSPC, os.strerror(errno.ENOSPC), name)
            publish(dir_fd, scratch, name)

        with patch.object(golive_record, "_rename_new", side_effect=fail_stage_14):
            report = self.run_batch()
        self.assertEqual(report.refused, [("a.json", "stage 14: No space left on device")])
        self.assertEqual(sorted(r["stage"] for r in self.rows()), ["13", "19"])
        self.assertTrue(all(not p.name.startswith(".") for p in self.evidence.iterdir()))
        healed = self.run_batch(LATER + timedelta(minutes=1))
        self.assertEqual((len(healed.recorded), healed.already_recorded, healed.refused), (1, 2, []))
        self.assertEqual(sorted(r["stage"] for r in self.rows()), ["13", "14", "19"])

    def test_c4_refused_multi_stage_receipt_writes_no_row_at_all(self):
        """C4: A receipt with one stage the evidence schema rejects writes none of its stages."""
        self.put("a.json", self.rehearsal(("13", "unknown-stage")))
        self.put("b.json", self.rehearsal(("18",)))
        report = self.run_batch()
        self.assertEqual([name for name, _ in report.refused], ["a.json"])
        self.assertEqual([r["stage"] for r in self.rows()], ["18"])

    def test_c4_receipt_changed_after_scan_is_refused(self):
        """C4: Bytes swapped between classification and recording are not recorded under old hash."""
        path = self.put("a.json", self.gate())
        real_scan = batch.scan

        def scan_then_swap(root):
            found = real_scan(root)
            path.write_text(json.dumps(self.gate(gate_id="swapped-gate")))
            return found

        with patch.object(batch, "scan", side_effect=scan_then_swap):
            report = self.run_batch()
        self.assertEqual(report.refused, [("a.json", "receipt changed while it was being recorded")])
        self.assertEqual(self.rows(), [])


if __name__ == "__main__":
    unittest.main()
