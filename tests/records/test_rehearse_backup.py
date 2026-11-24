"""Runtime tests for the synthetic stage 19 rehearsal."""

import hashlib
import io
import json
import os
import stat
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from campaign_tool.records import rehearse_backup as driver, run_stages


class RehearseBackupTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(dir=Path.cwd())
        self.addCleanup(tmp.cleanup)
        self.sandbox = Path(tmp.name).resolve()

    def _output(self, path):
        return {"path": path, "sha256": hashlib.sha256((self.sandbox / path).read_bytes()).hexdigest()}

    def _expected(self, stopped=None, code="OK"):
        outputs = {
            "seed_project": [run_stages.SANDBOX_MARKER],
            "quiescence": ["units.txt", "quiescence.json"],
            "backup": ["sets/backup-digests.jsonl", *[
                p.relative_to(self.sandbox).as_posix() for p in self.sandbox.glob("sets/set-*/manifest.json")]],
            "backup_verify": ["backup-verify.json"],
            "restore": ["restored/manifest.json"],
            "compare": [],
        }
        steps = []
        for name, paths in outputs.items():
            if name == stopped:
                steps.append({"name": name, "code": code, "outputs": []})
                break
            if name == "backup":
                self.assertEqual(len(paths), 2)
            if name == "compare":
                manifest = json.loads((self.sandbox / "restored/manifest.json").read_bytes())
                paths = sorted("restored/" + row["path"] for row in manifest["files"])
            steps.append({"name": name, "code": "OK", "outputs": [self._output(path) for path in paths]})
        return {"stages": ["19"], "steps": steps, "manual_steps": [], "codes": [code] if stopped else []}

    def _cli(self):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            status = driver.main(["--sandbox", str(self.sandbox)])
        self.assertEqual(err.getvalue(), "")
        self.assertEqual(out.getvalue().count("\n"), 1)
        return status, json.loads(out.getvalue()), out.getvalue()

    def test_C1_owner_only_marker(self):
        """C1: Mark the sandbox without exposing or replacing its marker."""
        driver._mark_sandbox(self.sandbox)
        marker = self.sandbox / run_stages.SANDBOX_MARKER
        self.assertEqual(list(self.sandbox.iterdir()), [marker])
        self.assertEqual(stat.S_IMODE(marker.stat().st_mode), 0o600)
        self.assertIsNone(run_stages.sandbox_refusal(str(self.sandbox)))
        with self.assertRaises(FileExistsError) as refusal:
            driver._mark_sandbox(self.sandbox)
        self.assertIs(type(refusal.exception), FileExistsError)

    def test_C1_full_rehearsal(self):
        """C1: Restore a real closed set and check every declared source file."""
        self.sandbox /= "absent"  # CLI must also accept a not-yet-created sandbox.
        def first_write_check(project):
            # A runner that seeds files before creating its marker is not isolated.
            self.assertEqual(list(self.sandbox.iterdir()), [self.sandbox / run_stages.SANDBOX_MARKER])
            return seed(project)

        seed = driver._seed_project
        with patch.object(driver, "_seed_project", side_effect=first_write_check):
            status, receipt, printed = self._cli()
        # A successful comparison with empty outputs leaves Sitting 5 no payload proof.
        self.assertEqual((status, receipt), (0, self._expected()))
        audit = json.loads((self.sandbox / "quiescence.json").read_bytes())
        self.assertEqual((audit["all_writers_stopped"], audit["checks"]["codes"]), (True, []))
        verify = json.loads((self.sandbox / "backup-verify.json").read_bytes())
        self.assertEqual(verify, {"code": "OK", "files": 5, "sqlite_snapshots": 2})
        restored = self.sandbox / "restored"
        manifest = json.loads((restored / "manifest.json").read_bytes())
        project = self.sandbox / "project"
        original = b"synthetic-stage-19-original\n"
        source_paths = {
            "approvals/queue.jsonl", "processing-v1/ledger.sqlite", "processing-v1/intake/intake.sqlite",
            "processing-v1/intake/blobs/" + hashlib.sha256(original).hexdigest(),
            "processing-v1/intake/document-receipts/" + hashlib.sha256(b'{"synthetic":true}\n').hexdigest(),
        }
        self.assertEqual({Path(row["source"]).relative_to(project).as_posix() for row in manifest["files"]}, source_paths)
        self.assertEqual({p.relative_to(restored).as_posix() for p in restored.rglob("*")},
                         {"manifest.json", "payload", *(row["path"] for row in manifest["files"])})
        for row in manifest["files"]:
            with self.subTest(path=Path(row["source"]).relative_to(project)):
                source_hash = hashlib.sha256(Path(row["source"]).read_bytes()).hexdigest()
                self.assertEqual(self._output("restored/" + row["path"])["sha256"], source_hash)
                self.assertEqual(row["sha256"], source_hash)
        self.assertEqual((driver.STAGES, bool(driver.TITLE)), (("19",), True))
        # C3: Strict whole-output comparison would fail on any raw adapter stdout.
        self.assertNotIn(original.decode().strip(), printed)
        self.assertNotIn("Synthetic stage 19 unsent draft", printed)
        self.assertNotIn(str(self.sandbox), printed)

    def test_C2_busy_units_refused_by_backup(self):
        """C2: Audit a busy capture and propagate the real backup adapter's refusal."""
        capture = driver._capture_units

        def busy(path):
            capture(path)
            path.write_text(path.read_text().replace("ActiveState=inactive\nSubState=dead",
                                                     "ActiveState=active\nSubState=running", 1))

        with patch.object(driver, "_capture_units", side_effect=busy), patch.object(
                driver.records_backup, "backup_main", wraps=driver.records_backup.backup_main) as backup:
            status, result, _ = self._cli()
        backup.assert_called_once()
        self.assertEqual((status, result), (1, self._expected("backup", "QUIESCENCE_REFUSED")))
        evidence = json.loads((self.sandbox / "quiescence.json").read_bytes())
        self.assertEqual((evidence["all_writers_stopped"], evidence["checks"]["codes"]), (False, ["WRITER_ACTIVE"]))
        self.assertFalse((self.sandbox / "sets").exists())
        self.assertFalse((self.sandbox / "restored").exists())

    def test_C1_audit_failure_stops_before_backup(self):
        """C1: Do not attempt backup if the audit couldn't write its evidence."""
        capture = driver._capture_units

        def block_evidence(path):
            capture(path)
            (self.sandbox / "quiescence.json").mkdir()

        with patch.object(driver, "_capture_units", side_effect=block_evidence):
            self.assertEqual(driver.run(self.sandbox), self._expected("quiescence", "QUIESCENCE_AUDIT_FAILED"))
        self.assertFalse((self.sandbox / "sets").exists())

    def test_C1_verify_failure_stops_before_restore(self):
        """C1: A payload edit is refused by the real backup verifier."""
        verify = driver.records_backup.verify_main

        def corrupt_then_verify(args):
            payload = next((Path(args[1]) / "payload").iterdir())
            payload.chmod(0o600)
            payload.write_bytes(payload.read_bytes() + b" ")
            return verify(args)

        with patch.object(driver.records_backup, "verify_main", side_effect=corrupt_then_verify):
            self.assertEqual(driver.run(self.sandbox), self._expected("backup_verify", "VERIFY_FAILED"))
        self.assertFalse((self.sandbox / "restored").exists())

    def test_C1_restore_failure_stops_before_compare(self):
        """C1: The real restore refuses a target that is no longer fresh."""
        restore = driver.backup_v2.restore_v2

        def occupy_then_restore(source, destination, **options):
            Path(destination).mkdir(mode=0o700)
            return restore(source, destination, **options)

        with patch.object(driver.backup_v2, "restore_v2", side_effect=occupy_then_restore):
            self.assertEqual(driver.run(self.sandbox), self._expected("restore", "RESTORE_FAILED"))

    def test_C1_compare_rejects_changes_in_every_restored_payload_member(self):
        """C1: Compare cannot pass by checking only the manifest or the first payload member."""
        root = self.sandbox
        restore = driver.backup_v2.restore_v2
        for index in range(5):
            self.sandbox = root / str(index)
            self.sandbox.mkdir(mode=0o700)

            def restore_then_change(source, destination, **options):
                result = restore(source, destination, **options)
                path = sorted((Path(destination) / "payload").iterdir())[index]
                path.chmod(0o600)
                path.write_bytes(b"changed after the real restore")
                return result

            with self.subTest(index=index), patch.object(driver.backup_v2, "restore_v2", side_effect=restore_then_change):
                self.assertEqual(driver.run(self.sandbox), self._expected("compare", "RESTORE_MISMATCH"))

    def test_C1_compare_checks_restored_types_and_modes(self):
        """C1: Byte equality cannot qualify a restore with unsafe metadata."""
        root = self.sandbox
        restore, sha = driver.backup_v2.restore_v2, driver._sha
        for case in ("read-only file", "group-readable file", "executable file", "root mode",
                     "payload directory mode", "manifest symlink", "payload FIFO"):
            with self.subTest(case=case):
                self.sandbox = root / case
                self.sandbox.mkdir(mode=0o700)
                def restore_then_change(source, destination, **options):
                    result = restore(source, destination, **options)
                    target = Path(destination)
                    payload = target / "payload"
                    member = next(payload.iterdir())
                    if case == "root mode":
                        target.chmod(0o750)
                    elif case == "payload directory mode":
                        payload.chmod(0o750)
                    elif case == "manifest symlink":
                        manifest = target / "manifest.json"
                        manifest.unlink()
                        manifest.symlink_to(Path(source) / "manifest.json")
                    elif case == "payload FIFO":
                        member.unlink()
                        os.mkfifo(member, 0o600)
                    else:
                        member.chmod({"read-only file": 0o400, "group-readable file": 0o640,
                                      "executable file": 0o700}[case])
                    return result

                def check_sha(path):
                    if case in ("manifest symlink", "payload FIFO"):
                        self.assertNotIn(self.sandbox / "restored", path.parents,
                                         "must refuse the tree before hashing restored content")
                    return sha(path)

                with patch.object(driver.backup_v2, "restore_v2", side_effect=restore_then_change), patch.object(
                        driver, "_sha", side_effect=check_sha):
                    result = driver.run(self.sandbox)
                expected = self._expected() if case == "read-only file" else self._expected(
                    "compare", "RESTORE_MISMATCH")
                self.assertEqual(result, expected)

    def test_C3_cli_refuses_bad_sandbox_without_emitting_record_content(self):
        """C3: Invalid destinations exit 2 before entering the rehearsal or printing a JSON receipt."""
        marker = self.sandbox / "already-present"
        marker.write_bytes(b"leave unchanged")
        link = self.sandbox / "link"
        link.symlink_to(self.sandbox / "missing")
        for argv in ([], ["--sandbox", str(self.sandbox)], ["--sandbox", str(marker)], ["--sandbox", str(link)]):
            with self.subTest(argv=argv), redirect_stdout(io.StringIO()) as out, redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as refusal:
                    driver.main(argv)
                self.assertEqual((type(refusal.exception), refusal.exception.code, out.getvalue()), (SystemExit, 2, ""))
        self.assertEqual(marker.read_bytes(), b"leave unchanged")
        self.assertFalse((self.sandbox / "missing").exists())

    def test_C3_seed_failure_reports_only_fixed_code(self):
        """C3: I/O exception details do not appear in the JSON receipt or on stderr."""
        with patch.object(driver, "_seed_project", side_effect=PermissionError("content not for the receipt")):
            status, result, text = self._cli()
        self.assertEqual((status, result), (1, self._expected("seed_project", "SEED_PROJECT_FAILED")))
        self.assertNotIn("content not for the receipt", text)
