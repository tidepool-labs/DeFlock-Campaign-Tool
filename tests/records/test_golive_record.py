"""Runtime contracts for hash-linked go-live evidence recording."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

from campaign_tool.records import golive_evidence, golive_gate, golive_record, rehearsal_receipt


NOW = datetime(2026, 1, 5, 20, 30, tzinfo=timezone.utc)
ENGINE = "ab" * 20


class GoLiveRecordTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory(dir=Path.cwd())))
        self.evidence = self.root / "golive-evidence"
        self.evidence.mkdir()
        (self.root / rehearsal_receipt.SANDBOX_MARKER).touch(mode=0o600)

    def save(self, receipt):
        path = self.root / "receipt.json"
        # Use noncanonical bytes to catch hashing of reserialized dictionaries.
        path.write_bytes((json.dumps(receipt, indent=1) + "\r\n").encode())
        return path

    def expected(self, *, stage="18", kind="gate", result="pass", codes=(), engine=None):
        return {
            "schema": "records-golive-evidence-v1", "stage": stage, "kind": kind,
            "result": result, "recorded_at": "2026-01-05T20:30:00Z", "engine": engine,
            "source": {
                "schema": "records-golive-gate-v1" if kind == "gate" else "records-rehearsal-v1",
                "sha256": hashlib.sha256((self.root / "receipt.json").read_bytes()).hexdigest(),
                "name": "receipt.json",
            },
            "actor": "operator" if kind == "gate" else "rehearsal", "codes": list(codes),
        }

    def rehearsal(self, *, time=NOW, engine: str | None = ENGINE, **overrides):
        outcome = dict(stages=["18"], steps=[dict(name="observe", code="OK", outputs=[])],
                       manual_steps=[], codes=["SYNTHETIC_NOTE"])
        outcome.update(overrides)
        return rehearsal_receipt.build("fake_driver", outcome, self.root, time, time, engine)

    def gate(self, *, stages=("18",), checks=None, now=NOW):
        return golive_gate.evaluate({
            "schema": "records-golive-gate-spec-v1", "gate_id": "sample-gate", "title": "Sample",
            "stages": list(stages),
            "checks": checks or [dict(type="window", label="Window", days=list(golive_gate.DAYS),
                                      start="00:00", end="23:59")],
        }, {}, now)

    def test_c1_qualified_receipt(self):
        """C1: A fresh qualified rehearsal passes; bind the bytes, stage and engine."""
        path = self.save(self.rehearsal(engine=ENGINE.upper()))
        clock = NOW.astimezone(timezone(timedelta(hours=-8)))
        self.assertEqual(golive_record.from_rehearsal(path, None, clock),
                         self.expected(kind="rehearsal", engine=ENGINE))

    def test_c1_requalifies_at_recording_clock(self):
        """C1: A cached pass cannot mask a stale or future finish at recording time."""
        for time in (NOW - timedelta(days=14, seconds=1), NOW + timedelta(seconds=1)):
            with self.subTest(time=time):
                receipt = self.rehearsal(time=time)
                self.assertIs(receipt["qualified"], True)
                self.assertEqual(golive_record.from_rehearsal(self.save(receipt), "18", NOW),
                                 self.expected(kind="rehearsal", engine=ENGINE, result="fail",
                                               codes=["REHEARSAL_STALE"]))

    def test_c1_nonqualified_receipt_stays_fail(self):
        """C1: A driver pass or missing target context cannot erase qualification failures."""
        cases = [
            (self.rehearsal(engine=None, steps=[]), None,
             ["REHEARSAL_NO_STEPS", "REHEARSAL_ENGINE_UNKNOWN"]),
            (self.rehearsal(manual_steps=["Observe"], steps=[dict(name="bad", code="FAILED", outputs=[])]),
             ENGINE, ["REHEARSAL_FAILED_STEP", "REHEARSAL_MANUAL_STEP"]),
            (dict(self.rehearsal(), qualified=False, qualify_codes=["REHEARSAL_ENGINE_MISMATCH"]),
             ENGINE, ["REHEARSAL_ENGINE_MISMATCH"]),
        ]
        for receipt, engine, codes in cases:
            with self.subTest(codes=codes):
                self.assertEqual(golive_record.from_rehearsal(self.save(receipt), "18", NOW),
                                 self.expected(kind="rehearsal", engine=engine, result="fail", codes=codes))

    def test_c1_receipt_or_stage_refused(self):
        """C1: Reject a forged qualification, unbound stage, or ambiguous inference."""
        for receipt, stage, reason in (
            (dict(self.rehearsal(engine=None), qualified=True, qualify_codes=[]), "18", "qualification_inconsistent"),
            (self.rehearsal(), "19", "not declared"),
            (self.rehearsal(stages=["18", "19"]), None, "--stage is required"),
        ):
            with self.subTest(reason=reason), self.assertRaisesRegex(ValueError, reason) as caught:
                golive_record.from_rehearsal(self.save(receipt), stage, NOW)
            self.assertIs(type(caught.exception), ValueError)

    def test_c2_go_one_per_stage(self):
        """C2: Copy no pass codes, don't invent an engine, and keep first stage order."""
        path = self.save(self.gate(stages=("19", "18", "19")))
        self.assertEqual(golive_record.from_gate(path, NOW), [self.expected(stage="19"), self.expected()])

    def test_c2_uses_shared_stage_schema(self):
        """C2: Accept stage 17b from the evidence format even if absent from the stage catalog."""
        path = self.save(dict(self.gate(), stages=["17b"]))
        self.assertEqual(golive_record.from_gate(path, NOW), [self.expected(stage="17b")])

    def test_c2_no_go_deduplicates_nonpass_codes(self):
        """C2: Unknown and failed checks both block GO; retain distinct codes in source order."""
        window = dict(type="window", label="Closed", days=["tue"], start="11:00", end="13:00")
        missing = dict(type="file_present", label="Missing", input="copy")
        passing = dict(window, label="Open", days=["mon"])
        path = self.save(self.gate(checks=[missing, passing, window, missing]))
        self.assertEqual(golive_record.from_gate(path, NOW), [self.expected(
            result="fail", codes=["GATE_INPUT_MISSING", "GATE_WINDOW_CLOSED"])])

    def test_c2_code_limit(self):
        """C2: Preserve all 20 codes, but mark overflow without changing result or source hash."""
        codes = [f"FAIL_{i}" for i in range(21)]
        for count, expected in ((20, codes[:20]), (21, codes[:19] + ["CODES_TRUNCATED"])):
            with self.subTest(count=count):
                receipt = dict(self.gate(), verdict="NO-GO", checks=[
                    dict(type="synthetic", label="Observed failure", state="fail", code=code)
                    for code in codes[:count]
                ])
                path = self.save(receipt)
                self.assertEqual(golive_record.from_gate(path, NOW),
                                 [self.expected(result="fail", codes=expected)])

    def test_c2_inconsistent_or_malformed_gate_refused(self):
        """C2: A contradictory verdict or malformed receipt cannot enter the ledger."""
        good = self.gate()
        for changes, reason in (
            ({"verdict": "NO-GO"}, "inconsistent gate verdict"),
            ({"checks": [dict(good["checks"][0], state="unknown")]}, "inconsistent gate verdict"),
            ({"checks": []}, "invalid gate receipt"),
            ({"schema": "other"}, "invalid gate receipt"),
            ({"stages": ["unknown"]}, r"\$\.stage: invalid value"),
            ({"evaluated_at": "2026-01-05T20:30:00"}, "invalid gate timestamp"),
            ({"inputs": {"copy": "not-a-digest"}}, "invalid gate input digest"),
            ({"checks": [dict(good["checks"][0], code="bad-code")]}, "invalid gate check"),
        ):
            with self.subTest(changes=changes), self.assertRaisesRegex(ValueError, reason) as caught:
                golive_record.from_gate(self.save(dict(good, **changes)), NOW)
            self.assertIs(type(caught.exception), ValueError)

    def test_c2_duplicate_json_key_refused(self):
        """C2: Do not silently choose among conflicting JSON field values."""
        path = self.save(self.gate())
        raw = path.read_bytes().replace(b'{', b'{"verdict": "NO-GO",', 1)
        path.write_bytes(raw)
        with self.assertRaisesRegex(ValueError, "invalid receipt JSON") as caught:
            golive_record.from_gate(path, NOW)
        self.assertIs(type(caught.exception), ValueError)

    def test_c3_append_private_and_reader_compatible(self):
        """C3: A write yields the named 0600 file, no scratch residue, and exact readable metadata."""
        self.save(self.gate())
        record = self.expected()
        path = golive_record.append(record, self.evidence)
        self.assertEqual(path, self.evidence / f"20260105T203000Z-18-gate-{record['source']['sha256'][:8]}.json")
        self.assertEqual(list(self.evidence.iterdir()), [path])
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(golive_evidence.load(self.evidence), [record])

    def test_c3_same_filename_collision(self):
        """C3: Same filename with a different full hash must not replace prior evidence."""
        self.save(self.gate())
        record = self.expected()
        path = golive_record.append(record, self.evidence)
        before = path.read_bytes()
        digest = record["source"]["sha256"]
        other = "0" if digest[8] != "0" else "1"
        collision = dict(record, source=dict(record["source"], sha256=digest[:8] + other * 56))
        with self.assertRaises(golive_record.EvidenceDuplicate) as caught:
            golive_record.append(collision, self.evidence)
        self.assertIs(type(caught.exception), golive_record.EvidenceDuplicate)
        self.assertEqual(str(caught.exception), "EVIDENCE_DUPLICATE")
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(list(self.evidence.iterdir()), [path])

    def test_c3_full_identity_across_timestamps(self):
        """C3: Renaming a receipt or waiting a second cannot evade deduplication."""
        self.save(self.gate())
        record = self.expected()
        first = golive_record.append(record, self.evidence)
        later = dict(record, recorded_at="2026-01-05T20:30:01Z", source=dict(record["source"], name="copy.json"))
        with self.assertRaises(golive_record.EvidenceDuplicate) as caught:
            golive_record.append(later, self.evidence)
        self.assertIs(type(caught.exception), golive_record.EvidenceDuplicate)
        self.assertEqual(str(caught.exception), "EVIDENCE_DUPLICATE")
        self.assertEqual(list(self.evidence.iterdir()), [first])
        # Neither stage nor kind alone is the identity; both are part of it.
        different_stage = dict(later, stage="19")
        different_kind = dict(later, kind="live")
        golive_record.append(different_stage, self.evidence)
        golive_record.append(different_kind, self.evidence)
        self.assertCountEqual(golive_evidence.load(self.evidence), [record, different_stage, different_kind])

    def test_c3_late_filename_collision(self):
        """C3: An external file created after the scan must survive the actual rename."""
        self.save(self.gate())
        record = self.expected()
        publish = golive_record._rename_new
        path = self.evidence / f"20260105T203000Z-18-gate-{record['source']['sha256'][:8]}.json"

        def concurrent_create(dir_fd, scratch, name):
            path.write_bytes(b"concurrent winner")
            publish(dir_fd, scratch, name)

        with patch.object(golive_record, "_rename_new", side_effect=concurrent_create):
            with self.assertRaises(golive_record.EvidenceDuplicate) as caught:
                golive_record.append(record, self.evidence)
        self.assertIs(type(caught.exception), golive_record.EvidenceDuplicate)
        self.assertEqual(path.read_bytes(), b"concurrent winner")
        self.assertEqual(list(self.evidence.iterdir()), [path])

    def test_c3_concurrent_duplicate_keeps_one(self):
        """C3: Concurrent writers with distinct filenames cannot commit the same identity."""
        self.save(self.gate())
        records = [self.expected(), dict(self.expected(), recorded_at="2026-01-05T20:30:01Z")]
        barrier = threading.Barrier(2, timeout=5)

        def write(record):
            barrier.wait()
            return golive_record.append(record, self.evidence)

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(write, record) for record in records]
        outcomes = [future.exception() for future in futures]
        self.assertCountEqual([type(outcome) for outcome in outcomes], [type(None), golive_record.EvidenceDuplicate])
        winner = records[outcomes.index(None)]
        self.assertEqual(golive_evidence.load(self.evidence), [winner])

    def test_c3_invalid_or_oversized_not_written(self):
        """C3: Refuse schema violations and oversized serialized records before writing."""
        self.save(self.gate())
        for changes, reason in (({"actor": "other"}, r"\$\.actor: invalid value"),
                                ({"codes": ["X" * (64 * 1024)]}, "exceeds 64 KiB")):
            with self.subTest(reason=reason), self.assertRaisesRegex(ValueError, reason) as caught:
                golive_record.append(dict(self.expected(), **changes), self.evidence)
            self.assertIs(type(caught.exception), ValueError)
        self.assertEqual(list(self.evidence.iterdir()), [])

    def test_c3_full_directory_refused(self):
        """C3: The next append must not exceed the reader's 5000-entry bound."""
        self.save(self.gate())
        for i in range(5000):
            (self.evidence / f"marker-{i}").touch()
        with self.assertRaisesRegex(ValueError, "evidence directory is full") as caught:
            golive_record.append(self.expected(), self.evidence)
        self.assertIs(type(caught.exception), ValueError)
        self.assertEqual(len(list(self.evidence.iterdir())), 5000)

    def cli(self, kind, path, *args):
        result = subprocess.run([sys.executable, "-B", "-m", "campaign_tool.records.golive_record",
                                 kind, str(path), "--evidence", str(self.evidence), *args],
                                capture_output=True, text=True, timeout=10, check=False)
        return result.returncode, result.stdout.splitlines(), result.stderr

    def test_c4_pass_and_duplicate_exit(self):
        """C4: Module entry point prints all GO filenames; a replay refuses with exit 2."""
        path = self.save(self.gate(stages=("18", "19")))
        status, names, err = self.cli("gate", path)
        records = golive_evidence.load(self.evidence)
        self.assertEqual((status, names, err), (0, [
            next(p.name for p in self.evidence.iterdir() if f"-{stage}-gate-" in p.name)
            for stage in ("18", "19")], ""))
        self.assertEqual([(r["stage"], r["result"]) for r in records], [("18", "pass"), ("19", "pass")])
        self.assertEqual(self.cli("gate", path), (2, [], "golive_record: EVIDENCE_DUPLICATE\n"))
        self.assertEqual(golive_evidence.load(self.evidence), records)

    def test_c4_fail_written_and_stage_selection(self):
        """C4: An explicit rehearsal stage is retained and exit 1 still publishes the failure."""
        path = self.save(self.rehearsal(time=datetime.now(timezone.utc), stages=["18", "19"], engine=None))
        status, names, err = self.cli("rehearsal", path, "--stage", "19")
        self.assertEqual((status, names, err), (1, [p.name for p in self.evidence.iterdir()], ""))
        record, = golive_evidence.load(self.evidence)
        self.assertEqual((record["stage"], record["result"], record["codes"]),
                         ("19", "fail", ["REHEARSAL_ENGINE_UNKNOWN"]))

    def test_c4_wrong_gate_stage_refused(self):
        """C4: A stage outside the gate receipt is a refusal, not empty success."""
        self.assertEqual(self.cli("gate", self.save(self.gate()), "--stage", "19"),
                         (2, [], "golive_record: stage is not declared in the gate receipt\n"))
        self.assertEqual(list(self.evidence.iterdir()), [])

    def test_c4_selects_one_gate_stage(self):
        """C4: A declared gate stage selects just that stage, not every stage."""
        path = self.save(self.gate(stages=("18", "19")))
        status, names, err = self.cli("gate", path, "--stage", "19")
        self.assertEqual((status, names, err), (0, [p.name for p in self.evidence.iterdir()], ""))
        self.assertEqual([record["stage"] for record in golive_evidence.load(self.evidence)], ["19"])
