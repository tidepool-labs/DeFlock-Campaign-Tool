"""Synthetic runtime checks for the rehearsal receipt contract."""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch

from campaign_tool.records import golive_record
from campaign_tool.records import rehearsal_receipt as rr
from campaign_tool.records.run_stages import SANDBOX_MARKER

START = "2026-01-02T03:04:04Z"
FINISH = "2026-01-02T03:04:05Z"
NOW = datetime.fromisoformat(FINISH)
ENGINE = "a" * 40
MODULE = "rehearse_synthetic"
NAME = "20260102T030405Z-rehearse_synthetic.json"
OK_HASH = hashlib.sha256(b"synthetic\n").hexdigest()


class ReceiptTests(unittest.TestCase):
    def setUp(self):
        fixture = tempfile.TemporaryDirectory(dir=Path.cwd())
        self.addCleanup(fixture.cleanup)
        self.root = Path(fixture.name)
        self.sandbox = self.root / "sandbox"
        self.sandbox.mkdir(mode=0o700)
        self.marker = self.sandbox / SANDBOX_MARKER
        self.marker.touch(mode=0o600)
        (self.sandbox / "nested").mkdir()
        self.output = self.sandbox / "nested" / "output.txt"
        self.output.write_bytes(b"synthetic\n")
        self.driver = {
            "stages": ["3", "A5"],
            "steps": [{"name": "invented step", "code": "OK", "outputs": [
                {"path": "nested/output.txt", "sha256": OK_HASH},
            ]}],
            "manual_steps": [],
            "codes": ["SYNTHETIC_INFO"],
        }

    def build(self, *, engine: str | None = ENGINE, sandbox=None,
              start: str | datetime = START, finish: str | datetime = FINISH):
        return rr.build(MODULE, self.driver, self.sandbox if sandbox is None else sandbox,
                        start, finish, engine)

    def expected(self, *codes, result="pass", engine: str | None = ENGINE):
        return {
            "schema": "records-rehearsal-v1", "module": MODULE,
            "started_at": START, "finished_at": FINISH, "engine": engine,
            **deepcopy(self.driver), "result": result,
            "qualified": not codes, "qualify_codes": list(codes),
        }

    def assert_refused(self, exception, fn, *args):
        with self.assertRaises(exception) as caught:
            fn(*args)
        self.assertIs(type(caught.exception), exception)

    def test_c1_hashes_nested_output_and_snapshots_driver(self):
        """C1: Do not drop driver codes or alias mutable driver output."""
        offset = timezone(timedelta(hours=4))
        actual = self.build(start=datetime.fromisoformat(START).astimezone(offset), finish=NOW)
        expected = self.expected()
        self.driver["steps"][0]["outputs"].clear()
        self.assertEqual(actual, expected)

    def test_c1_checks_every_output_even_after_first_mismatch(self):
        """C1: Hash each declaration, including after a changed output."""
        self.output.write_bytes(b"changed\n")
        (self.sandbox / "other.txt").write_bytes(b"synthetic\n")
        self.driver["steps"].append({"name": "second", "code": "OK", "outputs": [
            {"path": "other.txt", "sha256": OK_HASH},
        ]})
        digests = []
        digest = hashlib.file_digest

        def record(source, algorithm):
            actual = digest(source, algorithm)
            digests.append(actual.hexdigest())
            return actual

        with patch.object(hashlib, "file_digest", side_effect=record):
            self.assertEqual(self.build(), self.expected("REHEARSAL_OUTPUT_MISMATCH"))
        self.assertEqual(digests, [hashlib.sha256(b"changed\n").hexdigest(), OK_HASH])

    def test_c1_refuses_unsafe_path_syntax(self):
        """C1: Absolute, traversing and unusable paths never enter a receipt."""
        for path in (str(self.output), "../peer.txt", "nested/../nested/output.txt", ".", "bad\0name", "a\\b"):
            with self.subTest(path=path):
                self.driver["steps"][0]["outputs"][0]["path"] = path
                self.assert_refused(ValueError, self.build)

    def test_c1_rejects_leaf_and_intermediate_symlinks_without_hashing(self):
        """C1: Do not hash symlink targets, even when the target hash matches."""
        peer = self.root / "peer"
        peer.mkdir()
        (peer / "file").write_bytes(b"synthetic\n")
        (self.sandbox / "leaf").symlink_to(peer / "file")
        (self.sandbox / "directory").symlink_to(peer, target_is_directory=True)
        (self.sandbox / "inside").symlink_to(self.output)
        for path in ("leaf", "directory/file", "inside"):
            with self.subTest(path=path):
                self.driver["steps"][0]["outputs"][0]["path"] = path
                self.assertEqual(self.build(), self.expected("REHEARSAL_OUTPUT_MISMATCH"))

    def test_c1_unhashable_outputs_are_mismatches(self):
        """C1: Missing paths, non-directory parents and non-files cannot qualify."""
        os.mkfifo(self.sandbox / "pipe", 0o600)
        for path in ("missing", "nested/output.txt/child", "nested", "pipe"):
            with self.subTest(path=path):
                self.driver["steps"][0]["outputs"][0]["path"] = path
                self.assertEqual(self.build(), self.expected("REHEARSAL_OUTPUT_MISMATCH"))

    def test_c1_does_not_follow_sandbox_root_symlink(self):
        """C1: A linked or missing sandbox contributes no filesystem evidence."""
        alias = self.root / "alias"
        alias.symlink_to(self.sandbox, target_is_directory=True)
        for sandbox in (alias, self.root / "absent"):
            with self.subTest(sandbox=sandbox.name):
                self.assertEqual(self.build(sandbox=sandbox), self.expected(
                    "REHEARSAL_OUTPUT_MISMATCH", "REHEARSAL_NOT_SANDBOX"))
        self.driver["steps"][0]["outputs"].clear()
        self.assertEqual(self.build(sandbox=alias), self.expected("REHEARSAL_NOT_SANDBOX"))

    def test_c1_refuses_non_private_markers(self):
        """C1: Mere path existence is not the run-stages marker convention."""
        self.marker.chmod(0o644)
        self.assertEqual(self.build(), self.expected("REHEARSAL_NOT_SANDBOX"))
        self.marker.unlink()
        self.marker.mkdir(mode=0o700)
        self.assertEqual(self.build(), self.expected("REHEARSAL_NOT_SANDBOX"))
        self.marker.rmdir()
        self.marker.symlink_to(self.output)
        self.assertEqual(self.build(), self.expected("REHEARSAL_NOT_SANDBOX"))

    def test_c2_failed_step_only(self):
        """C2: An actual driver failure fails regardless of valid hashes."""
        self.driver["steps"][0]["code"] = "INVENTED_FAILURE"
        receipt = self.build()
        self.assertEqual(receipt, self.expected("REHEARSAL_FAILED_STEP", result="fail"))
        self.assertEqual(rr.qualify(receipt, now=NOW), (False, ["REHEARSAL_FAILED_STEP"]))

    def test_c2_manual_step_only(self):
        """C2: Successful automated steps cannot waive manual work."""
        self.driver["manual_steps"] = ["invented manual step"]
        receipt = self.build()
        self.assertEqual(receipt, self.expected("REHEARSAL_MANUAL_STEP", result="fail"))
        self.assertEqual(rr.qualify(receipt, now=NOW), (False, ["REHEARSAL_MANUAL_STEP"]))

    def test_c2_no_steps_only(self):
        """C2: Vacuous result pass is not qualification."""
        self.driver["steps"].clear()
        receipt = self.build()
        self.assertEqual(receipt, self.expected("REHEARSAL_NO_STEPS"))
        self.assertEqual(rr.qualify(receipt, now=NOW), (False, ["REHEARSAL_NO_STEPS"]))

    def test_c2_ok_step_can_have_no_outputs(self):
        """C2: Do not confuse an outputless successful check with an empty rehearsal."""
        self.driver["steps"][0]["outputs"].clear()
        receipt = self.build()
        self.assertEqual(receipt, self.expected())
        self.assertEqual(rr.qualify(receipt, now=NOW), (True, []))

    def test_c2_output_mismatch_only(self):
        """C2: Requalification preserves the failed build-time hash observation."""
        self.output.write_bytes(b"changed\n")
        receipt = self.build()
        self.output.write_bytes(b"synthetic\n")
        self.assertEqual(receipt, self.expected("REHEARSAL_OUTPUT_MISMATCH"))
        self.assertEqual(rr.qualify(receipt, now=NOW), (False, ["REHEARSAL_OUTPUT_MISMATCH"]))

    def test_c2_not_sandbox_only(self):
        """C2: Adding a marker after the run cannot upgrade its receipt."""
        self.marker.unlink()
        receipt = self.build()
        self.marker.touch(mode=0o600)
        self.assertEqual(receipt, self.expected("REHEARSAL_NOT_SANDBOX"))
        self.assertEqual(rr.qualify(receipt, now=NOW), (False, ["REHEARSAL_NOT_SANDBOX"]))

    def test_c2_engine_unknown_only(self):
        """C2: No engine is distinct from a known engine that disagrees."""
        receipt = self.build(engine=None)
        self.assertEqual(receipt, self.expected("REHEARSAL_ENGINE_UNKNOWN", engine=None))
        for target in (None, ENGINE):
            self.assertEqual(rr.qualify(receipt, now=NOW, target_engine=target),
                             (False, ["REHEARSAL_ENGINE_UNKNOWN"]))

    def test_c2_stale_only(self):
        """C2: Exact age boundary is inclusive; future is not fresh."""
        receipt = self.build()
        for days in (0, 14):
            self.assertEqual(rr.qualify(receipt, now=NOW + timedelta(days=days)), (True, []))
        for delta in (timedelta(days=14, microseconds=1), timedelta(microseconds=-1)):
            self.assertEqual(rr.qualify(receipt, now=NOW + delta), (False, ["REHEARSAL_STALE"]))
        self.assertEqual(rr.qualify(receipt, now=NOW + timedelta(days=1), max_age_days=1), (True, []))
        self.assertEqual(rr.qualify(receipt, now=NOW + timedelta(days=2), max_age_days=1),
                         (False, ["REHEARSAL_STALE"]))

    def test_c2_engine_mismatch_only(self):
        """C2: A matching optional target passes; a different target fails."""
        receipt = self.build()
        self.assertEqual(rr.qualify(receipt, now=FINISH, target_engine=ENGINE), (True, []))
        self.assertEqual(rr.qualify(receipt, now=NOW, target_engine="b" * 40),
                         (False, ["REHEARSAL_ENGINE_MISMATCH"]))

    def test_c2_combines_reasons_in_fixed_order(self):
        """C2: Multiple refusal reasons are ordered and the stored result is not mutated."""
        self.driver["steps"][0]["code"] = "INVENTED_FAILURE"
        self.driver["manual_steps"] = ["invented manual step"]
        self.marker.unlink()
        self.output.write_bytes(b"changed\n")
        receipt = self.build()
        reasons = ["REHEARSAL_FAILED_STEP", "REHEARSAL_MANUAL_STEP", "REHEARSAL_OUTPUT_MISMATCH",
                   "REHEARSAL_NOT_SANDBOX"]
        self.assertEqual(rr.qualify(receipt, now=NOW + timedelta(days=15), target_engine="b" * 40),
                         (False, reasons + ["REHEARSAL_STALE", "REHEARSAL_ENGINE_MISMATCH"]))
        self.assertEqual(receipt, self.expected(*reasons, result="fail"))

    def test_c2_recomputes_contextual_verdict(self):
        """C2: A recorded stale/target decision is not sticky at another clock or target."""
        receipt = self.expected("REHEARSAL_STALE", "REHEARSAL_ENGINE_MISMATCH")
        self.assertEqual(rr.qualify(receipt, now=NOW, target_engine=ENGINE), (True, []))

    def test_c2_refuses_naive_clock(self):
        """C2: An unspecified timezone must not be silently interpreted as local time."""
        with self.assertRaises(ValueError) as caught:
            rr.qualify(self.build(), now=NOW.replace(tzinfo=None))
        self.assertIs(type(caught.exception), ValueError)

    def test_c3_round_trip_and_refuse_replacement(self):
        """C3: Commit exact data at the derived owner-only path; never clobber it."""
        # Literal identities: neither case nor surrounding whitespace is normalized.
        self.driver["steps"].extend(
            {"name": name, "code": "OK", "outputs": []} for name in ("Invented step", " invented step ")
        )
        receipt = self.build()
        path = rr.write(receipt, self.root)
        self.assertEqual(path, self.root / NAME)
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        before = path.read_bytes()
        loaded = rr.read(path)
        self.assertEqual(loaded, receipt)
        self.assertEqual(loaded["stages"], ["3", "A5"])
        self.assertEqual(rr.qualify(loaded, now=NOW, target_engine=ENGINE), (True, []))
        self.assertEqual(golive_record.from_rehearsal(path, "3", NOW)["result"], "pass")
        different = dict(receipt, codes=["SYNTHETIC_ALTERNATE"])
        self.assert_refused(FileExistsError, rr.write, different, self.root)
        self.assertEqual((path.read_bytes(), sorted(p.name for p in self.root.iterdir())),
                         (before, [NAME, "sandbox"]))

    def test_c3_refuses_existing_dangling_link(self):
        """C3: A dangling link is still an occupied destination."""
        target = self.root / NAME
        missing = self.root / "missing"
        target.symlink_to(missing)
        self.assert_refused(FileExistsError, rr.write, self.build(), self.root)
        self.assertEqual((target.readlink(), missing.exists()), (missing, False))

    def test_c3_refuses_when_no_atomic_rename_is_available(self):
        """C3: Do not fall back to an overwriting rename."""
        with patch.object(rr.ctypes, "CDLL", return_value=object()):
            self.assert_refused(NotImplementedError, rr.write, self.build(), self.root)
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), ["sandbox"])

    def test_c3_writer_validates_basename_before_writing(self):
        """C3: Receipt-controlled filenames cannot add path components."""
        self.assert_refused(ValueError, rr.write, dict(self.build(), module="x/../../escape"), self.root)
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), ["sandbox"])

    def test_c3_reader_refuses_bad_json(self):
        """C3: A parser cannot accept truncated, duplicate-key or non-UTF8 receipts."""
        path = self.root / "receipt.json"
        valid = json.dumps(self.build()).encode()
        for raw in (b"{", b"\xff", b'{"schema":"shadowed", ' + valid[1:]):
            with self.subTest(raw=raw[:20]):
                path.write_bytes(raw)
                self.assert_refused(ValueError, rr.read, path)

    def test_c3_reader_refuses_shape_deviations(self):
        """C3: Reject wrong keys, types, formats and inconsistent status, including nested rows."""
        path = self.root / "receipt.json"
        step = self.driver["steps"][0]
        output = step["outputs"][0]
        unqualified = {"qualified": False}
        variants = (
            {"schema": "records-rehearsal-v2"}, {"extra": "unexpected"}, {"module": "../outside"},
            {"stages": "3"}, {"stages": [3]}, {"manual_steps": [False]}, {"codes": ["Not_UPPER"]},
            {"started_at": "2026-02-30T00:00:00Z"}, {"finished_at": "2026-01-02T03:04:05+00:00"},
            {"finished_at": "2026-01-01T00:00:00Z"}, {"engine": "g" * 40}, {"steps": {}},
            {"steps": [{}]}, {"steps": [dict(step, name=1)]}, {"steps": [dict(step, code="bad")]},
            {"steps": [dict(step, outputs=None)]}, {"steps": [dict(step, outputs=[{}])]},
            {"steps": [dict(step, outputs=[dict(output, sha256="A" * 64)])]},
            {"steps": [dict(step, outputs=[dict(output, path="../out")])]},
            {"result": "fail"}, {"qualified": 1}, {"qualified": False}, {"qualify_codes": ""},
            dict(unqualified, qualify_codes=["INVENTED"]),
            dict(unqualified, qualify_codes=["REHEARSAL_STALE", "REHEARSAL_STALE"]),
            dict(unqualified, qualify_codes=["REHEARSAL_STALE", "REHEARSAL_NO_STEPS"]),
        )
        valid = self.build()
        for candidate in ([], {k: v for k, v in valid.items() if k != "engine"},
                          *(dict(valid, **variant) for variant in variants)):
            with self.subTest(candidate=candidate):
                path.write_text(json.dumps(candidate), encoding="utf-8")
                self.assert_refused(ValueError, rr.read, path)

        # A list-of-strings check alone lets an OK rehearsal qualify without
        # a ledger target, or claim the same stage more than once.
        for stages in ([], [""], [" \t"], ["3", " \t"], ["3", "A5", "3"]):
            with self.subTest(stages=stages, boundary="build"):
                self.driver["stages"] = stages
                self.assert_refused(ValueError, self.build)
            with self.subTest(stages=stages, boundary="read"):
                path.write_text(json.dumps(dict(valid, stages=stages)), encoding="utf-8")
                self.assert_refused(ValueError, rr.read, path)

        # A repeated OK name can masquerade as a distinct completed checkpoint.
        # The go-live recorder must also refuse a previously saved malformed receipt.
        self.driver["stages"] = valid["stages"]
        for name in (step["name"], "", " \t\n"):
            steps = [step, {"name": name, "code": "OK", "outputs": []}]
            with self.subTest(name=name, boundary="build"):
                self.driver["steps"] = steps
                self.assert_refused(ValueError, self.build)
            path.write_text(json.dumps(dict(valid, steps=steps)), encoding="utf-8")
            with self.subTest(name=name, boundary="read"):
                self.assert_refused(ValueError, rr.read, path)
            with self.subTest(name=name, boundary="go-live recorder"):
                self.assert_refused(ValueError, golive_record.from_rehearsal, path, "3", NOW)

    def tampered(self, **changes):
        """A shape-valid receipt whose recorded verdict contradicts its own facts."""
        return dict(self.build(), **changes)

    def assert_inconsistent_both_ways(self, receipt):
        """Writer refuses before creating anything; reader refuses the hand-written file."""
        with self.assertRaises(ValueError) as caught:
            rr.write(receipt, self.root)
        self.assertEqual(str(caught.exception), "qualification_inconsistent")
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), ["sandbox"])
        path = self.root / "hand-written.json"
        path.write_text(json.dumps(receipt), encoding="utf-8")
        self.assert_refused(ValueError, rr.read, path)
        path.unlink()
        with self.assertRaises(ValueError):
            rr.qualify(receipt, now=NOW)

    def test_c3_refuses_failed_step_recorded_as_qualified(self):
        """C3: A failed step with result=fail cannot be stored as qualified with no codes."""
        self.driver["steps"][0]["code"] = "INVENTED_FAILURE"
        receipt = self.tampered(qualified=True, qualify_codes=[])
        self.assertEqual(receipt["result"], "fail")
        self.assert_inconsistent_both_ways(receipt)

    def test_c3_refuses_recorded_codes_that_differ_from_recomputed(self):
        """C3: A missing, extra or out-of-order code is refused, not trusted."""
        self.driver["steps"][0]["code"] = "INVENTED_FAILURE"
        self.driver["manual_steps"] = ["invented manual step"]
        failed, manual = "REHEARSAL_FAILED_STEP", "REHEARSAL_MANUAL_STEP"
        for codes in ([failed], [manual], [failed, manual, "REHEARSAL_NO_STEPS"]):
            with self.subTest(codes=codes):
                self.assert_inconsistent_both_ways(self.tampered(qualified=False, qualify_codes=codes))

    def test_c3_refuses_extra_code_on_clean_receipt(self):
        """C3: A recorded fact code with no supporting fact is refused."""
        self.assert_inconsistent_both_ways(self.tampered(qualified=False, qualify_codes=["REHEARSAL_NO_STEPS"]))

    def test_c3_refuses_unrecorded_manual_step_recorded_as_qualified(self):
        """C3: A manual step cannot hide behind qualified=true."""
        self.driver["manual_steps"] = ["invented manual step"]
        self.assert_inconsistent_both_ways(self.tampered(qualified=True, qualify_codes=[]))

    def test_c3_refuses_unknown_engine_recorded_as_qualified(self):
        """C3: A receipt with no engine identity cannot claim to be qualified."""
        self.assert_inconsistent_both_ways(dict(self.build(engine=None), qualified=True, qualify_codes=[]))

    def test_c3_consistent_receipts_round_trip_byte_exact(self):
        """C3: Honest qualified and unqualified receipts are still accepted unchanged."""
        self.marker.unlink()
        for index, receipt in enumerate((self.build(engine=None), self.build())):
            with self.subTest(qualified=receipt["qualified"]):
                out = self.root / f"out{index}"
                out.mkdir()
                path = rr.write(receipt, out)
                self.assertEqual(path.read_bytes(),
                                 (json.dumps(receipt, sort_keys=True, indent=2) + "\n").encode())
                self.assertEqual(rr.read(path), receipt)
        self.marker.touch(mode=0o600)
        self.driver["stages"].reverse()  # Preserve driver order, not sorted order.
        receipt = self.build()
        self.assertTrue(receipt["qualified"])
        loaded = rr.read(rr.write(receipt, self.root))
        self.assertEqual(loaded, receipt)
        self.assertEqual(loaded["stages"], ["A5", "3"])


class IdentityTests(unittest.TestCase):
    def test_c4_verified_engine_commit(self):
        """C4: Use the provenance commit, not a version/tag surrogate."""
        with patch.object(rr.release_manifest, "version_report",
                          return_value={"commit": ENGINE, "source_dirty": False}):
            self.assertEqual(rr.engine_identity(), ENGINE)

    def test_c4_unknown_engine_commit(self):
        """C4: Missing, malformed and dirty source provenance cannot identify running bytes."""
        for report in ({}, {"commit": "invalid"}, {"commit": ENGINE, "source_dirty": True}):
            with self.subTest(report=report), patch.object(rr.release_manifest, "version_report", return_value=report):
                self.assertIsNone(rr.engine_identity())
        with patch.object(rr.release_manifest, "version_report", side_effect=RuntimeError("unavailable")):
            self.assertIsNone(rr.engine_identity())
