import hashlib
import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
from pathlib import Path

from campaign_tool.records import golive_adapt_backup as adapter, golive_evidence


MANIFEST = "ab" * 32
OTHER = MANIFEST[:-1] + "c"  # Same filename prefix; only the full SHA can distinguish the sets.
NOW = datetime(2026, 11, 23, 1, 2, 3, tzinfo=timezone.utc)
NOW_Z = "2026-11-23T01:02:03Z"
PIN_Z = "2026-11-22T09:10:11Z"
PIN = {"at": "2026-11-22T04:10:11.987654-05:00", "manifest_sha256": MANIFEST}
# The successful v2 producer returns are deliberately the same shape. The
# caller captures each operation independently; verify_v2 raises on failure.
REPORT = {"schema": "records-backup-v2", "manifest_sha256": MANIFEST, "files": 5,
          "sqlite_snapshots": 2, "declared_scope_closed": True, "live_pipeline_recovery_proven": False}
LEGACY = {"manifest_sha256": MANIFEST, "files": 4, "total_bytes": 80,
          "scope": "explicit_selected_files", "backup_complete": True}
RESTORE = {"schema": "records-restore-v1", "manifest_sha256": OTHER, "files": 4,
           "total_bytes": 80, "hash_reconciliation": "exact", "scope": "explicit_selected_files",
           "isolated": True, "sends": 0, "deployments": 0, "activation": False}
RESTORE_V2 = dict(REPORT, isolated=True, sends=0, activation=False, replay=0,
                  runnable_reconciliation_proven=False)
COUNTS = {"code": "OK", "files": 5, "sqlite_snapshots": 2}
EXPECTED = {
    "schema": "records-golive-evidence-v1", "stage": "19", "kind": "live", "result": "pass",
    "recorded_at": PIN_Z, "engine": None, "actor": "operator", "codes": [],
    "source": {"schema": "records-backup-v2", "sha256": MANIFEST, "name": "manifest.json"},
}


def failure(code, **fields):
    return dict(EXPECTED, result="fail", codes=[code], **fields)


def receipt_source(value, schema, name):
    # Legacy backup/restore summaries do not carry a digest of their own bytes.
    raw = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return {"schema": schema, "sha256": hashlib.sha256(raw).hexdigest(), "name": name}


class BackupEvidenceTests(unittest.TestCase):
    def assert_records(self, got, expected):
        self.assertEqual(got, expected)
        for row in got:
            self.assertIsNone(golive_evidence.validate(row))

    def assert_value_error(self, *args, **kwargs):
        with self.assertRaises(ValueError) as caught:
            adapter.records(*args, **kwargs)
        self.assertIs(type(caught.exception), ValueError)

    def test_c1_v2_pass(self):
        """C1: Use the manifest hash and digest-row instant, not report bytes or now."""
        pin = dict(PIN, set="/synthetic/set-1", evidence_sha256="cd" * 32)
        self.assert_records(adapter.records(REPORT, dict(REPORT), NOW, digest_row=pin), [EXPECTED])

    def test_c1_missing(self):
        """C1: Backup plus pin without a verify return is missing, not a pass."""
        self.assert_records(adapter.records(REPORT, None, NOW, digest_row=PIN),
                            [failure("BACKUP_VERIFY_MISSING")])

    def test_c1_hash_mismatch(self):
        """C1: Compare each full hash against the pin, including when both agree on the wrong set."""
        other = dict(REPORT, manifest_sha256=OTHER)
        for backup, verify in ((REPORT, other), (other, REPORT), (other, other)):
            with self.subTest(backup=backup["manifest_sha256"], verify=verify["manifest_sha256"]):
                self.assert_records(adapter.records(backup, verify, NOW, digest_row=PIN),
                                    [failure("BACKUP_MISMATCH")])
        for malformed in (MANIFEST[:8], MANIFEST.upper(), MANIFEST + "\n", None):
            with self.subTest(hash_shape=malformed):
                self.assert_records(adapter.records(REPORT, dict(REPORT, manifest_sha256=malformed), NOW,
                                                    digest_row=PIN), [failure("BACKUP_MISMATCH")])

    def test_c1_scope_required_on_both_sides(self):
        """C1: A hash match never replaces either literal closed-scope flag."""
        for invalid in (dict(REPORT, declared_scope_closed=False), dict(REPORT, declared_scope_closed=1),
                        {k: v for k, v in REPORT.items() if k != "declared_scope_closed"}):
            for backup, verify in ((invalid, REPORT), (REPORT, invalid)):
                with self.subTest(backup=backup, verify=verify):
                    self.assert_records(adapter.records(backup, verify, NOW, digest_row=PIN),
                                        [failure("BACKUP_VERIFY_FAILED")])

    def test_c1_nonverify_input(self):
        """C1: Count-only OK, error, restore, or malformed results cannot stand in for verify."""
        for verify in (COUNTS, {"code": "VERIFY_FAILED"}, RESTORE_V2, {},
                       dict(REPORT, files=True), dict(REPORT, sqlite_snapshots=6),
                       dict(REPORT, live_pipeline_recovery_proven=True)):
            with self.subTest(verify=verify):
                self.assert_records(adapter.records(REPORT, verify, NOW, digest_row=PIN),
                                    [failure("BACKUP_VERIFY_FAILED")])

    def test_c1_missing_or_unusable_pin(self):
        """C1: Require a full independent hash and an unambiguous digest-row time for v2."""
        for pin in (None, [], {}, dict(PIN, manifest_sha256=MANIFEST[:8]), {"manifest_sha256": MANIFEST},
                    dict(PIN, at="2026-11-22T09:10:11"), dict(PIN, at="2026-02-30T09:10:11Z")):
            with self.subTest(pin=pin):
                self.assert_value_error(REPORT, REPORT, NOW, digest_row=pin)
        self.assert_value_error(LEGACY, REPORT, NOW)  # v2 on the verify side also needs a pin.

    def test_c1_manifest_and_counts_not_backup_reports(self):
        """C1: Neither a manifest nor a CLI OK line alone is a usable backup report."""
        self.assert_value_error(COUNTS, COUNTS, NOW, digest_row=PIN)
        self.assert_value_error(None, None, NOW)
        self.assert_value_error(dict(LEGACY, backup_complete=False), None, NOW)
        self.assert_value_error({"schema": "records-backup-v1", "files": []}, None, NOW)
        manifest = {"schema": "records-backup-v2", "scope": "declared_profile_closed", "files": []}
        self.assert_records(adapter.records(manifest, None, NOW, digest_row=PIN),
                            [failure("BACKUP_VERIFY_FAILED")])

    def test_c1_legacy_no_valid_verify(self):
        """C1: Keep the legacy positional call but do not invent a v1 verify format."""
        source = receipt_source(LEGACY, "records-backup-v1", "backup-receipt.json")
        for verify, code in ((None, "BACKUP_VERIFY_MISSING"), (LEGACY, "BACKUP_VERIFY_FAILED"),
                             (RESTORE, "BACKUP_VERIFY_FAILED")):
            with self.subTest(verify=verify):
                self.assert_records(adapter.records(LEGACY, verify, NOW),
                                    [failure(code, source=source, recorded_at=NOW_Z)])

    def test_c2_restore_separate(self):
        """C2: Isolated restore appends rehearsal without changing either live verdict."""
        for receipt, name in ((RESTORE, "RESTORE-RECEIPT.json"), (RESTORE_V2, "restore-receipt.json")):
            rest = dict(EXPECTED, kind="rehearsal", recorded_at=NOW_Z,
                        source=receipt_source(receipt, receipt["schema"], name))
            for verify, live in ((None, failure("BACKUP_VERIFY_MISSING")), (REPORT, EXPECTED)):
                with self.subTest(schema=receipt["schema"], live=live["result"]):
                    self.assert_records(adapter.records(REPORT, verify, NOW, digest_row=PIN,
                                                        restore_receipt=receipt), [live, rest])

    def test_c2_refuse_invalid_restore(self):
        """C2: Do not convert a non-restore or activation report into a rehearsal pass."""
        for receipt in ([], REPORT, dict(RESTORE, activation=True), dict(RESTORE, sends=False),
                        dict(RESTORE_V2, isolated=False), dict(RESTORE_V2, manifest_sha256="")):
            with self.subTest(receipt=receipt):
                self.assert_value_error(REPORT, REPORT, NOW, digest_row=PIN, restore_receipt=receipt)


