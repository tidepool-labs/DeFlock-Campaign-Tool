"""A new legacy export receipt must describe the original actually on disk."""
import hashlib
from pathlib import Path
import tempfile
import unittest

from campaign_tool.records.intake import eml_export, mail_delta


RAW = b"Subject: Synthetic message\r\nContent-Type: text/plain\r\n\r\nBody A\r\n"


class ExistingOriginalTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.sha = hashlib.sha256(RAW).hexdigest()
        self.base = self.root / self.sha
        self.base.mkdir(mode=0o700)
        self.original = self.base / "message.eml"

    def export(self):
        return eml_export.export_message(
            RAW, mail_root=self.root, account="synthetic", mailbox="INBOX",
            uidvalidity=1, uid=1,
        )

    def seed_original(self, raw):
        self.original.write_bytes(raw)
        self.original.chmod(0o600)
        return self.original.stat().st_ino

    def test_mismatched_original_blocks_new_receipt_without_overwriting(self):
        # Same length: existence and size alone cannot establish byte identity.
        mismatched = RAW.replace(b"Body A", b"Body B")
        inode = self.seed_original(mismatched)

        with self.assertRaisesRegex(mail_delta.Rejected, r"^export_identity_conflict$"):
            self.export()

        self.assertEqual(list(self.base.iterdir()), [self.original])
        self.assertEqual(self.original.read_bytes(), mismatched)
        self.assertEqual(self.original.stat().st_ino, inode)

    def test_identical_original_allows_new_receipt_without_overwriting(self):
        # A matching original left by an interrupted export is safe to reuse.
        inode = self.seed_original(RAW)

        path = self.export()

        self.assertEqual(path, self.base / "receipt.json")
        receipt, _, _ = mail_delta.load_receipt(path)
        self.assertIs(receipt["complete"], True)
        self.assertEqual(receipt["attachments"], [])
        self.assertNotIn("schema", receipt)
        self.assertEqual(receipt["original_eml"], {
            "path": f"{self.sha}/message.eml", "bytes": len(RAW), "sha256": self.sha,
        })
        self.assertEqual(self.original.read_bytes(), RAW)
        self.assertEqual(self.original.stat().st_ino, inode)