class BackupAdapterCliTests(unittest.TestCase):
    def cli(self, argv):
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            status = adapter.main(argv)
        return status, stdout.getvalue(), stderr.getvalue()

    def fixture(self, root, name, value):
        path = Path(root, name)
        path.write_text(json.dumps(value), encoding="utf-8")
        return str(path)

    def test_c3_json_and_text(self):
        """C3: Read saved API reports and print complete JSON or the standard text row."""
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as root:
            args = [self.fixture(root, "backup.json", REPORT), self.fixture(root, "verify.json", REPORT),
                    "--digest-row", self.fixture(root, "pin.json", PIN)]
            status, stdout, stderr = self.cli([*args, "--json"])
            self.assertEqual((status, json.loads(stdout), stderr), (0, [EXPECTED], ""))
            self.assertEqual(self.cli(args), (0, f"19\tlive\tpass\t{PIN_Z}\trecords-backup-v2\t\n", ""))

    def test_c3_negative_evidence(self):
        """C3: A failed pairing prints the fail record and exits 2, not 0."""
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as root:
            backup = self.fixture(root, "backup.json", REPORT)
            verify = self.fixture(root, "verify.json", COUNTS)
            pin = self.fixture(root, "pin.json", PIN)
            status, stdout, stderr = self.cli([backup, verify, "--digest-row", pin, "--json"])
        self.assertEqual((status, json.loads(stdout), stderr), (2, [failure("BACKUP_VERIFY_FAILED")], ""))

    def test_c3_bad_input(self):
        """C3: Refused inputs exit 2 without partial evidence or receipt details."""
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as root:
            backup = self.fixture(root, "backup.json", REPORT)
            pin = self.fixture(root, "pin.json", PIN)
            path = Path(root, "bad.json")
            for raw in ("not JSON", '{"code":"OK","code":"other"}', "[]", '"' + "a" * 65536 + '"'):
                with self.subTest(case=raw[:32]):
                    path.write_text(raw, encoding="utf-8")
                    self.assertEqual(self.cli([backup, str(path), "--digest-row", pin, "--json"]),
                                     (2, "", "BACKUP_EVIDENCE_BAD_INPUT\n"))
            for args in ([backup], [str(path.with_name("absent.json"))], [root]):
                with self.subTest(args=args):
                    self.assertEqual(self.cli([*args, "--json"]), (2, "", "BACKUP_EVIDENCE_BAD_INPUT\n"))
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as caught:
            adapter.main([])
        self.assertEqual((type(caught.exception), caught.exception.code), (SystemExit, 2))
